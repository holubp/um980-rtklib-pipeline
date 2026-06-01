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
    warnings: list[str]
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
    residuals = _residual_summary(stat, segments) if stat else _empty_residual_summary()
    slips = _slip_summary(stat, epochs) if stat else _empty_slip_summary()
    rejections = _rejection_summary(stat) if stat else {"available": False, "count": None, "top_satellites": []}
    transitions = _transition_summary(epochs, segments, limits)
    suspicion = _false_fix_suspicion(segments, epochs, stat, transitions, limits)
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


def _false_fix_suspicion(
    segments: list[Segment],
    epochs: list[SolutionEpoch],
    stat: _StatAccumulator | None,
    transitions: dict[str, object],
    thresholds: QualityThresholds,
) -> dict[str, object]:
    trusted_time = provisional_time = suspect_time = 0.0
    trusted_distance = provisional_distance = suspect_distance = 0.0
    reasons = {
        "short_time_segment": 0.0,
        "short_distance_while_moving": 0.0,
        "recent_slip": 0.0,
        "high_residual": 0.0,
        "transition_jump": 0.0,
    }
    fixed_entry_warning_indexes = {
        item["segment_start_index"]
        for item in transitions.get("fixed_entry_jumps", [])  # type: ignore[union-attr]
        if isinstance(item, dict) and item.get("horizontal_m", 0) >= thresholds.transition_jump_warning_m
    }
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
        if stat and _has_recent_slip(stat, segment, thresholds.recent_slip_window_s):
            flags.append("recent_slip")
        if _segment_high_residual(stat, segment, thresholds):
            flags.append("high_residual")
        if segment.start_index in fixed_entry_warning_indexes:
            flags.append("transition_jump")
        for flag in set(flags):
            if flag in reasons:
                reasons[flag] += segment.duration_s
        severe = "very_short_time_segment" in flags or "transition_jump" in flags or ("recent_slip" in flags and "short_time_segment" in flags)
        if severe:
            suspect_time += segment.duration_s
            suspect_distance += segment.distance_m
        elif flags:
            provisional_time += segment.duration_s
            provisional_distance += segment.distance_m
        else:
            trusted_time += segment.duration_s
            trusted_distance += segment.distance_m
    total_fixed_time = trusted_time + provisional_time + suspect_time
    total_fixed_distance = trusted_distance + provisional_distance + suspect_distance
    return {
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


def _transition_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    thresholds: QualityThresholds,
) -> dict[str, object]:
    fixed_entries: list[dict[str, object]] = []
    fixed_exits: list[dict[str, object]] = []
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
        "fixed_entry_gt_warning": sum(1 for item in fixed_entries if item["horizontal_m"] > thresholds.transition_jump_warning_m),
        "fixed_entry_gt_severe": sum(1 for item in fixed_entries if item["horizontal_m"] > thresholds.transition_jump_severe_m),
        "fixed_exit_gt_warning": sum(1 for item in fixed_exits if item["horizontal_m"] > thresholds.transition_jump_warning_m),
        "fixed_exit_gt_severe": sum(1 for item in fixed_exits if item["horizontal_m"] > thresholds.transition_jump_severe_m),
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
    return {
        "time": right.time.isoformat(),
        "segment_start_index": segment_start_index,
        "from_quality": left.quality,
        "to_quality": right.quality,
        "horizontal_m": _haversine_m(left.lat, left.lon, right.lat, right.lon),
        "vertical_m": vertical,
    }


def _residual_summary(stat: _StatAccumulator | None, _segments: list[Segment]) -> dict[str, object]:
    if stat is None or stat.parsed_sat_lines == 0:
        return _empty_residual_summary()
    return {
        "available": True,
        "carrier_abs_m": {
            "global": _stats(stat.carrier_residual_abs_m),
            "fixed_p95": None,
            "float_p95": None,
        },
        "code_abs_m": {
            "global": _stats(stat.code_residual_abs_m),
            "fixed_p95": None,
            "float_p95": None,
        },
        "top_satellites_by_high_residual_count": _top_counts(stat.top_residuals_by_sat),
    }


