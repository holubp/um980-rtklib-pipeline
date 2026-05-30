"""BESTNAV receiver-solution parsing and NMEA synthesis.

BESTNAV[A/B] records are receiver solution products. They are useful for
app-readable tracks and diagnostics, but they are not raw observations and must
not be used as RTKLIB estimation input.
"""

from __future__ import annotations

import csv
import struct
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .nmea import make_sentence
from .stream import StreamRecord
from .timeutil import gps_week_tow_to_utc_datetime

BestNavSource = Literal["BESTNAVA", "BESTNAVB"]
BestNavNmeaSentence = Literal["GGA", "RMC", "VTG"]
BestNavNmeaSource = Literal["auto", "ascii", "binary"]

VALID_BESTNAV_SENTENCES = {"GGA", "RMC", "VTG"}
VALID_BESTNAV_SOURCES = {"auto", "ascii", "binary"}
MPS_TO_KNOTS = 1.94384449
MPS_TO_KMH = 3.6
BESTNAVB_PAYLOAD_BYTES = 120
POSITION_TYPE_NAMES = {
    0: "NONE",
    1: "FIXEDPOS",
    2: "FIXEDHEIGHT",
    8: "DOPPLER_VELOCITY",
    16: "SINGLE",
    17: "PSRDIFF",
    18: "SBAS",
    32: "L1_FLOAT",
    33: "IONOFREE_FLOAT",
    34: "NARROW_FLOAT",
    48: "L1_INT",
    49: "WIDE_INT",
    50: "NARROW_INT",
    52: "INS",
    53: "INS_PSRSP",
    54: "INS_PSRDIFF",
    55: "INS_RTKFLOAT",
    56: "INS_RTKFIXED",
    68: "PPP_CONVERGING",
    69: "PPP",
}
SOLUTION_STATUS_NAMES = {
    0: "SOL_COMPUTED",
    1: "INSUFFICIENT_OBS",
    2: "NO_CONVERGENCE",
    4: "COV_TRACE",
}


@dataclass(frozen=True)
class BestNavRecord:
    """One decoded UM980 BESTNAV receiver-solution record.

    Attributes mirror the documented BESTNAV fields used by generated NMEA.
    Unknown enum values are preserved as text where possible so diagnostics can
    remain useful without guessing semantics.
    """

    source: BestNavSource
    time_utc: datetime
    gps_week: int
    tow_s: float
    pos_sol_status: str
    pos_type: str
    lat_deg: float
    lon_deg: float
    height_msl_m: float
    undulation_m: float
    datum: str | int
    lat_sigma_m: float | None
    lon_sigma_m: float | None
    height_sigma_m: float | None
    station_id: str | None
    differential_age_s: float | None
    solution_age_s: float | None
    satellites_tracked: int | None
    satellites_used: int | None
    vel_sol_status: str | None
    vel_type: str | None
    horizontal_speed_mps: float | None
    track_deg: float | None
    vertical_speed_mps: float | None
    raw_record_index: int | None

    @property
    def time_key(self) -> float:
        """Return a monotonic-ish timestamp key in GPS seconds."""

        return self.gps_week * 604800.0 + self.tow_s


