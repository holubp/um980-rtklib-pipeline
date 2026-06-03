"""Command line interface for um980-ppk."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from glob import glob
from pathlib import Path

from .badsat import BadSatConfig, choose_bad_sats, compute_sat_metrics, parse_rtklib_stat
from .badsat_report import write_badsat_json_report, write_badsat_markdown_report
from .base_rt import convert_rtcm_to_rinex, fetch_ntrip_sourcetable, record_ntrip_base
from .base_advisory import CURATED_STATION_POSITIONS, build_base_advisory_report, format_base_advisory
from .bestnav import (
    bestnav_records_to_nmea,
    extract_bestnav_records,
    filter_bestnav_records,
    parse_bestnav_rate,
    parse_bestnav_sentences,
)
from .config import deep_get, load_config
from .diagnostics import extract_diagnostics
from .euref import (
    BasePosition,
    download_urls,
    fetch_epn_station_position,
    filter_urls_by_remote_listing,
    normalise_rinex_file,
    parse_rinex_approx_position,
    planned_urls,
    requires_crx2rnx,
    resolve_station,
)
from .files import (
    RinexObsCapabilities,
    basename_for,
    classify_rinex_file,
    detect_rinex_obs_systems,
    ensure_out_dir,
    filter_rinex_obs_by_overlap,
    read_rinex_obs_capabilities,
    read_rinex_obs_time_span,
)
from .initgen import (
    InitProfile,
    ION_MESSAGES,
    NMEA_PRESETS,
    SBAS_MODES,
    UTC_MESSAGES,
    debug_ascii_ephemeris_policy,
    ephemeris_policy,
    parse_nmea_overrides,
    render_init_script,
    write_json_report,
)
from .logging_config import configure_logging
from .message_stats import build_message_stats, log_message_stats
from .nav_resolver import resolve_nav_sources
from .obs_decode import decode_observations, write_observations_csv
from .optimizer import (
    build_optimizer_plan,
    execute_optimizer_plan,
    format_optimizer_plan,
    load_bases_from_candidates,
    load_base_list,
    parse_duration_seconds,
)
from .quality import (
    QualityThresholds,
    analyze_rtk_quality,
    build_analysis,
    compare_quality_reports,
    format_quality_comparison_markdown,
    format_quality_markdown,
    format_quality_text,
    write_analysis_json,
    write_quality_json,
    write_quality_segments_jsonl,
)
from .rinex_nav import extract_rover_nav, rover_nav_files
from .rinex_obs import observations_for_rinex, write_rinex_obs
from .rtklib import (
    executable_exists,
    executable_for_subprocess,
    format_command,
    is_windows_path,
    resolve_rtklib_tool,
    run_rnx2rtkp,
)
from .rtklib_config_patch import patch_config_with_autoqc
from .rtklib_summary import format_rtklib_solution_summary, summarize_rtklib_solution
from .solution import (
    SolutionExtraction,
    SolutionPoint,
    bestnav_records_to_solution_extraction,
    extract_solutions,
    position_nmea_records,
    write_all_records_csv,
    write_gpx,
    write_lines,
    write_solution_csv,
    write_solution_nmea,
)
from .stations import default_station_catalog_cache, load_station_catalog
from .stream import parse_stream
from .trace_quality import analyze_rtklib_trace
from .time_window import ProcessingWindow, processing_window_from_values
from .timeutil import gps_week_tow_to_datetime

BASE_PROVIDER_CHOICES = ("bev-nrt", "bkg-euref-nrt", "bkg-euref-highrate", "bkg-igs-highrate")
BASE_RATE_HIGH = "1s"
BASE_RATE_LOW = "30s"
HIGH_RATE_ARCHIVE_MARGIN_S = 300
DUPLICATE_SINGLE_VALUE_OPTIONS = {
    "--nav-source",
    "--nav-merge",
    "--crx2rnx",
    "--quality-trace",
    "--rtklib-trace-level",
    "--rtkconf",
    "--output-format",
    "--base-resolution",
    "--base-rinex-version",
}
PIPELINE_STEPS = (
    "parse_rover",
    "extract_receiver_products",
    "write_rinex_obs",
    "extract_rover_nav",
    "resolve_base",
    "run_rtklib",
    "quality",
    "cleanup",
)
RTK_POS_MODE_CODES = {
    "single": "0",
    "dgps": "1",
    "kinematic": "2",
    "static": "3",
    "moving-base": "4",
    "fixed": "5",
    "ppp-kinematic": "6",
    "ppp-static": "7",
    "ppp-fixed": "8",
}
RTK_FREQUENCY_CODES = {
    "l1": "1",
    "l1+l2": "2",
    "l1+l2+l5": "3",
    "l1+l5": "4",
}
RTK_NAVSYS_CODES = {
    "gps": "G",
    "glo": "R",
    "gal": "E",
    "bds": "C",
    "qzs": "J",
    "sbs": "S",
    "irn": "I",
}
RTK_NAVSYS_PRESETS = {
    "gps": "G",
    "gps-glo": "G,R",
    "gps-glo-gal-bds": "G,R,E,C",
    "all": "G,R,E,C,J",
}


@dataclass(frozen=True)
class BaseArchiveCandidateGroup:
    """Planned archive candidates for one base-observation provider group."""

    source_kind: str
    resolution: str
    rinex_version: str
    provider: str
    nominal_rate: str
    is_fallback: bool = False


def _csv_items(value: object) -> list[str]:
    """Return lowercase comma-separated items from CLI/config values."""

    if value is None or value is False:
        return []
    if value is True:
        return ["all"]
    if isinstance(value, str):
        return [item.strip().lower() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            items.extend(_csv_items(item))
        return items
    return [str(value).strip().lower()]


def _parse_float_csv(value: object | None) -> list[float]:
    """Parse comma-separated floats from a CLI value."""

    return [float(item) for item in _csv_items(value)]


def _scan_duplicate_options(argv: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Find repeated single-value options before argparse collapses them."""

    seen: dict[str, list[tuple[int, str]]] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        option, inline_value = (token.split("=", 1) if "=" in token else (token, None))
        if option in DUPLICATE_SINGLE_VALUE_OPTIONS:
            option_index = index
            if inline_value is not None:
                value = inline_value
            elif index + 1 < len(argv):
                value = argv[index + 1]
                index += 1
            else:
                value = "<missing>"
            seen.setdefault(option, []).append((option_index, value))
        index += 1
    return {option: values for option, values in seen.items() if len(values) > 1}


def _emit_duplicate_option_warnings(args: argparse.Namespace) -> None:
    """Warn about duplicate single-value CLI options when verbose/debug is on."""

    duplicates = getattr(args, "_duplicate_options", {})
    if getattr(args, "_duplicate_options_reported", False):
        return
    if not duplicates or not (getattr(args, "verbose", False) or getattr(args, "debug", False)):
        return
    setattr(args, "_duplicate_options_reported", True)
    for option, values in sorted(duplicates.items()):
        last_value = values[-1][1]
        previous = ", ".join(value for _, value in values[:-1])
        if getattr(args, "debug", False):
            positions = ", ".join(f"{position}:{value}" for position, value in values)
            logging.warning(
                "option %s specified multiple times; using last value: %s; previous values: %s; positions: %s",
                option,
                last_value,
                previous,
                positions,
            )
        else:
            logging.warning(
                "option %s specified multiple times; using last value: %s; previous values: %s",
                option,
                last_value,
                previous,
            )


def _effective_trace_level_for_summary(args: argparse.Namespace) -> str:
    mode = getattr(args, "quality_trace", "off")
    if mode in {"temporary", "keep"}:
        value = getattr(args, "rtklib_trace_level", None)
        return str(3 if value is None else value)
    return "none"


def _log_effective_run_summary(args: argparse.Namespace) -> None:
    """Log one compact effective run configuration block."""

    if not logging.getLogger().isEnabledFor(logging.INFO):
        return
    logging.info("effective run configuration:")
    for key, value in (
        ("station", getattr(args, "station", None)),
        ("base_resolution", getattr(args, "base_resolution", None)),
        ("base_rinex_version", getattr(args, "base_rinex_version", None)),
        ("nav_source", getattr(args, "nav_source", None)),
        ("nav_merge", getattr(args, "nav_merge", None)),
        ("rtkconf", getattr(args, "rtkconf", None)),
        ("output_format", ",".join(_rtklib_output_formats(args)) if hasattr(args, "output_format") else None),
        ("quality_trace", getattr(args, "quality_trace", "off")),
        ("effective_trace_level", _effective_trace_level_for_summary(args)),
        ("stat_cleanup", bool(getattr(args, "quality_clean_stat", False))),
    ):
        if value is not None:
            logging.info("  %s=%s", key, value)


def _apply_solution_hz(nmea: dict[str, float], hz: object | None) -> None:
    """Set the primary NMEA solution sentence rate when requested."""

    if hz is None:
        return
    value = float(hz)
    if value <= 0:
        raise ValueError("NMEA solution frequency must be greater than zero")
    nmea["GNGGA"] = value
    nmea["GNRMC"] = value


def _resolve_ion_messages(args: argparse.Namespace, diag_cfg: dict[str, object]) -> tuple[str, ...]:
    """Resolve requested ionosphere command families from CLI and config."""

    requested: list[str] = []
    requested.extend(_csv_items(diag_cfg.get("ion")))
    if diag_cfg.get("gpsion", False):
        requested.append("gps")
    if args.include_gpsion:
        requested.append("gps")
    if args.include_ion:
        requested.append("all")
    requested.extend(_csv_items(args.ion))
    if "all" in requested:
        requested.extend(ION_MESSAGES)
    unique = tuple(item for item in dict.fromkeys(requested) if item != "all")
    invalid = sorted(set(unique) - set(ION_MESSAGES))
    if invalid:
        raise ValueError(f"unsupported --ion values: {', '.join(invalid)}")
    return unique


def _resolve_utc_messages(args: argparse.Namespace, diag_cfg: dict[str, object]) -> tuple[str, ...]:
    """Resolve requested UTC/time-system command families from CLI and config."""

    requested: list[str] = []
    requested.extend(_csv_items(diag_cfg.get("utc")))
    if getattr(args, "include_utc", False):
        requested.append("all")
    requested.extend(_csv_items(getattr(args, "utc", None)))
    if "all" in requested:
        requested.extend(UTC_MESSAGES)
    unique = tuple(item for item in dict.fromkeys(requested) if item != "all")
    invalid = sorted(set(unique) - set(UTC_MESSAGES))
    if invalid:
        raise ValueError(f"unsupported --utc values: {', '.join(invalid)}")
    return unique


def _add_bestnav_nmea_args(parser: argparse.ArgumentParser) -> None:
    """Add BESTNAV-derived NMEA product arguments."""

    parser.add_argument(
        "--bestnav-nmea",
        help="Write generated GGA/RMC/VTG NMEA from decoded BESTNAV receiver-solution records.",
    )
    parser.add_argument(
        "--bestnav-nmea-sentences",
        default="GGA,RMC,VTG",
        help="Comma-separated generated BESTNAV NMEA sentences. Supported: GGA,RMC,VTG.",
    )
    parser.add_argument(
        "--bestnav-nmea-rate",
        default="native",
        help="native or a positive Hz value. Numeric values decimate by timestamp without interpolation.",
    )
    parser.add_argument(
        "--bestnav-nmea-source",
        choices=["auto", "ascii", "binary"],
        default="auto",
        help="BESTNAV source for generated NMEA. auto uses every decoded source.",
    )
    parser.add_argument(
        "--bestnav-nmea-talk-id",
        choices=["GN", "GP"],
        default="GN",
        help="Talker ID for generated BESTNAV NMEA sentences.",
    )


def _add_track_source_arg(parser: argparse.ArgumentParser) -> None:
    """Add solution-track source selection."""

    parser.add_argument(
        "--track-source",
        choices=["auto", "nmea", "ppp", "adr", "gga", "bestnav", "bestnavb"],
        default="auto",
        help="Solution-track source. auto falls back to BESTNAV when live position NMEA is absent.",
    )


def _add_emit_ion_utc_arg(parser: argparse.ArgumentParser) -> None:
    """Add safe ION/UTC RINEX emission policy selection."""

    parser.add_argument(
        "--emit-ion-utc",
        choices=["off", "auto", "strict"],
        default="off",
        help=(
            "ION/UTC RINEX NAV header policy. off preserves diagnostics only; auto emits only mappings "
            "verified against RTKLIB, currently none; strict fails if ION/UTC records are present but "
            "cannot be emitted safely."
        ),
    )


def _add_sbas_source_args(parser: argparse.ArgumentParser) -> None:
    """Add RTKLIB SBAS correction sidecar source controls."""

    parser.add_argument(
        "--sbas-source",
        choices=["off", "rover", "base", "external", "auto"],
        default="auto",
        help=(
            "SBAS correction-message source for RTKLIB .sbs input. auto uses --sbas-file first, "
            "then a real rover-generated .sbs sidecar; off passes no .sbs file. No fake .sbs file is created."
        ),
    )
    parser.add_argument("--sbas-file", action="append", help="External RTKLIB .sbs correction-message file.")


def _add_time_window_args(parser: argparse.ArgumentParser) -> None:
    """Add selected-recording-interval options."""

    parser.add_argument(
        "--start-time",
        "--datetime-start",
        dest="start_time",
        help=(
            "Process only data at/after this ISO-8601 datetime. Naive values are treated as UTC. "
            "The same window is applied to all timestamped products. Initial stream parsing may "
            "read full input, but downstream products are windowed."
        ),
    )
    parser.add_argument(
        "--end-time",
        "--datetime-end",
        dest="end_time",
        help=(
            "Process only data at/before this ISO-8601 datetime. Naive values are treated as UTC. "
            "The same window is applied to all timestamped products. Initial stream parsing may "
            "read full input, but downstream products are windowed."
        ),
    )


def _add_step_control_args(parser: argparse.ArgumentParser) -> None:
    """Add lightweight composable-step controls shared by step commands."""

    existing = getattr(parser, "_option_string_actions", {})
    if "--manifest" not in existing:
        parser.add_argument("--manifest", help="Optional pipeline manifest path for composed reruns.")
    if "--skip-existing" not in existing:
        parser.add_argument("--skip-existing", action="store_true", help="Reuse existing outputs for this step when possible.")
    if "--force" not in existing:
        parser.add_argument("--force", action="store_true", help="Regenerate this step even if outputs already exist.")


def _add_quality_analyze_args(parser: argparse.ArgumentParser) -> None:
    """Add RTK quality-analysis threshold options."""

    parser.add_argument("--trusted-fixed-min-duration-s", type=float, default=10.0)
    parser.add_argument("--trusted-fixed-min-distance-m", type=float, default=20.0)
    parser.add_argument("--provisional-fixed-min-duration-s", type=float, default=3.0)
    parser.add_argument("--recent-slip-window-s", type=float, default=10.0)
    parser.add_argument("--transition-jump-warning-m", type=float, default=1.0)
    parser.add_argument("--transition-jump-severe-m", type=float, default=3.0)
    parser.add_argument("--vertical-jump-warning-m", type=float, default=1.5)
    parser.add_argument("--carrier-residual-warning-m", type=float, default=0.20)
    parser.add_argument("--carrier-residual-severe-m", type=float, default=0.50)
    parser.add_argument("--code-residual-warning-m", type=float, default=5.0)
    parser.add_argument("--code-residual-severe-m", type=float, default=10.0)
    parser.add_argument("--low-used-signals-warning", type=int, default=12)
    parser.add_argument("--low-snr-warning-dbHz", dest="low_snr_warning_dbhz", type=float, default=35.0)
    parser.add_argument("--gap-split-s", type=float, default=2.0)
    parser.add_argument("--stationary-speed-threshold-mps", type=float, default=0.3)
    parser.add_argument("--quality-motion-profile", choices=["auto", "static", "walking", "cycling", "vehicle", "highway"], default="auto")
    parser.add_argument("--quality-max-speed-mps", type=float)
    parser.add_argument("--quality-max-accel-mps2", type=float)
    parser.add_argument("--quality-transition-window-s", type=float, default=2.0)
    parser.add_argument("--quality-route-bin-km", type=float, default=10.0)
    parser.add_argument("--quality-no-route-bins", action="store_true")
    parser.add_argument("--quality-baseline-bins", default="0,10,20,30,40,50,75,100,150")
    parser.add_argument("--quality-trace-align-tolerance-s", type=float, default=0.5)
    parser.add_argument("--quality-md-raw-json", action="store_true")
    parser.add_argument("--quality-md-show-empty-baseline-bins", action="store_true")
    parser.add_argument("--quality-include-empty-bins", action="store_true", help="Include empty baseline/route bins in quality JSON.")
    parser.add_argument("--quality-include-all-segments", action="store_true", help="Include verbose fixed-segment arrays in quality JSON.")
    parser.add_argument("--quality-include-geometry-segments", action="store_true", help="Include verbose geometry segment diagnostics in quality JSON.")
    parser.add_argument("--quality-out-detail-json", help="Write verbose quality JSON with all optional diagnostic arrays.")
    parser.add_argument("--quality-out-segments-jsonl", help="Write fixed segment detail as JSON Lines.")
    parser.add_argument("--quality-trace-examples", type=int, default=20, help="Maximum stored RTKLIB trace examples per category.")
    parser.add_argument("--quality-stat-max-lines", type=int, default=0, help="Maximum RTKLIB .stat lines to parse; 0 is unlimited.")
    parser.add_argument("--quality-stat-max-seconds", type=float, default=0.0, help="Maximum wall-clock seconds to spend parsing .stat; 0 is unlimited.")
    parser.add_argument("--quality-fast", action="store_true", help="Skip expensive STAT detail parsing while keeping raw solution summaries.")


def _add_quality_trace_args(parser: argparse.ArgumentParser, *, standalone: bool = False) -> None:
    """Add optional RTKLIB trace and cleanup diagnostics arguments."""

    parser.add_argument("--quality-trace", choices=["off", "temporary", "keep", "existing"], default="off")
    parser.add_argument("--trace", help="Existing RTKLIB trace path for --quality-trace existing.")
    parser.add_argument("--rtklib-trace-file", help="Retained RTKLIB trace destination for --quality-trace keep.")
    if standalone:
        parser.add_argument("--rtklib-trace-level", type=int, choices=range(0, 6), metavar="0..5")
    parser.add_argument(
        "--quality-trace-max-bytes",
        "--rtklib-trace-max-bytes",
        dest="quality_trace_max_bytes",
        type=int,
        default=0,
        help="Maximum RTKLIB trace bytes to parse; 0 parses the full trace with streaming reads.",
    )
    parser.add_argument(
        "--rtklib-trace-cleanup",
        choices=["always", "on-success", "never"],
        default="always",
        help="Temporary trace cleanup policy.",
    )
    parser.add_argument(
        "--quality-clean-stat",
        "--quality-stat-cleanup",
        dest="quality_clean_stat",
        action="store_true",
        help=(
            "Delete generated RTKLIB .stat files after successful quality analysis. "
            "Standalone quality-analyze refuses this to avoid deleting archived .stat files."
            if standalone
            else "Delete generated RTKLIB .stat files after successful quality analysis."
        ),
    )


def _add_quality_pipeline_args(parser: argparse.ArgumentParser) -> None:
    """Add optional pipeline RTK quality-analysis outputs."""

    parser.add_argument("--quality-analyze", action="store_true", help="Analyse generated RTKLIB solution quality after post-processing.")
    parser.add_argument("--quality-out-md", help="Markdown RTK quality report path.")
    parser.add_argument("--quality-out-json", help="JSON RTK quality report path.")
    _add_quality_trace_args(parser)
    _add_quality_analyze_args(parser)


