"""Summarise RTKLIB solution quality output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Literal

from .nmea import dm_to_decimal, float_or_none, int_or_none, parse_sentence, sentence_type


QualitySystem = Literal["rtklib_q", "nmea_gga"]

RTKLIB_Q_LABELS = {
    1: "fixed",
    2: "float",
    4: "dgps",
    5: "single",
}

NMEA_GGA_LABELS = {
    0: "invalid",
    1: "gps",
    2: "dgps",
    4: "rtk fixed",
    5: "rtk float",
}

RTKLIB_Q_TO_NMEA_GGA = {
    0: 0,
    1: 4,
    2: 5,
    4: 2,
    5: 1,
}
NMEA_GGA_TO_RTKLIB_Q = {value: key for key, value in RTKLIB_Q_TO_NMEA_GGA.items()}


@dataclass(frozen=True)
class RtklibSolutionSample:
    """One parsed RTKLIB solution sample.

    Attributes:
        time_s: Monotonic sample time in seconds when available.
        lat_deg: Latitude in decimal degrees.
        lon_deg: Longitude in decimal degrees.
        quality: RTKLIB `Q` or NMEA GGA fix-quality code.
        quality_system: Source and semantics of `quality`.
    """

    time_s: float | None
    lat_deg: float
    lon_deg: float
    quality: int
    quality_system: QualitySystem


@dataclass(frozen=True)
class RtklibQualityBucket:
    """Aggregated RTKLIB solution quality metrics for one `Q` value."""

    quality: int
    count: int
    percent: float
    duration_s: float
    distance_m: float


@dataclass(frozen=True)
class RtklibSolutionSummary:
    """Aggregated RTKLIB solution output metrics.

    Attributes:
        sample_count: Number of parsed quality-coded position samples.
        duration_s: Total elapsed time across parsed consecutive samples.
        distance_m: Total track length across parsed consecutive samples.
        quality_system: Whether qualities are RTKLIB `Q` values or NMEA GGA
            fix-quality values.
        buckets: Per-quality aggregate metrics.
    """

    sample_count: int
    duration_s: float
    distance_m: float
    quality_system: QualitySystem
    buckets: tuple[RtklibQualityBucket, ...]


def summarize_rtklib_solution(path: Path) -> RtklibSolutionSummary | None:
    """Parse an RTKLIB solution output file and aggregate quality metrics.

    Args:
        path: RTKLIB `.pos`/`.llh` or NMEA solution file.

    Returns:
        Aggregated quality statistics, or `None` when the file contains no
        parseable position samples with quality codes.
    """

    samples = _read_rtklib_solution_samples(path)
    if not samples:
        return None
    duration_by_q: dict[int, float] = {}
    distance_by_q: dict[int, float] = {}
    counts_by_q: dict[int, int] = {}
    total_duration = 0.0
    total_distance = 0.0
    previous: RtklibSolutionSample | None = None
    for sample in samples:
        counts_by_q[sample.quality] = counts_by_q.get(sample.quality, 0) + 1
        if previous is not None:
            distance = _haversine_m(previous.lat_deg, previous.lon_deg, sample.lat_deg, sample.lon_deg)
            distance_by_q[sample.quality] = distance_by_q.get(sample.quality, 0.0) + distance
            total_distance += distance
            if previous.time_s is not None and sample.time_s is not None and sample.time_s >= previous.time_s:
                duration = sample.time_s - previous.time_s
                duration_by_q[sample.quality] = duration_by_q.get(sample.quality, 0.0) + duration
                total_duration += duration
        previous = sample
    total = len(samples)
    buckets = tuple(
        RtklibQualityBucket(
            quality=quality,
            count=count,
            percent=(100.0 * count / total) if total else 0.0,
            duration_s=duration_by_q.get(quality, 0.0),
            distance_m=distance_by_q.get(quality, 0.0),
        )
        for quality, count in sorted(counts_by_q.items())
    )
    return RtklibSolutionSummary(
        sample_count=total,
        duration_s=total_duration,
        distance_m=total_distance,
        quality_system=samples[0].quality_system,
        buckets=buckets,
    )


def format_rtklib_solution_summary(summary: RtklibSolutionSummary) -> list[str]:
    """Return human-readable RTKLIB quality summary lines."""

    lines = [
        (
            "RTKLIB solution summary: "
            f"epochs={summary.sample_count} "
            f"duration={_format_duration(summary.duration_s)} "
            f"track={_format_distance(summary.distance_m)}"
        )
    ]
    for bucket in summary.buckets:
        label = _quality_label(summary.quality_system, bucket.quality)
        lines.append(
            (
                f"{_quality_prefix(summary.quality_system)}={bucket.quality} "
                f"({_quality_label_with_mapping(summary.quality_system, bucket.quality, label)}): "
                f"{bucket.count} epochs ({bucket.percent:.1f}%), "
                f"duration={_format_duration(bucket.duration_s)}, "
                f"track={_format_distance(bucket.distance_m)}"
            )
        )
    return lines


def _read_rtklib_solution_samples(path: Path) -> list[RtklibSolutionSample]:
    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except OSError:
        return []
    samples: list[RtklibSolutionSample] = []
    nmea_day_offset = 0.0
    previous_nmea_time: float | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("%"):
            continue
        if stripped.startswith("$"):
            sample, previous_nmea_time, nmea_day_offset = _parse_nmea_solution_line(
                stripped,
                previous_nmea_time,
                nmea_day_offset,
            )
        else:
            sample = _parse_pos_solution_line(stripped)
        if sample is not None:
            samples.append(sample)
    return samples


def _parse_pos_solution_line(line: str) -> RtklibSolutionSample | None:
    fields = line.split()
    if len(fields) < 6:
        return None
    time_s = _parse_pos_time_s(fields[0], fields[1])
    lat = float_or_none(fields[2])
    lon = float_or_none(fields[3])
    quality = int_or_none(fields[5])
    if lat is None or lon is None or quality is None:
        return None
    return RtklibSolutionSample(time_s=time_s, lat_deg=lat, lon_deg=lon, quality=quality, quality_system="rtklib_q")


def _parse_pos_time_s(date_text: str, time_text: str) -> float | None:
    try:
        dt = datetime.strptime(f"{date_text} {time_text}", "%Y/%m/%d %H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(f"{date_text} {time_text}", "%Y/%m/%d %H:%M:%S")
        except ValueError:
            return None
    return dt.replace(tzinfo=UTC).timestamp()


def _parse_nmea_solution_line(
    line: str,
    previous_time_s: float | None,
    day_offset_s: float,
) -> tuple[RtklibSolutionSample | None, float | None, float]:
    parsed = parse_sentence(line)
    if parsed is None or sentence_type(parsed.talker_type) != "GGA":
        return None, previous_time_s, day_offset_s
    fields = parsed.fields
    if len(fields) < 6:
        return None, previous_time_s, day_offset_s
    time_of_day = _nmea_time_of_day_s(fields[0])
    if time_of_day is not None and previous_time_s is not None and time_of_day + day_offset_s < previous_time_s:
        day_offset_s += 86400.0
    time_s = (time_of_day + day_offset_s) if time_of_day is not None else None
    lat = dm_to_decimal(fields[1], fields[2])
    lon = dm_to_decimal(fields[3], fields[4])
    quality = int_or_none(fields[5])
    if lat is None or lon is None or quality is None:
        return None, time_s if time_s is not None else previous_time_s, day_offset_s
    return (
        RtklibSolutionSample(time_s=time_s, lat_deg=lat, lon_deg=lon, quality=quality, quality_system="nmea_gga"),
        time_s,
        day_offset_s,
    )


def _quality_prefix(system: QualitySystem) -> str:
    return "RTKLIB Q" if system == "rtklib_q" else "NMEA GGA quality"


def _quality_label(system: QualitySystem, quality: int) -> str:
    labels = RTKLIB_Q_LABELS if system == "rtklib_q" else NMEA_GGA_LABELS
    return labels.get(quality, "other")


def _quality_label_with_mapping(system: QualitySystem, quality: int, label: str) -> str:
    if system == "rtklib_q":
        mapped = RTKLIB_Q_TO_NMEA_GGA.get(quality)
        if mapped is None:
            return label
        mapped_label = NMEA_GGA_LABELS.get(mapped, "other")
        return f"{label}; GGA quality={mapped} {mapped_label}"
    mapped = NMEA_GGA_TO_RTKLIB_Q.get(quality)
    if mapped is None:
        return label
    mapped_label = RTKLIB_Q_LABELS.get(mapped, "other")
    return f"{label}; RTKLIB Q={mapped} {mapped_label}"


def _nmea_time_of_day_s(value: str) -> float | None:
    if len(value) < 6:
        return None
    hour = int_or_none(value[0:2])
    minute = int_or_none(value[2:4])
    second = float_or_none(value[4:])
    if hour is None or minute is None or second is None:
        return None
    return hour * 3600.0 + minute * 60.0 + second


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371008.8
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2.0) ** 2
    return 2.0 * radius_m * asin(min(1.0, sqrt(a)))


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _format_distance(meters: float) -> str:
    if meters >= 1000.0:
        return f"{meters / 1000.0:.3f} km"
    return f"{meters:.1f} m"
