"""Analysis JSON assembly and RTK solution quality analysis."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .nmea import datetime_from_time_date, datetime_from_time_with_context, dm_to_decimal, float_or_none, int_or_none, parse_sentence, sentence_type
from .obs_decode import ObservationExtraction
from .rinex_nav import NavExtractionReport
from .solution import SolutionExtraction
from .stream import StreamDiagnostics
from .timeutil import gps_week_tow_to_utc_datetime

QUALITY_ORDER = ("fixed", "float", "dgps", "single", "invalid", "unknown")
RTKLIB_Q_TO_QUALITY = {1: "fixed", 2: "float", 4: "dgps", 5: "single", 0: "invalid"}
GGA_TO_QUALITY = {4: "fixed", 5: "float", 2: "dgps", 1: "single", 0: "invalid"}


@dataclass(frozen=True)
class SolutionEpoch:
    """One parsed solution epoch from RTKLIB POS/LLH or NMEA GGA."""

    time: datetime
    lat: float | None
    lon: float | None
    height_m: float | None
    quality: str
    raw_quality: int | str | None
    num_sats: int | None
    hdop: float | None
    source: str


@dataclass(frozen=True)
class Segment:
    """Contiguous solution segment with the same quality state."""

    quality: str
    start_time: datetime
    end_time: datetime
    duration_s: float
    distance_m: float
    epoch_count: int
    median_speed_mps: float | None
    max_step_m: float | None
    start_index: int
    end_index: int
    raw_distance_m: float
    clipped_distance_m: float
    mean_speed_mps: float | None
    median_step_m: float | None


@dataclass(frozen=True)
class StatEpochSummary:
    """Tolerant summary of RTKLIB `.stat` evidence for one epoch."""

    time: datetime
    q: int | None
    used_count: int
    rejected_count: int
    slip_count: int
    lock_reset_count: int
    carrier_residual_abs_m: list[float]
    code_residual_abs_m: list[float]
    snr_values: list[float]
    by_sat: dict[str, dict[str, object]]


@dataclass(frozen=True)
class QualityThresholds:
    """Thresholds controlling quality-analysis warnings and classification."""

    trusted_fixed_min_duration_s: float = 10.0
    trusted_fixed_min_distance_m: float = 20.0
    provisional_fixed_min_duration_s: float = 3.0
    recent_slip_window_s: float = 10.0
    transition_jump_warning_m: float = 1.0
    transition_jump_severe_m: float = 3.0
    vertical_jump_warning_m: float = 1.5
    carrier_residual_warning_m: float = 0.20
    carrier_residual_severe_m: float = 0.50
    code_residual_warning_m: float = 5.0
    code_residual_severe_m: float = 10.0
    low_used_signals_warning: int = 12
    low_snr_warning_dbhz: float = 35.0
    gap_split_s: float = 2.0
    stationary_speed_threshold_mps: float = 0.3
    jump_clip_m: float = 100.0
    motion_profile: str = "auto"
    max_speed_mps: float | None = None
    max_accel_mps2: float | None = None
    transition_window_s: float = 2.0
    route_bin_km: float | None = 10.0
    baseline_bins: list[float] = field(default_factory=lambda: [0, 10, 20, 30, 40, 50, 75, 100, 150])


@dataclass(frozen=True)
class QualityAnalysis:
    """Full RTK solution quality-analysis result."""

    inputs: dict[str, object]
    parser_coverage: dict[str, object]
    time_summary: dict[str, object]
    distance_summary: dict[str, object]
    segments: dict[str, object]
    residuals: dict[str, object]
    slips: dict[str, object]
    rejections: dict[str, object]
    transition_jumps: dict[str, object]
    false_fix_suspicion: dict[str, object]
    motion: dict[str, object] = field(default_factory=dict)
    dropout_reacquisition: dict[str, object] = field(default_factory=dict)
    baseline_summary: dict[str, object] = field(default_factory=dict)
    route_bins: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trace: dict[str, object] | None = None
    cleanup: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return stable JSON-friendly analysis data."""

        return {
            "inputs": self.inputs,
            "parser_coverage": self.parser_coverage,
            "time_summary": self.time_summary,
            "distance_summary": self.distance_summary,
            "segments": self.segments,
            "residuals": self.residuals,
            "slips": self.slips,
            "rejections": self.rejections,
            "transition_jumps": self.transition_jumps,
            "false_fix_suspicion": self.false_fix_suspicion,
            "motion": self.motion,
            "dropout_reacquisition": self.dropout_reacquisition,
            "baseline_summary": self.baseline_summary,
            "route_bins": self.route_bins,
            "trace": self.trace or {"available": False},
            "cleanup": self.cleanup
            or {
                "trace_cleanup_requested": False,
                "trace_deleted": False,
                "stat_cleanup_requested": False,
                "stat_files_deleted": [],
                "stat_files_kept": [],
            },
            "warnings": self.warnings,
        }


@dataclass
class _StatAccumulator:
    stat_lines: int = 0
    sat_lines: int = 0
    parsed_sat_lines: int = 0
    unparsed_sat_lines: int = 0
    carrier_residual_abs_m: list[float] = field(default_factory=list)
    code_residual_abs_m: list[float] = field(default_factory=list)
    slip_times: list[datetime] = field(default_factory=list)
    slip_events: set[tuple[datetime, str, str, str]] = field(default_factory=set)
    slip_count: int = 0
    rejected_count: int = 0
    used_counts_by_time: dict[datetime, int] = field(default_factory=dict)
    snr_values_by_time: dict[datetime, list[float]] = field(default_factory=dict)
    residuals_by_time: dict[datetime, dict[str, list[float]]] = field(default_factory=dict)
    top_residuals_by_sat: dict[str, int] = field(default_factory=dict)
    slips_by_sat: dict[str, int] = field(default_factory=dict)
    rejections_by_sat: dict[str, int] = field(default_factory=dict)


