"""Receiver solution extraction and output writers."""

from __future__ import annotations

import csv
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Literal

from .nmea import (
    FIX_QUALITY,
    NmeaRecord,
    datetime_from_time_date,
    datetime_from_time_with_context,
    dm_to_decimal,
    float_or_none,
    int_or_none,
    make_sentence,
    parse_sentence,
    sentence_type,
)
from .stream import StreamRecord

PositionNmeaMode = Literal["all", "best"]


@dataclass
class SolutionPoint:
    """One decoded rover solution point.

    Attributes:
        time_utc: UTC timestamp.
        source: Source NMEA/diagnostic message family.
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        h_ell: Ellipsoidal height in meters when known.
        h_msl: Mean-sea-level height in meters when known.
        fix_quality: Numeric NMEA fix quality.
        fix_quality_text: Human-readable fix quality.
        pos_type: Receiver position type text.
        sol_status: Receiver solution status text.
        num_sats: Number of satellites used/reported.
        hdop: Horizontal dilution of precision.
        vdop: Vertical dilution of precision.
        pdop: Position dilution of precision.
        sigma_e: East standard deviation in meters.
        sigma_n: North standard deviation in meters.
        sigma_u: Up standard deviation in meters.
        speed_mps: Ground speed in meters per second.
        course_deg: Course over ground in degrees.
        age_diff: Differential correction age in seconds.
    """

    time_utc: datetime
    source: Literal["GGA", "GNS", "RMC", "PPPNAVA", "ADRNAVA"]
    lat: float
    lon: float
    h_ell: float | None = None
    h_msl: float | None = None
    fix_quality: int | None = None
    fix_quality_text: str | None = None
    pos_type: str | None = None
    sol_status: str | None = None
    num_sats: int | None = None
    hdop: float | None = None
    vdop: float | None = None
    pdop: float | None = None
    sigma_e: float | None = None
    sigma_n: float | None = None
    sigma_u: float | None = None
    speed_mps: float | None = None
    course_deg: float | None = None
    age_diff: float | None = None


@dataclass
class SolutionExtraction:
    """Outputs produced by solution extraction.

    Attributes:
        all_nmea: Checksum-valid NMEA records.
        solution_records: NMEA records that produced solution points.
        solution_points: Decoded solution points.
        all_rows: CSV-friendly rows for all NMEA records.
        nmea_cadence: Per-sentence cadence metrics.
        warnings: User-facing extraction warnings.
    """

    all_nmea: list[str]
    solution_records: list[str]
    solution_points: list[SolutionPoint]
    all_rows: list[dict[str, object]]
    nmea_cadence: dict[str, dict[str, float | int]]
    warnings: list[str]


def _gga_solution(record: NmeaRecord, context_date: datetime | None) -> SolutionPoint | None:
    fields = record.fields
    if len(fields) < 9:
        return None
    dt = datetime_from_time_with_context(fields[0], context_date)
    lat = dm_to_decimal(fields[1], fields[2])
    lon = dm_to_decimal(fields[3], fields[4])
    if dt is None or lat is None or lon is None:
        return None
    fix = int_or_none(fields[5])
    h_msl = float_or_none(fields[8])
    geoid_sep = float_or_none(fields[10]) if len(fields) > 10 else None
    return SolutionPoint(
        time_utc=dt,
        source="GGA",
        lat=lat,
        lon=lon,
        h_msl=h_msl,
        h_ell=(h_msl + geoid_sep) if h_msl is not None and geoid_sep is not None else None,
        fix_quality=fix,
        fix_quality_text=FIX_QUALITY.get(fix or -1),
        num_sats=int_or_none(fields[6]),
        hdop=float_or_none(fields[7]),
        age_diff=float_or_none(fields[12]) if len(fields) > 12 else None,
    )


