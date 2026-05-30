import argparse
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.euref import (
    _run_crx2rnx,
    download_urls,
    filter_urls_by_remote_listing,
    normalise_rinex_file,
    parse_epn_station_position,
    parse_rinex_approx_position,
    planned_urls,
    requires_crx2rnx,
    resolve_station,
)
from um980_rtklib_pipeline.files import filter_rinex_obs_by_overlap, read_rinex_obs_time_span
from um980_rtklib_pipeline.nav_resolver import resolve_nav_sources
from um980_rtklib_pipeline import rtklib
from um980_rtklib_pipeline.rtklib import (
    _warn_about_rtklib_result,
    build_rnx2rtkp_command,
    cygdrive_to_windows,
    detect_rtklib_path_style,
    executable_for_subprocess,
    format_command,
    path_for_rtklib_argument,
    resolve_rtklib_tool,
    run_rnx2rtkp,
    validate_rtklib_inputs,
)


def test_station_alias_and_bev_url():
    assert resolve_station("CPAR") == "CPAR00CZE"
    assert resolve_station("TUBO00CZE0") == "TUBO00CZE"
    assert resolve_station("GOP") == "GOP00CZE"
    assert resolve_station("ignored", station_long="KUNZ00CZE0") == "KUNZ00CZE"
    urls = planned_urls(
        station="CPAR",
        start=datetime(2026, 5, 20, 12, 10, tzinfo=UTC),
        end=datetime(2026, 5, 20, 12, 20, tzinfo=UTC),
    )
    assert "CPAR00CZE_R_20261401200_01H_30S_MO.crx.gz" in urls[0]


def test_pipeline_defaults_to_rtklib_compatible_rinex():
    parser = cli.build_parser()
    args = parser.parse_args(["pipeline", "rover.unc"])
    assert args.rinex_compat == "convbin"
    assert args.rtkconf is None
    assert cli._generated_rtk_options(args) == ["-p", "2", "-f", "3", "-sys", "G,R,E,C,J", "-m", "10", "-t", "-c"]


def test_postprocess_no_longer_requires_rtklib_config_file():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "postprocess",
            "rover.unc",
            "--rover-obs",
            "rover.obs",
            "--base-obs",
            "base.obs",
            "--nav-file",
            "brdc.nav",
        ]
    )
    assert args.rtkconf is None
    assert cli._generated_rtk_options(args)[0:4] == ["-p", "2", "-f", "3"]


def test_generated_rtk_options_request_real_nmea_output():
    parser = cli.build_parser()
    args = parser.parse_args(["pipeline", "rover.unc", "--output-format", "nmea"])

    assert "-n" in cli._generated_rtk_options(args)


def test_rtklib_output_format_accepts_repeated_and_csv_values():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "rover.unc",
            "--output-format",
            "pos,nmea",
            "--output-format",
            "llh",
        ]
    )

    assert cli._rtklib_output_formats(args) == ["pos", "nmea", "llh"]


def test_rtkconf_nmea_output_adds_command_line_override():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "rover.unc",
            "--rtkconf",
            "um980.conf",
            "--output-format",
            "nmea",
        ]
    )

    rtkconf, rtk_options = cli._rtklib_config_and_options(args)

    assert rtkconf == Path("um980.conf")
    assert rtk_options == ["-n"]


def test_rtkconf_multiple_output_formats_resolve_per_format_options():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "rover.unc",
            "--rtkconf",
            "um980.conf",
            "--output-format",
            "pos,nmea",
        ]
    )

    pos_conf, pos_options = cli._rtklib_config_and_options(args, output_format="pos")
    nmea_conf, nmea_options = cli._rtklib_config_and_options(args, output_format="nmea")

    assert pos_conf == nmea_conf == Path("um980.conf")
    assert pos_options is None
    assert nmea_options == ["-n"]


def test_extract_defaults_to_best_position_nmea_output():
    parser = cli.build_parser()
    args = parser.parse_args(["extract", "rover.unc"])

    assert args.position_nmea == "best"


def test_download_base_accepts_rtklib_dir_for_crx2rnx_discovery():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "download-base",
            "rover.unc",
            "--station",
            "TUBO",
            "--rtklib-dir",
            "RTKLIB_EX_2.5.0",
            "--crx2rnx",
            "./crx2rnx.exe",
        ]
    )

    assert args.rtklib_dir == "RTKLIB_EX_2.5.0"
    assert args.crx2rnx == "./crx2rnx.exe"


def test_raw_rnx2rtkp_options_can_override_generated_output_format():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "rover.unc",
            "--output-format",
            "nmea",
            "--rnx2rtkp-option=-e",
        ]
    )

    assert cli._generated_rtk_options(args)[-2:] == ["-n", "-e"]


def test_rtklib_trace_and_stat_levels_are_named_options():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "pipeline",
            "rover.unc",
            "--rtklib-trace-level",
            "4",
            "--rtklib-stat-level",
            "2",
        ]
    )

    assert cli._generated_rtk_options(args)[-4:] == ["-x", "4", "-y", "2"]


def test_rtkconf_keeps_trace_and_stat_command_line_overrides():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "postprocess",
            "rover.unc",
            "--rover-obs",
            "rover.obs",
            "--base-obs",
            "base.obs",
            "--rtkconf",
            "um980.conf",
            "--rtklib-trace-level",
            "4",
            "--rtklib-stat-level",
            "2",
        ]
    )

    rtkconf, rtk_options = cli._rtklib_config_and_options(args)

    assert rtkconf == Path("um980.conf")
    assert rtk_options == ["-x", "4", "-y", "2"]


def test_rinex_v2_url_planning():
    urls = planned_urls(
        station="tubo",
        start=datetime(2026, 5, 18, 7, 16, tzinfo=UTC),
        end=datetime(2026, 5, 18, 7, 20, tzinfo=UTC),
        provider_name="bev-nrt",
        rinex_version="2",
    )
    assert urls[0] == "ftp://gnss.bev.gv.at/pub/nrt/138/07/tubo138h.26d.gz"
    high = planned_urls(
        station="tubo",
        start=datetime(2026, 5, 18, 7, 16, tzinfo=UTC),
        end=datetime(2026, 5, 18, 7, 20, tzinfo=UTC),
        provider_name="bkg-euref-highrate",
        base_rate="1s",
        rinex_version="2",
    )
    assert high[0].endswith("/tubo138h15.26d.Z")


