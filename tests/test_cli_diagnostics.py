from __future__ import annotations

import json
import logging
import os
import subprocess
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


def test_quality_compare_json_does_not_require_solution(tmp_path: Path, capsys) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text(
        json.dumps(
            {
                "false_fix_suspicion": {"raw_fixed_time_s": 100.0},
                "long_fixed_metrics": {"fixed_time_ge_thresholds_s": {"60": 80.0}},
                "track_plausibility": {"track_consistency_score": 0.9, "fixed_internal_jump_count": 0},
                "residuals": {"carrier_abs_m": {"fixed_p95": 0.2}},
                "rejections": {"count": 100},
                "slips": {"raw_slip_flags_total": 100},
            }
        ),
        encoding="utf-8",
    )
    right.write_text(
        json.dumps(
            {
                "false_fix_suspicion": {"raw_fixed_time_s": 110.0},
                "long_fixed_metrics": {"fixed_time_ge_thresholds_s": {"60": 10.0}},
                "track_plausibility": {"track_consistency_score": 0.1, "fixed_internal_jump_count": 2},
                "residuals": {"carrier_abs_m": {"fixed_p95": 0.1}},
                "rejections": {"count": 10},
                "slips": {"raw_slip_flags_total": 10},
            }
        ),
        encoding="utf-8",
    )

    rc = cli.main(["quality", "--compare-json", str(left), str(right), "--format", "json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]


def test_quality_detail_outputs_are_explicit(tmp_path: Path) -> None:
    solution = tmp_path / "run.pos"
    out_json = tmp_path / "quality.json"
    detail_json = tmp_path / "quality-detail.json"
    segments_jsonl = tmp_path / "segments.jsonl"
    solution.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 16\n",
        encoding="ascii",
    )

    rc = cli.main(
        [
            "quality",
            "--solution",
            str(solution),
            "--out-json",
            str(out_json),
            "--quality-out-detail-json",
            str(detail_json),
            "--quality-out-segments-jsonl",
            str(segments_jsonl),
        ]
    )

    assert rc == 0
    compact = json.loads(out_json.read_text(encoding="utf-8"))
    detail = json.loads(detail_json.read_text(encoding="utf-8"))
    assert "segment_qc" not in compact["long_fixed_metrics"]
    assert "segment_qc" in detail["long_fixed_metrics"]
    assert segments_jsonl.exists()


def test_quality_cli_accepts_time_window(tmp_path: Path) -> None:
    solution = tmp_path / "run.pos"
    out_json = tmp_path / "quality.json"
    solution.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:02.000 50.000200 14.000000 250.0 2 16\n",
        encoding="ascii",
    )

    rc = cli.main(
        [
            "quality",
            "--solution",
            str(solution),
            "--start-time",
            "2026-05-30T05:00:01Z",
            "--end-time",
            "2026-05-30T05:00:02Z",
            "--out-json",
            str(out_json),
        ]
    )

    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["inputs"]["quality_window_applied"] is True
    assert data["parser_coverage"]["solution_epochs"] == 2


