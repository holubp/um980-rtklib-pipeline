"""Lightweight base-observation archive availability probing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .euref import planned_urls

ProbeStatus = Literal["available", "partial", "missing", "unknown", "error"]


@dataclass(frozen=True)
class RinexHeaderSummary:
    """Small subset of RINEX OBS header metadata."""

    marker_name: str | None = None
    approximate_position_xyz: tuple[float, float, float] | None = None
    interval: float | None = None
    time_of_first_obs: str | None = None
    time_of_last_obs: str | None = None
    observation_types: dict[str, list[str]] | None = None
    receiver: str | None = None
    antenna: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly header details."""

        return {
            "marker_name": self.marker_name,
            "approximate_position_xyz": self.approximate_position_xyz,
            "interval": self.interval,
            "time_of_first_obs": self.time_of_first_obs,
            "time_of_last_obs": self.time_of_last_obs,
            "observation_types": self.observation_types or {},
            "receiver": self.receiver,
            "antenna": self.antenna,
        }


@dataclass(frozen=True)
class ArchiveProbeResult:
    """Availability probe for one station/resolution/window."""

    station_id: str
    start_time: str
    end_time: str
    resolution: str
    expected_files: int
    available_files: int
    missing_files: int
    checked_urls: list[str]
    probe_method: str
    status: ProbeStatus
    error_message: str | None = None
    rinex_header_summary: RinexHeaderSummary | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly probe details."""

        return {
            "station_id": self.station_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "resolution": self.resolution,
            "expected_files": self.expected_files,
            "available_files": self.available_files,
            "missing_files": self.missing_files,
            "checked_urls": self.checked_urls,
            "probe_method": self.probe_method,
            "status": self.status,
            "error_message": self.error_message,
            "rinex_header_summary": self.rinex_header_summary.as_dict() if self.rinex_header_summary else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ArchiveProbeResult":
        """Build a probe result from cached JSON."""

        header_data = data.get("rinex_header_summary")
        return cls(
            station_id=str(data["station_id"]),
            start_time=str(data["start_time"]),
            end_time=str(data["end_time"]),
            resolution=str(data["resolution"]),
            expected_files=int(data["expected_files"]),
            available_files=int(data["available_files"]),
            missing_files=int(data["missing_files"]),
            checked_urls=[str(item) for item in data.get("checked_urls", [])] if isinstance(data.get("checked_urls"), list) else [],
            probe_method=str(data.get("probe_method") or "unknown"),
            status=str(data.get("status") or "unknown"),  # type: ignore[arg-type]
            error_message=str(data["error_message"]) if data.get("error_message") is not None else None,
            rinex_header_summary=RinexHeaderSummary(**header_data) if isinstance(header_data, dict) else None,
        )


def default_probe_cache_dir() -> Path:
    """Return the default probe cache directory."""

    return Path.home() / ".cache" / "um980-rtklib-pipeline" / "archive-probes"


def probe_station_archives(
    *,
    station: str,
    start: datetime,
    end: datetime,
    resolution: str,
    rinex_version: str = "3",
    cache_dir: Path | None = None,
    refresh: bool = False,
    download_headers_only: bool = False,
) -> ArchiveProbeResult:
    """Probe planned EUREF/BKG observation URLs without full downloads."""

    urls = _planned_urls_for_resolution(station=station, start=start, end=end, resolution=resolution, rinex_version=rinex_version)
    cache = cache_dir or default_probe_cache_dir()
    cache_key = _cache_key(station, start, end, resolution, rinex_version)
    cache_path = cache / f"{cache_key}.json"
    if not refresh and cache_path.exists():
        try:
            return ArchiveProbeResult.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    if not urls:
        result = ArchiveProbeResult(
            station_id=station,
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            resolution=resolution,
            expected_files=0,
            available_files=0,
            missing_files=0,
            checked_urls=[],
            probe_method="not_checked",
            status="unknown",
            error_message="no archive URLs were planned",
        )
        _write_probe_cache(cache_path, result)
        return result
    available = 0
    errors: list[str] = []
    for url in urls:
        ok, error = _url_exists(url)
        if ok:
            available += 1
        elif error:
            errors.append(f"{url}: {error}")
    missing = len(urls) - available
    if available == len(urls):
        status: ProbeStatus = "available"
    elif available > 0:
        status = "partial"
    else:
        status = "missing"
    header = None
    if download_headers_only and available:
        # Header range support for gzip/Hatanaka archives is deliberately not
        # implemented yet; reporting availability is cheap and deterministic.
        header = None
    result = ArchiveProbeResult(
        station_id=station,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        resolution=resolution,
        expected_files=len(urls),
        available_files=available,
        missing_files=missing,
        checked_urls=urls,
        probe_method="head",
        status=status,
        error_message="; ".join(errors[:3]) if errors and status == "missing" else None,
        rinex_header_summary=header,
    )
    _write_probe_cache(cache_path, result)
    return result


def planned_probe_result(*, station: str, start: datetime, end: datetime, resolution: str, rinex_version: str = "3") -> ArchiveProbeResult:
    """Return a non-network planned archive result."""

    urls = _planned_urls_for_resolution(station=station, start=start, end=end, resolution=resolution, rinex_version=rinex_version)
    return ArchiveProbeResult(
        station_id=station,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        resolution=resolution,
        expected_files=len(urls),
        available_files=0,
        missing_files=0,
        checked_urls=urls,
        probe_method="not_checked",
        status="unknown",
    )


def _planned_urls_for_resolution(*, station: str, start: datetime, end: datetime, resolution: str, rinex_version: str) -> list[str]:
    if resolution == "high":
        return [
            *planned_urls(station=station, start=start, end=end, provider_name="bkg-euref-highrate", base_rate="1s", rinex_version=rinex_version),
            *planned_urls(station=station, start=start, end=end, provider_name="bkg-igs-highrate", base_rate="1s", rinex_version=rinex_version),
        ]
    return planned_urls(station=station, start=start, end=end, provider_name="bev-nrt", base_rate="30s", rinex_version=rinex_version)


def _url_exists(url: str) -> tuple[bool, str | None]:
    if not url.startswith("http"):
        return False, "HEAD probe supports HTTP(S) URLs only"
    request = Request(url, method="HEAD")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - explicit user-triggered archive probe
            status = getattr(response, "status", 200)
            return 200 <= status < 400, None
    except HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # pragma: no cover - platform/network dependent
        return False, str(exc)


def _cache_key(station: str, start: datetime, end: datetime, resolution: str, rinex_version: str) -> str:
    safe = f"{station}_{start:%Y%m%dT%H%M%S}_{end:%Y%m%dT%H%M%S}_{resolution}_rnx{rinex_version}"
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in safe)


def _write_probe_cache(path: Path, result: ArchiveProbeResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