def test_highrate_url_planning_includes_only_touching_chunks():
    urls = planned_urls(
        station="TUBO",
        start=datetime(2026, 5, 22, 21, 15, tzinfo=UTC),
        end=datetime(2026, 5, 22, 21, 15, tzinfo=UTC),
        provider_name="bkg-euref-highrate",
        base_rate="1s",
        rinex_version="3",
    )
    assert [
        url.rsplit("/", 1)[-1]
        for url in urls
    ] == [
        "TUBO00CZE_S_20261422100_15M_01S_MO.crx.gz",
        "TUBO00CZE_S_20261422100_15M_01S_MO.crx.gz",
        "TUBO00CZE_S_20261422115_15M_01S_MO.crx.gz",
        "TUBO00CZE_S_20261422115_15M_01S_MO.crx.gz",
    ]
    assert urls[0].startswith("https://igs.bkg.bund.de/root_ftp/EUREF/highrate/")
    assert urls[1].startswith("ftp://igs-ftp.bkg.bund.de/EUREF/highrate/")


def test_verified_bkg_highrate_sample_urls():
    urls = planned_urls(
        station="CPAR",
        start=datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
        end=datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
        provider_name="bkg-euref-highrate",
        base_rate="1s",
        rinex_version="3",
    )

    assert urls == [
        "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/2026/143/f/CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz",
        "ftp://igs-ftp.bkg.bund.de/EUREF/highrate/2026/143/f/CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz",
    ]
    assert not any("_R_" in url for url in urls)


def test_hourly_url_planning_uses_recorded_span_without_margin():
    urls = planned_urls(
        station="TUBO",
        start=datetime(2026, 5, 22, 21, 1, 33, tzinfo=UTC),
        end=datetime(2026, 5, 22, 21, 18, 24, tzinfo=UTC),
        provider_name="bev-nrt",
        rinex_version="3",
    )
    names = [url.rsplit("/", 1)[-1] for url in urls]
    assert all(name.startswith("TUBO00CZE_R_20261422100_01H_30S_MO.") for name in names)
    assert not any("20261422000" in name for name in names)


def test_script_mentioned_station_aliases_and_v2_short_codes():
    for short, long_name in {
        "GOP7": "GOP700CZE",
        "KUNZ": "KUNZ00CZE",
        "GOPE": "GOPE00CZE",
        "CPAR": "CPAR00CZE",
        "TUBO": "TUBO00CZE",
        "GRAZ": "GRAZ00AUT",
        "CFRM": "CFRM00CZE",
        "TRF2": "TRF200AUT",
        "MOPI": "MOPI00SVK",
        "MOP2": "MOP200SVK",
    }.items():
        assert resolve_station(short) == long_name

    for short in ["tubo", "kunz", "pfa2", "mopi", "mop2"]:
        urls = planned_urls(
            station=short,
            start=datetime(2026, 5, 18, 7, 0, tzinfo=UTC),
            end=datetime(2026, 5, 18, 7, 1, tzinfo=UTC),
            provider_name="bev-nrt",
            rinex_version="2",
        )
        assert f"/{short}138h.26d.gz" in urls[0]


def test_nav_resolver_selects_explicit(tmp_path: Path):
    nav = tmp_path / "BRDC00WRD_R_20261400000_01D_MN.rnx"
    nav.write_text(
        "     3.04           NAVIGATION DATA     M                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n"
    )
    resolution = resolve_nav_sources(explicit=[nav], observed_systems={"GPS", "Galileo"})
    assert resolution.selected
    assert not resolution.missing_systems


def test_nav_resolver_rejects_header_only_nav(tmp_path: Path):
    nav = tmp_path / "empty.nav"
    nav.write_text(
        "     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
    )
    resolution = resolve_nav_sources(explicit=[nav], observed_systems={"GPS"})
    assert not resolution.selected
    assert "NAV file has no data records" in resolution.candidates[0].notes[-1]


def test_nav_resolver_rejects_empty_sbs(tmp_path: Path):
    sbs = tmp_path / "empty.sbs"
    sbs.write_text("", encoding="ascii")
    resolution = resolve_nav_sources(explicit=[sbs], observed_systems={"SBAS"})
    assert not resolution.selected
    assert "SBAS message file is empty" in resolution.candidates[0].notes[-1]


