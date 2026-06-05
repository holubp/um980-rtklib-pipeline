"""Rootless Termux USB capture wrapper for UM980 receivers."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .capture_profiles import CaptureProfile, parse_capture_profile
from .capture_validate import CaptureValidationResult, validate_capture_file
from .rtklib import executable_for_subprocess, mirror_non_executable_tool


@dataclass(frozen=True)
class CaptureUsbOptions:
    """Options for invoking the native Termux USB fd helper."""

    termux_device: str | None
    duration_s: float = 20.0
    out: Path | None = None
    native_helper: Path = Path("tools/termux/um980-usb-fd")
    profile: Path | None = None
    probe: bool = False
    dry_run_profile: bool = False
    analysis_json: Path | None = None
    validate: bool = False
    extract_check: bool = False
    expect_min_bytes: int = 0
    expect_mode: str = "passive"
    expect_messages: tuple[str, ...] = ()
    profile_line_delay_ms: int | None = None
    capture_after_profile_delay_ms: int | None = None
    read_timeout_ms: int | None = None
    max_bytes: int | None = None
    interface: int | None = None
    altsetting: int | None = None
    ep_in: str | None = None
    ep_out: str | None = None
    verbose: bool = False


@dataclass(frozen=True)
class CaptureUsbResult:
    """Result of a rootless Termux capture wrapper run."""

    usb_analysis: dict[str, object] | None = None
    validation: CaptureValidationResult | None = None
    extract_check: dict[str, object] | None = None
    profile: CaptureProfile | None = None
    command: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-compatible summary."""

        return {
            "usb": self.usb_analysis,
            "validation": self.validation.as_dict() if self.validation else None,
            "extract_check": self.extract_check,
            "profile": {
                "path": str(self.profile.path),
                "enabled": self.profile.enabled,
                "mode": self.profile.mode,
                "commands": list(self.profile.commands),
                "warnings": list(self.profile.warnings),
            }
            if self.profile
            else None,
            "command": self.command,
        }


def run_capture_usb(options: CaptureUsbOptions) -> CaptureUsbResult:
    """Run rootless Termux UM980 USB capture and optional validation."""

    profile = parse_capture_profile(options.profile) if options.profile else None
    if options.dry_run_profile:
        if profile is None:
            raise ValueError("--dry-run-profile requires --profile")
        return CaptureUsbResult(profile=profile)
    if not options.termux_device:
        raise ValueError("--termux-device is required for USB capture/probe")
    if not options.probe and options.out is None:
        raise ValueError("--out is required unless --probe is used")
    if profile is not None and not profile.enabled:
        raise ValueError(f"profile is disabled and will not be sent: {profile.path}")

    ensure_native_helper(options.native_helper)
    usb_analysis_path = _usb_analysis_path(options)
    command = _termux_usb_command(options, usb_analysis_path)
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if options.verbose and completed.stdout:
        print(completed.stdout, end="")
    if options.verbose and completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"termux USB capture failed with exit code {completed.returncode}: {completed.stderr.strip()}")

    usb_analysis = _read_json_if_exists(usb_analysis_path)
    validation: CaptureValidationResult | None = None
    extract_result: dict[str, object] | None = None
    if options.out is not None and options.expect_min_bytes > 0 and options.out.stat().st_size < options.expect_min_bytes:
        raise RuntimeError(
            f"capture wrote {options.out.stat().st_size} bytes, below --expect-min-bytes {options.expect_min_bytes}"
        )
    if options.validate and options.out is not None:
        validation = validate_capture_file(
            options.out,
            expect_mode=options.expect_mode,  # type: ignore[arg-type]
            expected_messages=list(options.expect_messages),
        )
        if validation.errors:
            raise RuntimeError(f"capture validation failed: {'; '.join(validation.errors)}")
    if options.extract_check and options.out is not None:
        extract_result = run_extract_check(options.out, verbose=options.verbose)
        if not extract_result.get("passed"):
            raise RuntimeError(f"extract-check failed: {extract_result.get('error_message', 'unknown error')}")
    result = CaptureUsbResult(
        usb_analysis=usb_analysis,
        validation=validation,
        extract_check=extract_result,
        profile=profile,
        command=command,
    )
    if options.analysis_json:
        options.analysis_json.parent.mkdir(parents=True, exist_ok=True)
        options.analysis_json.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def ensure_native_helper(helper: Path) -> None:
    """Build the native helper when it is absent."""

    if helper.exists():
        return
    build_script = Path("tools/termux/build-um980-usb-fd.sh")
    if not build_script.exists():
        raise FileNotFoundError(f"native helper {helper} is missing and build script {build_script} was not found")
    shell = Path("/data/data/com.termux/files/usr/bin/sh")
    command = [str(shell if shell.exists() else Path("sh")), str(build_script)]
    subprocess.run(command, check=True)
    if not helper.exists():
        raise FileNotFoundError(f"native helper build completed but {helper} still does not exist")


