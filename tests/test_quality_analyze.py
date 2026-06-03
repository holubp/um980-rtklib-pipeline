from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.quality import (
    EpochIndex,
    QualityThresholds,
    SolutionEpoch,
    analyze_rtk_quality,
    compare_quality_reports,
    compute_segments,
    format_quality_markdown,
    parse_solution_epochs,
    parse_stat_file,
)
from um980_rtklib_pipeline.time_window import processing_window_from_values
from um980_rtklib_pipeline.timeutil import gps_week_tow_to_utc_datetime


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


def test_epoch_index_nearest_lookup_exact_between_and_bounds() -> None:
    base = gps_week_tow_to_utc_datetime(2419, 450000.0)
    epochs = [
        _epoch_at(base, 0.0),
        _epoch_at(base, 1.0),
        _epoch_at(base, 2.0),
    ]
    index = EpochIndex.build(epochs)

    assert index.nearest(base) == epochs[0]
    assert index.nearest(base + timedelta(seconds=1.4)) == epochs[1]
    assert index.nearest(base - timedelta(seconds=5.0)) is None
    assert index.nearest(base + timedelta(seconds=5.0)) is None


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


def test_trace_events_align_to_solution_epochs_and_quality_states(tmp_path: Path):
    solution = tmp_path / "trace-align.pos"
    solution.write_text(
        "2026/05/30 05:02:10.000 50.000000 14.000000 250.0 1 18\n"
        "2026/05/30 05:02:11.000 50.000010 14.000000 250.0 1 18\n"
        "2026/05/30 05:02:12.000 50.000020 14.000000 250.0 2 18\n",
        encoding="ascii",
    )
    trace_summary = {
        "available": True,
        "events": {
            "event_time_aggregates": [
                {
                    "time": "2026-05-30T05:02:10+00:00",
                    "counts": {"ar_ratio": 1, "cycle_slip": 1},
                    "ar_ratio_min": 2.4,
                    "ar_threshold": 3.0,
                },
                {
                    "time": "2026-05-30T05:02:12+00:00",
                    "counts": {"observation_rejection": 3},
                },
            ]
        },
    }

    data = analyze_rtk_quality(solution_path=solution, trace_summary=trace_summary).as_dict()
    alignment = data["trace"]["alignment"]

    assert alignment["trace_events_aligned"] == 5
    assert alignment["trace_events_unaligned"] == 0
    assert alignment["event_counts_by_quality"]["fixed"]["ar_ratio"] == 1
    assert alignment["event_counts_by_quality"]["fixed"]["cycle_slip"] == 1
    assert alignment["event_counts_by_quality"]["float"]["observation_rejection"] == 3
    assert data["false_fix_suspicion"]["reasons"]["trace_low_ar_ratio"] > 0.0
    assert data["false_fix_suspicion"]["evidence_sources"]["trace_low_ar_ratio"] == ["trace"]


def test_time_of_day_trace_events_align_to_solution_date(tmp_path: Path):
    solution = tmp_path / "trace-tod.pos"
    solution.write_text(
        "2026/05/30 05:10:30.000 50.000000 14.000000 250.0 1 18\n"
        "2026/05/30 05:10:31.000 50.000010 14.000000 250.0 1 18\n",
        encoding="ascii",
    )
    trace_summary = {
        "available": True,
        "events": {
            "event_time_aggregates": [
                {
                    "time": "1970-01-01T05:10:30.400000+00:00",
                    "time_basis": "time_of_day",
                    "counts": {"ambiguity_validation_failed": 1, "ar_ratio": 1},
                    "ar_ratio_min": 2.0,
                    "ar_threshold": 3.0,
                }
            ]
        },
    }

    data = analyze_rtk_quality(solution_path=solution, trace_summary=trace_summary).as_dict()

    assert data["trace"]["alignment"]["available"] is True
    assert data["trace"]["alignment"]["trace_events_aligned"] == 2
    assert data["false_fix_suspicion"]["reasons"]["trace_ambiguity_validation_failed"] > 0.0
    assert data["false_fix_suspicion"]["reason_details"]["trace_ambiguity_validation_failed"]["aligned"] is True