def _slip_summary(stat: _StatAccumulator, epochs: list[SolutionEpoch]) -> dict[str, object]:
    if stat.parsed_sat_lines == 0:
        return _empty_slip_summary()
    duration_min = 0.0
    if epochs:
        duration_min = max(0.0, (epochs[-1].time - epochs[0].time).total_seconds() / 60.0)
    return {
        "available": True,
        "events_total": stat.slip_count,
        "events_per_min": (stat.slip_count / duration_min) if duration_min else 0.0,
        "epochs_with_slip_pct": None,
        "top_satellites": _top_counts(stat.slips_by_sat),
    }


def _rejection_summary(stat: _StatAccumulator) -> dict[str, object]:
    if stat.parsed_sat_lines == 0:
        return {"available": False, "count": None, "top_satellites": []}
    return {"available": True, "count": stat.rejected_count, "top_satellites": _top_counts(stat.rejections_by_sat)}


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
    lines.append("Fixed quality:")
    lines.append(
        f"  trusted:     {float(suspicion['trusted_fixed_time_s']):8.1f} s, {float(suspicion['trusted_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    lines.append(
        f"  provisional: {float(suspicion['provisional_fixed_time_s']):8.1f} s, {float(suspicion['provisional_fixed_distance_m']) / 1000.0:7.3f} km"
    )
    lines.append(
        f"  suspect:     {float(suspicion['suspect_fixed_time_s']):8.1f} s, {float(suspicion['suspect_fixed_distance_m']) / 1000.0:7.3f} km"
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


def format_quality_markdown(analysis: QualityAnalysis) -> str:
    """Return Markdown RTK quality report."""

    data = analysis.as_dict()
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
            "## 4. Trusted / Provisional / Suspect Fixed",
            "",
            "Suspect fixed is heuristic evidence, not proof of a false fix.",
            "",
            json.dumps(data["false_fix_suspicion"], indent=2, sort_keys=True),
            "",
            "## 5. Residual Summary",
            "",
            json.dumps(data["residuals"], indent=2, sort_keys=True),
            "",
            "## 6. Slip / Rejection Summary",
            "",
            json.dumps({"slips": data["slips"], "rejections": data["rejections"]}, indent=2, sort_keys=True),
            "",
            "## 7. RTKLIB Trace Diagnostics",
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
                f"- Bytes parsed: {trace.get('bytes_read')}",
                f"- Lines parsed: {trace.get('lines_read')}",
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
    lines.extend(["", "## 8. Top Warnings And Interpretation", ""])
    warnings = data.get("warnings", [])
    if warnings:
        lines.extend(f"- WARNING: {warning}" for warning in warnings)  # type: ignore[union-attr]
    else:
        lines.append("- No high-level quality warnings generated.")
    lines.extend(
        [
            "",
            "## 9. Suggested Next Actions",
            "",
            "- Optimise on trusted fixed time and trusted fixed distance, not raw fixed percentage alone.",
            "- Inspect missing/no-output time before comparing configurations.",
            "- Use STAT residual/slip evidence when available before treating short fixed islands as reliable.",
        ]
    )
    return "\n".join(lines) + "\n"


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
        "carrier_abs_m": {"global": _stats([]), "fixed_p95": None, "float_p95": None},
        "code_abs_m": {"global": _stats([]), "fixed_p95": None, "float_p95": None},
        "top_satellites_by_high_residual_count": [],
    }


def _empty_slip_summary() -> dict[str, object]:
    return {"available": False, "events_total": None, "events_per_min": None, "epochs_with_slip_pct": None, "top_satellites": []}


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
        warnings.append(f"{suspect:.1f}% of fixed time is classified as suspect fixed")
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
