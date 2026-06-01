from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path

from um980_rtklib_pipeline import brdc


def test_planned_brdc_urls_prefers_rinex3_brdc_for_auto_provider() -> None:
    plans = brdc.planned_brdc_urls(
        datetime(2026, 5, 20, 12, 0),
        datetime(2026, 5, 20, 12, 5),
        provider="auto",
    )

    assert plans[0].provider == "bkg-igs-brdc"
    assert "BRDC00IGS_R_20261400000_01D_MN.rnx.gz" in plans[0].url
    assert any("BRDM00DLR_S_20261400000_01D_MN.rnx.gz" in plan.url for plan in plans)


def test_download_brdc_nav_offline_returns_only_cached_files(tmp_path: Path) -> None:
    cached = tmp_path / "BRDC00IGS_R_20261400000_01D_MN.rnx"
    cached.write_text(
        "     3.04           NAVIGATION DATA     M                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n",
        encoding="ascii",
    )

    paths = brdc.download_brdc_nav(
        datetime(2026, 5, 20, 12, 0),
        datetime(2026, 5, 20, 12, 5),
        provider="bkg-igs-brdc",
        cache_dir=tmp_path,
        offline=True,
    )

    assert paths == [cached]


def test_download_brdc_nav_downloads_and_decompresses_gzip(tmp_path: Path, monkeypatch) -> None:
    gz_payload = gzip.compress(
        b"     3.04           NAVIGATION DATA     M                   RINEX VERSION / TYPE\n"
        b"                                                            END OF HEADER\n"
        b"G01 2026 05 20 00 00 00 0.0 0.0 0.0\n"
    )

    def fake_urlretrieve(url: str, filename: str | Path):
        Path(filename).write_bytes(gz_payload)
        return str(filename), None

    monkeypatch.setattr(brdc.request, "urlretrieve", fake_urlretrieve)

    paths = brdc.download_brdc_nav(
        datetime(2026, 5, 20, 12, 0),
        datetime(2026, 5, 20, 12, 5),
        provider="bkg-igs-brdc",
        cache_dir=tmp_path,
    )

    assert len(paths) == 1
    assert paths[0].suffix == ".rnx"
    assert "G01" in paths[0].read_text(encoding="ascii")
