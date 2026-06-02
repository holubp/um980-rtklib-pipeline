from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from um980_rtklib_pipeline import cli


def test_duplicate_nav_merge_warns_and_last_value_wins(caplog) -> None:
    argv = ["pipeline", "rover.ubx", "--nav-merge", "best-per-system", "--nav-merge", "all", "-v"]
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    setattr(args, "_duplicate_options", cli._scan_duplicate_options(argv))

    with caplog.at_level(logging.WARNING):
        cli._emit_duplicate_option_warnings(args)

    assert args.nav_merge == "all"
    assert "option --nav-merge specified multiple times" in caplog.text
    assert "using last value: all" in caplog.text
    assert "previous values: best-per-system" in caplog.text


def test_duplicate_crx2rnx_debug_warning_includes_positions(caplog) -> None:
    argv = ["pipeline", "rover.ubx", "--crx2rnx", "./crx2rnx", "--crx2rnx", "./crx2rnx.exe", "-d"]
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    setattr(args, "_duplicate_options", cli._scan_duplicate_options(argv))

    with caplog.at_level(logging.WARNING):
        cli._emit_duplicate_option_warnings(args)

    assert args.crx2rnx == "./crx2rnx.exe"
    assert "option --crx2rnx specified multiple times" in caplog.text
    assert "positions:" in caplog.text
    assert "./crx2rnx.exe" in caplog.text


def test_extract_does_not_dump_analysis_summary_without_analysis_json(tmp_path: Path, monkeypatch, capsys) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"")
    analysis = {
        "stream": {"binary_resynchronisation_events": 3},
        "solution_points": 0,
        "raw_observations": {},
    }

    def fake_extract_bundle(_args):
        return (
            rover,
            None,
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            None,
            SimpleNamespace(),
            None,
            analysis,
        )

    monkeypatch.setattr(cli, "_extract_bundle", fake_extract_bundle)

    rc = cli.main(["extract", str(rover), "--solution", "none", "--out-dir", str(tmp_path), "-v"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "binary_resynchronisation_events" not in captured.out
    assert "binary_resynchronisation_events" not in captured.err


def test_extract_prints_analysis_summary_once_with_analysis_json(tmp_path: Path, monkeypatch, capsys) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"")
    analysis = {
        "stream": {"binary_resynchronisation_events": 3},
        "solution_points": 0,
        "raw_observations": {},
    }

    def fake_extract_bundle(_args):
        return (
            rover,
            None,
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            None,
            SimpleNamespace(),
            None,
            analysis,
        )

    monkeypatch.setattr(cli, "_extract_bundle", fake_extract_bundle)

    rc = cli.main(["extract", str(rover), "--solution", "none", "--analysis-json", "--out-dir", str(tmp_path), "-v"])

    captured = capsys.readouterr()
    assert rc == 0
    assert (captured.out + captured.err).count("binary_resynchronisation_events") == 1
