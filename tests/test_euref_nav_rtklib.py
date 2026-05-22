import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.euref import (
    download_urls,
    normalise_rinex_file,
    parse_epn_station_position,
    parse_rinex_approx_position,
    planned_urls,
    resolve_station,
)
from um980_rtklib_pipeline.nav_resolver import resolve_nav_sources
from um980_rtklib_pipeline import rtklib
from um980_rtklib_pipeline.rtklib import (
    _warn_about_rtklib_result,
    build_rnx2rtkp_command,
    cygdrive_to_windows,
    detect_rtklib_path_style,
    executable_for_subprocess,
    path_for_rtklib_argument,
    resolve_rtklib_tool,
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


def test_normalise_rinex_v2_hatanaka_suffix(tmp_path: Path, monkeypatch):
    compressed = tmp_path / "cpar138h.26d"
    produced = tmp_path / "cpar138h.26o"
    compressed.write_text("hatanaka", encoding="ascii")

    def fake_run(args: list[str], check: bool, capture_output: bool, text: bool):
        produced.write_text(
            "     2.11           OBSERVATION DATA    G                   RINEX VERSION / TYPE\n"
            "                                                            END OF HEADER\n",
            encoding="ascii",
        )
        return argparse.Namespace(returncode=0, stderr="")

    monkeypatch.setattr("um980_rtklib_pipeline.euref.subprocess.run", fake_run)
    assert normalise_rinex_file(compressed, crx2rnx="crx2rnx") == produced


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


def test_non_executable_local_tool_is_mirrored_for_subprocess(tmp_path: Path):
    tool = tmp_path / "rnx2rtkp"
    tool.write_bytes(b"binary-placeholder")
    tool.chmod(0o600)
    mirrored = Path(executable_for_subprocess(str(tool)))
    assert mirrored.exists()
    assert mirrored != tool
    assert mirrored.stat().st_mode & 0o111


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
    )

    monkeypatch.setattr(
        cli,
        "_time_window_from_solutions",
        lambda _args, _margin: (
            datetime(2026, 5, 18, 7, 20, tzinfo=UTC),
            datetime(2026, 5, 18, 7, 25, tzinfo=UTC),
        ),
    )

    def fake_download_urls(urls: list[str], cache_dir: Path):
        attempts.append(urls[0])
        if "highrate" in urls[0]:
            raise RuntimeError("404")
        low_file.write_text(
            "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
            "                                                            END OF HEADER\n",
            encoding="ascii",
        )
        return [low_file]

    monkeypatch.setattr(cli, "download_urls", fake_download_urls)
    paths = cli._download_base_files(args)
    assert paths == [low_file]
    assert "highrate" in attempts[0]
    assert "nrt" in attempts[1]
    assert any("trying provider=bev-nrt rate=30s rinex=3" in record.getMessage() for record in caplog.records)


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


def test_pipeline_executes_rtklib_with_generated_rover_obs(tmp_path: Path, monkeypatch):
    calls: dict[str, object] = {}
    nav = tmp_path / "brdc.rnx"
    base_obs = tmp_path / "base.obs"
    out_dir = tmp_path / "out"

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
        output_format="pos",
        rtklib_path_style="unix",
        dry_run=True,
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
        calls.update(kwargs)
        return argparse.Namespace(args=["rnx2rtkp"], output_file=kwargs["output_file"])

    monkeypatch.setattr(cli, "run_rnx2rtkp", fake_run_rnx2rtkp)
    assert cli.cmd_pipeline(args) == 0
    assert calls["rover_obs"] == out_dir / "rover.direct.obs"
    assert calls["base_obs"] == [base_obs]
    assert calls["nav_files"] == [nav]
    assert calls["output_file"] == out_dir / "rover-rtk.pos"
