"""Real-time base stream recording and RTCM conversion helpers."""

from __future__ import annotations

import json
import lzma
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from .rtklib import (
    RtklibPathStyle,
    detect_rtklib_path_style,
    executable_exists,
    executable_for_subprocess,
    format_command,
    path_for_rtklib_argument,
    resolve_rtklib_tool,
)


@dataclass(frozen=True)
class BaseRtRecordingResult:
    """Metadata for a recorded NTRIP/RTCM base stream.

    Attributes:
        source_kind: Stable source kind label for downstream tools.
        caster: NTRIP caster host.
        port: NTRIP caster TCP port.
        mountpoint: NTRIP mountpoint.
        station: Optional user station label.
        start_time_utc: Recording start timestamp.
        end_time_utc: Recording end timestamp.
        duration_s: Recording duration in seconds.
        output_rtcm3: Raw RTCM3 output file.
        metadata_json: Metadata JSON file.
        record_log: Recorder process log file.
        rtklib_str2str: Resolved str2str executable.
        bytes_written: RTCM output byte count.
        credential_user_present: True when a username was supplied.
        password_redacted: Always true; passwords are never stored in metadata.
    """

    source_kind: str
    caster: str
    port: int
    mountpoint: str
    station: str | None
    start_time_utc: str
    end_time_utc: str
    duration_s: float
    output_rtcm3: str
    metadata_json: str
    record_log: str
    rtklib_str2str: str
    bytes_written: int
    credential_user_present: bool
    password_redacted: bool = True


PopenFactory = Callable[..., subprocess.Popen]


def _base_rtcm_metadata_candidates(rtcm_path: Path) -> list[Path]:
    """Return sidecar metadata paths used by recorded RTCM streams."""

    candidates = [rtcm_path.with_suffix(".meta.json")]
    name = rtcm_path.name
    for suffix in (".rtcm3.xz", ".rtcm3"):
        if name.endswith(suffix):
            candidates.append(rtcm_path.with_name(name[: -len(suffix)] + ".meta.json"))
    return list(dict.fromkeys(candidates))


def _base_rtcm_approx_time(rtcm_path: Path) -> datetime | None:
    """Return approximate RTCM start time from recording metadata, if available."""

    for candidate in _base_rtcm_metadata_candidates(rtcm_path):
        if not candidate.exists():
            continue
        try:
            metadata = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("could not read base RTCM metadata sidecar: %s", candidate)
            continue
        value = metadata.get("start_time_utc")
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logging.warning("could not parse base RTCM start_time_utc from %s: %s", candidate, value)
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def build_ntrip_url(*, caster: str, port: int, mountpoint: str, user: str | None, password: str | None) -> str:
    """Return a RTKLIB str2str NTRIP input URL."""

    credentials = ""
    if user:
        credentials = user
        if password:
            credentials += f":{password}"
        credentials += "@"
    return f"ntrip://{credentials}{caster}:{port}/{mountpoint}"


def redact_ntrip_url(url: str) -> str:
    """Return an NTRIP URL safe for logs."""

    prefix = "ntrip://"
    if not url.startswith(prefix) or "@" not in url:
        return url
    return prefix + "***:***@" + url.split("@", 1)[1]


def build_str2str_record_command(
    *,
    str2str: str,
    caster: str,
    port: int,
    mountpoint: str,
    user: str | None,
    password: str | None,
    output_rtcm3: Path,
    path_style: RtklibPathStyle = "auto",
) -> tuple[list[str], list[str]]:
    """Build real and redacted `str2str` argv vectors for base recording."""

    resolved_style = detect_rtklib_path_style(str2str, path_style)
    ntrip_url = build_ntrip_url(caster=caster, port=port, mountpoint=mountpoint, user=user, password=password)
    output_url = "file://" + path_for_rtklib_argument(output_rtcm3, resolved_style)
    command = [executable_for_subprocess(str2str), "-in", ntrip_url, "-out", output_url]
    redacted = [command[0], "-in", redact_ntrip_url(ntrip_url), "-out", output_url]
    return command, redacted