def _add_rerun_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--print-step-commands", action="store_true", help="Log copy-pasteable commands for major pipeline steps.")
    parser.add_argument("--emit-run-script", nargs="?", const="auto", help="Write rerun shell script; optional PATH or 'auto'.")
    parser.add_argument("--no-emit-run-script", action="store_true", help="Disable automatic rerun script emission.")
    parser.add_argument("--dry-run-plan", action="store_true", help="Print planned phases/commands without running RTKLIB.")
    parser.add_argument("--from-step", choices=PIPELINE_STEPS, help="Reuse earlier pipeline outputs and run from this step onward.")
    parser.add_argument("--only-step", choices=PIPELINE_STEPS, help="Run only one composable pipeline step.")
    parser.add_argument("--skip-existing", action="store_true", help="Reuse existing outputs for selected pipeline steps when present.")
    parser.add_argument("--force-step", choices=PIPELINE_STEPS, action="append", default=[], help="Regenerate this step even when --skip-existing is set.")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable verbose progress plus exact external command diagnostics.",
    )
    parser.add_argument("--out-dir")
    parser.add_argument("--basename")
    parser.add_argument("--analysis-json", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file")


def _debug_enabled(args: argparse.Namespace) -> bool:
    """Return true when debug diagnostics were requested."""

    return bool(getattr(args, "debug", False))


def _quality_thresholds_from_args(args: argparse.Namespace) -> QualityThresholds:
    """Build RTK quality-analysis thresholds from CLI arguments."""

    return QualityThresholds(
        trusted_fixed_min_duration_s=getattr(args, "trusted_fixed_min_duration_s", 10.0),
        trusted_fixed_min_distance_m=getattr(args, "trusted_fixed_min_distance_m", 20.0),
        provisional_fixed_min_duration_s=getattr(args, "provisional_fixed_min_duration_s", 3.0),
        recent_slip_window_s=getattr(args, "recent_slip_window_s", 10.0),
        transition_jump_warning_m=getattr(args, "transition_jump_warning_m", 1.0),
        transition_jump_severe_m=getattr(args, "transition_jump_severe_m", 3.0),
        vertical_jump_warning_m=getattr(args, "vertical_jump_warning_m", 1.5),
        carrier_residual_warning_m=getattr(args, "carrier_residual_warning_m", 0.20),
        carrier_residual_severe_m=getattr(args, "carrier_residual_severe_m", 0.50),
        code_residual_warning_m=getattr(args, "code_residual_warning_m", 5.0),
        code_residual_severe_m=getattr(args, "code_residual_severe_m", 10.0),
        low_used_signals_warning=getattr(args, "low_used_signals_warning", 12),
        low_snr_warning_dbhz=getattr(args, "low_snr_warning_dbhz", 35.0),
        gap_split_s=getattr(args, "gap_split_s", 2.0),
        stationary_speed_threshold_mps=getattr(args, "stationary_speed_threshold_mps", 0.3),
        motion_profile=getattr(args, "quality_motion_profile", "auto"),
        max_speed_mps=getattr(args, "quality_max_speed_mps", None),
        max_accel_mps2=getattr(args, "quality_max_accel_mps2", None),
        transition_window_s=getattr(args, "quality_transition_window_s", 2.0),
        route_bin_km=None if getattr(args, "quality_no_route_bins", False) else getattr(args, "quality_route_bin_km", 10.0),
        baseline_bins=_parse_float_csv(getattr(args, "quality_baseline_bins", "0,10,20,30,40,50,75,100,150")),
        trace_align_tolerance_s=getattr(args, "quality_trace_align_tolerance_s", 0.5),
    )


def _format_quality_markdown_from_args(args: argparse.Namespace, analysis) -> str:
    """Render quality Markdown with CLI-controlled optional sections."""

    return format_quality_markdown(
        analysis,
        include_raw_json=bool(getattr(args, "quality_md_raw_json", False)),
        show_empty_baseline_bins=bool(getattr(args, "quality_md_show_empty_baseline_bins", False)),
    )


def _write_quality_outputs(args: argparse.Namespace, analysis, json_path: Path | None = None) -> None:
    """Write compact and optional detailed quality outputs requested by CLI args."""

    if json_path is not None:
        write_quality_json(
            json_path,
            analysis,
            include_all_segments=bool(getattr(args, "quality_include_all_segments", False)),
            include_geometry_segments=bool(getattr(args, "quality_include_geometry_segments", False)),
            include_empty_bins=bool(getattr(args, "quality_include_empty_bins", False)),
        )
    detail_path = getattr(args, "quality_out_detail_json", None)
    if detail_path:
        write_quality_json(
            Path(detail_path),
            analysis,
            include_all_segments=True,
            include_geometry_segments=True,
            include_empty_bins=True,
        )
    segments_path = getattr(args, "quality_out_segments_jsonl", None)
    if segments_path:
        write_quality_segments_jsonl(Path(segments_path), analysis)


def _format_quality_comparison_text(comparison: dict[str, object]) -> str:
    lines = ["Quality comparison:"]
    deltas = comparison.get("deltas", {})
    if isinstance(deltas, dict):
        for key in ("raw_fixed_time_s", "long_fixed_time_ge_60s", "track_consistency_score", "fixed_internal_jump_count", "rejection_count", "raw_slip_flags"):
            lines.append(f"  {key}: delta={deltas.get(key)}")
    for warning in comparison.get("warnings", []):  # type: ignore[union-attr]
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)


def _verbose_enabled(args: argparse.Namespace) -> bool:
    """Return true when progress logging should be enabled."""

    return bool(getattr(args, "verbose", False) or _debug_enabled(args))


def _configure_cli_logging(args: argparse.Namespace) -> None:
    """Configure CLI logging from common arguments."""

    configure_logging(_verbose_enabled(args), args.log_file, debug=_debug_enabled(args))
    _emit_duplicate_option_warnings(args)


def _processing_window_from_args(args: argparse.Namespace) -> ProcessingWindow:
    """Return the selected UTC processing window from CLI args."""

    return processing_window_from_values(getattr(args, "start_time", None), getattr(args, "end_time", None))


def _span_from_solution_points(points: list[SolutionPoint]) -> dict[str, str | None]:
    if not points:
        return {"start": None, "end": None}
    return {"start": min(point.time_utc for point in points).isoformat(), "end": max(point.time_utc for point in points).isoformat()}


def _span_from_observations(observations) -> dict[str, str | None]:
    if not observations:
        return {"start": None, "end": None}
    times = [gps_week_tow_to_datetime(obs.gps_week, obs.tow) for obs in observations]
    return {"start": min(times).isoformat(), "end": max(times).isoformat()}


def _row_in_window(row: dict[str, object], window: ProcessingWindow) -> bool:
    value = row.get("time_utc")
    if not isinstance(value, str):
        return True
    try:
        parsed = processing_window_from_values(value, None).start
    except ValueError:
        return True
    return bool(parsed is None or window.contains(parsed))


def _filter_solution_extraction(solutions: SolutionExtraction, window: ProcessingWindow) -> SolutionExtraction:
    """Return solution extraction restricted to the selected processing window."""

    if not window.enabled:
        return solutions
    kept_indices = [index for index, point in enumerate(solutions.solution_points) if window.contains(point.time_utc)]
    points = [solutions.solution_points[index] for index in kept_indices]
    rows = [row for row in solutions.all_rows if _row_in_window(row, window)]
    solution_nmea = _filter_parallel_solution_lines(solutions.solution_nmea, len(solutions.solution_points), kept_indices)
    solution_records = _filter_parallel_solution_lines(solutions.solution_records, len(solutions.solution_points), kept_indices)
    warnings = list(solutions.warnings)
    warnings.append(
        f"solution output filtered by selected processing window: kept {len(points)} of "
        f"{len(solutions.solution_points)} points"
    )
    return SolutionExtraction(
        all_nmea=solutions.all_nmea,
        solution_records=solution_records,
        solution_points=points,
        all_rows=rows,
        nmea_cadence=solutions.nmea_cadence,
        warnings=warnings,
        solution_nmea=solution_nmea,
    )


def _filter_parallel_solution_lines(lines: list[str], point_count: int, kept_indices: list[int]) -> list[str]:
    """Filter solution line groups that are one-or-three records per point."""

    if not lines or point_count <= 0:
        return []
    if len(lines) == point_count:
        return [lines[index] for index in kept_indices]
    if len(lines) == point_count * 3:
        filtered: list[str] = []
        for index in kept_indices:
            filtered.extend(lines[index * 3 : index * 3 + 3])
        return filtered
    return lines


def _filter_observation_extraction(observations, window: ProcessingWindow):
    """Return observation extraction restricted to the selected processing window."""

    if not window.enabled:
        return observations
    kept = [
        obs
        for obs in observations.observations
        if window.contains(gps_week_tow_to_datetime(obs.gps_week, obs.tow))
    ]
    from .obs_decode import ObservationExtraction, observation_metrics

    metrics = observation_metrics(kept)
    metrics["time_unknown_reasons"] = dict(observations.time_unknown_reasons)
    warnings = list(observations.warnings)
    warnings.append(
        f"raw observations filtered by selected processing window: kept {len(kept)} of "
        f"{len(observations.observations)} observations"
    )
    return ObservationExtraction(
        observations=kept,
        unsupported_records=observations.unsupported_records,
        metrics=metrics,
        warnings=warnings,
        time_unknown_reasons=observations.time_unknown_reasons,
        skipped_records=observations.skipped_records,
    )


def _human_bytes(size: int) -> str:
    """Return a compact human-readable byte count."""

    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def _performance(args: argparse.Namespace) -> dict[str, object]:
    perf = getattr(args, "_performance", None)
    if not isinstance(perf, dict):
        perf = {"phases": {}}
        setattr(args, "_performance", perf)
    return perf


class _PhaseTimer:
    def __init__(self, args: argparse.Namespace, name: str, **counts: object) -> None:
        self.args = args
        self.name = name
        self.counts = counts
        self.started = 0.0

    def __enter__(self) -> "_PhaseTimer":
        self.started = time.perf_counter()
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug("phase start: %s", self.name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed = time.perf_counter() - self.started
        perf = _performance(self.args)
        phases = perf.setdefault("phases", {})
        if isinstance(phases, dict):
            phases[self.name] = {"elapsed_s": elapsed, **self.counts}
        if logging.getLogger().isEnabledFor(logging.INFO):
            logging.info("phase %s elapsed=%.3fs", self.name, elapsed)


def _time_phase(args: argparse.Namespace, name: str, **counts: object) -> _PhaseTimer:
    return _PhaseTimer(args, name, **counts)


def _quote_command(items: list[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in items)


def _rerun_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_emit_run_script", False):
        return False
    requested = getattr(args, "emit_run_script", None)
    if requested:
        return True
    return bool(getattr(args, "verbose", False) or getattr(args, "debug", False))


def _init_rerun_artifacts(args: argparse.Namespace, out_dir: Path, basename: str) -> None:
    """Create rerun script/Markdown files when requested."""

    if not _rerun_enabled(args):
        return
    requested = getattr(args, "emit_run_script", None)
    script = out_dir / f"{basename}.rerun.sh" if requested in {None, "auto"} else Path(requested)
    commands_md = out_dir / f"{basename}.commands.md"
    setattr(args, "_rerun_script", script)
    setattr(args, "_commands_md", commands_md)
    original = getattr(args, "_original_argv", None)
    script.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Working directory: {Path.cwd()}",
        'REQUEST_MODE="${1:-all}"',
        'REQUEST_STEP="${2:-}"',
        "STARTED_FROM=0",
        "run_step() {",
        '  step="$1"',
        "  shift",
        '  case "$REQUEST_MODE" in',
        '    all) "$@" ;;',
        '    quality) [ "$step" = "quality" ] && "$@" || true ;;',
        '    only) [ "$step" = "$REQUEST_STEP" ] && "$@" || true ;;',
        '    from) if [ "$step" = "$REQUEST_STEP" ]; then STARTED_FROM=1; fi; [ "$STARTED_FROM" = 1 ] && "$@" || true ;;',
        '    *) echo "usage: $0 [all|quality|only STEP|from STEP]" >&2; exit 2 ;;',
        "  esac",
        "}",
    ]
    if original:
        header.append("# Original command:")
        header.append(_quote_command(["PYTHONPATH=src", "python", "-m", "um980_rtklib_pipeline.cli", *original]))
    script.write_text("\n".join(header) + "\n\n", encoding="utf-8")
    commands_md.write_text("# Reproducible Run Commands\n\n", encoding="utf-8")
    logging.info("rerun script: %s", script)
    logging.info("commands markdown: %s", commands_md)


def _append_rerun_command(args: argparse.Namespace, title: str, command: list[str] | str) -> None:
    """Append a copy-pasteable command to rerun artifacts."""

    script = getattr(args, "_rerun_script", None)
    commands_md = getattr(args, "_commands_md", None)
    rendered = command if isinstance(command, str) else _quote_command([str(item) for item in command])
    if getattr(args, "print_step_commands", False) or getattr(args, "verbose", False):
        logging.info("%s: %s", title, rendered)
    if script:
        step = _rerun_step_name(title)
        Path(script).write_text(
            Path(script).read_text(encoding="utf-8") + f"# {title}\nrun_step {shlex.quote(step)} sh -c {shlex.quote(rendered)}\n\n",
            encoding="utf-8",
        )
    if commands_md:
        Path(commands_md).write_text(
            Path(commands_md).read_text(encoding="utf-8") + f"## {title}\n\n```bash\n{rendered}\n```\n\n",
            encoding="utf-8",
        )


def _rerun_step_name(title: str) -> str:
    lowered = title.lower()
    if "quality" in lowered:
        return "quality"
    if "rtklib" in lowered:
        return "run_rtklib"
    if "rinex" in lowered:
        return "write_rinex_obs"
    if "nav" in lowered:
        return "extract_rover_nav"
    if "base" in lowered:
        return "resolve_base"
    if "extract" in lowered:
        return "extract_receiver_products"
    return "parse_rover"


def _init_pipeline_manifest(args: argparse.Namespace, out_dir: Path, basename: str) -> None:
    """Initialise a lightweight pipeline step manifest."""

    path = out_dir / f"{basename}.pipeline-manifest.json"
    window = _processing_window_from_args(args)
    manifest = {
        "basename": basename,
        "cwd": str(Path.cwd()),
        "inputs": {
            "rover_log": str(getattr(args, "rover_log", "")),
            "start_time": window.start.isoformat() if window.start else None,
            "end_time": window.end.isoformat() if window.end else None,
        },
        "original_command": _quote_command(["PYTHONPATH=src", "python", "-m", "um980_rtklib_pipeline.cli", *getattr(args, "_original_argv", [])]),
        "effective_processing_window": window.to_json(),
        "processing_window": window.to_json(),
        "steps": [],
    }
    setattr(args, "_pipeline_manifest_path", path)
    setattr(args, "_pipeline_manifest", manifest)
    _write_pipeline_manifest(args)


def _write_pipeline_manifest(args: argparse.Namespace) -> None:
    path = getattr(args, "_pipeline_manifest_path", None)
    manifest = getattr(args, "_pipeline_manifest", None)
    if path is None or not isinstance(manifest, dict):
        return
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _record_pipeline_step(
    args: argparse.Namespace,
    name: str,
    *,
    inputs: list[Path | str] | None = None,
    outputs: list[Path | str] | None = None,
    command: list[str] | str | None = None,
    dependencies: list[str] | None = None,
    status: str = "planned",
    elapsed_s: float | None = None,
    reused: bool = False,
) -> None:
    manifest = getattr(args, "_pipeline_manifest", None)
    if not isinstance(manifest, dict):
        return
    steps = manifest.setdefault("steps", [])
    if not isinstance(steps, list):
        return
    window = _processing_window_from_args(args)
    entry = {
        "name": name,
        "inputs": [str(item) for item in inputs or []],
        "outputs": [str(item) for item in outputs or []],
        "command": _quote_command(command) if isinstance(command, list) else command,
        "dependencies": dependencies or [],
        "processing_window": window.to_json(),
        "status": status,
        "elapsed_s": elapsed_s,
        "reused": reused,
        "can_skip_if_outputs_exist": True,
        "cache_key": _pipeline_step_cache_key(args, name, inputs or []),
    }
    for index, existing in enumerate(steps):
        if isinstance(existing, dict) and existing.get("name") == name and existing.get("status") == "planned":
            merged = {**existing, **{key: value for key, value in entry.items() if value not in (None, [], "")}}
            merged["status"] = status
            merged["reused"] = reused
            steps[index] = merged
            break
    else:
        steps.append(entry)
    _write_pipeline_manifest(args)


def _pipeline_step_cache_key(args: argparse.Namespace, step: str, inputs: list[Path | str]) -> dict[str, object]:
    """Return a small reproducibility cache key for a pipeline step."""

    input_meta = []
    for item in inputs:
        path = Path(item)
        if path.exists():
            stat = path.stat()
            input_meta.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        else:
            input_meta.append({"path": str(path), "missing": True})
    return {
        "step": step,
        "inputs": input_meta,
        "processing_window": _processing_window_from_args(args).to_json(),
    }


def _step_order_index(step: str) -> int:
    try:
        return PIPELINE_STEPS.index(step)
    except ValueError:
        return len(PIPELINE_STEPS)


def _should_run_pipeline_step(args: argparse.Namespace, step: str) -> bool:
    only = getattr(args, "only_step", None)
    if only:
        return step == only
    start = getattr(args, "from_step", None)
    if start and _step_order_index(step) < _step_order_index(start):
        return False
    return True


def _forced_step(args: argparse.Namespace, step: str) -> bool:
    return step in set(getattr(args, "force_step", []) or [])


def _existing_outputs(paths: list[Path]) -> bool:
    return bool(paths) and all(path.exists() and path.stat().st_size > 0 for path in paths)


def _load_records(path: Path):
    size = path.stat().st_size
    logging.info("reading rover log: %s (%s)", path, _human_bytes(size))
    data = path.read_bytes()
    logging.info("parsing rover byte stream")
    records, diagnostics = parse_stream(data, progress=logging.getLogger().isEnabledFor(logging.INFO))
    logging.info(
        "parsed rover log: records=%d nmea=%d unicore_ascii=%d unicore_binary=%d noise=%s",
        len(records),
        diagnostics.valid_nmea_records,
        diagnostics.unicore_ascii_records,
        diagnostics.unicore_binary_records,
        _human_bytes(diagnostics.noise_bytes),
    )
    return records, diagnostics


def _extract_bundle(args: argparse.Namespace):
    cached = getattr(args, "_extract_bundle_cache", None)
    if cached is not None:
        logging.info("reusing parsed rover products from current pipeline run")
        return cached
    rover = Path(args.rover_log)
    progress = logging.getLogger().isEnabledFor(logging.INFO)
    window = _processing_window_from_args(args)
    if window.enabled:
        logging.info(
            "selected processing window: start=%s end=%s",
            window.start.isoformat() if window.start else "recording-start",
            window.end.isoformat() if window.end else "recording-end",
        )
    with _time_phase(args, "rover_parse", input_bytes=rover.stat().st_size if rover.exists() else None):
        records, stream_diag = _load_records(rover)
    logging.info("extracting solution records")
    with _time_phase(args, "extract_solutions"):
        solutions = extract_solutions(records, progress=progress)
    logging.info(
        "extracted solutions: points=%d nmea_records=%d all_nmea=%d",
        len(solutions.solution_points),
        len(solutions.solution_records),
        len(solutions.all_nmea),
    )
    logging.info("decoding raw observations")
    with _time_phase(args, "decode_observations"):
        observations = decode_observations(records, progress=progress)
    logging.info(
        "decoded raw observations: observations=%d epochs=%s unsupported_observation_records=%d skipped_non_observation_records=%d",
        len(observations.observations),
        observations.metrics.get("epochs", 0),
        sum(observations.unsupported_records.values()),
        sum(observations.skipped_records.values()),
    )
    logging.info("scanning rover navigation records")
    with _time_phase(args, "scan_rover_navigation"):
        rover_nav = extract_rover_nav(records)
    converted_nav = sum(rover_nav.converted.values())
    logging.info("scanned rover navigation: converted=%d warnings=%d", converted_nav, len(rover_nav.warnings))
    logging.info("scanning BESTNAV receiver-solution records")
    with _time_phase(args, "scan_bestnav"):
        bestnav = extract_bestnav_records(records)
    logging.info(
        "decoded BESTNAV records: present=%d valid_epochs=%d malformed=%d",
        sum(bestnav.present.values()),
        len(bestnav.records),
        sum(bestnav.malformed.values()),
    )
    track_source = getattr(args, "track_source", "auto")
    if track_source in {"bestnav", "bestnavb"} or (
        track_source == "auto" and not solutions.solution_points and bestnav.records
    ):
        bestnav_source = "binary" if track_source == "bestnavb" else "auto"
        bestnav_solutions = bestnav_records_to_solution_extraction(
            bestnav.records,
            source=bestnav_source,
            talk_id=getattr(args, "bestnav_nmea_talk_id", "GN"),
        )
        if bestnav_solutions.solution_points:
            logging.info(
                "using BESTNAV-derived solutions: points=%d nmea_records=%d source=%s",
                len(bestnav_solutions.solution_points),
                len(bestnav_solutions.solution_nmea),
                bestnav_source,
            )
            solutions = bestnav_solutions
        elif track_source in {"bestnav", "bestnavb"}:
            raise ValueError(f"--track-source {track_source} was requested but no valid BESTNAV solution epochs exist")
    original_solution_span = _span_from_solution_points(solutions.solution_points)
    original_observation_span = _span_from_observations(observations.observations)
    solutions = _filter_solution_extraction(solutions, window)
    observations = _filter_observation_extraction(observations, window)
    effective_solution_span = _span_from_solution_points(solutions.solution_points)
    effective_observation_span = _span_from_observations(observations.observations)
    logging.info("scanning ION/UTC/TROPINFO diagnostics")
    emit_ion_utc = getattr(args, "emit_ion_utc", "off")
    with _time_phase(args, "scan_diagnostics"):
        diagnostics = extract_diagnostics(records, emit_policy=emit_ion_utc)
    logging.info(
        "preserved diagnostics: records=%d malformed=%d present_not_converted=%d emit_ion_utc=%s",
        len(diagnostics.records),
        sum(diagnostics.malformed.values()),
        sum(diagnostics.present_not_converted.values()),
        emit_ion_utc,
    )
    if emit_ion_utc == "strict":
        blocked = {
            name: count
            for name, count in diagnostics.present_not_converted.items()
            if name.upper().startswith(("GPSION", "BDSION", "BD3ION", "GALION", "GPSUTC", "BDSUTC", "BD3UTC", "GALUTC"))
        }
        if blocked:
            detail = ", ".join(f"{name}={count}" for name, count in sorted(blocked.items()))
            raise ValueError(
                "--emit-ion-utc strict requested, but no ION/UTC family currently has a verified "
                f"RTKLIB-compatible RINEX NAV mapping: {detail}"
            )
    with _time_phase(args, "build_message_stats"):
        message_stats = build_message_stats(
            records=records,
            stream=stream_diag,
            solutions=solutions,
            observations=observations,
            rover_nav=rover_nav,
            bestnav=bestnav,
        )
    if logging.getLogger().isEnabledFor(logging.INFO):
        log_message_stats(message_stats, debug=logging.getLogger().isEnabledFor(logging.DEBUG))
    logging.info("building analysis report")
    analysis = build_analysis(
        stream=stream_diag,
        solutions=solutions,
        observations=observations,
        rover_nav=rover_nav,
        extra={
            "bestnav": bestnav.as_dict(),
            "diagnostics": {**diagnostics.as_dict(), "emit_ion_utc_policy": emit_ion_utc},
            "message_stats": message_stats.as_dict(),
            "performance": _performance(args),
            "processing_window": {
                "selected": window.as_dict(),
                "original_solution_span": original_solution_span,
                "original_observation_span": original_observation_span,
                "effective_solution_span": effective_solution_span,
                "effective_observation_span": effective_observation_span,
            },
        },
    )
    analysis["inputs"] = {
        "rover_log": str(rover),
        "start_time": window.start.isoformat() if window.start else None,
        "end_time": window.end.isoformat() if window.end else None,
    }
    analysis["effective_processing_window"] = window.to_json()
    analysis["warnings"] = list(dict.fromkeys([*analysis.get("warnings", []), *bestnav.warnings, *message_stats.warnings]))
    bundle = (rover, records, stream_diag, solutions, observations, rover_nav, bestnav, message_stats, analysis)
    setattr(args, "_extract_bundle_cache", bundle)
    return bundle


def _print_analysis_summary(analysis: dict[str, object]) -> None:
    stream = analysis["stream"]  # type: ignore[index]
    print(json.dumps(stream, indent=2, sort_keys=True))
    print(f"solution_points={analysis['solution_points']}")
    raw = analysis["raw_observations"]  # type: ignore[index]
    if isinstance(raw, dict):
        summary_keys = (
            "epochs",
            "observations",
            "mean_hz",
            "median_hz",
            "interval_median_s",
            "interval_max_s",
            "large_gaps",
            "constellations",
            "bands",
            "rinex_observation_codes",
        )
        raw_summary = {key: raw[key] for key in summary_keys if key in raw}
        print(f"raw_observations={json.dumps(raw_summary, sort_keys=True)}")


def _log_analysis_warnings(analysis: dict[str, object]) -> None:
    warnings = analysis.get("warnings", [])
    if not isinstance(warnings, list):
        return
    for warning in warnings:
        logging.warning("%s", warning)


def _write_observation_csv_once(args: argparse.Namespace, path: Path, observations) -> None:
    """Write observation CSV at most once per output path in this process."""

    written = getattr(args, "_observation_csv_written", None)
    if not isinstance(written, set):
        written = set()
        setattr(args, "_observation_csv_written", written)
    key = str(path.resolve())
    if key in written:
        logging.info("observation CSV already written, skipping duplicate: %s", path)
        return
    with _time_phase(args, "observation_csv_write", observations=len(observations)):
        write_observations_csv(path, observations)
    written.add(key)
    logging.info("wrote observation CSV: %s", path)


def _ecef_from_llh(lat_deg: float, lon_deg: float, height_m: float) -> tuple[float, float, float]:
    """Convert WGS84 geodetic coordinates to ECEF meters."""

    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    eccentricity_sq = flattening * (2.0 - flattening)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    radius = semi_major / math.sqrt(1.0 - eccentricity_sq * sin_lat * sin_lat)
    x = (radius + height_m) * cos_lat * math.cos(lon)
    y = (radius + height_m) * cos_lat * math.sin(lon)
    z = (radius * (1.0 - eccentricity_sq) + height_m) * sin_lat
    return x, y, z


def _approx_position_from_solutions(points: list[SolutionPoint]) -> tuple[float, float, float] | None:
    """Return an approximate rover ECEF position from decoded solution points."""

    for point in points:
        height = point.h_ell if point.h_ell is not None else point.h_msl
        if height is not None:
            return _ecef_from_llh(point.lat, point.lon, height)
    return None


def _add_base_position_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-station")
    parser.add_argument(
        "--base-position-source",
        choices=["auto", "euref", "rinex-header", "none"],
        default="auto",
    )
    parser.add_argument("--base-ecef", nargs=3, type=float, metavar=("X", "Y", "Z"))
    parser.add_argument("--base-llh", nargs=3, type=float, metavar=("LAT", "LON", "HEIGHT"))
    parser.add_argument("--base-position-cache-dir")


def _resolve_base_position(
    args: argparse.Namespace,
    base_obs: list[Path] | None = None,
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None]:
    if args.base_ecef and args.base_llh:
        raise ValueError("--base-ecef and --base-llh are mutually exclusive")
    if args.base_ecef:
        return tuple(float(value) for value in args.base_ecef), None  # type: ignore[return-value]
    if args.base_llh:
        return None, tuple(float(value) for value in args.base_llh)  # type: ignore[return-value]
    if args.base_position_source == "none":
        return None, None

    station = args.base_station or getattr(args, "station", None)
    cache_dir = Path(args.base_position_cache_dir) if args.base_position_cache_dir else None
    if station and args.base_position_source in {"auto", "euref"}:
        try:
            position = fetch_epn_station_position(station, cache_dir=cache_dir)
            _log_base_position(position)
            return position.ecef_xyz_m, None
        except Exception as exc:
            if args.base_position_source == "euref":
                raise ValueError(f"could not resolve EUREF/EPN base coordinates for {station}: {exc}") from exc
            logging.warning("could not resolve EUREF/EPN base coordinates for %s: %s", station, exc)

    if args.base_position_source in {"auto", "rinex-header"}:
        base_candidates = base_obs or [Path(item) for item in getattr(args, "base_obs", None) or []]
        if not base_candidates:
            if args.base_position_source == "rinex-header":
                raise ValueError("--base-obs is required to resolve base position from RINEX header")
            return None, None
        position = parse_rinex_approx_position(base_candidates[0])
        _log_base_position(position)
        return position.ecef_xyz_m, None

    if args.base_position_source == "euref" and not station:
        raise ValueError("--base-station or --station is required with --base-position-source=euref")
    return None, None


def _add_base_download_args(parser: argparse.ArgumentParser, *, require_station: bool, include_rtklib_dir: bool = True) -> None:
    parser.add_argument("--station", required=require_station)
    parser.add_argument("--station-long")
    if include_rtklib_dir:
        parser.add_argument("--rtklib-dir")
    parser.add_argument("--base-provider", choices=BASE_PROVIDER_CHOICES, default="bev-nrt")
    parser.add_argument("--base-rate", choices=["30s", "1s"], default="30s")
    parser.add_argument("--base-resolution", choices=["low", "high"], default="low")
    parser.add_argument("--base-rinex-version", choices=["3", "2", "auto"], default="3")
    parser.add_argument("--no-base-fallback", action="store_true")
    parser.add_argument("--base-template")
    parser.add_argument("--base-dir")
    parser.add_argument("--cache-dir")
    parser.add_argument(
        "--time-margin",
        type=int,
        default=0,
        help=(
            "Extra seconds added before and after the rover recorded time span "
            "when planning base downloads. Defaults to 0 so only products that "
            "overlap or touch the recorded interval are requested."
        ),
    )
    parser.add_argument("--whole-day", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Redownload planned EUREF source archives instead of reusing cached files.",
    )
    parser.add_argument("--crx2rnx")
    parser.add_argument("--cleanup", action="store_true")


def _add_rtklib_processing_args(parser: argparse.ArgumentParser, *, require_base_obs: bool) -> None:
    parser.add_argument("--rtklib-dir")
    parser.add_argument("--rnx2rtkp", default="rnx2rtkp")
    parser.add_argument("--convbin")
    parser.add_argument("--crx2rnx")
    parser.add_argument("--rtkconf")
    parser.add_argument("--rtklib-path-style", choices=["auto", "unix", "windows"], default="auto")
    parser.add_argument(
        "--output-format",
        action="append",
        default=None,
        metavar="FORMAT",
        help=(
            "RTKLIB solution output format: pos, llh, or nmea. Repeat the option "
            "or pass a comma-separated list to run rnx2rtkp once per format. pos "
            "is the standard .pos file suffix for LLH content; nmea passes "
            "rnx2rtkp -n, not only a .nmea filename suffix."
        ),
    )
    parser.add_argument("--base-obs", action="append", required=require_base_obs)
    parser.add_argument(
        "--base-rtcm",
        help="Recorded real-time base RTCM3 stream; converted with convbin and used as the RTKLIB base observation input.",
    )
    parser.add_argument(
        "--rtk-pos-mode",
        choices=sorted(RTK_POS_MODE_CODES),
        default="kinematic",
        help="Generated rnx2rtkp mode when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--rtk-frequency",
        choices=sorted(RTK_FREQUENCY_CODES),
        default="l1+l2+l5",
        help="Generated rnx2rtkp frequency setting when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--navsys",
        choices=sorted(RTK_NAVSYS_PRESETS),
        default="all",
        help="Generated rnx2rtkp navigation-system preset when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--rtk-navsys",
        help="Comma-separated generated rnx2rtkp systems, e.g. gps,glo,gal,bds,qzs,sbs.",
    )
    parser.add_argument(
        "--rtk-elevation-mask",
        type=float,
        default=10.0,
        help="Generated rnx2rtkp elevation mask in degrees when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--rtk-soltype",
        choices=["forward", "backward", "combined"],
        default="combined",
        help="Generated rnx2rtkp post-processing direction when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--rtk-ar-mode",
        choices=["continuous", "instantaneous", "fix-and-hold"],
        default="continuous",
        help="Generated rnx2rtkp ambiguity mode shortcut when --rtkconf is omitted.",
    )
    parser.add_argument(
        "--rnx2rtkp-option",
        action="append",
        default=[],
        help="Additional raw rnx2rtkp argument. Repeat for each token, e.g. --rnx2rtkp-option=-x --rnx2rtkp-option=4.",
    )
    parser.add_argument(
        "--rtklib-trace-level",
        type=int,
        choices=range(0, 6),
        metavar="0..5",
        help="Pass rnx2rtkp -x LEVEL to write a debug trace file, e.g. 4 for AR/residual diagnostics.",
    )
    parser.add_argument(
        "--rtklib-stat-level",
        type=int,
        choices=[0, 1, 2],
        metavar="0..2",
        help="Pass rnx2rtkp -y LEVEL to write solution status details; 2 includes residuals.",
    )
    _add_sbas_source_args(parser)
    _add_auto_sat_qc_args(parser)


def _add_auto_sat_qc_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-sat-qc",
        action="store_true",
        help="Run explicit two-pass RTKLIB `.stat` based satellite QC. Requires --rtkconf.",
    )
    parser.add_argument("--auto-sat-qc-source", choices=["stat"], default="stat")
    parser.add_argument("--max-auto-exclude", type=int, default=4)
    parser.add_argument("--max-high-el-exclude", type=int, default=1)
    parser.add_argument("--max-low-el-exclude", type=int, default=3)
    parser.add_argument("--min-remaining-sats", type=int, default=9)
    parser.add_argument("--min-remaining-constellations", type=int, default=2)