def test_global_unaligned_trace_does_not_mark_fixed_suspect(tmp_path: Path):
    solution = _write_minimal_pos(tmp_path)
    trace_summary = {
        "available": True,
        "counters": {"cycle_slip_lines": 1000, "residual_outlier_lines": 1000},
        "events": {"event_time_aggregates": []},
    }

    data = analyze_rtk_quality(solution_path=solution, trace_summary=trace_summary).as_dict()

    assert data["trace"]["alignment"]["trace_events_aligned"] == 0
    assert data["false_fix_suspicion"]["reasons"]["trace_recent_slip"] == 0.0
    assert data["false_fix_suspicion"]["reasons"]["trace_residual_outlier"] == 0.0


def test_auto_motion_profile_uses_high_speed_evidence(tmp_path: Path):
    path = tmp_path / "vehicle.pos"
    lines = []
    for index in range(30):
        lat_step = 0.00027 if index < 20 else 0.00002
        lat = 50.0 + index * lat_step
        lines.append(f"2026/05/30 05:00:{index:02d}.000 {lat:.7f} 14.0000000 250.0 1 18\n")
    path.write_text("".join(lines), encoding="ascii")

    data = analyze_rtk_quality(solution_path=path).as_dict()

    assert data["motion"]["inferred_profile"] in {"vehicle", "highway"}
    assert data["motion"]["max_speed_threshold_mps"] >= 45.0


def test_baseline_summary_uses_standalone_base_llh(tmp_path: Path):
    solution = _write_minimal_pos(tmp_path)

    data = analyze_rtk_quality(solution_path=solution, base_llh=(50.0, 14.0, 250.0)).as_dict(include_empty_bins=True)

    assert data["baseline_summary"]["available"] is True
    assert data["baseline_summary"]["max_distance_km"] is not None
    assert data["baseline_summary"]["quality_by_baseline_bin"]


def test_baseline_bins_and_markdown_include_quality_evolution(tmp_path: Path):
    path = tmp_path / "baseline.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:02.000 50.000200 14.000000 250.0 2 18\n",
        encoding="ascii",
    )

    analysis = analyze_rtk_quality(solution_path=path, base_llh=(50.0, 14.0, 250.0))
    data = analysis.as_dict()
    bins = data["baseline_summary"]["quality_by_baseline_bin"]
    populated = [item for item in bins if item["populated"]]

    assert populated
    assert populated[0]["elapsed_time_s"] > 0.0
    assert "fixed_pct_of_elapsed" in populated[0]
    assert "trace_low_ar_count" in populated[0]
    markdown = format_quality_markdown(analysis)
    assert "## 12. Quality By Base-Rover Distance" in markdown
    assert "Empty baseline bins omitted" in markdown


def test_track_plausibility_exposes_bad_fixed_island(tmp_path: Path):
    path = tmp_path / "jumpy.pos"
    path.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 2 18\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:02.000 50.001500 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:03.000 50.000300 14.000000 250.0 2 18\n",
        encoding="ascii",
    )

    data = analyze_rtk_quality(solution_path=path).as_dict()

    assert data["track_plausibility"]["fixed_internal_jump_count"] >= 1
    assert data["track_plausibility"]["speed_mps_by_quality"]["fixed"]["max"] > 90.0
    markdown = format_quality_markdown(analyze_rtk_quality(solution_path=path))
    assert "## 8. Track Plausibility" in markdown


def test_quality_comparison_warns_when_cleaner_but_less_plausible() -> None:
    left = {
        "false_fix_suspicion": {"raw_fixed_time_s": 100.0, "qc_supported_fixed_time_s": 80.0},
        "long_fixed_metrics": {"fixed_time_ge_thresholds_s": {"60": 80.0}, "fixed_distance_ge_thresholds_m": {"1000": 2000.0}},
        "track_plausibility": {"track_consistency_score": 0.9, "fixed_internal_jump_count": 0, "fixed_islands_with_large_offset_count": 0},
        "residuals": {"carrier_abs_m": {"fixed_p95": 0.2}},
        "rejections": {"count": 100},
        "slips": {"raw_slip_flags_total": 1000},
    }
    right = {
        "false_fix_suspicion": {"raw_fixed_time_s": 110.0, "qc_supported_fixed_time_s": 20.0},
        "long_fixed_metrics": {"fixed_time_ge_thresholds_s": {"60": 10.0}, "fixed_distance_ge_thresholds_m": {"1000": 100.0}},
        "track_plausibility": {"track_consistency_score": 0.3, "fixed_internal_jump_count": 5, "fixed_islands_with_large_offset_count": 3},
        "residuals": {"carrier_abs_m": {"fixed_p95": 0.1}},
        "rejections": {"count": 10},
        "slips": {"raw_slip_flags_total": 10},
    }

    comparison = compare_quality_reports(left, right)

    assert comparison["warnings"]
    assert "reduced noisy observations" in comparison["warnings"][0]


