"""Command line interface for um980-ppk."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from .config import deep_get, load_config
from .euref import download_urls, normalise_rinex_file, planned_urls, resolve_station
from .files import basename_for, ensure_out_dir
from .initgen import (
    InitProfile,
    NMEA_PRESETS,
    ephemeris_policy,
    parse_nmea_overrides,
    render_init_script,
    write_json_report,
)
from .logging_config import configure_logging
from .nav_resolver import resolve_nav_sources
from .obs_decode import decode_observations, write_observations_csv
from .quality import build_analysis, write_analysis_json
from .rinex_nav import extract_rover_nav
from .rinex_obs import write_rinex_obs
from .rtklib import run_rnx2rtkp
from .solution import (
    extract_solutions,
    write_all_records_csv,
    write_gpx,
    write_lines,
    write_solution_csv,
    write_solution_nmea,
)
from .stream import parse_stream


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--out-dir")
    parser.add_argument("--basename")
    parser.add_argument("--analysis-json", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file")


def _load_records(path: Path):
    data = path.read_bytes()
    return parse_stream(data)


def _extract_bundle(args: argparse.Namespace):
    rover = Path(args.rover_log)
    records, stream_diag = _load_records(rover)
    solutions = extract_solutions(records)
    observations = decode_observations(records)
    rover_nav = extract_rover_nav(records)
    analysis = build_analysis(
        stream=stream_diag,
        solutions=solutions,
        observations=observations,
        rover_nav=rover_nav,
    )
    return rover, records, stream_diag, solutions, observations, rover_nav, analysis


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


def _profile_from_args(args: argparse.Namespace) -> InitProfile:
    config = load_config(args.config)
    raw_cfg = config.get("raw", {}) if isinstance(config.get("raw", {}), dict) else {}
    ppp_cfg = config.get("ppp", {}) if isinstance(config.get("ppp", {}), dict) else {}
    diag_cfg = config.get("diagnostics", {}) if isinstance(config.get("diagnostics", {}), dict) else {}
    eph_cfg = config.get("ephemeris", {}) if isinstance(config.get("ephemeris", {}), dict) else {}

    preset = args.nmea_preset or config.get("nmea_preset") or deep_get(config, "nmea", "preset", default="minimal")
    nmea = dict(NMEA_PRESETS[preset])
    nmea.update(parse_nmea_overrides(args.nmea))
    overrides = deep_get(config, "nmea", "overrides", default={})
    if isinstance(overrides, dict):
        nmea.update({str(k).upper(): float(v) for k, v in overrides.items()})

    raw_hz = args.raw_hz
    if raw_hz is None and args.raw_period:
        raw_hz = 1.0 / args.raw_period if args.raw_period else 0.0
    if raw_hz is None:
        raw_hz = float(raw_cfg.get("hz", 0.0))

    eph_policy = args.ephemeris
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
        expected_obs_per_epoch=int(
            args.expected_obs_per_epoch or raw_cfg.get("expected_obs_per_epoch", 100)
        ),
        ephemeris=ephemeris_policy(eph_policy or "off", [system.strip() for system in systems]),
        ppp=(args.ppp or ppp_cfg.get("mode", "none")).lower(),
        ppp_datum=args.ppp_datum or ppp_cfg.get("datum", "WGS84"),
        ppp_timeout=args.ppp_timeout or ppp_cfg.get("timeout"),
        ppp_converge=converge,
        include_tropinfo=bool(args.include_tropinfo or diag_cfg.get("tropinfo", False)),
        include_gpsion=bool(args.include_gpsion or diag_cfg.get("gpsion", False)),
        save_config=bool(args.save_config or config.get("save_config", False)),
    )


def cmd_init_generate(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    profile = _profile_from_args(args)
    script, estimate = render_init_script(
        profile,
        strict_bitrate=args.strict_bitrate,
        allow_overload=args.allow_overload,
    )
    if args.out:
        Path(args.out).write_text(script, encoding="ascii")
    else:
        print(script, end="")
    if args.json:
        write_json_report(Path(args.json), profile, estimate)
    if args.verbose:
        print(json.dumps(estimate.as_dict(), indent=2, sort_keys=True))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    rover, _, _, solutions, observations, rover_nav, analysis = _extract_bundle(args)
    if args.analysis_json:
        out_dir = ensure_out_dir(args.out_dir)
        base = basename_for(rover, args.basename)
        write_analysis_json(out_dir / f"{base}.analysis.json", analysis)
    _log_analysis_warnings(analysis)
    _print_analysis_summary(analysis)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    rover, _, _, solutions, observations, rover_nav, analysis = _extract_bundle(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(rover, args.basename)

    solution = args.solution
    if solution in {"all", "nmea"}:
        write_lines(out_dir / f"{base}.clean.nmea", solutions.clean_nmea)
        write_solution_nmea(out_dir / f"{base}.solution.nmea", solutions.solution_points)
    if solution in {"all", "csv"}:
        write_solution_csv(out_dir / f"{base}.solution.csv", solutions.solution_points)
        write_all_records_csv(out_dir / f"{base}.solution_all_records.csv", solutions.all_rows)
    if solution in {"all", "gpx"}:
        write_gpx(out_dir / f"{base}.solution.gpx", solutions.solution_points)
    if args.obs_csv:
        write_observations_csv(out_dir / f"{base}.observations.csv", observations.observations)
    if args.analysis_json:
        write_analysis_json(out_dir / f"{base}.analysis.json", analysis)
    _log_analysis_warnings(analysis)
    if args.verbose:
        _print_analysis_summary(analysis)
    return 0


def cmd_rinex(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    rover, records, _, solutions, observations, rover_nav, analysis = _extract_bundle(args)
    out_dir = ensure_out_dir(args.out_dir)
    base = basename_for(rover, args.basename)
    if args.obs_csv:
        write_observations_csv(out_dir / f"{base}.observations.csv", observations.observations)
    write_rinex_obs(out_dir / f"{base}.direct.obs", observations.observations, rinex_version=args.rinex_version)
    nav_path = out_dir / f"{base}.rover-gps.nav"
    nav_report = extract_rover_nav(records, nav_path)
    if args.analysis_json:
        analysis["ephemeris"] = nav_report.as_dict()
        analysis["warnings"] = list(dict.fromkeys([*analysis.get("warnings", []), *nav_report.warnings]))
        write_analysis_json(out_dir / f"{base}.analysis.json", analysis)
    _log_analysis_warnings(analysis)
    if args.verbose:
        _print_analysis_summary(analysis)
    return 0


def _time_window_from_solutions(args: argparse.Namespace, margin_s: int):
    rover, _, _, solutions, _, _, _ = _extract_bundle(args)
    if not solutions.solution_points:
        raise ValueError("no rover time window could be determined from solution records")
    start = min(point.time_utc for point in solutions.solution_points) - timedelta(seconds=margin_s)
    end = max(point.time_utc for point in solutions.solution_points) + timedelta(seconds=margin_s)
    return start, end


def cmd_download_base(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    station_long = resolve_station(args.station, args.station_long)
    start, end = _time_window_from_solutions(args, args.time_margin)
    urls = planned_urls(
        station=args.station,
        station_long=station_long,
        start=start,
        end=end,
        provider_name=args.base_provider,
        base_rate=args.base_rate,
        whole_day=args.whole_day,
    )
    if args.offline or args.dry_run:
        print("\n".join(urls))
        return 0
    cache_dir = Path(args.cache_dir or args.base_dir or "euref-cache")
    downloaded = download_urls(urls, cache_dir)
    normalised = [
        normalise_rinex_file(path, crx2rnx=args.crx2rnx, cleanup=args.cleanup) for path in downloaded
    ]
    print("\n".join(str(path) for path in normalised))
    return 0


def cmd_postprocess(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    out_dir = ensure_out_dir(args.out_dir)
    base = args.basename or Path(args.rover_log).stem
    nav_resolution = resolve_nav_sources(explicit=args.nav_file, observed_systems=set(), merge_policy=args.nav_merge)
    if not nav_resolution.selected:
        raise ValueError(nav_resolution.warnings[0])
    rnx2rtkp = _tool_from_rtklib_dir(args.rnx2rtkp, args.rtklib_dir)
    command = run_rnx2rtkp(
        rnx2rtkp=rnx2rtkp,
        rtkconf=Path(args.rtkconf),
        output_file=out_dir / f"{base}-rtk.{args.output_format}",
        rover_obs=Path(args.rover_obs),
        base_obs=[Path(item) for item in args.base_obs],
        nav_files=[candidate.path for candidate in nav_resolution.selected],
        path_style=args.rtklib_path_style,
        dry_run=args.dry_run,
    )
    print(" ".join(command.args))
    return 0


def _tool_from_rtklib_dir(tool: str, rtklib_dir: str | None) -> str:
    """Resolve a RTKLIB tool from `--rtklib-dir` when the tool has no directory."""

    if not rtklib_dir:
        return tool
    tool_path = Path(tool)
    if tool_path.parent != Path("."):
        return tool
    return str(Path(rtklib_dir) / tool)


def cmd_pipeline(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)
    # Local extraction and RINEX generation first. Download/RTKLIB remain explicit
    # through supplied base and NAV inputs unless the user enables downloads.
    cmd_extract(args)
    cmd_rinex(args)
    if args.station and args.download_base:
        cmd_download_base(args)
    logging.warning(
        "pipeline currently stops after extraction/RINEX generation%s; RTKLIB post-processing "
        "requires an explicit postprocess command with rover OBS, base OBS, NAV, and rtkconf inputs.",
        " and base-download planning" if args.station and args.download_base else "",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
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
    gen.add_argument("--raw-format", choices=["none", "obsvma", "obsvmb", "obsvmcmpb"])
    gen.add_argument("--raw-hz", type=float)
    gen.add_argument("--raw-period", type=float)
    gen.add_argument("--expected-sats", type=int)
    gen.add_argument("--expected-obs-per-epoch", type=int)
    gen.add_argument("--ephemeris")
    gen.add_argument("--ephemeris-systems")
    gen.add_argument("--ppp", choices=["none", "e6-has", "b2b-ppp", "ssr-rx"])
    gen.add_argument("--ppp-datum", choices=["WGS84", "PPPORIGINAL"])
    gen.add_argument("--ppp-timeout", type=int)
    gen.add_argument("--ppp-converge")
    gen.add_argument("--include-tropinfo", action="store_true")
    gen.add_argument("--include-gpsion", action="store_true")
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
            p.add_argument("--track-source", choices=["auto", "nmea", "ppp", "adr", "gga"], default="auto")
            p.add_argument("--obs-csv", action="store_true")
            p.add_argument("--raw-output", choices=["none", "ascii", "binary", "all"], default="none")
            p.add_argument("--rinex-version", default="3.04")
        p.set_defaults(func=func)

    dl = sub.add_parser("download-base")
    dl.add_argument("rover_log")
    dl.add_argument("--station", required=True)
    dl.add_argument("--station-long")
    dl.add_argument("--base-provider", choices=sorted(PROVIDERS := {"bev-nrt", "bkg-euref-nrt", "bkg-euref-highrate"}), default="bev-nrt")
    dl.add_argument("--base-rate", choices=["30s", "1s"], default="30s")
    dl.add_argument("--base-template")
    dl.add_argument("--base-dir")
    dl.add_argument("--cache-dir")
    dl.add_argument("--time-margin", type=int, default=300)
    dl.add_argument("--whole-day", action="store_true")
    dl.add_argument("--offline", action="store_true")
    dl.add_argument("--crx2rnx")
    dl.add_argument("--cleanup", action="store_true")
    _add_common(dl)
    dl.set_defaults(func=cmd_download_base)

    post = sub.add_parser("postprocess")
    post.add_argument("rover_log")
    post.add_argument("--rover-obs", required=True)
    post.add_argument("--base-obs", action="append", required=True)
    post.add_argument("--nav-file", action="append")
    post.add_argument("--nav-glob", action="append")
    post.add_argument("--nav-provider", choices=["auto", "custom", "none"], default="auto")
    post.add_argument("--download-nav", action="store_true")
    post.add_argument("--no-download-nav", action="store_true")
    post.add_argument("--use-rover-nav", action="store_true")
    post.add_argument("--no-use-rover-nav", action="store_true")
    post.add_argument("--nav-merge", choices=["best-per-system", "all"], default="best-per-system")
    post.add_argument("--rtklib-dir")
    post.add_argument("--rnx2rtkp", default="rnx2rtkp")
    post.add_argument("--convbin")
    post.add_argument("--crx2rnx")
    post.add_argument("--rtkconf", required=True)
    post.add_argument("--rtklib-path-style", choices=["auto", "unix", "windows"], default="auto")
    post.add_argument("--output-format", choices=["nmea", "pos", "llh"], default="pos")
    post.add_argument("--navsys", choices=["gps", "gps-glo", "gps-glo-gal-bds", "all"], default="all")
    _add_common(post)
    post.set_defaults(func=cmd_postprocess)

    pipe = sub.add_parser("pipeline")
    pipe.add_argument("rover_log")
    pipe.add_argument("--station")
    pipe.add_argument("--download-base", action="store_true")
    pipe.add_argument("--obs-csv", action="store_true", default=True)
    pipe.add_argument("--solution", choices=["all", "csv", "gpx", "nmea", "none"], default="all")
    pipe.add_argument("--raw-output", choices=["none", "ascii", "binary", "all"], default="all")
    pipe.add_argument("--rinex-version", default="3.04")
    pipe.add_argument("--station-long")
    pipe.add_argument("--base-provider", choices=["bev-nrt", "bkg-euref-nrt", "bkg-euref-highrate"], default="bev-nrt")
    pipe.add_argument("--base-rate", choices=["30s", "1s"], default="30s")
    pipe.add_argument("--time-margin", type=int, default=300)
    pipe.add_argument("--whole-day", action="store_true")
    pipe.add_argument("--offline", action="store_true")
    pipe.add_argument("--cache-dir")
    pipe.add_argument("--base-dir")
    pipe.add_argument("--crx2rnx")
    pipe.add_argument("--cleanup", action="store_true")
    _add_common(pipe)
    pipe.set_defaults(func=cmd_pipeline)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