def _base_download_attempts(args: argparse.Namespace) -> list[BaseArchiveCandidateGroup]:
    requested_resolution = args.base_resolution
    if args.base_rate == BASE_RATE_HIGH or args.base_provider in {
        "bkg-euref-highrate",
        "bkg-euref-highrate-v2",
        "bkg-igs-highrate",
        "bkg-igs-highrate-v2",
    }:
        requested_resolution = "high"
    versions = ["3", "2"] if args.base_rinex_version == "auto" else [args.base_rinex_version]
    attempts: list[BaseArchiveCandidateGroup] = []
    for version in versions:
        attempts.extend(_base_download_attempts_for_resolution(args, requested_resolution, version, is_fallback=False))
    if requested_resolution == "high" and _base_fallback_allowed(args):
        for version in versions:
            attempts.extend(_base_download_attempts_for_resolution(args, "low", version, is_fallback=True))
    unique: list[BaseArchiveCandidateGroup] = []
    seen: set[tuple[str, str, str, str, str, bool]] = set()
    for attempt in attempts:
        key = (
            attempt.source_kind,
            attempt.resolution,
            attempt.rinex_version,
            attempt.provider,
            attempt.nominal_rate,
            attempt.is_fallback,
        )
        if key not in seen:
            unique.append(attempt)
            seen.add(key)
    return unique


def _base_download_attempts_for_resolution(
    args: argparse.Namespace,
    resolution: str,
    rinex_version: str,
    *,
    is_fallback: bool,
) -> list[BaseArchiveCandidateGroup]:
    if resolution == "high":
        providers = _high_rate_archive_providers(args.base_provider, rinex_version)
        return [
            BaseArchiveCandidateGroup(
                source_kind="archive",
                resolution=resolution,
                rinex_version=rinex_version,
                provider=provider,
                nominal_rate=BASE_RATE_HIGH,
                is_fallback=is_fallback,
            )
            for provider in providers
        ]
    provider = args.base_provider
    if provider in {"bkg-euref-highrate", "bkg-igs-highrate"}:
        provider = "bkg-euref-nrt"
    return [
        BaseArchiveCandidateGroup(
            source_kind="archive",
            resolution=resolution,
            rinex_version=rinex_version,
            provider=provider,
            nominal_rate=BASE_RATE_LOW,
            is_fallback=is_fallback,
        )
    ]


def _high_rate_archive_providers(base_provider: str, rinex_version: str) -> list[str]:
    """Return generic high-rate archive providers to try for a station."""

    suffix = "-v2" if rinex_version == "2" else ""
    euref = f"bkg-euref-highrate{suffix}"
    igs = f"bkg-igs-highrate{suffix}"
    if base_provider in {"bkg-igs-highrate", "bkg-igs-highrate-v2"}:
        return [igs, euref]
    if base_provider in {"bkg-euref-highrate", "bkg-euref-highrate-v2"}:
        return [euref, igs]
    return [euref, igs]


def _base_fallback_allowed(args: argparse.Namespace) -> bool:
    """Return true when high-rate EUREF requests may fall back to low-rate data."""

    return not bool(getattr(args, "no_base_fallback", False))


def _base_rate_from_filename(path: Path) -> str:
    """Classify a base observation file by known EUREF filename rate patterns.

    Returns:
        `1s`, `30s`, or `unknown`.
    """

    name = path.name.upper()
    if "_01S_" in name or "15M_01S" in name:
        return BASE_RATE_HIGH
    if "_30S_" in name or "01H_30S" in name:
        return BASE_RATE_LOW
    # Compact RINEX 2 high-rate BKG names include two minute digits after the
    # hour letter, e.g. TUBO143F15.26O; low-rate hourly names stop at the hour.
    if re.fullmatch(r"[A-Z0-9]{4}\d{3}[A-X]\d{2}\.\d{2}[OD](?:\.(?:GZ|Z))?", name):
        return BASE_RATE_HIGH
    if re.fullmatch(r"[A-Z0-9]{4}\d{3}[A-X]\.\d{2}[OD](?:\.(?:GZ|Z))?", name):
        return BASE_RATE_LOW
    return "unknown"


def _selected_base_rate(paths: list[Path], nominal_rate: str) -> str:
    """Return the selected base rate, preferring explicit filename evidence."""

    rates = {_base_rate_from_filename(path) for path in paths}
    rates.discard("unknown")
    if len(rates) == 1:
        return rates.pop()
    if len(rates) > 1:
        return "mixed"
    return nominal_rate


def _validate_selected_base_resolution(
    *,
    paths: list[Path],
    requested_resolution: str,
    selected_resolution: str,
    nominal_rate: str,
    provider: str,
    rinex_version: str,
    station_long: str,
    fallback_used: bool,
    allow_fallback: bool,
) -> str:
    """Validate and log selected EUREF base files against the requested rate."""

    selected_rate = _selected_base_rate(paths, nominal_rate)
    low_selected = selected_rate in {BASE_RATE_LOW, "mixed"} or any(
        _base_rate_from_filename(path) == BASE_RATE_LOW for path in paths
    )
    if requested_resolution == "high" and low_selected:
        message = (
            "requested high-rate base data but selected low-rate 30 s base observations: "
            f"station={station_long} provider={provider} rinex={rinex_version}"
        )
        if fallback_used and allow_fallback:
            logging.warning(
                "%s; falling back because fallback is enabled. This run is not a high-rate base run.",
                message,
            )
        else:
            raise RuntimeError(
                message
                + "; rerun with fallback enabled or choose another station/provider with high-rate 1 s data"
            )
    for path in paths:
        logging.info(
            "selected base observation: station=%s provider=%s requested_resolution=%s "
            "selected_resolution=%s selected_rate=%s nominal_rate=%s rinex=%s fallback=%s file=%s",
            station_long,
            provider,
            requested_resolution,
            selected_resolution,
            selected_rate,
            nominal_rate,
            rinex_version,
            fallback_used,
            path,
        )
    return selected_rate


def _download_base_files(args: argparse.Namespace) -> list[Path]:
    if not args.station:
        raise ValueError("--station is required to download base observations")
    start, end = _time_window_from_solutions(args, args.time_margin)
    return _download_base_files_for_window(args, start, end)


def _download_base_files_for_window(args: argparse.Namespace, start, end) -> list[Path]:
    """Download base observations covering the requested inclusive time window."""

    if not args.station:
        raise ValueError("--station is required to download base observations")
    station_long = _resolve_station_for_base_download(args)
    attempts = _base_download_attempts(args)
    requested_resolution = "high" if attempts and attempts[0].resolution == "high" else args.base_resolution
    allow_fallback = _base_fallback_allowed(args)
    logging.info(
        "base resolution request: requested_base_resolution=%s requested_base_rinex_version=%s "
        "station=%s allow_base_fallback=%s",
        requested_resolution,
        args.base_rinex_version,
        station_long,
        str(allow_fallback).lower(),
    )
    planned_by_attempt: list[tuple[BaseArchiveCandidateGroup, list[str]]] = []
    for attempt in attempts:
        plan_start = start
        plan_end = end
        if attempt.nominal_rate == BASE_RATE_HIGH:
            plan_start = start - timedelta(seconds=HIGH_RATE_ARCHIVE_MARGIN_S)
            plan_end = end + timedelta(seconds=HIGH_RATE_ARCHIVE_MARGIN_S)
        urls = planned_urls(
            station=args.station,
            station_long=station_long,
            start=plan_start,
            end=plan_end,
            provider_name=attempt.provider,
            base_rate=attempt.nominal_rate,
            whole_day=args.whole_day,
            rinex_version=attempt.rinex_version,
        )
        logging.info(
            "base candidate group: source=%s provider=%s resolution=%s nominal_interval=%s rinex=%s fallback=%s "
            "window=%s..%s candidates=%d",
            attempt.source_kind,
            attempt.provider,
            attempt.resolution,
            attempt.nominal_rate,
            attempt.rinex_version,
            str(attempt.is_fallback).lower(),
            plan_start,
            plan_end,
            len(urls),
        )
        planned_by_attempt.append((attempt, urls))

    if args.offline or args.dry_run:
        target_dir = Path(args.cache_dir or args.base_dir or "euref-cache")
        mode = "offline" if args.offline else "dry-run"
        logging.info(
            "%s mode: no EUREF base files will be downloaded; planned local cache directory is %s",
            mode,
            target_dir,
        )
        for _, urls in planned_by_attempt:
            print("\n".join(urls))
        return []

    cache_dir = Path(args.cache_dir or args.base_dir or "euref-cache")
    logging.info("EUREF base files will be stored in %s", cache_dir)
    last_error: Exception | None = None
    for index, (attempt, urls) in enumerate(planned_by_attempt):
        fallback_attempt = attempt.is_fallback
        try:
            logging.info(
                "%s EUREF base observations: station=%s provider=%s rate=%s rinex=%s resolution=%s fallback=%s",
                "force-downloading" if args.force_download else "resolving cached/downloaded",
                station_long,
                attempt.provider,
                attempt.nominal_rate,
                attempt.rinex_version,
                attempt.resolution,
                str(fallback_attempt).lower(),
            )
            listed_urls = filter_urls_by_remote_listing(urls, cache_dir, force=args.force_download)
            if len(listed_urls) != len(urls):
                logging.info(
                    "remote listing preflight retained %d of %d planned EUREF URLs",
                    len(listed_urls),
                    len(urls),
                )
            downloaded = download_urls(listed_urls, cache_dir, force=args.force_download)
            for path in downloaded:
                logging.info("resolved EUREF base candidate: %s", path)
            crx2rnx = _resolve_crx2rnx_for_download(args, downloaded)
            normalised = [
                normalise_rinex_file(path, crx2rnx=crx2rnx, cleanup=args.cleanup)
                for path in downloaded
            ]
            for source, normalised_path in zip(downloaded, normalised, strict=True):
                if normalised_path == source:
                    logging.info("using EUREF base observation file: %s", normalised_path)
                else:
                    logging.info("normalised EUREF base observation file: %s -> %s", source, normalised_path)
            if normalised:
                selected_rate = _validate_selected_base_resolution(
                    paths=normalised,
                    requested_resolution=requested_resolution,
                    selected_resolution=attempt.resolution,
                    nominal_rate=attempt.nominal_rate,
                    provider=attempt.provider,
                    rinex_version=attempt.rinex_version,
                    station_long=station_long,
                    fallback_used=fallback_attempt,
                    allow_fallback=allow_fallback,
                )
                if fallback_attempt:
                    logging.warning(
                        "requested high-rate base data but no high-rate archive candidate was available; "
                        "falling back to low-rate %s base data because fallback is enabled",
                        selected_rate,
                    )
                    logging.warning(
                        "using fallback EUREF base observations: provider=%s rate=%s rinex=%s",
                        attempt.provider,
                        attempt.nominal_rate,
                        attempt.rinex_version,
                    )
                return normalised
            last_error = RuntimeError("downloaded EUREF base observation list was empty")
        except Exception as exc:
            last_error = exc
            if index + 1 < len(planned_by_attempt):
                next_attempt, _ = planned_by_attempt[index + 1]
                logging.warning(
                    "EUREF base observations unavailable for provider=%s rate=%s rinex=%s: %s; "
                    "trying provider=%s rate=%s rinex=%s",
                    attempt.provider,
                    attempt.nominal_rate,
                    attempt.rinex_version,
                    exc,
                    next_attempt.provider,
                    next_attempt.nominal_rate,
                    next_attempt.rinex_version,
                )
            elif attempt.resolution == "high":
                logging.warning("high-rate EUREF base observations unavailable and fallback is disabled: %s", exc)
    if last_error:
        raise RuntimeError(f"no usable EUREF base observation files were available: {last_error}") from last_error
    raise RuntimeError("no usable EUREF base observation files were available")


