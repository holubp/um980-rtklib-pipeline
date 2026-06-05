from pathlib import Path

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline import capture_termux
from um980_rtklib_pipeline.capture_termux import CaptureUsbResult


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
