"""Safe RTKLIB command assembly and execution."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from hashlib import sha256
import logging
import shlex
from os import access, environ, X_OK
from pathlib import Path
from typing import Literal

from .files import classify_rinex_file, has_unresolved_wildcard
from .trace_quality import analyze_rtklib_trace

RtklibPathStyle = Literal["auto", "unix", "windows"]

USER_RTKLIB_BIN = Path.home() / "RTKLIB-ex-bin" / "bin"
LOCAL_RTKLIB_BIN = Path("build-tools/RTKLIB-ex-bin/bin")


@dataclass(frozen=True)
class RtklibCommand:
    """Executed or prepared RTKLIB command metadata.

    Attributes:
        args: Argument vector passed to `subprocess.run`.
        output_file: Expected RTKLIB solution output path.
        stdout_log: Captured RTKLIB stdout log path.
        stderr_log: Captured RTKLIB stderr log path.
        wrapper_file: Reproducible shell wrapper containing the same command.
    """

    args: list[str]
    output_file: Path
    stdout_log: Path
    stderr_log: Path
    wrapper_file: Path
    trace_file: Path | None = None
    trace_generated_temporarily: bool = False
    trace_retained: bool = False
    trace_effective_level: int | None = None
    trace_summary: dict[str, object] | None = None
    generated_stat_files: list[Path] = field(default_factory=list)
    deleted_stat_files: list[Path] = field(default_factory=list)


def is_cygwin() -> bool:
    """Return true when Python is running under Cygwin."""

    return sys.platform.startswith("cygwin")


def is_termux() -> bool:
    """Return true when Python is running in an Android/Termux environment."""

    if is_cygwin() or sys.platform != "linux":
        return False
    prefix = environ.get("PREFIX", "")
    return (
        "/com.termux/" in prefix
        or prefix.startswith("/data/data/com.termux/")
        or Path("/data/data/com.termux/files/usr").exists()
    )


def is_windows_path(value: str) -> bool:
    """Return true for drive-letter or UNC Windows path strings."""

    return (
        len(value) >= 3
        and value[1] == ":"
        and value[0].isalpha()
        and value[2] in {"\\", "/"}
    ) or value.startswith("\\\\")


def cygdrive_to_windows(value: str) -> str | None:
    """Convert `/cygdrive/c/path` to `C:\\path` without invoking `cygpath`."""

    prefix = "/cygdrive/"
    if not value.startswith(prefix) or len(value) < len(prefix) + 2:
        return None
    drive = value[len(prefix)]
    rest = value[len(prefix) + 1 :].replace("/", "\\")
    return f"{drive.upper()}:\\{rest.lstrip('\\')}"


def _run_cygpath(flag: str, value: str) -> str | None:
    try:
        result = subprocess.run(
            ["cygpath", flag, value],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    converted = result.stdout.strip()
    return converted or None


def detect_rtklib_path_style(rnx2rtkp: str, requested: RtklibPathStyle = "auto") -> Literal["unix", "windows"]:
    """Resolve `auto` path style for RTKLIB command arguments.

    Native Linux/macOS RTKLIB builds need POSIX paths. Windows RTKLIB binaries,
    including `.exe` or PE binaries launched from Cygwin, need Windows paths for
    input, output, and config files. The executable path itself is handled
    separately.
    """

    if requested != "auto":
        return requested
    lower = rnx2rtkp.lower()
    if sys.platform == "win32":
        return "windows"
    if is_cygwin() and (lower.endswith(".exe") or is_windows_executable(rnx2rtkp)):
        return "windows"
    return "unix"


def is_windows_executable(executable: str) -> bool:
    """Return true when `executable` appears to be a Windows PE binary."""

    try:
        with Path(executable).open("rb") as file:
            return file.read(2) == b"MZ"
    except OSError:
        return False


def path_for_rtklib_argument(path: Path, style: Literal["unix", "windows"]) -> str:
    """Return a path string suitable for RTKLIB command-line arguments."""

    value = str(path)
    if style == "unix":
        return value
    if is_windows_path(value):
        return value.replace("/", "\\")
    if is_cygwin():
        if has_unresolved_wildcard(path):
            return cygwin_wildcard_to_windows(path)
        converted = _run_cygpath("-w", value)
        if converted:
            return converted
    cygdrive = cygdrive_to_windows(value)
    if cygdrive:
        return cygdrive
    if sys.platform == "win32":
        return str(Path(value))
    return value


def cygwin_wildcard_to_windows(path: Path) -> str:
    """Convert a Cygwin wildcard path while preserving wildcard characters."""

    parent = path.parent
    parent_value = str(parent if str(parent) else Path("."))
    if parent_value == ".":
        parent_value = "."
    converted_parent = _run_cygpath("-w", parent_value)
    if converted_parent:
        return converted_parent.rstrip("\\/") + "\\" + path.name
    cygdrive = cygdrive_to_windows(parent_value)
    if cygdrive:
        return cygdrive.rstrip("\\/") + "\\" + path.name
    return str(path).replace("/", "\\")


def executable_for_subprocess(executable: str) -> str:
    """Return an executable path usable by the current Python subprocess."""

    if is_cygwin() and is_windows_path(executable):
        converted = _run_cygpath("-u", executable)
        if converted:
            return converted
    path = Path(executable)
    if path.exists() and not access(path, X_OK) and can_mirror_non_executable_tool(path):
        return str(mirror_non_executable_tool(path))
    return executable


def executable_exists(executable: str) -> bool:
    """Check executable availability across POSIX, Windows, and Cygwin paths."""

    if shutil.which(executable):
        return True
    path = Path(executable)
    if is_cygwin() and path.exists():
        return True
    if path.exists() and (access(path, X_OK) or can_mirror_non_executable_tool(path)):
        return True
    if is_cygwin() and is_windows_path(executable):
        converted = _run_cygpath("-u", executable)
        return bool(converted and Path(converted).exists())
    return False


def can_mirror_non_executable_tool(path: Path) -> bool:
    """Return true if a readable local tool can be mirrored to executable storage."""

    return is_termux() and path.is_file()


def mirror_non_executable_tool(path: Path) -> Path:
    """Copy a readable tool to executable temp storage and return that path.

    Android shared storage often strips execute bits and may be mounted noexec.
    This keeps `build-tools/RTKLIB-ex-bin/` as the local installation source
    while running a mirrored copy from Termux-private executable storage.
    """

    source = path.resolve()
    digest = sha256(str(source).encode("utf-8")).hexdigest()[:16]
    mirror_dir = Path("/data/data/com.termux/files/usr/tmp") / "um980-rtklib-tools" / digest
    mirror_dir.mkdir(parents=True, exist_ok=True)
    target = mirror_dir / path.name
    if (
        not target.exists()
        or target.stat().st_size != source.stat().st_size
        or int(target.stat().st_mtime) < int(source.stat().st_mtime)
    ):
        shutil.copyfile(source, target)
        target.chmod(0o755)
    return target


def resolve_rtklib_tool(
    tool: str,
    *,
    rtklib_dir: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str:
    """Resolve a RTKLIB executable from explicit, local, or system locations.

    Resolution order is:
    1. explicit path supplied as `tool`;
    2. `rtklib_dir/tool` when `--rtklib-dir` is supplied and `tool` is bare;
    3. user-local `~/RTKLIB-ex-bin/bin/tool`;
    4. repository-local `build-tools/RTKLIB-ex-bin/bin/tool`;
    5. the original bare tool name for normal `PATH` lookup.
    """

    tool_path = Path(tool)
    if tool_path.parent != Path("."):
        return tool
    if rtklib_dir:
        candidate = Path(rtklib_dir) / tool
        if candidate.exists():
            return str(candidate)
        return str(candidate)
    user_candidate = USER_RTKLIB_BIN / tool
    if user_candidate.exists():
        return str(user_candidate)
    root = Path(cwd) if cwd is not None else Path.cwd()
    local_candidate = root / LOCAL_RTKLIB_BIN / tool
    if local_candidate.exists():
        return str(local_candidate)
    return tool


def _has_rinex_body_records(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except OSError:
        return False
    in_body = False
    for line in lines:
        if in_body and line.strip():
            return True
        if "END OF HEADER" in line:
            in_body = True
    return False


def validate_rtklib_inputs(
    *,
    rnx2rtkp: str,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
) -> None:
    """Validate local files before any path conversion for RTKLIB execution."""

    if not executable_exists(rnx2rtkp):
        raise FileNotFoundError(f"rnx2rtkp executable missing: {rnx2rtkp}")
    all_paths = [rover_obs, *base_obs, *nav_files]
    for path in all_paths:
        if has_unresolved_wildcard(path):
            raise ValueError(f"unresolved wildcard in RTKLIB input path: {path}")
        if not path.exists():
            raise FileNotFoundError(f"RTKLIB input does not exist: {path}")
    if classify_rinex_file(rover_obs) != "obs":
        raise ValueError(f"rover observation file is not RINEX OBS: {rover_obs}")
    if not base_obs:
        raise ValueError("no base observation files supplied")
    for path in base_obs:
        if classify_rinex_file(path) != "obs":
            raise ValueError(f"base file is not RINEX OBS: {path}")
    if not nav_files:
        raise ValueError("no NAV/SP3/CLK/SBS files supplied")
    for path in nav_files:
        kind = classify_rinex_file(path)
        if kind == "nav" and not _has_rinex_body_records(path):
            raise ValueError(f"NAV file has no data records after END OF HEADER: {path}")
        if kind == "sbs" and path.stat().st_size == 0:
            raise ValueError(f"SBAS message file is empty: {path}")
        if kind not in {"nav", "sp3", "clk", "sbs"}:
            raise ValueError(f"NAV/SP3/CLK/SBS file could not be classified: {path}")


def build_rnx2rtkp_command(
    *,
    rnx2rtkp: str,
    rtkconf: Path | None,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    base_obs_arg: Path | None = None,
    rtk_options: list[str] | None = None,
    base_ecef_xyz_m: tuple[float, float, float] | None = None,
    base_llh: tuple[float, float, float] | None = None,
    path_style: RtklibPathStyle = "auto",
) -> list[str]:
    """Build an `rnx2rtkp` argv list with platform-appropriate path strings.

    Args:
        rnx2rtkp: RTKLIB executable path or command name.
        rtkconf: Optional RTKLIB configuration file.
        output_file: Destination solution path.
        rover_obs: Rover RINEX observation file.
        base_obs: Concrete base RINEX OBS files validated before invocation.
        nav_files: Concrete NAV/SP3/CLK/SBS files validated before invocation.
        base_obs_arg: Optional single RTKLIB argument representing all base
            observations, typically a tool-expanded wildcard. The concrete
            `base_obs` list remains the validation source.
        rtk_options: Command-line RTKLIB processing options.
        base_ecef_xyz_m: Optional fixed base position in ECEF meters.
        base_llh: Optional fixed base position as latitude, longitude, height.
        path_style: Path conversion style for RTKLIB arguments.

    Returns:
        Argument vector ready for `subprocess.run`.
    """

    if base_ecef_xyz_m is not None and base_llh is not None:
        raise ValueError("base_ecef_xyz_m and base_llh are mutually exclusive")
    resolved_style = detect_rtklib_path_style(rnx2rtkp, path_style)
    args = [
        executable_for_subprocess(rnx2rtkp),
        "-o",
        path_for_rtklib_argument(output_file, resolved_style),
    ]
    if rtkconf is not None:
        args[1:1] = ["-k", path_for_rtklib_argument(rtkconf, resolved_style)]
    if rtk_options:
        args[1:1] = list(rtk_options)
    if base_ecef_xyz_m is not None:
        args.extend(["-r", *(f"{value:.4f}" for value in base_ecef_xyz_m)])
    if base_llh is not None:
        args.extend(["-l", f"{base_llh[0]:.10f}", f"{base_llh[1]:.10f}", f"{base_llh[2]:.4f}"])
    args.append(path_for_rtklib_argument(rover_obs, resolved_style))
    if base_obs_arg is not None:
        args.append(path_for_rtklib_argument(base_obs_arg, resolved_style))
    else:
        args.extend(path_for_rtklib_argument(path, resolved_style) for path in base_obs)
    args.extend(path_for_rtklib_argument(path, resolved_style) for path in nav_files)
    return args


def write_wrapper(path: Path, args: list[str]) -> None:
    """Write a shell wrapper for a prepared RTKLIB command.

    Args:
        path: Destination wrapper path.
        args: Command argument vector to quote into the wrapper.
    """

    path.write_text("#!/bin/sh\nset -eu\n" + format_command(args) + "\n", encoding="utf-8")
    path.chmod(0o755)


def format_command(args: list[str]) -> str:
    """Return a shell-quoted representation of an argument vector."""

    return shlex.join(args)


def _has_rtklib_solution_rows(path: Path) -> bool:
    """Return true when an RTKLIB `.pos`/`.llh` style output has data rows."""

    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except OSError:
        return False
    return any(line.strip() and not line.startswith("%") for line in lines)


def _warn_about_rtklib_result(output_file: Path, stderr: str) -> None:
    """Log predictable RTKLIB success-with-bad-output situations."""

    invalid_options = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip().startswith("invalid option value")
    ]
    for line in invalid_options:
        logging.warning("RTKLIB reported an invalid configuration option: %s", line)
    if not output_file.exists():
        logging.warning("RTKLIB completed but did not create output file: %s", output_file)
        return
    if not _has_rtklib_solution_rows(output_file):
        logging.warning(
            "RTKLIB output has no solution rows: %s. Check rover/base time overlap, "
            "NAV coverage, RINEX signal mappings, and RTKLIB configuration.",
            output_file,
        )
    usable_quality_seen = any(f"Q={quality}" in stderr for quality in (1, 2, 4, 5))
    if "Q=0" in stderr and not usable_quality_seen:
        logging.warning("RTKLIB progress reported only Q=0 epochs; no usable solution was produced.")


def _recover_output_from_stdout(output_file: Path, stdout: str) -> bool:
    """Write captured RTKLIB stdout to `output_file` when `-o` was not honored."""

    if output_file.exists() or not stdout.strip():
        return False
    output_file.write_text(stdout, encoding="utf-8")
    logging.warning(
        "RTKLIB did not create %s but wrote data to stdout; saved captured stdout to the requested output file.",
        output_file,
    )
    return True


def _raise_missing_rtklib_output(output_file: Path, stdout_log: Path, stderr_log: Path, wrapper_file: Path, args: list[str]) -> None:
    """Raise a clear error when RTKLIB exits successfully without output."""

    raise RuntimeError(
        "rnx2rtkp completed with exit code 0 but did not create the requested output file: "
        f"{output_file}\n"
        f"command: {format_command(args)}\n"
        f"stdout log: {stdout_log}\n"
        f"stderr log: {stderr_log}\n"
        f"wrapper: {wrapper_file}"
    )


def _resolve_wildcard_parent(path: Path) -> Path:
    """Return an absolute wildcard path while preserving literal wildcard name."""

    if has_unresolved_wildcard(path):
        return path.parent.resolve() / path.name
    return path.resolve()


def _effective_trace_level(trace_mode: str, trace_level: int | None) -> int | None:
    if trace_mode not in {"off", "temporary", "keep"}:
        raise ValueError(f"unsupported RTKLIB trace mode for execution: {trace_mode}")
    if trace_mode == "off":
        return None
    if trace_level is None:
        return 3
    if trace_level <= 0:
        raise ValueError("--rtklib-trace-level must be greater than 0 when trace generation is requested")
    if trace_level == 1:
        logging.warning("RTKLIB trace level 1 is likely too sparse for useful quality diagnostics")
    if trace_level >= 4:
        logging.warning("RTKLIB trace level %d may create very large trace files", trace_level)
    return trace_level


def _find_trace_candidate(directory: Path) -> Path | None:
    """Find the most likely RTKLIB trace file in an isolated run directory."""

    candidates: dict[Path, None] = {}
    for pattern in ("rnx2rtkp.trace", "*.trace", "trace*"):
        for path in directory.glob(pattern):
            if path.is_file() and path.stat().st_size > 0:
                candidates[path] = None
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def run_rnx2rtkp(
    *,
    rnx2rtkp: str,
    rtkconf: Path | None,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    base_obs_arg: Path | None = None,
    rtk_options: list[str] | None = None,
    base_ecef_xyz_m: tuple[float, float, float] | None = None,
    base_llh: tuple[float, float, float] | None = None,
    path_style: RtklibPathStyle = "auto",
    dry_run: bool = False,
    debug: bool = False,
    trace_mode: Literal["off", "temporary", "keep"] = "off",
    trace_level: int | None = None,
    trace_file: Path | None = None,
    trace_cleanup: Literal["always", "on-success", "never"] = "always",
) -> RtklibCommand:
    """Validate, prepare, and optionally run `rnx2rtkp`.

    Args:
        rnx2rtkp: RTKLIB executable path or command name.
        rtkconf: Optional RTKLIB configuration file.
        output_file: Destination solution path.
        rover_obs: Rover RINEX observation file.
        base_obs: Base RINEX observation files.
        nav_files: NAV/SP3/CLK/SBS files passed to RTKLIB.
        base_obs_arg: Optional single RTKLIB argument representing all base
            observations while `base_obs` supplies validation inputs.
        rtk_options: Command-line RTKLIB processing options used when no
            configuration file is supplied.
        base_ecef_xyz_m: Optional fixed base position in ECEF meters.
        base_llh: Optional fixed base position as latitude, longitude, height.
        path_style: Path conversion style for RTKLIB arguments.
        dry_run: When true, only validate and write the wrapper.
        debug: Log the exact command and output log paths before execution.

    Returns:
        Prepared command metadata.

    Raises:
        FileNotFoundError: If required inputs or executable are missing.
        ValueError: If inputs are invalid or ambiguous.
        RuntimeError: If RTKLIB exits with a non-zero status.
    """

    validate_rtklib_inputs(rnx2rtkp=rnx2rtkp, rover_obs=rover_obs, base_obs=base_obs, nav_files=nav_files)
    if rtkconf is not None and not rtkconf.exists():
        raise FileNotFoundError(f"RTKLIB configuration file does not exist: {rtkconf}")
    if rtkconf is None and not rtk_options:
        raise ValueError("RTKLIB requires either --rtkconf or generated command-line RTK options")
    effective_trace_level = _effective_trace_level(trace_mode, trace_level)
    run_cwd: Path | None = None
    cleanup_trace_dir = False
    trace_dir: Path | None = None
    if trace_mode in {"temporary", "keep"}:
        trace_dir = Path(tempfile.mkdtemp(prefix="um980-rtklib-trace-"))
        run_cwd = trace_dir
        cleanup_trace_dir = trace_mode == "temporary" and trace_cleanup == "always"
        rtk_options = ["-x", str(effective_trace_level), *(rtk_options or [])]
        rnx2rtkp_path = Path(rnx2rtkp)
        if rnx2rtkp_path.exists() and not rnx2rtkp_path.is_absolute():
            rnx2rtkp = str(rnx2rtkp_path.resolve())
        output_file = output_file.resolve()
        rover_obs = rover_obs.resolve()
        base_obs = [path.resolve() for path in base_obs]
        nav_files = [path.resolve() for path in nav_files]
        if rtkconf is not None:
            rtkconf = rtkconf.resolve()
        if base_obs_arg is not None:
            base_obs_arg = _resolve_wildcard_parent(base_obs_arg)
        logging.info(
            "quality_trace=%s requested_rtklib_trace_level=%s effective_rtklib_trace_level=%s",
            trace_mode,
            trace_level if trace_level is not None else "<unset>",
            effective_trace_level,
        )

    args = build_rnx2rtkp_command(
        rnx2rtkp=rnx2rtkp,
        rtkconf=rtkconf,
        output_file=output_file,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        base_obs_arg=base_obs_arg,
        rtk_options=rtk_options,
        base_ecef_xyz_m=base_ecef_xyz_m,
        base_llh=base_llh,
        path_style=path_style,
    )
    stdout_log = output_file.with_suffix(".rtklib.stdout.log")
    stderr_log = output_file.with_suffix(".rtklib.stderr.log")
    wrapper_file = output_file.with_suffix(".rtkpost-wrapper.sh")
    write_wrapper(wrapper_file, args)
    if debug:
        logging.info("RTKLIB command: %s", format_command(args))
        logging.info("RTKLIB wrapper: %s", wrapper_file)
        logging.info("RTKLIB stdout log: %s", stdout_log)
        logging.info("RTKLIB stderr log: %s", stderr_log)
    if not dry_run:
        logging.debug("executing RTKLIB argv: %r", args)
        result = subprocess.run(args, check=False, capture_output=True, text=True, cwd=run_cwd)
        stdout_log.write_text(result.stdout, encoding="utf-8")
        stderr_log.write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            detail = stderr or stdout or "no stdout/stderr output captured"
            raise RuntimeError(
                f"rnx2rtkp failed with exit code {result.returncode}: {detail}\n"
                f"command: {format_command(args)}\n"
                f"stdout log: {stdout_log}\n"
                f"stderr log: {stderr_log}\n"
                f"wrapper: {wrapper_file}"
            )
        _recover_output_from_stdout(output_file, result.stdout)
        _warn_about_rtklib_result(output_file, result.stderr)
        if not output_file.exists():
            _raise_missing_rtklib_output(output_file, stdout_log, stderr_log, wrapper_file, args)
    trace_summary: dict[str, object] | None = None
    selected_trace: Path | None = None
    trace_retained = False
    if trace_dir is not None and not dry_run:
        selected_trace = _find_trace_candidate(trace_dir)
        if selected_trace is not None:
            retained_path: Path | None = None
            if trace_mode == "keep":
                retained_path = trace_file or output_file.with_suffix(".trace")
                retained_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(selected_trace), retained_path)
                selected_trace = retained_path
                trace_retained = True
            trace_summary = analyze_rtklib_trace(selected_trace)
            trace_summary.update(
                {
                    "source": trace_mode,
                    "generated_temporarily": trace_mode == "temporary",
                    "retained": trace_retained,
                    "path": str(selected_trace) if trace_retained else None,
                    "effective_level": effective_trace_level,
                }
            )
            logging.info(
                "parsed RTKLIB trace diagnostics: mode=%s level=%s retained=%s file=%s",
                trace_mode,
                effective_trace_level,
                trace_retained,
                selected_trace if trace_retained else "<temporary>",
            )
        else:
            logging.warning("RTKLIB trace mode %s was requested, but no trace file was found in %s", trace_mode, trace_dir)
        if trace_mode == "temporary" and trace_cleanup == "on-success":
            cleanup_trace_dir = output_file.exists()
        if trace_mode == "temporary" and trace_cleanup == "never":
            cleanup_trace_dir = False
            logging.info("temporary RTKLIB trace directory retained by policy: %s", trace_dir)
        if cleanup_trace_dir:
            shutil.rmtree(trace_dir, ignore_errors=True)
    return RtklibCommand(
        args,
        output_file,
        stdout_log,
        stderr_log,
        wrapper_file,
        trace_file=selected_trace if trace_retained else None,
        trace_generated_temporarily=trace_mode == "temporary",
        trace_retained=trace_retained,
        trace_effective_level=effective_trace_level,
        trace_summary=trace_summary,
        generated_stat_files=[candidate for candidate in (Path(str(output_file) + ".stat"), output_file.with_suffix(".stat")) if candidate.exists()],
    )
