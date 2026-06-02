from __future__ import annotations

import argparse
import json
from pathlib import Path

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.quality import (
    QualityThresholds,
    analyze_rtk_quality,
    compute_segments,
    parse_solution_epochs,
    parse_stat_file,
)


def _gga(time: str, lat: str, lon: str, quality: int, sats: int = 18, hdop: float = 0.7, height: float = 250.0) -> str:
    return make_sentence(f"GNGGA,{time},{lat},N,{lon},E,{quality},{sats},{hdop:.1f},{height:.1f},M,45.0,M,,")


def _rmc(time: str = "050000.00", date: str = "300526") -> str:
    return make_sentence(f"GNRMC,{time},A,5000.0000,N,01400.0000,E,0.0,0.0,{date},,,A")


def test_nmea_gga_parser_maps_quality_and_fields(tmp_path: Path):
    path = tmp_path / "run.nmea"
    path.write_text(
        "\n".join(
            [
                _rmc(),
                _gga("050000.00", "5000.0000", "01400.0000", 4),
                _gga("050000.50", "5000.0010", "01400.0000", 5),
                _gga("050001.00", "5000.0020", "01400.0000", 2),
                _gga("050001.50", "5000.0030", "01400.0000", 1),
                _gga("050002.00", "5000.0040", "01400.0000", 0),
                "not nmea",
            ]
        )
        + "\n",
        encoding="ascii",
    )

    epochs, warnings, kind = parse_solution_epochs(path)

    assert kind == "nmea"
    assert warnings == []
    assert [epoch.quality for epoch in epochs] == ["fixed", "float", "dgps", "single", "invalid"]
    assert epochs[0].lat == 50.0
    assert epochs[0].lon == 14.0
    assert epochs[0].height_m == 250.0
    assert epochs[0].num_sats == 18
    assert epochs[0].hdop == 0.7


def test_segment_computation_splits_on_quality_and_gaps(tmp_path: Path):
    path = tmp_path / "run.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:02.000 50.000200 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:10.000 50.000300 14.000000 250.0 2 16\n",
        encoding="ascii",
    )
    epochs, _, _ = parse_solution_epochs(path)

    segments = compute_segments(epochs, gap_split_s=2.0, jump_clip_m=100.0)

    assert [segment.quality for segment in segments] == ["fixed", "float", "float"]
    assert segments[0].duration_s == 1.0
    assert segments[0].distance_m > 10.0


def test_stationary_fixed_segment_not_suspect_for_low_distance(tmp_path: Path):
    path = tmp_path / "stationary.pos"
    path.write_text(
        "".join(
            f"2026/05/30 05:00:{sec:02d}.000 50.000000 14.000000 250.0 1 16\n"
            for sec in range(0, 12)
        ),
        encoding="ascii",
    )

    analysis = analyze_rtk_quality(solution_path=path)
    suspicion = analysis.as_dict()["false_fix_suspicion"]

    assert suspicion["trusted_fixed_time_s"] >= 10.0
    assert suspicion["reasons"]["short_distance_while_moving"] == 0.0


def test_short_moving_fixed_segment_is_provisional_for_low_distance(tmp_path: Path):
    path = tmp_path / "short.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:01.000 50.000000 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:02.000 50.000010 14.000000 250.0 1 16\n",
        encoding="ascii",
    )

    analysis = analyze_rtk_quality(solution_path=path)
    suspicion = analysis.as_dict()["false_fix_suspicion"]

    assert suspicion["qc_provisional_fixed_time_s"] > 0.0
    assert suspicion["qc_suspect_fixed_time_s"] == 0.0
    assert suspicion["reasons"]["short_distance_while_moving"] > 0.0


def test_missing_output_and_impossible_transition_jump_are_reported(tmp_path: Path):
    path = tmp_path / "gap.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:01.000 50.000001 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:10.000 50.000002 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:10.200 50.000720 14.000000 250.0 1 16\n",
        encoding="ascii",
    )

    analysis = analyze_rtk_quality(solution_path=path)
    data = analysis.as_dict()

    assert data["time_summary"]["missing_time_s"] > 0.0
    assert data["transition_jumps"]["fixed_entry_gt_warning"] == 1
    assert data["transition_jumps"]["fixed_entry_jumps"][0]["motion_anomaly"] == "severe"


def test_highway_motion_does_not_create_transition_false_warning(tmp_path: Path):
    path = tmp_path / "highway.pos"
    lines = []
    for index in range(10):
        quality = 1 if index < 4 or index >= 6 else 2
        # About 7.2 m per 0.2 s, i.e. 36 m/s. This is normal highway motion.
        lat = 50.0 + index * 0.0000647
        seconds = index * 0.2
        lines.append(f"2026/05/30 05:00:{seconds:06.3f} {lat:.7f} 14.0000000 250.0 {quality} 16\n")
    path.write_text("".join(lines), encoding="ascii")

    analysis = analyze_rtk_quality(solution_path=path, thresholds=QualityThresholds(motion_profile="highway"))
    data = analysis.as_dict()

    assert data["motion"]["inferred_profile"] == "highway"
    assert data["transition_jumps"]["fixed_entry_gt_warning"] == 0
    assert data["false_fix_suspicion"]["qc_suspect_fixed_time_s"] == 0.0


