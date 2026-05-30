"""Command line interface for um980-ppk."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .badsat import BadSatConfig, choose_bad_sats, compute_sat_metrics, parse_rtklib_stat
from .badsat_report import write_badsat_json_report, write_badsat_markdown_report
from .base_rt import convert_rtcm_to_rinex, fetch_ntrip_sourcetable, record_ntrip_base
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
    ensure_out_dir,
    filter_rinex_obs_by_overlap,
    read_rinex_obs_capabilities,
    read_rinex_obs_time_span,
)
from .initgen import (
    InitProfile,
    ION_MESSAGES,
    NMEA_PRESETS,
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
from .quality import build_analysis, write_analysis_json
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
    SolutionPoint,
    extract_solutions,
    position_nmea_records,
    write_all_records_csv,
    write_gpx,
    write_lines,
    write_solution_csv,
    write_solution_nmea,
)
from .stream import parse_stream

BASE_PROVIDER_CHOICES = ("bev-nrt", "bkg-euref-nrt", "bkg-euref-highrate", "bkg-igs-highrate")
BASE_RATE_HIGH = "1s"
BASE_RATE_LOW = "30s"
HIGH_RATE_ARCHIVE_MARGIN_S = 300
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


def _verbose_enabled(args: argparse.Namespace) -> bool:
    """Return true when progress logging should be enabled."""

    return bool(getattr(args, "verbose", False) or _debug_enabled(args))


def _configure_cli_logging(args: argparse.Namespace) -> None:
    """Configure CLI logging from common arguments."""

    configure_logging(_verbose_enabled(args), args.log_file, debug=_debug_enabled(args))


def _human_bytes(size: int) -> str:
    """Return a compact human-readable byte count."""

    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


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
    rover = Path(args.rover_log)
    progress = logging.getLogger().isEnabledFor(logging.INFO)
    records, stream_diag = _load_records(rover)
    logging.info("extracting solution records")
    solutions = extract_solutions(records, progress=progress)
    logging.info(
        "extracted solutions: points=%d nmea_records=%d all_nmea=%d",
        len(solutions.solution_points),
        len(solutions.solution_records),
        len(solutions.all_nmea),
    )
    logging.info("decoding raw observations")
    observations = decode_observations(records, progress=progress)
    logging.info(
        "decoded raw observations: observations=%d epochs=%s unsupported=%d",
        len(observations.observations),
        observations.metrics.get("epochs", 0),
        sum(observations.unsupported_records.values()),
    )
    logging.info("scanning rover navigation records")
    rover_nav = extract_rover_nav(records)
    converted_nav = sum(rover_nav.converted.values())
    logging.info("scanned rover navigation: converted=%d warnings=%d", converted_nav, len(rover_nav.warnings))
    logging.info("scanning BESTNAV receiver-solution records")
    bestnav = extract_bestnav_records(records)
    logging.info(
        "decoded BESTNAV records: present=%d valid_epochs=%d malformed=%d",
        sum(bestnav.present.values()),
        len(bestnav.records),
        sum(bestnav.malformed.values()),
    )
    logging.info("scanning ION/UTC/TROPINFO diagnostics")
    diagnostics = extract_diagnostics(records)
    logging.info(
        "preserved diagnostics: records=%d malformed=%d present_not_converted=%d",
        len(diagnostics.records),
        sum(diagnostics.malformed.values()),
        sum(diagnostics.present_not_converted.values()),
    )
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
            "diagnostics": diagnostics.as_dict(),
            "message_stats": message_stats.as_dict(),
        },
    )
    analysis["warnings"] = list(dict.fromkeys([*analysis.get("warnings", []), *bestnav.warnings, *message_stats.warnings]))
    return rover, records, stream_diag, solutions, observations, rover_nav, bestnav, message_stats, analysis


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
    trace_level = getattr(args, "rtklib_trace_level", None)
    if trace_level is not None:
        options.extend(["-x", str(trace_level)])
    stat_level = getattr(args, "rtklib_stat_level", None)
    if stat_level is not None:
        options.extend(["-y", str(stat_level)])
    return options


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
        )
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
        commands.append(command)
    return commands


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
    _log_analysis_warnings(analysis)
    _print_analysis_summary(analysis)
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
        write_solution_nmea(solution_nmea, solutions.solution_points)
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
        write_observations_csv(obs_csv, observations.observations)
        logging.info("wrote observation CSV: %s", obs_csv)
    if getattr(args, "bestnav_nmea", None):
        _write_bestnav_nmea(Path(args.bestnav_nmea), bestnav, args)
    if args.analysis_json:
        analysis_path = out_dir / f"{base}.analysis.json"
        write_analysis_json(analysis_path, analysis)
        logging.info("wrote analysis JSON: %s", analysis_path)
    _log_analysis_warnings(analysis)
    if args.verbose:
        _print_analysis_summary(analysis)
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
        write_observations_csv(obs_csv, observations.observations)
        logging.info("wrote observation CSV: %s", obs_csv)
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
    _log_analysis_warnings(analysis)
    if args.verbose:
        _print_analysis_summary(analysis)
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


def cmd_postprocess(args: argparse.Namespace) -> int:
    """Handle `postprocess`.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """

    _configure_cli_logging(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = args.basename or Path(args.rover_log).stem
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
    nav_resolution = resolve_nav_sources(
        explicit=args.nav_file,
        base_rtcm=base_rtcm_nav,
        rover=rover_nav,
        observed_systems=set(),
        merge_policy=args.nav_merge,
    )
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
    commands = _run_rtklib_output_formats(
        args=args,
        rnx2rtkp=rnx2rtkp,
        out_dir=out_dir,
        basename=base,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=_dedupe_paths([candidate.path for candidate in nav_resolution.selected] + rover_nav),
        base_obs_arg=base_obs_arg,
        base_ecef=base_ecef,
        base_llh=base_llh,
    )
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
    if getattr(args, "base_rtcm", None) and args.download_base:
        raise ValueError("--base-rtcm and --download-base are mutually exclusive; choose one base source")
    logging.info("pipeline step 1/3: extract receiver products")
    cmd_extract(args)
    logging.info("pipeline step 2/3: generate rover RINEX and navigation files")
    cmd_rinex(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(args.rover_log, args.basename)
    rover_obs = out_dir / f"{base}.direct.obs"
    base_obs = [Path(item) for item in args.base_obs or []]
    converted_base_obs, base_rtcm_nav = _convert_base_rtcm_if_requested(args, out_dir, base)
    base_obs.extend(converted_base_obs)
    if args.station and args.download_base:
        logging.info("pipeline base download: deriving time span from %s", rover_obs)
        rover_span = read_rinex_obs_time_span(rover_obs)
        if rover_span.start is None or rover_span.end is None:
            logging.warning(
                "could not determine generated rover RINEX time span from %s; "
                "falling back to solution-record time span for base downloads",
                rover_obs,
            )
            base_obs.extend(_download_base_files(args))
        else:
            margin = timedelta(seconds=args.time_margin)
            logging.info(
                "pipeline base download window: %s to %s (margin=%ds)",
                rover_span.start - margin,
                rover_span.end + margin,
                args.time_margin,
            )
            base_obs.extend(_download_base_files_for_window(args, rover_span.start - margin, rover_span.end + margin))
    should_run_rtklib = bool(args.run_rtklib or args.rtkconf or args.nav_file or base_obs)
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
    nav_resolution = resolve_nav_sources(
        explicit=args.nav_file,
        base_rtcm=base_rtcm_nav,
        rover=generated_rover_nav,
        observed_systems=set(),
        merge_policy=args.nav_merge,
    )
    for warning in nav_resolution.warnings:
        logging.warning("%s", warning)
    if not nav_resolution.selected:
        raise ValueError(nav_resolution.warnings[0])
    rnx2rtkp = resolve_rtklib_tool(args.rnx2rtkp, rtklib_dir=args.rtklib_dir)
    logging.info("resolved rnx2rtkp executable: %s", rnx2rtkp)
    logging.info("resolving base position")
    base_ecef, base_llh = _resolve_base_position(args, base_obs=base_obs)
    base_obs_arg = _prepare_rtklib_base_obs_argument(base_obs, out_dir, base)
    commands = _run_rtklib_output_formats(
        args=args,
        rnx2rtkp=rnx2rtkp,
        out_dir=out_dir,
        basename=base,
        rover_obs=rover_obs,
        base_obs=base_obs,
        nav_files=_dedupe_paths([candidate.path for candidate in nav_resolution.selected] + generated_rover_nav),
        base_obs_arg=base_obs_arg,
        base_ecef=base_ecef,
        base_llh=base_llh,
    )
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
    gen.add_argument("--save-config", action="store_true")
    gen.add_argument("--no-save-config", action="store_true")
    gen.add_argument("--strict-bitrate", action="store_true")
    gen.add_argument("--allow-overload", action="store_true")
    gen.add_argument("--out")
    gen.add_argument("--json")
    _add_common(gen)
    gen.set_defaults(func=cmd_init_generate)

    for name, func in (("analyze", cmd_analyze), ("extract", cmd_extract), ("rinex", cmd_rinex)):
        p = sub.add_parser(name)
        p.add_argument("rover_log")
        _add_common(p)
        if name in {"extract", "rinex"}:
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
            p.add_argument("--track-source", choices=["auto", "nmea", "ppp", "adr", "gga"], default="auto")
            p.add_argument("--obs-csv", action="store_true")
            p.add_argument("--raw-output", choices=["none", "ascii", "binary", "all"], default="none")
            p.add_argument("--rinex-version", default="3.04")
            p.add_argument("--rinex-compat", choices=["native", "convbin"], default="native")
            if name == "extract":
                _add_bestnav_nmea_args(p)
        p.set_defaults(func=func)

    dl = sub.add_parser("download-base")
    dl.add_argument("rover_log")
    _add_base_download_args(dl, require_station=True)
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

    post = sub.add_parser("postprocess")
    post.add_argument("rover_log")
    post.add_argument("--rover-obs", required=True)
    post.add_argument("--nav-file", action="append")
    post.add_argument("--nav-glob", action="append")
    post.add_argument("--nav-provider", choices=["auto", "custom", "none"], default="auto")
    post.add_argument("--download-nav", action="store_true")
    post.add_argument("--no-download-nav", action="store_true")
    post.add_argument("--use-rover-nav", action="store_true")
    post.add_argument("--no-use-rover-nav", action="store_true")
    post.add_argument("--nav-merge", choices=["best-per-system", "all"], default="best-per-system")
    _add_rtklib_processing_args(post, require_base_obs=False)
    _add_base_position_args(post)
    _add_common(post)
    post.set_defaults(func=cmd_postprocess)

    pipe = sub.add_parser("pipeline")
    pipe.add_argument("rover_log")
    pipe.add_argument("--download-base", action="store_true")
    pipe.add_argument("--base-obs", action="append")
    pipe.add_argument(
        "--base-rtcm",
        help="Recorded real-time base RTCM3 stream; converted with convbin and used as the RTKLIB base observation input.",
    )
    pipe.add_argument("--nav-file", action="append")
    pipe.add_argument("--nav-merge", choices=["best-per-system", "all"], default="best-per-system")
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
    pipe.add_argument("--obs-csv", action="store_true", default=True)
    pipe.add_argument("--solution", choices=["all", "csv", "gpx", "nmea", "none"], default="all")
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
    _add_bestnav_nmea_args(pipe)
    _add_base_download_args(pipe, require_station=False, include_rtklib_dir=False)
    _add_base_position_args(pipe)
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
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
