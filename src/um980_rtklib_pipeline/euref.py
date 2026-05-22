"""EUREF/EPN base observation URL planning and normalisation."""

from __future__ import annotations

import gzip
import html
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, urlretrieve


STATION_ALIASES = {
    "CPAR": "CPAR00CZE",
    "KUNZ": "KUNZ00CZE",
    "GOP": "GOP00CZE",
    "TUBO": "TUBO00CZE",
    "GOPE": "GOPE00CZE",
    "GOP7": "GOP700CZE",
    "GRAZ": "GRAZ00AUT",
    "CFRM": "CFRM00CZE",
    "TRF2": "TRF200AUT",
    "MOPI": "MOPI00SVK",
    "MOP2": "MOP200SVK",
}


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    templates: tuple[str, ...]


@dataclass(frozen=True)
class BasePosition:
    station: str
    ecef_xyz_m: tuple[float, float, float]
    source: str
    frame: str | None = None
    epoch: str | None = None
    valid_from_to: str | None = None


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
            "https://igs.bkg.bund.de/root_ftp/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
            "https://igs.bkg.bund.de/root_ftp/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.rnx.gz",
        ),
    ),
    "bkg-euref-highrate": Provider(
        "bkg_euref_highrate_v3",
        "obs",
        (
            "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station_long}_S_{yyyy}{doy}{hh}{minute}_15M_01S_MO.crx.gz",
        ),
    ),
    "bev-nrt-v2": Provider(
        "bev_nrt_v2_hourly",
        "obs",
        (
            "ftp://gnss.bev.gv.at/pub/nrt/{doy}/{hh}/{station4}{doy}{hour_letter}.{yy}d.gz",
        ),
    ),
    "bkg-euref-nrt-v2": Provider(
        "bkg_euref_nrt_v2_hourly",
        "obs",
        (
            "https://igs.bkg.bund.de/root_ftp/EUREF/nrt/{doy}/{hh}/{station4}{doy}{hour_letter}.{yy}d.Z",
            "https://igs.bkg.bund.de/root_ftp/EUREF/nrt/{doy}/{hh}/{station4}{doy}{hour_letter}.{yy}d.gz",
            "https://igs.bkg.bund.de/root_ftp/EUREF/nrt/{doy}/{hh}/{station4}{doy}{hour_letter}.{yy}o.gz",
        ),
    ),
    "bkg-euref-highrate-v2": Provider(
        "bkg_euref_highrate_v2",
        "obs",
        (
            "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station4}{doy}{hour_letter}{minute}.{yy}d.Z",
            "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station4}{doy}{hour_letter}{minute}.{yy}d.gz",
            "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station4}{doy}{hour_letter}{minute}.{yy}o.gz",
        ),
    ),
}


def resolve_station(station: str, station_long: str | None = None) -> str:
    if station_long:
        return _normalise_station_marker(station_long)
    code = station.upper()
    normalised = _normalise_station_marker(code)
    if len(normalised) == 9:
        return normalised
    if code in STATION_ALIASES:
        return STATION_ALIASES[code]
    raise ValueError(
        f"station {station} could not be resolved to a RINEX 3 marker name. "
        "Use --station-long or add an alias to the configuration."
    )


def _normalise_station_marker(station: str) -> str:
    """Return a 9-character EUREF/RINEX 3 marker when a trailing zero is given."""

    code = station.upper()
    if len(code) == 10 and code.endswith("0"):
        return code[:-1]
    return code


def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _format_vars(dt: datetime, station_long: str, minute: int = 0) -> dict[str, str]:
    doy = f"{dt.timetuple().tm_yday:03d}"
    hh = f"{dt.hour:02d}"
    return {
        "yyyy": f"{dt.year:04d}",
        "yy": f"{dt.year % 100:02d}",
        "doy": doy,
        "hh": hh,
        "minute": f"{minute:02d}",
        "hour_letter": chr(ord("a") + dt.hour),
        "station_long": station_long,
        "station4": station_long[:4].lower(),
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
    rinex_version: str = "3",
) -> list[str]:
    resolved = _resolve_station_for_url_planning(station, station_long, rinex_version)
    provider_name = _provider_for_version(provider_name, base_rate, rinex_version)
    provider = PROVIDERS[provider_name]
    urls: list[str] = []
    if provider_name in {"bkg-euref-highrate", "bkg-euref-highrate-v2"} or base_rate == "1s":
        for hour in overlapping_hours(start, end, whole_day):
            for minute in (0, 15, 30, 45):
                chunk_start = hour.replace(minute=minute)
                chunk_end = chunk_start + timedelta(minutes=15)
                if chunk_end < start or chunk_start > end:
                    continue
                for template in provider.templates:
                    urls.append(template.format(**_format_vars(hour, resolved, minute)))
        return urls
    for hour in overlapping_hours(start, end, whole_day):
        for template in provider.templates:
            urls.append(template.format(**_format_vars(hour, resolved)))
    return urls


def _resolve_station_for_url_planning(
    station: str,
    station_long: str | None,
    rinex_version: str,
) -> str:
    try:
        return resolve_station(station, station_long)
    except ValueError:
        if rinex_version == "2" and station_long is None and re.fullmatch(r"[A-Za-z0-9]{4}", station):
            return station.upper()
        raise