def test_rtklib_validation_rejects_empty_sbs(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    sbs = tmp_path / "empty.sbs"
    obs.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    base.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    sbs.write_text("", encoding="ascii")
    with pytest.raises(ValueError, match="SBAS message file is empty"):
        validate_rtklib_inputs(rnx2rtkp="/bin/sh", rover_obs=obs, base_obs=[base], nav_files=[sbs])


def test_rtklib_validation_rejects_wildcard(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "base.nav"
    obs.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    base.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    nav.write_text(
        "     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n"
    )
    with pytest.raises(ValueError, match="wildcard"):
        validate_rtklib_inputs(rnx2rtkp="/bin/sh", rover_obs=Path("*.obs"), base_obs=[base], nav_files=[nav])
    args = build_rnx2rtkp_command(
        rnx2rtkp="rnx2rtkp",
        rtkconf=tmp_path / "rtk.conf",
        output_file=tmp_path / "out.pos",
        rover_obs=obs,
        base_obs=[base],
        nav_files=[nav],
    )
    assert args[:5] == ["rnx2rtkp", "-k", str(tmp_path / "rtk.conf"), "-o", str(tmp_path / "out.pos")]


def test_rnx2rtkp_command_can_use_generated_options_without_config(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "base.nav"
    args = build_rnx2rtkp_command(
        rnx2rtkp="rnx2rtkp",
        rtkconf=None,
        rtk_options=["-p", "2", "-f", "3", "-sys", "G,R,E,C,J", "-m", "10", "-t", "-c"],
        output_file=tmp_path / "out.pos",
        rover_obs=obs,
        base_obs=[base],
        nav_files=[nav],
    )
    assert args[:11] == ["rnx2rtkp", "-p", "2", "-f", "3", "-sys", "G,R,E,C,J", "-m", "10", "-t", "-c"]
    assert "-k" not in args
    assert args[-3:] == [str(obs), str(base), str(nav)]


def test_rnx2rtkp_command_can_pass_base_wildcard_as_single_argument(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    base_a = tmp_path / "base-0000.rnx"
    base_b = tmp_path / "base-0001.rnx"
    nav = tmp_path / "base.nav"
    base_pattern = tmp_path / "base-*.rnx"

    args = build_rnx2rtkp_command(
        rnx2rtkp="rnx2rtkp",
        rtkconf=None,
        rtk_options=["-p", "2"],
        output_file=tmp_path / "out.pos",
        rover_obs=obs,
        base_obs=[base_a, base_b],
        nav_files=[nav],
        base_obs_arg=base_pattern,
    )

    assert args[-3:] == [str(obs), str(base_pattern), str(nav)]
    assert str(base_a) not in args
    assert str(base_b) not in args


def test_rnx2rtkp_command_includes_base_ecef(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "base.nav"
    args = build_rnx2rtkp_command(
        rnx2rtkp="rnx2rtkp",
        rtkconf=tmp_path / "rtk.conf",
        output_file=tmp_path / "out.pos",
        rover_obs=obs,
        base_obs=[base],
        nav_files=[nav],
        base_ecef_xyz_m=(3949919.0811, 1116467.0408, 4865832.5323),
    )
    assert args[5:9] == ["-r", "3949919.0811", "1116467.0408", "4865832.5323"]


def test_format_command_shell_quotes_arguments():
    assert format_command(["rnx2rtkp", "-k", "config dir/rtk.conf"]) == "rnx2rtkp -k 'config dir/rtk.conf'"


def test_run_rnx2rtkp_debug_logs_command_before_dry_run(tmp_path: Path, caplog):
    rover = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "base.nav"
    conf = tmp_path / "rtk.conf"
    output = tmp_path / "out.pos"
    rover.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    base.write_text("     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n")
    nav.write_text(
        "     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n",
        encoding="ascii",
    )
    conf.write_text("pos1-posmode =kinematic\n", encoding="ascii")

    caplog.set_level(logging.INFO)
    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        dry_run=True,
        debug=True,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any("RTKLIB command: /bin/sh -k" in message for message in messages)
    assert any(str(command.wrapper_file) in message for message in messages)


def _write_minimal_rtklib_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    rover = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "base.nav"
    conf = tmp_path / "rtk.conf"
    output = tmp_path / "out.nmea"
    obs_header = "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
    rover.write_text(obs_header, encoding="ascii")
    base.write_text(obs_header, encoding="ascii")
    nav.write_text(
        "     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n",
        encoding="ascii",
    )
    conf.write_text("pos1-posmode =kinematic\n", encoding="ascii")
    return rover, base, nav, conf, output


def test_run_rnx2rtkp_recovers_missing_output_from_stdout(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _write_minimal_rtklib_inputs(tmp_path)
    stdout = "$GNRMC,161708.50,A,5000.0000,N,01400.0000,E,0.0,0.0,250526,,,A*00\n"

    def fake_run(*args, **kwargs):
        return argparse.Namespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
    )

    assert output.read_text(encoding="utf-8") == stdout


def test_run_rnx2rtkp_fails_when_successful_run_has_no_output(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _write_minimal_rtklib_inputs(tmp_path)

    def fake_run(*args, **kwargs):
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not create the requested output file"):
        run_rnx2rtkp(
            rnx2rtkp="/bin/sh",
            rtkconf=conf,
            output_file=output,
            rover_obs=rover,
            base_obs=[base],
            nav_files=[nav],
        )


def test_parse_epn_station_position():
    page = """
    <section><label>Positions and Velocities</a> from C2385 expressed in ETRF2000 </label>
    <table>
    <tr><th>header</th></tr>
    <tr>
      <td class="align-center">2011-10-26 / 2025-09-27</td>
      <td class="align-center">2</td>
      <td class="align-center">2020-01-01</td>
      <td class="align-right">  3949919.0811 <br />&plusmn; 0.0001</td>
      <td class="align-right">  1116467.0408 <br />&plusmn; 0.0001</td>
      <td class="align-right">  4865832.5323 <br />&plusmn; 0.0001</td>
      <td class="align-right">  -0.0000 <br />&plusmn; 0.0001</td>
      <td class="align-right">  -0.0004 <br />&plusmn; 0.0001</td>
      <td class="align-right">  -0.0004 <br />&plusmn; 0.0001</td>
    </tr>
    </table>
    """
    position = parse_epn_station_position(page, "CPAR")
    assert position.station == "CPAR00CZE"
    assert position.ecef_xyz_m == (3949919.0811, 1116467.0408, 4865832.5323)
    assert position.frame == "ETRF2000"


def test_parse_rinex_approx_position(tmp_path: Path):
    obs = tmp_path / "base.obs"
    obs.write_text(
        "CPAR                                                        MARKER NAME\n"
        "  3949919.0831  1116467.0453  4865832.5410                  APPROX POSITION XYZ\n"
        "                                                            END OF HEADER\n",
        encoding="ascii",
    )
    position = parse_rinex_approx_position(obs)
    assert position.station == "CPAR"
    assert position.ecef_xyz_m == (3949919.0831, 1116467.0453, 4865832.5410)


def _write_obs_with_span(path: Path, start: str, end: str) -> None:
    path.write_text(
        "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
        f"  {start}     GPS         TIME OF FIRST OBS\n"
        f"  {end}     GPS         TIME OF LAST OBS\n"
        "                                                            END OF HEADER\n",
        encoding="ascii",
    )


def test_rinex_obs_time_span_parses_header(tmp_path: Path):
    obs = tmp_path / "rover.obs"
    _write_obs_with_span(obs, "2026 05 22 21 01 33.2000000", "2026 05 22 21 18 24.2000000")
    span = read_rinex_obs_time_span(obs)
    assert span.start == datetime(2026, 5, 22, 21, 1, 33, 200000)
    assert span.end == datetime(2026, 5, 22, 21, 18, 24, 200000)


def test_base_obs_without_rover_overlap_is_filtered(tmp_path: Path):
    rover = tmp_path / "rover.obs"
    stale = tmp_path / "TUBO00CZE_R_20261422000_01H_30S_MO.rnx"
    current = tmp_path / "TUBO00CZE_R_20261422100_01H_30S_MO.rnx"
    _write_obs_with_span(rover, "2026 05 22 21 01 33.2000000", "2026 05 22 21 18 24.2000000")
    _write_obs_with_span(stale, "2026 05 22 20 00 00.0000000", "2026 05 22 20 59 30.0000000")
    _write_obs_with_span(current, "2026 05 22 21 00 00.0000000", "2026 05 22 21 59 30.0000000")

    retained, warnings = filter_rinex_obs_by_overlap(rover, [stale, current])

    assert retained == [current]
    assert any(str(stale) in warning and "no rover overlap" in warning for warning in warnings)


def test_base_obs_overlap_filter_rejects_all_stale_files(tmp_path: Path):
    rover = tmp_path / "rover.obs"
    stale = tmp_path / "TUBO00CZE_R_20261422000_01H_30S_MO.rnx"
    _write_obs_with_span(rover, "2026 05 22 21 01 33.2000000", "2026 05 22 21 18 24.2000000")
    _write_obs_with_span(stale, "2026 05 22 20 00 00.0000000", "2026 05 22 20 59 30.0000000")

    with pytest.raises(ValueError, match="no base observation files overlap"):
        filter_rinex_obs_by_overlap(rover, [stale])


def test_normalise_rinex_v2_hatanaka_suffix(tmp_path: Path, monkeypatch):
    compressed = tmp_path / "cpar138h.26d"
    produced = tmp_path / "cpar138h.26o"
    compressed.write_text("hatanaka", encoding="ascii")

    def fake_run(crx2rnx: str, current: Path, output: Path, *, timeout_s: float):
        assert crx2rnx == "crx2rnx"
        assert current == compressed
        assert output == produced
        produced.write_text(
            "     2.11           OBSERVATION DATA    G                   RINEX VERSION / TYPE\n"
            "                                                            END OF HEADER\n",
            encoding="ascii",
        )
        return argparse.Namespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("um980_rtklib_pipeline.euref._run_crx2rnx", fake_run)
    assert normalise_rinex_file(compressed, crx2rnx="crx2rnx") == produced


def test_crx2rnx_runner_forces_overwrite_and_closes_stdin(tmp_path: Path, monkeypatch):
    compressed = tmp_path / "base.crx"
    produced = tmp_path / "base.rnx"
    calls: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout: float | None = None):
            return "ok", ""

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("um980_rtklib_pipeline.euref.subprocess.Popen", fake_popen)

    result = _run_crx2rnx("crx2rnx", compressed, produced, timeout_s=30.0)

    assert result.returncode == 0
    assert calls["command"] == ["crx2rnx", str(compressed), "-f"]
    assert calls["stdin"] == subprocess.DEVNULL
    assert calls["stdout"] == subprocess.PIPE
    assert calls["stderr"] == subprocess.PIPE


def test_hatanaka_detection_includes_compressed_rinex2_and_rinex3():
    assert requires_crx2rnx(Path("TUBO00CZE_R_20261430500_01H_30S_MO.crx.gz"))
    assert requires_crx2rnx(Path("tubo143f.26d.gz"))
    assert requires_crx2rnx(Path("tubo143f.26d.Z"))
    assert not requires_crx2rnx(Path("TUBO00CZE_R_20261430500_01H_30S_MO.rnx.gz"))
    assert not requires_crx2rnx(Path("tubo143f.26o.gz"))


def test_normalise_rinex_preflights_crx2rnx_before_decompression(tmp_path: Path):
    compressed = tmp_path / "TUBO00CZE_R_20261430500_01H_30S_MO.crx.gz"
    compressed.write_bytes(b"not actually gzip")

    with pytest.raises(RuntimeError, match="before decompression"):
        normalise_rinex_file(compressed, crx2rnx=None)

    assert not compressed.with_suffix("").exists()


def test_download_base_auto_resolves_crx2rnx_for_hatanaka_downloads(tmp_path: Path, monkeypatch):
    tool = tmp_path / "crx2rnx"
    args = argparse.Namespace(crx2rnx=None, rtklib_dir=None)

    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool_name, rtklib_dir=None: str(tool))
    monkeypatch.setattr(cli, "executable_exists", lambda executable: executable == str(tool))
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: f"{executable}.run")

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == f"{tool}.run"


def test_download_base_prefers_crx2rnx_from_rtklib_dir(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool = rtklib_dir / "crx2rnx"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx=None, rtklib_dir=str(rtklib_dir))

    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_resolves_crx2rnx_from_current_directory(tmp_path: Path, monkeypatch):
    tool = tmp_path / "crx2rnx"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx=None, rtklib_dir=None)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_resolves_explicit_bare_crx2rnx_from_current_directory(tmp_path: Path, monkeypatch):
    tool = tmp_path / "crx2rnx"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx="crx2rnx", rtklib_dir=None)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)
    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool_name, rtklib_dir=None: tool_name)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_honors_explicit_relative_crx2rnx_before_rtklib_dir(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool = tmp_path / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx="./crx2rnx.exe", rtklib_dir=str(rtklib_dir))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_honors_explicit_relative_crx2rnx_without_exe_suffix(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool = tmp_path / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx="./crx2rnx", rtklib_dir=str(rtklib_dir))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_honors_explicit_backslash_relative_crx2rnx_on_cygwin(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool = tmp_path / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx=".\\crx2rnx", rtklib_dir=str(rtklib_dir))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_honors_explicit_subdir_crx2rnx_before_rtklib_dir(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    tool = tool_dir / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx="tools/crx2rnx.exe", rtklib_dir=str(rtklib_dir))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_honors_explicit_subdir_crx2rnx_without_exe_suffix(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    tool = tool_dir / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx="tools/crx2rnx", rtklib_dir=str(rtklib_dir))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_uses_crx2rnx_exe_from_rtklib_dir(tmp_path: Path, monkeypatch):
    rtklib_dir = tmp_path / "rtklib"
    rtklib_dir.mkdir()
    tool = rtklib_dir / "crx2rnx.exe"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    args = argparse.Namespace(crx2rnx=None, rtklib_dir=str(rtklib_dir))

    monkeypatch.setattr(cli, "executable_for_subprocess", lambda executable: executable)

    assert cli._resolve_crx2rnx_for_download(args, [Path("base.crx.gz")]) == str(tool)


