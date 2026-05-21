"""Safe RTKLIB command assembly and execution."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .files import classify_rinex_file, has_unresolved_wildcard

RtklibPathStyle = Literal["auto", "unix", "windows"]


@dataclass(frozen=True)
class RtklibCommand:
    args: list[str]
    output_file: Path
    stdout_log: Path
    stderr_log: Path
    wrapper_file: Path


def is_cygwin() -> bool:
    """Return true when Python is running under Cygwin."""

    return sys.platform.startswith("cygwin")


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
    including `.exe` launched from Cygwin, need Windows paths for input, output,
    and config files. The executable path itself is handled separately.
    """

    if requested != "auto":
        return requested
    lower = rnx2rtkp.lower()
    if sys.platform == "win32":
        return "windows"
    if is_cygwin() and lower.endswith(".exe"):
        return "windows"
    return "unix"


def path_for_rtklib_argument(path: Path, style: Literal["unix", "windows"]) -> str:
    """Return a path string suitable for RTKLIB command-line arguments."""

    value = str(path)
    if style == "unix":
        return value
    if is_windows_path(value):
        return value.replace("/", "\\")
    if is_cygwin():
        converted = _run_cygpath("-w", value)
        if converted:
            return converted
    cygdrive = cygdrive_to_windows(value)
    if cygdrive:
        return cygdrive
    if sys.platform == "win32":
        return str(Path(value))
    return value


def executable_for_subprocess(executable: str) -> str:
    """Return an executable path usable by the current Python subprocess."""

    if is_cygwin() and is_windows_path(executable):
        converted = _run_cygpath("-u", executable)
        if converted:
            return converted
    return executable


def executable_exists(executable: str) -> bool:
    """Check executable availability across POSIX, Windows, and Cygwin paths."""

    if shutil.which(executable):
        return True
    if Path(executable).exists():
        return True
    if is_cygwin() and is_windows_path(executable):
        converted = _run_cygpath("-u", executable)
        return bool(converted and Path(converted).exists())
    return False


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
        if kind not in {"nav", "sp3", "clk", "sbs"}:
            raise ValueError(f"NAV/SP3/CLK/SBS file could not be classified: {path}")


def build_rnx2rtkp_command(
    *,
    rnx2rtkp: str,
    rtkconf: Path,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    path_style: RtklibPathStyle = "auto",
) -> list[str]:
    """Build an `rnx2rtkp` argv list with platform-appropriate path strings."""

    resolved_style = detect_rtklib_path_style(rnx2rtkp, path_style)
    return [
        executable_for_subprocess(rnx2rtkp),
        "-k",
        path_for_rtklib_argument(rtkconf, resolved_style),
        "-o",
        path_for_rtklib_argument(output_file, resolved_style),
        path_for_rtklib_argument(rover_obs, resolved_style),
        *[path_for_rtklib_argument(path, resolved_style) for path in base_obs],
        *[path_for_rtklib_argument(path, resolved_style) for path in nav_files],
    ]


def write_wrapper(path: Path, args: list[str]) -> None:
    quoted = " ".join("'" + arg.replace("'", "'\"'\"'") + "'" for arg in args)
    path.write_text("#!/bin/sh\nset -eu\n" + quoted + "\n", encoding="utf-8")
    path.chmod(0o755)


def run_rnx2rtkp(
    *,
    rnx2rtkp: str,
    rtkconf: Path,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    path_style: RtklibPathStyle = "auto",
    dry_run: bool = False,
) -> RtklibCommand:
    validate_rtklib_inputs(rnx2rtkp=rnx2rtkp, rover_obs=rover_obs, base_obs=base_obs, nav_files=nav_files)
    args = build_rnx2rtkp_command(
        rnx2rtkp=rnx2rtkp,
        rtkconf=rtkconf,
        output_file=output_file,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        path_style=path_style,
    )
    stdout_log = output_file.with_suffix(".rtklib.stdout.log")
    stderr_log = output_file.with_suffix(".rtklib.stderr.log")
    wrapper_file = output_file.with_suffix(".rtkpost-wrapper.sh")
    write_wrapper(wrapper_file, args)
    if not dry_run:
        result = subprocess.run(args, check=False, capture_output=True, text=True)
        stdout_log.write_text(result.stdout, encoding="utf-8")
        stderr_log.write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"rnx2rtkp failed with exit code {result.returncode}: {result.stderr.strip()}")
    return RtklibCommand(args, output_file, stdout_log, stderr_log, wrapper_file)