def record_ntrip_base(
    *,
    caster: str,
    port: int,
    mountpoint: str,
    out_dir: Path,
    station: str | None = None,
    user: str | None = None,
    password: str | None = None,
    str2str: str = "str2str",
    rtklib_dir: str | Path | None = None,
    path_style: RtklibPathStyle = "auto",
    popen_factory: PopenFactory = subprocess.Popen,
    terminate_timeout_s: float = 5.0,
) -> BaseRtRecordingResult:
    """Record an NTRIP mountpoint to a raw RTCM3 file with RTKLIB `str2str`.

    Raises:
        FileNotFoundError: If `str2str` cannot be resolved.
        RuntimeError: If recording creates no RTCM bytes.
    """

    resolved_str2str = resolve_rtklib_tool(str2str, rtklib_dir=rtklib_dir)
    if not executable_exists(resolved_str2str):
        raise FileNotFoundError(f"str2str executable missing: {resolved_str2str}")
    out_dir.mkdir(parents=True, exist_ok=True)
    start = datetime.now(UTC)
    stem = f"{mountpoint}_{start.strftime('%Y%m%dT%H%M%SZ')}"
    output_rtcm3 = out_dir / f"{stem}.rtcm3"
    metadata_json = out_dir / f"{stem}.meta.json"
    record_log = out_dir / f"{stem}.record.log"
    command, redacted = build_str2str_record_command(
        str2str=resolved_str2str,
        caster=caster,
        port=port,
        mountpoint=mountpoint,
        user=user,
        password=password,
        output_rtcm3=output_rtcm3,
        path_style=path_style,
    )
    logging.info("starting real-time base recording: %s", format_command(redacted))
    logging.info("real-time base output: %s", output_rtcm3)
    start_monotonic = time.monotonic()
    returncode: int | None = None
    with record_log.open("w", encoding="utf-8") as log_file:
        log_file.write(format_command(redacted) + "\n")
        log_file.flush()
        process = popen_factory(  # noqa: S603 - user-selected local RTKLIB executable.
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            returncode = process.wait()
        except KeyboardInterrupt:
            logging.info("stopping real-time base recording after Ctrl+C")
            process.terminate()
            try:
                returncode = process.wait(timeout=terminate_timeout_s)
            except subprocess.TimeoutExpired:
                logging.warning("str2str did not stop within %.1fs; killing it", terminate_timeout_s)
                process.kill()
                returncode = process.wait()
    end = datetime.now(UTC)
    duration_s = max(0.0, time.monotonic() - start_monotonic)
    bytes_written = output_rtcm3.stat().st_size if output_rtcm3.exists() else 0
    result = BaseRtRecordingResult(
        source_kind="ntrip-realtime-recording",
        caster=caster,
        port=port,
        mountpoint=mountpoint,
        station=station,
        start_time_utc=start.isoformat(),
        end_time_utc=end.isoformat(),
        duration_s=duration_s,
        output_rtcm3=str(output_rtcm3),
        metadata_json=str(metadata_json),
        record_log=str(record_log),
        rtklib_str2str=resolved_str2str,
        bytes_written=bytes_written,
        credential_user_present=bool(user),
        password_redacted=True,
    )
    metadata_json.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if bytes_written <= 0:
        raise RuntimeError(
            f"real-time base recording produced no RTCM data: {output_rtcm3}; "
            f"str2str_returncode={returncode} log={record_log}"
        )
    if returncode not in {0, None}:
        logging.warning("str2str exited with code %s after writing %d bytes", returncode, bytes_written)
    return result


def convert_rtcm_to_rinex(
    *,
    rtcm_path: Path,
    out_dir: Path,
    basename: str,
    convbin: str = "convbin",
    rtklib_dir: str | Path | None = None,
    path_style: RtklibPathStyle = "auto",
) -> tuple[Path, list[Path]]:
    """Convert a recorded RTCM3 base stream to RINEX OBS/NAV with `convbin`."""

    resolved_convbin = resolve_rtklib_tool(convbin, rtklib_dir=rtklib_dir)
    if not executable_exists(resolved_convbin):
        raise FileNotFoundError(f"convbin executable missing: {resolved_convbin}")
    if not rtcm_path.exists() or rtcm_path.stat().st_size <= 0:
        raise FileNotFoundError(f"base RTCM input is missing or empty: {rtcm_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    convbin_input = rtcm_path
    if rtcm_path.suffix.lower() == ".xz":
        convbin_input = out_dir / f"{basename}.input.rtcm3"
        with lzma.open(rtcm_path, "rb") as source, convbin_input.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        if convbin_input.stat().st_size <= 0:
            raise RuntimeError(f"decompressed base RTCM input is empty: {rtcm_path}")
        source_stat = rtcm_path.stat()
        os.utime(convbin_input, (source_stat.st_atime, source_stat.st_mtime))
        logging.info("decompressed base RTCM for convbin: %s -> %s", rtcm_path, convbin_input)
    obs = out_dir / f"{basename}.base.obs"
    nav = out_dir / f"{basename}.base.nav"
    resolved_style = detect_rtklib_path_style(resolved_convbin, path_style)
    command = [
        executable_for_subprocess(resolved_convbin),
        path_for_rtklib_argument(convbin_input, resolved_style),
        "-r",
        "rtcm3",
        "-od",
        "-os",
        "-oi",
        "-ot",
        "-o",
        path_for_rtklib_argument(obs, resolved_style),
        "-n",
        path_for_rtklib_argument(nav, resolved_style),
    ]
    approx_time = _base_rtcm_approx_time(rtcm_path)
    if approx_time is not None:
        command[2:2] = ["-tr", approx_time.strftime("%Y/%m/%d"), approx_time.strftime("%H:%M:%S")]
    logging.info("converting recorded base RTCM to RINEX: %s", format_command(command))
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    stdout_log = obs.with_suffix(".convbin.stdout.log")
    stderr_log = obs.with_suffix(".convbin.stderr.log")
    stdout_log.write_text(result.stdout, encoding="utf-8")
    stderr_log.write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"convbin failed for base RTCM {rtcm_path}: {result.stderr.strip() or result.stdout.strip()}\n"
            f"command: {format_command(command)}\nstdout log: {stdout_log}\nstderr log: {stderr_log}"
        )
    if not obs.exists() or obs.stat().st_size <= 0:
        raise RuntimeError(f"convbin did not create a non-empty base OBS file: {obs}")
    nav_files = [path for path in [nav] if path.exists() and path.stat().st_size > 0]
    return obs, nav_files


def fetch_ntrip_sourcetable(
    *,
    caster: str,
    port: int,
    out: Path,
    contains: list[str] | None = None,
    user: str | None = None,
    password: str | None = None,
    timeout_s: float = 20.0,
) -> str:
    """Fetch and store a raw NTRIP sourcetable."""

    url = build_ntrip_url(caster=caster, port=port, mountpoint="", user=user, password=password).rstrip("/")
    request = Request(url, headers={"Ntrip-Version": "Ntrip/2.0", "User-Agent": "um980-ppk"})  # noqa: S310
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - explicit user-triggered caster request.
        text = response.read().decode("utf-8", errors="replace")
    if contains:
        lowered = [item.lower() for item in contains]
        lines = [line for line in text.splitlines() if any(item in line.lower() for item in lowered)]
        stored = "\n".join(lines) + ("\n" if lines else "")
    else:
        stored = text
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(stored, encoding="utf-8")
    return stored