def test_download_base_rejects_hatanaka_without_crx2rnx_before_extraction(tmp_path: Path, monkeypatch):
    downloaded = tmp_path / "base.crx.gz"
    downloaded.write_bytes(b"not actually gzip")
    args = argparse.Namespace(crx2rnx=None, rtklib_dir=None)

    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool_name, rtklib_dir=None: str(tmp_path / "missing"))
    monkeypatch.setattr(cli, "executable_exists", lambda executable: False)

    with pytest.raises(RuntimeError, match="crx2rnx is required"):
        cli._resolve_crx2rnx_for_download(args, [downloaded])

    assert not downloaded.with_suffix("").exists()


def test_cygdrive_paths_convert_to_windows_arguments():
    assert cygdrive_to_windows("/cygdrive/c/data/rover.obs") == "C:\\data\\rover.obs"
    assert path_for_rtklib_argument(Path("/cygdrive/d/base/base.obs"), "windows") == "D:\\base\\base.obs"


def test_cygwin_exe_auto_uses_windows_argument_paths(monkeypatch):
    monkeypatch.setattr(rtklib.sys, "platform", "cygwin")
    monkeypatch.setattr(rtklib, "_run_cygpath", lambda flag, value: None)
    assert detect_rtklib_path_style("/cygdrive/c/rtklib/rnx2rtkp.exe") == "windows"
    args = build_rnx2rtkp_command(
        rnx2rtkp="/cygdrive/c/rtklib/rnx2rtkp.exe",
        rtkconf=Path("/cygdrive/c/cfg/rtk.conf"),
        output_file=Path("/cygdrive/c/out/out.pos"),
        rover_obs=Path("/cygdrive/c/data/rover.obs"),
        base_obs=[Path("/cygdrive/c/data/base.obs")],
        nav_files=[Path("/cygdrive/c/data/brdc.nav")],
    )
    assert args[2] == "C:\\cfg\\rtk.conf"
    assert args[4] == "C:\\out\\out.pos"
    assert args[5:] == [
        "C:\\data\\rover.obs",
        "C:\\data\\base.obs",
        "C:\\data\\brdc.nav",
    ]