@dataclass
class BestNavExtraction:
    """Decoded BESTNAV records plus non-fatal parse diagnostics."""

    records: list[BestNavRecord] = field(default_factory=list)
    present: Counter[str] = field(default_factory=Counter)
    malformed: Counter[str] = field(default_factory=Counter)
    present_not_converted: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly BESTNAV extraction details."""

        return {
            "records": len(self.records),
            "valid_epochs": len(self.records),
            "present": dict(self.present),
            "malformed": dict(self.malformed),
            "present_not_converted": dict(self.present_not_converted),
            "warnings": self.warnings,
            "first_time": self.records[0].time_utc.isoformat() if self.records else None,
            "last_time": self.records[-1].time_utc.isoformat() if self.records else None,
            "native_hz": estimate_native_hz(self.records),
        }


def extract_bestnav_records(records: list[StreamRecord]) -> BestNavExtraction:
    """Decode supported BESTNAV receiver-solution records from a stream.

    ASCII `BESTNAVA` and documented binary `BESTNAVB` message ID 2118 are
    decoded. Malformed records are counted and skipped without stopping the
    surrounding extraction pipeline.
    """

    result = BestNavExtraction()
    for index, record in enumerate(records):
        if record.msg_type == "BESTNAVA":
            result.present["BESTNAVA"] += 1
            if record.checksum_ok is False:
                result.malformed["BESTNAVA_checksum"] += 1
                continue
            try:
                result.records.append(parse_bestnava(record.text or "", raw_record_index=index))
            except ValueError:
                result.malformed["BESTNAVA"] += 1
        elif record.msg_type == "BESTNAVB":
            result.present["BESTNAVB"] += 1
            try:
                result.records.append(parse_bestnavb(record.raw, raw_record_index=index))
            except ValueError:
                result.malformed["BESTNAVB"] += 1
    if result.present_not_converted:
        result.warnings.append("some BESTNAV records were present but not converted to generated NMEA.")
    if result.malformed:
        result.warnings.append(
            "some BESTNAV records were malformed or failed checksum validation: "
            + ", ".join(f"{key}={value}" for key, value in sorted(result.malformed.items()))
        )
    return result


def parse_bestnavb(raw: bytes, *, raw_record_index: int | None = None) -> BestNavRecord:
    """Parse one binary `BESTNAVB` frame.

    The payload layout follows Unicore message ID 2118. The stream parser
    supplies the complete frame, including the fixed 24-byte binary header and
    trailing CRC.
    """

    if len(raw) < 24 + BESTNAVB_PAYLOAD_BYTES + 4:
        raise ValueError("BESTNAVB frame is shorter than the documented payload")
    msg_id = int.from_bytes(raw[4:6], "little", signed=False)
    if msg_id != 2118:
        raise ValueError(f"not a BESTNAVB frame: message ID {msg_id}")
    payload_length = int.from_bytes(raw[6:8], "little", signed=False)
    if payload_length < BESTNAVB_PAYLOAD_BYTES:
        raise ValueError(f"BESTNAVB payload has {payload_length} bytes, expected at least {BESTNAVB_PAYLOAD_BYTES}")
    gps_week = int.from_bytes(raw[10:12], "little", signed=False)
    tow_ms = int.from_bytes(raw[12:16], "little", signed=False)
    tow_s = tow_ms / 1000.0
    payload = raw[24 : 24 + payload_length]

    pos_status_id = _u32(payload, 0)
    pos_type_id = _u32(payload, 4)
    vel_status_id = _u32(payload, 72)
    vel_type_id = _u32(payload, 76)
    station_id = payload[52:56].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    if station_id in {"", "0", "0000"}:
        station_id = None

    return BestNavRecord(
        source="BESTNAVB",
        time_utc=gps_week_tow_to_utc_datetime(gps_week, tow_s),
        gps_week=gps_week,
        tow_s=tow_s,
        pos_sol_status=SOLUTION_STATUS_NAMES.get(pos_status_id, f"STATUS_{pos_status_id}"),
        pos_type=POSITION_TYPE_NAMES.get(pos_type_id, f"TYPE_{pos_type_id}"),
        lat_deg=_f64(payload, 8),
        lon_deg=_f64(payload, 16),
        height_msl_m=_f64(payload, 24),
        undulation_m=_f32(payload, 32),
        datum=_u32(payload, 36),
        lat_sigma_m=_f32(payload, 40),
        lon_sigma_m=_f32(payload, 44),
        height_sigma_m=_f32(payload, 48),
        station_id=station_id,
        differential_age_s=_f32(payload, 56),
        solution_age_s=_f32(payload, 60),
        satellites_tracked=payload[64],
        satellites_used=payload[65],
        vel_sol_status=SOLUTION_STATUS_NAMES.get(vel_status_id, f"STATUS_{vel_status_id}"),
        vel_type=POSITION_TYPE_NAMES.get(vel_type_id, f"TYPE_{vel_type_id}"),
        horizontal_speed_mps=_f64(payload, 88),
        track_deg=_f64(payload, 96),
        vertical_speed_mps=_f64(payload, 104),
        raw_record_index=raw_record_index,
    )


def parse_bestnava(text: str, *, raw_record_index: int | None = None) -> BestNavRecord:
    """Parse one ASCII `BESTNAVA` record.

    Args:
        text: Complete Unicore ASCII record beginning with `#BESTNAVA`.
        raw_record_index: Optional source-record index for diagnostics.

    Returns:
        A decoded BESTNAV receiver solution.

    Raises:
        ValueError: If required time or position fields are missing.
    """

    if not text.startswith("#BESTNAVA"):
        raise ValueError("not a BESTNAVA record")
    body = text.strip()[1:].split("*", 1)[0]
    header_text, sep, payload_text = body.partition(";")
    if not sep:
        raise ValueError("BESTNAVA record has no payload separator")
    header = _csv_fields(header_text)
    payload = _csv_fields(payload_text)
    gps_week, tow_s = _extract_header_time(header)
    time_utc = gps_week_tow_to_utc_datetime(gps_week, tow_s)
    if len(payload) < 6:
        raise ValueError("BESTNAVA payload is too short")

    pos_status = payload[0].strip()
    pos_type = payload[1].strip()
    lat = _required_float(payload, 2, "latitude")
    lon = _required_float(payload, 3, "longitude")
    height_msl = _required_float(payload, 4, "height")
    undulation = _float_at(payload, 5) or 0.0
    station_id = _string_at(payload, 10)
    if station_id in {"0", "0000"}:
        station_id = None

    return BestNavRecord(
        source="BESTNAVA",
        time_utc=time_utc,
        gps_week=gps_week,
        tow_s=tow_s,
        pos_sol_status=pos_status,
        pos_type=pos_type,
        lat_deg=lat,
        lon_deg=lon,
        height_msl_m=height_msl,
        undulation_m=undulation,
        datum=_string_at(payload, 6) or "",
        lat_sigma_m=_float_at(payload, 7),
        lon_sigma_m=_float_at(payload, 8),
        height_sigma_m=_float_at(payload, 9),
        station_id=station_id,
        differential_age_s=_float_at(payload, 11),
        solution_age_s=_float_at(payload, 12),
        satellites_tracked=_int_at(payload, 13),
        satellites_used=_int_at(payload, 14),
        vel_sol_status=_string_at(payload, 18),
        vel_type=_string_at(payload, 19),
        horizontal_speed_mps=_float_at(payload, 20),
        track_deg=_float_at(payload, 21),
        vertical_speed_mps=_float_at(payload, 22),
        raw_record_index=raw_record_index,
    )


def parse_bestnav_sentences(value: str | None) -> tuple[BestNavNmeaSentence, ...]:
    """Parse and validate a comma-separated BESTNAV NMEA sentence list."""

    text = value or "GGA,RMC,VTG"
    requested = tuple(item.strip().upper() for item in text.split(",") if item.strip())
    invalid = sorted(set(requested) - VALID_BESTNAV_SENTENCES)
    if invalid:
        raise ValueError(f"unsupported BESTNAV NMEA sentence names: {', '.join(invalid)}")
    return requested or ("GGA", "RMC", "VTG")


def parse_bestnav_rate(value: str | None) -> float | None:
    """Return `None` for native output or a positive requested output Hz."""

    if value is None or value.strip().lower() == "native":
        return None
    rate = float(value)
    if rate <= 0:
        raise ValueError("--bestnav-nmea-rate must be native or greater than zero")
    return rate


def filter_bestnav_records(
    records: list[BestNavRecord],
    *,
    source: BestNavNmeaSource = "auto",
    rate_hz: float | None = None,
) -> list[BestNavRecord]:
    """Filter BESTNAV records by source and timestamp-decimate without upsampling."""

    if source not in VALID_BESTNAV_SOURCES:
        raise ValueError(f"unsupported BESTNAV source: {source}")
    allowed = {"BESTNAVA", "BESTNAVB"}
    if source == "ascii":
        allowed = {"BESTNAVA"}
    elif source == "binary":
        allowed = {"BESTNAVB"}
    candidates = [record for record in records if record.source in allowed]
    if rate_hz is None:
        return _dedupe_timestamps(candidates)

    interval = 1.0 / rate_hz
    selected: list[BestNavRecord] = []
    next_time: float | None = None
    seen: set[float] = set()
    for record in candidates:
        key = round(record.time_key, 7)
        if key in seen:
            continue
        seen.add(key)
        if next_time is None or record.time_key + 1e-7 >= next_time:
            selected.append(record)
            next_time = record.time_key + interval if next_time is None else next_time + interval
            while next_time is not None and record.time_key + 1e-7 >= next_time:
                next_time += interval
    return selected


def bestnav_records_to_nmea(
    records: list[BestNavRecord],
    *,
    sentences: tuple[BestNavNmeaSentence, ...] = ("GGA", "RMC", "VTG"),
    talk_id: str = "GN",
) -> list[str]:
    """Generate checksummed NMEA from decoded BESTNAV receiver solutions."""

    talk = talk_id.upper()
    if talk not in {"GN", "GP"}:
        raise ValueError("--bestnav-nmea-talk-id must be GN or GP")
    output: list[str] = []
    for record in records:
        if not _has_position(record):
            continue
        for sentence in sentences:
            if sentence == "GGA":
                output.append(_gga(record, talk))
            elif sentence == "RMC":
                output.append(_rmc(record, talk))
            elif sentence == "VTG":
                output.append(_vtg(record, talk))
            else:  # pragma: no cover - parse_bestnav_sentences guards this.
                raise ValueError(f"unsupported BESTNAV NMEA sentence: {sentence}")
    return output


def estimate_native_hz(records: list[BestNavRecord]) -> float | None:
    """Estimate native BESTNAV cadence from decoded timestamps."""

    times = [record.time_key for record in _dedupe_timestamps(records)]
    if len(times) < 2:
        return None
    intervals = [right - left for left, right in zip(times, times[1:], strict=False) if right > left]
    if not intervals:
        return None
    intervals.sort()
    median = intervals[len(intervals) // 2]
    return None if median <= 0 else 1.0 / median


def _csv_fields(text: str) -> list[str]:
    return next(csv.reader([text], skipinitialspace=True))


def _extract_header_time(header: list[str]) -> tuple[int, float]:
    for index, token in enumerate(header[:-1]):
        week = _int_text(token)
        tow = _float_text(header[index + 1])
        if week is None or tow is None:
            continue
        if 1024 <= week <= 4096 and 0 <= tow <= 604800000:
            tow_s = tow / 1000.0 if tow > 604800 else tow
            return week, tow_s
    raise ValueError("BESTNAVA header has no plausible GPS week/TOW")


def _required_float(fields: list[str], index: int, name: str) -> float:
    value = _float_at(fields, index)
    if value is None:
        raise ValueError(f"BESTNAVA missing {name}")
    return value


def _float_at(fields: list[str], index: int) -> float | None:
    if index >= len(fields):
        return None
    return _float_text(fields[index])


def _int_at(fields: list[str], index: int) -> int | None:
    if index >= len(fields):
        return None
    return _int_text(fields[index])


def _string_at(fields: list[str], index: int) -> str | None:
    if index >= len(fields):
        return None
    value = fields[index].strip().strip('"')
    return value or None


def _float_text(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


def _int_text(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def _u32(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<I", payload, offset)[0]


def _f32(payload: bytes, offset: int) -> float:
    return struct.unpack_from("<f", payload, offset)[0]


def _f64(payload: bytes, offset: int) -> float:
    return struct.unpack_from("<d", payload, offset)[0]


def _dedupe_timestamps(records: list[BestNavRecord]) -> list[BestNavRecord]:
    selected: list[BestNavRecord] = []
    seen: set[float] = set()
    for record in records:
        key = round(record.time_key, 7)
        if key in seen:
            continue
        seen.add(key)
        selected.append(record)
    return selected


def _has_position(record: BestNavRecord) -> bool:
    return record.pos_sol_status.upper() not in {"NONE", "NO_SOLUTION", "SOL_INVALID", "INSUFFICIENT_OBS"}


def _gga(record: BestNavRecord, talk: str) -> str:
    lat, ns = _format_lat(record.lat_deg)
    lon, ew = _format_lon(record.lon_deg)
    fields = [
        f"{talk}GGA",
        _time_text(record.time_utc),
        lat,
        ns,
        lon,
        ew,
        _gga_quality(record),
        str(record.satellites_used or record.satellites_tracked or ""),
        "",
        _format_float(record.height_msl_m, 3),
        "M",
        _format_float(record.undulation_m, 3),
        "M",
        _format_optional_float(record.differential_age_s, 1, blank_zero=True),
        record.station_id or "",
    ]
    return make_sentence(",".join(fields))


def _rmc(record: BestNavRecord, talk: str) -> str:
    lat, ns = _format_lat(record.lat_deg)
    lon, ew = _format_lon(record.lon_deg)
    speed_knots = None if record.horizontal_speed_mps is None else record.horizontal_speed_mps * MPS_TO_KNOTS
    fields = [
        f"{talk}RMC",
        _time_text(record.time_utc),
        "A" if _has_position(record) else "V",
        lat,
        ns,
        lon,
        ew,
        _format_optional_float(speed_knots, 3),
        _format_optional_float(record.track_deg, 3),
        _date_text(record.time_utc),
        "",
        "",
        _mode_indicator(record),
    ]
    return make_sentence(",".join(fields))


def _vtg(record: BestNavRecord, talk: str) -> str:
    speed_knots = None if record.horizontal_speed_mps is None else record.horizontal_speed_mps * MPS_TO_KNOTS
    speed_kmh = None if record.horizontal_speed_mps is None else record.horizontal_speed_mps * MPS_TO_KMH
    fields = [
        f"{talk}VTG",
        _format_optional_float(record.track_deg, 3),
        "T",
        "",
        "M",
        _format_optional_float(speed_knots, 3),
        "N",
        _format_optional_float(speed_kmh, 3),
        "K",
        _mode_indicator(record),
    ]
    return make_sentence(",".join(fields))


def _time_text(value: datetime) -> str:
    second = value.second + value.microsecond / 1_000_000.0
    return f"{value.hour:02d}{value.minute:02d}{second:06.3f}"


def _date_text(value: datetime) -> str:
    return f"{value.day:02d}{value.month:02d}{value.year % 100:02d}"


def _format_lat(value: float) -> tuple[str, str]:
    return _format_coord(value, 2, "N", "S")


def _format_lon(value: float) -> tuple[str, str]:
    return _format_coord(value, 3, "E", "W")


def _format_coord(value: float, degree_digits: int, positive: str, negative: str) -> tuple[str, str]:
    hemisphere = positive if value >= 0 else negative
    absolute = abs(value)
    degrees = int(absolute)
    minutes = (absolute - degrees) * 60.0
    return f"{degrees:0{degree_digits}d}{minutes:010.7f}", hemisphere


def _format_float(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _format_optional_float(value: float | None, decimals: int, *, blank_zero: bool = False) -> str:
    if value is None or (blank_zero and abs(value) < 1e-12):
        return ""
    return f"{value:.{decimals}f}"


def _gga_quality(record: BestNavRecord) -> str:
    pos_type = record.pos_type.upper()
    if not _has_position(record):
        return "0"
    if _is_rtk_fixed(pos_type):
        return "4"
    if _is_rtk_float(pos_type):
        return "5"
    if "DIFF" in pos_type or "SBAS" in pos_type:
        return "2"
    if "INS" in pos_type and "GNSS" not in pos_type and "RTK" not in pos_type:
        return "6"
    return "1"


def _mode_indicator(record: BestNavRecord) -> str:
    pos_type = record.pos_type.upper()
    if not _has_position(record):
        return "N"
    if _is_rtk_fixed(pos_type):
        return "R"
    if _is_rtk_float(pos_type):
        return "F"
    if "DIFF" in pos_type or "SBAS" in pos_type:
        return "D"
    if "INS" in pos_type and "GNSS" not in pos_type and "RTK" not in pos_type:
        return "E"
    return "A"


def _is_rtk_fixed(pos_type: str) -> bool:
    return any(token in pos_type for token in ("NARROW_INT", "L1_INT", "RTKFIXED", "FIXED"))


def _is_rtk_float(pos_type: str) -> bool:
    return any(token in pos_type for token in ("NARROW_FLOAT", "RTKFLOAT", "FLOAT"))
