from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from um980_rtklib_pipeline import cli


def _write_pos(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "%  GPST latitude(deg) longitude(deg) height(m) Q ns",
                "2026/05/30 05:02:10.000 50.0000000 14.0000000 250.0 1 18",
                "2026/05/30 05:02:11.000 50.0000100 14.0000100 250.1 1 18",
            ]
        )
        + "\n",
        encoding="ascii",
    )


def test_standalone_quality_clean_stat_refuses_user_stat(tmp_path: Path):
    solution = tmp_path / "solution.pos"
    stat = tmp_path / "solution.stat"
    _write_pos(solution)
    stat.write_text("$SAT,2026/05/30,05:02:10.000,G01\n", encoding="ascii")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "quality-analyze",
            "--solution",
            str(solution),
            "--stat",
            str(stat),
            "--quality-clean-stat",
        ]
    )

    with pytest.raises(ValueError, match="current pipeline/postprocess run"):
        cli.cmd_quality_analyze(args)
    assert stat.exists()


def test_pipeline_quality_cleanup_deletes_generated_stat_after_json(tmp_path: Path):
    out_dir = tmp_path
    basename = "rover"
    solution = out_dir / f"{basename}-rtk.pos"
    stat = out_dir / f"{basename}-rtk.stat"
    _write_pos(solution)
    stat.write_text("$SAT,2026/05/30,05:02:10.000,G01\n", encoding="ascii")
    args = argparse.Namespace(
        quality_analyze=True,
        quality_out_md=None,
        quality_out_json=None,
        quality_clean_stat=True,
        quality_trace="off",
        rtklib_trace_level=None,
        trusted_fixed_min_duration_s=10.0,
        trusted_fixed_min_distance_m=20.0,
        provisional_fixed_min_duration_s=3.0,
        recent_slip_window_s=10.0,
        transition_jump_warning_m=1.0,
        transition_jump_severe_m=3.0,
        vertical_jump_warning_m=1.5,
        carrier_residual_warning_m=0.20,
        carrier_residual_severe_m=0.50,
        code_residual_warning_m=5.0,
        code_residual_severe_m=10.0,
        low_used_signals_warning=12,
        low_snr_warning_dbhz=35.0,
        gap_split_s=2.0,
        stationary_speed_threshold_mps=0.3,
        output_format=["pos"],
    )

    cli._run_quality_analysis_if_requested(args, out_dir, basename, [])

    assert not stat.exists()
    quality_json = out_dir / f"{basename}-rtk.quality.json"
    assert quality_json.exists()
    text = quality_json.read_text(encoding="utf-8")
    assert '"stat_cleanup_requested": true' in text
    assert str(stat) in text


def test_pipeline_quality_cleanup_keeps_stat_by_default(tmp_path: Path):
    out_dir = tmp_path
    basename = "rover"
    solution = out_dir / f"{basename}-rtk.pos"
    stat = out_dir / f"{basename}-rtk.stat"
    _write_pos(solution)
    stat.write_text("$SAT,2026/05/30,05:02:10.000,G01\n", encoding="ascii")
    args = argparse.Namespace(
        quality_analyze=True,
        quality_out_md=None,
        quality_out_json=None,
        quality_clean_stat=False,
        quality_trace="off",
        rtklib_trace_level=None,
        trusted_fixed_min_duration_s=10.0,
        trusted_fixed_min_distance_m=20.0,
        provisional_fixed_min_duration_s=3.0,
        recent_slip_window_s=10.0,
        transition_jump_warning_m=1.0,
        transition_jump_severe_m=3.0,
        vertical_jump_warning_m=1.5,
        carrier_residual_warning_m=0.20,
        carrier_residual_severe_m=0.50,
        code_residual_warning_m=5.0,
        code_residual_severe_m=10.0,
        low_used_signals_warning=12,
        low_snr_warning_dbhz=35.0,
        gap_split_s=2.0,
        stationary_speed_threshold_mps=0.3,
        output_format=["pos"],
    )

    cli._run_quality_analysis_if_requested(args, out_dir, basename, [])

    assert stat.exists()
    text = (out_dir / f"{basename}-rtk.quality.json").read_text(encoding="utf-8")
    assert '"stat_cleanup_requested": false' in text
    assert '"stat_files_deleted": []' in text


def test_pipeline_quality_cleanup_skips_delete_when_analysis_fails(tmp_path: Path, monkeypatch):
    out_dir = tmp_path
    basename = "rover"
    solution = out_dir / f"{basename}-rtk.pos"
    stat = out_dir / f"{basename}-rtk.stat"
    _write_pos(solution)
    stat.write_text("$SAT,2026/05/30,05:02:10.000,G01\n", encoding="ascii")

    def fail_analysis(**_kwargs):
        raise RuntimeError("analysis failed")

    monkeypatch.setattr(cli, "analyze_rtk_quality", fail_analysis)
    args = argparse.Namespace(
        quality_analyze=True,
        quality_out_md=None,
        quality_out_json=None,
        quality_clean_stat=True,
        quality_trace="off",
        rtklib_trace_level=None,
        trusted_fixed_min_duration_s=10.0,
        trusted_fixed_min_distance_m=20.0,
        provisional_fixed_min_duration_s=3.0,
        recent_slip_window_s=10.0,
        transition_jump_warning_m=1.0,
        transition_jump_severe_m=3.0,
        vertical_jump_warning_m=1.5,
        carrier_residual_warning_m=0.20,
        carrier_residual_severe_m=0.50,
        code_residual_warning_m=5.0,
        code_residual_severe_m=10.0,
        low_used_signals_warning=12,
        low_snr_warning_dbhz=35.0,
        gap_split_s=2.0,
        stationary_speed_threshold_mps=0.3,
        output_format=["pos"],
    )

    with pytest.raises(RuntimeError, match="analysis failed"):
        cli._run_quality_analysis_if_requested(args, out_dir, basename, [])

    assert stat.exists()


def test_existing_trace_mode_requires_trace_path():
    args = argparse.Namespace(quality_trace="existing", trace=None, rtklib_trace_level=None)

    with pytest.raises(ValueError, match="requires --trace"):
        cli._validate_quality_trace_args(args)


def test_off_trace_mode_rejects_trace_level():
    args = argparse.Namespace(quality_trace="off", trace=None, rtklib_trace_level=2)

    with pytest.raises(ValueError, match="requires --quality-trace"):
        cli._validate_quality_trace_args(args)


def test_standalone_quality_trace_parses_explicit_trace_and_retains_it(tmp_path: Path):
    solution = tmp_path / "solution.pos"
    trace = tmp_path / "solution.pos.trace"
    out_json = tmp_path / "quality.json"
    out_md = tmp_path / "quality.md"
    _write_pos(solution)
    trace.write_text("".join("resamb: ratio=3.2\n" for _ in range(50)), encoding="ascii")

    rc = cli.main(
        [
            "quality",
            "--solution",
            str(solution),
            "--trace",
            str(trace),
            "--quality-trace-max-bytes",
            "0",
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
        ]
    )

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert rc == 0
    assert trace.exists()
    assert data["trace"]["available"] is True
    assert data["trace"]["source"] == "existing"
    assert data["trace"]["path"] == str(trace)
    assert data["trace"]["trace_file_size_bytes"] == trace.stat().st_size
    assert data["trace"]["trace_raw_bytes_read"] == trace.stat().st_size
    assert data["trace"]["trace_lines_read"] == 50
    assert data["trace"]["trace_truncated"] is False
    assert "Trace parsed path" in out_md.read_text(encoding="utf-8")
