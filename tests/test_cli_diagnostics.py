from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.stream import StreamDiagnostics


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


def test_extract_bundle_reuses_cached_rover_parse(tmp_path: Path, monkeypatch) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"")
    calls = {"load": 0}

    def fake_load_records(_path):
        calls["load"] += 1
        return [], StreamDiagnostics()

    monkeypatch.setattr(cli, "_load_records", fake_load_records)
    args = SimpleNamespace(
        rover_log=str(rover),
        track_source="auto",
        emit_ion_utc="off",
        start_time=None,
        end_time=None,
    )

    first = cli._extract_bundle(args)
    second = cli._extract_bundle(args)

    assert first is second
    assert calls["load"] == 1


def test_observation_csv_write_is_deduplicated(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def fake_write(path, _observations):
        calls.append(path)
        path.write_text("ok\n", encoding="ascii")

    monkeypatch.setattr(cli, "write_observations_csv", fake_write)
    args = SimpleNamespace()
    path = tmp_path / "obs.csv"

    cli._write_observation_csv_once(args, path, [])
    cli._write_observation_csv_once(args, path, [])

    assert calls == [path]


def test_quality_alias_writes_outputs(tmp_path: Path) -> None:
    solution = tmp_path / "run.pos"
    out_json = tmp_path / "quality.json"
    out_md = tmp_path / "quality.md"
    solution.write_text("2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n", encoding="ascii")

    rc = cli.main(["quality", "--solution", str(solution), "--out-json", str(out_json), "--out-md", str(out_md)])

    assert rc == 0
    assert out_json.exists()
    assert out_md.exists()


def test_quality_analyze_alias_warns_deprecated(tmp_path: Path, capsys) -> None:
    solution = tmp_path / "run.pos"
    out_json = tmp_path / "quality.json"
    solution.write_text("2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n", encoding="ascii")

    rc = cli.main(["quality-analyze", "--solution", str(solution), "--out-json", str(out_json), "--base-llh", "50", "14", "250"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "quality-analyze' is deprecated" in captured.err
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["baseline_summary"]["available"] is True


def test_quality_rerun_command_includes_trace_and_base_options(tmp_path: Path) -> None:
    solution = tmp_path / "run.pos"
    trace = Path(str(solution) + ".trace")
    json_path = tmp_path / "quality.json"
    md_path = tmp_path / "quality.md"
    solution.write_text("2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n", encoding="ascii")
    trace.write_text("resamb: ratio=3.2\n", encoding="ascii")
    args = SimpleNamespace(
        trace=None,
        quality_trace_max_bytes=1000,
        quality_trace_align_tolerance_s=0.25,
        base_llh=[50.0, 14.0, 250.0],
        base_ecef=None,
        quality_stat_max_lines=0,
        quality_stat_max_seconds=0.0,
        quality_motion_profile="auto",
        quality_route_bin_km=10.0,
        quality_fast=False,
    )

    command = cli._quality_rerun_command(args, solution, None, json_path, md_path)

    assert command[0] == "PYTHONPATH=src"
    assert "--trace" in command
    assert str(trace) in command
    assert "--quality-trace-max-bytes" in command
    assert "--quality-trace-align-tolerance-s" in command
    assert "--base-llh" in command


def test_pipeline_dry_run_plan_writes_manifest_without_rover_parse(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"

    rc = cli.main(["pipeline", str(tmp_path / "missing.ubx"), "--out-dir", str(out_dir), "--basename", "run", "--dry-run-plan"])

    assert rc == 0
    manifest = out_dir / "run.pipeline-manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert [step["name"] for step in data["steps"]][:3] == [
        "extract_receiver_products",
        "write_rinex_obs",
        "resolve_base",
    ]


def test_rerun_script_emits_quoted_commands(tmp_path: Path) -> None:
    args = SimpleNamespace(
        verbose=True,
        debug=False,
        no_emit_run_script=False,
        emit_run_script="auto",
        _original_argv=["pipeline", "rover file.ubx", "--rtkconf", "config with spaces.conf"],
        print_step_commands=False,
    )

    cli._init_rerun_artifacts(args, tmp_path, "rover")
    cli._append_rerun_command(args, "Standalone quality", ["python", "-m", "um980_rtklib_pipeline.cli", "quality", "--solution", "a b.pos"])

    script = (tmp_path / "rover.rerun.sh").read_text(encoding="utf-8")
    markdown = (tmp_path / "rover.commands.md").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert "'rover file.ubx'" in script
    assert "'a b.pos'" in script
    assert "Standalone quality" in markdown
