from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import subprocess

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline import optimizer
from um980_rtklib_pipeline.optimizer import build_optimizer_plan, parse_duration_seconds
from um980_rtklib_pipeline.time_window import ProcessingWindow


def test_parse_duration_seconds_accepts_compact_units():
    assert parse_duration_seconds("120s") == 120.0
    assert parse_duration_seconds("2m") == 120.0
    assert parse_duration_seconds("0.5h") == 1800.0


def test_optimizer_plan_is_bounded_by_max_runs(tmp_path: Path):
    window = ProcessingWindow(
        start=datetime(2026, 5, 30, 5, 0, tzinfo=UTC),
        end=datetime(2026, 5, 30, 5, 10, tzinfo=UTC),
    )
    plan = build_optimizer_plan(
        rover_files=[tmp_path / "a.ubx", tmp_path / "b.ubx"],
        config=Path("um980.conf"),
        bases=["TUBO", "CPAR"],
        base_resolution="auto",
        nav_source="auto-prefer-base",
        sbas_source="auto",
        emit_ion_utc="off",
        window=window,
        sample_count=4,
        sample_duration_s=120.0,
        max_variants=2,
        max_runs=5,
        dry_run=True,
    )
    assert len(plan.samples) == 4
    assert len(plan.variants) == 2
    assert len(plan.runs) == 5
    assert any("planned runs capped" in warning for warning in plan.warnings)


def test_optimize_settings_dry_run_json(capsys):
    rc = cli.main(
        [
            "optimize-settings",
            "rover.ubx",
            "--config",
            "um980.conf",
            "--bases",
            "TUBO,CPAR",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:20:00Z",
            "--sample-count",
            "2",
            "--sample-duration",
            "300s",
            "--max-runs",
            "4",
            "--format",
            "json",
            "--dry-run",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True
    assert len(data["samples"]) == 2
    assert len(data["variants"]) == 2
    assert len(data["runs"]) == 4


def test_optimize_settings_defaults_to_dry_run(capsys):
    rc = cli.main(["optimize-settings", "rover.ubx"])
    assert rc == 0
    assert "dry_run=true" in capsys.readouterr().out


def test_optimize_settings_execute_runs_bounded_subprocess(monkeypatch, tmp_path: Path, capsys):
    calls = []

    def fake_run(command, check, stdout, stderr, text):
        calls.append(command)
        out_dir = Path(command[command.index("--out-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "optimizer-rtk.pos").write_text(
            "% header\n"
            "2026/05/30 05:00:00.000 49.000000 14.000000 250.0 1 12\n"
            "2026/05/30 05:00:01.000 49.000100 14.000100 250.0 2 12\n",
            encoding="ascii",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(optimizer.subprocess, "run", fake_run)
    rc = cli.main(
        [
            "optimize-settings",
            "rover.ubx",
            "--base",
            "TUBO",
            "--start-time",
            "2026-05-30T05:00:00Z",
            "--end-time",
            "2026-05-30T05:02:00Z",
            "--sample-count",
            "1",
            "--max-runs",
            "1",
            "--out-dir",
            str(tmp_path),
            "--execute",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert calls
    assert data["dry_run"] is False
    assert data["results"][0]["status"] == "ok"
    assert data["results"][0]["metrics"]["epochs_total"] == 2


def test_optimize_settings_consumes_base_candidates_json(tmp_path: Path, capsys):
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps(
            {
                "candidates": [
                    {"marker": "CPAR00CZE", "score": 90},
                    {"marker": "TUBO00CZE", "score": 80},
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "optimize-settings",
            "rover.ubx",
            "--base-candidates-json",
            str(candidates),
            "--top-bases",
            "1",
            "--max-variants",
            "2",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["variants"][0]["base"] == "CPAR00CZE"