def test_bridge_like_dropout_reacquisition_is_reported_without_suspect_fix(tmp_path: Path):
    path = tmp_path / "bridge.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:02.000 50.000200 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:03.000 50.000300 14.000000 250.0 2 16\n"
        "2026/05/30 05:00:04.000 50.000400 14.000000 250.0 1 16\n"
        "2026/05/30 05:00:05.000 50.000500 14.000000 250.0 1 16\n",
        encoding="ascii",
    )

    data = analyze_rtk_quality(solution_path=path).as_dict()

    assert data["dropout_reacquisition"]["likely_occlusion_events"] == 1
    assert data["transition_jumps"]["fixed_entry_gt_warning"] == 0


def test_stat_parser_is_tolerant_and_reports_unavailable_metrics(tmp_path: Path):
    stat = tmp_path / "run.stat"
    stat.write_text("$FOO,ignored\n$SAT,bad,line\n", encoding="ascii")

    parsed = parse_stat_file(stat)
    analysis = analyze_rtk_quality(solution_path=_write_minimal_pos(tmp_path), stat_path=stat)

    assert parsed is not None
    assert parsed.unparsed_sat_lines == 1
    assert analysis.as_dict()["residuals"]["available"] is False


def test_stat_parser_counts_basic_sat_evidence(tmp_path: Path):
    stat = tmp_path / "run.stat"
    stat.write_text("$SAT,2419,450000.0,G01,1,45,120,0.12,2.5,1,42,0,1,0,0,0,1\n", encoding="ascii")

    analysis = analyze_rtk_quality(solution_path=_write_minimal_pos(tmp_path), stat_path=stat)
    data = analysis.as_dict()

    assert data["residuals"]["available"] is True
    assert data["slips"]["events_total"] == 1
    assert data["rejections"]["count"] == 1


def test_json_schema_uses_null_for_unavailable_metrics(tmp_path: Path):
    analysis = analyze_rtk_quality(solution_path=_write_minimal_pos(tmp_path))
    data = analysis.as_dict()

    for key in ("inputs", "parser_coverage", "time_summary", "distance_summary", "segments", "residuals", "slips", "false_fix_suspicion"):
        assert key in data
    assert data["residuals"]["carrier_abs_m"]["fixed_p95"] is None
    assert data["slips"]["events_total"] is None


def test_quality_analyze_cli_writes_json_and_markdown(tmp_path: Path):
    solution = _write_minimal_nmea(tmp_path)
    out_json = tmp_path / "quality.json"
    out_md = tmp_path / "quality.md"

    rc = cli.main(["quality-analyze", "--solution", str(solution), "--out-json", str(out_json), "--out-md", str(out_md)])

    assert rc == 0
    assert json.loads(out_json.read_text(encoding="utf-8"))["inputs"]["solution_type"] == "nmea"
    markdown = out_md.read_text(encoding="utf-8")
    assert "RTK Solution Quality Report" in markdown
    assert "Fixed Confidence Classification" in markdown
    assert "Residual Summary" in markdown
    assert "Slip / Rejection Summary" in markdown
    assert "Motion And Baseline Context" in markdown
    assert "```json" not in markdown


def test_incomplete_diagnostics_limit_qc_confidence_without_forcing_suspect(tmp_path: Path):
    solution = _write_minimal_pos(tmp_path)
    stat = tmp_path / "unaligned.stat"
    stat.write_text(
        "$SAT,2419,450000.0,G01,1,45,120,0.30,2.5,1,42,0,1,0,0,0,1\n",
        encoding="ascii",
    )

    data = analyze_rtk_quality(solution_path=solution, stat_path=stat).as_dict()

    assert data["residuals"]["available"] is True
    assert data["residuals"]["quality_aligned"] is False
    assert data["residuals"]["carrier_abs_m"]["fixed_p95"] is None
    assert data["false_fix_suspicion"]["qc_unknown_fixed_time_s"] >= 0.0
    assert data["false_fix_suspicion"]["qc_suspect_fixed_time_s"] == 0.0
    assert any("not aligned" in warning for warning in data["warnings"])


def test_pipeline_quality_analyze_invokes_analyzer(tmp_path: Path, monkeypatch):
    output = tmp_path / "rover-rtk.pos"
    output.write_text("2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n", encoding="ascii")
    calls = []

    monkeypatch.setattr(cli, "_rtklib_output_formats", lambda _args: ["pos"])
    monkeypatch.setattr(cli, "analyze_rtk_quality", lambda **kwargs: calls.append(kwargs) or analyze_rtk_quality(**kwargs))

    cli._run_quality_analysis_if_requested(
        argparse.Namespace(
            quality_analyze=True,
            quality_out_md=None,
            quality_out_json=None,
            trusted_fixed_min_duration_s=10.0,
            trusted_fixed_min_distance_m=20.0,
            provisional_fixed_min_duration_s=3.0,
            recent_slip_window_s=10.0,
            transition_jump_warning_m=1.0,
            transition_jump_severe_m=3.0,
            vertical_jump_warning_m=1.5,
            carrier_residual_warning_m=0.2,
            carrier_residual_severe_m=0.5,
            code_residual_warning_m=5.0,
            code_residual_severe_m=10.0,
            low_used_signals_warning=12,
            low_snr_warning_dbhz=35.0,
            gap_split_s=2.0,
            stationary_speed_threshold_mps=0.3,
        ),
        tmp_path,
        "rover",
    )

    assert calls
    assert (tmp_path / "rover-rtk.quality.json").exists()


def _write_minimal_pos(tmp_path: Path) -> Path:
    path = tmp_path / "run.pos"
    path.write_text("2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 16\n", encoding="ascii")
    return path


def _write_minimal_nmea(tmp_path: Path) -> Path:
    path = tmp_path / "run.nmea"
    path.write_text(_rmc() + "\n" + _gga("050000.00", "5000.0000", "01400.0000", 4) + "\n", encoding="ascii")
    return path