def run_extract_check(capture_file: Path, *, verbose: bool = False) -> dict[str, object]:
    """Run the existing extraction path as structural parser validation."""

    out_dir = capture_file.with_suffix("")
    out_dir = out_dir.parent / f"{out_dir.name}-extract"
    command = [
        sys.executable,
        "-m",
        "um980_rtklib_pipeline.cli",
        "extract",
        str(capture_file),
        "--solution",
        "all",
        "--track-source",
        "auto",
        "--out-dir",
        str(out_dir),
        "--basename",
        capture_file.stem,
        "--analysis-json",
    ]
    if verbose:
        command.append("-v")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"src{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "src"
    completed = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    analysis_path = out_dir / f"{capture_file.stem}.analysis.json"
    return {
        "passed": completed.returncode == 0 and out_dir.exists() and analysis_path.exists(),
        "returncode": completed.returncode,
        "command": command,
        "out_dir": str(out_dir),
        "analysis_json": str(analysis_path),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "error_message": completed.stderr.strip() if completed.returncode else "",
    }


def _usb_analysis_path(options: CaptureUsbOptions) -> Path | None:
    if options.analysis_json is None:
        return None
    if options.validate or options.extract_check:
        return Path(str(options.analysis_json) + ".usb.json")
    return options.analysis_json


def _termux_usb_command(options: CaptureUsbOptions, usb_analysis_path: Path | None) -> list[str]:
    helper_args = [_helper_for_subprocess(options.native_helper)]
    if options.probe:
        helper_args.append("--probe")
    else:
        helper_args.extend(["--read-passive", "--duration", str(options.duration_s)])
        if options.out is not None:
            helper_args.extend(["--out", str(options.out)])
    if usb_analysis_path is not None:
        helper_args.extend(["--analysis-json", str(usb_analysis_path)])
    if options.profile is not None:
        helper_args.extend(["--profile", str(options.profile)])
    _append_option(helper_args, "--profile-line-delay-ms", options.profile_line_delay_ms)
    _append_option(helper_args, "--capture-after-profile-delay-ms", options.capture_after_profile_delay_ms)
    _append_option(helper_args, "--read-timeout-ms", options.read_timeout_ms)
    _append_option(helper_args, "--max-bytes", options.max_bytes)
    _append_option(helper_args, "--interface", options.interface)
    _append_option(helper_args, "--altsetting", options.altsetting)
    _append_option(helper_args, "--ep-in", options.ep_in)
    _append_option(helper_args, "--ep-out", options.ep_out)
    _append_option(helper_args, "--expect-min-bytes", options.expect_min_bytes if options.expect_min_bytes > 0 else None)
    if options.verbose:
        helper_args.append("--verbose")
    return ["termux-usb", "-e", shlex.join(helper_args), str(options.termux_device)]


def _append_option(args: list[str], name: str, value: object | None) -> None:
    if value is not None:
        args.extend([name, str(value)])


def _read_json_if_exists(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _helper_for_subprocess(helper: Path) -> str:
    resolved = executable_for_subprocess(str(helper))
    path = Path(resolved)
    prefix = os.environ.get("PREFIX", "")
    if path.exists() and not os.access(path, os.X_OK) and prefix.startswith("/data/data/com.termux/"):
        return str(mirror_non_executable_tool(path))
    return resolved