def _resolve_crx2rnx_for_download(args: argparse.Namespace, downloaded: list[Path]) -> str | None:
    """Resolve a Hatanaka converter before mutating downloaded base files.

    Args:
        args: CLI arguments with optional `crx2rnx` and `rtklib_dir` values.
        downloaded: Downloaded base observation candidates.

    Returns:
        A subprocess-ready converter path when conversion is required, otherwise
        the user-provided converter value or None.

    Raises:
        RuntimeError: If Hatanaka files are present and no converter can be
            found.
    """

    if not any(requires_crx2rnx(path) for path in downloaded):
        return args.crx2rnx

    candidate = _find_crx2rnx_candidate(args)
    if candidate is None:
        raise RuntimeError(
            "crx2rnx is required to convert downloaded Hatanaka base observations before extraction; "
            "looked in --crx2rnx, --rtklib-dir, the current directory, ~/RTKLIB-ex-bin/bin, "
            "build-tools/RTKLIB-ex-bin/bin, and PATH."
        )
    resolved = executable_for_subprocess(candidate)
    logging.info("using crx2rnx for Hatanaka conversion: %s", resolved)
    return resolved


def _find_crx2rnx_candidate(args: argparse.Namespace) -> str | None:
    """Find a usable `crx2rnx` converter for downloaded Hatanaka files."""

    explicit = getattr(args, "crx2rnx", None)
    rtklib_dir = getattr(args, "rtklib_dir", None)
    if explicit:
        if is_windows_path(explicit):
            return explicit if executable_exists(explicit) else None
        explicit_path = Path(explicit.replace("\\", "/"))
        if explicit_path.parent != Path(".") or explicit.startswith("."):
            for explicit_candidate in _crx2rnx_path_candidates(explicit_path):
                if executable_exists(str(explicit_candidate)):
                    return str(explicit_candidate)
            return None
        for current_dir_candidate in _crx2rnx_path_candidates(Path.cwd() / explicit_path):
            if executable_exists(str(current_dir_candidate)):
                return str(current_dir_candidate)
        candidate = resolve_rtklib_tool(explicit, rtklib_dir=rtklib_dir)
        if executable_exists(candidate):
            return candidate
        return None

    tool_names = ["crx2rnx", "crx2rnx.exe"]
    candidates: list[str] = []
    if rtklib_dir:
        candidates.extend(str(Path(rtklib_dir) / name) for name in tool_names)
    candidates.extend(str(Path.cwd() / name) for name in tool_names)
    candidates.append(resolve_rtklib_tool("crx2rnx"))
    candidates.append(resolve_rtklib_tool("crx2rnx.exe"))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if executable_exists(candidate):
            return candidate
    return None


def _crx2rnx_path_candidates(path: Path) -> list[Path]:
    """Return exact and Windows-suffix variants for a requested converter path."""

    candidates = [path if path.is_absolute() else Path.cwd() / path]
    if candidates[0].suffix.lower() != ".exe":
        candidates.append(candidates[0].with_name(candidates[0].name + ".exe"))
    return candidates


RTKLIB_OUTPUT_FORMATS = {"nmea", "pos", "llh"}


def _rtklib_output_formats(args: argparse.Namespace) -> list[str]:
    """Return validated RTKLIB output formats requested for this run."""

    raw = getattr(args, "output_format", None)
    if raw is None:
        values = ["pos"]
    elif isinstance(raw, str):
        values = [raw]
    else:
        values = list(raw)
    formats: list[str] = []
    for value in values:
        for item in _csv_items(value):
            if item not in RTKLIB_OUTPUT_FORMATS:
                valid = ", ".join(sorted(RTKLIB_OUTPUT_FORMATS))
                raise ValueError(f"unsupported --output-format item {item!r}; expected one of: {valid}")
            formats.append(item)
    return list(dict.fromkeys(formats)) or ["pos"]


def _generated_rtk_options(args: argparse.Namespace, *, output_format: str | None = None) -> list[str]:
    """Return generated `rnx2rtkp` processing options when no config is used."""

    selected_format = output_format or _rtklib_output_formats(args)[0]
    options = [
        "-p",
        RTK_POS_MODE_CODES[args.rtk_pos_mode],
        "-f",
        RTK_FREQUENCY_CODES[args.rtk_frequency],
        "-sys",
        _rtk_navsys_arg(args),
        "-m",
        f"{float(args.rtk_elevation_mask):g}",
        "-t",
    ]
    if args.rtk_soltype == "backward":
        options.append("-b")
    elif args.rtk_soltype == "combined":
        options.append("-c")
    if args.rtk_ar_mode == "instantaneous":
        options.append("-i")
    elif args.rtk_ar_mode == "fix-and-hold":
        options.append("-h")
    options.extend(_rtklib_command_override_options(args, output_format=selected_format))
    options.extend(getattr(args, "rnx2rtkp_option", []) or [])
    return options


def _rtklib_command_override_options(args: argparse.Namespace, *, output_format: str | None = None) -> list[str]:
    """Return named RTKLIB command-line overrides that also apply with configs."""

    selected_format = output_format or _rtklib_output_formats(args)[0]
    return _rtklib_output_format_options(selected_format) + _rtklib_debug_output_options(args)


def _rtklib_output_format_options(output_format: str) -> list[str]:
    """Return `rnx2rtkp` options required by `--output-format`.

    Args:
        output_format: One validated RTKLIB output format.

    Returns:
        Command-line option tokens that make RTKLIB produce the requested
        content, not just the requested output filename suffix.

    Raises:
        ValueError: If an unsupported output format reaches this helper.
    """

    if output_format == "nmea":
        return ["-n"]
    if output_format in {"pos", "llh"}:
        # RTKLIB calls normal latitude/longitude/height solution content `llh`.
        # `pos` is the conventional filename suffix used for that default.
        return []
    raise ValueError(f"unsupported --output-format {output_format!r}")


def _rtklib_debug_output_options(args: argparse.Namespace) -> list[str]:
    """Return `rnx2rtkp` trace/stat options requested for solution debugging."""

    options: list[str] = []
    stat_level = getattr(args, "rtklib_stat_level", None)
    if stat_level is not None:
        options.extend(["-y", str(stat_level)])
    return options


def _validate_quality_trace_args(args: argparse.Namespace, *, standalone: bool = False) -> int | None:
    """Validate quality trace options and return the effective generation level."""

    mode = getattr(args, "quality_trace", "off")
    trace_level = getattr(args, "rtklib_trace_level", None)

    # Standalone `quality --trace PATH` analyses an existing trace file.  The
    # user should not also have to write `--quality-trace existing`; that option
    # is mainly a pipeline/postprocess policy for generated traces.
    if standalone and getattr(args, "trace", None) and mode == "off":
        return None

    if mode == "off":
        if trace_level is not None and trace_level > 0:
            raise ValueError("--rtklib-trace-level requires --quality-trace temporary or --quality-trace keep")
        return None
    if mode == "existing":
        if not getattr(args, "trace", None):
            raise ValueError("--quality-trace existing requires --trace PATH")
        if trace_level is not None:
            logging.warning("--rtklib-trace-level is ignored with --quality-trace existing")
        return None
    if standalone:
        raise ValueError("--quality-trace temporary/keep is only supported by pipeline/postprocess RTKLIB runs")
    if not getattr(args, "quality_analyze", False):
        raise ValueError("--quality-trace temporary/keep requires --quality-analyze")
    level = 3 if trace_level is None else trace_level
    if level <= 0:
        raise ValueError("--rtklib-trace-level must be greater than 0 when trace generation is requested")
    if level == 1:
        logging.warning("RTKLIB trace level 1 is likely too sparse for useful quality diagnostics")
    if level >= 4:
        logging.warning("RTKLIB trace level %d may create very large trace files", level)
    return level


def _rtk_navsys_arg(args: argparse.Namespace) -> str:
    requested = getattr(args, "rtk_navsys", None)
    if not requested:
        return RTK_NAVSYS_PRESETS[args.navsys]
    systems: list[str] = []
    for item in _csv_items(requested):
        if item not in RTK_NAVSYS_CODES:
            valid = ", ".join(sorted(RTK_NAVSYS_CODES))
            raise ValueError(f"unsupported --rtk-navsys item {item!r}; expected one of: {valid}")
        systems.append(RTK_NAVSYS_CODES[item])
    return ",".join(dict.fromkeys(systems))


def _rtklib_config_and_options(args: argparse.Namespace, *, output_format: str | None = None) -> tuple[Path | None, list[str] | None]:
    """Resolve RTKLIB config-file mode versus generated command-line mode."""

    selected_format = output_format or _rtklib_output_formats(args)[0]
    if args.rtkconf:
        override_options = _rtklib_command_override_options(args, output_format=selected_format) + (
            getattr(args, "rnx2rtkp_option", []) or []
        )
        if override_options:
            logging.info(
                "using --rtkconf plus %d rnx2rtkp command-line override tokens for %s output",
                len(override_options),
                selected_format,
            )
            return Path(args.rtkconf), override_options
        logging.info("using RTKLIB configuration file: %s", args.rtkconf)
        return Path(args.rtkconf), None
    generated = _generated_rtk_options(args, output_format=selected_format)
    logging.warning(
        "no --rtkconf supplied; using generated rnx2rtkp CLI options for %s output: %s. "
        "Provide --rtkconf for a full RTKLIB-EX configuration.",
        selected_format,
        " ".join(generated),
    )
    return None, generated


def _run_rtklib_with_optional_auto_qc(
    *,
    args: argparse.Namespace,
    rnx2rtkp: str,
    rtkconf: Path | None,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    base_obs_arg: Path | None,
    rtk_options: list[str] | None,
    base_ecef: tuple[float, float, float] | None,
    base_llh: tuple[float, float, float] | None,
):
    """Run one-pass RTKLIB or explicit two-pass auto-satellite-QC."""

    effective_trace_level = _validate_quality_trace_args(args)
    trace_mode = getattr(args, "quality_trace", "off")
    if not getattr(args, "auto_sat_qc", False):
        return run_rnx2rtkp(
            rnx2rtkp=rnx2rtkp,
            rtkconf=rtkconf,
            output_file=output_file,
            rover_obs=rover_obs,
            base_obs=base_obs,
            nav_files=nav_files,
            base_obs_arg=base_obs_arg,
            rtk_options=rtk_options,
            base_ecef_xyz_m=base_ecef,
            base_llh=base_llh,
            path_style=args.rtklib_path_style,
            dry_run=args.dry_run,
            debug=_debug_enabled(args),
            trace_mode=trace_mode if trace_mode in {"temporary", "keep"} else "off",
            trace_level=effective_trace_level,
            trace_file=Path(args.rtklib_trace_file) if getattr(args, "rtklib_trace_file", None) else None,
            trace_cleanup=getattr(args, "rtklib_trace_cleanup", "always"),
            trace_max_bytes=max(0, int(getattr(args, "quality_trace_max_bytes", 0) or 0)),
            trace_max_example_lines=max(0, int(getattr(args, "quality_trace_examples", 20) or 20)),
        )
    if trace_mode != "off":
        raise ValueError("--quality-trace temporary/keep is not supported with --auto-sat-qc")
    return _run_auto_sat_qc(
        args=args,
        rnx2rtkp=rnx2rtkp,
        rtkconf=rtkconf,
        output_file=output_file,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        base_obs_arg=base_obs_arg,
        rtk_options=rtk_options,
        base_ecef=base_ecef,
        base_llh=base_llh,
    )


def _run_auto_sat_qc(
    *,
    args: argparse.Namespace,
    rnx2rtkp: str,
    rtkconf: Path | None,
    output_file: Path,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    base_obs_arg: Path | None,
    rtk_options: list[str] | None,
    base_ecef: tuple[float, float, float] | None,
    base_llh: tuple[float, float, float] | None,
):
    """Run explicit two-pass satellite QC from RTKLIB `.stat` evidence."""

    if args.auto_sat_qc_source != "stat":
        raise ValueError("only --auto-sat-qc-source stat is supported")
    if rtkconf is None:
        raise ValueError("--auto-sat-qc requires --rtkconf; use um980-autoqc-baseline.conf for pass 1")
    root = output_file.with_suffix("") if output_file.suffix else output_file
    suffix = output_file.suffix or ".pos"
    pass1_output = root.parent / f"{root.name}.pass1{suffix}"
    pass1_stat = root.parent / f"{root.name}.pass1.stat"
    final_stat = root.with_suffix(".stat")
    derived_config = root.parent / f"{root.name}.autoqc.derived.conf"
    report_md = root.parent / f"{root.name}.autoqc.report.md"
    report_json = root.parent / f"{root.name}.autoqc.report.json"
    stat_options = _rtklib_options_with_stat_level(rtk_options, 2)
    logging.info("auto-sat-qc: enabled explicitly by user")
    logging.info("auto-sat-qc: pass 1 config: %s", rtkconf)
    logging.info("auto-sat-qc: pass 1 stat: %s", pass1_stat)
    pass1_command = run_rnx2rtkp(
        rnx2rtkp=rnx2rtkp,
        rtkconf=rtkconf,
        output_file=pass1_output,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        base_obs_arg=base_obs_arg,
        rtk_options=stat_options,
        base_ecef_xyz_m=base_ecef,
        base_llh=base_llh,
        path_style=args.rtklib_path_style,
        dry_run=args.dry_run,
        debug=_debug_enabled(args),
    )
    if args.dry_run:
        logging.info("auto-sat-qc: dry-run stopped before stat parsing")
        return pass1_command
    _move_rtklib_stat(pass1_output, pass1_stat)
    observations = parse_rtklib_stat(pass1_stat)
    if not observations:
        raise ValueError(f"auto-sat-qc: pass 1 stat contains no $SAT rows: {pass1_stat}")
    metrics = compute_sat_metrics(observations)
    config = BadSatConfig(
        max_auto_exclude=args.max_auto_exclude,
        max_high_el_exclude=args.max_high_el_exclude,
        max_low_el_exclude=args.max_low_el_exclude,
        min_remaining_sats=args.min_remaining_sats,
        min_remaining_constellations=args.min_remaining_constellations,
    )
    decision = choose_bad_sats(metrics, config)
    _log_auto_qc_decision(decision)
    patch_config_with_autoqc(
        base_config=rtkconf,
        out_config=derived_config,
        exclude_sats=decision.exclude_sats,
        recommended_elmask=decision.recommended_elmask,
        source_stat=pass1_stat,
        watch_sats=decision.watch_sats,
    )
    write_badsat_markdown_report(report_md, decision)
    write_badsat_json_report(report_json, decision)
    logging.info("auto-sat-qc: derived config written: %s", derived_config)
    logging.info("auto-sat-qc: report written: %s", report_md)
    logging.info("auto-sat-qc: running pass 2 with derived config")
    command = run_rnx2rtkp(
        rnx2rtkp=rnx2rtkp,
        rtkconf=derived_config,
        output_file=output_file,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        base_obs_arg=base_obs_arg,
        rtk_options=stat_options,
        base_ecef_xyz_m=base_ecef,
        base_llh=base_llh,
        path_style=args.rtklib_path_style,
        dry_run=False,
        debug=_debug_enabled(args),
    )
    _move_rtklib_stat(output_file, final_stat)
    return command


def _rtklib_options_with_stat_level(options: list[str] | None, level: int) -> list[str]:
    """Return RTKLIB options with a single `-y LEVEL` entry."""

    result: list[str] = []
    skip_next = False
    for item in options or []:
        if skip_next:
            skip_next = False
            continue
        if item == "-y":
            skip_next = True
            continue
        result.append(item)
    result.extend(["-y", str(level)])
    return result


def _move_rtklib_stat(output_file: Path, target: Path) -> None:
    """Move RTKLIB's default appended `.stat` file to the requested path."""

    source = Path(str(output_file) + ".stat")
    if not source.exists():
        raise FileNotFoundError(f"RTKLIB stat output was not created: {source}")
    if source != target:
        source.replace(target)


def _log_rtklib_solution_summary(args: argparse.Namespace, output_file: Path) -> None:
    """Log RTKLIB quality statistics when verbose diagnostics are enabled."""

    if not _verbose_enabled(args) or getattr(args, "dry_run", False):
        return
    summary = summarize_rtklib_solution(output_file)
    if summary is None:
        logging.warning("RTKLIB solution summary unavailable: no parseable quality-coded positions in %s", output_file)
        return
    for line in format_rtklib_solution_summary(summary):
        logging.info("%s", line)


def _rtklib_output_file(out_dir: Path, basename: str, output_format: str) -> Path:
    """Return the output path for one RTKLIB solution format."""

    return out_dir / f"{basename}-rtk.{output_format}"


def _run_rtklib_output_formats(
    *,
    args: argparse.Namespace,
    rnx2rtkp: str,
    out_dir: Path,
    basename: str,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    base_obs_arg: Path | None,
    base_ecef: tuple[float, float, float] | None,
    base_llh: tuple[float, float, float] | None,
):
    """Run RTKLIB once per requested solution output format."""

    formats = _rtklib_output_formats(args)
    if len(formats) > 1:
        logging.info("running RTKLIB postprocessing for output formats: %s", ", ".join(formats))
    commands = []
    for output_format in formats:
        output_file = _rtklib_output_file(out_dir, basename, output_format)
        rtkconf, rtk_options = _rtklib_config_and_options(args, output_format=output_format)
        logging.info("running RTKLIB postprocessing for %s output", output_format)
        command = _run_rtklib_with_optional_auto_qc(
            args=args,
            rnx2rtkp=rnx2rtkp,
            rtkconf=rtkconf,
            output_file=output_file,
            rover_obs=rover_obs,
            base_obs=base_obs,
            nav_files=nav_files,
            base_obs_arg=base_obs_arg,
            rtk_options=rtk_options,
            base_ecef=base_ecef,
            base_llh=base_llh,
        )
        logging.info("RTKLIB postprocessing finished: %s", output_file)
        _log_rtklib_solution_summary(args, output_file)
        wrapper = getattr(command, "wrapper_file", None)
        if wrapper is not None:
            _append_rerun_command(args, f"Rerun RTKLIB {output_format}", ["bash", str(wrapper)])
        commands.append(command)
    return commands