def test_quality_compare_subcommand_outputs_multiple_comparisons(tmp_path: Path, capsys) -> None:
    reports = []
    for index, fixed_km in enumerate((1.0, 0.5, 2.0)):
        path = tmp_path / f"report{index}.json"
        path.write_text(
            json.dumps(
                {
                    "fixed_continuity_summary": {
                        "raw_fixed_time_s": 10.0,
                        "raw_fixed_distance_km": fixed_km,
                        "fixed_time_ge_60s": 0.0,
                        "fixed_distance_ge_1000m": fixed_km,
                    },
                    "track_plausibility": {"track_consistency_score": 0.8, "fixed_internal_jump_count": 0},
                    "residuals": {"carrier_abs_m": {"fixed_p95": 0.2}},
                    "rejections": {"count": 10},
                    "slips": {"raw_slip_flags_total": 10},
                }
            ),
            encoding="utf-8",
        )
        reports.append(path)

    rc = cli.main(["quality-compare", *(str(path) for path in reports), "--format", "json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["comparisons"]) == 2


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
        start_time="2026-05-30T05:00:00Z",
        end_time="2026-05-30T05:01:00Z",
    )

    command = cli._quality_rerun_command(args, solution, None, json_path, md_path)

    assert command[0] == "PYTHONPATH=src"
    assert "--trace" in command
    assert str(trace) in command
    assert "--quality-trace-max-bytes" in command
    assert "--quality-trace-align-tolerance-s" in command
    assert "--base-llh" in command
    assert "--start-time" in command
    assert "--end-time" in command


def test_pipeline_dry_run_plan_writes_manifest_without_rover_parse(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"

    rc = cli.main(
        [
            "pipeline",
            str(tmp_path / "missing.ubx"),
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:01:00Z",
            "--dry-run-plan",
        ]
    )

    assert rc == 0
    manifest = out_dir / "run.pipeline-manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert [step["name"] for step in data["steps"]][:5] == [
        "parse_rover",
        "extract_receiver_products",
        "write_rinex_obs",
        "extract_rover_nav",
        "resolve_base",
    ]
    assert data["inputs"]["start_time"] == "2026-05-30T05:00:00+00:00"
    assert data["effective_processing_window"]["source"] == "cli"
    assert all(step["processing_window"]["start_time"] == "2026-05-30T05:00:00+00:00" for step in data["steps"])
    assert "--start-time 2026-05-30T05:00:00+00:00" in data["steps"][0]["command"]


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

    script = (tmp_path / "rerun.sh").read_text(encoding="utf-8")
    markdown = (tmp_path / "commands.md").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert f"cd {Path.cwd()}" in script
    assert "run_step()" in script
    assert "usage: $0 [all|quality|only STEP|from STEP]" in script
    assert "_ \"${EXTRA_ARGS[@]}\"" in script
    assert "'rover file.ubx'" in script
    assert "'a b.pos'" in script
    assert "Standalone quality" in markdown
    assert subprocess.run(["bash", "-n", str(tmp_path / "rerun.sh")], check=False).returncode == 0


def test_rerun_script_modes_execute_expected_steps_with_fake_python(tmp_path: Path) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"\n")
    out_dir = tmp_path / "out"
    log = tmp_path / "fake-python.log"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$UM980_FAKE_PYTHON_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["UM980_FAKE_PYTHON_LOG"] = str(log)

    rc = cli.main(
        [
            "pipeline",
            str(rover),
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
        "--run-rtklib",
        "--rtkconf",
        "cfg.conf",
        "--quality-analyze",
        "--quality-trace",
        "temporary",
        "--emit-run-script",
        "auto",
        "--start-time",
        "2026-05-30T05:00:00Z",
        "--end-time",
        "2026-05-30T05:01:00Z",
            "--dry-run-plan",
        ]
    )
    assert rc == 0

    script = out_dir / "rerun.sh"
    assert script.exists()
    script.chmod(0o755)

    def _invoke(mode: str, *extra: str) -> list[str]:
        log.unlink(missing_ok=True)
        command = [str(script), mode, *extra]
        subprocess.run(command, cwd=out_dir, env=env, check=False, text=True)
        lines = [line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        subcommands: list[str] = []
        for line in lines:
            parts = line.split()
            if "um980_rtklib_pipeline.cli" in parts:
                idx = parts.index("um980_rtklib_pipeline.cli")
                if idx + 1 < len(parts):
                    subcommands.append(parts[idx + 1])
        return subcommands

    all_steps = _invoke("all")
    assert all_steps == [
        "parse-rover",
        "extract",
        "rinex",
        "nav",
        "resolve-base",
        "run-rtklib",
        "quality",
        "cleanup",
    ]

    quality_only = _invoke("only", "quality")
    assert quality_only == ["quality"]

    from_rtklib = _invoke("from", "run_rtklib")
    assert from_rtklib == ["run-rtklib", "quality", "cleanup"]


def test_canonical_step_aliases_accept_window_and_step_controls() -> None:
    parser = cli.build_parser()

    parse_args = parser.parse_args(
        [
            "parse-rover",
            "rover.ubx",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:01:00Z",
            "--manifest",
            "run.pipeline-manifest.json",
            "--skip-existing",
            "--force",
        ]
    )
    nav_args = parser.parse_args(["nav", "rover.ubx", "--out-dir", "out", "--basename", "run"])
    resolve_args = parser.parse_args(["resolve-base", "rover.ubx", "--station", "TUBO"])
    run_args = parser.parse_args(["run-rtklib", "rover.ubx", "--rover-obs", "rover.obs", "--start-time", "2026-05-30T05:00:00Z"])
    cleanup_args = parser.parse_args(["cleanup", "--manifest", "run.pipeline-manifest.json"])

    assert parse_args.manifest == "run.pipeline-manifest.json"
    assert parse_args.skip_existing is True
    assert parse_args.force is True
    assert nav_args.solution == "none"
    assert resolve_args.station == "TUBO"
    assert run_args.start_time == "2026-05-30T05:00:00Z"
    assert cleanup_args.func is cli.cmd_cleanup


def test_pipeline_dry_run_plan_records_commands_for_all_steps(tmp_path: Path, capsys) -> None:
    out_dir = tmp_path / "out"
    rc = cli.main(
        [
            "pipeline",
            "rover file.ubx",
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:01:00Z",
            "--base-obs",
            "base obs.rnx",
            "--nav-file",
            "nav file.nav",
            "--run-rtklib",
            "--rtkconf",
            "config with spaces.conf",
            "--output-format",
            "nmea",
            "--quality-analyze",
            "--quality-trace",
            "temporary",
            "--raw-output",
            "all",
            "--rinex-compat",
            "convbin",
            "--emit-ion-utc",
            "auto",
            "--print-step-commands",
            "--dry-run-plan",
            "-v",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    manifest = json.loads((out_dir / "run.pipeline-manifest.json").read_text(encoding="utf-8"))
    commands = {step["name"]: step["command"] for step in manifest["steps"]}
    assert set(commands) >= {
        "extract_receiver_products",
        "write_rinex_obs",
        "extract_rover_nav",
        "resolve_base",
        "run_rtklib",
        "quality",
        "cleanup",
    }
    assert all(
        commands[name]
        for name in (
            "extract_receiver_products",
            "write_rinex_obs",
            "extract_rover_nav",
            "resolve_base",
            "run_rtklib",
            "quality",
            "cleanup",
        )
    )
    assert "--raw-output all" in commands["extract_receiver_products"]
    assert "--obs-csv" in commands["extract_receiver_products"]
    assert "--rinex-compat convbin" in commands["write_rinex_obs"]
    assert "run-rtklib" in commands["run_rtklib"]
    assert "--rover-obs" in commands["run_rtklib"]
    assert "'base obs.rnx'" in commands["run_rtklib"]
    assert "'nav file.nav'" in commands["run_rtklib"]
    assert "--rtkconf 'config with spaces.conf'" in commands["run_rtklib"]
    assert "--quality-trace temporary" in commands["run_rtklib"]
    assert "quality --solution" in commands["quality"]
    assert "--start-time 2026-05-30T05:00:00+00:00" in commands["quality"]

    log_text = captured.out + captured.err
    assert "Extract receiver products:" in log_text
    assert "Write RINEX OBS and rover NAV:" in log_text
    assert "Resolve base inputs:" in log_text
    assert "Run RTKLIB:" in log_text
    assert "Run quality analysis:" in log_text
    assert "Cleanup:" in log_text


def test_resolve_base_uses_cli_window_without_rover_parse(tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[tuple[object, object]] = []

    def fake_download_for_window(_args, start, end):
        calls.append((start, end))
        return [tmp_path / "base.obs"]

    def fail_time_from_solutions(_args, _margin):
        raise AssertionError("resolve-base must use explicit processing window before parsing rover solutions")

    monkeypatch.setattr(cli, "_download_base_files_for_window", fake_download_for_window)
    monkeypatch.setattr(cli, "_time_window_from_solutions", fail_time_from_solutions)

    rc = cli.main(
        [
            "resolve-base",
            "missing.ubx",
            "--station",
            "TUBO",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:01:00Z",
        ]
    )

    assert rc == 0
    assert calls
    assert calls[0][0].isoformat() == "2026-05-30T05:00:00+00:00"
    assert calls[0][1].isoformat() == "2026-05-30T05:01:00+00:00"
    assert "base.obs" in capsys.readouterr().out


def test_download_base_pipeline_plan_hands_base_list_to_rtklib(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"

    rc = cli.main(
        [
            "pipeline",
            "rover.ubx",
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
            "--download-base",
            "--station",
            "TUBO",
            "--run-rtklib",
            "--nav-file",
            "nav.nav",
            "--dry-run-plan",
        ]
    )

    assert rc == 0
    manifest = json.loads((out_dir / "run.pipeline-manifest.json").read_text(encoding="utf-8"))
    commands = {step["name"]: step["command"] for step in manifest["steps"]}
    base_list = out_dir / "run.base-observations.txt"
    assert f"--base-obs-list {base_list}" in commands["resolve_base"]
    assert f"--base-obs-list {base_list}" in commands["run_rtklib"]


def test_pipeline_only_step_resolve_base_executes_without_rtklib(tmp_path: Path, monkeypatch) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"\n")
    out_dir = tmp_path / "out"
    base_obs = tmp_path / "base.obs"
    base_obs.write_text("", encoding="utf-8")

    called = {"download": 0}

    def fake_download_base_files(_args):
        called["download"] += 1
        return [base_obs]

    def fail_rtklib_output(*_args, **_kwargs):
        raise AssertionError("RTKLIB output generation must not run for --only-step resolve-base")

    monkeypatch.setattr(cli, "_download_base_files", fake_download_base_files)
    monkeypatch.setattr(cli, "_run_rtklib_output_formats", fail_rtklib_output)

    rc = cli.main(
        [
            "pipeline",
            str(rover),
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
            "--download-base",
            "--station",
            "TUBO",
            "--only-step",
            "resolve-base",
            "--base-rinex-version",
            "3",
        ]
    )

    assert rc == 0
    assert called["download"] == 1
    base_list = out_dir / "run.base-observations.txt"
    assert base_list.exists()
    assert base_list.read_text(encoding="utf-8").strip() == str(base_obs)


def test_pipeline_from_step_resolve_base_then_run_rtklib_uses_cached_base_list(tmp_path: Path, monkeypatch) -> None:
    rover = tmp_path / "rover.ubx"
    rover.write_bytes(b"\n")
    out_dir = tmp_path / "out"
    rover_obs = out_dir / "run.direct.obs"
    rover_obs.parent.mkdir(parents=True, exist_ok=True)
    rover_obs.write_text("", encoding="utf-8")

    base_obs = out_dir / "base.obs"
    base_obs.write_text("", encoding="utf-8")
    nav_nav = out_dir / "nav.nav"
    nav_nav.write_text("", encoding="utf-8")

    calls: dict[str, list[str]] = {"base_obs": []}

    def fake_run_quality(*_args, **_kwargs) -> None:
        pass

    def fake_run_rtklib(
        *,
        args,
        rnx2rtkp,
        out_dir,
        basename,
        rover_obs,
        base_obs,
        nav_files,
        base_obs_arg,
        base_ecef,
        base_llh,
    ) -> list:
        calls["base_obs"] = [str(path) for path in base_obs]
        return [SimpleNamespace(args=["fake", "rnx2rtkp"])]

    def fake_resolve_base_position(*_args, **_kwargs):
        return None, None

    def fail_download_base(*_args, **_kwargs) -> list[Path]:
        raise AssertionError("resolve_base should reuse --base-obs-list when --from-step run-rtklib")

    def fake_resolve_nav_sources(**kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            candidates=[SimpleNamespace(path=Path(kwargs["explicit"][0]), role="explicit", priority=0, systems={"G"}, usable=True, notes=[])],
            selected=[SimpleNamespace(path=Path(kwargs["explicit"][0]), role="explicit", priority=0, systems={"G"}, usable=True, notes=[])],
            missing_systems=set(),
            warnings=[],
        )

    monkeypatch.setattr(cli, "_run_rtklib_output_formats", fake_run_rtklib)
    monkeypatch.setattr(cli, "_download_base_files", fail_download_base)
    monkeypatch.setattr(cli, "resolve_nav_sources", fake_resolve_nav_sources)
    monkeypatch.setattr(cli, "_resolve_base_position", fake_resolve_base_position)
    monkeypatch.setattr(cli, "_run_quality_analysis_if_requested", fake_run_quality)
    monkeypatch.setattr(cli, "filter_rinex_obs_by_overlap", lambda rover_file, base_files: (base_files, []))

    rc = cli.main(
        [
            "pipeline",
            str(rover),
            "--out-dir",
            str(out_dir),
            "--basename",
            "run",
            "--from-step",
            "run-rtklib",
            "--run-rtklib",
            "--base-obs",
            str(base_obs),
            "--nav-file",
            str(nav_nav),
            "--rtkconf",
            "cfg.conf",
        ]
    )

    assert rc == 0
    assert calls["base_obs"] == [str(base_obs)]