def _gns_solution(record: NmeaRecord, context_date: datetime | None) -> SolutionPoint | None:
    fields = record.fields
    if len(fields) < 8:
        return None
    dt = datetime_from_time_with_context(fields[0], context_date)
    lat = dm_to_decimal(fields[1], fields[2])
    lon = dm_to_decimal(fields[3], fields[4])
    if dt is None or lat is None or lon is None:
        return None
    h_msl = float_or_none(fields[8]) if len(fields) > 8 else None
    return SolutionPoint(
        time_utc=dt,
        source="GNS",
        lat=lat,
        lon=lon,
        h_msl=h_msl,
        fix_quality_text=fields[5] or None,
        num_sats=int_or_none(fields[6]),
        hdop=float_or_none(fields[7]),
        age_diff=float_or_none(fields[10]) if len(fields) > 10 else None,
    )


def _rmc_solution(record: NmeaRecord) -> SolutionPoint | None:
    fields = record.fields
    if len(fields) < 9:
        return None
    dt = datetime_from_time_date(fields[0], fields[8])
    lat = dm_to_decimal(fields[2], fields[3])
    lon = dm_to_decimal(fields[4], fields[5])
    if dt is None or lat is None or lon is None or fields[1] != "A":
        return None
    speed_knots = float_or_none(fields[6])
    return SolutionPoint(
        time_utc=dt,
        source="RMC",
        lat=lat,
        lon=lon,
        speed_mps=speed_knots * 0.514444 if speed_knots is not None else None,
        course_deg=float_or_none(fields[7]),
    )


def _ppp_adr_solution(record: NmeaRecord, source: Literal["PPPNAVA", "ADRNAVA"]) -> SolutionPoint | None:
    # UM980 PPPNAVA/ADRNAVA field variants are receiver-firmware dependent.
    # This parser handles common records containing lat/lon/height as numeric
    # decimal-degree fields and preserves status fields conservatively.
    fields = record.fields
    numeric = [float_or_none(field) for field in fields]
    lat_idx = lon_idx = None
    for idx, value in enumerate(numeric):
        if value is None or not -90 <= value <= 90:
            continue
        if idx + 1 < len(numeric) and numeric[idx + 1] is not None and -180 <= numeric[idx + 1] <= 180:
            lat_idx, lon_idx = idx, idx + 1
            break
    if lat_idx is None or lon_idx is None:
        return None
    return None


def _cadence(timestamps: list[datetime]) -> dict[str, float | int]:
    if not timestamps:
        return {"records": 0, "unique_timestamps": 0, "duplicates": 0}
    ordered = sorted(timestamps)
    unique = sorted(set(ordered))
    duplicates = len(ordered) - len(unique)
    intervals = [
        (right - left).total_seconds()
        for left, right in zip(unique, unique[1:])
        if (right - left).total_seconds() > 0
    ]
    if not intervals:
        return {
            "records": len(ordered),
            "unique_timestamps": len(unique),
            "duplicates": duplicates,
            "large_gaps": 0,
        }
    med = median(intervals)
    hz_values = [1.0 / interval for interval in intervals if interval > 0]
    return {
        "records": len(ordered),
        "unique_timestamps": len(unique),
        "mean_hz": mean(hz_values),
        "median_hz": median(hz_values),
        "min_hz": min(hz_values),
        "max_hz": max(hz_values),
        "interval_median_s": med,
        "interval_max_s": max(intervals),
        "duplicates": duplicates,
        "missing_est": sum(max(0, round(interval / med) - 1) for interval in intervals) if med else 0,
        "large_gaps": sum(1 for interval in intervals if med and interval > med * 3),
    }