def _run_quality_analysis_if_requested(
    args: argparse.Namespace,
    out_dir: Path,
    basename: str,
    commands: list | None = None,
    *,
    base_ecef: tuple[float, float, float] | None = None,
    base_llh: tuple[float, float, float] | None = None,
) -> None:
    """Run optional quality analysis for the first generated RTKLIB output."""

    if not getattr(args, "quality_analyze", False):
        return
    if getattr(args, "quality_trace", "off") in {"off", "existing"}:
        _validate_quality_trace_args(args)
    solution = _select_quality_solution_file(out_dir, basename, _rtklib_output_formats(args))
    if solution is None:
        logging.warning("quality analysis requested, but no RTKLIB solution output was found")
        return
    stat = _stat_file_for_solution(solution)
    if stat is None:
        logging.warning("quality analysis running without .stat evidence: %s", solution)
    md_path = Path(args.quality_out_md) if getattr(args, "quality_out_md", None) else out_dir / f"{basename}-rtk.quality.md"
    json_path = Path(args.quality_out_json) if getattr(args, "quality_out_json", None) else out_dir / f"{basename}-rtk.quality.json"
    _append_rerun_command(args, "Rerun quality analysis", _quality_rerun_command(args, solution, stat, json_path, md_path))
    trace_summary = _trace_summary_for_quality(args, commands or [])
    cleanup = {
        "trace_cleanup_requested": getattr(args, "quality_trace", "off") == "temporary",
        "trace_deleted": bool(trace_summary and trace_summary.get("trace_deleted")),
        "trace_cleanup_attempted_paths": trace_summary.get("trace_cleanup_attempted_paths", []) if isinstance(trace_summary, dict) else [],
        "trace_cleanup_deleted_paths": trace_summary.get("trace_cleanup_deleted_paths", []) if isinstance(trace_summary, dict) else [],
        "trace_cleanup_failed_paths": trace_summary.get("trace_cleanup_failed_paths", {}) if isinstance(trace_summary, dict) else {},
        "trace_cleanup_skipped_paths": trace_summary.get("trace_cleanup_skipped_paths", {}) if isinstance(trace_summary, dict) else {},
        "stat_cleanup_requested": bool(getattr(args, "quality_clean_stat", False)),
        "stat_files_deleted": [],
        "stat_files_kept": [str(stat)] if stat else [],
    }
    analysis = analyze_rtk_quality(
        solution_path=solution,
        stat_path=stat,
        thresholds=_quality_thresholds_from_args(args),
        trace_summary=trace_summary,
        cleanup=cleanup,
        stat_max_lines=max(0, int(getattr(args, "quality_stat_max_lines", 0) or 0)),
        stat_max_seconds=max(0.0, float(getattr(args, "quality_stat_max_seconds", 0.0) or 0.0)),
        fast=bool(getattr(args, "quality_fast", False)),
        base_ecef_xyz_m=base_ecef,
        base_llh=base_llh,
        processing_window=_processing_window_from_args(args),
    )
    _log_quality_performance(analysis)
    with _time_phase(args, "quality_report_write"):
        md_path.write_text(_format_quality_markdown_from_args(args, analysis), encoding="utf-8")
        _write_quality_outputs(args, analysis, json_path)
    deleted_stats: list[str] = []
    if getattr(args, "quality_clean_stat", False) and stat is not None:
        stat.unlink()
        deleted_stats.append(str(stat))
        logging.info("deleted generated RTKLIB .stat after successful quality analysis: %s", stat)
        analysis = replace(
            analysis,
            cleanup={
                **cleanup,
                "stat_files_deleted": deleted_stats,
                "stat_files_kept": [],
            },
        )
        with _time_phase(args, "quality_report_write_after_cleanup"):
            md_path.write_text(_format_quality_markdown_from_args(args, analysis), encoding="utf-8")
            _write_quality_outputs(args, analysis, json_path)
    logging.info("wrote RTK quality analysis: markdown=%s json=%s", md_path, json_path)


def _trace_summary_for_quality(args: argparse.Namespace, commands: list) -> dict[str, object] | None:
    """Return trace diagnostics requested for quality analysis."""

    mode = getattr(args, "quality_trace", "off")
    # Explicit standalone trace input always wins.  This fixes:
    #   um980-ppk quality --trace solution.nmea.trace ...
    # previously producing `"trace": {"available": false}` unless the user also
    # supplied `--quality-trace existing`.
    explicit_trace = getattr(args, "trace", None)
    if explicit_trace:
        trace_path = Path(explicit_trace)
        if not trace_path.exists():
            raise FileNotFoundError(f"--trace was specified but file does not exist: {trace_path}")
        summary = analyze_rtklib_trace(
            trace_path,
            max_bytes=max(0, int(getattr(args, "quality_trace_max_bytes", 0) or 0)),
            max_example_lines=max(0, int(getattr(args, "quality_trace_examples", 20) or 20)),
        )
        summary.update(
            {
                "source": "existing",
                "generated_temporarily": False,
                "retained": True,
                "effective_level": None,
                "path": str(trace_path),
            }
        )
        if logging.getLogger().isEnabledFor(logging.INFO):
            logging.info(
                "parsed RTKLIB trace diagnostics: source=existing file=%s size=%s bytes_read=%s lines=%s truncated=%s",
                trace_path,
                summary.get("trace_file_size_bytes"),
                summary.get("trace_bytes_read"),
                summary.get("trace_lines_read"),
                summary.get("trace_truncated"),
            )
        return summary

    for command in commands:
        summary = getattr(command, "trace_summary", None)
        if summary:
            return summary
    return None


def _log_quality_performance(analysis) -> None:
    """Log bounded performance counters from quality analysis."""

    data = analysis.as_dict()
    performance = data.get("performance", {})
    if not isinstance(performance, dict) or not logging.getLogger().isEnabledFor(logging.INFO):
        return
    logging.info(
        "quality STAT summary parsed: lines=%s sat_lines=%s raw_slip_flags=%s "
        "dedup_slip_events=%s unique_slip_epochs=%s elapsed=%.3fs",
        performance.get("stat_lines_read", 0),
        performance.get("sat_lines_parsed", 0),
        performance.get("raw_slip_flags", 0),
        performance.get("dedup_slip_events", 0),
        performance.get("unique_slip_epochs", 0),
        float(performance.get("stat_parse_elapsed_s", 0.0) or 0.0),
    )


def _quality_rerun_command(args: argparse.Namespace, solution: Path, stat: Path | None, json_path: Path, md_path: Path) -> list[str]:
    command = [
        "PYTHONPATH=src",
        "python",
        "-m",
        "um980_rtklib_pipeline.cli",
        "quality",
        "--solution",
        str(solution),
        "--out-json",
        str(json_path),
        "--out-md",
        str(md_path),
    ]
    if stat is not None:
        command.extend(["--stat", str(stat)])

    # Prefer an explicitly supplied trace, otherwise include RTKLIB's default
    # `<solution>.trace` when it exists.  This makes rerun.sh capable of
    # reproducing trace-aware QC without rerunning RTKLIB.
    trace_path: Path | None = None
    if getattr(args, "trace", None):
        trace_path = Path(args.trace)
    else:
        inferred_trace = Path(str(solution) + ".trace")
        if inferred_trace.exists():
            trace_path = inferred_trace
    if trace_path is not None:
        command.extend(["--trace", str(trace_path)])

    if getattr(args, "quality_trace_max_bytes", 0) not in {None, 0, 0.0}:
        command.extend(["--quality-trace-max-bytes", str(args.quality_trace_max_bytes)])
    if getattr(args, "quality_trace_align_tolerance_s", 0.5) not in {None, 0.5}:
        command.extend(["--quality-trace-align-tolerance-s", str(args.quality_trace_align_tolerance_s)])
    command.extend(_processing_window_from_args(args).to_cli_args())
    if getattr(args, "base_ecef", None):
        command.extend(["--base-ecef", *(str(value) for value in args.base_ecef)])
    if getattr(args, "base_llh", None):
        command.extend(["--base-llh", *(str(value) for value in args.base_llh)])

    for attr, option in (
        ("quality_stat_max_lines", "--quality-stat-max-lines"),
        ("quality_stat_max_seconds", "--quality-stat-max-seconds"),
        ("quality_motion_profile", "--quality-motion-profile"),
        ("quality_route_bin_km", "--quality-route-bin-km"),
    ):
        value = getattr(args, attr, None)
        if value not in {None, "", 0, 0.0, "auto"}:
            command.extend([option, str(value)])
    if getattr(args, "quality_fast", False):
        command.append("--quality-fast")
    if getattr(args, "quality_md_show_empty_baseline_bins", False):
        command.append("--quality-md-show-empty-baseline-bins")
    return command


def _select_quality_solution_file(out_dir: Path, basename: str, formats: list[str]) -> Path | None:
    for output_format in ("nmea", "pos", "llh"):
        if output_format not in formats:
            continue
        candidate = _rtklib_output_file(out_dir, basename, output_format)
        if candidate.exists():
            return candidate
    for output_format in formats:
        candidate = _rtklib_output_file(out_dir, basename, output_format)
        if candidate.exists():
            return candidate
    return None


def _stat_file_for_solution(solution: Path) -> Path | None:
    candidates = [Path(str(solution) + ".stat"), solution.with_suffix(".stat")]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _log_auto_qc_decision(decision) -> None:
    if decision.exclude_sats or decision.recommended_elmask is not None:
        elmask = (
            f"elmask -> {decision.recommended_elmask:g}"
            if decision.recommended_elmask is not None
            else "elmask unchanged"
        )
        logging.info(
            "auto-sat-qc: selected actions: %s; exclude %s; watch %s",
            elmask,
            " ".join(decision.exclude_sats) or "none",
            " ".join(decision.watch_sats) or "none",
        )
    else:
        logging.info("auto-sat-qc: no satellite exclusions selected; no elevation-mask change selected")
        logging.info("auto-sat-qc: derived config still written for reproducibility")
    for metric in decision.metrics:
        if metric.watch_score <= 0 and metric.hard_score <= 0:
            continue
        blocked = ";".join(decision.blocked_reasons.get(metric.sat, [])) or "none"
        logging.debug(
            "auto-sat-qc: %s hard_score=%.1f watch_score=%.1f mean_el=%.1f combined_p95=%.2f "
            "phase_p95=%.3f residual_gt5=%d rej_delta=%d blocked=%s reasons=%s",
            metric.sat,
            metric.hard_score,
            metric.watch_score,
            metric.mean_el,
            metric.combined_p95,
            metric.phase_p95,
            metric.residual_gt5,
            metric.rej_delta,
            blocked,
            ";".join(metric.reasons) or "none",
        )


def _prepare_rtklib_base_obs_argument(base_obs: list[Path], out_dir: Path, basename: str) -> Path | None:
    """Stage multiple base OBS files and return one wildcard argument for RTKLIB.

    RTKLIB/rnx2rtkp treats the second positional input as the base observation
    input. When a base station spans several hourly or high-rate RINEX files,
    passing each file as a separate positional argument causes later base files
    to be interpreted as navigation inputs. To keep the command deterministic,
    this function validates the concrete `base_obs` list elsewhere, stages only
    those files into a managed directory, and returns a wildcard that is passed
    as one subprocess argument for RTKLIB to expand internally.
    """

    if len(base_obs) <= 1:
        return None
    stage_dir = out_dir / f"{basename}.rtklib-base"
    stage_dir.mkdir(parents=True, exist_ok=True)
    for stale in stage_dir.glob("base-*"):
        if stale.is_file() or stale.is_symlink():
            stale.unlink()
    staged: list[Path] = []
    for index, source in enumerate(base_obs):
        suffix = source.suffix or ".obs"
        target = stage_dir / f"base-{index:04d}{suffix}"
        shutil.copyfile(source, target)
        staged.append(target)
    suffixes = {path.suffix for path in staged}
    glob_suffix = suffixes.pop() if len(suffixes) == 1 else ""
    pattern = stage_dir / f"base-*{glob_suffix}"
    logging.info("staged %d base observation files for RTKLIB wildcard input: %s", len(staged), pattern)
    return pattern


def _merge_rinex_obs_capabilities(capabilities: list[RinexObsCapabilities]) -> dict[str, set[str]]:
    """Return a union of RINEX observation types advertised by several files."""

    merged: dict[str, set[str]] = {}
    for capability in capabilities:
        for system, obs_types in capability.observation_types.items():
            merged.setdefault(system, set()).update(obs_types)
    return merged


def _bands_from_observation_types(observation_types: dict[str, set[str]]) -> dict[str, set[str]]:
    """Return frequency-band digits per RINEX system from observation types."""

    return {
        system: {
            obs_type[1]
            for obs_type in obs_types
            if len(obs_type) >= 2 and obs_type[0] in {"C", "L", "D", "S"} and obs_type[1].isdigit()
        }
        for system, obs_types in observation_types.items()
    }


def _format_observation_capability_summary(observation_types: dict[str, set[str]]) -> str:
    """Format a compact RINEX OBS capability summary for logs."""

    parts: list[str] = []
    bands_by_system = _bands_from_observation_types(observation_types)
    for system in sorted(observation_types):
        obs_codes = ",".join(sorted(observation_types[system])) or "-"
        bands = ",".join(sorted(bands_by_system.get(system, set()))) or "-"
        parts.append(f"{system}:bands={bands}:obs={obs_codes}")
    return "; ".join(parts) if parts else "none"


def _rinex_obs_capability_gaps(
    rover_types: dict[str, set[str]], base_types: dict[str, set[str]]
) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Return rover capabilities that are not advertised by a base source."""

    missing_systems = sorted(set(rover_types) - set(base_types))
    rover_bands = _bands_from_observation_types(rover_types)
    base_bands = _bands_from_observation_types(base_types)
    missing_bands = {
        system: sorted(bands - base_bands.get(system, set()))
        for system, bands in rover_bands.items()
        if bands - base_bands.get(system, set())
    }
    missing_codes = {
        system: sorted(obs_types - base_types.get(system, set()))
        for system, obs_types in rover_types.items()
        if obs_types - base_types.get(system, set())
    }
    return missing_systems, missing_bands, missing_codes


def _format_missing_bands(missing_bands: dict[str, list[str]]) -> str:
    """Format missing RINEX frequency bands for logs."""

    return "; ".join(f"{system}:{','.join(bands)}" for system, bands in sorted(missing_bands.items()))


def _log_rover_base_capability_report(rover_obs: Path, base_obs: list[Path]) -> None:
    """Log rover/base RINEX OBS constellation and frequency compatibility.

    The comparison is directional: the base must advertise every rover
    constellation and frequency band needed for differential processing. Extra
    base constellations or bands are logged as available capability and are not
    treated as a mismatch.
    """

    rover_capability = read_rinex_obs_capabilities(rover_obs)
    base_capabilities = [read_rinex_obs_capabilities(path) for path in base_obs]
    rover_types = {system: set(obs_types) for system, obs_types in rover_capability.observation_types.items()}
    base_types = _merge_rinex_obs_capabilities(base_capabilities)
    logging.info("rover RINEX OBS capabilities: %s", _format_observation_capability_summary(rover_types))
    logging.info(
        "base RINEX OBS aggregate capabilities: files=%d %s",
        len(base_obs),
        _format_observation_capability_summary(base_types),
    )
    for capability in base_capabilities:
        logging.debug(
            "base RINEX OBS capabilities: file=%s %s",
            capability.path,
            _format_observation_capability_summary(
                {system: set(obs_types) for system, obs_types in capability.observation_types.items()}
            ),
        )
    if not rover_types:
        logging.warning("could not read rover RINEX OBS capability header from %s", rover_obs)
        return
    if not base_types:
        logging.warning("could not read base RINEX OBS capability headers from %d file(s)", len(base_obs))
        return

    missing_systems, missing_bands, missing_codes = _rinex_obs_capability_gaps(rover_types, base_types)
    if missing_systems:
        logging.warning(
            "rover/base RINEX capability mismatch: base is missing rover constellation(s): %s",
            ",".join(missing_systems),
        )

    if missing_bands:
        logging.warning(
            "rover/base RINEX capability mismatch: base is missing rover frequency band(s): %s",
            _format_missing_bands(missing_bands),
        )

    if missing_codes:
        formatted = "; ".join(f"{system}:{','.join(codes)}" for system, codes in sorted(missing_codes.items()))
        logging.debug("base lacks exact rover RINEX observation code(s): %s", formatted)

    file_gaps: list[str] = []
    for capability in base_capabilities:
        capability_types = {system: set(obs_types) for system, obs_types in capability.observation_types.items()}
        file_missing_systems, file_missing_bands, _ = _rinex_obs_capability_gaps(rover_types, capability_types)
        if not file_missing_systems and not file_missing_bands:
            continue
        details: list[str] = []
        if file_missing_systems:
            details.append(f"missing_constellations={','.join(file_missing_systems)}")
        if file_missing_bands:
            details.append(f"missing_bands={_format_missing_bands(file_missing_bands)}")
        file_gaps.append(f"{capability.path} ({'; '.join(details)})")
    if file_gaps:
        examples = "; ".join(file_gaps[:5])
        omitted = "" if len(file_gaps) <= 5 else f"; {len(file_gaps) - 5} more"
        logging.warning(
            "rover/base RINEX capability mismatch: %d base file(s) are missing rover capability: %s%s",
            len(file_gaps),
            examples,
            omitted,
        )

    extra_systems = sorted(set(base_types) - set(rover_types))
    if extra_systems:
        logging.info(
            "base provides additional constellation(s) not present in rover RINEX; this is not a mismatch: %s",
            ",".join(extra_systems),
        )


def _resolve_station_for_base_download(args: argparse.Namespace) -> str:
    try:
        return resolve_station(args.station, args.station_long)
    except ValueError:
        if (
            args.base_rinex_version == "2"
            and args.station_long is None
            and len(args.station) == 4
            and args.station.isalnum()
        ):
            return args.station.upper()
        raise


def _log_base_position(position: BasePosition) -> None:
    x, y, z = position.ecef_xyz_m
    logging.info(
        "using base position for %s from %s: X=%.4f Y=%.4f Z=%.4f",
        position.station,
        position.source,
        x,
        y,
        z,
    )


def _profile_from_args(args: argparse.Namespace) -> InitProfile:
    config = load_config(args.config)
    raw_cfg = config.get("raw", {}) if isinstance(config.get("raw", {}), dict) else {}
    ppp_cfg = config.get("ppp", {}) if isinstance(config.get("ppp", {}), dict) else {}
    diag_cfg = config.get("diagnostics", {}) if isinstance(config.get("diagnostics", {}), dict) else {}
    eph_cfg = config.get("ephemeris", {}) if isinstance(config.get("ephemeris", {}), dict) else {}
    sbas_cfg = config.get("sbas", {}) if isinstance(config.get("sbas", {}), dict) else {}

    preset = args.nmea_preset or config.get("nmea_preset") or deep_get(config, "nmea", "preset", default="minimal")
    nmea = dict(NMEA_PRESETS[preset])
    _apply_solution_hz(nmea, deep_get(config, "nmea", "solution_hz"))
    overrides = deep_get(config, "nmea", "overrides", default={})
    if isinstance(overrides, dict):
        nmea.update({str(k).upper(): float(v) for k, v in overrides.items()})
    _apply_solution_hz(nmea, args.solution_hz)
    nmea.update(parse_nmea_overrides(args.nmea))

    raw_hz = args.raw_hz
    if raw_hz is None and args.raw_period:
        raw_hz = 1.0 / args.raw_period if args.raw_period else 0.0
    if raw_hz is None:
        raw_hz = float(raw_cfg.get("hz", 0.0))
    bestnav_cfg = config.get("bestnav", {}) if isinstance(config.get("bestnav", {}), dict) else {}
    bestnav_format = (args.bestnav_format or bestnav_cfg.get("format", "none")).lower()
    bestnav_hz = args.bestnav_hz if args.bestnav_hz is not None else bestnav_cfg.get("hz", 0.0)

    eph_format = (
        args.ephemeris_format
        or str(eph_cfg.get("format", "ascii") if eph_cfg else "ascii")
    ).lower()
    eph_policy = args.ephemeris
    debug_ascii_ephemeris = bool(
        args.debug_ascii_ephemeris or eph_cfg.get("debug_ascii_ephemeris", False)
    )
    if debug_ascii_ephemeris and eph_format != "ascii":
        raise ValueError("--debug-ascii-ephemeris cannot be combined with binary ephemeris format")
    if debug_ascii_ephemeris and (args.ephemeris or args.ephemeris_systems):
        raise ValueError(
            "--debug-ascii-ephemeris already enables GPSEPHA/GLOEPHA/GALEPHA/"
            "BDSEPHA/BD3EPHA/QZSSEPHA every 300 seconds; do not combine it with "
            "--ephemeris or --ephemeris-systems"
        )
    if eph_policy is None and eph_cfg:
        if eph_cfg.get("policy") == "every":
            eph_policy = f"every={eph_cfg.get('period', 300)}"
        else:
            eph_policy = eph_cfg.get("policy", "off")
    systems = (args.ephemeris_systems or ",".join(eph_cfg.get("systems", [])) or "gps,glo,gal,bds,bd3,qzss").split(",")

    converge = None
    converge_text = args.ppp_converge or ppp_cfg.get("converge")
    if isinstance(converge_text, str):
        left, right = converge_text.split(",", 1)
        converge = (int(left), int(right))
    elif isinstance(converge_text, list) and len(converge_text) == 2:
        converge = (int(converge_text[0]), int(converge_text[1]))
    ion_period = (
        args.ion_period
        if args.ion_period is not None
        else diag_cfg.get("ion_period_s", diag_cfg.get("ion_period"))
    )
    utc_period = (
        args.utc_period
        if args.utc_period is not None
        else diag_cfg.get("utc_period_s", diag_cfg.get("utc_period"))
    )
    diagnostic_format = (
        args.diagnostic_format
        or str(diag_cfg.get("format", diag_cfg.get("diagnostic_format", "ascii")))
    ).lower()

    return InitProfile(
        port=args.port or config.get("port") or deep_get(config, "receiver", "port", default="COM1"),
        baud=int(args.baud or config.get("baud") or deep_get(config, "receiver", "baud", default=230400)),
        mode=args.mode or config.get("mode") or deep_get(config, "receiver", "mode", default="rover"),
        base_lat=args.base_lat,
        base_lon=args.base_lon,
        base_height=args.base_height,
        nmea=nmea,
        raw_format=(args.raw_format or raw_cfg.get("format", "none")).lower(),
        raw_hz=float(raw_hz),
        bestnav_format=bestnav_format,
        bestnav_hz=float(bestnav_hz),
        expected_obs_per_epoch=int(
            args.expected_obs_per_epoch or raw_cfg.get("expected_obs_per_epoch", 100)
        ),
        ephemeris=(
            debug_ascii_ephemeris_policy()
            if debug_ascii_ephemeris
            else ephemeris_policy(
                eph_policy or "off",
                [system.strip() for system in systems],
                message_format=eph_format,
            )
        ),
        ephemeris_format=eph_format,
        debug_ascii_ephemeris=debug_ascii_ephemeris,
        ppp=(args.ppp or ppp_cfg.get("mode", "none")).lower(),
        ppp_datum=args.ppp_datum or ppp_cfg.get("datum", "WGS84"),
        ppp_timeout=args.ppp_timeout or ppp_cfg.get("timeout"),
        ppp_converge=converge,
        include_tropinfo=bool(args.include_tropinfo or diag_cfg.get("tropinfo", False)),
        diagnostic_format=diagnostic_format,
        ion_messages=_resolve_ion_messages(args, diag_cfg),
        ion_period_s=float(ion_period) if ion_period is not None else None,
        utc_messages=_resolve_utc_messages(args, diag_cfg),
        utc_period_s=float(utc_period) if utc_period is not None else None,
        sbas=(args.sbas or str(sbas_cfg.get("mode", "off"))).lower(),
        sbas_timeout_s=(
            int(args.sbas_timeout)
            if args.sbas_timeout is not None
            else (int(sbas_cfg["timeout"]) if "timeout" in sbas_cfg else None)
        ),
        include_gpsion=False,
        save_config=bool(args.save_config or config.get("save_config", False)),
    )


def cmd_init_generate(args: argparse.Namespace) -> int:
    """Handle `init generate`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    profile = _profile_from_args(args)
    logging.info("rendering receiver init script")
    script, estimate = render_init_script(
        profile,
        strict_bitrate=args.strict_bitrate,
        allow_overload=args.allow_overload,
    )
    if args.out:
        Path(args.out).write_text(script, encoding="ascii")
        logging.info("wrote receiver init script: %s", args.out)
    else:
        print(script, end="")
    if args.json:
        write_json_report(Path(args.json), profile, estimate)
        logging.info("wrote init estimate JSON: %s", args.json)
    if args.verbose:
        print(json.dumps(estimate.as_dict(), indent=2, sort_keys=True))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Handle `analyze`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    rover, _, _, solutions, observations, rover_nav, _, _, analysis = _extract_bundle(args)
    if args.analysis_json:
        out_dir = ensure_out_dir(args.out_dir)
        base = basename_for(rover, args.basename)
        analysis_path = out_dir / f"{base}.analysis.json"
        write_analysis_json(analysis_path, analysis)
        logging.info("wrote analysis JSON: %s", analysis_path)
        _print_analysis_summary(analysis)
    _log_analysis_warnings(analysis)
    return 0