def test_cygwin_pe_binary_without_exe_suffix_uses_windows_argument_paths(tmp_path: Path, monkeypatch):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"MZwindows-binary-placeholder")
    monkeypatch.setattr(rtklib.sys, "platform", "cygwin")

    def fake_cygpath(flag: str, value: str) -> str | None:
        assert flag == "-w"
        return "C:\\" + value.replace("/", "\\")

    monkeypatch.setattr(rtklib, "_run_cygpath", fake_cygpath)
    assert detect_rtklib_path_style(str(tool)) == "windows"

    args = build_rnx2rtkp_command(
        rnx2rtkp=str(tool),
        rtkconf=Path("um980-onepass-gps-gal-bds-el28.conf"),
        output_file=Path("out/run.nmea"),
        rover_obs=Path("out/rover.obs"),
        base_obs=[Path("base/base.rnx")],
        nav_files=[Path("out/rover-gps.nav")],
    )

    assert args[2] == "C:\\um980-onepass-gps-gal-bds-el28.conf"
    assert args[4] == "C:\\out\\run.nmea"
    assert args[-3:] == ["C:\\out\\rover.obs", "C:\\base\\base.rnx", "C:\\out\\rover-gps.nav"]


def test_cygwin_wildcard_path_preserves_asterisk(monkeypatch):
    monkeypatch.setattr(rtklib.sys, "platform", "cygwin")
    calls: list[str] = []

    def fake_cygpath(flag: str, value: str) -> str | None:
        calls.append(value)
        if "*" in value:
            return "C:\\bad\\base-\uf02a.rnx"
        return "C:\\" + value.replace("/", "\\")

    monkeypatch.setattr(rtklib, "_run_cygpath", fake_cygpath)

    converted = path_for_rtklib_argument(Path("run/rover.rtklib-base/base-*.rnx"), "windows")

    assert converted == "C:\\run\\rover.rtklib-base\\base-*.rnx"
    assert calls == ["run/rover.rtklib-base"]


def test_cygwin_build_command_preserves_base_wildcard_asterisk(tmp_path: Path, monkeypatch):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"MZwindows-binary-placeholder")
    monkeypatch.setattr(rtklib.sys, "platform", "cygwin")
    monkeypatch.setattr(rtklib, "_run_cygpath", lambda flag, value: "C:\\" + value.replace("/", "\\"))

    args = build_rnx2rtkp_command(
        rnx2rtkp=str(tool),
        rtkconf=Path("um980.conf"),
        output_file=Path("run/out.nmea"),
        rover_obs=Path("run/rover.obs"),
        base_obs=[Path("run/base-0000.rnx"), Path("run/base-0001.rnx")],
        base_obs_arg=Path("run/rover.rtklib-base/base-*.rnx"),
        nav_files=[Path("run/rover-gps.nav")],
    )

    assert args[-2] == "C:\\run\\rover.rtklib-base\\base-*.rnx"


def test_rtklib_dir_resolves_bare_tool_name_only():
    assert resolve_rtklib_tool("rnx2rtkp.exe", rtklib_dir="/opt/rtklib") == "/opt/rtklib/rnx2rtkp.exe"
    assert resolve_rtklib_tool("/custom/rnx2rtkp", rtklib_dir="/opt/rtklib") == "/custom/rnx2rtkp"


def test_rtklib_tool_resolves_local_build_tree(tmp_path: Path, monkeypatch):
    local_bin = tmp_path / "build-tools" / "RTKLIB-ex-bin" / "bin"
    local_bin.mkdir(parents=True)
    tool = local_bin / "rnx2rtkp"
    tool.write_text("#!/bin/sh\n")
    missing_home_bin = tmp_path / "missing-home" / "RTKLIB-ex-bin" / "bin"
    monkeypatch.setattr("um980_rtklib_pipeline.rtklib.USER_RTKLIB_BIN", missing_home_bin)
    assert resolve_rtklib_tool("rnx2rtkp", cwd=tmp_path) == str(tool)


def test_rtklib_tool_prefers_user_home_bin(tmp_path: Path, monkeypatch):
    home_bin = tmp_path / "RTKLIB-ex-bin" / "bin"
    home_bin.mkdir(parents=True)
    tool = home_bin / "rnx2rtkp"
    tool.write_text("#!/bin/sh\n")
    monkeypatch.setattr("um980_rtklib_pipeline.rtklib.USER_RTKLIB_BIN", home_bin)
    assert resolve_rtklib_tool("rnx2rtkp", cwd=tmp_path / "repo") == str(tool)


def test_non_executable_local_tool_is_mirrored_for_subprocess(tmp_path: Path, monkeypatch):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"binary-placeholder")
    tool.chmod(0o600)
    monkeypatch.setattr(rtklib, "is_termux", lambda: True)
    mirrored = Path(executable_for_subprocess(str(tool)))
    assert mirrored.exists()
    assert mirrored != tool
    assert mirrored.stat().st_mode & 0o111


def test_non_executable_tool_is_not_mirrored_outside_termux(tmp_path: Path, monkeypatch):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"binary-placeholder")
    tool.chmod(0o600)
    monkeypatch.setattr(rtklib, "is_termux", lambda: False)
    monkeypatch.setattr(rtklib, "is_cygwin", lambda: False)

    assert executable_for_subprocess(str(tool)) == str(tool)
    assert not rtklib.executable_exists(str(tool))


def test_cygwin_non_executable_tool_is_not_mirrored_to_termux(tmp_path: Path, monkeypatch):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"binary-placeholder")
    tool.chmod(0o600)
    monkeypatch.setattr(rtklib, "is_termux", lambda: False)
    monkeypatch.setattr(rtklib, "is_cygwin", lambda: True)

    assert executable_for_subprocess(str(tool)) == str(tool)
    assert rtklib.executable_exists(str(tool))


def test_cygwin_is_never_detected_as_termux(monkeypatch):
    monkeypatch.setattr(rtklib.sys, "platform", "cygwin")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")

    assert not rtklib.is_termux()


