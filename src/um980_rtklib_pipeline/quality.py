"""Analysis JSON assembly and RTK solution quality analysis."""

from __future__ import annotations

import json
import math
import time
from bisect import bisect_left
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
    trace_align_tolerance_s: float = 0.5


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
    fixed_continuity_summary: dict[str, object] = field(default_factory=dict)
    top_fixed_segments_by_distance: list[dict[str, object]] = field(default_factory=list)
    top_fixed_segments_by_duration: list[dict[str, object]] = field(default_factory=list)
    usable_supported_fixed_time_s: float = 0.0
    usable_supported_fixed_distance_km: float = 0.0
    usable_provisional_fixed_time_s: float = 0.0
    usable_provisional_fixed_distance_km: float = 0.0
    trajectory_suspect_fixed_time_s: float = 0.0
    trajectory_suspect_fixed_distance_km: float = 0.0
    strict_supported_fixed_time_s: float = 0.0
    strict_supported_fixed_distance_km: float = 0.0
    track_plausibility: dict[str, object] = field(default_factory=dict)
    stop_diagnostics: dict[str, object] = field(default_factory=dict)
    long_fixed_metrics: dict[str, object] = field(default_factory=dict)
    geometry_cost: dict[str, object] = field(default_factory=dict)
    motion: dict[str, object] = field(default_factory=dict)
    dropout_reacquisition: dict[str, object] = field(default_factory=dict)
    baseline_summary: dict[str, object] = field(default_factory=dict)
    route_bins: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trace: dict[str, object] | None = None
    cleanup: dict[str, object] | None = None
    performance: dict[str, object] = field(default_factory=dict)

    def as_dict(
        self,
        *,
        include_all_segments: bool = False,
        include_geometry_segments: bool = False,
        include_empty_bins: bool = False,
    ) -> dict[str, object]:
        """Return stable JSON-friendly analysis data."""

        long_fixed = dict(self.long_fixed_metrics)
        geometry_cost = dict(self.geometry_cost)
        baseline_summary = dict(self.baseline_summary)
        if not include_all_segments:
            long_fixed.pop("segment_qc", None)
        if not include_geometry_segments and not include_all_segments:
            geometry_cost.pop("segment_geometry_risk", None)
        if not include_empty_bins:
            bins = baseline_summary.get("quality_by_baseline_bin")
            if isinstance(bins, list):
                baseline_summary["quality_by_baseline_bin"] = [
                    item for item in bins if isinstance(item, dict) and item.get("populated")
                ]
            route_bins = [item for item in self.route_bins if item.get("populated")]
        else:
            route_bins = self.route_bins
        return {
            "inputs": self.inputs,
            "parser_coverage": self.parser_coverage,
            "time_summary": self.time_summary,
            "distance_summary": self.distance_summary,
            "fixed_continuity_summary": self.fixed_continuity_summary,
            "top_fixed_segments_by_distance": self.top_fixed_segments_by_distance,
            "top_fixed_segments_by_duration": self.top_fixed_segments_by_duration,
            "usable_supported_fixed_time_s": self.usable_supported_fixed_time_s,
            "usable_supported_fixed_distance_km": self.usable_supported_fixed_distance_km,
            "usable_provisional_fixed_time_s": self.usable_provisional_fixed_time_s,
            "usable_provisional_fixed_distance_km": self.usable_provisional_fixed_distance_km,
            "trajectory_suspect_fixed_time_s": self.trajectory_suspect_fixed_time_s,
            "trajectory_suspect_fixed_distance_km": self.trajectory_suspect_fixed_distance_km,
            "strict_supported_fixed_time_s": self.strict_supported_fixed_time_s,
            "strict_supported_fixed_distance_km": self.strict_supported_fixed_distance_km,
            "segments": self.segments,
            "residuals": self.residuals,
            "slips": self.slips,
            "rejections": self.rejections,
            "transition_jumps": self.transition_jumps,
            "false_fix_suspicion": self.false_fix_suspicion,
            "track_plausibility": self.track_plausibility,
            "stop_diagnostics": self.stop_diagnostics,
            "long_fixed_metrics": long_fixed,
            "geometry_cost": geometry_cost,
            "motion": self.motion,
            "dropout_reacquisition": self.dropout_reacquisition,
            "baseline_summary": baseline_summary,
            "route_bins": route_bins,
            "trace": self.trace or {"available": False},
            "cleanup": self.cleanup
            or {
                "trace_cleanup_requested": False,
                "trace_deleted": False,
                "stat_cleanup_requested": False,
                "stat_files_deleted": [],
                "stat_files_kept": [],
            },
            "performance": self.performance,
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
    rejections_by_time: dict[datetime, int] = field(default_factory=dict)
    parse_elapsed_s: float = 0.0
    truncated: bool = False
    truncate_reason: str | None = None
    max_lines: int = 0
    max_seconds: float = 0.0


@dataclass(frozen=True)
class EpochIndex:
    """Bisect-backed nearest-neighbour lookup for solution epochs."""

    epochs: list[SolutionEpoch]
    times_s: list[float]

    @classmethod
    def build(cls, epochs: list[SolutionEpoch]) -> "EpochIndex":
        """Build an index sorted by time."""

        ordered = sorted(epochs, key=lambda epoch: epoch.time)
        return cls(ordered, [_timestamp_s(epoch.time) for epoch in ordered])

    def nearest(self, when: datetime, *, max_dt_s: float = 0.51) -> SolutionEpoch | None:
        """Return the nearest epoch within `max_dt_s`, or `None`."""

        index = self.nearest_index(when, max_dt_s=max_dt_s)
        return self.epochs[index] if index is not None else None

    def nearest_index(self, when: datetime, *, max_dt_s: float = 0.51) -> int | None:
        """Return the nearest epoch index within `max_dt_s`, or `None`."""

        if not self.epochs:
            return None
        target = _timestamp_s(when)
        position = bisect_left(self.times_s, target)
        candidates: list[int] = []
        if position < len(self.epochs):
            candidates.append(position)
        if position:
            candidates.append(position - 1)
        best = min(candidates, key=lambda item: abs(self.times_s[item] - target))
        return best if abs(self.times_s[best] - target) <= max_dt_s else None


def _timestamp_s(value: datetime) -> float:
    """Return POSIX seconds, treating naive datetimes as UTC."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


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
    stat_max_lines: int = 0,
    stat_max_seconds: float = 0.0,
    fast: bool = False,
    base_ecef_xyz_m: tuple[float, float, float] | None = None,
    base_llh: tuple[float, float, float] | None = None,
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
    solution_parse_started = time.perf_counter()
    epochs, solution_warnings, solution_type = parse_solution_epochs(solution_path)
    solution_parse_elapsed_s = time.perf_counter() - solution_parse_started
    stat = parse_stat_file(stat_path, max_lines=stat_max_lines, max_seconds=stat_max_seconds, fast=fast) if stat_path and not fast else None
    warnings = [*solution_warnings]
    if stat_path and stat is None:
        if fast:
            warnings.append("quality-fast enabled; STAT parsing and detailed residual/slip QC were skipped")
        else:
            warnings.append(f"stat file unavailable or unreadable: {stat_path}")
    if stat_path is None:
        warnings.append("stat file not supplied; residual/slip/rejection analysis unavailable")
    elif stat is not None and stat.parsed_sat_lines == 0:
        warnings.append("stat residual/slip metrics unavailable: no recognised $SAT lines")
    if stat is not None and stat.truncated:
        warnings.append(f"STAT parsing truncated by {stat.truncate_reason}; QC confidence is limited")

    segments = compute_segments(epochs, gap_split_s=limits.gap_split_s, jump_clip_m=limits.jump_clip_m)
    time_summary = _time_summary(epochs, limits)
    distance_summary = _distance_summary(epochs)
    epoch_index = EpochIndex.build(epochs)
    residuals = _residual_summary(stat, epoch_index) if stat else _empty_residual_summary()
    slips = _slip_summary(stat, epoch_index) if stat else _empty_slip_summary()
    rejections = _rejection_summary(stat) if stat else {"available": False, "count": None, "top_satellites": []}
    motion = _motion_summary(epochs, limits)
    transitions = _transition_summary(epochs, segments, limits, motion)
    dropout = _dropout_reacquisition_summary(epochs, segments, limits)
    trace_align_started = time.perf_counter()
    aligned_trace = _align_trace_summary(trace_summary, epoch_index, limits.trace_align_tolerance_s)
    trace_align_elapsed_s = time.perf_counter() - trace_align_started
    suspicion = _false_fix_suspicion(segments, epochs, stat, transitions, residuals, slips, aligned_trace, limits)
    base_position = _base_position_llh(base_ecef_xyz_m=base_ecef_xyz_m, base_llh=base_llh)
    track_plausibility = _track_plausibility_summary(epochs, segments, limits, base_position=base_position)
    stop_diagnostics = _stop_diagnostics(epochs, limits)
    long_fixed_metrics = _long_fixed_metrics(segments, track_plausibility)
    top_fixed_by_distance = _top_fixed_segments(epochs, segments, base_position=base_position, order_by="distance")
    top_fixed_by_duration = _top_fixed_segments(epochs, segments, base_position=base_position, order_by="duration")
    fixed_continuity = _fixed_continuity_summary(segments, long_fixed_metrics, top_fixed_by_distance, top_fixed_by_duration)
    usable_totals = _usable_fixed_totals(segments)
    geometry_cost = _geometry_cost_summary(epochs, segments, stat, aligned_trace, limits)
    baseline_summary = _baseline_summary(
        epochs,
        segments,
        stat,
        aligned_trace,
        suspicion,
        limits,
        base_ecef_xyz_m=base_ecef_xyz_m,
        base_llh=base_llh,
    )
    route_bins = _route_bins(epochs, segments, stat, aligned_trace, suspicion, limits)
    if residuals.get("available") and residuals.get("quality_aligned") is False:
        warnings.append("STAT residuals parsed globally but not aligned to solution quality states; residuals not used for hard fixed-epoch suspicion.")
    if slips.get("available") and slips.get("epochs_with_slip_pct") is None:
        warnings.append("STAT slip flags are not sufficiently time-aligned/deduplicated; raw slip counts are not used for hard fixed-epoch suspicion.")
    if motion.get("warning"):
        warnings.append(str(motion["warning"]))
    if aligned_trace and aligned_trace.get("available") and aligned_trace.get("alignment"):
        alignment = aligned_trace.get("alignment", {})
        if isinstance(alignment, dict) and float(alignment.get("trace_alignment_pct", 0.0) or 0.0) < 20.0:
            warnings.append("Trace events were parsed but alignment coverage is low; global trace counters are not used for hard fixed-epoch suspicion.")
    if track_plausibility.get("fixed_track_inconsistent_time_s", 0.0):
        warnings.append("fixed trajectory contains locally implausible islands; inspect track-plausibility metrics before treating fixed percentage as quality")
    warnings.extend(_top_warnings(time_summary, suspicion, transitions, residuals, slips))
    parser_coverage = {
        "solution_epochs": len(epochs),
        "solution_warnings": solution_warnings,
        "stat_lines": stat.stat_lines if stat else 0,
        "stat_sat_lines_parsed": stat.parsed_sat_lines if stat else 0,
        "stat_sat_lines_unparsed": stat.unparsed_sat_lines if stat else 0,
        "stat_truncated": stat.truncated if stat else False,
        "stat_truncate_reason": stat.truncate_reason if stat else None,
        "warnings": warnings,
    }
    performance = {
        "solution_parse_elapsed_s": solution_parse_elapsed_s,
        "stat_parse_elapsed_s": stat.parse_elapsed_s if stat else 0.0,
        "stat_lines_per_s": (stat.stat_lines / stat.parse_elapsed_s) if stat and stat.parse_elapsed_s > 0 else None,
        "stat_lines_read": stat.stat_lines if stat else 0,
        "sat_lines_parsed": stat.parsed_sat_lines if stat else 0,
        "raw_slip_flags": stat.slip_count if stat else 0,
        "dedup_slip_events": len(stat.slip_events) if stat else 0,
        "unique_slip_epochs": len({event[0] for event in stat.slip_events}) if stat else 0,
        "trace_parse_elapsed_s": aligned_trace.get("trace_parse_elapsed_s") if isinstance(aligned_trace, dict) else 0.0,
        "trace_lines_per_s": (
            float(aligned_trace.get("trace_lines_read", 0) or 0) / float(aligned_trace.get("trace_parse_elapsed_s", 0) or 0)
            if isinstance(aligned_trace, dict) and float(aligned_trace.get("trace_parse_elapsed_s", 0) or 0) > 0
            else None
        ),
        "trace_align_elapsed_s": trace_align_elapsed_s,
        "event_alignment_cache_size": (
            aligned_trace.get("alignment", {}).get("unique_trace_times_mapped")
            if isinstance(aligned_trace, dict) and isinstance(aligned_trace.get("alignment"), dict)
            else 0
        ),
    }
    return QualityAnalysis(
        inputs={
            "solution": str(solution_path),
            "stat": str(stat_path) if stat_path else None,
            "solution_type": solution_type,
            "stat_available": stat is not None,
            "stat_max_lines": stat_max_lines,
            "stat_max_seconds": stat_max_seconds,
            "quality_fast": fast,
            "base_ecef_xyz_m": base_ecef_xyz_m,
            "base_llh": base_llh,
        },
        parser_coverage=parser_coverage,
        time_summary=time_summary,
        distance_summary=distance_summary,
        fixed_continuity_summary=fixed_continuity,
        top_fixed_segments_by_distance=top_fixed_by_distance,
        top_fixed_segments_by_duration=top_fixed_by_duration,
        usable_supported_fixed_time_s=usable_totals["usable_supported_fixed_time_s"],
        usable_supported_fixed_distance_km=usable_totals["usable_supported_fixed_distance_km"],
        usable_provisional_fixed_time_s=usable_totals["usable_provisional_fixed_time_s"],
        usable_provisional_fixed_distance_km=usable_totals["usable_provisional_fixed_distance_km"],
        trajectory_suspect_fixed_time_s=usable_totals["trajectory_suspect_fixed_time_s"],
        trajectory_suspect_fixed_distance_km=usable_totals["trajectory_suspect_fixed_distance_km"],
        strict_supported_fixed_time_s=float(suspicion.get("qc_supported_fixed_time_s", 0.0) or 0.0),
        strict_supported_fixed_distance_km=float(suspicion.get("qc_supported_fixed_distance_m", 0.0) or 0.0) / 1000.0,
        segments=_segment_summary(segments),
        residuals=residuals,
        slips=slips,
        rejections=rejections,
        transition_jumps=transitions,
        false_fix_suspicion=suspicion,
        track_plausibility=track_plausibility,
        stop_diagnostics=stop_diagnostics,
        long_fixed_metrics=long_fixed_metrics,
        geometry_cost=geometry_cost,
        motion=motion,
        dropout_reacquisition=dropout,
        baseline_summary=baseline_summary,
        route_bins=route_bins,
        warnings=list(dict.fromkeys(warnings)),
        trace=aligned_trace,
        cleanup=cleanup,
        performance=performance,
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


def parse_stat_file(
    path: Path | None,
    *,
    max_lines: int = 0,
    max_seconds: float = 0.0,
    fast: bool = False,
) -> _StatAccumulator | None:
    """Parse a tolerant subset of RTKLIB `.stat` SAT rows.

    Args:
        path: STAT path.
        max_lines: Optional maximum physical lines to scan. Zero is unlimited.
        max_seconds: Optional wall-clock parse budget. Zero is unlimited.
        fast: When true, skip parsing and return an empty accumulator.
    """

    if path is None or not path.exists():
        return None
    started = time.perf_counter()
    stat = _StatAccumulator()
    stat.max_lines = max(0, int(max_lines or 0))
    stat.max_seconds = max(0.0, float(max_seconds or 0.0))
    if fast:
        stat.truncated = True
        stat.truncate_reason = "quality-fast"
        stat.parse_elapsed_s = 0.0
        return stat
    with path.open("r", encoding="ascii", errors="ignore") as handle:
        for line in handle:
            if stat.max_lines and stat.stat_lines >= stat.max_lines:
                stat.truncated = True
                stat.truncate_reason = f"max_lines={stat.max_lines}"
                break
            if stat.max_seconds and time.perf_counter() - started >= stat.max_seconds:
                stat.truncated = True
                stat.truncate_reason = f"max_seconds={stat.max_seconds:g}"
                break
            line = line.rstrip("\r\n")
            stat.stat_lines += 1
            if not line.startswith("$SAT"):
                continue
            stat.sat_lines += 1
            if not _parse_sat_stat_line(line, stat):
                stat.unparsed_sat_lines += 1
    stat.parse_elapsed_s = time.perf_counter() - started
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
        stat.rejections_by_time[time] = stat.rejections_by_time.get(time, 0) + 1
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
    p90 = _percentile(speeds, 90) or 0.0
    p95 = _percentile(speeds, 95) or 0.0
    p99 = _percentile(speeds, 99) or 0.0
    max_observed = max(speeds) if speeds else None
    high_speed_count = sum(1 for speed in speeds if speed > 30.0)
    requested = thresholds.motion_profile
    if requested == "auto":
        if max_observed is not None and max_observed > 45.0 and (p90 > 10.0 or high_speed_count >= 3):
            profile = "highway"
        elif p99 > 45.0:
            profile = "highway"
        elif p95 > 15.0 or (max_observed is not None and max_observed > 45.0):
            profile = "vehicle"
        elif p95 <= 1.0:
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
        "p90_speed_mps": p90,
        "p95_speed_mps": _percentile(speeds, 95),
        "p99_speed_mps": p99,
        "max_speed_mps": max_observed,
        "high_speed_samples_gt_30_mps": high_speed_count,
        "max_speed_threshold_mps": max_speed,
        "max_accel_threshold_mps2": thresholds.max_accel_mps2,
        "warning": (
            f"observed max speed {max_observed:.1f} m/s exceeds inferred profile threshold {max_speed:.1f} m/s"
            if max_observed is not None and max_observed > max_speed * 1.25
            else None
        ),
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


def _base_position_llh(
    *,
    base_ecef_xyz_m: tuple[float, float, float] | None,
    base_llh: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    return base_llh or (_ecef_to_llh(*base_ecef_xyz_m) if base_ecef_xyz_m else None)


def _route_distances_km(epochs: list[SolutionEpoch]) -> list[float]:
    distances = [0.0 for _ in epochs]
    total = 0.0
    for index, (left, right) in enumerate(zip(epochs, epochs[1:], strict=False), start=1):
        if left.lat is not None and left.lon is not None and right.lat is not None and right.lon is not None:
            total += _haversine_m(left.lat, left.lon, right.lat, right.lon) / 1000.0
        distances[index] = total
    return distances


def _baseline_distances_km(
    epochs: list[SolutionEpoch],
    base_position: tuple[float, float, float] | None,
) -> list[float | None]:
    if base_position is None:
        return [None for _ in epochs]
    base_lat, base_lon, _height = base_position
    result: list[float | None] = []
    for epoch in epochs:
        if epoch.lat is None or epoch.lon is None:
            result.append(None)
        else:
            result.append(_haversine_m(base_lat, base_lon, epoch.lat, epoch.lon) / 1000.0)
    return result


def _track_plausibility_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    thresholds: QualityThresholds,
    *,
    base_position: tuple[float, float, float] | None,
) -> dict[str, object]:
    """Compute motion/track plausibility independent of RTK cleanliness."""

    route_km = _route_distances_km(epochs)
    baseline_km = _baseline_distances_km(epochs, base_position)
    steps_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    speeds_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    accel_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    jerk_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    heading_change_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    yaw_rate_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    curvature_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    speeds: list[float | None] = [None for _ in epochs]
    headings: list[float | None] = [None for _ in epochs]
    anomalies: list[dict[str, object]] = []
    max_speed = thresholds.max_speed_mps or {"static": 1.0, "walking": 3.0, "cycling": 15.0, "vehicle": 45.0, "highway": 60.0}.get(
        thresholds.motion_profile,
        45.0,
    )
    for index, (left, right) in enumerate(zip(epochs, epochs[1:], strict=False), start=1):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        dt = (right.time - left.time).total_seconds()
        if dt <= 0:
            continue
        step = _haversine_m(left.lat, left.lon, right.lat, right.lon)
        speed = step / dt
        heading = _bearing_deg(left.lat, left.lon, right.lat, right.lon)
        quality = right.quality
        steps_by_quality.setdefault(quality, []).append(step)
        speeds_by_quality.setdefault(quality, []).append(speed)
        speeds[index] = speed
        headings[index] = heading
        if right.quality == "fixed" and left.quality == "fixed" and speed > max_speed * 1.8:
            anomalies.append(
                {
                    "type": "fixed_internal_jump",
                    "time": right.time.isoformat(),
                    "horizontal_step_m": step,
                    "speed_mps": speed,
                    "route_km": route_km[index],
                    "baseline_km": baseline_km[index],
                }
            )
        if right.quality == "fixed" and speed > max_speed * 1.8 and _nearby_speed_low(speeds, index, thresholds.stationary_speed_threshold_mps):
            anomalies.append(
                {
                    "type": "fixed_jump_while_stationary",
                    "time": right.time.isoformat(),
                    "horizontal_step_m": step,
                    "speed_mps": speed,
                    "route_km": route_km[index],
                    "baseline_km": baseline_km[index],
                }
            )
    prev_accel: float | None = None
    for index in range(2, len(epochs)):
        if speeds[index] is None or speeds[index - 1] is None:
            continue
        dt = (epochs[index].time - epochs[index - 1].time).total_seconds()
        if dt <= 0:
            continue
        accel = abs(float(speeds[index]) - float(speeds[index - 1])) / dt
        accel_by_quality.setdefault(epochs[index].quality, []).append(accel)
        if prev_accel is not None:
            jerk_by_quality.setdefault(epochs[index].quality, []).append(abs(accel - prev_accel) / dt)
        prev_accel = accel
        if headings[index] is not None and headings[index - 1] is not None:
            change = _heading_delta_deg(float(headings[index - 1]), float(headings[index]))
            heading_change_by_quality.setdefault(epochs[index].quality, []).append(abs(change))
            yaw_rate_by_quality.setdefault(epochs[index].quality, []).append(abs(change) / dt)
            if speeds[index] and speeds[index] > 0:
                curvature_by_quality.setdefault(epochs[index].quality, []).append(abs(change) / max(float(speeds[index]) * dt, 1e-6))
    consistency = _fixed_island_consistency(epochs, segments, route_km, baseline_km)
    score = _track_consistency_score(anomalies, consistency)
    status = _track_consistency_status(score, anomalies, consistency)
    return {
        "horizontal_step_m_by_quality": {quality: _stats(values) for quality, values in steps_by_quality.items()},
        "speed_mps_by_quality": {quality: _stats(values) for quality, values in speeds_by_quality.items()},
        "accel_mps2_by_quality": {quality: _stats(values) for quality, values in accel_by_quality.items()},
        "jerk_mps3_by_quality": {quality: _stats(values) for quality, values in jerk_by_quality.items()},
        "heading_change_deg_by_quality": {quality: _stats(values) for quality, values in heading_change_by_quality.items()},
        "yaw_rate_dps_by_quality": {quality: _stats(values) for quality, values in yaw_rate_by_quality.items()},
        "curvature_deg_per_m_by_quality": {quality: _stats(values) for quality, values in curvature_by_quality.items()},
        "fixed_internal_jump_count": sum(1 for item in anomalies if item["type"] == "fixed_internal_jump"),
        "fixed_jumps_while_stationary_count": sum(1 for item in anomalies if item["type"] == "fixed_jump_while_stationary"),
        "anomalies": anomalies[:50],
        "fixed_island_cross_track_p95_m": consistency["fixed_island_cross_track_p95_m"],
        "fixed_island_max_offset_m": consistency["fixed_island_max_offset_m"],
        "fixed_islands_with_large_offset_count": consistency["fixed_islands_with_large_offset_count"],
        "fixed_track_consistent_time_s": consistency["fixed_track_consistent_time_s"],
        "fixed_track_consistent_distance_m": consistency["fixed_track_consistent_distance_m"],
        "fixed_track_inconsistent_time_s": consistency["fixed_track_inconsistent_time_s"],
        "fixed_track_inconsistent_distance_m": consistency["fixed_track_inconsistent_distance_m"],
        "track_consistency_score": score,
        "track_consistency_status": status,
    }


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    y = math.sin(delta) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _heading_delta_deg(left: float, right: float) -> float:
    return (right - left + 180.0) % 360.0 - 180.0


def _nearby_speed_low(speeds: list[float | None], index: int, threshold: float) -> bool:
    nearby = [speed for speed in speeds[max(0, index - 3) : index] if speed is not None]
    return bool(nearby) and max(float(speed) for speed in nearby) <= threshold


def _fixed_island_consistency(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    route_km: list[float],
    baseline_km: list[float | None],
) -> dict[str, object]:
    offsets: list[float] = []
    large_count = 0
    consistent_time = consistent_distance = 0.0
    inconsistent_time = inconsistent_distance = 0.0
    threshold_m = 10.0
    for segment in segments:
        if segment.quality != "fixed":
            continue
        anchor_left = _nearest_anchor_epoch(epochs, segment.start_index, -1)
        anchor_right = _nearest_anchor_epoch(epochs, segment.end_index, 1)
        segment_offsets: list[float] = []
        if anchor_left is not None and anchor_right is not None:
            for epoch in epochs[segment.start_index : segment.end_index + 1]:
                if epoch.lat is None or epoch.lon is None:
                    continue
                segment_offsets.append(
                    _cross_track_offset_m(
                        anchor_left.lat,
                        anchor_left.lon,
                        anchor_right.lat,
                        anchor_right.lon,
                        epoch.lat,
                        epoch.lon,
                    )
                )
        if segment_offsets:
            offsets.extend(segment_offsets)
            max_offset = max(segment_offsets)
            if max_offset > threshold_m:
                large_count += 1
                inconsistent_time += segment.duration_s
                inconsistent_distance += segment.distance_m
            else:
                consistent_time += segment.duration_s
                consistent_distance += segment.distance_m
        else:
            consistent_time += segment.duration_s
            consistent_distance += segment.distance_m
    return {
        "fixed_island_cross_track_p95_m": _percentile(offsets, 95),
        "fixed_island_max_offset_m": max(offsets) if offsets else None,
        "fixed_islands_with_large_offset_count": large_count,
        "fixed_track_consistent_time_s": consistent_time,
        "fixed_track_consistent_distance_m": consistent_distance,
        "fixed_track_inconsistent_time_s": inconsistent_time,
        "fixed_track_inconsistent_distance_m": inconsistent_distance,
    }


def _nearest_anchor_epoch(epochs: list[SolutionEpoch], start: int, direction: int) -> SolutionEpoch | None:
    index = start + direction
    while 0 <= index < len(epochs):
        epoch = epochs[index]
        if epoch.lat is not None and epoch.lon is not None and epoch.quality != "fixed":
            return epoch
        index += direction
    return None


def _cross_track_offset_m(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
    lat: float,
    lon: float,
) -> float:
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return 0.0
    # Local equirectangular projection around the first anchor.
    scale_x = 111_320.0 * math.cos(math.radians(lat1))
    ax, ay = 0.0, 0.0
    bx, by = (lon2 - lon1) * scale_x, (lat2 - lat1) * 111_320.0
    px, py = (lon - lon1) * scale_x, (lat - lat1) * 111_320.0
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return math.hypot(px - closest_x, py - closest_y)


def _track_consistency_score(anomalies: list[dict[str, object]], consistency: dict[str, object]) -> float | None:
    inconsistent = float(consistency.get("fixed_track_inconsistent_time_s", 0.0) or 0.0)
    consistent = float(consistency.get("fixed_track_consistent_time_s", 0.0) or 0.0)
    total = consistent + inconsistent
    if total <= 0.0 and not anomalies:
        return None
    penalty = (inconsistent / total) if total else 0.0
    penalty += min(0.5, 0.05 * len(anomalies))
    return max(0.0, 1.0 - penalty)


def _track_consistency_status(
    score: float | None,
    anomalies: list[dict[str, object]],
    consistency: dict[str, object],
) -> dict[str, object]:
    if score is None:
        return {"status": "not_computed", "score": None, "reason": "no local fixed-island anchors or motion anomalies"}
    if anomalies or float(consistency.get("fixed_track_inconsistent_time_s", 0.0) or 0.0) > 0.0:
        status = "suspect" if score < 0.5 else "warning"
    else:
        status = "ok"
    return {"status": status, "score": score, "reason": "local trajectory plausibility evidence"}


def _stop_diagnostics(epochs: list[SolutionEpoch], thresholds: QualityThresholds) -> dict[str, object]:
    stops: list[list[int]] = []
    current: list[int] = []
    speeds = [None for _ in epochs]
    for index, (left, right) in enumerate(zip(epochs, epochs[1:], strict=False), start=1):
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        dt = (right.time - left.time).total_seconds()
        if dt <= 0:
            continue
        speeds[index] = _haversine_m(left.lat, left.lon, right.lat, right.lon) / dt
    for index, speed in enumerate(speeds):
        if speed is not None and speed <= thresholds.stationary_speed_threshold_mps:
            current.append(index)
        elif current:
            if len(current) >= 3:
                stops.append(current)
            current = []
    if len(current) >= 3:
        stops.append(current)
    scatter_by_quality: dict[str, list[float]] = {quality: [] for quality in QUALITY_ORDER}
    max_drift = 0.0
    fixed_jumps = 0
    chatter_events = 0
    for stop in stops:
        qualities = [epochs[index].quality for index in stop]
        chatter_events += max(0, len(set(qualities)) - 1)
        for quality in set(qualities):
            points = [epochs[index] for index in stop if epochs[index].quality == quality and epochs[index].lat is not None and epochs[index].lon is not None]
            scatter = _position_scatter_m(points)
            scatter_by_quality.setdefault(quality, []).append(scatter)
            max_drift = max(max_drift, scatter)
        for left, right in zip(stop, stop[1:], strict=False):
            if epochs[right].quality == "fixed" and epochs[left].quality == "fixed" and speeds[right] and speeds[right] > thresholds.stationary_speed_threshold_mps * 5:
                fixed_jumps += 1
    return {
        "stop_count": len(stops),
        "scatter_m_by_quality": {quality: _stats(values) for quality, values in scatter_by_quality.items()},
        "max_drift_during_stop_m": max_drift if stops else None,
        "fixed_jumps_while_stationary": fixed_jumps,
        "quality_state_chatter_while_stopped": chatter_events,
    }


def _position_scatter_m(points: list[SolutionEpoch]) -> float:
    if len(points) < 2:
        return 0.0
    lat0 = sum(point.lat or 0.0 for point in points) / len(points)
    lon0 = sum(point.lon or 0.0 for point in points) / len(points)
    return max(_haversine_m(lat0, lon0, point.lat or lat0, point.lon or lon0) for point in points)


def _long_fixed_metrics(segments: list[Segment], track: dict[str, object]) -> dict[str, object]:
    fixed = [segment for segment in segments if segment.quality == "fixed"]
    durations = sorted((segment.duration_s for segment in fixed), reverse=True)
    distances = sorted((segment.distance_m for segment in fixed), reverse=True)
    segment_qc: list[dict[str, object]] = []
    for segment in fixed:
        if segment.duration_s >= 60.0 and (segment.max_step_m or 0.0) < 100.0:
            klass = "long_stable"
            reasons = ["duration_ge_60s", "no_large_internal_step"]
        elif segment.duration_s < 10.0:
            klass = "short_island"
            reasons = ["duration_lt_10s"]
        else:
            klass = "candidate"
            reasons = []
        segment_qc.append({**_segment_dict(segment), "qc_class": klass, "reasons": reasons})
    return {
        "fixed_time_ge_thresholds_s": {str(th): sum(segment.duration_s for segment in fixed if segment.duration_s >= th) for th in (10, 30, 60, 120)},
        "fixed_distance_ge_thresholds_m": {str(th): sum(segment.distance_m for segment in fixed if segment.distance_m >= th) for th in (100, 500, 1000, 2000)},
        "top_fixed_segments_by_duration": [_segment_dict(item) for item in sorted(fixed, key=lambda item: item.duration_s, reverse=True)[:10]],
        "top_fixed_segments_by_distance": [_segment_dict(item) for item in sorted(fixed, key=lambda item: item.distance_m, reverse=True)[:10]],
        "fixed_segment_duration_n50_s": _n_coverage_threshold(durations, 0.50),
        "fixed_segment_duration_n80_s": _n_coverage_threshold(durations, 0.80),
        "fixed_segment_distance_n50_m": _n_coverage_threshold(distances, 0.50),
        "fixed_segment_distance_n80_m": _n_coverage_threshold(distances, 0.80),
        "segment_qc": segment_qc,
    }


def _fixed_continuity_summary(
    segments: list[Segment],
    long_fixed: dict[str, object],
    top_by_distance: list[dict[str, object]],
    top_by_duration: list[dict[str, object]],
) -> dict[str, object]:
    fixed = [segment for segment in segments if segment.quality == "fixed"]
    raw_time = sum(segment.duration_s for segment in fixed)
    raw_distance_m = sum(segment.distance_m for segment in fixed)
    time_thresholds = long_fixed.get("fixed_time_ge_thresholds_s", {})
    distance_thresholds = long_fixed.get("fixed_distance_ge_thresholds_m", {})
    fixed_ge_30 = float(time_thresholds.get("30", 0.0) if isinstance(time_thresholds, dict) else 0.0)
    fixed_ge_60 = float(time_thresholds.get("60", 0.0) if isinstance(time_thresholds, dict) else 0.0)
    fixed_ge_500m = float(distance_thresholds.get("500", 0.0) if isinstance(distance_thresholds, dict) else 0.0) / 1000.0
    fixed_ge_1000m = float(distance_thresholds.get("1000", 0.0) if isinstance(distance_thresholds, dict) else 0.0) / 1000.0
    if fixed_ge_30 <= 0.0 and fixed_ge_500m <= 0.0:
        interpretation = "No useful long fixed intervals were found."
    elif fixed_ge_60 > 0.0 or fixed_ge_1000m > 0.0:
        interpretation = "Usable long fixed coverage exists; inspect top fixed segments and local diagnostics."
    else:
        interpretation = "Some fixed continuity exists, but it is mostly short or local; inspect segment tables before comparing configurations."
    top5_duration = top_by_duration[:5]
    top5_distance = top_by_distance[:5]
    return {
        "raw_fixed_time_s": raw_time,
        "raw_fixed_distance_km": raw_distance_m / 1000.0,
        "fixed_time_ge_10s": float(time_thresholds.get("10", 0.0) if isinstance(time_thresholds, dict) else 0.0),
        "fixed_time_ge_30s": fixed_ge_30,
        "fixed_time_ge_60s": fixed_ge_60,
        "fixed_time_ge_120s": float(time_thresholds.get("120", 0.0) if isinstance(time_thresholds, dict) else 0.0),
        "fixed_distance_ge_100m": float(distance_thresholds.get("100", 0.0) if isinstance(distance_thresholds, dict) else 0.0) / 1000.0,
        "fixed_distance_ge_500m": fixed_ge_500m,
        "fixed_distance_ge_1000m": fixed_ge_1000m,
        "fixed_distance_ge_2000m": float(distance_thresholds.get("2000", 0.0) if isinstance(distance_thresholds, dict) else 0.0) / 1000.0,
        "fixed_segment_duration_n50_s": long_fixed.get("fixed_segment_duration_n50_s"),
        "fixed_segment_duration_n80_s": long_fixed.get("fixed_segment_duration_n80_s"),
        "fixed_segment_distance_n50_m": long_fixed.get("fixed_segment_distance_n50_m"),
        "fixed_segment_distance_n80_m": long_fixed.get("fixed_segment_distance_n80_m"),
        "longest_fixed_segment_duration_s": max((segment.duration_s for segment in fixed), default=0.0),
        "longest_fixed_segment_distance_m": max((segment.distance_m for segment in fixed), default=0.0),
        "top5_fixed_segments_total_time_s": sum(float(item.get("duration_s", 0.0) or 0.0) for item in top5_duration),
        "top5_fixed_segments_total_distance_km": sum(float(item.get("distance_km", 0.0) or 0.0) for item in top5_distance),
        "interpretation": interpretation,
    }


def _top_fixed_segments(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    *,
    base_position: tuple[float, float, float] | None,
    order_by: str,
    limit: int = 10,
) -> list[dict[str, object]]:
    route_km = _route_distances_km(epochs)
    baseline_km = _baseline_distances_km(epochs, base_position)
    fixed = [segment for segment in segments if segment.quality == "fixed"]
    key = (lambda item: item.distance_m) if order_by == "distance" else (lambda item: item.duration_s)
    return [
        _fixed_segment_record(index, segment, route_km, baseline_km)
        for index, segment in enumerate(sorted(fixed, key=key, reverse=True)[:limit], start=1)
    ]


def _fixed_segment_record(
    rank: int,
    segment: Segment,
    route_km: list[float],
    baseline_km: list[float | None],
) -> dict[str, object]:
    klass, reasons = _classify_fixed_segment(segment)
    return {
        "rank": rank,
        "start_time": segment.start_time.isoformat(),
        "end_time": segment.end_time.isoformat(),
        "duration_s": segment.duration_s,
        "distance_km": segment.distance_m / 1000.0,
        "route_start_km": route_km[segment.start_index] if segment.start_index < len(route_km) else None,
        "route_end_km": route_km[segment.end_index] if segment.end_index < len(route_km) else None,
        "baseline_start_km": baseline_km[segment.start_index] if segment.start_index < len(baseline_km) else None,
        "baseline_end_km": baseline_km[segment.end_index] if segment.end_index < len(baseline_km) else None,
        "median_speed_mps": segment.median_speed_mps,
        "max_step_m": segment.max_step_m,
        "usable_class": klass,
        "qc_class": klass,
        "main_local_reasons": reasons,
    }


def _classify_fixed_segment(segment: Segment) -> tuple[str, list[str]]:
    reasons: list[str] = []
    max_step = segment.max_step_m or 0.0
    if max_step > 100.0:
        return "trajectory_suspect", ["large_internal_step"]
    if segment.duration_s >= 60.0 or segment.distance_m >= 1000.0:
        reasons.append("long_fixed_continuity")
        return "useful_supported", reasons
    if segment.duration_s >= 30.0 or segment.distance_m >= 500.0:
        reasons.append("moderate_fixed_continuity")
        return "useful_provisional", reasons
    if segment.duration_s < 10.0 and segment.distance_m < 100.0:
        return "short_island", ["short_time_and_distance"]
    return "useful_provisional", ["short_but_nontrivial_fixed_coverage"]


def _usable_fixed_totals(segments: list[Segment]) -> dict[str, float]:
    totals = {
        "usable_supported_fixed_time_s": 0.0,
        "usable_supported_fixed_distance_km": 0.0,
        "usable_provisional_fixed_time_s": 0.0,
        "usable_provisional_fixed_distance_km": 0.0,
        "trajectory_suspect_fixed_time_s": 0.0,
        "trajectory_suspect_fixed_distance_km": 0.0,
    }
    for segment in segments:
        if segment.quality != "fixed":
            continue
        klass, _reasons = _classify_fixed_segment(segment)
        if klass == "useful_supported":
            totals["usable_supported_fixed_time_s"] += segment.duration_s
            totals["usable_supported_fixed_distance_km"] += segment.distance_m / 1000.0
        elif klass == "useful_provisional":
            totals["usable_provisional_fixed_time_s"] += segment.duration_s
            totals["usable_provisional_fixed_distance_km"] += segment.distance_m / 1000.0
        elif klass == "trajectory_suspect":
            totals["trajectory_suspect_fixed_time_s"] += segment.duration_s
            totals["trajectory_suspect_fixed_distance_km"] += segment.distance_m / 1000.0
    return totals


def _n_coverage_threshold(values_desc: list[float], fraction: float) -> float | None:
    if not values_desc:
        return None
    target = sum(values_desc) * fraction
    total = 0.0
    for value in values_desc:
        total += value
        if total >= target:
            return value
    return values_desc[-1]


def _geometry_cost_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    stat: _StatAccumulator | None,
    trace_summary: dict[str, object] | None,
    thresholds: QualityThresholds,
) -> dict[str, object]:
    epoch_index = EpochIndex.build(epochs)
    used_counts: list[int] = []
    if stat is not None:
        for time, count in stat.used_counts_by_time.items():
            if epoch_index.nearest_index(time) is not None:
                used_counts.append(count)
    sats = [epoch.num_sats for epoch in epochs if epoch.num_sats is not None]
    ar_nb: list[int] = []
    trace_by_epoch = _trace_alignment_by_epoch(trace_summary)
    for item in trace_by_epoch.values():
        nb = item.get("trace_nb")
        if isinstance(nb, int):
            ar_nb.append(nb)
    segment_risk: list[dict[str, object]] = []
    for segment in segments:
        used_near = [
            stat.used_counts_by_time[time]
            for time in stat.used_counts_by_time
            if stat is not None and segment.start_time <= time <= segment.end_time
        ] if stat is not None else []
        median_used = _percentile(used_near, 50)
        score = 0.0
        reasons: list[str] = []
        if median_used is not None and median_used < thresholds.low_used_signals_warning:
            score += 0.4
            reasons.append("low_used_observations")
        if segment.quality == "fixed" and segment.duration_s < thresholds.trusted_fixed_min_duration_s:
            score += 0.2
            reasons.append("short_fixed_segment")
        segment_risk.append({**_segment_dict(segment), "geometry_risk_score": min(1.0, score), "reasons": reasons})
    return {
        "satellites_per_epoch": _stats([float(value) for value in sats]),
        "observations_used_per_epoch": _stats([float(value) for value in used_counts]),
        "common_base_rover_satellites": None,
        "constellation_counts": {},
        "frequency_counts": {},
        "pdop": None,
        "gdop": None,
        "trace_ar_nb": _stats([float(value) for value in ar_nb]),
        "before_after_elevation_mask_counts": None,
        "before_after_snr_mask_counts": None,
        "observations_removed_by_snr_threshold": None,
        "segment_geometry_risk": segment_risk,
    }


def _align_trace_summary(
    trace_summary: dict[str, object] | None,
    epoch_index: EpochIndex,
    tolerance_s: float,
) -> dict[str, object] | None:
    """Return trace summary enriched with solution-epoch alignment data."""

    if not trace_summary or not trace_summary.get("available"):
        return trace_summary
    enriched = dict(trace_summary)
    events = trace_summary.get("events")
    if not isinstance(events, dict):
        enriched["alignment"] = {
            "available": False,
            "reason": "trace event aggregates unavailable",
            "trace_events_aligned": 0,
            "trace_events_unaligned": 0,
            "trace_alignment_pct": None,
        }
        return enriched
    aggregates = events.get("event_time_aggregates", [])
    if not isinstance(aggregates, list):
        aggregates = []
    aligned_events = 0
    unaligned_events = 0
    counts_by_quality = {quality: {} for quality in QUALITY_ORDER}
    per_epoch: dict[int, dict[str, object]] = {}
    cache: dict[str, int | None] = {}
    for item in aggregates:
        if not isinstance(item, dict):
            continue
        time_text = item.get("time")
        time_basis = item.get("time_basis")
        counts = item.get("counts", {})
        if not isinstance(time_text, str) or not isinstance(counts, dict):
            continue
        event_total = sum(int(value) for value in counts.values() if isinstance(value, int | float))
        if event_total <= 0:
            continue
        cache_key = f"{time_basis or 'absolute'}:{time_text}"
        if cache_key not in cache:
            cache[cache_key] = _nearest_trace_epoch_index(time_text, str(time_basis or "absolute"), epoch_index, tolerance_s)
        nearest_index = cache[cache_key]
        if nearest_index is None:
            unaligned_events += event_total
            continue
        aligned_events += event_total
        epoch = epoch_index.epochs[nearest_index]
        quality_counts = counts_by_quality.setdefault(epoch.quality, {})
        epoch_bucket = per_epoch.setdefault(
            nearest_index,
            {
                "epoch_index": nearest_index,
                "time": epoch.time.isoformat(),
                "quality": epoch.quality,
                "counts": {},
                "trace_ar_ratio": None,
                "trace_ar_threshold": None,
                "trace_nb": None,
                "trace_nx": None,
                "trace_base_rover_dt": None,
            },
        )
        epoch_counts = epoch_bucket["counts"]
        for event_type, count in counts.items():
            if not isinstance(count, int | float):
                continue
            quality_counts[event_type] = int(quality_counts.get(event_type, 0)) + int(count)
            if isinstance(epoch_counts, dict):
                epoch_counts[event_type] = int(epoch_counts.get(event_type, 0)) + int(count)
        if item.get("ar_ratio_min") is not None:
            current = epoch_bucket.get("trace_ar_ratio")
            value = float(item["ar_ratio_min"])
            epoch_bucket["trace_ar_ratio"] = value if current is None else min(float(current), value)
        if item.get("ar_threshold") is not None:
            epoch_bucket["trace_ar_threshold"] = item.get("ar_threshold")
        if item.get("nb") is not None:
            epoch_bucket["trace_nb"] = item.get("nb")
        if item.get("nx") is not None:
            epoch_bucket["trace_nx"] = item.get("nx")
        if item.get("base_rover_dt_s") is not None:
            epoch_bucket["trace_base_rover_dt"] = item.get("base_rover_dt_s")
    total = aligned_events + unaligned_events
    enriched["alignment"] = {
        "available": bool(aggregates),
        "tolerance_s": tolerance_s,
        "trace_events_aligned": aligned_events,
        "trace_events_unaligned": unaligned_events,
        "trace_alignment_pct": (100.0 * aligned_events / total) if total else None,
        "event_counts_by_quality": counts_by_quality,
        "per_epoch": list(per_epoch.values()),
        "unique_trace_times_mapped": len(cache),
    }
    return enriched


def _nearest_trace_epoch_index(time_text: str, time_basis: str, epoch_index: EpochIndex, tolerance_s: float) -> int | None:
    """Resolve a trace event timestamp against solution epochs."""

    try:
        parsed = datetime.fromisoformat(time_text)
    except ValueError:
        return None
    if time_basis != "time_of_day":
        return epoch_index.nearest_index(parsed, max_dt_s=tolerance_s)
    if not epoch_index.epochs:
        return None
    tod = parsed.timetz().replace(tzinfo=None)
    base_date = epoch_index.epochs[0].time.astimezone(UTC).date()
    best_index: int | None = None
    best_delta = float("inf")
    for day_offset in (-1, 0, 1):
        candidate = datetime.combine(base_date + timedelta(days=day_offset), tod, tzinfo=UTC)
        index = epoch_index.nearest_index(candidate, max_dt_s=tolerance_s)
        if index is None:
            continue
        delta = abs(epoch_index.times_s[index] - _timestamp_s(candidate))
        if delta < best_delta:
            best_delta = delta
            best_index = index
    return best_index


def _baseline_summary(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    stat: _StatAccumulator | None,
    trace_summary: dict[str, object] | None,
    suspicion: dict[str, object],
    thresholds: QualityThresholds,
    *,
    base_ecef_xyz_m: tuple[float, float, float] | None,
    base_llh: tuple[float, float, float] | None,
) -> dict[str, object]:
    base_position = base_llh or (_ecef_to_llh(*base_ecef_xyz_m) if base_ecef_xyz_m else None)
    if base_position is None:
        return {
            "available": False,
            "reason": "base coordinates not supplied to quality analyzer",
            "bins_km": thresholds.baseline_bins,
            "interpretation": "Baseline distance is reported for context when available and is not used by itself to mark fixed epochs suspect.",
        }
    base_lat, base_lon, base_height = base_position
    distances: list[float] = []
    epoch_bin_values: list[float | None] = []
    for epoch in epochs:
        if epoch.lat is None or epoch.lon is None:
            epoch_bin_values.append(None)
            continue
        distance_km = _haversine_m(base_lat, base_lon, epoch.lat, epoch.lon) / 1000.0
        distances.append(distance_km)
        epoch_bin_values.append(distance_km)
    bins = _quality_bins_for_ranges(
        epochs,
        segments,
        thresholds,
        thresholds.baseline_bins,
        epoch_bin_values,
        stat=stat,
        trace_summary=trace_summary,
        suspicion=suspicion,
    )
    return {
        "available": bool(distances),
        "base_llh": {"lat": base_lat, "lon": base_lon, "height_m": base_height},
        "start_distance_km": distances[0] if distances else None,
        "median_distance_km": _percentile(distances, 50),
        "end_distance_km": distances[-1] if distances else None,
        "min_distance_km": min(distances) if distances else None,
        "max_distance_km": max(distances) if distances else None,
        "bins_km": thresholds.baseline_bins,
        "quality_by_baseline_bin": bins,
        "interpretation": "Baseline distance is context for expected ambiguity-resolution difficulty and is not used by itself to mark fixed epochs suspect.",
    }


def _quality_bins_for_ranges(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    thresholds: QualityThresholds,
    ranges: list[float],
    epoch_values_km: list[float | None],
    *,
    stat: _StatAccumulator | None,
    trace_summary: dict[str, object] | None,
    suspicion: dict[str, object],
) -> list[dict[str, object]]:
    if len(ranges) < 2:
        return []
    result: list[dict[str, object]] = []
    for start, end in zip(ranges, ranges[1:], strict=False):
        result.append(
            {
                "start_km": start,
                "end_km": end,
                "populated": False,
                "quality_time_s": {quality: 0.0 for quality in QUALITY_ORDER},
                "quality_distance_km": {quality: 0.0 for quality in QUALITY_ORDER},
                "epoch_count": 0,
                "elapsed_time_s": 0.0,
                "emitted_time_s": 0.0,
                "missing_time_s": 0.0,
                "track_distance_km": 0.0,
                "fixed_time_s": 0.0,
                "float_time_s": 0.0,
                "dgps_time_s": 0.0,
                "single_time_s": 0.0,
                "invalid_time_s": 0.0,
                "fixed_pct_of_elapsed": 0.0,
                "fixed_pct_of_emitted": 0.0,
                "fixed_distance_km": 0.0,
                "float_distance_km": 0.0,
                "dgps_distance_km": 0.0,
                "fixed_distance_pct": 0.0,
                "float_distance_pct": 0.0,
                "dgps_distance_pct": 0.0,
                "fixed_segment_count": 0,
                "fixed_segment_median_s": None,
                "fixed_segment_p95_s": None,
                "fixed_segment_max_s": None,
                "qc_supported_fixed_time_s": 0.0,
                "qc_provisional_fixed_time_s": 0.0,
                "qc_suspect_fixed_time_s": 0.0,
                "qc_unknown_fixed_time_s": 0.0,
                "stat_slip_epochs_pct": None,
                "stat_rejection_count": 0,
                "stat_carrier_p95_by_quality": {},
                "trace_low_ar_count": 0,
                "trace_slip_count": 0,
                "trace_rejection_count": 0,
                "trace_residual_outlier_count": 0,
                "fixed_jump_count": 0,
                "max_fixed_step_m": None,
                "max_speed_mps_by_quality": {},
                "long_fixed_time_ge_60s": 0.0,
                "long_fixed_distance_ge_1000m": 0.0,
                "fixed_time_ge_30s": 0.0,
                "fixed_time_ge_60s": 0.0,
                "fixed_distance_ge_500m": 0.0,
                "fixed_distance_ge_1000m": 0.0,
                "longest_fixed_segment_s": None,
                "longest_fixed_segment_km": None,
                "fixed_segment_distance_n80_m": None,
                "useful_supported_fixed_km": 0.0,
                "useful_provisional_fixed_km": 0.0,
                "trajectory_suspect_fixed_km": 0.0,
                "track_consistency_score": None,
                "trace_event_density_per_min": None,
                "stat_rejection_density_per_min": None,
            }
        )
    epoch_to_bin: dict[int, int] = {}
    for index, value in enumerate(epoch_values_km):
        if value is None:
            continue
        bin_index = _range_index(value, ranges)
        if bin_index is not None:
            epoch_to_bin[index] = bin_index
    for index, (left, right) in enumerate(zip(epochs, epochs[1:], strict=False)):
        bin_index = epoch_to_bin.get(index)
        if bin_index is None:
            continue
        item = result[bin_index]
        dt = max(0.0, (right.time - left.time).total_seconds())
        emitted_dt = min(dt, thresholds.gap_split_s) if dt <= thresholds.gap_split_s * 10 else 0.0
        step_km = (
            _haversine_m(left.lat, left.lon, right.lat, right.lon) / 1000.0
            if left.lat is not None and left.lon is not None and right.lat is not None and right.lon is not None
            else 0.0
        )
        speed = step_km * 1000.0 / dt if dt > 0 else 0.0
        item["populated"] = True
        item["epoch_count"] = int(item.get("epoch_count", 0)) + 1
        item["elapsed_time_s"] = float(item.get("elapsed_time_s", 0.0)) + dt
        item["emitted_time_s"] = float(item.get("emitted_time_s", 0.0)) + emitted_dt
        item["missing_time_s"] = float(item.get("missing_time_s", 0.0)) + max(0.0, dt - emitted_dt)
        item["track_distance_km"] = float(item.get("track_distance_km", 0.0)) + step_km
        quality_time = item["quality_time_s"]
        quality_distance = item["quality_distance_km"]
        if isinstance(quality_time, dict):
            quality_time[left.quality] = float(quality_time.get(left.quality, 0.0)) + emitted_dt
        if isinstance(quality_distance, dict):
            quality_distance[left.quality] = float(quality_distance.get(left.quality, 0.0)) + step_km
        max_speed_by_quality = item["max_speed_mps_by_quality"]
        if isinstance(max_speed_by_quality, dict):
            max_speed_by_quality[left.quality] = max(float(max_speed_by_quality.get(left.quality, 0.0) or 0.0), speed)
        if left.quality == "fixed" and right.quality == "fixed":
            step_m = step_km * 1000.0
            item["max_fixed_step_m"] = step_m if item["max_fixed_step_m"] is None else max(float(item["max_fixed_step_m"]), step_m)
            if speed > 90.0:
                item["fixed_jump_count"] = int(item.get("fixed_jump_count", 0)) + 1
    _add_segment_bin_metrics(result, ranges, epoch_values_km, segments, suspicion)
    _add_stat_bin_metrics(result, stat, epochs, epoch_to_bin)
    _add_trace_bin_metrics(result, trace_summary, epoch_to_bin)
    for item in result:
        quality_time = item.get("quality_time_s", {})
        quality_distance = item.get("quality_distance_km", {})
        if not isinstance(quality_time, dict):
            quality_time = {}
        if not isinstance(quality_distance, dict):
            quality_distance = {}
        elapsed = float(item.get("elapsed_time_s", 0.0) or 0.0)
        emitted = float(item.get("emitted_time_s", 0.0) or 0.0)
        track = float(item.get("track_distance_km", 0.0) or 0.0)
        item["fixed_time_s"] = float(quality_time.get("fixed", 0.0) or 0.0)
        item["float_time_s"] = float(quality_time.get("float", 0.0) or 0.0)
        item["dgps_time_s"] = float(quality_time.get("dgps", 0.0) or 0.0)
        item["single_time_s"] = float(quality_time.get("single", 0.0) or 0.0)
        item["invalid_time_s"] = float(quality_time.get("invalid", 0.0) or 0.0)
        item["fixed_pct_of_elapsed"] = _pct(float(item["fixed_time_s"]), elapsed)
        item["fixed_pct_of_emitted"] = _pct(float(item["fixed_time_s"]), emitted)
        item["fixed_distance_km"] = float(quality_distance.get("fixed", 0.0) or 0.0)
        item["float_distance_km"] = float(quality_distance.get("float", 0.0) or 0.0)
        item["dgps_distance_km"] = float(quality_distance.get("dgps", 0.0) or 0.0)
        item["fixed_distance_pct"] = _pct(float(item["fixed_distance_km"]), track)
        item["float_distance_pct"] = _pct(float(item["float_distance_km"]), track)
        item["dgps_distance_pct"] = _pct(float(item["dgps_distance_km"]), track)
        trace_events = sum(
            int(item.get(key, 0) or 0)
            for key in ("trace_low_ar_count", "trace_slip_count", "trace_rejection_count", "trace_residual_outlier_count")
        )
        item["trace_event_density_per_min"] = trace_events / (emitted / 60.0) if emitted else None
        item["stat_rejection_density_per_min"] = float(item.get("stat_rejection_count", 0) or 0) / (emitted / 60.0) if emitted else None
    return result


def _range_index(value: float, ranges: list[float]) -> int | None:
    for index, (start, end) in enumerate(zip(ranges, ranges[1:], strict=False)):
        if start <= value < end:
            return index
    return None


def _add_segment_bin_metrics(
    bins: list[dict[str, object]],
    ranges: list[float],
    epoch_values_km: list[float | None],
    segments: list[Segment],
    suspicion: dict[str, object],
) -> None:
    fixed_durations: dict[int, list[float]] = {index: [] for index in range(len(bins))}
    fixed_distances: dict[int, list[float]] = {index: [] for index in range(len(bins))}
    for segment in segments:
        value = epoch_values_km[segment.start_index] if segment.start_index < len(epoch_values_km) else None
        if value is None:
            continue
        bin_index = _range_index(value, ranges)
        if bin_index is None or segment.quality != "fixed":
            continue
        fixed_durations[bin_index].append(segment.duration_s)
        fixed_distances[bin_index].append(segment.distance_m)
        klass, _reasons = _classify_fixed_segment(segment)
        if klass == "useful_supported":
            bins[bin_index]["useful_supported_fixed_km"] = float(bins[bin_index].get("useful_supported_fixed_km", 0.0) or 0.0) + segment.distance_m / 1000.0
        elif klass == "useful_provisional":
            bins[bin_index]["useful_provisional_fixed_km"] = float(bins[bin_index].get("useful_provisional_fixed_km", 0.0) or 0.0) + segment.distance_m / 1000.0
        elif klass == "trajectory_suspect":
            bins[bin_index]["trajectory_suspect_fixed_km"] = float(bins[bin_index].get("trajectory_suspect_fixed_km", 0.0) or 0.0) + segment.distance_m / 1000.0
        bins[bin_index]["longest_fixed_segment_s"] = (
            segment.duration_s
            if bins[bin_index]["longest_fixed_segment_s"] is None
            else max(float(bins[bin_index]["longest_fixed_segment_s"]), segment.duration_s)
        )
        bins[bin_index]["longest_fixed_segment_km"] = (
            segment.distance_m / 1000.0
            if bins[bin_index]["longest_fixed_segment_km"] is None
            else max(float(bins[bin_index]["longest_fixed_segment_km"]), segment.distance_m / 1000.0)
        )
        if segment.duration_s >= 30.0:
            bins[bin_index]["fixed_time_ge_30s"] = float(bins[bin_index].get("fixed_time_ge_30s", 0.0) or 0.0) + segment.duration_s
        if segment.duration_s >= 60.0:
            bins[bin_index]["long_fixed_time_ge_60s"] = float(bins[bin_index].get("long_fixed_time_ge_60s", 0.0) or 0.0) + segment.duration_s
            bins[bin_index]["fixed_time_ge_60s"] = float(bins[bin_index].get("fixed_time_ge_60s", 0.0) or 0.0) + segment.duration_s
        if segment.distance_m >= 500.0:
            bins[bin_index]["fixed_distance_ge_500m"] = float(bins[bin_index].get("fixed_distance_ge_500m", 0.0) or 0.0) + segment.distance_m / 1000.0
        if segment.distance_m >= 1000.0:
            bins[bin_index]["long_fixed_distance_ge_1000m"] = float(bins[bin_index].get("long_fixed_distance_ge_1000m", 0.0) or 0.0) + segment.distance_m / 1000.0
            bins[bin_index]["fixed_distance_ge_1000m"] = float(bins[bin_index].get("fixed_distance_ge_1000m", 0.0) or 0.0) + segment.distance_m / 1000.0
    raw_fixed = float(suspicion.get("raw_fixed_time_s", 0.0) or 0.0)
    shares = {
        "qc_supported_fixed_time_s": float(suspicion.get("qc_supported_fixed_time_s", 0.0) or 0.0) / raw_fixed if raw_fixed else 0.0,
        "qc_provisional_fixed_time_s": float(suspicion.get("qc_provisional_fixed_time_s", 0.0) or 0.0) / raw_fixed if raw_fixed else 0.0,
        "qc_suspect_fixed_time_s": float(suspicion.get("qc_suspect_fixed_time_s", 0.0) or 0.0) / raw_fixed if raw_fixed else 0.0,
        "qc_unknown_fixed_time_s": float(suspicion.get("qc_unknown_fixed_time_s", 0.0) or 0.0) / raw_fixed if raw_fixed else 0.0,
    }
    for index, durations in fixed_durations.items():
        item = bins[index]
        quality_time = item.get("quality_time_s", {})
        fixed_time = float(quality_time.get("fixed", 0.0) or 0.0) if isinstance(quality_time, dict) else 0.0
        item["fixed_segment_count"] = len(durations)
        item["fixed_segment_median_s"] = _percentile(durations, 50)
        item["fixed_segment_p95_s"] = _percentile(durations, 95)
        item["fixed_segment_max_s"] = max(durations) if durations else None
        item["fixed_segment_distance_n80_m"] = _n_coverage_threshold(sorted(fixed_distances[index], reverse=True), 0.80)
        for key, share in shares.items():
            item[key] = fixed_time * share
        inconsistent = float(item.get("qc_suspect_fixed_time_s", 0.0) or 0.0)
        supported = float(item.get("qc_supported_fixed_time_s", 0.0) or 0.0)
        total = supported + inconsistent + float(item.get("qc_provisional_fixed_time_s", 0.0) or 0.0) + float(item.get("qc_unknown_fixed_time_s", 0.0) or 0.0)
        item["track_consistency_score"] = (supported / total) if total else None


def _add_stat_bin_metrics(
    bins: list[dict[str, object]],
    stat: _StatAccumulator | None,
    epochs: list[SolutionEpoch],
    epoch_to_bin: dict[int, int],
) -> None:
    if stat is None or not epochs:
        return
    epoch_index = EpochIndex.build(epochs)
    slip_epoch_bins: dict[int, set[int]] = {index: set() for index in range(len(bins))}
    for time, *_rest in stat.slip_events:
        nearest = epoch_index.nearest_index(time)
        if nearest is None:
            continue
        bin_index = epoch_to_bin.get(nearest)
        if bin_index is not None:
            slip_epoch_bins[bin_index].add(nearest)
    for time, count in stat.rejections_by_time.items():
        nearest = epoch_index.nearest_index(time)
        if nearest is None:
            continue
        bin_index = epoch_to_bin.get(nearest)
        if bin_index is not None:
            bins[bin_index]["stat_rejection_count"] = int(bins[bin_index].get("stat_rejection_count", 0)) + int(count)
    residuals_by_bin: dict[int, dict[str, dict[str, list[float]]]] = {
        index: {quality: {"carrier": []} for quality in QUALITY_ORDER} for index in range(len(bins))
    }
    for time, residuals in stat.residuals_by_time.items():
        nearest = epoch_index.nearest_index(time)
        if nearest is None:
            continue
        bin_index = epoch_to_bin.get(nearest)
        if bin_index is None:
            continue
        residuals_by_bin[bin_index][epochs[nearest].quality]["carrier"].extend(residuals.get("carrier", []))
    for index, item in enumerate(bins):
        epoch_count = int(item.get("epoch_count", 0) or 0)
        item["stat_slip_epochs_pct"] = _pct(len(slip_epoch_bins[index]), epoch_count) if epoch_count else None
        item["stat_carrier_p95_by_quality"] = {
            quality: _percentile(values["carrier"], 95)
            for quality, values in residuals_by_bin[index].items()
            if values["carrier"]
        }


def _add_trace_bin_metrics(
    bins: list[dict[str, object]],
    trace_summary: dict[str, object] | None,
    epoch_to_bin: dict[int, int],
) -> None:
    trace_by_epoch = _trace_alignment_by_epoch(trace_summary)
    for epoch_index, item in trace_by_epoch.items():
        bin_index = epoch_to_bin.get(epoch_index)
        if bin_index is None:
            continue
        counts = item.get("counts", {})
        if not isinstance(counts, dict):
            continue
        bins[bin_index]["trace_low_ar_count"] = int(bins[bin_index].get("trace_low_ar_count", 0)) + int(counts.get("ar_ratio", 0) or 0)
        bins[bin_index]["trace_slip_count"] = int(bins[bin_index].get("trace_slip_count", 0)) + int(counts.get("cycle_slip", 0) or 0)
        bins[bin_index]["trace_rejection_count"] = int(bins[bin_index].get("trace_rejection_count", 0)) + int(counts.get("observation_rejection", 0) or 0)
        bins[bin_index]["trace_residual_outlier_count"] = int(bins[bin_index].get("trace_residual_outlier_count", 0)) + int(counts.get("residual_outlier", 0) or 0)


def _route_bins(
    epochs: list[SolutionEpoch],
    segments: list[Segment],
    stat: _StatAccumulator | None,
    trace_summary: dict[str, object] | None,
    suspicion: dict[str, object],
    thresholds: QualityThresholds,
) -> list[dict[str, object]]:
    if thresholds.route_bin_km is None or thresholds.route_bin_km <= 0:
        return []
    bin_m = thresholds.route_bin_km * 1000.0
    distance = 0.0
    epoch_values: list[float | None] = []
    for left, right in zip(epochs, epochs[1:], strict=False):
        epoch_values.append(distance / 1000.0)
        if left.lat is None or left.lon is None or right.lat is None or right.lon is None:
            continue
        distance += _haversine_m(left.lat, left.lon, right.lat, right.lon)
    if epochs:
        epoch_values.append(distance / 1000.0)
    max_km = max(epoch_values) if epoch_values else 0.0
    ranges = [index * thresholds.route_bin_km for index in range(int(max_km // thresholds.route_bin_km) + 2)]
    return _quality_bins_for_ranges(
        epochs,
        segments,
        thresholds,
        ranges,
        epoch_values,
        stat=stat,
        trace_summary=trace_summary,
        suspicion=suspicion,
    )


def _false_fix_suspicion(
    segments: list[Segment],
    epochs: list[SolutionEpoch],
    stat: _StatAccumulator | None,
    transitions: dict[str, object],
    residuals: dict[str, object],
    slips: dict[str, object],
    trace_summary: dict[str, object] | None,
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
        "stat_recent_slip": 0.0,
        "stat_high_residual": 0.0,
        "stat_rejection_burst": 0.0,
        "transition_jump": 0.0,
        "incomplete_diagnostics": 0.0,
        "trace_low_ar_ratio": 0.0,
        "trace_ambiguity_validation_failed": 0.0,
        "trace_recent_slip": 0.0,
        "trace_residual_outlier": 0.0,
        "trace_rejection_burst": 0.0,
        "trace_base_rover_dt": 0.0,
        "trace_warning_or_error": 0.0,
    }
    evidence_sources = {
        "short_time_segment": ["solution"],
        "short_distance_while_moving": ["solution"],
        "recent_slip": ["stat"],
        "high_residual": ["stat"],
        "stat_recent_slip": ["stat"],
        "stat_high_residual": ["stat"],
        "stat_rejection_burst": ["stat"],
        "transition_jump": ["solution"],
        "incomplete_diagnostics": ["stat"],
        "trace_low_ar_ratio": ["trace"],
        "trace_ambiguity_validation_failed": ["trace"],
        "trace_recent_slip": ["trace"],
        "trace_residual_outlier": ["trace"],
        "trace_rejection_burst": ["trace"],
        "trace_base_rover_dt": ["trace"],
        "trace_warning_or_error": ["trace"],
    }
    trace_by_epoch = _trace_alignment_by_epoch(trace_summary)
    stat_slip_times_s = (
        sorted({_timestamp_s(time) for time, _, _, _ in stat.slip_events})
        if stat and slips.get("epochs_with_slip_pct") is not None
        else []
    )
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
        if stat_slip_times_s and slips_hard and _has_recent_slip(stat_slip_times_s, segment, thresholds.recent_slip_window_s):
            flags.extend(["recent_slip", "stat_recent_slip"])
        if residuals_hard and _segment_high_residual(stat, segment, thresholds):
            flags.extend(["high_residual", "stat_high_residual"])
        if segment.start_index in fixed_entry_warning_indexes:
            flags.append("transition_jump")
        flags.extend(_trace_flags_for_segment(segment, trace_by_epoch))
        for flag in set(flags):
            if flag in reasons:
                reasons[flag] += segment.duration_s
        diagnostics_incomplete = bool(stat) and (not residuals_hard or not slips_hard)
        if diagnostics_incomplete:
            reasons["incomplete_diagnostics"] += segment.duration_s
        severe = (
            "transition_jump" in flags
            or ("stat_recent_slip" in flags and "short_time_segment" in flags)
            or ("trace_low_ar_ratio" in flags and "short_time_segment" in flags)
            or ("trace_ambiguity_validation_failed" in flags and "short_time_segment" in flags)
            or ("trace_residual_outlier" in flags and "short_time_segment" in flags)
        )
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
    reason_details = _reason_details(reasons, evidence_sources, total_fixed_time, total_fixed_distance, residuals, slips, trace_summary)
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
        "reason_details": reason_details,
        "evidence_sources": evidence_sources,
    }


def _reason_details(
    reasons: dict[str, float],
    evidence_sources: dict[str, list[str]],
    total_fixed_time: float,
    total_fixed_distance: float,
    residuals: dict[str, object],
    slips: dict[str, object],
    trace_summary: dict[str, object] | None,
) -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}
    trace_alignment = trace_summary.get("alignment", {}) if isinstance(trace_summary, dict) else {}
    for reason, affected_time in reasons.items():
        sources = evidence_sources.get(reason, [])
        aligned = True
        if "trace" in sources:
            aligned = bool(isinstance(trace_alignment, dict) and trace_alignment.get("available"))
        elif reason in {"recent_slip", "stat_recent_slip"}:
            aligned = slips.get("epochs_with_slip_pct") is not None
        elif reason in {"high_residual", "stat_high_residual"}:
            aligned = bool(residuals.get("quality_aligned"))
        affected_distance = total_fixed_distance * affected_time / total_fixed_time if total_fixed_time else 0.0
        details[reason] = {
            "source": "+".join(sources) if sources else "unknown",
            "aligned": aligned,
            "affected_fixed_time_s": affected_time,
            "affected_fixed_distance_m": affected_distance,
            "affected_epoch_count": None,
        }
    return details


def _trace_alignment_by_epoch(trace_summary: dict[str, object] | None) -> dict[int, dict[str, object]]:
    if not trace_summary:
        return {}
    alignment = trace_summary.get("alignment")
    if not isinstance(alignment, dict):
        return {}
    per_epoch = alignment.get("per_epoch", [])
    if not isinstance(per_epoch, list):
        return {}
    result: dict[int, dict[str, object]] = {}
    for item in per_epoch:
        if not isinstance(item, dict):
            continue
        index = item.get("epoch_index")
        if isinstance(index, int):
            result[index] = item
    return result


def _trace_flags_for_segment(segment: Segment, trace_by_epoch: dict[int, dict[str, object]]) -> list[str]:
    flags: set[str] = set()
    for index in range(segment.start_index, segment.end_index + 1):
        item = trace_by_epoch.get(index)
        if not item:
            continue
        counts = item.get("counts", {})
        if not isinstance(counts, dict):
            continue
        ar_ratio = item.get("trace_ar_ratio")
        ar_threshold = item.get("trace_ar_threshold")
        if ar_ratio is not None:
            threshold = float(ar_threshold) if ar_threshold is not None else 3.0
            if float(ar_ratio) < threshold:
                flags.add("trace_low_ar_ratio")
        if int(counts.get("ambiguity_validation_failed", 0) or 0):
            flags.add("trace_ambiguity_validation_failed")
        if int(counts.get("cycle_slip", 0) or 0) or int(counts.get("lli", 0) or 0):
            flags.add("trace_recent_slip")
        if int(counts.get("residual_outlier", 0) or 0):
            flags.add("trace_residual_outlier")
        if int(counts.get("observation_rejection", 0) or 0) >= 5:
            flags.add("trace_rejection_burst")
        if item.get("trace_base_rover_dt") is not None and abs(float(item["trace_base_rover_dt"])) > 1.0:
            flags.add("trace_base_rover_dt")
        if int(counts.get("warning_or_error", 0) or 0):
            flags.add("trace_warning_or_error")
    return sorted(flags)


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


def _residual_summary(stat: _StatAccumulator | None, epoch_index: EpochIndex) -> dict[str, object]:
    if stat is None or stat.parsed_sat_lines == 0:
        return _empty_residual_summary()
    by_quality: dict[str, dict[str, list[float]]] = {quality: {"carrier": [], "code": []} for quality in QUALITY_ORDER}
    for time, residuals in stat.residuals_by_time.items():
        nearest = epoch_index.nearest(time)
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


def _slip_summary(stat: _StatAccumulator, epoch_index: EpochIndex) -> dict[str, object]:
    if stat.parsed_sat_lines == 0:
        return _empty_slip_summary()
    duration_min = 0.0
    epochs = epoch_index.epochs
    if epochs:
        duration_min = max(0.0, (epochs[-1].time - epochs[0].time).total_seconds() / 60.0)
    slip_epoch_indexes: set[int] = set()
    unique_stat_slip_epochs = {time for time, _, _, _ in stat.slip_events}
    stat_epoch_to_solution_index: dict[datetime, int | None] = {
        time: epoch_index.nearest_index(time) for time in unique_stat_slip_epochs
    }
    for time, _, _, _ in stat.slip_events:
        nearest_index = stat_epoch_to_solution_index.get(time)
        if nearest_index is not None:
            slip_epoch_indexes.add(nearest_index)
    epochs_with_slip_pct = (100.0 * len(slip_epoch_indexes) / len(epochs)) if epochs and slip_epoch_indexes else None
    fixed_count = sum(1 for epoch in epochs if epoch.quality == "fixed")
    fixed_with_recent = 0
    if fixed_count and slip_epoch_indexes:
        slip_times_s = sorted(_timestamp_s(epochs[index].time) for index in slip_epoch_indexes)
        for epoch in epochs:
            if epoch.quality != "fixed":
                continue
            epoch_s = _timestamp_s(epoch.time)
            pos = bisect_left(slip_times_s, epoch_s - 10.0)
            if pos < len(slip_times_s) and slip_times_s[pos] <= epoch_s:
                fixed_with_recent += 1
    return {
        "available": True,
        "raw_slip_flags_total": stat.slip_count,
        "raw_slip_flags_per_min": (stat.slip_count / duration_min) if duration_min else 0.0,
        "deduplicated_slip_events_total": len(stat.slip_events),
        "unique_slip_epochs": len(unique_stat_slip_epochs),
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


def write_quality_json(
    path: Path,
    analysis: QualityAnalysis,
    *,
    include_all_segments: bool = False,
    include_geometry_segments: bool = False,
    include_empty_bins: bool = False,
) -> None:
    """Write RTK quality analysis JSON."""

    path.write_text(
        json.dumps(
            analysis.as_dict(
                include_all_segments=include_all_segments,
                include_geometry_segments=include_geometry_segments,
                include_empty_bins=include_empty_bins,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )


def write_quality_segments_jsonl(path: Path, analysis: QualityAnalysis) -> None:
    """Write full fixed segment detail as JSON Lines."""

    data = analysis.as_dict(include_all_segments=True)
    segments = data.get("long_fixed_metrics", {}).get("segment_qc", {}) if isinstance(data.get("long_fixed_metrics"), dict) else []
    lines = []
    if isinstance(segments, list):
        lines = [json.dumps(item, sort_keys=True, default=str) for item in segments if isinstance(item, dict)]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def compare_quality_reports(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    """Compare two quality-analysis JSON reports."""

    left_metrics = _comparison_metrics(left)
    right_metrics = _comparison_metrics(right)
    deltas = {
        key: (
            float(right_metrics[key]) - float(left_metrics[key])
            if isinstance(left_metrics.get(key), int | float) and isinstance(right_metrics.get(key), int | float)
            else None
        )
        for key in sorted(set(left_metrics) | set(right_metrics))
    }
    warnings: list[str] = []
    cleanliness_improved = (
        (deltas.get("rejection_count") is not None and float(deltas["rejection_count"]) < 0)
        or (deltas.get("raw_slip_flags") is not None and float(deltas["raw_slip_flags"]) < 0)
        or (deltas.get("fixed_carrier_p95") is not None and float(deltas["fixed_carrier_p95"]) < 0)
    )
    track_worsened = (
        (deltas.get("track_consistency_score") is not None and float(deltas["track_consistency_score"]) < -0.05)
        or (deltas.get("fixed_internal_jump_count") is not None and float(deltas["fixed_internal_jump_count"]) > 0)
        or (deltas.get("long_fixed_time_ge_60s") is not None and float(deltas["long_fixed_time_ge_60s"]) < 0)
    )
    if cleanliness_improved and track_worsened:
        warnings.append(
            "SNR/filtering reduced noisy observations but produced worse trajectory consistency; do not treat this as a quality improvement."
        )
    return {"left": left_metrics, "right": right_metrics, "deltas": deltas, "warnings": warnings}


def format_quality_comparison_markdown(comparison: dict[str, object]) -> str:
    """Render a human-readable comparison of two quality reports."""

    deltas = comparison.get("deltas", {})
    left = comparison.get("left", {})
    right = comparison.get("right", {})
    lines = [
        "# RTK Quality Comparison",
        "",
        "| Metric | Left | Right | Delta |",
        "| --- | ---: | ---: | ---: |",
    ]
    if isinstance(deltas, dict) and isinstance(left, dict) and isinstance(right, dict):
        for key in (
            "raw_fixed_time_s",
            "raw_fixed_distance_km",
            "long_fixed_time_ge_60s",
            "long_fixed_distance_ge_1000m",
            "fixed_segment_duration_n80_s",
            "fixed_segment_distance_n80_m",
            "longest_fixed_segment_distance_m",
            "usable_supported_fixed_distance_km",
            "usable_provisional_fixed_distance_km",
            "trajectory_suspect_fixed_distance_km",
            "track_consistency_score",
            "fixed_internal_jump_count",
            "fixed_islands_with_large_offset_count",
            "fixed_carrier_p95",
            "rejection_count",
            "raw_slip_flags",
        ):
            lines.append(f"| {key} | {_fmt_any(left.get(key))} | {_fmt_any(right.get(key))} | {_fmt_any(deltas.get(key))} |")
    warnings = comparison.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- WARNING: {warning}" for warning in warnings)  # type: ignore[union-attr]
    return "\n".join(lines) + "\n"


def _comparison_metrics(report: dict[str, object]) -> dict[str, object]:
    suspicion = report.get("false_fix_suspicion", {})
    continuity = report.get("fixed_continuity_summary", {})
    residuals = report.get("residuals", {})
    slips = report.get("slips", {})
    rejections = report.get("rejections", {})
    track = report.get("track_plausibility", {})
    long_fixed = report.get("long_fixed_metrics", {})
    fixed_time_thresholds = long_fixed.get("fixed_time_ge_thresholds_s", {}) if isinstance(long_fixed, dict) else {}
    fixed_distance_thresholds = long_fixed.get("fixed_distance_ge_thresholds_m", {}) if isinstance(long_fixed, dict) else {}
    carrier = residuals.get("carrier_abs_m", {}) if isinstance(residuals, dict) else {}
    return {
        "raw_fixed_time_s": (
            continuity.get("raw_fixed_time_s")
            if isinstance(continuity, dict) and continuity.get("raw_fixed_time_s") is not None
            else suspicion.get("raw_fixed_time_s")
            if isinstance(suspicion, dict)
            else None
        ),
        "raw_fixed_distance_km": continuity.get("raw_fixed_distance_km") if isinstance(continuity, dict) else None,
        "qc_supported_fixed_time_s": suspicion.get("qc_supported_fixed_time_s") if isinstance(suspicion, dict) else None,
        "long_fixed_time_ge_60s": (
            continuity.get("fixed_time_ge_60s")
            if isinstance(continuity, dict) and continuity.get("fixed_time_ge_60s") is not None
            else fixed_time_thresholds.get("60")
            if isinstance(fixed_time_thresholds, dict)
            else None
        ),
        "long_fixed_distance_ge_1000m": (
            continuity.get("fixed_distance_ge_1000m")
            if isinstance(continuity, dict) and continuity.get("fixed_distance_ge_1000m") is not None
            else (fixed_distance_thresholds.get("1000") / 1000.0 if isinstance(fixed_distance_thresholds, dict) and isinstance(fixed_distance_thresholds.get("1000"), int | float) else None)
        ),
        "fixed_segment_duration_n80_s": continuity.get("fixed_segment_duration_n80_s") if isinstance(continuity, dict) else None,
        "fixed_segment_distance_n80_m": continuity.get("fixed_segment_distance_n80_m") if isinstance(continuity, dict) else None,
        "longest_fixed_segment_distance_m": continuity.get("longest_fixed_segment_distance_m") if isinstance(continuity, dict) else None,
        "usable_supported_fixed_distance_km": report.get("usable_supported_fixed_distance_km"),
        "usable_provisional_fixed_distance_km": report.get("usable_provisional_fixed_distance_km"),
        "trajectory_suspect_fixed_distance_km": report.get("trajectory_suspect_fixed_distance_km"),
        "track_consistency_score": track.get("track_consistency_score") if isinstance(track, dict) else None,
        "fixed_internal_jump_count": track.get("fixed_internal_jump_count") if isinstance(track, dict) else None,
        "fixed_islands_with_large_offset_count": track.get("fixed_islands_with_large_offset_count") if isinstance(track, dict) else None,
        "fixed_carrier_p95": carrier.get("fixed_p95") if isinstance(carrier, dict) else None,
        "rejection_count": rejections.get("count") if isinstance(rejections, dict) else None,
        "raw_slip_flags": slips.get("raw_slip_flags_total") if isinstance(slips, dict) else None,
    }


def format_quality_text(analysis: QualityAnalysis) -> str:
    """Return compact terminal quality summary."""

    data = analysis.as_dict()
    time_summary = data["time_summary"]  # type: ignore[index]
    distance_summary = data["distance_summary"]  # type: ignore[index]
    suspicion = data["false_fix_suspicion"]  # type: ignore[index]
    continuity = data.get("fixed_continuity_summary", {})
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
    lines.append("Usable fixed continuity:")
    if isinstance(continuity, dict):
        lines.append(
            f"  >=60 s: {float(continuity.get('fixed_time_ge_60s', 0.0) or 0.0):8.1f} s, "
            f">=1 km: {float(continuity.get('fixed_distance_ge_1000m', 0.0) or 0.0):7.3f} km, "
            f"N80: {continuity.get('fixed_segment_duration_n80_s')} s / {continuity.get('fixed_segment_distance_n80_m')} m"
        )
        lines.append(f"  {continuity.get('interpretation', '')}")
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


def format_quality_markdown(
    analysis: QualityAnalysis,
    *,
    include_raw_json: bool = False,
    show_empty_baseline_bins: bool = False,
) -> str:
    """Return Markdown RTK quality report."""

    data = analysis.as_dict(include_empty_bins=show_empty_baseline_bins)
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
        "## 3. Usable Fixed Continuity",
        "",
    ]
    continuity = data.get("fixed_continuity_summary", {})
    if isinstance(continuity, dict):
        lines.extend(
            [
                str(continuity.get("interpretation", "")),
                "",
                "N80 means 80% of fixed time/distance lies in segments at least this long.",
                "Median segment duration is a fragmentation diagnostic only; it is not a vehicle/highway quality headline.",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Raw fixed time / distance | {_fmt_any(continuity.get('raw_fixed_time_s'))} s / {_fmt_any(continuity.get('raw_fixed_distance_km'))} km |",
                f"| Fixed time >=10/30/60/120 s | {_fmt_any(continuity.get('fixed_time_ge_10s'))} / {_fmt_any(continuity.get('fixed_time_ge_30s'))} / {_fmt_any(continuity.get('fixed_time_ge_60s'))} / {_fmt_any(continuity.get('fixed_time_ge_120s'))} |",
                f"| Fixed distance >=100/500/1000/2000 m | {_fmt_any(continuity.get('fixed_distance_ge_100m'))} / {_fmt_any(continuity.get('fixed_distance_ge_500m'))} / {_fmt_any(continuity.get('fixed_distance_ge_1000m'))} / {_fmt_any(continuity.get('fixed_distance_ge_2000m'))} km |",
                f"| Fixed duration N50 / N80 | {_fmt_any(continuity.get('fixed_segment_duration_n50_s'))} s / {_fmt_any(continuity.get('fixed_segment_duration_n80_s'))} s |",
                f"| Fixed distance N50 / N80 | {_fmt_any(continuity.get('fixed_segment_distance_n50_m'))} m / {_fmt_any(continuity.get('fixed_segment_distance_n80_m'))} m |",
                f"| Longest fixed segment | {_fmt_any(continuity.get('longest_fixed_segment_duration_s'))} s / {_fmt_any(continuity.get('longest_fixed_segment_distance_m'))} m |",
                f"| Top-5 fixed segment total | {_fmt_any(continuity.get('top5_fixed_segments_total_time_s'))} s / {_fmt_any(continuity.get('top5_fixed_segments_total_distance_km'))} km |",
            ]
        )
    lines.extend(
        [
            "",
            "## 4. Top Fixed Segments By Distance",
            "",
            "| Rank | Start | End | Duration s | Distance km | Route km | Baseline km | Median speed m/s | Max step m | Class | Main local reasons |",
            "| ---: | --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for item in data.get("top_fixed_segments_by_distance", []):  # type: ignore[union-attr]
        if isinstance(item, dict):
            lines.append(_fixed_segment_markdown_row(item))
    if not data.get("top_fixed_segments_by_distance"):
        lines.append("| n/a | n/a | n/a | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## 5. Top Fixed Segments By Duration",
            "",
            "| Rank | Start | End | Duration s | Distance km | Route km | Baseline km | Median speed m/s | Max step m | Class | Main local reasons |",
            "| ---: | --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    for item in data.get("top_fixed_segments_by_duration", []):  # type: ignore[union-attr]
        if isinstance(item, dict):
            lines.append(_fixed_segment_markdown_row(item))
    if not data.get("top_fixed_segments_by_duration"):
        lines.append("| n/a | n/a | n/a | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## 6. Segment Summary",
            "",
            "| Quality | Count | Median duration s | P95 duration s | Max duration s | Median distance m | P95 distance m | Max distance m |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for quality in ("fixed", "float", "dgps", "single", "invalid"):
        item = data["segments"][quality]  # type: ignore[index]
        lines.append(
            f"| {quality} | {item['count']} | {item['duration_s']['median']} | {item['duration_s']['p95']} | {item['duration_s']['max']} | "
            f"{item['distance_m']['median']} | {item['distance_m']['p95']} | {item['distance_m']['max']} |"
        )
    lines.extend(
        [
            "",
            "## 7. Fixed Confidence Classification",
            "",
            "Suspect fixed is heuristic evidence, not proof of a false fix.",
            "Raw fixed percentage and median segment duration are diagnostics, not quality headlines.",
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
            "## 8. Track Plausibility",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    track = data.get("track_plausibility", {})
    if isinstance(track, dict):
        fixed_steps = _quality_stat_scope(track.get("horizontal_step_m_by_quality"), "fixed")
        fixed_speeds = _quality_stat_scope(track.get("speed_mps_by_quality"), "fixed")
        lines.append(f"| fixed step p95 / p99 / max m | {_fmt_any(fixed_steps.get('p95'))} / {_fmt_any(fixed_steps.get('p99'))} / {_fmt_any(fixed_steps.get('max'))} |")
        lines.append(f"| fixed speed p95 / p99 / max m/s | {_fmt_any(fixed_speeds.get('p95'))} / {_fmt_any(fixed_speeds.get('p99'))} / {_fmt_any(fixed_speeds.get('max'))} |")
        lines.append(f"| fixed internal jumps | {_fmt_any(track.get('fixed_internal_jump_count'))} |")
        lines.append(f"| fixed jumps while stationary | {_fmt_any(track.get('fixed_jumps_while_stationary_count'))} |")
        lines.append(f"| fixed island cross-track p95 m | {_fmt_any(track.get('fixed_island_cross_track_p95_m'))} |")
        lines.append(f"| fixed island max offset m | {_fmt_any(track.get('fixed_island_max_offset_m'))} |")
        lines.append(f"| fixed islands with large offset | {_fmt_any(track.get('fixed_islands_with_large_offset_count'))} |")
        lines.append(f"| track consistency score | {_fmt_any(track.get('track_consistency_score'))} |")
    long_fixed = data.get("long_fixed_metrics", {})
    if isinstance(long_fixed, dict):
        lines.extend(
            [
                "",
                "### Long Stable Fixed Coverage",
                "",
                "| Threshold | Fixed time s |",
                "| --- | ---: |",
            ]
        )
        for threshold, seconds in (long_fixed.get("fixed_time_ge_thresholds_s") or {}).items():
            lines.append(f"| >= {threshold} s | {_fmt_any(seconds)} |")
        lines.extend(["", "| Threshold | Fixed distance m |", "| --- | ---: |"])
        for threshold, meters in (long_fixed.get("fixed_distance_ge_thresholds_m") or {}).items():
            lines.append(f"| >= {threshold} m | {_fmt_any(meters)} |")
    stop = data.get("stop_diagnostics", {})
    if isinstance(stop, dict):
        lines.extend(
            [
                "",
                "### Stop / Low-Speed Diagnostics",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| stop count | {_fmt_any(stop.get('stop_count'))} |",
                f"| max drift during stop m | {_fmt_any(stop.get('max_drift_during_stop_m'))} |",
                f"| fixed jumps while stationary | {_fmt_any(stop.get('fixed_jumps_while_stationary'))} |",
                f"| quality-state chatter while stopped | {_fmt_any(stop.get('quality_state_chatter_while_stopped'))} |",
            ]
        )
    lines.extend(
        [
            "",
            "## 9. Residual Summary",
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
            "## 10. Slip / Rejection Summary",
            "",
            "Raw slip flags and rejection totals are observation-cleanliness diagnostics; they are not position-correctness proof.",
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
            "## 11. Motion And Baseline Context",
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
            "## 12. Quality By Base-Rover Distance",
            "",
            "| Baseline km | Epochs | Track km | Fixed s / % | Useful >=60s | Useful >=1km | Longest fixed s / km | Fixed segs | Trace low AR / slips / outliers |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    baseline_bins = baseline.get("quality_by_baseline_bin", []) if isinstance(baseline, dict) else []
    baseline_rows = []
    if isinstance(baseline_bins, list):
        for item in baseline_bins:
            if isinstance(item, dict) and (show_empty_baseline_bins or item.get("populated")):
                baseline_rows.append(item)
    if baseline_rows:
        for item in baseline_rows:
            lines.append(_quality_bin_markdown_row(item, label=f"{_fmt_any(item.get('start_km'))}-{_fmt_any(item.get('end_km'))}"))
        if not show_empty_baseline_bins:
            lines.append("")
            lines.append("Empty baseline bins omitted; full bins are in JSON.")
    elif isinstance(baseline, dict) and baseline.get("available"):
        lines.append("| n/a | 0 | 0 | n/a | n/a | n/a | n/a | 0 | n/a |")
    else:
        lines.append("| unavailable | 0 | 0 | n/a | n/a | n/a | n/a | 0 | n/a |")
    lines.extend(
        [
            "",
            "## 13. Quality By Route Distance",
            "",
            "| Route km | Epochs | Track km | Fixed s / % | Useful >=60s | Useful >=1km | Longest fixed s / km | Fixed segs | Trace low AR / slips / outliers |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    route_bins = data.get("route_bins", [])
    if isinstance(route_bins, list) and route_bins:
        for item in route_bins[:20]:
            if not isinstance(item, dict):
                continue
            lines.append(_quality_bin_markdown_row(item, label=f"{_fmt_any(item.get('start_km'))}-{_fmt_any(item.get('end_km'))}"))
    else:
        lines.append("| n/a | 0 | 0 | n/a | n/a | n/a | n/a | 0 | n/a |")
    lines.extend(
        [
            "",
            "## 14. RTKLIB Trace Diagnostics",
            "",
        ]
    )
    trace = data.get("trace", {"available": False})
    if isinstance(trace, dict) and trace.get("available"):
        counters = trace.get("counters", {})
        lines.extend(
            [
                f"- Trace mode: `{trace.get('source')}`",
                f"- Trace parsed path: `{trace.get('parsed_trace_path', trace.get('path'))}`",
                f"- Effective trace level: `{trace.get('effective_level')}`",
                f"- Trace retained: {'yes' if trace.get('retained') else 'no'}",
                f"- Trace file size bytes: {trace.get('trace_file_size_bytes', trace.get('bytes_read'))}",
                f"- Trace raw bytes read: {trace.get('trace_raw_bytes_read', trace.get('trace_bytes_read', trace.get('bytes_read')))}",
                f"- Trace decoded chars read: {trace.get('trace_decoded_chars_read', 'n/a')}",
                f"- Trace lines read: {trace.get('trace_lines_read', trace.get('lines_read'))}",
                f"- Trace truncated: {trace.get('trace_truncated', False)}",
                f"- Trace parse elapsed s: {_fmt_any(trace.get('trace_parse_elapsed_s'))}",
                f"- Trace parse rate MB/s: {_fmt_any(trace.get('trace_parse_rate_mb_s'))}",
                f"- Trace deleted: {trace.get('trace_deleted', False)}",
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
        alignment = trace.get("alignment", {})
        if isinstance(alignment, dict) and alignment.get("available"):
            lines.extend(
                [
                    "",
                    "| Trace alignment metric | Value |",
                    "| --- | ---: |",
                    f"| aligned events | {_fmt_any(alignment.get('trace_events_aligned'))} |",
                    f"| unaligned events | {_fmt_any(alignment.get('trace_events_unaligned'))} |",
                    f"| alignment % | {_fmt_any(alignment.get('trace_alignment_pct'))} |",
                    f"| unique trace times mapped | {_fmt_any(alignment.get('unique_trace_times_mapped'))} |",
                ]
            )
            counts_by_quality = alignment.get("event_counts_by_quality", {})
            if isinstance(counts_by_quality, dict):
                lines.extend(["", "| Quality | Trace events |", "| --- | ---: |"])
                for quality in ("fixed", "float", "dgps", "single", "invalid"):
                    counts = counts_by_quality.get(quality, {})
                    total = sum(int(value) for value in counts.values()) if isinstance(counts, dict) else 0
                    lines.append(f"| {quality} | {total} |")
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
    lines.extend(["", "## 15. Top Warnings And Interpretation", ""])
    warnings = data.get("warnings", [])
    if warnings:
        lines.extend(f"- WARNING: {warning}" for warning in warnings)  # type: ignore[union-attr]
    else:
        lines.append("- No high-level quality warnings generated.")
    lines.extend(
        [
            "",
            "## 16. Suggested Next Actions",
            "",
            "- Optimise on QC-supported fixed time and QC-supported fixed distance, not raw fixed percentage alone.",
            "- Inspect missing/no-output time before comparing configurations.",
            "- Use STAT residual/slip evidence when available before treating short fixed islands as reliable.",
        ]
    )
    if include_raw_json:
        lines.extend(["", "## 17. Raw JSON Appendix", "", "```json", json.dumps(data, indent=2, sort_keys=True, default=str), "```"])
    return "\n".join(lines) + "\n"


def _quality_stat_scope(value: object, quality: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    item = value.get(quality, {})
    return item if isinstance(item, dict) else {}


def _fixed_segment_markdown_row(item: dict[str, object]) -> str:
    route = f"{_fmt_any(item.get('route_start_km'))}-{_fmt_any(item.get('route_end_km'))}"
    baseline = f"{_fmt_any(item.get('baseline_start_km'))}-{_fmt_any(item.get('baseline_end_km'))}"
    reasons = item.get("main_local_reasons", [])
    reason_text = ", ".join(str(reason) for reason in reasons) if isinstance(reasons, list) else str(reasons)
    return (
        f"| {_fmt_any(item.get('rank'))} | {item.get('start_time')} | {item.get('end_time')} | "
        f"{_fmt_any(item.get('duration_s'))} | {_fmt_any(item.get('distance_km'))} | "
        f"{route} | {baseline} | {_fmt_any(item.get('median_speed_mps'))} | "
        f"{_fmt_any(item.get('max_step_m'))} | {item.get('usable_class', item.get('qc_class'))} | {reason_text or 'n/a'} |"
    )


def _quality_bin_markdown_row(item: dict[str, object], *, label: str) -> str:
    elapsed = float(item.get("elapsed_time_s", 0.0) or 0.0)
    emitted = float(item.get("emitted_time_s", 0.0) or 0.0)
    missing = float(item.get("missing_time_s", 0.0) or 0.0)
    fixed = float(item.get("fixed_time_s", 0.0) or 0.0)
    track_km = float(item.get("track_distance_km", 0.0) or 0.0)
    trace = (
        f"{_fmt_any(item.get('trace_low_ar_count'))} / "
        f"{_fmt_any(item.get('trace_slip_count'))} / "
        f"{_fmt_any(item.get('trace_residual_outlier_count'))}"
    )
    return (
        f"| {label} | {_fmt_any(item.get('epoch_count'))} | {_fmt_any(track_km)} | "
        f"{_fmt_any(fixed)} / {_fmt_pct(fixed, elapsed)} | "
        f"{_fmt_any(item.get('fixed_time_ge_60s'))} | {_fmt_any(item.get('fixed_distance_ge_1000m'))} | "
        f"{_fmt_any(item.get('longest_fixed_segment_s'))} / {_fmt_any(item.get('longest_fixed_segment_km'))} | "
        f"{_fmt_any(item.get('fixed_segment_count'))} | {trace} |"
    )


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
        "stat_recent_slip": "deduplicated recent STAT slip evidence",
        "stat_high_residual": "time-aligned STAT residual outlier",
        "stat_rejection_burst": "time-aligned STAT rejection burst",
        "transition_jump": "speed-normalised motion anomaly",
        "incomplete_diagnostics": "confidence limited by incomplete diagnostics",
        "trace_low_ar_ratio": "time-aligned low trace AR ratio",
        "trace_ambiguity_validation_failed": "time-aligned trace ambiguity validation failure",
        "trace_recent_slip": "time-aligned trace slip evidence",
        "trace_residual_outlier": "time-aligned trace residual outlier",
        "trace_rejection_burst": "time-aligned trace rejection burst",
        "trace_base_rover_dt": "time-aligned trace base/rover time issue",
        "trace_warning_or_error": "time-aligned trace warning/error",
    }
    return mapping.get(reason, "diagnostic context")


def _reason_status(reason: str, data: dict[str, object]) -> str:
    if reason in {"recent_slip", "stat_recent_slip"}:
        slips = data.get("slips", {})
        return "aligned" if isinstance(slips, dict) and slips.get("epochs_with_slip_pct") is not None else "not time-aligned"
    if reason in {"high_residual", "stat_high_residual"}:
        residuals = data.get("residuals", {})
        return "aligned" if isinstance(residuals, dict) and residuals.get("quality_aligned") else "global only"
    if reason.startswith("trace_"):
        trace = data.get("trace", {})
        alignment = trace.get("alignment", {}) if isinstance(trace, dict) else {}
        return "aligned" if isinstance(alignment, dict) and alignment.get("available") else "global only"
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
        "unique_slip_epochs": None,
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


def _has_recent_slip(slip_times_s: list[float], segment: Segment, window_s: float) -> bool:
    start = _timestamp_s(segment.start_time) - window_s
    end = _timestamp_s(segment.end_time)
    position = bisect_left(slip_times_s, start)
    return position < len(slip_times_s) and slip_times_s[position] <= end


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
    return {key: min(100.0, max(0.0, 100.0 * value / denominator)) if denominator else 0.0 for key, value in values.items()}


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


def _ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert WGS84 ECEF XYZ meters to latitude, longitude, height."""

    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    semi_minor = semi_major * (1.0 - flattening)
    eccentricity2 = 1.0 - (semi_minor * semi_minor) / (semi_major * semi_major)
    ep2 = (semi_major * semi_major - semi_minor * semi_minor) / (semi_minor * semi_minor)
    p = math.hypot(x, y)
    theta = math.atan2(z * semi_major, p * semi_minor)
    lon = math.atan2(y, x)
    lat = math.atan2(
        z + ep2 * semi_minor * math.sin(theta) ** 3,
        p - eccentricity2 * semi_major * math.cos(theta) ** 3,
    )
    sin_lat = math.sin(lat)
    n = semi_major / math.sqrt(1.0 - eccentricity2 * sin_lat * sin_lat)
    height = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), height


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
    return min(100.0, max(0.0, 100.0 * value / denominator)) if denominator else 0.0
