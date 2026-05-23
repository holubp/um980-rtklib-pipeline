"""EUREF/EPN base observation URL planning and normalisation."""

from __future__ import annotations

import gzip
import html
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, urlretrieve

from .rtklib import detect_rtklib_path_style, format_command, path_for_rtklib_argument


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
    """EUREF download provider definition.

    Attributes:
        name: Internal provider name.
        kind: Product kind, currently observation data.
        templates: URL templates used for planning downloads.
    """

    name: str
    kind: str
    templates: tuple[str, ...]


@dataclass(frozen=True)
class BasePosition:
    """Resolved base station position.

    Attributes:
        station: Station marker name.
        ecef_xyz_m: ECEF XYZ coordinates in meters.
        source: Source URL or file path.
        frame: Coordinate frame when known.
        epoch: Coordinate epoch when known.
        valid_from_to: Validity interval text when known.
    """

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
        ),
    ),
    "bkg-euref-nrt": Provider(
        "bkg_euref_nrt_v3_hourly",
        "obs",
        (
            "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
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
            "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station4}{doy}{hour_letter}.{yy}d.Z",
        ),
    ),
    "bkg-euref-highrate-v2": Provider(
        "bkg_euref_highrate_v2",
        "obs",
        (
            "ftp://igs.bkg.bund.de/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/"
            "{station4}{doy}{hour_letter}{minute}.{yy}d.Z",
        ),
    ),
}


def resolve_station(station: str, station_long: str | None = None) -> str:
    """Resolve a station alias to a RINEX 3 marker.

    Args:
        station: Short alias or marker supplied by the user.
        station_long: Optional explicit marker override.

    Returns:
        Normalised station marker.

    Raises:
        ValueError: If the station cannot be resolved.
    """

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
    """Return hourly boundaries overlapping a time interval.

    Args:
        start: Interval start.
        end: Interval end.
        whole_day: Expand to the whole UTC day containing `start`.

    Returns:
        Hour timestamps used for EUREF hourly URL planning.
    """

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
    """Plan EUREF/EPN observation download URLs.

    Args:
        station: Station alias or marker.
        start: Rover time-window start.
        end: Rover time-window end.
        provider_name: Base provider selector.
        station_long: Optional explicit RINEX 3 station marker.
        base_rate: Desired base data rate, such as `30s` or `1s`.
        whole_day: Plan the whole day instead of only overlapping hours.
        rinex_version: Source RINEX version (`2` or `3`).

    Returns:
        Ordered candidate URLs.
    """

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