def _provider_for_version(provider_name: str, base_rate: str, rinex_version: str) -> str:
    if rinex_version not in {"2", "3"}:
        raise ValueError(f"unsupported EUREF RINEX source version: {rinex_version}")
    if rinex_version == "3":
        return provider_name
    if provider_name in {"bkg-euref-highrate", "bkg-euref-highrate-v2"} or base_rate == "1s":
        return "bkg-euref-highrate-v2"
    if provider_name == "bev-nrt":
        return "bev-nrt-v2"
    return "bkg-euref-nrt-v2"


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
    if current.suffix.lower() == ".z":
        gzip_exe = shutil.which("gzip")
        if not gzip_exe:
            raise RuntimeError(f"gzip is required to decompress Unix-compress file {current}")
        target = current.with_suffix("")
        if not target.exists():
            with target.open("wb") as dst:
                result = subprocess.run(
                    [gzip_exe, "-cd", str(current)],
                    check=False,
                    stdout=dst,
                    stderr=subprocess.PIPE,
                    text=False,
                )
            if result.returncode != 0:
                target.unlink(missing_ok=True)
                stderr = result.stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(f"gzip failed for {current}: {stderr}")
        current = target
    if current.suffix.lower() == ".gz":
        target = current.with_suffix("")
        if not target.exists():
            with gzip.open(current, "rb") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        current = target
    if _is_hatanaka_observation_file(current):
        if not crx2rnx:
            raise RuntimeError(f"crx2rnx required for Hatanaka file {current}")
        result = subprocess.run([crx2rnx, str(current)], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"crx2rnx failed for {current}: {result.stderr.strip()}")
        produced = _hatanaka_output_path(current)
        if produced.exists():
            if cleanup:
                current.unlink()
            return produced
    return current


def _is_hatanaka_observation_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in {".crx", ".d"} or bool(re.fullmatch(r"\.\d{2}d", suffix))


def _hatanaka_output_path(path: Path) -> Path:
    suffix = path.suffix.lower()
    if re.fullmatch(r"\.\d{2}d", suffix):
        return path.with_suffix(suffix[:-1] + "o")
    return path.with_suffix(".rnx")


def epn_station_coordinate_url(station: str) -> str:
    """Return the official EPN station coordinate page URL."""

    return "https://www.epncb.oma.be/_productsservices/coordinates/crd4station.php?station=" + resolve_station(station)


def _strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _first_float(value: str) -> float:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    if not match:
        raise ValueError(f"no numeric coordinate value found in {value!r}")
    return float(match.group(0))


def parse_epn_station_position(page: str, station: str, *, frame: str = "ETRF2000") -> BasePosition:
    """Parse an EPN station coordinate page and return the latest ECEF XYZ row."""

    marker = f"expressed in {frame}"
    if marker not in page:
        raise ValueError(f"EPN coordinate page for {station} has no {frame} coordinate table")
    section = page.split(marker, 1)[1]
    table = section.split("</table>", 1)[0]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.IGNORECASE | re.DOTALL)
    for row in rows:
        cells = [
            _strip_html(cell)
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        ]
        if len(cells) < 9:
            continue
        x, y, z = (_first_float(cells[3]), _first_float(cells[4]), _first_float(cells[5]))
        return BasePosition(
            station=resolve_station(station),
            ecef_xyz_m=(x, y, z),
            source=epn_station_coordinate_url(station),
            frame=frame,
            epoch=cells[2],
            valid_from_to=cells[0],
        )
    raise ValueError(f"EPN coordinate page for {station} contains no parseable {frame} coordinate rows")


def fetch_epn_station_position(
    station: str,
    *,
    cache_dir: Path | None = None,
    timeout_s: float = 20.0,
    frame: str = "ETRF2000",
) -> BasePosition:
    """Fetch and parse the official EPN ECEF position for a station."""

    resolved = resolve_station(station)
    cache_path = cache_dir / f"{resolved}.epn-coordinates.html" if cache_dir else None
    page: str
    if cache_path and cache_path.exists() and cache_path.stat().st_size > 0:
        page = cache_path.read_text(encoding="utf-8", errors="ignore")
    else:
        with urlopen(epn_station_coordinate_url(resolved), timeout=timeout_s) as response:  # noqa: S310
            page = response.read().decode("utf-8", errors="ignore")
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(page, encoding="utf-8")
    return parse_epn_station_position(page, resolved, frame=frame)


def parse_rinex_approx_position(path: Path) -> BasePosition:
    """Read `APPROX POSITION XYZ` from a RINEX OBS header."""

    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read RINEX header for base position: {path}") from exc
    marker_name = path.stem
    for line in lines:
        if "MARKER NAME" in line:
            value = line[:60].strip()
            if value:
                marker_name = value
        if "APPROX POSITION XYZ" in line:
            fields = line[:60].split()
            if len(fields) < 3:
                raise ValueError(f"RINEX header has incomplete APPROX POSITION XYZ: {path}")
            return BasePosition(
                station=marker_name,
                ecef_xyz_m=(float(fields[0]), float(fields[1]), float(fields[2])),
                source=str(path),
                frame="rinex-header",
            )
        if "END OF HEADER" in line:
            break
    raise ValueError(f"RINEX OBS header has no APPROX POSITION XYZ: {path}")