def build_analysis(
    *,
    stream: StreamDiagnostics,
    solutions: SolutionExtraction,
    observations: ObservationExtraction,
    rover_nav: NavExtractionReport | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble analysis JSON data.

    Args:
        stream: Stream parser diagnostics.
        solutions: Solution extraction result.
        observations: Observation extraction result.
        rover_nav: Optional rover navigation extraction report.
        extra: Optional extra top-level fields.

    Returns:
        JSON-friendly analysis dictionary.
    """

    analysis: dict[str, object] = {
        "stream": stream.as_dict(),
        "solution_points": len(solutions.solution_points),
        "nmea_cadence": solutions.nmea_cadence,
        "raw_observations": observations.metrics,
        "observation_decode": {
            "unsupported_records": observations.unsupported_records,
            "time_unknown_reasons": observations.time_unknown_reasons,
            "skipped_non_observation_records": observations.skipped_records,
        },
        "unsupported_observation_records": observations.unsupported_records,
        "warnings": list(dict.fromkeys([*solutions.warnings, *observations.warnings])),
    }
    if rover_nav is not None:
        analysis["ephemeris"] = rover_nav.as_dict()
        analysis["warnings"].extend(rover_nav.warnings)  # type: ignore[index,union-attr]
    if extra:
        analysis.update(extra)
    analysis["warnings"] = list(dict.fromkeys(analysis.get("warnings", [])))  # type: ignore[arg-type]
    return analysis


def write_analysis_json(path: Path, analysis: dict[str, object]) -> None:
    """Write analysis JSON to disk.

    Args:
        path: Destination JSON path.
        analysis: Analysis dictionary to serialise.
    """

    path.write_text(json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def analyze_rtk_quality(
    *,
    solution_path: Path,
    stat_path: Path | None = None,
    thresholds: QualityThresholds | None = None,
    trace_summary: dict[str, object] | None = None,
    cleanup: dict[str, object] | None = None,
) -> QualityAnalysis:
    """Analyse RTKLIB solution quality from `.nmea`/`.pos` and optional `.stat`.

    Args:
        solution_path: RTKLIB NMEA, POS or LLH solution output.
        stat_path: Optional RTKLIB `.stat` file.
        thresholds: Analysis thresholds.

    Returns:
        Structured quality analysis.
    """

    limits = thresholds or QualityThresholds()
    epochs, solution_warnings, solution_type = parse_solution_epochs(solution_path)
    stat = parse_stat_file(stat_path) if stat_path else None
    warnings = [*solution_warnings]
    if stat_path and stat is None:
        warnings.append(f"stat file unavailable or unreadable: {stat_path}")
    if stat_path is None:
        warnings.append("stat file not supplied; residual/slip/rejection analysis unavailable")
    elif stat is not None and stat.parsed_sat_lines == 0:
        warnings.append("stat residual/slip metrics unavailable: no recognised $SAT lines")

    segments = compute_segments(epochs, gap_split_s=limits.gap_split_s, jump_clip_m=limits.jump_clip_m)
    time_summary = _time_summary(epochs, limits)
    distance_summary = _distance_summary(epochs)
    residuals = _residual_summary(stat, epochs) if stat else _empty_residual_summary()
    slips = _slip_summary(stat, epochs) if stat else _empty_slip_summary()
    rejections = _rejection_summary(stat) if stat else {"available": False, "count": None, "top_satellites": []}
    motion = _motion_summary(epochs, limits)
    transitions = _transition_summary(epochs, segments, limits, motion)
    dropout = _dropout_reacquisition_summary(epochs, segments, limits)
    suspicion = _false_fix_suspicion(segments, epochs, stat, transitions, residuals, slips, limits)
    baseline_summary = _baseline_summary(epochs, limits)
    route_bins = _route_bins(epochs, limits)
    if residuals.get("available") and residuals.get("quality_aligned") is False:
        warnings.append("STAT residuals parsed globally but not aligned to solution quality states; residuals not used for hard fixed-epoch suspicion.")
    if slips.get("available") and slips.get("epochs_with_slip_pct") is None:
        warnings.append("STAT slip flags are not sufficiently time-aligned/deduplicated; raw slip counts are not used for hard fixed-epoch suspicion.")
    warnings.extend(_top_warnings(time_summary, suspicion, transitions, residuals, slips))
    parser_coverage = {
        "solution_epochs": len(epochs),
        "solution_warnings": solution_warnings,
        "stat_lines": stat.stat_lines if stat else 0,
        "stat_sat_lines_parsed": stat.parsed_sat_lines if stat else 0,
        "stat_sat_lines_unparsed": stat.unparsed_sat_lines if stat else 0,
        "warnings": warnings,
    }
    return QualityAnalysis(
        inputs={
            "solution": str(solution_path),
            "stat": str(stat_path) if stat_path else None,
            "solution_type": solution_type,
            "stat_available": stat is not None,
        },
        parser_coverage=parser_coverage,
        time_summary=time_summary,
        distance_summary=distance_summary,
        segments=_segment_summary(segments),
        residuals=residuals,
        slips=slips,
        rejections=rejections,
        transition_jumps=transitions,
        false_fix_suspicion=suspicion,
        motion=motion,
        dropout_reacquisition=dropout,
        baseline_summary=baseline_summary,
        route_bins=route_bins,
        warnings=list(dict.fromkeys(warnings)),
        trace=trace_summary,
        cleanup=cleanup,
    )


def parse_solution_epochs(path: Path) -> tuple[list[SolutionEpoch], list[str], str]:
    """Parse NMEA GGA/RMC or RTKLIB POS solution epochs."""

    text = path.read_text(encoding="ascii", errors="ignore")
    if any(line.lstrip().startswith("$") for line in text.splitlines()):
        epochs, warnings = _parse_nmea_epochs(text.splitlines())
        return epochs, warnings, "nmea"
    return _parse_pos_epochs(text.splitlines()), [], "pos"


def _parse_nmea_epochs(lines: list[str]) -> tuple[list[SolutionEpoch], list[str]]:
    warnings: list[str] = []
    context_date: datetime | None = None
    previous_time: datetime | None = None
    day_offset = timedelta(0)
    pending: list[tuple[datetime | None, list[str]]] = []
    epochs: list[SolutionEpoch] = []

    for line in lines:
        parsed = parse_sentence(line.strip())
        if parsed is None:
            continue
        typ = sentence_type(parsed.talker_type)
        if typ == "RMC" and len(parsed.fields) >= 9:
            status = parsed.fields[1] if len(parsed.fields) > 1 else ""
            if status in {"A", "V", ""}:
                dt = datetime_from_time_date(parsed.fields[0], parsed.fields[8])
                if dt is not None:
                    context_date = dt
                    for _time_hint, fields in pending:
                        epoch = _gga_epoch_from_fields(fields, context_date, previous_time, day_offset)
                        if epoch is not None:
                            epoch, previous_time, day_offset = _normalise_nmea_epoch_time(epoch, previous_time, day_offset)
                            epochs.append(epoch)
                    pending.clear()
        elif typ == "GGA":
            if context_date is None:
                pending.append((None, parsed.fields))
                continue
            epoch = _gga_epoch_from_fields(parsed.fields, context_date, previous_time, day_offset)
            if epoch is not None:
                epoch, previous_time, day_offset = _normalise_nmea_epoch_time(epoch, previous_time, day_offset)
                epochs.append(epoch)
    if pending:
        warnings.append("NMEA GGA date could not be inferred from RMC; using 1970-01-01 UTC for relative analysis")
        context_date = datetime(1970, 1, 1, tzinfo=UTC)
        for _time_hint, fields in pending:
            epoch = _gga_epoch_from_fields(fields, context_date, previous_time, day_offset)
            if epoch is not None:
                epoch, previous_time, day_offset = _normalise_nmea_epoch_time(epoch, previous_time, day_offset)
                epochs.append(epoch)
    epochs.sort(key=lambda item: item.time)
    return epochs, warnings


def _gga_epoch_from_fields(
    fields: list[str],
    context_date: datetime,
    _previous_time: datetime | None,
    _day_offset: timedelta,
) -> SolutionEpoch | None:
    if len(fields) < 9:
        return None
    dt = datetime_from_time_with_context(fields[0], context_date)
    if dt is None:
        return None
    lat = dm_to_decimal(fields[1], fields[2])
    lon = dm_to_decimal(fields[3], fields[4])
    raw_quality = int_or_none(fields[5])
    return SolutionEpoch(
        time=dt,
        lat=lat,
        lon=lon,
        height_m=float_or_none(fields[8]),
        quality=GGA_TO_QUALITY.get(raw_quality, "unknown"),
        raw_quality=raw_quality,
        num_sats=int_or_none(fields[6]),
        hdop=float_or_none(fields[7]),
        source="nmea",
    )


def _normalise_nmea_epoch_time(
    epoch: SolutionEpoch,
    previous_time: datetime | None,
    day_offset: timedelta,
) -> tuple[SolutionEpoch, datetime, timedelta]:
    current = epoch.time + day_offset
    if previous_time is not None and current < previous_time:
        day_offset += timedelta(days=1)
        current = epoch.time + day_offset
    normalised = SolutionEpoch(
        time=current,
        lat=epoch.lat,
        lon=epoch.lon,
        height_m=epoch.height_m,
        quality=epoch.quality,
        raw_quality=epoch.raw_quality,
        num_sats=epoch.num_sats,
        hdop=epoch.hdop,
        source=epoch.source,
    )
    return normalised, current, day_offset


def _parse_pos_epochs(lines: list[str]) -> list[SolutionEpoch]:
    epochs: list[SolutionEpoch] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("%"):
            continue
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            dt = datetime.strptime(f"{fields[0]} {fields[1]}", "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=UTC)
        except ValueError:
            try:
                dt = datetime.strptime(f"{fields[0]} {fields[1]}", "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                continue
        raw_quality = int_or_none(fields[5])
        epochs.append(
            SolutionEpoch(
                time=dt,
                lat=float_or_none(fields[2]),
                lon=float_or_none(fields[3]),
                height_m=float_or_none(fields[4]),
                quality=RTKLIB_Q_TO_QUALITY.get(raw_quality, "unknown"),
                raw_quality=raw_quality,
                num_sats=int_or_none(fields[6]) if len(fields) > 6 else None,
                hdop=None,
                source="pos",
            )
        )
    return epochs


def compute_segments(
    epochs: list[SolutionEpoch],
    *,
    gap_split_s: float,
    jump_clip_m: float,
) -> list[Segment]:
    """Compute contiguous same-quality solution segments."""

    if not epochs:
        return []
    segments: list[Segment] = []
    start = 0
    for index in range(1, len(epochs)):
        previous = epochs[index - 1]
        current = epochs[index]
        gap = (current.time - previous.time).total_seconds()
        if (
            current.quality != previous.quality
            or gap > gap_split_s
            or gap < 0
            or current.lat is None
            or current.lon is None
            or previous.lat is None
            or previous.lon is None
        ):
            segments.append(_segment_from_epochs(epochs, start, index - 1, jump_clip_m=jump_clip_m))
            start = index
    segments.append(_segment_from_epochs(epochs, start, len(epochs) - 1, jump_clip_m=jump_clip_m))
    return segments


def _segment_from_epochs(epochs: list[SolutionEpoch], start: int, end: int, *, jump_clip_m: float) -> Segment:
    rows = epochs[start : end + 1]
    steps: list[float] = []
    clipped_steps: list[float] = []
    speeds: list[float] = []
    for left, right in zip(rows, rows[1:], strict=False):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        step = _haversine_m(left.lat, left.lon, right.lat, right.lon)
        dt = (right.time - left.time).total_seconds()
        steps.append(step)
        if step <= jump_clip_m:
            clipped_steps.append(step)
        if dt > 0:
            speeds.append(step / dt)
    duration = max(0.0, (rows[-1].time - rows[0].time).total_seconds())
    return Segment(
        quality=rows[0].quality,
        start_time=rows[0].time,
        end_time=rows[-1].time,
        duration_s=duration,
        distance_m=sum(clipped_steps),
        epoch_count=len(rows),
        median_speed_mps=_percentile(speeds, 50),
        max_step_m=max(steps) if steps else None,
        start_index=start,
        end_index=end,
        raw_distance_m=sum(steps),
        clipped_distance_m=sum(clipped_steps),
        mean_speed_mps=(sum(speeds) / len(speeds)) if speeds else None,
        median_step_m=_percentile(steps, 50),
    )


def parse_stat_file(path: Path | None) -> _StatAccumulator | None:
    """Parse a tolerant subset of RTKLIB `.stat` SAT rows."""

    if path is None or not path.exists():
        return None
    stat = _StatAccumulator()
    for line in path.read_text(encoding="ascii", errors="ignore").splitlines():
        stat.stat_lines += 1
        if not line.startswith("$SAT"):
            continue
        stat.sat_lines += 1
        if not _parse_sat_stat_line(line, stat):
            stat.unparsed_sat_lines += 1
    return stat


def _parse_sat_stat_line(line: str, stat: _StatAccumulator) -> bool:
    fields = line.split(",")
    if len(fields) < 10:
        return False
    week = int_or_none(fields[1])
    tow = float_or_none(fields[2])
    if week is None or tow is None:
        return False
    try:
        time = gps_week_tow_to_utc_datetime(week, tow)
    except Exception:
        return False
    sat = fields[3] if len(fields) > 3 else "?"
    carrier = _abs_float(fields[7]) if len(fields) > 7 else None
    code = _abs_float(fields[8]) if len(fields) > 8 else None
    used = int_or_none(fields[9]) if len(fields) > 9 else None
    snr = float_or_none(fields[10]) if len(fields) > 10 else None
    slip = _any_int(fields[12:13] + fields[15:16])
    rejected = _any_int(fields[16:17])
    stat.parsed_sat_lines += 1
    if carrier is not None:
        stat.carrier_residual_abs_m.append(carrier)
        stat.residuals_by_time.setdefault(time, {"carrier": [], "code": []})["carrier"].append(carrier)
        if carrier > 0.2:
            stat.top_residuals_by_sat[sat] = stat.top_residuals_by_sat.get(sat, 0) + 1
    if code is not None:
        stat.code_residual_abs_m.append(code)
        stat.residuals_by_time.setdefault(time, {"carrier": [], "code": []})["code"].append(code)
        if code > 5.0:
            stat.top_residuals_by_sat[sat] = stat.top_residuals_by_sat.get(sat, 0) + 1
    if used and used > 0:
        stat.used_counts_by_time[time] = stat.used_counts_by_time.get(time, 0) + 1
    if snr is not None:
        stat.snr_values_by_time.setdefault(time, []).append(snr)
    if slip:
        stat.slip_count += 1
        stat.slip_times.append(time)
        stat.slip_events.add((time, sat, fields[4] if len(fields) > 4 else "", "slip"))
        stat.slips_by_sat[sat] = stat.slips_by_sat.get(sat, 0) + 1
    if rejected:
        stat.rejected_count += 1
        stat.rejections_by_sat[sat] = stat.rejections_by_sat.get(sat, 0) + 1
    return True


def _time_summary(epochs: list[SolutionEpoch], thresholds: QualityThresholds) -> dict[str, object]:
    if not epochs:
        return _empty_time_summary()
    deltas = [
        (right.time - left.time).total_seconds()
        for left, right in zip(epochs, epochs[1:], strict=False)
        if (right.time - left.time).total_seconds() > 0
    ]
    median_delta = _percentile(deltas, 50)
    elapsed = max(0.0, (epochs[-1].time - epochs[0].time).total_seconds())
    expected_epochs = int(round(elapsed / median_delta)) + 1 if median_delta and elapsed > 0 else len(epochs)
    missing_epochs = max(0, expected_epochs - len(epochs))
    missing_time = missing_epochs * median_delta if median_delta else 0.0
    quality_time = {quality: 0.0 for quality in QUALITY_ORDER}
    gaps: list[float] = []
    for left, right in zip(epochs, epochs[1:], strict=False):
        dt = (right.time - left.time).total_seconds()
        if dt <= 0:
            continue
        if dt > thresholds.gap_split_s:
            gaps.append(dt)
        quality_time[left.quality] = quality_time.get(left.quality, 0.0) + min(dt, thresholds.gap_split_s)
    emitted_time = sum(quality_time.values())
    return {
        "duration_s": elapsed,
        "median_epoch_interval_s": median_delta,
        "emitted_epochs": len(epochs),
        "expected_epochs": expected_epochs,
        "missing_epochs": missing_epochs,
        "missing_time_s": missing_time,
        "missing_pct": (100.0 * missing_time / elapsed) if elapsed else 0.0,
        "longest_output_gap_s": max(gaps) if gaps else 0.0,
        "output_gaps_gt_2s": sum(1 for gap in gaps if gap > 2.0),
        "output_gaps_gt_10s": sum(1 for gap in gaps if gap > 10.0),
        "output_gaps_gt_60s": sum(1 for gap in gaps if gap > 60.0),
        "quality_time_s": quality_time,
        "quality_pct_of_elapsed": _percentages(quality_time, elapsed),
        "quality_pct_of_emitted": _percentages(quality_time, emitted_time),
    }


def _distance_summary(epochs: list[SolutionEpoch]) -> dict[str, object]:
    quality_distance = {quality: 0.0 for quality in QUALITY_ORDER}
    total = 0.0
    for left, right in zip(epochs, epochs[1:], strict=False):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        distance = _haversine_m(left.lat, left.lon, right.lat, right.lon)
        total += distance
        quality_distance[right.quality] = quality_distance.get(right.quality, 0.0) + distance
    return {
        "total_distance_m": total,
        "quality_distance_m": quality_distance,
        "quality_pct_of_distance": _percentages(quality_distance, total),
    }


def _segment_summary(segments: list[Segment]) -> dict[str, object]:
    result: dict[str, object] = {}
    for quality in QUALITY_ORDER:
        selected = [segment for segment in segments if segment.quality == quality]
        durations = [segment.duration_s for segment in selected]
        distances = [segment.distance_m for segment in selected]
        result[quality] = {
            "count": len(selected),
            "duration_s": _stats(durations),
            "distance_m": _stats(distances),
            "time_ge_thresholds_s": {str(th): sum(item.duration_s for item in selected if item.duration_s >= th) for th in (3, 5, 10, 30, 60)},
            "distance_ge_thresholds_m": {str(th): sum(item.distance_m for item in selected if item.distance_m >= th) for th in (10, 20, 50, 100, 500, 1000)},
            "longest_by_time": [_segment_dict(item) for item in sorted(selected, key=lambda item: item.duration_s, reverse=True)[:5]],
            "longest_by_distance": [_segment_dict(item) for item in sorted(selected, key=lambda item: item.distance_m, reverse=True)[:5]],
        }
    return result


def _motion_summary(epochs: list[SolutionEpoch], thresholds: QualityThresholds) -> dict[str, object]:
    speeds: list[float] = []
    for left, right in zip(epochs, epochs[1:], strict=False):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        dt = (right.time - left.time).total_seconds()
        if dt <= 0:
            continue
        speeds.append(_haversine_m(left.lat, left.lon, right.lat, right.lon) / dt)
    p95 = _percentile(speeds, 95) or 0.0
    requested = thresholds.motion_profile
    if requested == "auto":
        if p95 <= 1.0:
            profile = "static"
        elif p95 <= 3.0:
            profile = "walking"
        elif p95 <= 15.0:
            profile = "cycling"
        elif p95 <= 45.0:
            profile = "vehicle"
        else:
            profile = "highway"
    else:
        profile = requested
    defaults = {"static": 1.0, "walking": 3.0, "cycling": 15.0, "vehicle": 45.0, "highway": 60.0}
    max_speed = thresholds.max_speed_mps or defaults.get(profile, 45.0)
    return {
        "requested_profile": requested,
        "inferred_profile": profile,
        "median_speed_mps": _percentile(speeds, 50),
        "p95_speed_mps": _percentile(speeds, 95),
        "max_speed_mps": max(speeds) if speeds else None,
        "max_speed_threshold_mps": max_speed,
        "max_accel_threshold_mps2": thresholds.max_accel_mps2,
    }


def _dropout_reacquisition_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    _thresholds: QualityThresholds,
) -> dict[str, object]:
    outages: list[float] = []
    reacq: list[float] = []
    for previous, outage, current in zip(segments, segments[1:], segments[2:], strict=False):
        if previous.quality == "fixed" and current.quality == "fixed" and outage.quality != "fixed":
            outages.append(outage.duration_s)
            if previous.end_index < len(epochs) and current.start_index < len(epochs):
                reacq.append(max(0.0, (epochs[current.start_index].time - epochs[previous.end_index].time).total_seconds()))
    return {
        "likely_occlusion_events": len(outages),
        "median_outage_s": _percentile(outages, 50),
        "max_outage_s": max(outages) if outages else None,
        "median_reacquisition_s": _percentile(reacq, 50),
    }


def _baseline_summary(_epochs: list[SolutionEpoch], thresholds: QualityThresholds) -> dict[str, object]:
    return {
        "available": False,
        "reason": "base coordinates not supplied to quality analyzer",
        "bins_km": thresholds.baseline_bins,
        "interpretation": "Baseline distance is reported for context when available and is not used by itself to mark fixed epochs suspect.",
    }


def _route_bins(epochs: list[SolutionEpoch], thresholds: QualityThresholds) -> list[dict[str, object]]:
    if thresholds.route_bin_km is None or thresholds.route_bin_km <= 0:
        return []
    bins: list[dict[str, object]] = []
    bin_m = thresholds.route_bin_km * 1000.0
    current_start = 0.0
    quality_time: dict[str, float] = {quality: 0.0 for quality in QUALITY_ORDER}
    distance = 0.0
    for left, right in zip(epochs, epochs[1:], strict=False):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        step = _haversine_m(left.lat, left.lon, right.lat, right.lon)
        dt = max(0.0, (right.time - left.time).total_seconds())
        while distance + step >= current_start + bin_m and step > 0:
            bins.append(
                {
                    "start_km": current_start / 1000.0,
                    "end_km": (current_start + bin_m) / 1000.0,
                    "quality_time_s": dict(quality_time),
                }
            )
            current_start += bin_m
            quality_time = {quality: 0.0 for quality in QUALITY_ORDER}
        quality_time[left.quality] = quality_time.get(left.quality, 0.0) + dt
        distance += step
    if epochs:
        bins.append({"start_km": current_start / 1000.0, "end_km": distance / 1000.0, "quality_time_s": dict(quality_time)})
    return bins


def _false_fix_suspicion(
    segments: list[Segment],
    epochs: list[SolutionEpoch],
    stat: _StatAccumulator | None,
    transitions: dict[str, object],
    residuals: dict[str, object],
    slips: dict[str, object],
    thresholds: QualityThresholds,
) -> dict[str, object]:
    trusted_time = provisional_time = suspect_time = 0.0
    trusted_distance = provisional_distance = suspect_distance = 0.0
    unknown_time = unknown_distance = 0.0
    reasons = {
        "short_time_segment": 0.0,
        "short_distance_while_moving": 0.0,
        "recent_slip": 0.0,
        "high_residual": 0.0,
        "transition_jump": 0.0,
        "incomplete_diagnostics": 0.0,
    }
    fixed_entry_warning_indexes = {
        item["segment_start_index"]
        for item in transitions.get("fixed_entry_jumps", [])  # type: ignore[union-attr]
        if isinstance(item, dict) and item.get("motion_anomaly") in {"warning", "severe"}
    }
    residuals_hard = bool(residuals.get("quality_aligned"))
    slips_hard = slips.get("epochs_with_slip_pct") is not None
    for segment in segments:
        if segment.quality != "fixed":
            continue
        flags: list[str] = []
        if segment.duration_s < thresholds.trusted_fixed_min_duration_s:
            flags.append("short_time_segment")
        if segment.duration_s < thresholds.provisional_fixed_min_duration_s:
            flags.append("very_short_time_segment")
        if (
            segment.distance_m < thresholds.trusted_fixed_min_distance_m
            and (segment.median_speed_mps or 0.0) >= thresholds.stationary_speed_threshold_mps
        ):
            flags.append("short_distance_while_moving")
        if stat and slips_hard and _has_recent_slip(stat, segment, thresholds.recent_slip_window_s):
            flags.append("recent_slip")
        if residuals_hard and _segment_high_residual(stat, segment, thresholds):
            flags.append("high_residual")
        if segment.start_index in fixed_entry_warning_indexes:
            flags.append("transition_jump")
        for flag in set(flags):
            if flag in reasons:
                reasons[flag] += segment.duration_s
        diagnostics_incomplete = bool(stat) and (not residuals_hard or not slips_hard)
        if diagnostics_incomplete:
            reasons["incomplete_diagnostics"] += segment.duration_s
        severe = "transition_jump" in flags or ("recent_slip" in flags and "short_time_segment" in flags)
        if severe:
            suspect_time += segment.duration_s
            suspect_distance += segment.distance_m
        elif flags:
            provisional_time += segment.duration_s
            provisional_distance += segment.distance_m
        elif diagnostics_incomplete:
            unknown_time += segment.duration_s
            unknown_distance += segment.distance_m
        else:
            trusted_time += segment.duration_s
            trusted_distance += segment.distance_m
    total_fixed_time = trusted_time + provisional_time + suspect_time + unknown_time
    total_fixed_distance = trusted_distance + provisional_distance + suspect_distance + unknown_distance
    return {
        "qc_confidence_available": total_fixed_time > 0.0,
        "raw_fixed_time_s": total_fixed_time,
        "raw_fixed_distance_m": total_fixed_distance,
        "qc_supported_fixed_time_s": trusted_time,
        "qc_supported_fixed_distance_m": trusted_distance,
        "qc_provisional_fixed_time_s": provisional_time,
        "qc_provisional_fixed_distance_m": provisional_distance,
        "qc_suspect_fixed_time_s": suspect_time,
        "qc_suspect_fixed_distance_m": suspect_distance,
        "qc_unknown_fixed_time_s": unknown_time,
        "qc_unknown_fixed_distance_m": unknown_distance,
        "qc_limitations": _qc_limitations(residuals, slips),
        "trusted_fixed_time_s": trusted_time,
        "provisional_fixed_time_s": provisional_time,
        "suspect_fixed_time_s": suspect_time,
        "trusted_fixed_distance_m": trusted_distance,
        "provisional_fixed_distance_m": provisional_distance,
        "suspect_fixed_distance_m": suspect_distance,
        "trusted_fixed_time_pct": (100.0 * trusted_time / total_fixed_time) if total_fixed_time else 0.0,
        "provisional_fixed_time_pct": (100.0 * provisional_time / total_fixed_time) if total_fixed_time else 0.0,
        "suspect_fixed_time_pct": (100.0 * suspect_time / total_fixed_time) if total_fixed_time else 0.0,
        "trusted_fixed_distance_pct": (100.0 * trusted_distance / total_fixed_distance) if total_fixed_distance else 0.0,
        "provisional_fixed_distance_pct": (100.0 * provisional_distance / total_fixed_distance) if total_fixed_distance else 0.0,
        "suspect_fixed_distance_pct": (100.0 * suspect_distance / total_fixed_distance) if total_fixed_distance else 0.0,
        "reasons": reasons,
    }


def _qc_limitations(residuals: dict[str, object], slips: dict[str, object]) -> list[str]:
    limitations: list[str] = []
    if residuals.get("available") and residuals.get("quality_aligned") is False:
        limitations.append("residuals_global_not_quality_aligned")
    if slips.get("available") and slips.get("epochs_with_slip_pct") is None:
        limitations.append("slips_not_time_aligned")
    if residuals.get("available") is False:
        limitations.append("residuals_unavailable")
    if slips.get("available") is False:
        limitations.append("slips_unavailable")
    return limitations


def _transition_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    thresholds: QualityThresholds,
    motion: dict[str, object],
) -> dict[str, object]:
    fixed_entries: list[dict[str, object]] = []
    fixed_exits: list[dict[str, object]] = []
    setattr(_transition_jump_item, "_max_speed_mps", float(motion.get("max_speed_threshold_mps") or 45.0))
    for previous, current in zip(segments, segments[1:], strict=False):
        if current.quality == "fixed" and previous.quality != "fixed":
            item = _transition_jump_item(epochs[previous.end_index], epochs[current.start_index], current.start_index)
            if item:
                fixed_entries.append(item)
        if previous.quality == "fixed" and current.quality != "fixed":
            item = _transition_jump_item(epochs[previous.end_index], epochs[current.start_index], previous.start_index)
            if item:
                fixed_exits.append(item)
    return {
        "fixed_entry_gt_warning": sum(1 for item in fixed_entries if item.get("motion_anomaly") in {"warning", "severe"}),
        "fixed_entry_gt_severe": sum(1 for item in fixed_entries if item.get("motion_anomaly") == "severe"),
        "fixed_exit_gt_warning": sum(1 for item in fixed_exits if item.get("motion_anomaly") in {"warning", "severe"}),
        "fixed_exit_gt_severe": sum(1 for item in fixed_exits if item.get("motion_anomaly") == "severe"),
        "largest_fixed_entry_jump_m": max((item["horizontal_m"] for item in fixed_entries), default=0.0),
        "largest_fixed_exit_jump_m": max((item["horizontal_m"] for item in fixed_exits), default=0.0),
        "fixed_entry_jumps": fixed_entries,
        "fixed_exit_jumps": fixed_exits,
    }


def _transition_jump_item(left: SolutionEpoch, right: SolutionEpoch, segment_start_index: int) -> dict[str, object] | None:
    if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
        return None
    vertical = None
    if left.height_m is not None and right.height_m is not None:
        vertical = abs(right.height_m - left.height_m)
    dt = max(0.0, (right.time - left.time).total_seconds())
    horizontal = _haversine_m(left.lat, left.lon, right.lat, right.lon)
    implied_speed = (horizontal / dt) if dt else None
    max_speed = getattr(_transition_jump_item, "_max_speed_mps", 45.0)
    anomaly = "none"
    if implied_speed is not None:
        if implied_speed > max_speed * 1.8:
            anomaly = "severe"
        elif implied_speed > max_speed * 1.25:
            anomaly = "warning"
    return {
        "time": right.time.isoformat(),
        "segment_start_index": segment_start_index,
        "from_quality": left.quality,
        "to_quality": right.quality,
        "dt_s": dt,
        "horizontal_m": horizontal,
        "implied_speed_mps": implied_speed,
        "vertical_m": vertical,
        "motion_anomaly": anomaly,
    }


def _residual_summary(stat: _StatAccumulator | None, epochs: list[SolutionEpoch]) -> dict[str, object]:
    if stat is None or stat.parsed_sat_lines == 0:
        return _empty_residual_summary()
    by_quality: dict[str, dict[str, list[float]]] = {quality: {"carrier": [], "code": []} for quality in QUALITY_ORDER}
    for time, residuals in stat.residuals_by_time.items():
        nearest = _nearest_epoch(epochs, time)
        if nearest is None:
            continue
        by_quality[nearest.quality]["carrier"].extend(residuals.get("carrier", []))
        by_quality[nearest.quality]["code"].extend(residuals.get("code", []))
    quality_aligned = any(values["carrier"] or values["code"] for values in by_quality.values())
    return {
        "available": True,
        "quality_aligned": quality_aligned,
        "carrier_abs_m": {
            "global": _stats(stat.carrier_residual_abs_m),
            "fixed_p95": _stats(by_quality["fixed"]["carrier"])["p95"],
            "float_p95": _stats(by_quality["float"]["carrier"])["p95"],
            "dgps_p95": _stats(by_quality["dgps"]["carrier"])["p95"],
        },
        "code_abs_m": {
            "global": _stats(stat.code_residual_abs_m),
            "fixed_p95": _stats(by_quality["fixed"]["code"])["p95"],
            "float_p95": _stats(by_quality["float"]["code"])["p95"],
            "dgps_p95": _stats(by_quality["dgps"]["code"])["p95"],
        },
        "by_quality": {
            quality: {"carrier_abs_m": _stats(values["carrier"]), "code_abs_m": _stats(values["code"])}
            for quality, values in by_quality.items()
        },
        "top_satellites_by_high_residual_count": _top_counts(stat.top_residuals_by_sat),
    }


def _slip_summary(stat: _StatAccumulator, epochs: list[SolutionEpoch]) -> dict[str, object]:
    if stat.parsed_sat_lines == 0:
        return _empty_slip_summary()
    duration_min = 0.0
    if epochs:
        duration_min = max(0.0, (epochs[-1].time - epochs[0].time).total_seconds() / 60.0)
    slip_epoch_indexes: set[int] = set()
    for time, _, _, _ in stat.slip_events:
        nearest = _nearest_epoch(epochs, time)
        if nearest is not None:
            try:
                slip_epoch_indexes.add(epochs.index(nearest))
            except ValueError:
                pass
    epochs_with_slip_pct = (100.0 * len(slip_epoch_indexes) / len(epochs)) if epochs and slip_epoch_indexes else None
    fixed_count = sum(1 for epoch in epochs if epoch.quality == "fixed")
    fixed_with_recent = 0
    if fixed_count and slip_epoch_indexes:
        slip_times = [epochs[index].time for index in slip_epoch_indexes]
        for epoch in epochs:
            if epoch.quality != "fixed":
                continue
            if any(0 <= (epoch.time - slip_time).total_seconds() <= 10.0 for slip_time in slip_times):
                fixed_with_recent += 1
    return {
        "available": True,
        "raw_slip_flags_total": stat.slip_count,
        "raw_slip_flags_per_min": (stat.slip_count / duration_min) if duration_min else 0.0,
        "deduplicated_slip_events_total": len(stat.slip_events),
        "epochs_with_slip": len(slip_epoch_indexes) if slip_epoch_indexes else None,
        "fixed_epochs_with_recent_slip_pct": (100.0 * fixed_with_recent / fixed_count) if fixed_count and slip_epoch_indexes else None,
        "events_total": stat.slip_count,
        "events_per_min": (stat.slip_count / duration_min) if duration_min else 0.0,
        "epochs_with_slip_pct": epochs_with_slip_pct,
        "top_satellites": _top_counts(stat.slips_by_sat),
    }


def _rejection_summary(stat: _StatAccumulator) -> dict[str, object]:
    if stat.parsed_sat_lines == 0:
        return {"available": False, "count": None, "top_satellites": []}
    return {"available": True, "count": stat.rejected_count, "top_satellites": _top_counts(stat.rejections_by_sat)}


def _nearest_epoch(epochs: list[SolutionEpoch], time: datetime, *, max_dt_s: float = 0.51) -> SolutionEpoch | None:
    """Return the nearest solution epoch within a bounded time tolerance."""

    if not epochs:
        return None
    nearest = min(epochs, key=lambda epoch: abs((epoch.time - time).total_seconds()))
    if abs((nearest.time - time).total_seconds()) <= max_dt_s:
        return nearest
    return None


def write_quality_json(path: Path, analysis: QualityAnalysis) -> None:
    """Write RTK quality analysis JSON."""

    path.write_text(json.dumps(analysis.as_dict(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def format_quality_text(analysis: QualityAnalysis) -> str:
    """Return compact terminal quality summary."""

    data = analysis.as_dict()
    time_summary = data["time_summary"]  # type: ignore[index]
    distance_summary = data["distance_summary"]  # type: ignore[index]
    suspicion = data["false_fix_suspicion"]  # type: ignore[index]
    quality_time = time_summary["quality_time_s"]  # type: ignore[index]
    quality_dist = distance_summary["quality_distance_m"]  # type: ignore[index]
    elapsed = float(time_summary["duration_s"])  # type: ignore[index]
    total_dist = float(distance_summary["total_distance_m"])  # type: ignore[index]
    lines = ["Quality summary:"]
    for quality in ("fixed", "float", "dgps", "single", "invalid"):
        seconds = float(quality_time.get(quality, 0.0))  # type: ignore[union-attr]
        meters = float(quality_dist.get(quality, 0.0))  # type: ignore[union-attr]
        lines.append(
            f"  {quality:7s}: {seconds:8.1f} s ({_pct(seconds, elapsed):5.1f}%), "
            f"{meters / 1000.0:7.3f} km ({_pct(meters, total_dist):5.1f}%)"
        )
    lines.append(f"  missing: {float(time_summary['missing_time_s']):8.1f} s ({float(time_summary['missing_pct']):5.1f}%)")
    lines.append("")
    lines.append("Fixed QC confidence:")
    lines.append(
        f"  supported:   {float(suspicion['qc_supported_fixed_time_s']):8.1f} s, {float(suspicion['qc_supported_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    lines.append(
        f"  provisional: {float(suspicion['qc_provisional_fixed_time_s']):8.1f} s, {float(suspicion['qc_provisional_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    lines.append(
        f"  suspect:     {float(suspicion['qc_suspect_fixed_time_s']):8.1f} s, {float(suspicion['qc_suspect_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    lines.append(
        f"  unknown:     {float(suspicion['qc_unknown_fixed_time_s']):8.1f} s, {float(suspicion['qc_unknown_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    fixed = data["segments"]["fixed"]  # type: ignore[index]
    lines.append("")
    lines.append(
        "Fixed segments: "
        f"count={fixed['count']}, "
        f"median={fixed['duration_s']['median']} s / {fixed['distance_m']['median']} m, "
        f"max={fixed['duration_s']['max']} s / {fixed['distance_m']['max']} m"
    )
    return "\n".join(lines)


def format_quality_markdown(analysis: QualityAnalysis, *, include_raw_json: bool = False) -> str:
    """Return Markdown RTK quality report."""

    data = analysis.as_dict()
    suspicion = data["false_fix_suspicion"]  # type: ignore[index]
    raw_fixed_time = float(suspicion.get("raw_fixed_time_s", 0.0)) if isinstance(suspicion, dict) else 0.0
    raw_fixed_distance = float(suspicion.get("raw_fixed_distance_m", 0.0)) if isinstance(suspicion, dict) else 0.0
    lines = [
        "# RTK Solution Quality Report",
        "",
        "## 1. Input Files And Parser Coverage",
        "",
        f"- Solution: `{data['inputs']['solution']}`",
        f"- Solution type: `{data['inputs']['solution_type']}`",
        f"- STAT: `{data['inputs']['stat']}`",
        f"- Solution epochs: {data['parser_coverage']['solution_epochs']}",
        f"- Parsed `$SAT` lines: {data['parser_coverage']['stat_sat_lines_parsed']}",
        "",
        "## 2. Solution-State Summary",
        "",
        "```text",
        format_quality_text(analysis),
        "```",
        "",
        "## 3. Segment Summary",
        "",
        "| Quality | Count | Median duration s | P95 duration s | Max duration s | Median distance m | P95 distance m | Max distance m |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for quality in ("fixed", "float", "dgps", "single", "invalid"):
        item = data["segments"][quality]  # type: ignore[index]
        lines.append(
            f"| {quality} | {item['count']} | {item['duration_s']['median']} | {item['duration_s']['p95']} | {item['duration_s']['max']} | "
            f"{item['distance_m']['median']} | {item['distance_m']['p95']} | {item['distance_m']['max']} |"
        )
    lines.extend(
        [
            "",
            "## 4. Fixed Confidence Classification",
            "",
            "Suspect fixed is heuristic evidence, not proof of a false fix.",
            "",
            "| Class | Time s | Time % of raw fixed | Distance km | Distance % of raw fixed |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, time_key, dist_key in (
        ("supported", "qc_supported_fixed_time_s", "qc_supported_fixed_distance_m"),
        ("provisional", "qc_provisional_fixed_time_s", "qc_provisional_fixed_distance_m"),
        ("suspect", "qc_suspect_fixed_time_s", "qc_suspect_fixed_distance_m"),
        ("unknown", "qc_unknown_fixed_time_s", "qc_unknown_fixed_distance_m"),
    ):
        seconds = float(suspicion.get(time_key, 0.0)) if isinstance(suspicion, dict) else 0.0
        meters = float(suspicion.get(dist_key, 0.0)) if isinstance(suspicion, dict) else 0.0
        lines.append(f"| {label} | {_fmt(seconds)} | {_fmt_pct(seconds, raw_fixed_time)} | {_fmt(meters / 1000.0)} | {_fmt_pct(meters, raw_fixed_distance)} |")
    reasons = suspicion.get("reasons", {}) if isinstance(suspicion, dict) else {}
    lines.extend(["", "| Reason | Affected fixed time s | Interpretation | Evidence status |", "| --- | ---: | --- | --- |"])
    if isinstance(reasons, dict):
        for reason, seconds in sorted(reasons.items()):
            lines.append(f"| {reason} | {_fmt_any(seconds)} | {_reason_interpretation(reason)} | {_reason_status(reason, data)} |")
    lines.extend(
        [
            "",
            "## 5. Residual Summary",
            "",
            "| Scope | Carrier median m | Carrier p95 m | Carrier p99 m | Carrier max m | Code median m | Code p95 m | Code p99 m | Code max m |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    residuals = data["residuals"]  # type: ignore[index]
    if isinstance(residuals, dict):
        carrier_global = residuals.get("carrier_abs_m", {}).get("global", {}) if isinstance(residuals.get("carrier_abs_m"), dict) else {}
        code_global = residuals.get("code_abs_m", {}).get("global", {}) if isinstance(residuals.get("code_abs_m"), dict) else {}
        lines.append(_residual_table_row("all", carrier_global, code_global))
        by_quality = residuals.get("by_quality", {})
        if isinstance(by_quality, dict):
            for quality in ("fixed", "float", "dgps"):
                item = by_quality.get(quality, {})
                if isinstance(item, dict):
                    lines.append(_residual_table_row(quality, item.get("carrier_abs_m", {}), item.get("code_abs_m", {})))
        if residuals.get("quality_aligned") is False:
            lines.append("")
            lines.append("WARNING: STAT residuals parsed globally but not aligned to solution quality states; residuals are not used for hard fixed-epoch suspicion.")
    lines.extend(
        [
            "",
            "## 6. Slip / Rejection Summary",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    slips = data["slips"]  # type: ignore[index]
    rejections = data["rejections"]  # type: ignore[index]
    if isinstance(slips, dict):
        for key in ("raw_slip_flags_total", "raw_slip_flags_per_min", "deduplicated_slip_events_total", "epochs_with_slip", "epochs_with_slip_pct", "fixed_epochs_with_recent_slip_pct"):
            lines.append(f"| {key.replace('_', ' ')} | {_fmt_any(slips.get(key))} |")
    if isinstance(rejections, dict):
        lines.append(f"| observation rejections | {_fmt_any(rejections.get('count'))} |")
    lines.extend(
        [
            "",
            "## 7. Motion And Baseline Context",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    motion = data.get("motion", {})
    if isinstance(motion, dict):
        for key in ("requested_profile", "inferred_profile", "median_speed_mps", "p95_speed_mps", "max_speed_mps", "max_speed_threshold_mps"):
            lines.append(f"| {key.replace('_', ' ')} | {_fmt_any(motion.get(key))} |")
    dropout = data.get("dropout_reacquisition", {})
    if isinstance(dropout, dict):
        lines.append(f"| likely occlusion events | {_fmt_any(dropout.get('likely_occlusion_events'))} |")
        lines.append(f"| median outage s | {_fmt_any(dropout.get('median_outage_s'))} |")
        lines.append(f"| max outage s | {_fmt_any(dropout.get('max_outage_s'))} |")
    baseline = data.get("baseline_summary", {})
    if isinstance(baseline, dict):
        lines.append(f"| baseline available | {baseline.get('available')} |")
        lines.append(f"| baseline note | {baseline.get('interpretation', baseline.get('reason', 'n/a'))} |")
    lines.extend(
        [
            "",
            "## 8. Quality By Route Distance",
            "",
            "| Start km | End km | Fixed s | Float s | DGPS s | Invalid/missing s |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    route_bins = data.get("route_bins", [])
    if isinstance(route_bins, list) and route_bins:
        for item in route_bins[:20]:
            if not isinstance(item, dict):
                continue
            quality_time = item.get("quality_time_s", {})
            if not isinstance(quality_time, dict):
                quality_time = {}
            lines.append(
                f"| {_fmt_any(item.get('start_km'))} | {_fmt_any(item.get('end_km'))} | "
                f"{_fmt_any(quality_time.get('fixed'))} | {_fmt_any(quality_time.get('float'))} | "
                f"{_fmt_any(quality_time.get('dgps'))} | {_fmt_any(quality_time.get('invalid'))} |"
            )
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## 9. RTKLIB Trace Diagnostics",
            "",
        ]
    )
    trace = data.get("trace", {"available": False})
    if isinstance(trace, dict) and trace.get("available"):
        counters = trace.get("counters", {})
        lines.extend(
            [
                f"- Trace mode: `{trace.get('source')}`",
                f"- Effective trace level: `{trace.get('effective_level')}`",
                f"- Trace retained: {'yes' if trace.get('retained') else 'no'}",
                f"- Trace file size bytes: {trace.get('trace_file_size_bytes', trace.get('bytes_read'))}",
                f"- Trace bytes read: {trace.get('trace_bytes_read', trace.get('bytes_read'))}",
                f"- Trace lines read: {trace.get('trace_lines_read', trace.get('lines_read'))}",
                f"- Trace truncated: {trace.get('trace_truncated', False)}",
                "",
                "| Event category | Count |",
                "| --- | ---: |",
            ]
        )
        for key, label in (
            ("ar_ratio_lines", "AR ratio lines"),
            ("ambiguity_reset_lines", "Ambiguity reset lines"),
            ("cycle_slip_lines", "Cycle slip lines"),
            ("observation_rejection_lines", "Observation rejection lines"),
            ("residual_outlier_lines", "Residual outlier lines"),
            ("missing_ephemeris_lines", "Missing ephemeris lines"),
            ("base_rover_time_issue_lines", "Base/rover time issues"),
        ):
            count = counters.get(key, 0) if isinstance(counters, dict) else 0
            lines.append(f"| {label} | {count} |")
        lines.extend(["", "Trace evidence can indicate marginal or suspect fixes, but does not prove false fixes."])
    else:
        lines.append("- Trace diagnostics were not requested or no trace file was available.")
    cleanup = data.get("cleanup", {})
    if isinstance(cleanup, dict):
        deleted = cleanup.get("stat_files_deleted", [])
        lines.extend(
            [
                "",
                f"- `.stat` cleanup requested: {'yes' if cleanup.get('stat_cleanup_requested') else 'no'}",
                f"- `.stat` files deleted after successful analysis: {len(deleted) if isinstance(deleted, list) else 0}",
            ]
        )
    lines.extend(["", "## 10. Top Warnings And Interpretation", ""])
    warnings = data.get("warnings", [])
    if warnings:
        lines.extend(f"- WARNING: {warning}" for warning in warnings)  # type: ignore[union-attr]
    else:
        lines.append("- No high-level quality warnings generated.")
    lines.extend(
        [
            "",
            "## 11. Suggested Next Actions",
            "",
            "- Optimise on QC-supported fixed time and QC-supported fixed distance, not raw fixed percentage alone.",
            "- Inspect missing/no-output time before comparing configurations.",
            "- Use STAT residual/slip evidence when available before treating short fixed islands as reliable.",
        ]
    )
    if include_raw_json:
        lines.extend(["", "## 12. Raw JSON Appendix", "", "```json", json.dumps(data, indent=2, sort_keys=True, default=str), "```"])
    return "\n".join(lines) + "\n"


def _fmt(value: float) -> str:
    return f"{value:,.3f}" if abs(value) < 10 else f"{value:,.1f}"


def _fmt_pct(value: float, denominator: float) -> str:
    return f"{_pct(value, denominator):.1f}%"


def _fmt_any(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return _fmt(value)
    return str(value)


def _residual_table_row(scope: str, carrier: object, code: object) -> str:
    c = carrier if isinstance(carrier, dict) else {}
    k = code if isinstance(code, dict) else {}
    return (
        f"| {scope} | {_fmt_any(c.get('median'))} | {_fmt_any(c.get('p95'))} | {_fmt_any(c.get('p99'))} | {_fmt_any(c.get('max'))} | "
        f"{_fmt_any(k.get('median'))} | {_fmt_any(k.get('p95'))} | {_fmt_any(k.get('p99'))} | {_fmt_any(k.get('max'))} |"
    )


def _reason_interpretation(reason: str) -> str:
    mapping = {
        "short_time_segment": "short fixed island",
        "short_distance_while_moving": "short moving segment",
        "recent_slip": "deduplicated recent slip evidence",
        "high_residual": "time-aligned residual outlier",
        "transition_jump": "speed-normalised motion anomaly",
        "incomplete_diagnostics": "confidence limited by incomplete diagnostics",
    }
    return mapping.get(reason, "diagnostic context")


def _reason_status(reason: str, data: dict[str, object]) -> str:
    if reason == "recent_slip":
        slips = data.get("slips", {})
        return "aligned" if isinstance(slips, dict) and slips.get("epochs_with_slip_pct") is not None else "not time-aligned"
    if reason == "high_residual":
        residuals = data.get("residuals", {})
        return "aligned" if isinstance(residuals, dict) and residuals.get("quality_aligned") else "global only"
    return "available"


def _empty_time_summary() -> dict[str, object]:
    return {
        "duration_s": 0.0,
        "median_epoch_interval_s": None,
        "emitted_epochs": 0,
        "expected_epochs": 0,
        "missing_epochs": 0,
        "missing_time_s": 0.0,
        "missing_pct": 0.0,
        "longest_output_gap_s": 0.0,
        "output_gaps_gt_2s": 0,
        "output_gaps_gt_10s": 0,
        "output_gaps_gt_60s": 0,
        "quality_time_s": {quality: 0.0 for quality in QUALITY_ORDER},
        "quality_pct_of_elapsed": {quality: 0.0 for quality in QUALITY_ORDER},
        "quality_pct_of_emitted": {quality: 0.0 for quality in QUALITY_ORDER},
    }


def _empty_residual_summary() -> dict[str, object]:
    return {
        "available": False,
        "quality_aligned": False,
        "carrier_abs_m": {"global": _stats([]), "fixed_p95": None, "float_p95": None},
        "code_abs_m": {"global": _stats([]), "fixed_p95": None, "float_p95": None},
        "by_quality": {},
        "top_satellites_by_high_residual_count": [],
    }


def _empty_slip_summary() -> dict[str, object]:
    return {
        "available": False,
        "raw_slip_flags_total": None,
        "raw_slip_flags_per_min": None,
        "deduplicated_slip_events_total": None,
        "epochs_with_slip": None,
        "fixed_epochs_with_recent_slip_pct": None,
        "events_total": None,
        "events_per_min": None,
        "epochs_with_slip_pct": None,
        "top_satellites": [],
    }


def _top_warnings(
    time_summary: dict[str, object],
    suspicion: dict[str, object],
    transitions: dict[str, object],
    residuals: dict[str, object],
    slips: dict[str, object],
) -> list[str]:
    warnings: list[str] = []
    if float(time_summary.get("missing_pct", 0.0)) > 5.0:
        warnings.append(
            f"missing/no-output time is {float(time_summary['missing_pct']):.1f}%; fixed/float/DGPS percentages should not be interpreted without this"
        )
    suspect = float(suspicion.get("suspect_fixed_time_pct", 0.0))
    if suspect > 10.0:
        warnings.append(f"{suspect:.1f}% of fixed time has local suspect-fix evidence")
    unknown = float(suspicion.get("qc_unknown_fixed_time_s", 0.0))
    raw_fixed = float(suspicion.get("raw_fixed_time_s", 0.0))
    if raw_fixed and unknown / raw_fixed > 0.25:
        warnings.append(
            f"Fixed confidence could not be established for {100.0 * unknown / raw_fixed:.1f}% because residual/slip diagnostics are incomplete."
        )
    if int(transitions.get("fixed_entry_gt_warning", 0)) > 0:
        warnings.append(f"{transitions['fixed_entry_gt_warning']} fixed-entry jumps exceed the warning threshold")
    if residuals.get("available") is False:
        warnings.append("residual metrics unavailable: .stat fields not recognised or stat file missing")
    if slips.get("available") is False:
        warnings.append("slip metrics unavailable: .stat fields not recognised or stat file missing")
    return warnings


def _segment_high_residual(stat: _StatAccumulator | None, segment: Segment, thresholds: QualityThresholds) -> bool:
    if stat is None:
        return False
    carrier: list[float] = []
    code: list[float] = []
    for time, residuals in stat.residuals_by_time.items():
        if segment.start_time <= time <= segment.end_time:
            carrier.extend(residuals.get("carrier", []))
            code.extend(residuals.get("code", []))
    carrier_p95 = _percentile(carrier, 95)
    code_p95 = _percentile(code, 95)
    return (carrier_p95 is not None and carrier_p95 > thresholds.carrier_residual_warning_m) or (
        code_p95 is not None and code_p95 > thresholds.code_residual_warning_m
    )


def _has_recent_slip(stat: _StatAccumulator, segment: Segment, window_s: float) -> bool:
    start = segment.start_time - timedelta(seconds=window_s)
    return any(start <= slip_time <= segment.end_time for slip_time in stat.slip_times)


def _segment_dict(segment: Segment) -> dict[str, object]:
    return {
        "quality": segment.quality,
        "start_time": segment.start_time.isoformat(),
        "end_time": segment.end_time.isoformat(),
        "duration_s": segment.duration_s,
        "distance_m": segment.distance_m,
        "epoch_count": segment.epoch_count,
        "median_speed_mps": segment.median_speed_mps,
        "max_step_m": segment.max_step_m,
        "start_index": segment.start_index,
        "end_index": segment.end_index,
    }


def _stats(values: list[float]) -> dict[str, float | None]:
    return {
        "median": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": max(values) if values else None,
    }


def _percentages(values: dict[str, float], denominator: float) -> dict[str, float]:
    return {key: (100.0 * value / denominator) if denominator else 0.0 for key, value in values.items()}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _top_counts(counts: dict[str, int], limit: int = 10) -> list[dict[str, object]]:
    return [{"id": key, "count": value} for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * radius_m * math.asin(min(1.0, math.sqrt(a)))


def _abs_float(value: str) -> float | None:
    parsed = float_or_none(value)
    return abs(parsed) if parsed is not None else None


def _any_int(values: list[str]) -> bool:
    for value in values:
        parsed = int_or_none(value)
        if parsed is not None and parsed > 0:
            return True
    return False


def _pct(value: float, denominator: float) -> float:
    return 100.0 * value / denominator if denominator else 0.0