def test_fixed_continuity_summary_highlights_long_highway_segment(tmp_path: Path):
    path = tmp_path / "highway-long.pos"
    lines = []
    # 36 m/s for 70 s, sampled once per second: this is useful highway continuity
    # even if older strict diagnostic confidence stays unavailable.
    for second in range(71):
        lat = 50.0 + second * 0.0003235
        lines.append(f"2026/05/30 05:{second // 60:02d}:{second % 60:02d}.000 {lat:.7f} 14.0000000 250.0 1 18\n")
    path.write_text("".join(lines), encoding="ascii")

    analysis = analyze_rtk_quality(solution_path=path, thresholds=QualityThresholds(motion_profile="highway"))
    data = analysis.as_dict()

    continuity = data["fixed_continuity_summary"]
    assert continuity["raw_fixed_time_s"] >= 70.0
    assert continuity["fixed_time_ge_60s"] >= 70.0
    assert continuity["fixed_distance_ge_1000m"] > 2.0
    assert continuity["longest_fixed_segment_distance_m"] > 2000.0
    assert "Usable long fixed coverage exists" in continuity["interpretation"]
    assert data["usable_supported_fixed_time_s"] >= 70.0

    markdown = format_quality_markdown(analysis)
    assert "## 3. Usable Fixed Continuity" in markdown
    assert "N80 means 80% of fixed time/distance lies in segments at least this long." in markdown
    assert "Median segment duration is a fragmentation diagnostic only" in markdown
    assert "## 4. Top Fixed Segments By Distance" in markdown
    assert "## 5. Top Fixed Segments By Duration" in markdown


def test_fragmented_run_warns_no_useful_long_fixed(tmp_path: Path):
    path = tmp_path / "fragmented.pos"
    lines = []
    for second in range(0, 40, 4):
        lines.append(f"2026/05/30 05:00:{second:02d}.000 50.{second:06d} 14.0000000 250.0 1 18\n")
        lines.append(f"2026/05/30 05:00:{second + 1:02d}.000 50.{second + 1:06d} 14.0000000 250.0 1 18\n")
        lines.append(f"2026/05/30 05:00:{second + 2:02d}.000 50.{second + 2:06d} 14.0000000 250.0 2 18\n")
    path.write_text("".join(lines), encoding="ascii")

    analysis = analyze_rtk_quality(solution_path=path)
    data = analysis.as_dict()

    assert data["fixed_continuity_summary"]["fixed_time_ge_30s"] == 0.0
    assert data["fixed_continuity_summary"]["fixed_distance_ge_500m"] == 0.0
    assert "No useful long fixed intervals were found" in data["fixed_continuity_summary"]["interpretation"]
    assert "No useful long fixed intervals were found" in format_quality_markdown(analysis)


def test_default_quality_json_is_compact_and_detail_json_expands(tmp_path: Path):
    path = _write_minimal_pos(tmp_path)
    analysis = analyze_rtk_quality(solution_path=path)
    compact = analysis.as_dict()
    detail = analysis.as_dict(include_all_segments=True)

    assert "segment_qc" not in compact["long_fixed_metrics"]
    assert "segment_geometry_risk" not in compact["geometry_cost"]
    assert "segment_qc" in detail["long_fixed_metrics"]
    assert "segment_geometry_risk" in detail["geometry_cost"]


def test_track_consistency_not_computed_uses_null_status(tmp_path: Path):
    path = _write_minimal_pos(tmp_path)
    data = analyze_rtk_quality(solution_path=path).as_dict()

    status = data["track_plausibility"]["track_consistency_status"]
    assert status["status"] in {"ok", "warning", "suspect", "not_computed"}
    if status["status"] == "not_computed":
        assert status["score"] is None