def extract_solutions(records: list[StreamRecord], *, progress: bool = False) -> SolutionExtraction:
    """Extract solution tracks and clean NMEA from stream records.

    Args:
        records: Parsed mixed-stream records.
        progress: Emit coarse record-progress messages through logging.

    Returns:
        Solution extraction products and warnings.
    """

    all_nmea: list[str] = []
    solution_records: list[str] = []
    points: list[SolutionPoint] = []
    all_rows: list[dict[str, object]] = []
    warnings: list[str] = []
    context_date: datetime | None = None
    timestamps_by_type: dict[str, list[datetime]] = {}
    progress_step = 100_000

    for index, stream_record in enumerate(records, start=1):
        if progress and index % progress_step == 0:
            logging.info("scanned %d/%d records for solution data", index, len(records))
        if stream_record.kind != "nmea" or stream_record.text is None:
            continue
        parsed = parse_sentence(stream_record.text, stream_record.checksum_ok)
        if parsed is None:
            continue
        if stream_record.checksum_ok is not False:
            all_nmea.append(stream_record.text)
        typ = sentence_type(parsed.talker_type)
        point: SolutionPoint | None = None
        if typ == "RMC":
            point = _rmc_solution(parsed)
            if point is not None:
                context_date = point.time_utc
        elif typ == "GGA":
            point = _gga_solution(parsed, context_date)
        elif typ == "GNS":
            point = _gns_solution(parsed, context_date)
        elif parsed.talker_type == "PPPNAVA":
            point = _ppp_adr_solution(parsed, "PPPNAVA")
            if point is None and not any(warning.startswith("PPPNAVA records") for warning in warnings):
                warnings.append(
                    "PPPNAVA records are preserved in solution_all_records.csv but not converted to "
                    "solution points because receiver timestamp field mapping is not implemented."
                )
        elif parsed.talker_type == "ADRNAVA":
            point = _ppp_adr_solution(parsed, "ADRNAVA")
            if point is None and not any(warning.startswith("ADRNAVA records") for warning in warnings):
                warnings.append(
                    "ADRNAVA records are preserved in solution_all_records.csv but not converted to "
                    "solution points because receiver timestamp field mapping is not implemented."
                )

        row = {
            "offset": stream_record.offset,
            "type": parsed.talker_type,
            "checksum_ok": stream_record.checksum_ok,
            "text": stream_record.text,
        }
        if point is not None:
            points.append(point)
            solution_records.append(stream_record.text)
            timestamps_by_type.setdefault(parsed.talker_type, []).append(point.time_utc)
            row.update(
                {
                    "time_utc": point.time_utc.isoformat(),
                    "lat": point.lat,
                    "lon": point.lon,
                    "height": point.h_ell if point.h_ell is not None else point.h_msl,
                }
            )
        all_rows.append(row)

    cadence = {name: _cadence(values) for name, values in timestamps_by_type.items()}
    return SolutionExtraction(all_nmea, solution_records, points, all_rows, cadence, warnings)


