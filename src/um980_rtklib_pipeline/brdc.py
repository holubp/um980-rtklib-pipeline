"""External broadcast navigation download helpers."""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib import request


BRDC_PROVIDER_ORDER = ("bkg-igs-brdc", "bkg-igs-nrt", "bkg-mgex-brdc", "bkg-euref-brdc")


@dataclass(frozen=True)
class NavUrlPlan:
    """Planned external broadcast NAV download.

    Attributes:
        provider: Provider key selected by the user or auto policy.
        url: Remote URL.
        local_gzip: Local cache path for the compressed file.
        local_path: Local decompressed RINEX NAV path.
        start_time: Start of the nominal day covered by the product.
        end_time: End of the nominal day covered by the product.
    """

    provider: str
    url: str
    local_gzip: Path
    local_path: Path
    start_time: datetime
    end_time: datetime


def planned_brdc_urls(
    start: datetime,
    end: datetime,
    provider: str = "auto",
    *,
    cache_dir: str | Path = ".",
) -> list[NavUrlPlan]:
    """Plan external BRDC/BRDM mixed broadcast NAV URLs for a time window.

    Args:
        start: First rover/base time requiring NAV coverage.
        end: Last rover/base time requiring NAV coverage.
        provider: Provider key or ``auto``.
        cache_dir: Local directory where files are cached.

    Returns:
        Planned URL/cache pairs. No network access is performed.
    """

    if provider == "none":
        return []
    providers = BRDC_PROVIDER_ORDER if provider == "auto" else (provider,)
    out_dir = Path(cache_dir)
    plans: list[NavUrlPlan] = []
    for day in _covered_days(start, end):
        year = day.year
        doy = int(day.strftime("%j"))
        day_start = datetime(day.year, day.month, day.day)
        day_end = day_start + timedelta(days=1)
        for provider_name in providers:
            filename, url = _provider_filename_url(provider_name, year, doy)
            local_gzip = out_dir / filename
            local_path = out_dir / filename.removesuffix(".gz")
            plans.append(
                NavUrlPlan(
                    provider=provider_name,
                    url=url,
                    local_gzip=local_gzip,
                    local_path=local_path,
                    start_time=day_start,
                    end_time=day_end,
                )
            )
    return plans


def download_brdc_nav(
    start: datetime,
    end: datetime,
    *,
    provider: str = "auto",
    cache_dir: str | Path,
    offline: bool = False,
    dry_run: bool = False,
    cleanup: bool = False,
) -> list[Path]:
    """Download external broadcast NAV files for a time window.

    Args:
        start: First rover/base time requiring NAV coverage.
        end: Last rover/base time requiring NAV coverage.
        provider: Provider key or ``auto``.
        cache_dir: Cache directory for compressed and decompressed files.
        offline: Reuse only files already present in ``cache_dir``.
        dry_run: Plan URLs but do not download.
        cleanup: Remove compressed files after successful decompression.

    Returns:
        Decompressed RINEX NAV paths that are present locally.
    """

    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    selected: list[Path] = []
    for plan in planned_brdc_urls(start, end, provider, cache_dir=out_dir):
        if plan.local_path.exists():
            selected.append(plan.local_path)
            continue
        if offline or dry_run:
            logging.info("external BRDC candidate not present locally: %s", plan.local_path)
            continue
        if not plan.local_gzip.exists():
            logging.info("downloading external BRDC NAV: provider=%s url=%s", plan.provider, plan.url)
            try:
                request.urlretrieve(plan.url, plan.local_gzip)
            except Exception as exc:  # pragma: no cover - network failures depend on provider availability
                logging.warning("failed to download external BRDC NAV %s: %s", plan.url, exc)
                continue
        _decompress_gzip(plan.local_gzip, plan.local_path)
        selected.append(plan.local_path)
        if cleanup:
            plan.local_gzip.unlink(missing_ok=True)
    return _dedupe_paths(selected)


def _covered_days(start: datetime, end: datetime) -> list[datetime]:
    current = datetime(start.year, start.month, start.day)
    stop = datetime(end.year, end.month, end.day)
    days: list[datetime] = []
    while current <= stop:
        days.append(current)
        current += timedelta(days=1)
    return days


def _provider_filename_url(provider: str, year: int, doy: int) -> tuple[str, str]:
    if provider == "bkg-igs-brdc":
        filename = f"BRDC00IGS_R_{year}{doy:03d}0000_01D_MN.rnx.gz"
        return filename, f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/{filename}"
    if provider == "bkg-igs-nrt":
        filename = f"BRDC00WRD_S_{year}{doy:03d}0000_01D_MN.rnx.gz"
        return filename, f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy:03d}/{filename}"
    if provider == "bkg-mgex-brdc":
        filename = f"BRDM00DLR_S_{year}{doy:03d}0000_01D_MN.rnx.gz"
        return filename, f"https://igs.bkg.bund.de/root_ftp/MGEX/BRDC/{year}/{doy:03d}/{filename}"
    if provider == "bkg-euref-brdc":
        filename = f"BRDC00WRD_S_{year}{doy:03d}0000_01D_MN.rnx.gz"
        return filename, f"https://igs.bkg.bund.de/root_ftp/EUREF/BRDC/{year}/{doy:03d}/{filename}"
    raise ValueError(f"unsupported NAV provider: {provider}")


def _decompress_gzip(source: Path, destination: Path) -> None:
    with gzip.open(source, "rb") as src, destination.open("wb") as dst:
        shutil_copyfileobj(src, dst)


def shutil_copyfileobj(src, dst) -> None:
    """Small wrapper to keep imports narrow and easy to monkeypatch in tests."""

    import shutil

    shutil.copyfileobj(src, dst)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result
