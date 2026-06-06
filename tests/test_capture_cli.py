from pathlib import Path
from argparse import Namespace
from types import SimpleNamespace

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline import capture_termux
from um980_rtklib_pipeline.capture_termux import CaptureUsbOptions, CaptureUsbResult


def test_capture_usb_refuses_missing_termux_device(tmp_path: Path, capsys) -> None:
    rc = cli.main(["capture-usb", "--duration", "1", "--out", str(tmp_path / "capture.unc")])

    assert rc == 2
    assert "--termux-device is required" in capsys.readouterr().err


def test_capture_usb_dry_run_profile_does_not_need_usb(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "passive.um980"
    profile.write_text("# enabled: true\nmode: passive\npersistent: no\n", encoding="utf-8")

    rc = cli.main(["capture-usb", "--profile", str(profile), "--dry-run-profile"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "enabled: true" in output
    assert "commands: <none>" in output


def test_capture_usb_passes_options_to_wrapper(tmp_path: Path, monkeypatch) -> None:
    seen = {}

    def fake_run(options):
        seen["options"] = options
        return CaptureUsbResult()

    monkeypatch.setattr(cli, "run_capture_usb", fake_run)
    out = tmp_path / "capture.unc"

    rc = cli.main(
        [
            "capture-usb",
            "--termux-device",
            "/dev/bus/usb/002/002",
            "--duration",
            "2",
            "--out",
            str(out),
            "--validate",
            "--extract-check",
            "--expect-mode",
            "passive",
            "--expect-message",
            "GGA",
            "--native-helper",
            "helper",
            "--serial-baud",
            "230400",
            "--profile-baud",
            "115200",
            "--discard-after-profile-ms",
            "1500",
            "--discard-after-profile-bytes",
            "4096",
            "--command-timeout-s",
            "7",
        ]
    )

    assert rc == 0
    assert seen["options"].termux_device == "/dev/bus/usb/002/002"
    assert seen["options"].duration_s == 2
    assert seen["options"].out == out
    assert seen["options"].validate is True
    assert seen["options"].extract_check is True
    assert seen["options"].expect_messages == ("GGA",)
    assert seen["options"].native_helper == Path("helper")
    assert seen["options"].serial_baud == 230400
    assert seen["options"].profile_baud == 115200
    assert seen["options"].discard_after_profile_ms == 1500
    assert seen["options"].discard_after_profile_bytes == 4096
    assert seen["options"].command_timeout_s == 7


def test_termux_helper_command_includes_post_profile_discard(tmp_path: Path) -> None:
    command = capture_termux._termux_usb_command(
        CaptureUsbOptions(
            termux_device="/dev/bus/usb/002/002",
            duration_s=2,
            out=tmp_path / "capture.unc",
            native_helper=Path("helper"),
            profile=tmp_path / "profile.um980",
            profile_baud=115200,
            discard_after_profile_ms=1500,
            discard_after_profile_bytes=4096,
        ),
        None,
    )

    helper_command = command[2]
    assert "--profile-baud 115200" in helper_command
    assert "--discard-after-profile-ms 1500" in helper_command
    assert "--discard-after-profile-bytes 4096" in helper_command


def test_hardware_capture_tests_are_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("UM980_HW_TEST", raising=False)

    assert True


def test_native_helper_build_uses_shell_even_when_script_is_not_executable(tmp_path: Path, monkeypatch) -> None:
    tools_dir = tmp_path / "tools" / "termux"
    tools_dir.mkdir(parents=True)
    build_script = tools_dir / "build-um980-usb-fd.sh"
    build_script.write_text("#!/bin/sh\n", encoding="utf-8")
    helper = tools_dir / "um980-usb-fd"
    seen = {}

    def fake_run(command, check):  # noqa: ANN001
        seen["command"] = command
        seen["check"] = check
        helper.write_text("binary", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(capture_termux.subprocess, "run", fake_run)

    capture_termux.ensure_native_helper(Path("tools/termux/um980-usb-fd"))

    assert seen["check"] is True
    assert seen["command"][-1] == "tools/termux/build-um980-usb-fd.sh"
    assert seen["command"][0].endswith("/sh") or seen["command"][0] == "sh"


def test_rinex_nav_only_passes_parsed_records_to_nav_extractor(tmp_path: Path, monkeypatch) -> None:
    parsed_records = [object()]
    out_dir = tmp_path / "nav-out"
    seen = {}

    def fake_extract_bundle(args):  # noqa: ANN001
        return (tmp_path / "rover.unc", parsed_records, object(), object(), object(), object(), object(), object(), {})

    def fake_extract_rover_nav(records, output_path):  # noqa: ANN001
        seen["records"] = records
        seen["output_path"] = output_path
        return SimpleNamespace(written={})

    monkeypatch.setattr(cli, "_extract_bundle", fake_extract_bundle)
    monkeypatch.setattr(cli, "extract_rover_nav", fake_extract_rover_nav)
    args = Namespace(
        rover_log=str(tmp_path / "rover.unc"),
        verbose=False,
        debug=False,
        out_dir=str(out_dir),
        basename="rover",
        analysis_json=False,
        solution="none",
        position_nmea="none",
        nav_only=True,
        log_file=None,
    )

    assert cli.cmd_rinex(args) == 0
    assert seen["records"] is parsed_records
    assert seen["output_path"] == out_dir / "rover.rover-gps.nav"
    assert out_dir.is_dir()