def write_solution_csv(path: Path, points: list[SolutionPoint]) -> None:
    """Write decoded solution points as CSV.

    Args:
        path: Destination CSV path.
        points: Solution points to write.
    """

    fields = list(asdict(points[0]).keys()) if points else list(SolutionPoint.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for point in points:
            row = asdict(point)
            row["time_utc"] = point.time_utc.isoformat()
            writer.writerow(row)


def write_all_records_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write all parsed NMEA records as CSV.

    Args:
        path: Destination CSV path.
        rows: Record rows produced by `extract_solutions`.
    """

    fields = ["offset", "type", "checksum_ok", "time_utc", "lat", "lon", "height", "text"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_lines(path: Path, lines: list[str]) -> None:
    """Write text lines with CRLF endings.

    Args:
        path: Destination text path.
        lines: Lines to write.
    """

    path.write_text("".join(line.rstrip("\r\n") + "\r\n" for line in lines), encoding="ascii", errors="ignore")


def position_nmea_records(lines: list[str], mode: PositionNmeaMode = "best") -> list[str]:
    """Return original NMEA position sentences.

    Args:
        lines: Checksum-valid or checksum-absent NMEA sentence lines.
        mode: `all` keeps every usable position sentence; `best` keeps the
            highest-information sentence per NMEA timestamp.

    Returns:
        Filtered original NMEA sentences containing position reports. GGA and
        GNS are preferred over RMC in `best` mode because they carry fix
        quality/mode information. Fractional timestamp text is preserved in the
        grouping key, so multi-Hz position streams keep every epoch.
    """

    if mode == "all":
        return [line for line in lines if _position_nmea_candidate(line) is not None]
    if mode != "best":
        raise ValueError(f"unsupported position NMEA mode: {mode}")

    records: list[str] = []
    current_key: str | None = None
    current_best: tuple[int, str] | None = None
    for line in lines:
        candidate = _position_nmea_candidate(line)
        if candidate is None:
            continue
        key, rank = candidate
        if current_key is not None and key != current_key and current_best is not None:
            records.append(current_best[1])
            current_best = None
        current_key = key
        if current_best is None or rank > current_best[0]:
            current_best = (rank, line)
    if current_best is not None:
        records.append(current_best[1])
    return records


def _position_nmea_candidate(line: str) -> tuple[str, int] | None:
    parsed = parse_sentence(line)
    if parsed is None:
        return None
    typ = sentence_type(parsed.talker_type)
    fields = parsed.fields
    if typ == "GGA":
        if len(fields) < 6 or dm_to_decimal(fields[1], fields[2]) is None or dm_to_decimal(fields[3], fields[4]) is None:
            return None
        fix = int_or_none(fields[5]) or 0
        if fix <= 0:
            return None
        return fields[0], 300 + fix
    if typ == "GNS":
        if len(fields) < 6 or dm_to_decimal(fields[1], fields[2]) is None or dm_to_decimal(fields[3], fields[4]) is None:
            return None
        modes = fields[5]
        usable_modes = sum(1 for char in modes if char and char.upper() != "N")
        if usable_modes <= 0:
            return None
        return fields[0], 250 + usable_modes
    if typ == "RMC":
        if len(fields) < 6 or fields[1] != "A":
            return None
        if dm_to_decimal(fields[2], fields[3]) is None or dm_to_decimal(fields[4], fields[5]) is None:
            return None
        return fields[0], 100
    return None


def write_solution_nmea(path: Path, points: list[SolutionPoint]) -> None:
    """Write compact proprietary NMEA solution summary records.

    Args:
        path: Destination NMEA path.
        points: Solution points to summarise.
    """

    lines = []
    for point in points:
        body = (
            f"PUM980Q,{point.source},{point.sol_status or ''},{point.pos_type or ''},"
            f"{point.num_sats or ''},{point.sigma_e or ''},{point.sigma_n or ''},"
            f"{point.sigma_u or ''},{point.age_diff or ''}"
        )
        lines.append(make_sentence(body))
    write_lines(path, lines)


def write_gpx(path: Path, points: list[SolutionPoint]) -> None:
    """Write solution points as a GPX track.

    Args:
        path: Destination GPX path.
        points: Solution points to write.
    """

    ET.register_namespace("", "http://www.topografix.com/GPX/1/1")
    ET.register_namespace("um980", "https://github.com/holubp/um980-rtklib-pipeline")
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "um980-ppk",
            "xmlns": "http://www.topografix.com/GPX/1/1",
            "xmlns:um980": "https://github.com/holubp/um980-rtklib-pipeline",
        },
    )
    trk = ET.SubElement(root, "trk")
    ET.SubElement(trk, "name").text = path.stem
    seg = ET.SubElement(trk, "trkseg")
    for point in points:
        trkpt = ET.SubElement(seg, "trkpt", {"lat": f"{point.lat:.10f}", "lon": f"{point.lon:.10f}"})
        if point.h_ell is not None or point.h_msl is not None:
            ET.SubElement(trkpt, "ele").text = f"{(point.h_ell if point.h_ell is not None else point.h_msl):.4f}"
        ET.SubElement(trkpt, "time").text = point.time_utc.isoformat().replace("+00:00", "Z")
        ext = ET.SubElement(trkpt, "extensions")
        ns = "{https://github.com/holubp/um980-rtklib-pipeline}"
        values = {
            "source": point.source,
            "fixQuality": point.fix_quality_text or point.fix_quality,
            "positionType": point.pos_type,
            "solutionStatus": point.sol_status,
            "numSats": point.num_sats,
            "sigmaE": point.sigma_e,
            "sigmaN": point.sigma_n,
            "sigmaU": point.sigma_u,
        }
        for name, value in values.items():
            if value is not None:
                ET.SubElement(ext, ns + name).text = str(value)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