def _write_bestnav_nmea(path: Path, bestnav, args: argparse.Namespace) -> None:
    """Write generated NMEA from BESTNAV receiver-solution records."""

    sentences = parse_bestnav_sentences(args.bestnav_nmea_sentences)
    rate = parse_bestnav_rate(args.bestnav_nmea_rate)
    selected = filter_bestnav_records(
        bestnav.records,
        source=args.bestnav_nmea_source,
        rate_hz=rate,
    )
    lines = bestnav_records_to_nmea(selected, sentences=sentences, talk_id=args.bestnav_nmea_talk_id)
    if not lines:
        present = ", ".join(f"{key}={value}" for key, value in sorted(bestnav.present.items())) or "none"
        raise ValueError(
            "no valid BESTNAV records were available for generated NMEA "
            f"(requested source={args.bestnav_nmea_source}, present={present})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_lines(path, lines)
    logging.info(
        "wrote BESTNAV-derived NMEA: %s (%d sentences, %d epochs, rate=%s)",
        path,
        len(lines),
        len(selected),
        args.bestnav_nmea_rate,
    )


def cmd_extract(args: argparse.Namespace) -> int:
    """Handle `extract`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    rover, _, _, solutions, observations, rover_nav, bestnav, _, analysis = _extract_bundle(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(rover, args.basename)

    solution = args.solution
    if solution in {"all", "nmea"}:
        position_nmea_mode = getattr(args, "position_nmea", "best")
        all_nmea = out_dir / f"{base}.all.nmea"
        position_nmea = out_dir / f"{base}.position.nmea"
        solution_nmea = out_dir / f"{base}.solution.nmea"
        write_lines(all_nmea, solutions.all_nmea)
        if position_nmea_mode != "none":
            write_lines(position_nmea, position_nmea_records(solutions.all_nmea, position_nmea_mode))
        write_solution_nmea(solution_nmea, solutions.solution_points, solutions.solution_nmea)
        logging.info(
            "wrote NMEA outputs: %s%s, %s",
            all_nmea,
            f", {position_nmea}" if position_nmea_mode != "none" else "",
            solution_nmea,
        )
    if solution in {"all", "csv"}:
        solution_csv = out_dir / f"{base}.solution.csv"
        all_records_csv = out_dir / f"{base}.solution_all_records.csv"
        write_solution_csv(solution_csv, solutions.solution_points)
        write_all_records_csv(all_records_csv, solutions.all_rows)
        logging.info("wrote solution CSV outputs: %s, %s", solution_csv, all_records_csv)
    if solution in {"all", "gpx"}:
        gpx = out_dir / f"{base}.solution.gpx"
        write_gpx(gpx, solutions.solution_points)
        logging.info("wrote GPX output: %s", gpx)
    if args.obs_csv:
        obs_csv = out_dir / f"{base}.observations.csv"
        _write_observation_csv_once(args, obs_csv, observations.observations)
    if getattr(args, "bestnav_nmea", None):
        _write_bestnav_nmea(Path(args.bestnav_nmea), bestnav, args)
    if args.analysis_json:
        analysis_path = out_dir / f"{base}.analysis.json"
        write_analysis_json(analysis_path, analysis)
        logging.info("wrote analysis JSON: %s", analysis_path)
        _print_analysis_summary(analysis)
    _log_analysis_warnings(analysis)
    return 0


def cmd_rinex(args: argparse.Namespace) -> int:
    """Handle `rinex`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    rover, records, _, solutions, observations, rover_nav, _, _, analysis = _extract_bundle(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(rover, args.basename)
    if args.obs_csv:
        obs_csv = out_dir / f"{base}.observations.csv"
        _write_observation_csv_once(args, obs_csv, observations.observations)
    nav_path = out_dir / f"{base}.rover-gps.nav"
    logging.info("extracting rover navigation files: base=%s", nav_path)
    nav_report = extract_rover_nav(records, nav_path)
    for kind, path in sorted(nav_report.written.items()):
        logging.info("wrote rover %s file: %s", kind, path)
    analysis["ephemeris"] = nav_report.as_dict()
    analysis["warnings"] = list(dict.fromkeys([*analysis.get("warnings", []), *nav_report.warnings]))
    rinex_observations = observations_for_rinex(
        observations.observations,
        compatibility=args.rinex_compat,
    )
    dropped = len(observations.observations) - len(rinex_observations)
    if dropped:
        logging.warning(
            "RINEX %s compatibility dropped %d observations that are not safe for that profile",
            args.rinex_compat,
            dropped,
        )
    try:
        obs_path = out_dir / f"{base}.direct.obs"
        logging.info("writing rover RINEX OBS: %s", obs_path)
        approx_position = _approx_position_from_solutions(solutions.solution_points)
        if approx_position is None:
            logging.warning("no decoded rover solution height available; RINEX APPROX POSITION XYZ will be 0 0 0")
        with _time_phase(args, "rinex_obs_write", observations=len(rinex_observations)):
            write_rinex_obs(
                obs_path,
                rinex_observations,
                rinex_version=args.rinex_version,
                compatibility=args.rinex_compat,
                approx_position=approx_position,
                progress=logging.getLogger().isEnabledFor(logging.INFO),
            )
        logging.info("wrote rover RINEX OBS: %s", obs_path)
    except ValueError as exc:
        analysis["warnings"] = list(
            dict.fromkeys([*analysis.get("warnings", []), f"RINEX OBS was not written: {exc}"])
        )
        if args.analysis_json:
            analysis_path = out_dir / f"{base}.analysis.json"
            write_analysis_json(analysis_path, analysis)
            logging.info("wrote analysis JSON: %s", analysis_path)
        _log_analysis_warnings(analysis)
        raise
    if args.analysis_json:
        analysis_path = out_dir / f"{base}.analysis.json"
        write_analysis_json(analysis_path, analysis)
        logging.info("wrote analysis JSON: %s", analysis_path)
        _print_analysis_summary(analysis)
    _log_analysis_warnings(analysis)
    return 0


def cmd_base_candidates(args: argparse.Namespace) -> int:
    """Handle non-destructive base-station advisory."""

    _configure_cli_logging(args)
    rover, _, _, solutions, _, _, _, _, _ = _extract_bundle(args)
    window = _processing_window_from_args(args)
    if not solutions.solution_points:
        raise ValueError("base-candidates requires live NMEA or BESTNAV solution points")
    catalog_cache = Path(args.station_catalog_cache) if args.station_catalog_cache else default_station_catalog_cache()
    catalog = load_station_catalog(
        cache_path=catalog_cache,
        source=args.station_catalog_source,
        refresh=args.refresh_station_catalog,
        curated_positions=CURATED_STATION_POSITIONS,
    )
    report = build_base_advisory_report(
        solution_points=solutions.solution_points,
        start=window.start,
        end=window.end,
        radius_km=args.radius_km,
        max_candidates=args.max_candidates,
        base_resolution=args.base_resolution,
        allow_resolution_fallback=args.allow_resolution_fallback == "yes",
        stations=_csv_items(args.stations) or None,
        station_catalog=catalog,
        probe_archives=args.probe_archives,
        download_headers_only=args.download_headers_only,
        refresh_probes=args.refresh_probes,
        probe_cache_dir=Path(args.probe_cache_dir) if args.probe_cache_dir else None,
        nav_source=args.nav_source,
        require_nav=args.require_nav,
        require_constellations=_csv_items(args.require_constellations),
    )
    rendered = format_base_advisory(report, args.format)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        logging.info("wrote base-candidate advisory: %s", args.out)
    else:
        print(rendered)
    if args.analysis_json:
        out_dir = ensure_out_dir(args.out_dir)
        base = basename_for(rover, args.basename)
        analysis_path = out_dir / f"{base}.base-candidates.json"
        write_analysis_json(analysis_path, report.as_dict())
        logging.info("wrote base-candidate JSON: %s", analysis_path)
    return 0


def cmd_optimize_settings(args: argparse.Namespace) -> int:
    """Handle bounded RTKLIB settings/base optimisation planning."""

    _configure_cli_logging(args)
    window = _processing_window_from_args(args)
    bases = _csv_items(args.bases)
    if args.base:
        bases.insert(0, args.base)
    if args.base_list:
        bases.extend(load_base_list(Path(args.base_list)))
    if args.base_candidates_json:
        bases.extend(load_bases_from_candidates(Path(args.base_candidates_json), top_bases=args.top_bases))
    bases = list(dict.fromkeys(base for base in bases if base))
    sample_duration_s = parse_duration_seconds(args.sample_duration)
    plan = build_optimizer_plan(
        rover_files=[Path(path) for path in args.rover_log],
        config=Path(args.config) if args.config else None,
        bases=bases,
        base_resolution=args.base_resolution,
        nav_source=args.nav_source,
        sbas_source=args.sbas_source,
        emit_ion_utc=args.emit_ion_utc,
        window=window,
        sample_count=args.sample_count,
        sample_duration_s=sample_duration_s,
        max_variants=args.max_variants,
        max_runs=args.max_runs,
        dry_run=not args.execute,
    )
    if args.execute:
        plan = execute_optimizer_plan(
            plan,
            out_dir=ensure_out_dir(args.out_dir) if args.out_dir else Path("optimizer-out"),
            keep_intermediate=args.keep_intermediate == "yes",
        )
    rendered = format_optimizer_plan(plan, args.format)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        logging.info("wrote optimizer plan: %s", args.out)
    else:
        print(rendered)
    if args.analysis_json:
        out_dir = ensure_out_dir(args.out_dir)
        analysis_path = out_dir / "optimizer-plan.json"
        write_analysis_json(analysis_path, plan.as_dict())
        logging.info("wrote optimizer JSON: %s", analysis_path)
    return 0


def cmd_quality_analyze(args: argparse.Namespace) -> int:
    """Handle standalone RTK solution quality analysis."""

    _configure_cli_logging(args)
    if getattr(args, "_quality_alias", "quality") == "quality-analyze":
        logging.warning("standalone subcommand 'quality-analyze' is deprecated; use 'quality' instead")
    if getattr(args, "compare_json", None):
        left_path, right_path = (Path(item) for item in args.compare_json)
        comparison = compare_quality_reports(
            json.loads(left_path.read_text(encoding="utf-8")),
            json.loads(right_path.read_text(encoding="utf-8")),
        )
        rendered = (
            json.dumps(comparison, indent=2, sort_keys=True, default=str)
            if args.format == "json"
            else format_quality_comparison_markdown(comparison)
            if args.format == "markdown"
            else _format_quality_comparison_text(comparison)
        )
        if args.out_json:
            Path(args.out_json).write_text(json.dumps(comparison, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        if args.out_md:
            Path(args.out_md).write_text(format_quality_comparison_markdown(comparison), encoding="utf-8")
        print(rendered)
        return 0
    if not args.solution:
        raise ValueError("quality requires --solution unless --compare-json LEFT RIGHT is supplied")
    if getattr(args, "quality_clean_stat", False):
        raise ValueError(
            "--quality-clean-stat is only supported for .stat files generated by the current pipeline/postprocess run"
        )
    _validate_quality_trace_args(args, standalone=True)
    base_ecef, base_llh = _resolve_base_position(args, base_obs=[])
    trace_summary = _trace_summary_for_quality(args, [])
    cleanup = {
        "trace_cleanup_requested": False,
        "trace_deleted": False,
        "stat_cleanup_requested": False,
        "stat_files_deleted": [],
        "stat_files_kept": [str(args.stat)] if args.stat else [],
    }
    analysis = analyze_rtk_quality(
        solution_path=Path(args.solution),
        stat_path=Path(args.stat) if args.stat else None,
        thresholds=_quality_thresholds_from_args(args),
        trace_summary=trace_summary,
        cleanup=cleanup,
        stat_max_lines=max(0, int(getattr(args, "quality_stat_max_lines", 0) or 0)),
        stat_max_seconds=max(0.0, float(getattr(args, "quality_stat_max_seconds", 0.0) or 0.0)),
        fast=bool(getattr(args, "quality_fast", False)),
        base_ecef_xyz_m=base_ecef,
        base_llh=base_llh,
        processing_window=_processing_window_from_args(args),
    )
    _log_quality_performance(analysis)
    if args.out_json:
        _write_quality_outputs(args, analysis, Path(args.out_json))
        logging.info("wrote quality JSON: %s", args.out_json)
    else:
        _write_quality_outputs(args, analysis, None)
    if args.out_md:
        Path(args.out_md).write_text(_format_quality_markdown_from_args(args, analysis), encoding="utf-8")
        logging.info("wrote quality Markdown: %s", args.out_md)
    rendered = (
        json.dumps(
            analysis.as_dict(
                include_all_segments=bool(getattr(args, "quality_include_all_segments", False)),
                include_geometry_segments=bool(getattr(args, "quality_include_geometry_segments", False)),
                include_empty_bins=bool(getattr(args, "quality_include_empty_bins", False)),
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
        if args.format == "json"
        else _format_quality_markdown_from_args(args, analysis)
        if args.format == "markdown"
        else format_quality_text(analysis)
    )
    if not args.out_json or not args.out_md or args.format != "text":
        print(rendered)
    return 0


def cmd_quality_compare(args: argparse.Namespace) -> int:
    """Handle comparison of existing quality JSON reports."""

    _configure_cli_logging(args)
    if len(args.reports) < 2:
        raise ValueError("quality-compare requires at least two quality JSON reports")
    baseline = json.loads(Path(args.reports[0]).read_text(encoding="utf-8"))
    comparisons = []
    for report in args.reports[1:]:
        comparison = compare_quality_reports(baseline, json.loads(Path(report).read_text(encoding="utf-8")))
        comparisons.append({"report": str(report), "comparison": comparison})
    result = {"baseline": str(args.reports[0]), "comparisons": comparisons}
    if args.format == "json":
        rendered = json.dumps(result, indent=2, sort_keys=True, default=str)
    elif args.format == "markdown":
        lines = ["# RTK Quality Comparison", ""]
        for item in comparisons:
            lines.extend([f"## {item['report']}", "", format_quality_comparison_markdown(item["comparison"]).strip(), ""])
        rendered = "\n".join(lines)
    else:
        lines = [f"Baseline: {args.reports[0]}"]
        for item in comparisons:
            lines.append(f"Compared: {item['report']}")
            lines.append(_format_quality_comparison_text(item["comparison"]))
        rendered = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def _time_window_from_solutions(args: argparse.Namespace, margin_s: int):
    rover, _, _, solutions, _, _, _, _, _ = _extract_bundle(args)
    if not solutions.solution_points:
        raise ValueError("no rover time window could be determined from solution records")
    start = min(point.time_utc for point in solutions.solution_points) - timedelta(seconds=margin_s)
    end = max(point.time_utc for point in solutions.solution_points) + timedelta(seconds=margin_s)
    return start, end


def cmd_download_base(args: argparse.Namespace) -> int:
    """Handle `download-base`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    logging.info("starting EUREF base download")
    normalised = _download_base_files(args)
    if normalised:
        print("\n".join(str(path) for path in normalised))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Handle the composable cleanup step without deleting user data by default."""

    _configure_cli_logging(args)
    logging.info("cleanup step has no default destructive actions; generated .stat/.trace cleanup remains opt-in")
    return 0


def cmd_record_base_rt(args: argparse.Namespace) -> int:
    """Handle `record-base-rt` for NTRIP base RTCM recording."""

    _configure_cli_logging(args)
    result = record_ntrip_base(
        caster=args.caster,
        port=args.port,
        mountpoint=args.mountpoint,
        out_dir=Path(args.out_dir),
        station=args.station,
        user=args.user,
        password=args.password,
        str2str=args.str2str,
        rtklib_dir=args.rtklib_dir,
        path_style=args.rtklib_path_style,
    )
    logging.info(
        "real-time base recording stopped: output=%s duration=%.1fs bytes=%d metadata=%s",
        result.output_rtcm3,
        result.duration_s,
        result.bytes_written,
        result.metadata_json,
    )
    print(result.output_rtcm3)
    return 0


def cmd_ntrip_sourcetable(args: argparse.Namespace) -> int:
    """Handle `ntrip-sourcetable` raw caster listing fetches."""

    _configure_cli_logging(args)
    text = fetch_ntrip_sourcetable(
        caster=args.caster,
        port=args.port,
        out=Path(args.out),
        contains=args.contains,
        user=args.user,
        password=args.password,
    )
    logging.info("wrote NTRIP sourcetable output: %s (%d bytes)", args.out, len(text.encode("utf-8")))
    return 0


def _convert_base_rtcm_if_requested(args: argparse.Namespace, out_dir: Path, basename: str) -> tuple[list[Path], list[Path]]:
    """Convert a user-recorded base RTCM stream to RTKLIB-ready RINEX inputs."""

    base_rtcm = getattr(args, "base_rtcm", None)
    if not base_rtcm:
        return [], []
    if getattr(args, "download_base", False):
        raise ValueError("--base-rtcm and --download-base are mutually exclusive; choose one base source")
    rtcm_path = Path(base_rtcm)
    obs, nav_files = convert_rtcm_to_rinex(
        rtcm_path=rtcm_path,
        out_dir=out_dir,
        basename=f"{basename}.base-rtcm",
        convbin=getattr(args, "convbin", None) or "convbin",
        rtklib_dir=getattr(args, "rtklib_dir", None),
        path_style=getattr(args, "rtklib_path_style", "auto"),
    )
    logging.info(
        "base_source=realtime-recording base_rtcm=%s converted_base_obs=%s converted_base_nav=%s",
        rtcm_path,
        obs,
        ",".join(str(path) for path in nav_files) or "none",
    )
    return [obs], nav_files


def _expand_nav_inputs(files: list[str] | None, patterns: list[str] | None) -> list[Path]:
    """Return explicit files plus glob-expanded NAV input paths."""

    paths = [Path(path) for path in files or []]
    for pattern in patterns or []:
        matches = sorted(glob(pattern))
        if not matches:
            logging.warning("NAV glob matched no files: %s", pattern)
        paths.extend(Path(match) for match in matches)
    return _dedupe_paths(paths)


def _nav_source_for_resolver(args: argparse.Namespace) -> str:
    """Map CLI NAV source aliases onto resolver policy names."""

    source = getattr(args, "nav_source", "auto")
    if source in {"auto-prefer-base", "merge"}:
        return "auto"
    return source


def _nav_merge_for_resolver(args: argparse.Namespace) -> str:
    """Return NAV merge policy, applying the legacy `--nav-source merge` alias."""

    if getattr(args, "nav_source", "auto") == "merge":
        return "best-per-system"
    return getattr(args, "nav_merge", "best-per-system")


def _log_nav_resolution(resolution, *, source: str, merge: str) -> None:
    """Log concise NAV source and rover/base usability diagnostics."""

    logging.info(
        "NAV source policy: source=%s merge=%s priority=explicit>base>rover>external",
        source,
        merge,
    )
    for candidate in getattr(resolution, "candidates", []):
        logging.debug(
            "candidate NAV: role=%s path=%s systems=%s usable=%s notes=%s",
            getattr(candidate, "role", getattr(candidate, "source", "unknown")),
            candidate.path,
            ",".join(sorted(getattr(candidate, "systems", set()))) or "none",
            getattr(candidate, "usable", "unknown"),
            "; ".join(getattr(candidate, "notes", [])) or "none",
        )
    for candidate in getattr(resolution, "selected", []):
        logging.info(
            "selected NAV: role=%s path=%s systems=%s",
            getattr(candidate, "role", getattr(candidate, "source", "unknown")),
            candidate.path,
            ",".join(sorted(getattr(candidate, "systems", set()))) or "none",
        )
    if merge == "all":
        logging.info("NAV merge policy all: all usable NAV inputs are included")
    for system, candidate in sorted(getattr(resolution, "system_sources", {}).items()):
        logging.info(
            "NAV merge %s=%s reason=%s path=%s",
            system,
            getattr(candidate, "role", "unknown"),
            getattr(resolution, "system_reasons", {}).get(system, "selected"),
            candidate.path,
        )
    rover_obs_systems = getattr(resolution, "rover_obs_systems", set())
    base_obs_systems = getattr(resolution, "base_obs_systems", set())
    if rover_obs_systems or base_obs_systems:
        logging.info(
            "NAV usability: rover_obs_systems=%s base_obs_systems=%s usable_rtk_systems=%s not_useful=%s",
            ",".join(sorted(rover_obs_systems)) or "unknown",
            ",".join(sorted(base_obs_systems)) or "unknown",
            ",".join(sorted(getattr(resolution, "usable_rtk_systems", set()))) or "none",
            ",".join(sorted(getattr(resolution, "nav_systems_not_useful", set()))) or "none",
        )


def _file_inventory(path: Path) -> dict[str, object]:
    """Return local file metadata for RTKLIB input inventory."""

    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else None,
        "kind": classify_rinex_file(path) if exists else "missing",
    }


def _rtklib_input_inventory(
    *,
    rover_obs: Path,
    base_obs: list[Path],
    nav_files: list[Path],
    args: argparse.Namespace,
) -> dict[str, object]:
    """Build a reproducible RTKLIB input inventory."""

    return {
        "rover_obs": _file_inventory(rover_obs),
        "base_obs": [_file_inventory(path) for path in base_obs],
        "nav_sbas_inputs": [_file_inventory(path) for path in nav_files],
        "nav_source": getattr(args, "nav_source", "auto"),
        "nav_merge": getattr(args, "nav_merge", "best-per-system"),
        "sbas_source": getattr(args, "sbas_source", "auto"),
        "emit_ion_utc": getattr(args, "emit_ion_utc", "off"),
        "processing_window": _processing_window_from_args(args).as_dict(),
        "rtkconf": getattr(args, "rtkconf", None),
        "output_format": getattr(args, "output_format", None),
    }


def _log_rtklib_input_inventory(inventory: dict[str, object]) -> None:
    """Log a concise RTKLIB input inventory."""

    rover = inventory["rover_obs"]  # type: ignore[index]
    logging.info(
        "RTKLIB input rover_obs: %s kind=%s size=%s",
        rover["path"],  # type: ignore[index]
        rover["kind"],  # type: ignore[index]
        rover["size_bytes"],  # type: ignore[index]
    )
    for item in inventory["base_obs"]:  # type: ignore[index]
        logging.info(
            "RTKLIB input base_obs: %s kind=%s size=%s",
            item["path"],
            item["kind"],
            item["size_bytes"],
        )
    for item in inventory["nav_sbas_inputs"]:  # type: ignore[index]
        logging.info(
            "RTKLIB input nav_sbas: %s kind=%s size=%s",
            item["path"],
            item["kind"],
            item["size_bytes"],
        )
    logging.info(
        "RTKLIB source modes: nav_source=%s nav_merge=%s sbas_source=%s emit_ion_utc=%s",
        inventory["nav_source"],
        inventory["nav_merge"],
        inventory["sbas_source"],
        inventory["emit_ion_utc"],
    )


def _write_rtklib_inventory_analysis(out_dir: Path, basename: str, inventory: dict[str, object]) -> None:
    """Merge RTKLIB inventory into existing analysis JSON when present."""

    analysis_path = out_dir / f"{basename}.analysis.json"
    if not analysis_path.exists():
        return
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("could not update analysis JSON with RTKLIB inventory: %s", analysis_path)
        return
    analysis["rtklib_inventory"] = inventory
    write_analysis_json(analysis_path, analysis)
    logging.info("updated analysis JSON with RTKLIB inventory: %s", analysis_path)


def cmd_postprocess(args: argparse.Namespace) -> int:
    """Handle `postprocess`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    _log_effective_run_summary(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = args.basename or Path(args.rover_log).stem
    if getattr(args, "dry_run_plan", False):
        setattr(args, "dry_run", True)
    _init_rerun_artifacts(args, out_dir, base)
    rover_nav = []
    if args.use_rover_nav:
        logging.info("extracting rover navigation for RTKLIB input")
        records, _ = _load_records(Path(args.rover_log))
        rover_nav_path = out_dir / f"{base}.rover-gps.nav"
        nav_report = extract_rover_nav(records, rover_nav_path)
        for warning in nav_report.warnings:
            logging.warning("%s", warning)
        rover_nav = rover_nav_files(rover_nav_path)
        logging.info("using %d rover navigation sidecar files", len(rover_nav))
    rover_obs = Path(args.rover_obs)
    base_obs = [Path(item) for item in args.base_obs or []]
    converted_base_obs, base_rtcm_nav = _convert_base_rtcm_if_requested(args, out_dir, base)
    base_obs.extend(converted_base_obs)
    if not base_obs:
        raise ValueError("--base-obs or --base-rtcm is required for postprocess")
    logging.info("resolving navigation inputs for RTKLIB")
    explicit_nav = _expand_nav_inputs(getattr(args, "nav_file", None), getattr(args, "nav_glob", None))
    base_nav = _expand_nav_inputs(getattr(args, "base_nav_file", None), getattr(args, "base_nav_glob", None))
    manual_rover_nav = _expand_nav_inputs(getattr(args, "rover_nav_file", None), getattr(args, "rover_nav_glob", None))
    rover_nav = _dedupe_paths([*manual_rover_nav, *rover_nav])
    nav_source = _nav_source_for_resolver(args)
    nav_merge = _nav_merge_for_resolver(args)
    nav_resolution = resolve_nav_sources(
        explicit=explicit_nav,
        base=base_nav,
        base_rtcm=base_rtcm_nav,
        rover=rover_nav,
        rover_obs_systems=detect_rinex_obs_systems(rover_obs),
        base_obs_systems=set().union(*(detect_rinex_obs_systems(path) for path in base_obs)),
        nav_source=nav_source,
        merge_policy=nav_merge,
    )
    _log_nav_resolution(nav_resolution, source=getattr(args, "nav_source", "auto"), merge=nav_merge)
    if not nav_resolution.selected:
        raise ValueError(nav_resolution.warnings[0])
    rnx2rtkp = resolve_rtklib_tool(args.rnx2rtkp, rtklib_dir=args.rtklib_dir)
    logging.info("resolved rnx2rtkp executable: %s", rnx2rtkp)
    logging.info("checking rover/base observation time overlap: rover=%s base_files=%d", rover_obs, len(base_obs))
    base_obs, overlap_warnings = filter_rinex_obs_by_overlap(rover_obs, base_obs)
    for warning in overlap_warnings:
        logging.warning("%s", warning)
    logging.info("retained %d base observation files after overlap filtering", len(base_obs))
    _log_rover_base_capability_report(rover_obs, base_obs)
    logging.info("resolving base position")
    base_ecef, base_llh = _resolve_base_position(args, base_obs=base_obs)
    base_obs_arg = _prepare_rtklib_base_obs_argument(base_obs, out_dir, base)
    nav_files = _apply_sbas_source_policy(args, _dedupe_paths([candidate.path for candidate in nav_resolution.selected] + rover_nav))
    inventory = _rtklib_input_inventory(rover_obs=rover_obs, base_obs=base_obs, nav_files=nav_files, args=args)
    _log_rtklib_input_inventory(inventory)
    commands = _run_rtklib_output_formats(
        args=args,
        rnx2rtkp=rnx2rtkp,
        out_dir=out_dir,
        basename=base,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=nav_files,
        base_obs_arg=base_obs_arg,
        base_ecef=base_ecef,
        base_llh=base_llh,
    )
    _run_quality_analysis_if_requested(args, out_dir, base, commands, base_ecef=base_ecef, base_llh=base_llh)
    for command in commands:
        print(format_command(command.args))
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Handle the integrated `pipeline` command.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    _log_effective_run_summary(args)
    if getattr(args, "base_rtcm", None) and args.download_base:
        raise ValueError("--base-rtcm and --download-base are mutually exclusive; choose one base source")
    if getattr(args, "dry_run_plan", False):
        setattr(args, "dry_run", True)
        logging.info("dry-run plan enabled: RTKLIB execution will be planned but not run")
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(args.rover_log, args.basename)
    _init_rerun_artifacts(args, out_dir, base)
    rover_obs = out_dir / f"{base}.direct.obs"
    _init_pipeline_manifest(args, out_dir, base)
    window_cli_args = _processing_window_from_args(args).to_cli_args()
    extract_step_command = [
        "PYTHONPATH=src",
        "python",
        "-m",
        "um980_rtklib_pipeline.cli",
        "extract",
        str(args.rover_log),
        "--out-dir",
        str(out_dir),
        "--basename",
        base,
        *window_cli_args,
    ]
    rinex_step_command = [
        "PYTHONPATH=src",
        "python",
        "-m",
        "um980_rtklib_pipeline.cli",
        "rinex",
        str(args.rover_log),
        "--out-dir",
        str(out_dir),
        "--basename",
        base,
        *window_cli_args,
    ]
    _append_rerun_command(args, "Extract receiver products", extract_step_command)
    _append_rerun_command(args, "Write RINEX OBS and rover NAV", rinex_step_command)
    _record_pipeline_step(
        args,
        "extract_receiver_products",
        inputs=[args.rover_log],
        outputs=[out_dir / f"{base}.solution.csv", out_dir / f"{base}.solution.nmea"],
        command=extract_step_command,
        status="planned",
    )
    _record_pipeline_step(
        args,
        "write_rinex_obs",
        inputs=[args.rover_log],
        outputs=[rover_obs, out_dir / f"{base}.rover-gps.nav"],
        command=rinex_step_command,
        dependencies=["extract_receiver_products"],
        status="planned",
    )
    _record_pipeline_step(args, "resolve_base", inputs=[rover_obs], outputs=[], dependencies=["write_rinex_obs"], status="planned")
    _record_pipeline_step(args, "run_rtklib", inputs=[rover_obs], outputs=[_rtklib_output_file(out_dir, base, _rtklib_output_formats(args)[0])], dependencies=["resolve_base"], status="planned")
    _record_pipeline_step(args, "quality", inputs=[_rtklib_output_file(out_dir, base, _rtklib_output_formats(args)[0])], outputs=[out_dir / f"{base}-rtk.quality.json", out_dir / f"{base}-rtk.quality.md"], dependencies=["run_rtklib"], status="planned")
    if getattr(args, "dry_run_plan", False):
        logging.info("dry-run plan written: %s", getattr(args, "_pipeline_manifest_path", None))
        return 0
    if _should_run_pipeline_step(args, "extract_receiver_products"):
        extract_outputs = [out_dir / f"{base}.solution.csv", out_dir / f"{base}.solution.nmea"]
        if getattr(args, "skip_existing", False) and not _forced_step(args, "extract_receiver_products") and _existing_outputs(extract_outputs):
            logging.info("pipeline step extract_receiver_products: reusing existing outputs")
            _record_pipeline_step(args, "extract_receiver_products", inputs=[args.rover_log], outputs=extract_outputs, status="skipped", reused=True)
        else:
            started = time.perf_counter()
            logging.info("pipeline step 1/3: extract receiver products")
            cmd_extract(args)
            _record_pipeline_step(args, "extract_receiver_products", inputs=[args.rover_log], outputs=extract_outputs, status="generated", elapsed_s=time.perf_counter() - started)
    if getattr(args, "only_step", None) == "extract_receiver_products":
        return 0
    if _should_run_pipeline_step(args, "write_rinex_obs"):
        rinex_outputs = [rover_obs, out_dir / f"{base}.rover-gps.nav"]
        if getattr(args, "skip_existing", False) and not _forced_step(args, "write_rinex_obs") and _existing_outputs([rover_obs]):
            logging.info("pipeline step write_rinex_obs: reusing existing outputs")
            _record_pipeline_step(args, "write_rinex_obs", inputs=[args.rover_log], outputs=rinex_outputs, status="skipped", reused=True)
        else:
            started = time.perf_counter()
            logging.info("pipeline step 2/3: generate rover RINEX and navigation files")
            cmd_rinex(args)
            _record_pipeline_step(args, "write_rinex_obs", inputs=[args.rover_log], outputs=rinex_outputs, status="generated", elapsed_s=time.perf_counter() - started)
    if getattr(args, "only_step", None) == "write_rinex_obs":
        return 0
    if getattr(args, "only_step", None) == "quality":
        setattr(args, "quality_analyze", True)
        _run_quality_analysis_if_requested(args, out_dir, base, [], base_ecef=None, base_llh=None)
        _record_pipeline_step(args, "quality", inputs=[_rtklib_output_file(out_dir, base, _rtklib_output_formats(args)[0])], outputs=[out_dir / f"{base}-rtk.quality.json"], status="generated")
        return 0
    base_obs = [Path(item) for item in args.base_obs or []]
    converted_base_obs, base_rtcm_nav = _convert_base_rtcm_if_requested(args, out_dir, base)
    base_obs.extend(converted_base_obs)
    if args.station and args.download_base:
        base_started = time.perf_counter()
        logging.info("pipeline base download: deriving time span from %s", rover_obs)
        rover_span = read_rinex_obs_time_span(rover_obs)
        if rover_span.start is None or rover_span.end is None:
            logging.warning(
                "could not determine generated rover RINEX time span from %s; "
                "falling back to solution-record time span for base downloads",
                rover_obs,
            )
            with _time_phase(args, "base_download_staging"):
                base_obs.extend(_download_base_files(args))
        else:
            margin = timedelta(seconds=args.time_margin)
            logging.info(
                "pipeline base download window: %s to %s (margin=%ds)",
                rover_span.start - margin,
                rover_span.end + margin,
                args.time_margin,
            )
            with _time_phase(args, "base_download_staging"):
                base_obs.extend(_download_base_files_for_window(args, rover_span.start - margin, rover_span.end + margin))
        _record_pipeline_step(args, "resolve_base", inputs=[rover_obs], outputs=base_obs, status="generated", elapsed_s=time.perf_counter() - base_started)
    should_run_rtklib = bool(
        args.run_rtklib
        or args.rtkconf
        or getattr(args, "nav_file", None)
        or getattr(args, "nav_glob", None)
        or getattr(args, "base_nav_file", None)
        or getattr(args, "base_nav_glob", None)
        or getattr(args, "rover_nav_file", None)
        or getattr(args, "rover_nav_glob", None)
        or base_obs
    )
    if not should_run_rtklib:
        logging.warning(
            "pipeline generated extraction/RINEX products but did not run RTKLIB. "
            "Provide --run-rtklib with NAV data and --base-obs or --download-base."
        )
        return 0
    if not base_obs:
        raise ValueError("--base-obs or --download-base is required when pipeline runs RTKLIB")
    logging.info("pipeline step 3/3: run RTKLIB postprocessing")
    logging.info("checking rover/base observation time overlap: rover=%s base_files=%d", rover_obs, len(base_obs))
    base_obs, overlap_warnings = filter_rinex_obs_by_overlap(rover_obs, base_obs)
    for warning in overlap_warnings:
        logging.warning("%s", warning)
    logging.info("retained %d base observation files after overlap filtering", len(base_obs))
    _log_rover_base_capability_report(rover_obs, base_obs)
    generated_rover_nav = rover_nav_files(out_dir / f"{base}.rover-gps.nav")
    logging.info("found %d generated rover navigation sidecar files", len(generated_rover_nav))
    explicit_nav = _expand_nav_inputs(getattr(args, "nav_file", None), getattr(args, "nav_glob", None))
    base_nav = _expand_nav_inputs(getattr(args, "base_nav_file", None), getattr(args, "base_nav_glob", None))
    manual_rover_nav = _expand_nav_inputs(getattr(args, "rover_nav_file", None), getattr(args, "rover_nav_glob", None))
    rover_nav = _dedupe_paths([*manual_rover_nav, *generated_rover_nav])
    nav_source = _nav_source_for_resolver(args)
    nav_merge = _nav_merge_for_resolver(args)
    nav_resolution = resolve_nav_sources(
        explicit=explicit_nav,
        base=base_nav,
        base_rtcm=base_rtcm_nav,
        rover=rover_nav,
        rover_obs_systems=detect_rinex_obs_systems(rover_obs),
        base_obs_systems=set().union(*(detect_rinex_obs_systems(path) for path in base_obs)),
        nav_source=nav_source,
        merge_policy=nav_merge,
    )
    _log_nav_resolution(nav_resolution, source=getattr(args, "nav_source", "auto"), merge=nav_merge)
    for warning in nav_resolution.warnings:
        logging.warning("%s", warning)
    if not nav_resolution.selected:
        raise ValueError(nav_resolution.warnings[0])
    rnx2rtkp = resolve_rtklib_tool(args.rnx2rtkp, rtklib_dir=args.rtklib_dir)
    logging.info("resolved rnx2rtkp executable: %s", rnx2rtkp)
    logging.info("resolving base position")
    base_ecef, base_llh = _resolve_base_position(args, base_obs=base_obs)
    base_obs_arg = _prepare_rtklib_base_obs_argument(base_obs, out_dir, base)
    nav_files = _apply_sbas_source_policy(
        args,
        _dedupe_paths([candidate.path for candidate in nav_resolution.selected] + rover_nav),
    )
    inventory = _rtklib_input_inventory(rover_obs=rover_obs, base_obs=base_obs, nav_files=nav_files, args=args)
    _log_rtklib_input_inventory(inventory)
    if getattr(args, "analysis_json", False):
        _write_rtklib_inventory_analysis(out_dir, base, inventory)
    with _time_phase(args, "rtklib_run", output_formats=",".join(_rtklib_output_formats(args))):
        rtklib_started = time.perf_counter()
        commands = _run_rtklib_output_formats(
            args=args,
            rnx2rtkp=rnx2rtkp,
            out_dir=out_dir,
            basename=base,
            rover_obs=rover_obs,
            base_obs=base_obs,
            nav_files=nav_files,
            base_obs_arg=base_obs_arg,
            base_ecef=base_ecef,
            base_llh=base_llh,
        )
    _record_pipeline_step(args, "run_rtklib", inputs=[rover_obs, *base_obs, *nav_files], outputs=[_rtklib_output_file(out_dir, base, fmt) for fmt in _rtklib_output_formats(args)], status="generated", elapsed_s=time.perf_counter() - rtklib_started)
    quality_started = time.perf_counter()
    _run_quality_analysis_if_requested(args, out_dir, base, commands, base_ecef=base_ecef, base_llh=base_llh)
    if getattr(args, "quality_analyze", False):
        _record_pipeline_step(args, "quality", inputs=[_rtklib_output_file(out_dir, base, _rtklib_output_formats(args)[0])], outputs=[out_dir / f"{base}-rtk.quality.json", out_dir / f"{base}-rtk.quality.md"], status="generated", elapsed_s=time.perf_counter() - quality_started)
    for command in commands:
        print(format_command(command.args))
    return 0


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def _is_sbs_path(path: Path) -> bool:
    """Return true when `path` appears to be an RTKLIB SBAS sidecar."""

    if path.suffix.lower() == ".sbs":
        return True
    return path.exists() and classify_rinex_file(path) == "sbs"


def _apply_sbas_source_policy(args: argparse.Namespace, nav_files: list[Path]) -> list[Path]:
    """Filter/add RTKLIB SBAS correction sidecars according to CLI policy."""

    policy = getattr(args, "sbas_source", "auto")
    explicit = [Path(path) for path in getattr(args, "sbas_file", None) or []]
    non_sbs = [path for path in nav_files if not _is_sbs_path(path)]
    discovered = [path for path in nav_files if _is_sbs_path(path)]

    if policy == "off":
        if discovered or explicit:
            logging.info(
                "SBAS correction source disabled; not passing %d .sbs file(s) to RTKLIB",
                len(discovered) + len(explicit),
            )
        return non_sbs
    if policy == "external":
        if not explicit:
            raise ValueError("--sbas-source external requires --sbas-file")
        logging.info("SBAS correction source=external files=%s", ",".join(str(path) for path in explicit))
        return _dedupe_paths([*non_sbs, *explicit])
    if policy == "rover":
        if not discovered:
            raise ValueError("--sbas-source rover requested, but no rover-derived .sbs sidecar was generated")
        logging.info("SBAS correction source=rover files=%s", ",".join(str(path) for path in discovered))
        return _dedupe_paths([*non_sbs, *discovered])
    if policy == "base":
        raise ValueError("--sbas-source base is not implemented; provide --sbas-file or use --sbas-source auto/off")
    if policy != "auto":
        raise ValueError(f"unsupported SBAS source policy: {policy}")
    if explicit:
        logging.info("SBAS correction source=external files=%s", ",".join(str(path) for path in explicit))
        return _dedupe_paths([*non_sbs, *explicit])
    if discovered:
        logging.info("SBAS correction source=rover files=%s", ",".join(str(path) for path in discovered))
        return _dedupe_paths([*non_sbs, *discovered])
    logging.info("SBAS correction source=auto: no valid .sbs sidecar available; RTKLIB will run without SBAS corrections")
    return non_sbs


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""

    parser = argparse.ArgumentParser(prog="um980-ppk")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init_sub = init.add_subparsers(dest="init_command", required=True)
    gen = init_sub.add_parser("generate")
    gen.add_argument("--port")
    gen.add_argument("--baud", type=int, choices=[115200, 230400, 460800, 921600])
    gen.add_argument("--mode", choices=["rover", "base"])
    gen.add_argument("--base-lat", type=float)
    gen.add_argument("--base-lon", type=float)
    gen.add_argument("--base-height", type=float)
    gen.add_argument("--nmea", action="append")
    gen.add_argument("--nmea-preset", choices=sorted(NMEA_PRESETS))
    gen.add_argument(
        "--solution-hz",
        type=float,
        help="Set GNGGA and GNRMC solution output frequency in Hz, e.g. 1, 2, 4, 5, 10, or 20.",
    )
    gen.add_argument("--raw-format", choices=["none", "obsvma", "obsvmb", "obsvmcmpb"])
    gen.add_argument("--raw-hz", type=float)
    gen.add_argument("--raw-period", type=float)
    gen.add_argument("--bestnav-format", choices=["none", "ascii", "binary"], default=None)
    gen.add_argument("--bestnav-hz", type=float, help="Emit BESTNAVA/B at this frequency in Hz.")
    gen.add_argument("--expected-sats", type=int)
    gen.add_argument("--expected-obs-per-epoch", type=int)
    gen.add_argument("--ephemeris")
    gen.add_argument("--ephemeris-systems")
    gen.add_argument("--ephemeris-format", choices=["ascii", "binary"])
    gen.add_argument(
        "--debug-ascii-ephemeris",
        action="store_true",
        help=(
            "Enable all ASCII ephemeris messages every 300 s for short debugging "
            "captures; this can create large .unc files."
        ),
    )
    gen.add_argument("--ppp", choices=["none", "e6-has", "b2b-ppp", "ssr-rx"])
    gen.add_argument("--ppp-datum", choices=["WGS84", "PPPORIGINAL"])
    gen.add_argument("--ppp-timeout", type=int, help="PPP timeout in seconds; defaults to 120 when PPP is enabled.")
    gen.add_argument(
        "--ppp-converge",
        help="PPP convergence thresholds as 'horizontal,vertical'; defaults to 15,30 when PPP is enabled.",
    )
    gen.add_argument(
        "--include-tropinfo",
        action="store_true",
        help="Emit selected-format TROPINFO ONCE and ONCHANGED; requires --ppp.",
    )
    gen.add_argument(
        "--diagnostic-format",
        choices=["ascii", "binary"],
        help="Use A or B suffixed TROPINFO/ION diagnostics; default is ascii.",
    )
    gen.add_argument(
        "--ion",
        help="Comma-separated ionosphere families to emit as ONCHANGED: gps,bds,bd3,gal.",
    )
    gen.add_argument(
        "--ion-period",
        type=float,
        help="Also repeat selected ionosphere messages every N seconds, e.g. 300.",
    )
    gen.add_argument("--include-ion", action="store_true", help="Enable all ionosphere families.")
    gen.add_argument("--include-gpsion", action="store_true", help="Compatibility shortcut for --ion gps.")
    gen.add_argument(
        "--utc",
        help="Comma-separated UTC/time-system families to emit as ONCHANGED: gps,bds,bd3,gal.",
    )
    gen.add_argument(
        "--utc-period",
        type=float,
        help="Also repeat selected UTC/time-system messages every N seconds, e.g. 300.",
    )
    gen.add_argument("--include-utc", action="store_true", help="Enable all UTC/time-system families.")
    gen.add_argument(
        "--sbas",
        choices=sorted(SBAS_MODES),
        help="Explicit SBAS receiver mode for generated init commands; default is off unless configured.",
    )
    gen.add_argument(
        "--sbas-timeout",
        type=int,
        help="Generate CONFIG SBAS TIMEOUT seconds; UM980 accepts 0 or 120..1800 on supported firmware.",
    )
    gen.add_argument("--save-config", action="store_true")
    gen.add_argument("--no-save-config", action="store_true")
    gen.add_argument("--strict-bitrate", action="store_true")
    gen.add_argument("--allow-overload", action="store_true")
    gen.add_argument("--out")
    gen.add_argument("--json")
    _add_common(gen)
    gen.set_defaults(func=cmd_init_generate)

    for name, func in (
        ("analyze", cmd_analyze),
        ("parse-rover", cmd_analyze),
        ("extract", cmd_extract),
        ("rinex", cmd_rinex),
        ("nav", cmd_rinex),
    ):
        p = sub.add_parser(name)
        p.add_argument("rover_log")
        _add_common(p)
        _add_time_window_args(p)
        _add_step_control_args(p)
        _add_emit_ion_utc_arg(p)
        if name in {"extract", "rinex", "nav"}:
            p.add_argument("--solution", choices=["all", "csv", "gpx", "nmea", "none"], default="all")
            p.add_argument(
                "--position-nmea",
                choices=["none", "all", "best"],
                default="best",
                help=(
                    "Write <basename>.position.nmea from original position sentences. "
                    "best keeps GGA/GNS over RMC per timestamp; all keeps every usable GGA/GNS/RMC."
                ),
            )
            _add_track_source_arg(p)
            p.add_argument("--obs-csv", "--write-observation-csv", dest="obs_csv", action="store_true")
            p.add_argument("--raw-output", choices=["none", "ascii", "binary", "all"], default="none")
            p.add_argument("--rinex-version", default="3.04")
            p.add_argument("--rinex-compat", choices=["native", "convbin"], default="native")
            if name == "extract":
                _add_bestnav_nmea_args(p)
        if name == "nav":
            p.set_defaults(func=func, solution="none", position_nmea="none")
        else:
            p.set_defaults(func=func)

    base_candidates = sub.add_parser("base-candidates")
    base_candidates.add_argument("rover_log")
    base_candidates.add_argument("--network", choices=["euref"], default="euref")
    base_candidates.add_argument("--radius-km", type=float, default=150.0)
    base_candidates.add_argument("--max-candidates", type=int, default=10)
    base_candidates.add_argument("--stations", help="Optional comma-separated station aliases/markers to evaluate.")
    base_candidates.add_argument("--base-resolution", choices=["low", "high", "auto"], default="auto")
    base_candidates.add_argument("--allow-resolution-fallback", choices=["yes", "no"], default="yes")
    base_candidates.add_argument("--nav-source", choices=["rover", "base", "auto-prefer-base", "merge"], default="auto-prefer-base")
    base_candidates.add_argument("--require-constellations", help="Comma-separated required rover/base constellations for advisory scoring.")
    base_candidates.add_argument("--require-nav", action="store_true", help="Penalise candidates when requested NAV source is not known to be available.")
    base_candidates.add_argument("--format", choices=["table", "markdown", "json"], default="table")
    base_candidates.add_argument("--out")
    base_candidates.add_argument("--refresh-station-catalog", action="store_true", help="Refresh the cached official EPN SSC station catalogue.")
    base_candidates.add_argument("--station-catalog-source", choices=["auto", "cache", "epn-latest", "curated"], default="auto")
    base_candidates.add_argument("--station-catalog-cache", help="Station catalogue JSON cache path.")
    base_candidates.add_argument("--probe-archives", action="store_true", help="Probe planned archive URLs with lightweight HEAD requests.")
    base_candidates.add_argument("--download-headers-only", action="store_true", help="Probe mode placeholder: do not download full observation bodies.")
    base_candidates.add_argument("--refresh-probes", action="store_true", help="Ignore cached archive probe results.")
    base_candidates.add_argument("--probe-cache-dir", help="Archive probe cache directory.")
    _add_track_source_arg(base_candidates)
    _add_bestnav_nmea_args(base_candidates)
    _add_time_window_args(base_candidates)
    _add_emit_ion_utc_arg(base_candidates)
    _add_common(base_candidates)
    base_candidates.set_defaults(
        func=cmd_base_candidates,
        solution="none",
        position_nmea="none",
        obs_csv=False,
        raw_output="none",
        rinex_version="3.04",
        rinex_compat="native",
    )

    for quality_name in ("quality-analyze", "quality"):
        quality = sub.add_parser(quality_name)
        quality.add_argument("--solution", help="RTKLIB NMEA/POS/LLH solution output.")
        quality.add_argument("--solution-type", choices=["nmea", "pos", "auto"], default="auto", help="Accepted for rerun-script compatibility; auto detects from file contents.")
        quality.add_argument("--stat", help="Optional RTKLIB .stat file.")
        quality.add_argument("--out-md", help="Markdown report output path.")
        quality.add_argument("--out-json", help="JSON report output path.")
        quality.add_argument("--compare-json", nargs=2, metavar=("LEFT", "RIGHT"), help="Compare two existing quality JSON reports.")
        quality.add_argument("--format", choices=["text", "markdown", "json"], default="text")
        _add_quality_trace_args(quality, standalone=True)
        _add_quality_analyze_args(quality)
        _add_base_position_args(quality)
        _add_time_window_args(quality)
        _add_common(quality)
        quality.set_defaults(func=cmd_quality_analyze, _quality_alias=quality_name)

    quality_compare = sub.add_parser("quality-compare")
    quality_compare.add_argument("reports", nargs="+", help="Quality JSON reports; first report is the baseline.")
    quality_compare.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    quality_compare.add_argument("--out", help="Optional comparison output path.")
    _add_common(quality_compare)
    quality_compare.set_defaults(func=cmd_quality_compare)

    opt = sub.add_parser("optimize-settings")
    opt.add_argument("rover_log", nargs="+")
    opt.add_argument("--base", help="Primary base station or base file for the baseline variant.")
    opt.add_argument("--bases", help="Comma-separated base stations/files to compare.")
    opt.add_argument("--base-list", help="Small CSV/text file with one base station/file per line.")
    opt.add_argument("--base-candidates-json", help="Consume base-candidates JSON and add its top ranked stations.")
    opt.add_argument("--top-bases", type=int, default=3)
    opt.add_argument("--base-resolution", choices=["low", "high", "auto"], default="auto")
    opt.add_argument("--allow-resolution-fallback", choices=["yes", "no"], default="yes")
    opt.add_argument(
        "--nav-source",
        choices=["rover", "base", "auto-prefer-base", "merge", "auto", "explicit", "external", "none"],
        default="auto-prefer-base",
    )
    opt.add_argument("--sbas-source", choices=["off", "rover", "base", "external", "auto"], default="auto")
    _add_emit_ion_utc_arg(opt)
    _add_time_window_args(opt)
    opt.add_argument("--sample-count", type=int, default=4)
    opt.add_argument("--sample-duration", default="120s", help="Sample duration such as 120s, 5m, or 1h.")
    opt.add_argument("--max-variants", type=int, default=6)
    opt.add_argument("--max-runs", type=int, default=24)
    opt.add_argument("--format", choices=["table", "markdown", "json"], default="table")
    opt.add_argument("--out")
    opt.add_argument("--execute", action="store_true", help="Execute the bounded plan. Default is dry-run planning only.")
    opt.add_argument("--keep-intermediate", choices=["yes", "no"], default="no")
    _add_common(opt)
    opt.set_defaults(func=cmd_optimize_settings)

    for base_step in ("download-base", "resolve-base"):
        dl = sub.add_parser(base_step)
        dl.add_argument("rover_log")
        _add_base_download_args(dl, require_station=True)
        _add_time_window_args(dl)
        _add_step_control_args(dl)
        _add_common(dl)
        dl.set_defaults(func=cmd_download_base)

    rec = sub.add_parser("record-base-rt")
    rec.add_argument("--caster", required=True)
    rec.add_argument("--port", type=int, default=2101)
    rec.add_argument("--mountpoint", required=True)
    rec.add_argument("--user")
    rec.add_argument("--password")
    rec.add_argument("--station")
    rec.add_argument("--rtklib-dir")
    rec.add_argument("--str2str", default="str2str")
    rec.add_argument("--rtklib-path-style", choices=["auto", "unix", "windows"], default="auto")
    _add_common(rec)
    rec.set_defaults(func=cmd_record_base_rt, out_dir="base-recordings")

    sourcetable = sub.add_parser("ntrip-sourcetable")
    sourcetable.add_argument("--caster", required=True)
    sourcetable.add_argument("--port", type=int, default=2101)
    sourcetable.add_argument("--out", required=True)
    sourcetable.add_argument("--contains", action="append")
    sourcetable.add_argument("--user")
    sourcetable.add_argument("--password")
    _add_common(sourcetable)
    sourcetable.set_defaults(func=cmd_ntrip_sourcetable)

    for post_name in ("postprocess", "run-rtklib"):
        post = sub.add_parser(post_name)
        post.add_argument("rover_log")
        post.add_argument("--rover-obs", required=True)
        post.add_argument("--nav-file", action="append")
        post.add_argument("--nav-glob", action="append")
        post.add_argument("--base-nav-file", action="append")
        post.add_argument("--base-nav-glob", action="append")
        post.add_argument("--rover-nav-file", action="append")
        post.add_argument("--rover-nav-glob", action="append")
        post.add_argument(
            "--nav-source",
            choices=["auto", "explicit", "base", "rover", "external", "none", "auto-prefer-base", "merge"],
            default="auto",
            help="NAV source policy. auto prefers explicit, then base, rover, external. auto-prefer-base is an alias for auto; merge selects best-per-system.",
        )
        post.add_argument("--nav-provider", choices=["auto", "custom", "none"], default="auto")
        post.add_argument("--download-nav", action="store_true")
        post.add_argument("--no-download-nav", action="store_true")
        post.add_argument("--use-rover-nav", action="store_true")
        post.add_argument("--no-use-rover-nav", action="store_true")
        post.add_argument("--nav-merge", choices=["off", "best-per-system", "all"], default="best-per-system")
        _add_rtklib_processing_args(post, require_base_obs=False)
        _add_quality_pipeline_args(post)
        _add_base_position_args(post)
        _add_time_window_args(post)
        _add_rerun_args(post)
        _add_step_control_args(post)
        _add_common(post)
        post.set_defaults(func=cmd_postprocess)

    cleanup = sub.add_parser("cleanup")
    _add_time_window_args(cleanup)
    _add_step_control_args(cleanup)
    _add_common(cleanup)
    cleanup.set_defaults(func=cmd_cleanup)

    pipe = sub.add_parser("pipeline")
    pipe.add_argument("rover_log")
    pipe.add_argument("--download-base", action="store_true")
    pipe.add_argument("--base-obs", action="append")
    pipe.add_argument(
        "--base-rtcm",
        help="Recorded real-time base RTCM3 stream; converted with convbin and used as the RTKLIB base observation input.",
    )
    pipe.add_argument("--nav-file", action="append")
    pipe.add_argument("--nav-glob", action="append")
    pipe.add_argument("--base-nav-file", action="append")
    pipe.add_argument("--base-nav-glob", action="append")
    pipe.add_argument("--rover-nav-file", action="append")
    pipe.add_argument("--rover-nav-glob", action="append")
    pipe.add_argument(
        "--nav-source",
        choices=["auto", "explicit", "base", "rover", "external", "none", "auto-prefer-base", "merge"],
        default="auto",
        help="NAV source policy. auto prefers explicit, then base, rover, external. auto-prefer-base is an alias for auto; merge selects best-per-system.",
    )
    pipe.add_argument("--nav-merge", choices=["off", "best-per-system", "all"], default="best-per-system")
    pipe.add_argument("--run-rtklib", action="store_true")
    pipe.add_argument("--rtklib-dir")
    pipe.add_argument("--rnx2rtkp", default="rnx2rtkp")
    pipe.add_argument("--convbin")
    pipe.add_argument("--rtkconf")
    pipe.add_argument("--rtklib-path-style", choices=["auto", "unix", "windows"], default="auto")
    pipe.add_argument(
        "--output-format",
        action="append",
        default=None,
        metavar="FORMAT",
        help=(
            "RTKLIB solution output format: pos, llh, or nmea. Repeat the option "
            "or pass a comma-separated list to run rnx2rtkp once per format. pos "
            "is the standard .pos file suffix for LLH content; nmea passes "
            "rnx2rtkp -n, not only a .nmea filename suffix."
        ),
    )
    pipe.add_argument("--rtk-pos-mode", choices=sorted(RTK_POS_MODE_CODES), default="kinematic")
    pipe.add_argument("--rtk-frequency", choices=sorted(RTK_FREQUENCY_CODES), default="l1+l2+l5")
    pipe.add_argument("--navsys", choices=sorted(RTK_NAVSYS_PRESETS), default="all")
    pipe.add_argument("--rtk-navsys")
    pipe.add_argument("--rtk-elevation-mask", type=float, default=10.0)
    pipe.add_argument("--rtk-soltype", choices=["forward", "backward", "combined"], default="combined")
    pipe.add_argument("--rtk-ar-mode", choices=["continuous", "instantaneous", "fix-and-hold"], default="continuous")
    pipe.add_argument("--rnx2rtkp-option", action="append", default=[])
    pipe.add_argument(
        "--rtklib-trace-level",
        type=int,
        choices=range(0, 6),
        metavar="0..5",
        help="Pass rnx2rtkp -x LEVEL to write a debug trace file, e.g. 4 for AR/residual diagnostics.",
    )
    pipe.add_argument(
        "--rtklib-stat-level",
        type=int,
        choices=[0, 1, 2],
        metavar="0..2",
        help="Pass rnx2rtkp -y LEVEL to write solution status details; 2 includes residuals.",
    )
    _add_auto_sat_qc_args(pipe)
    pipe.add_argument("--obs-csv", "--write-observation-csv", dest="obs_csv", action="store_true", default=True)
    pipe.add_argument("--no-write-observation-csv", dest="obs_csv", action="store_false")
    pipe.add_argument("--solution", choices=["all", "csv", "gpx", "nmea", "none"], default="all")
    _add_track_source_arg(pipe)
    pipe.add_argument(
        "--position-nmea",
        choices=["none", "all", "best"],
        default="best",
        help=(
            "Write <basename>.position.nmea from original position sentences. "
            "best keeps GGA/GNS over RMC per timestamp; all keeps every usable GGA/GNS/RMC."
        ),
    )
    pipe.add_argument("--raw-output", choices=["none", "ascii", "binary", "all"], default="all")
    pipe.add_argument("--rinex-version", default="3.04")
    pipe.add_argument("--rinex-compat", choices=["native", "convbin"], default="convbin")
    _add_time_window_args(pipe)
    _add_emit_ion_utc_arg(pipe)
    _add_sbas_source_args(pipe)
    _add_bestnav_nmea_args(pipe)
    _add_base_download_args(pipe, require_station=False, include_rtklib_dir=False)
    _add_quality_pipeline_args(pipe)
    _add_base_position_args(pipe)
    _add_rerun_args(pipe)
    _add_common(pipe)
    pipe.set_defaults(func=cmd_pipeline)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI entrypoint.

    Args:
        argv: Optional argument vector. Uses `sys.argv` when omitted.

    Returns:
        Process exit code.
    """

    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    duplicate_options = _scan_duplicate_options(raw_argv)
    args = parser.parse_args(raw_argv)
    setattr(args, "_duplicate_options", duplicate_options)
    setattr(args, "_original_argv", raw_argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
