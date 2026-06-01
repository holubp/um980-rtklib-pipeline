"""EPN/EUREF station catalogue loading and caching."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen


DEFAULT_EPN_SSC_URLS = (
    "https://epncb.oma.be/pub/product/referenceframe/latest/EPN_ETRF2000.SSC",
    "https://epncb.oma.be/pub/product/referenceframe/latest/EPN_ETRF2000_short.SSC",
)


@dataclass(frozen=True)
class StationRecord:
    """One reference-station catalogue row."""

    station_id_long: str
    station_id_short: str
    network: str
    x: float | None = None
    y: float | None = None
    z: float | None = None
    lat: float | None = None
    lon: float | None = None
    height: float | None = None
    country: str | None = None
    frame: str | None = None
    source: str = "unknown"
    source_file: str | None = None
    source_download_time: str | None = None
    solution_number: str | None = None
    active: bool | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly station details."""

        return {
            "station_id_long": self.station_id_long,
            "station_id_short": self.station_id_short,
            "network": self.network,
            "country": self.country,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "lat": self.lat,
            "lon": self.lon,
            "height": self.height,
            "frame": self.frame,
            "source": self.source,
            "source_file": self.source_file,
            "source_download_time": self.source_download_time,
            "solution_number": self.solution_number,
            "active": self.active,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StationRecord":
        """Build a station record from cached JSON."""

        return cls(
            station_id_long=str(data["station_id_long"]),
            station_id_short=str(data.get("station_id_short") or str(data["station_id_long"])[:4]),
            network=str(data.get("network") or "EPN"),
            x=_float_or_none(data.get("x")),
            y=_float_or_none(data.get("y")),
            z=_float_or_none(data.get("z")),
            lat=_float_or_none(data.get("lat")),
            lon=_float_or_none(data.get("lon")),
            height=_float_or_none(data.get("height")),
            country=str(data["country"]) if data.get("country") is not None else None,
            frame=str(data["frame"]) if data.get("frame") is not None else None,
            source=str(data.get("source") or "unknown"),
            source_file=str(data["source_file"]) if data.get("source_file") is not None else None,
            source_download_time=str(data["source_download_time"]) if data.get("source_download_time") is not None else None,
            solution_number=str(data["solution_number"]) if data.get("solution_number") is not None else None,
            active=bool(data["active"]) if data.get("active") is not None else None,
            warnings=[str(item) for item in data.get("warnings", [])] if isinstance(data.get("warnings"), list) else [],
        )


@dataclass(frozen=True)
class StationCatalog:
    """Reference-station catalogue with lookup helpers."""

    records: list[StationRecord]
    loaded_from: str
    generated_at: str
    frame: str | None = None
    warnings: list[str] = field(default_factory=list)

    def find_by_short_id(self, station_id: str) -> list[StationRecord]:
        """Find stations by four-character ID."""

        short = station_id.upper()[:4]
        return [record for record in self.records if record.station_id_short == short]

    def find_by_long_id(self, station_id: str) -> StationRecord | None:
        """Find one station by long RINEX marker ID."""

        marker = station_id.upper()
        for record in self.records:
            if record.station_id_long == marker:
                return record
        return None

    def nearest(
        self,
        *,
        lat: float,
        lon: float,
        radius_km: float,
        max_candidates: int,
    ) -> list[tuple[StationRecord, float]]:
        """Return nearest stations with known coordinates."""

        rows: list[tuple[StationRecord, float]] = []
        for record in self.records:
            if record.lat is None or record.lon is None:
                continue
            distance = _haversine_km(lat, lon, record.lat, record.lon)
            if distance <= radius_km:
                rows.append((record, distance))
        rows.sort(key=lambda item: (item[1], item[0].station_id_long))
        return rows[:max_candidates]

    def merge_with_curated(self, curated: list[StationRecord]) -> "StationCatalog":
        """Return a catalogue where official rows override curated rows."""

        by_marker = {record.station_id_long: record for record in curated}
        by_marker.update({record.station_id_long: record for record in self.records})
        return StationCatalog(
            records=sorted(by_marker.values(), key=lambda item: item.station_id_long),
            loaded_from=self.loaded_from,
            generated_at=self.generated_at,
            frame=self.frame,
            warnings=self.warnings,
        )

    def to_json(self) -> dict[str, object]:
        """Return JSON-friendly catalogue details."""

        return {
            "loaded_from": self.loaded_from,
            "generated_at": self.generated_at,
            "frame": self.frame,
            "warnings": self.warnings,
            "records": [record.as_dict() for record in self.records],
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "StationCatalog":
        """Build a catalogue from cached JSON."""

        return cls(
            records=[StationRecord.from_dict(item) for item in data.get("records", []) if isinstance(item, dict)],
            loaded_from=str(data.get("loaded_from") or "cache"),
            generated_at=str(data.get("generated_at") or datetime.now(UTC).isoformat()),
            frame=str(data["frame"]) if data.get("frame") is not None else None,
            warnings=[str(item) for item in data.get("warnings", [])] if isinstance(data.get("warnings"), list) else [],
        )


def default_station_catalog_cache() -> Path:
    """Return the default station catalogue cache path."""

    return Path.home() / ".cache" / "um980-rtklib-pipeline" / "epn-stations.json"


def curated_station_catalog(curated_positions: dict[str, tuple[float, float, float]]) -> StationCatalog:
    """Build a catalogue from built-in curated ECEF station positions."""

    records: list[StationRecord] = []
    for marker, xyz in sorted(curated_positions.items()):
        lat, lon, height = ecef_to_geodetic(*xyz)
        records.append(
            StationRecord(
                station_id_long=marker,
                station_id_short=marker[:4],
                network="EPN",
                x=xyz[0],
                y=xyz[1],
                z=xyz[2],
                lat=lat,
                lon=lon,
                height=height,
                frame="ETRF2000",
                source="curated",
            )
        )
    return StationCatalog(
        records=records,
        loaded_from="curated",
        generated_at=datetime.now(UTC).isoformat(),
        frame="ETRF2000",
    )


def load_station_catalog(
    *,
    cache_path: Path | None = None,
    source: str = "auto",
    refresh: bool = False,
    max_age_days: int = 30,
    urls: tuple[str, ...] = DEFAULT_EPN_SSC_URLS,
    curated_positions: dict[str, tuple[float, float, float]] | None = None,
) -> StationCatalog:
    """Load the EPN station catalogue with cache-first behaviour."""

    cache = cache_path or default_station_catalog_cache()
    curated = curated_station_catalog(curated_positions or {})
    if source == "curated":
        return curated
    if source in {"auto", "cache"} and not refresh:
        cached = _read_cached_catalog(cache, max_age_days=max_age_days)
        if cached is not None:
            return cached.merge_with_curated(curated.records)
        if source == "cache":
            return StationCatalog(
                curated.records,
                loaded_from="curated",
                generated_at=curated.generated_at,
                frame=curated.frame,
                warnings=[f"station catalogue cache not available: {cache}; using curated stations only"],
            )
    if source in {"auto", "epn-latest"} or refresh:
        try:
            downloaded = download_epn_ssc_catalog(urls=urls, cache_path=cache)
            return downloaded.merge_with_curated(curated.records)
        except Exception as exc:  # pragma: no cover - network dependent
            cached = _read_cached_catalog(cache, max_age_days=3650)
            if cached is not None:
                return StationCatalog(
                    cached.records,
                    loaded_from=cached.loaded_from,
                    generated_at=cached.generated_at,
                    frame=cached.frame,
                    warnings=[*cached.warnings, f"failed to refresh EPN station catalogue: {exc}; using cached catalogue"],
                ).merge_with_curated(curated.records)
            return StationCatalog(
                curated.records,
                loaded_from="curated",
                generated_at=curated.generated_at,
                frame=curated.frame,
                warnings=[f"failed to refresh EPN station catalogue: {exc}; using curated stations only"],
            )
    raise ValueError(f"unsupported station catalogue source: {source}")


def download_epn_ssc_catalog(*, urls: tuple[str, ...], cache_path: Path) -> StationCatalog:
    """Download and cache official EPN SSC station coordinates."""

    records: list[StationRecord] = []
    warnings: list[str] = []
    downloaded_at = datetime.now(UTC).isoformat()
    for url in urls:
        try:
            with urlopen(url, timeout=20) as response:  # noqa: S310 - explicit user-triggered refresh
                text = response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - network dependent
            warnings.append(f"failed to download {url}: {exc}")
            continue
        records.extend(parse_ssc(text, source_file=url, downloaded_at=downloaded_at))
    if not records:
        raise RuntimeError("; ".join(warnings) or "no EPN SSC station records were parsed")
    by_marker = {record.station_id_long: record for record in records}
    catalog = StationCatalog(
        records=sorted(by_marker.values(), key=lambda item: item.station_id_long),
        loaded_from="epn-latest",
        generated_at=downloaded_at,
        frame="ETRF2000",
        warnings=warnings,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(catalog.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return catalog


def parse_ssc(text: str, *, source_file: str, downloaded_at: str) -> list[StationRecord]:
    """Parse SINEX/SSC ``SOLUTION/ESTIMATE`` station coordinates."""

    estimates: dict[tuple[str, str], dict[str, float]] = {}
    solutions: dict[tuple[str, str], str] = {}
    in_estimate = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("+SOLUTION/ESTIMATE"):
            in_estimate = True
            continue
        if line.startswith("-SOLUTION/ESTIMATE"):
            in_estimate = False
            continue
        if not in_estimate or not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 9 or parts[1] not in {"STAX", "STAY", "STAZ"}:
            continue
        param = parts[1]
        code = parts[2].upper()
        if len(code) < 4:
            continue
        solution = parts[4] if len(parts) > 4 else "1"
        value = _float_or_none(parts[-2])
        if value is None:
            continue
        key = (_normalise_marker(code), solution)
        estimates.setdefault(key, {})[param] = value
        solutions[key] = solution
    records: list[StationRecord] = []
    for (marker, solution), values in estimates.items():
        if not {"STAX", "STAY", "STAZ"}.issubset(values):
            continue
        x, y, z = values["STAX"], values["STAY"], values["STAZ"]
        lat, lon, height = ecef_to_geodetic(x, y, z)
        records.append(
            StationRecord(
                station_id_long=marker,
                station_id_short=marker[:4],
                network="EPN",
                country=marker[-3:] if len(marker) == 9 else None,
                x=x,
                y=y,
                z=z,
                lat=lat,
                lon=lon,
                height=height,
                frame="ETRF2000",
                source="epn_ssc",
                source_file=source_file,
                source_download_time=downloaded_at,
                solution_number=solutions.get((marker, solution)),
                active=None,
            )
        )
    return records


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Convert ECEF XYZ to geodetic latitude, longitude and ellipsoidal height."""

    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    semi_minor = semi_major * (1.0 - flattening)
    eccentricity_sq = 1.0 - (semi_minor * semi_minor) / (semi_major * semi_major)
    ep_sq = (semi_major * semi_major - semi_minor * semi_minor) / (semi_minor * semi_minor)
    p = math.hypot(x, y)
    theta = math.atan2(z * semi_major, p * semi_minor)
    lon = math.atan2(y, x)
    lat = math.atan2(
        z + ep_sq * semi_minor * math.sin(theta) ** 3,
        p - eccentricity_sq * semi_major * math.cos(theta) ** 3,
    )
    normal = semi_major / math.sqrt(1.0 - eccentricity_sq * math.sin(lat) ** 2)
    height = p / math.cos(lat) - normal
    return math.degrees(lat), math.degrees(lon), height


def write_station_catalog_cache(path: Path, catalog: StationCatalog) -> None:
    """Write station catalogue JSON cache."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_cached_catalog(path: Path, *, max_age_days: int) -> StationCatalog | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        catalog = StationCatalog.from_json(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    try:
        generated = datetime.fromisoformat(catalog.generated_at)
    except ValueError:
        return catalog
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    if datetime.now(UTC) - generated > timedelta(days=max_age_days):
        return None
    return catalog


def _normalise_marker(code: str) -> str:
    code = code.upper()
    if len(code) == 4:
        return code + "00XXX"
    if len(code) == 10 and code.endswith("0"):
        return code[:-1]
    return code


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * radius_km * math.asin(min(1.0, math.sqrt(a)))


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