def download_urls(urls: list[str], cache_dir: Path, *, retries: int = 1, force: bool = False) -> list[Path]:
    """Download URL candidates into a cache directory.

    Args:
        urls: Candidate URLs to download.
        cache_dir: Local cache directory.
        retries: Attempts per URL.
        force: Download URL targets even when cached products already exist.

    Returns:
        Downloaded or already cached file paths.

    Raises:
        RuntimeError: If every URL fails.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    failures: list[str] = []
    for url in urls:
        target = cache_dir / url.rsplit("/", 1)[-1]
        cached = _cached_product_for_url_target(target)
        if cached is not None and not force:
            logging.info("using cached EUREF base candidate: %s", cached)
            paths.append(cached)
            continue
        if target.exists() and target.stat().st_size > 0 and not force:
            logging.info("using cached EUREF base candidate: %s", target)
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


def _cached_product_for_url_target(target: Path) -> Path | None:
    """Return an already usable cache product corresponding to a planned URL.

    The downloader plans archive URLs, but previous runs may already have
    decompressed or Hatanaka-converted those archives. Reusing those products
    avoids network access and avoids re-running `crx2rnx` unless the user
    explicitly requests `--force-download`.
    """

    for candidate in _cache_variants_for_target(target):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _cache_variants_for_target(target: Path) -> list[Path]:
    variants: list[Path] = []
    decompressed = target.with_suffix("") if target.suffix.lower() in {".gz", ".z"} else target
    suffix = decompressed.suffix.lower()
    if suffix == ".crx":
        variants.append(decompressed.with_suffix(".rnx"))
        variants.append(decompressed)
    elif suffix == ".d":
        variants.append(decompressed.with_suffix(".o"))
        variants.append(decompressed)
    elif re.fullmatch(r"\.\d{2}d", suffix):
        variants.append(decompressed.with_suffix(suffix[:-1] + "o"))
        variants.append(decompressed)
    else:
        variants.append(decompressed)
    return list(dict.fromkeys(variants))


def normalise_rinex_file(
    path: Path,
    *,
    crx2rnx: str | None = None,
    cleanup: bool = False,
    crx2rnx_timeout_s: float = 300.0,
) -> Path:
    """Convert compressed/Hatanaka files to ordinary RINEX where possible.

    Args:
        path: Input observation file.
        crx2rnx: Optional Hatanaka converter executable.
        cleanup: Remove intermediate Hatanaka file after conversion.
        crx2rnx_timeout_s: Maximum seconds allowed for one Hatanaka
            conversion.

    Returns:
        Normalised RINEX path.

    Raises:
        RuntimeError: If decompression or Hatanaka conversion fails.
    """

    if requires_crx2rnx(path) and not crx2rnx:
        raise RuntimeError(f"crx2rnx required for Hatanaka file {path} before decompression")

    current = path
    if current.suffix.lower() == ".z":
        gzip_exe = shutil.which("gzip")
        if not gzip_exe:
            raise RuntimeError(f"gzip is required to decompress Unix-compress file {current}")
        target = current.with_suffix("")
        if not target.exists():
            logging.info("decompressing Unix-compress RINEX file: %s -> %s", current, target)
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
        else:
            logging.info("using existing decompressed RINEX candidate: %s", target)
        current = target
    if current.suffix.lower() == ".gz":
        target = current.with_suffix("")
        if not target.exists():
            logging.info("decompressing gzip RINEX file: %s -> %s", current, target)
            with gzip.open(current, "rb") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            logging.info("using existing decompressed RINEX candidate: %s", target)
        current = target
    if _is_hatanaka_observation_file(current):
        if not crx2rnx:
            raise RuntimeError(f"crx2rnx required for Hatanaka file {current}")
        produced = _hatanaka_output_path(current)
        result = _run_crx2rnx(crx2rnx, current, produced, timeout_s=crx2rnx_timeout_s)
        if result.returncode not in {0, 2}:
            raise RuntimeError(f"crx2rnx failed for {current}: {result.stderr.strip() or result.stdout.strip()}")
        if produced.exists():
            if produced.stat().st_size <= 0:
                raise RuntimeError(f"crx2rnx produced an empty RINEX file: {produced}")
            if result.returncode == 2:
                logging.warning("crx2rnx converted %s with warnings: %s", current, result.stderr.strip() or result.stdout.strip())
            if cleanup:
                current.unlink()
            return produced
        raise RuntimeError(f"crx2rnx completed for {current} but did not create {produced}")
    return current


def _run_crx2rnx(crx2rnx: str, current: Path, produced: Path, *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run `crx2rnx` with overwrite and progress-safe subprocess handling."""

    path_style = detect_rtklib_path_style(crx2rnx)
    command = [crx2rnx, path_for_rtklib_argument(current, path_style), "-f"]
    logging.info("converting Hatanaka RINEX: %s -> %s", current, produced)
    logging.debug("crx2rnx command: %s", format_command(command))
    try:
        process = subprocess.Popen(  # noqa: S603 - executable is user/local RTKLIB tool.
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start crx2rnx for {current}: {exc}") from exc

    deadline = time.monotonic() + timeout_s
    stdout = ""
    stderr = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"crx2rnx timed out after {timeout_s:.0f}s for {current}. "
                f"stdout={stdout.strip()} stderr={stderr.strip()}"
            )
        try:
            stdout, stderr = process.communicate(timeout=min(10.0, remaining))
            break
        except subprocess.TimeoutExpired:
            logging.info("still converting Hatanaka RINEX: %s", current)

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def requires_crx2rnx(path: Path) -> bool:
    """Return true when a RINEX observation file needs Hatanaka conversion.

    Args:
        path: RINEX observation file path, optionally with `.gz` or `.Z`
            compression still present.

    Returns:
        True for compressed or uncompressed Hatanaka observation names.
    """

    candidate = path
    if candidate.suffix.lower() in {".gz", ".z"}:
        candidate = candidate.with_suffix("")
    return _is_hatanaka_observation_file(candidate)


def _is_hatanaka_observation_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in {".crx", ".d"} or bool(re.fullmatch(r"\.\d{2}d", suffix))


def _hatanaka_output_path(path: Path) -> Path:
    suffix = path.suffix.lower()
    if re.fullmatch(r"\.\d{2}d", suffix):
        return path.with_suffix(suffix[:-1] + "o")
    return path.with_suffix(".rnx")


def epn_station_coordinate_url(station: str) -> str:
    """Return the official EPN station coordinate page URL.

    Args:
        station: Station alias or marker.

    Returns:
        EPN station coordinate page URL.
    """

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
    """Parse an EPN station coordinate page.

    Args:
        page: HTML page content.
        station: Station alias or marker.
        frame: Coordinate frame table to parse.

    Returns:
        Latest parseable ECEF XYZ row.

    Raises:
        ValueError: If the requested frame or coordinate row is missing.
    """

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
    """Fetch and parse the official EPN ECEF position for a station.

    Args:
        station: Station alias or marker.
        cache_dir: Optional HTML cache directory.
        timeout_s: Network timeout in seconds.
        frame: Coordinate frame table to parse.

    Returns:
        Parsed base station position.
    """

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
    """Read `APPROX POSITION XYZ` from a RINEX OBS header.

    Args:
        path: RINEX observation file.

    Returns:
        Base position from the header.

    Raises:
        ValueError: If the header cannot be read or has no approximate position.
    """

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
