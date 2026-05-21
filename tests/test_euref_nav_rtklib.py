from datetime import UTC, datetime
from pathlib import Path

import pytest

from um980_rtklib_pipeline.euref import download_urls, planned_urls, resolve_station
from um980_rtklib_pipeline.nav_resolver import resolve_nav_sources
from um980_rtklib_pipeline import cli, rtklib
from um980_rtklib_pipeline.rtklib import (
    build_rnx2rtkp_command,
    cygdrive_to_windows,
    detect_rtklib_path_style,
    path_for_rtklib_argument,
    validate_rtklib_inputs,
)


def test_station_alias_and_bev_url():
    assert resolve_station("CPAR") == "CPAR00CZE"
    urls = planned_urls(
        station="CPAR",
        start=datetime(2026, 5, 20, 12, 10, tzinfo=UTC),
        end=datetime(2026, 5, 20, 12, 20, tzinfo=UTC),
    )
    assert "CPAR00CZE_R_20261401200_01H_30S_MO.crx.gz" in urls[0]


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
    assert cli._tool_from_rtklib_dir("rnx2rtkp.exe", "/opt/rtklib") == "/opt/rtklib/rnx2rtkp.exe"
    assert cli._tool_from_rtklib_dir("/custom/rnx2rtkp", "/opt/rtklib") == "/custom/rnx2rtkp"


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
