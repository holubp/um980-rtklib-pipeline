"""Advisory EUREF/EPN base-station ranking."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .archive_probe import ArchiveProbeResult, planned_probe_result, probe_station_archives
from .euref import STATION_ALIASES, planned_urls, resolve_station
from .solution import SolutionPoint
from .stations import StationCatalog, StationRecord, curated_station_catalog

AdvisoryFormat = Literal["table", "markdown", "json"]

CURATED_STATION_POSITIONS: dict[str, tuple[float, float, float]] = {
    # Existing repository fixtures and user logs use current EPN/EUREF ETRF2000
    # coordinates for these stations. Other aliases are reported without a
    # distance until a coordinate source/cache is available.
    "CPAR00CZE": (3949919.0811, 1116467.0408, 4865832.5323),
    "TUBO00CZE": (4001470.5995, 1192345.3042, 4805795.3148),
}


@dataclass(frozen=True)
class BaseCandidateAdvice:
    """One ranked base-station advisory row."""

    station: str
    marker: str
    distance_km: float | None
    score: float
    recommendation: str
    requested_resolution: str
    high_rate_planned_files: int
    low_rate_planned_files: int
    fallback_needed: bool | None
    warnings: list[str]
    low_rate_status: str = "unknown"
    high_rate_status: str = "unknown"
    low_rate_available_files: int = 0
    high_rate_available_files: int = 0
    coordinate_source: str | None = None
    coordinate_frame: str | None = None
    nav_availability: dict[str, object] | None = None
    archive_probes: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly candidate details."""

        return {
            "station": self.station,
            "marker": self.marker,
            "distance_km": self.distance_km,
            "score": self.score,
            "recommendation": self.recommendation,
            "requested_resolution": self.requested_resolution,
            "high_rate_planned_files": self.high_rate_planned_files,
            "low_rate_planned_files": self.low_rate_planned_files,
            "low_rate_status": self.low_rate_status,
            "high_rate_status": self.high_rate_status,
            "low_rate_available_files": self.low_rate_available_files,
            "high_rate_available_files": self.high_rate_available_files,
            "fallback_needed": self.fallback_needed,
            "coordinate_source": self.coordinate_source,
            "coordinate_frame": self.coordinate_frame,
            "nav_availability": self.nav_availability or {},
            "archive_probes": self.archive_probes or {},
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class BaseAdvisoryReport:
    """Base-candidate advisory result."""

    representative_position: dict[str, float] | None
    recording_span: dict[str, str | None]
    selected_span: dict[str, str | None]
    candidates: list[BaseCandidateAdvice]
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly report details."""

        return {
            "representative_position": self.representative_position,
            "recording_span": self.recording_span,
            "selected_span": self.selected_span,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "warnings": self.warnings,
        }


def build_base_advisory_report(
    *,
    solution_points: list[SolutionPoint],
    start: datetime | None,
    end: datetime | None,
    radius_km: float,
    max_candidates: int,
    base_resolution: str,
    allow_resolution_fallback: bool,
    stations: list[str] | None = None,
    station_catalog: StationCatalog | None = None,
    probe_archives: bool = False,
    download_headers_only: bool = False,
    refresh_probes: bool = False,
    probe_cache_dir: Path | None = None,
    nav_source: str = "auto-prefer-base",
    require_nav: bool = False,
    require_constellations: list[str] | None = None,
) -> BaseAdvisoryReport:
    """Rank known base stations for a rover recording without large downloads."""

    warnings: list[str] = []
    if not solution_points:
        return BaseAdvisoryReport(None, {"start": None, "end": None}, {"start": None, "end": None}, [], ["no rover solution points available for base advisory"])
    recording_span = {
        "start": min(point.time_utc for point in solution_points).isoformat(),
        "end": max(point.time_utc for point in solution_points).isoformat(),
    }
    selected_span = {
        "start": start.isoformat() if start else recording_span["start"],
        "end": end.isoformat() if end else recording_span["end"],
    }
    representative = _representative_position(solution_points)
    rover_ecef = _ecef_from_llh(representative["lat"], representative["lon"], representative["height_m"])
    catalog = station_catalog or curated_station_catalog(CURATED_STATION_POSITIONS)
    warnings.extend(catalog.warnings)
    candidate_records = _candidate_records(catalog, stations, representative, radius_km, max_candidates, warnings)
    candidates: list[BaseCandidateAdvice] = []
    for station, record in candidate_records:
        marker = record.station_id_long
        ecef = (record.x, record.y, record.z) if None not in (record.x, record.y, record.z) else CURATED_STATION_POSITIONS.get(marker)
        row_warnings: list[str] = []
        distance_km: float | None = None
        if ecef is None:
            row_warnings.append("no curated station coordinates available; distance and score are limited")
        else:
            distance_km = _ecef_distance_km(rover_ecef, ecef)  # type: ignore[arg-type]
            if distance_km > radius_km:
                continue
        low_probe = _probe_or_plan(
            marker=marker,
            selected_span=selected_span,
            resolution="low",
            probe_archives=probe_archives,
            download_headers_only=download_headers_only,
            refresh_probes=refresh_probes,
            probe_cache_dir=probe_cache_dir,
            row_warnings=row_warnings,
        )
        high_probe = _probe_or_plan(
            marker=marker,
            selected_span=selected_span,
            resolution="high",
            probe_archives=probe_archives,
            download_headers_only=download_headers_only,
            refresh_probes=refresh_probes,
            probe_cache_dir=probe_cache_dir,
            row_warnings=row_warnings,
        )
        high_urls = high_probe.expected_files
        low_urls = low_probe.expected_files
        fallback_needed = None
        if base_resolution == "high":
            high_available = high_probe.status in {"available", "partial"}
            low_available = low_probe.status in {"available", "partial", "unknown"} and low_urls > 0
            fallback_needed = (not high_available) and low_available
            if fallback_needed and not allow_resolution_fallback:
                row_warnings.append("high-rate base data unavailable or unverified; low-rate fallback is forbidden")
        nav_availability = _nav_availability(nav_source=nav_source, require_nav=require_nav, row_warnings=row_warnings)
        score = _score(
            distance_km,
            high_urls,
            low_urls,
            base_resolution,
            low_probe=low_probe,
            high_probe=high_probe,
            warnings=row_warnings,
            require_constellations=require_constellations,
        )
        candidates.append(
            BaseCandidateAdvice(
                station=station,
                marker=marker,
                distance_km=distance_km,
                score=score,
                recommendation=_recommendation(score, row_warnings),
                requested_resolution=base_resolution,
                high_rate_planned_files=high_urls,
                low_rate_planned_files=low_urls,
                low_rate_status=low_probe.status,
                high_rate_status=high_probe.status,
                low_rate_available_files=low_probe.available_files,
                high_rate_available_files=high_probe.available_files,
                fallback_needed=fallback_needed,
                coordinate_source=record.source,
                coordinate_frame=record.frame,
                nav_availability=nav_availability,
                archive_probes={"low": low_probe.as_dict(), "high": high_probe.as_dict()},
                warnings=row_warnings,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.distance_km if item.distance_km is not None else math.inf, item.marker))
    return BaseAdvisoryReport(representative, recording_span, selected_span, candidates[:max_candidates], warnings)


def format_base_advisory(report: BaseAdvisoryReport, output_format: AdvisoryFormat) -> str:
    """Render an advisory report."""

    if output_format == "json":
        return json.dumps(report.as_dict(), indent=2, sort_keys=True)
    header = [
        "station",
        "marker",
        "distance_km",
        "score",
        "recommendation",
        "high",
        "low",
        "fallback",
        "coord_source",
        "warnings",
    ]
    rows = [
        [
            item.station,
            item.marker,
            "" if item.distance_km is None else f"{item.distance_km:.1f}",
            f"{item.score:.1f}",
            item.recommendation,
            f"{item.high_rate_status} {item.high_rate_available_files}/{item.high_rate_planned_files}",
            f"{item.low_rate_status} {item.low_rate_available_files}/{item.low_rate_planned_files}",
            "" if item.fallback_needed is None else ("needed" if item.fallback_needed else "not-needed"),
            item.coordinate_source or "",
            "; ".join(item.warnings),
        ]
        for item in report.candidates
    ]
    if output_format == "markdown":
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        lines.extend("| " + " | ".join(row) + " |" for row in rows)
        return "\n".join(lines)
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(len(header))]
    lines = ["  ".join(header[index].ljust(widths[index]) for index in range(len(header)))]
    lines.extend("  ".join(row[index].ljust(widths[index]) for index in range(len(header))) for row in rows)
    return "\n".join(lines)


def _representative_position(points: list[SolutionPoint]) -> dict[str, float]:
    lat = sum(point.lat for point in points) / len(points)
    lon = sum(point.lon for point in points) / len(points)
    heights = [point.h_ell if point.h_ell is not None else point.h_msl for point in points]
    valid_heights = [height for height in heights if height is not None]
    return {"lat": lat, "lon": lon, "height_m": sum(valid_heights) / len(valid_heights) if valid_heights else 0.0}


def _ecef_distance_km(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b, strict=True))) / 1000.0


def _ecef_from_llh(lat_deg: float, lon_deg: float, height_m: float) -> tuple[float, float, float]:
    semi_major = 6378137.0
    flattening = 1.0 / 298.257223563
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    prime_vertical = semi_major / math.sqrt(1 - (2 * flattening - flattening * flattening) * sin_lat * sin_lat)
    x = (prime_vertical + height_m) * cos_lat * math.cos(lon)
    y = (prime_vertical + height_m) * cos_lat * math.sin(lon)
    z = (prime_vertical * (1 - flattening) ** 2 + height_m) * sin_lat
    return x, y, z


def _planned_file_count(marker: str, span: dict[str, str | None], *, resolution: str) -> int:
    if not span["start"] or not span["end"]:
        return 0
    start = datetime.fromisoformat(span["start"])
    end = datetime.fromisoformat(span["end"])
    if resolution == "high":
        return len(planned_urls(station=marker, start=start - timedelta(minutes=5), end=end + timedelta(minutes=5), provider_name="bkg-euref-highrate", base_rate="1s", rinex_version="3"))
    return len(planned_urls(station=marker, start=start, end=end, provider_name="bev-nrt", base_rate="30s", rinex_version="3"))


def _score(
    distance_km: float | None,
    high_urls: int,
    low_urls: int,
    requested_resolution: str,
    *,
    low_probe: ArchiveProbeResult,
    high_probe: ArchiveProbeResult,
    warnings: list[str],
    require_constellations: list[str] | None,
) -> float:
    score = 50.0
    if distance_km is not None:
        score += max(0.0, 50.0 - distance_km / 2.0)
    if requested_resolution == "high":
        if high_probe.status == "available":
            score += 25.0
        elif high_probe.status == "partial":
            score += 10.0
        elif high_urls:
            score -= 5.0
        else:
            score -= 20.0
    elif low_probe.status == "available":
        score += 15.0
    elif low_urls:
        score += 5.0
    if require_constellations:
        score -= 5.0
        warnings.append("constellation overlap scoring requires probed RINEX headers and is not fully available")
    score -= min(20.0, len(warnings) * 5.0)
    return score


def _recommendation(score: float, warnings: list[str]) -> str:
    if warnings:
        return "check"
    if score >= 80:
        return "preferred"
    if score >= 60:
        return "usable"
    return "weak"


def _candidate_records(
    catalog: StationCatalog,
    stations: list[str] | None,
    representative: dict[str, float],
    radius_km: float,
    max_candidates: int,
    warnings: list[str],
) -> list[tuple[str, StationRecord]]:
    if not stations:
        return [(record.station_id_short, record) for record, _distance in catalog.nearest(lat=representative["lat"], lon=representative["lon"], radius_km=radius_km, max_candidates=max_candidates * 3)]
    rows: list[tuple[str, StationRecord]] = []
    for station in stations:
        text = station.upper()
        record = catalog.find_by_long_id(text)
        if record is not None:
            rows.append((station, record))
            continue
        matches = catalog.find_by_short_id(text)
        if len(matches) == 1:
            rows.append((station, matches[0]))
            continue
        if len(matches) > 1:
            warnings.append(f"station short ID {station} is ambiguous: {','.join(record.station_id_long for record in matches)}")
            continue
        try:
            marker = resolve_station(station)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        fallback_catalog = curated_station_catalog(CURATED_STATION_POSITIONS)
        record = fallback_catalog.find_by_long_id(marker)
        if record is None:
            warnings.append(f"station {station} could not be resolved in station catalogue")
            continue
        rows.append((station, record))
    return rows


def _probe_or_plan(
    *,
    marker: str,
    selected_span: dict[str, str | None],
    resolution: str,
    probe_archives: bool,
    download_headers_only: bool,
    refresh_probes: bool,
    probe_cache_dir: Path | None,
    row_warnings: list[str],
) -> ArchiveProbeResult:
    if not selected_span["start"] or not selected_span["end"]:
        return ArchiveProbeResult(
            station_id=marker,
            start_time="",
            end_time="",
            resolution=resolution,
            expected_files=0,
            available_files=0,
            missing_files=0,
            checked_urls=[],
            probe_method="not_checked",
            status="unknown",
            error_message="no selected span",
        )
    start = datetime.fromisoformat(selected_span["start"])
    end = datetime.fromisoformat(selected_span["end"])
    if not probe_archives:
        return planned_probe_result(station=marker, start=start, end=end, resolution=resolution)
    try:
        return probe_station_archives(
            station=marker,
            start=start,
            end=end,
            resolution=resolution,
            cache_dir=probe_cache_dir,
            refresh=refresh_probes,
            download_headers_only=download_headers_only,
        )
    except Exception as exc:  # pragma: no cover - defensive
        row_warnings.append(f"{resolution}-rate archive probe failed: {exc}")
        return planned_probe_result(station=marker, start=start, end=end, resolution=resolution)


def _nav_availability(*, nav_source: str, require_nav: bool, row_warnings: list[str]) -> dict[str, object]:
    if nav_source in {"rover", "auto-prefer-base", "merge", "auto"}:
        status = {"rover": "depends_on_rover_extraction", "base": "not_probed", "global": "not_probed"}
    elif nav_source == "base":
        status = {"base": "not_probed"}
        if require_nav:
            row_warnings.append("base NAV availability is not probed for candidate stations")
    else:
        status = {nav_source: "not_probed"}
    return {"source_policy": nav_source, "by_source": status}
