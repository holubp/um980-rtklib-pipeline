"""EUREF/EPN base observation URL planning and normalisation."""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlretrieve


STATION_ALIASES = {
    "CPAR": "CPAR00CZE",
    "KUNZ": "KUNZ00CZE",
    "TUBO": "TUBO00CZE",
    "GOPE": "GOPE00CZE",
    "GOP7": "GOP700CZE",
    "GRAZ": "GRAZ00AUT",
}


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    templates: tuple[str, ...]


PROVIDERS = {
    "bev-nrt": Provider(
        "bev_nrt_v3_hourly",
        "obs",
        (
            "ftp://gnss.bev.gv.at/pub/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
            "ftp://gnss.bev.gv.at/pub/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.rnx.gz",
        ),
    ),
    "bkg-euref-nrt": Provider(
        "bkg_euref_nrt_v3_hourly",
        "obs",
        (
            "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
            "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.rnx.gz",
        ),
    ),
    "bkg-euref-highrate": Provider(
        "bkg_euref_highrate_v3",
        "obs",
        (
            "ftp://igs.bkg.bund.de/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station_long}_S_{yyyy}{doy}{hh}{minute}_15M_01S_MO.crx.gz",
        ),
    ),
}


def resolve_station(station: str, station_long: str | None = None) -> str:
    if station_long:
        return station_long.upper()
    code = station.upper()
    if len(code) == 9:
        return code
    if code in STATION_ALIASES:
        return STATION_ALIASES[code]
    raise ValueError(
        f"station {station} could not be resolved to a RINEX 3 marker name. "
        "Use --station-long or add an alias to the configuration."
    )


def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _format_vars(dt: datetime, station_long: str, minute: int = 0) -> dict[str, str]:
    doy = f"{dt.timetuple().tm_yday:03d}"
    hh = f"{dt.hour:02d}"
    return {
        "yyyy": f"{dt.year:04d}",
        "doy": doy,
        "hh": hh,
        "minute": f"{minute:02d}",
        "hour_letter": chr(ord("a") + dt.hour),
        "station_long": station_long,
    }


def overlapping_hours(start: datetime, end: datetime, whole_day: bool = False) -> list[datetime]:
    if whole_day:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
    current = _hour_floor(start)
    hours = []
    while current <= end:
        hours.append(current)
        current += timedelta(hours=1)
    return hours


def planned_urls(
    *,
    station: str,
    start: datetime,
    end: datetime,
    provider_name: str = "bev-nrt",
    station_long: str | None = None,
    base_rate: str = "30s",
    whole_day: bool = False,
) -> list[str]:
    resolved = resolve_station(station, station_long)
    provider = PROVIDERS[provider_name]
    urls: list[str] = []
    if provider_name == "bkg-euref-highrate" or base_rate == "1s":
        for hour in overlapping_hours(start, end, whole_day):
            for minute in (0, 15, 30, 45):
                chunk_start = hour.replace(minute=minute)
                chunk_end = chunk_start + timedelta(minutes=15)
                if chunk_end < start or chunk_start > end:
                    continue
                for template in PROVIDERS["bkg-euref-highrate"].templates:
                    urls.append(template.format(**_format_vars(hour, resolved, minute)))
        return urls
    for hour in overlapping_hours(start, end, whole_day):
        for template in provider.templates:
            urls.append(template.format(**_format_vars(hour, resolved)))
    return urls


def download_urls(urls: list[str], cache_dir: Path, *, retries: int = 1) -> list[Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    failures: list[str] = []
    for url in urls:
        target = cache_dir / url.rsplit("/", 1)[-1]
        if target.exists() and target.stat().st_size > 0:
            paths.append(target)
            continue
        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                urlretrieve(url, target)  # noqa: S310 - explicit user-triggered download
                if target.stat().st_size > 0:
                    paths.append(target)
                    break
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
        else:
            if last_error:
                message = f"failed to download {url}: {last_error}"
                failures.append(message)
                logging.warning("%s", message)
            else:
                message = f"failed to download {url}: downloaded file was empty"
                failures.append(message)
                logging.warning("%s", message)
    if not paths and failures:
        raise RuntimeError(
            "no base observation files could be downloaded. Tried URLs:\n"
            + "\n".join(f"- {url}" for url in urls)
        )
    return paths


def normalise_rinex_file(path: Path, *, crx2rnx: str | None = None, cleanup: bool = False) -> Path:
    """Convert compressed/Hatanaka files to ordinary RINEX where possible."""

    current = path
    if current.suffix.lower() == ".gz":
        target = current.with_suffix("")
        if not target.exists():
            with gzip.open(current, "rb") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        current = target
    if current.suffix.lower() in {".crx", ".d"}:
        if not crx2rnx:
            raise RuntimeError(f"crx2rnx required for Hatanaka file {current}")
        result = subprocess.run([crx2rnx, str(current)], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"crx2rnx failed for {current}: {result.stderr.strip()}")
        produced = current.with_suffix(".rnx")
        if produced.exists():
            if cleanup:
                current.unlink()
            return produced
    return current