def test_quality_analysis_filters_solution_to_processing_window(tmp_path: Path):
    solution = tmp_path / "window.pos"
    solution.write_text(
        "2026/05/30 05:00:00.000 50.000000 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:01.000 50.000100 14.000000 250.0 1 18\n"
        "2026/05/30 05:00:02.000 50.000200 14.000000 250.0 2 18\n",
        encoding="ascii",
    )
    window = processing_window_from_values("2026-05-30T05:00:01Z", "2026-05-30T05:00:02Z")

    data = analyze_rtk_quality(solution_path=solution, processing_window=window).as_dict()

    assert data["inputs"]["quality_window_applied"] is True
    assert data["parser_coverage"]["solution_epochs"] == 2
    assert data["time_summary"]["quality_time_s"]["fixed"] == 1.0
    assert data["time_summary"]["quality_time_s"]["float"] == 0.0
    assert data["effective_processing_window"]["start_time"] == "2026-05-30T05:00:01+00:00"


def test_large_stat_slip_summary_uses_deduplicated_epoch_alignment(tmp_path: Path):
    base = gps_week_tow_to_utc_datetime(2419, 450000.0)
    solution = tmp_path / "large.pos"
    solution.write_text(
        "".join(
            f"{(base + timedelta(seconds=index)).strftime('%Y/%m/%d %H:%M:%S')}.000 "
            f"{50.0 + index * 0.000001:.7f} 14.0000000 250.0 1 18\n"
            for index in range(10_000)
        ),
        encoding="ascii",
    )
    stat = tmp_path / "large.stat"
    sats = [f"G{sat:02d}" for sat in range(1, 11)]
    lines = []
    for index in range(100_000):
        tow = 450000.0 + (index % 1000)
        sat = sats[index % len(sats)]
        # Same epoch/sat/frequency repeats many times and must collapse.
        lines.append(f"$SAT,2419,{tow:.1f},{sat},L1,45,120,0.12,2.5,1,42,0,1,0,0,0,0\n")
    stat.write_text("".join(lines), encoding="ascii")

    data = analyze_rtk_quality(solution_path=solution, stat_path=stat).as_dict()

    assert data["slips"]["raw_slip_flags_total"] == 100_000
    assert data["slips"]["deduplicated_slip_events_total"] == 1000
    assert data["slips"]["unique_slip_epochs"] == 1000
    assert data["slips"]["epochs_with_slip"] == 1000
    assert data["performance"]["raw_slip_flags"] == 100_000
    assert data["performance"]["dedup_slip_events"] == 1000


def test_quality_stat_max_lines_marks_limited_confidence(tmp_path: Path):
    solution = _write_minimal_pos(tmp_path)
    stat = tmp_path / "limited.stat"
    stat.write_text(
        "".join("$SAT,2419,450000.0,G01,L1,45,120,0.12,2.5,1,42,0,1,0,0,0,0\n" for _ in range(10)),
        encoding="ascii",
    )

    data = analyze_rtk_quality(solution_path=solution, stat_path=stat, stat_max_lines=3).as_dict()

    assert data["parser_coverage"]["stat_truncated"] is True
    assert data["parser_coverage"]["stat_lines"] == 3
    assert any("STAT parsing truncated" in warning for warning in data["warnings"])


def test_quality_fast_skips_stat_detail(tmp_path: Path):
    solution = _write_minimal_pos(tmp_path)
    stat = tmp_path / "run.stat"
    stat.write_text("$SAT,2419,450000.0,G01,L1,45,120,0.12,2.5,1,42,0,1,0,0,0,0\n", encoding="ascii")

    data = analyze_rtk_quality(solution_path=solution, stat_path=stat, fast=True).as_dict()

    assert data["inputs"]["quality_fast"] is True
    assert data["inputs"]["stat_available"] is False
    assert any("quality-fast enabled" in warning for warning in data["warnings"])


def test_no_linear_nearest_epoch_scan_in_quality_module() -> None:
    source = Path("src/um980_rtklib_pipeline/quality.py").read_text(encoding="utf-8")

    assert "min(epochs, key=" not in source


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


def _epoch_at(base: datetime, offset_s: float):
    return SolutionEpoch(
        time=base + timedelta(seconds=offset_s),
        lat=50.0,
        lon=14.0,
        height_m=250.0,
        quality="fixed",
        raw_quality=1,
        num_sats=16,
        hdop=None,
        source="pos",
    )
    return path


def _write_minimal_nmea(tmp_path: Path) -> Path:
    path = tmp_path / "run.nmea"
    path.write_text(_rmc() + "\n" + _gga("050000.00", "5000.0000", "01400.0000", 4) + "\n", encoding="ascii")
    return path