def test_rtklib_header_only_output_is_logged(tmp_path: Path, caplog):
    output = tmp_path / "out.pos"
    output.write_text("% header only\n", encoding="ascii")
    _warn_about_rtklib_result(
        output,
        "invalid option value pos1-posmode (rtk.conf:2)\nprocessing : 2026/05/18 07:00:00 Q=0\n",
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any("invalid configuration option" in message for message in messages)
    assert any("no solution rows" in message for message in messages)
    assert any("only Q=0 epochs" in message for message in messages)


def test_rtklib_single_quality_is_not_logged_as_q0_failure(tmp_path: Path, caplog):
    output = tmp_path / "out.pos"
    output.write_text("$GPGGA,120000.00,5000.0,N,01400.0,E,1,08,1.0,250.0,M,0.0,M,0.0,0000*00\n", encoding="ascii")
    _warn_about_rtklib_result(
        output,
        "processing : 2026/05/18 07:00:00 Q=0\nprocessing : 2026/05/18 07:00:01 Q=5\n",
    )
    messages = [record.getMessage() for record in caplog.records]
    assert not any("only Q=0 epochs" in message for message in messages)


def test_download_urls_continues_after_failed_alternative(tmp_path: Path, monkeypatch):
    def fake_urlretrieve(url: str, target: Path):
        if url.endswith("missing.crx.gz"):
            raise OSError("not found")
        target.write_bytes(b"ok")
        return str(target), None

    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlretrieve", fake_urlretrieve)
    paths = download_urls(
        ["ftp://example.test/missing.crx.gz", "ftp://example.test/present.rnx.gz"],
        tmp_path,
    )
    assert [path.name for path in paths] == ["present.rnx.gz"]


def test_bkg_listing_preflight_skips_absent_highrate_station(tmp_path: Path, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'<a href="CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz">CPAR</a>'

    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlopen", lambda url, timeout: Response())
    urls = [
        "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/2026/143/f/TUBO00CZE_S_20261430530_15M_01S_MO.crx.gz",
        "ftp://igs-ftp.bkg.bund.de/EUREF/highrate/2026/143/f/TUBO00CZE_S_20261430530_15M_01S_MO.crx.gz",
    ]

    with pytest.raises(RuntimeError, match="no planned BKG base observation files are listed"):
        filter_urls_by_remote_listing(urls, tmp_path)


def test_bkg_listing_preflight_keeps_verified_and_cached_urls(tmp_path: Path, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'<a href="CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz">CPAR</a>'

    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlopen", lambda url, timeout: Response())
    cached = tmp_path / "TUBO00CZE_S_20261430530_15M_01S_MO.rnx"
    cached.write_text("cached\n", encoding="ascii")
    urls = [
        "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/2026/143/f/CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz",
        "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/2026/143/f/TUBO00CZE_S_20261430530_15M_01S_MO.crx.gz",
    ]

    assert filter_urls_by_remote_listing(urls, tmp_path) == urls


def test_base_download_highrate_falls_back_to_lowrate(tmp_path: Path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    attempts: list[str] = []
    low_file = tmp_path / "CPAR00CZE_R_20261380700_01H_30S_MO.rnx"

    args = argparse.Namespace(
        station="CPAR",
        station_long=None,
        rover_log="rover.unc",
        time_margin=300,
        base_resolution="high",
        base_rinex_version="3",
        base_rate="1s",
        base_provider="bev-nrt",
        no_base_fallback=False,
        whole_day=False,
        offline=False,
        dry_run=False,
        cache_dir=str(tmp_path),
        base_dir=None,
        crx2rnx=None,
        cleanup=False,
        force_download=False,
    )

    monkeypatch.setattr(
        cli,
        "_time_window_from_solutions",
        lambda _args, _margin: (
            datetime(2026, 5, 18, 7, 20, tzinfo=UTC),
            datetime(2026, 5, 18, 7, 25, tzinfo=UTC),
        ),
    )

    def fake_download_urls(urls: list[str], cache_dir: Path, *, force: bool = False):
        assert force is False
        attempts.append(urls[0])
        if "highrate" in urls[0]:
            raise RuntimeError("404")
        low_file.write_text(
            "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
            "                                                            END OF HEADER\n",
            encoding="ascii",
        )
        return [low_file]

    monkeypatch.setattr(cli, "filter_urls_by_remote_listing", lambda urls, cache_dir, *, force=False: urls)
    monkeypatch.setattr(cli, "download_urls", fake_download_urls)
    paths = cli._download_base_files(args)
    assert paths == [low_file]
    assert "highrate" in attempts[0]
    assert "nrt" in attempts[1]
    assert any("trying provider=bev-nrt rate=30s rinex=3" in record.getMessage() for record in caplog.records)
    assert any("This run is not a high-rate base run" in record.getMessage() for record in caplog.records)
    assert any("falling back to low-rate 30s base data" in record.getMessage() for record in caplog.records)


def test_high_resolution_prefers_1s_candidates(tmp_path: Path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    high_file = tmp_path / "CPAR00CZE_S_20261430530_15M_01S_MO.rnx"
    high_file.write_text(
        "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n",
        encoding="ascii",
    )
    calls: list[str] = []
    args = argparse.Namespace(
        station="CPAR",
        station_long=None,
        base_resolution="high",
        base_rinex_version="3",
        base_rate="1s",
        base_provider="bev-nrt",
        no_base_fallback=False,
        whole_day=False,
        offline=False,
        dry_run=False,
        cache_dir=str(tmp_path),
        base_dir=None,
        crx2rnx=None,
        cleanup=False,
        force_download=False,
    )

    def fake_download_urls(urls: list[str], cache_dir: Path, *, force: bool = False):
        calls.append(urls[0])
        if "highrate" not in urls[0]:
            raise AssertionError("low-rate fallback should not run when high-rate data is available")
        return [high_file]

    monkeypatch.setattr(cli, "filter_urls_by_remote_listing", lambda urls, cache_dir, *, force=False: urls)
    monkeypatch.setattr(cli, "download_urls", fake_download_urls)

    paths = cli._download_base_files_for_window(
        args,
        datetime(2026, 5, 23, 5, 29, tzinfo=UTC),
        datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
    )

    assert paths == [high_file]
    assert len(calls) == 1
    assert any("requested_base_resolution=high" in record.getMessage() for record in caplog.records)
    assert any("selected_rate=1s" in record.getMessage() for record in caplog.records)


def test_high_resolution_does_not_use_30s_cache_without_fallback(tmp_path: Path, monkeypatch):
    cached_low = tmp_path / "CPAR00CZE_R_20261430500_01H_30S_MO.rnx"
    cached_low.write_text("cached low-rate\n", encoding="ascii")
    attempted: list[str] = []
    args = argparse.Namespace(
        station="CPAR",
        station_long=None,
        base_resolution="high",
        base_rinex_version="3",
        base_rate="1s",
        base_provider="bev-nrt",
        no_base_fallback=True,
        whole_day=False,
        offline=False,
        dry_run=False,
        cache_dir=str(tmp_path),
        base_dir=None,
        crx2rnx=None,
        cleanup=False,
        force_download=False,
    )

    def fail_urlretrieve(url: str, target: Path):
        attempted.append(url)
        raise OSError("not available")

    monkeypatch.setattr(cli, "filter_urls_by_remote_listing", lambda urls, cache_dir, *, force=False: urls)
    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlretrieve", fail_urlretrieve)

    with pytest.raises(RuntimeError, match="no usable EUREF base observation files"):
        cli._download_base_files_for_window(
            args,
            datetime(2026, 5, 23, 5, 29, tzinfo=UTC),
            datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
        )

    assert attempted
    assert all("highrate" in url for url in attempted)


def test_low_resolution_accepts_30s(tmp_path: Path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    low_file = tmp_path / "CPAR00CZE_R_20261430500_01H_30S_MO.rnx"
    low_file.write_text("low-rate\n", encoding="ascii")
    args = argparse.Namespace(
        station="CPAR",
        station_long=None,
        base_resolution="low",
        base_rinex_version="3",
        base_rate="30s",
        base_provider="bev-nrt",
        no_base_fallback=False,
        whole_day=False,
        offline=False,
        dry_run=False,
        cache_dir=str(tmp_path),
        base_dir=None,
        crx2rnx=None,
        cleanup=False,
        force_download=False,
    )
    monkeypatch.setattr(cli, "filter_urls_by_remote_listing", lambda urls, cache_dir, *, force=False: urls)
    monkeypatch.setattr(cli, "download_urls", lambda urls, cache_dir, *, force=False: [low_file])

    paths = cli._download_base_files_for_window(
        args,
        datetime(2026, 5, 23, 5, 29, tzinfo=UTC),
        datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
    )

    assert paths == [low_file]
    assert any("requested_base_resolution=low" in record.getMessage() for record in caplog.records)
    assert any("selected_rate=30s" in record.getMessage() for record in caplog.records)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_download_urls_reuses_normalised_cache_without_network(tmp_path: Path, monkeypatch):
    cached = tmp_path / "CPAR00CZE_R_20261430500_01H_30S_MO.rnx"
    cached.write_text("rinex\n", encoding="ascii")

    def fail_urlretrieve(url: str, target: Path):
        raise AssertionError("network should not be used when converted RINEX is cached")

    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlretrieve", fail_urlretrieve)
    paths = download_urls(
        ["ftp://gnss.bev.gv.at/pub/nrt/143/05/CPAR00CZE_R_20261430500_01H_30S_MO.crx.gz"],
        tmp_path,
    )

    assert paths == [cached]


def test_download_urls_force_download_ignores_normalised_cache(tmp_path: Path, monkeypatch):
    cached = tmp_path / "CPAR00CZE_R_20261430500_01H_30S_MO.rnx"
    cached.write_text("old rinex\n", encoding="ascii")

    def fake_urlretrieve(url: str, target: Path):
        target.write_bytes(b"new archive")
        return str(target), None

    monkeypatch.setattr("um980_rtklib_pipeline.euref.urlretrieve", fake_urlretrieve)
    paths = download_urls(
        ["ftp://gnss.bev.gv.at/pub/nrt/143/05/CPAR00CZE_R_20261430500_01H_30S_MO.crx.gz"],
        tmp_path,
        force=True,
    )

    assert [path.name for path in paths] == ["CPAR00CZE_R_20261430500_01H_30S_MO.crx.gz"]


def test_multiple_base_obs_are_staged_for_rtklib_wildcard(tmp_path: Path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    first = tmp_path / "TUBO00CZE_R_20261430500_01H_30S_MO.rnx"
    second = tmp_path / "TUBO00CZE_R_20261430600_01H_30S_MO.rnx"
    stale = out_dir / "rover.rtklib-base" / "base-stale.rnx"
    stale.parent.mkdir()
    stale.write_text("stale\n", encoding="ascii")
    first.write_text("first\n", encoding="ascii")
    second.write_text("second\n", encoding="ascii")

    pattern = cli._prepare_rtklib_base_obs_argument([first, second], out_dir, "rover")

    assert pattern == out_dir / "rover.rtklib-base" / "base-*.rnx"
    assert not stale.exists()
    assert sorted(path.name for path in pattern.parent.glob("base-*.rnx")) == ["base-0000.rnx", "base-0001.rnx"]
    assert (pattern.parent / "base-0000.rnx").read_text(encoding="ascii") == "first\n"


def test_v2_highrate_fallback_uses_bev_lowrate_provider():
    args = argparse.Namespace(
        base_resolution="high",
        base_rinex_version="2",
        base_rate="1s",
        base_provider="bev-nrt",
        no_base_fallback=False,
    )
    assert cli._base_download_attempts(args) == [
        ("high", "2", "bkg-euref-highrate-v2", "1s"),
        ("low", "2", "bev-nrt", "30s"),
    ]


def test_v2_download_allows_legacy_short_station_code():
    args = argparse.Namespace(
        station="pfa2",
        station_long=None,
        base_rinex_version="2",
    )
    assert cli._resolve_station_for_base_download(args) == "PFA2"


def test_pipeline_passes_base_resolution_to_resolver(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "out"
    nav = tmp_path / "nav.rnx"
    base = tmp_path / "CPAR00CZE_S_20261430530_15M_01S_MO.rnx"
    nav.write_text("nav\n", encoding="ascii")
    base.write_text("base\n", encoding="ascii")
    captured: dict[str, object] = {}
    args = cli.build_parser().parse_args(
        [
            "pipeline",
            "rover.unc",
            "--out-dir",
            str(out_dir),
            "--basename",
            "rover",
            "--download-base",
            "--station",
            "CPAR",
            "--base-resolution",
            "high",
            "--run-rtklib",
            "--nav-file",
            str(nav),
            "--base-position-source",
            "none",
        ]
    )

    monkeypatch.setattr(cli, "cmd_extract", lambda _args: 0)
    monkeypatch.setattr(cli, "cmd_rinex", lambda _args: 0)
    monkeypatch.setattr(
        cli,
        "read_rinex_obs_time_span",
        lambda _path: argparse.Namespace(
            start=datetime(2026, 5, 23, 5, 29, tzinfo=UTC),
            end=datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
        ),
    )

    def fake_download(download_args, start, end):
        captured["base_resolution"] = download_args.base_resolution
        captured["no_base_fallback"] = download_args.no_base_fallback
        return [base]

    monkeypatch.setattr(cli, "_download_base_files_for_window", fake_download)
    monkeypatch.setattr(cli, "filter_rinex_obs_by_overlap", lambda _rover, base_obs: (base_obs, []))
    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool, rtklib_dir=None: tool)

    class Candidate:
        path = nav

    class Resolution:
        warnings: list[str] = []
        selected = [Candidate()]

    monkeypatch.setattr(cli, "resolve_nav_sources", lambda **_kwargs: Resolution())
    monkeypatch.setattr(cli, "_run_rtklib_output_formats", lambda **_kwargs: [])

    assert cli.cmd_pipeline(args) == 0
    assert captured == {"base_resolution": "high", "no_base_fallback": False}


def test_no_base_fallback_is_honoured_by_pipeline(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "out"
    nav = tmp_path / "nav.rnx"
    base = tmp_path / "CPAR00CZE_S_20261430530_15M_01S_MO.rnx"
    nav.write_text("nav\n", encoding="ascii")
    base.write_text("base\n", encoding="ascii")
    captured: dict[str, object] = {}
    args = cli.build_parser().parse_args(
        [
            "pipeline",
            "rover.unc",
            "--out-dir",
            str(out_dir),
            "--basename",
            "rover",
            "--download-base",
            "--station",
            "CPAR",
            "--base-resolution",
            "high",
            "--no-base-fallback",
            "--run-rtklib",
            "--nav-file",
            str(nav),
            "--base-position-source",
            "none",
        ]
    )

    monkeypatch.setattr(cli, "cmd_extract", lambda _args: 0)
    monkeypatch.setattr(cli, "cmd_rinex", lambda _args: 0)
    monkeypatch.setattr(
        cli,
        "read_rinex_obs_time_span",
        lambda _path: argparse.Namespace(
            start=datetime(2026, 5, 23, 5, 29, tzinfo=UTC),
            end=datetime(2026, 5, 23, 5, 31, tzinfo=UTC),
        ),
    )

    def fake_download(download_args, start, end):
        captured["base_resolution"] = download_args.base_resolution
        captured["no_base_fallback"] = download_args.no_base_fallback
        return [base]

    monkeypatch.setattr(cli, "_download_base_files_for_window", fake_download)
    monkeypatch.setattr(cli, "filter_rinex_obs_by_overlap", lambda _rover, base_obs: (base_obs, []))
    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool, rtklib_dir=None: tool)

    class Candidate:
        path = nav

    class Resolution:
        warnings: list[str] = []
        selected = [Candidate()]

    monkeypatch.setattr(cli, "resolve_nav_sources", lambda **_kwargs: Resolution())
    monkeypatch.setattr(cli, "_run_rtklib_output_formats", lambda **_kwargs: [])

    assert cli.cmd_pipeline(args) == 0
    assert captured == {"base_resolution": "high", "no_base_fallback": True}


def test_pipeline_executes_rtklib_with_generated_rover_obs(tmp_path: Path, monkeypatch):
    calls: dict[str, object] = {}
    nav = tmp_path / "brdc.rnx"
    base_obs = tmp_path / "base.obs"
    base_obs_2 = tmp_path / "base2.obs"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    rover_nav = [
        out_dir / "rover.rover-gps.nav",
        out_dir / "rover.rover-glo.gnav",
        out_dir / "rover.rover-gal.lnav",
        out_dir / "rover.rover-sbas.sbs",
    ]
    for path in rover_nav:
        path.write_text("data\n", encoding="ascii")
    base_obs.write_text("base one\n", encoding="ascii")
    base_obs_2.write_text("base two\n", encoding="ascii")

    args = argparse.Namespace(
        rover_log="rover.unc",
        out_dir=str(out_dir),
        basename="rover",
        base_obs=[str(base_obs), str(base_obs_2)],
        station=None,
        download_base=False,
        run_rtklib=True,
        rtkconf=str(tmp_path / "rtk.conf"),
        nav_file=[nav],
        nav_merge="all",
        rnx2rtkp="rnx2rtkp",
        rtklib_dir=None,
        output_format="pos",
        rtklib_path_style="unix",
        dry_run=True,
        debug=True,
        rtk_pos_mode="kinematic",
        rtk_frequency="l1+l2+l5",
        navsys="all",
        rtk_navsys=None,
        rtk_elevation_mask=10.0,
        rtk_soltype="combined",
        rtk_ar_mode="continuous",
        rnx2rtkp_option=[],
        base_ecef=None,
        base_llh=None,
        base_position_source="none",
        base_station=None,
        base_position_cache_dir=None,
        verbose=False,
        log_file=None,
    )

    monkeypatch.setattr(cli, "cmd_extract", lambda _args: 0)
    monkeypatch.setattr(cli, "cmd_rinex", lambda _args: 0)
    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool, rtklib_dir=None: tool)

    class Candidate:
        path = nav

    class Resolution:
        warnings: list[str] = []
        selected = [Candidate()]

    def fake_resolve_nav_sources(**kwargs):
        assert kwargs["rover"] == rover_nav
        return Resolution()

    monkeypatch.setattr(cli, "resolve_nav_sources", fake_resolve_nav_sources)

    def fake_run_rnx2rtkp(**kwargs):
        calls.update(kwargs)
        return argparse.Namespace(args=["rnx2rtkp"], output_file=kwargs["output_file"])

    monkeypatch.setattr(cli, "run_rnx2rtkp", fake_run_rnx2rtkp)
    assert cli.cmd_pipeline(args) == 0
    assert calls["rover_obs"] == out_dir / "rover.direct.obs"
    assert calls["base_obs"] == [base_obs, base_obs_2]
    assert calls["base_obs_arg"] == out_dir / "rover.rtklib-base" / "base-*.obs"
    assert calls["nav_files"] == [nav, *rover_nav]
    assert calls["output_file"] == out_dir / "rover-rtk.pos"
    assert calls["debug"] is True


def test_pipeline_runs_rtklib_once_per_requested_output_format(tmp_path: Path, monkeypatch):
    calls: list[dict[str, object]] = []
    nav = tmp_path / "brdc.rnx"
    base_obs = tmp_path / "base.obs"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    nav.write_text("nav\n", encoding="ascii")
    base_obs.write_text("base\n", encoding="ascii")

    args = argparse.Namespace(
        rover_log="rover.unc",
        out_dir=str(out_dir),
        basename="rover",
        base_obs=[str(base_obs)],
        station=None,
        download_base=False,
        run_rtklib=True,
        rtkconf=str(tmp_path / "rtk.conf"),
        nav_file=[nav],
        nav_merge="all",
        rnx2rtkp="rnx2rtkp",
        rtklib_dir=None,
        output_format=["pos,nmea"],
        rtklib_path_style="unix",
        dry_run=True,
        debug=False,
        rtk_pos_mode="kinematic",
        rtk_frequency="l1+l2+l5",
        navsys="all",
        rtk_navsys=None,
        rtk_elevation_mask=10.0,
        rtk_soltype="combined",
        rtk_ar_mode="continuous",
        rnx2rtkp_option=[],
        base_ecef=None,
        base_llh=None,
        base_position_source="none",
        base_station=None,
        base_position_cache_dir=None,
        verbose=False,
        log_file=None,
    )

    monkeypatch.setattr(cli, "cmd_extract", lambda _args: 0)
    monkeypatch.setattr(cli, "cmd_rinex", lambda _args: 0)
    monkeypatch.setattr(cli, "resolve_rtklib_tool", lambda tool, rtklib_dir=None: tool)

    class Candidate:
        path = nav

    class Resolution:
        warnings: list[str] = []
        selected = [Candidate()]

    monkeypatch.setattr(cli, "resolve_nav_sources", lambda **_kwargs: Resolution())

    def fake_run_rnx2rtkp(**kwargs):
        calls.append(kwargs)
        return argparse.Namespace(args=["rnx2rtkp", str(kwargs["output_file"])], output_file=kwargs["output_file"])

    monkeypatch.setattr(cli, "run_rnx2rtkp", fake_run_rnx2rtkp)

    assert cli.cmd_pipeline(args) == 0

    assert [call["output_file"] for call in calls] == [out_dir / "rover-rtk.pos", out_dir / "rover-rtk.nmea"]
    assert calls[0]["rtk_options"] is None
    assert calls[1]["rtk_options"] == ["-n"]
