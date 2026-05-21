"""NMEA parsing and sentence generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isnan

from .timeutil import parse_ddmmyy, parse_hhmmss


@dataclass(frozen=True)
class NmeaRecord:
    talker_type: str
    fields: list[str]
    text: str
    checksum_ok: bool | None


def parse_sentence(text: str, checksum_ok: bool | None = None) -> NmeaRecord | None:
    if not text.startswith("$"):
        return None
    body = text.strip()[1:].split("*", 1)[0]
    parts = body.split(",")
    if not parts or len(parts[0]) < 3:
        return None
    return NmeaRecord(parts[0], parts[1:], text.strip(), checksum_ok)


def sentence_type(talker_type: str) -> str:
    return talker_type[-3:]


def checksum(body: str) -> str:
    value = 0
    for char in body.encode("ascii"):
        value ^= char
    return f"{value:02X}"


def make_sentence(body: str) -> str:
    return f"${body}*{checksum(body)}"


def dm_to_decimal(value: str, hemisphere: str) -> float | None:
    if not value or not hemisphere:
        return None
    try:
        if "." in value:
            dot = value.index(".")
            degrees_len = dot - 2
        else:
            degrees_len = len(value) - 2
        degrees = int(value[:degrees_len])
        minutes = float(value[degrees_len:])
    except (ValueError, IndexError):
        return None
    result = degrees + minutes / 60.0
    if hemisphere.upper() in {"S", "W"}:
        result = -result
    return result


def float_or_none(value: str) -> float | None:
    if value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return None if isnan(result) else result


def int_or_none(value: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def datetime_from_time_date(time_text: str, date_text: str | None) -> datetime | None:
    parsed_time = parse_hhmmss(time_text)
    if parsed_time is None or not date_text:
        return None
    parsed_date = parse_ddmmyy(date_text)
    if parsed_date is None:
        return None
    year, month, day = parsed_date
    hour, minute, second = parsed_time
    whole_second = int(second)
    microsecond = int(round((second - whole_second) * 1_000_000))
    return datetime(year, month, day, hour, minute, whole_second, microsecond, tzinfo=UTC)


def datetime_from_time_with_context(time_text: str, context_date: datetime | None) -> datetime | None:
    parsed_time = parse_hhmmss(time_text)
    if parsed_time is None or context_date is None:
        return None
    hour, minute, second = parsed_time
    whole_second = int(second)
    microsecond = int(round((second - whole_second) * 1_000_000))
    return datetime(
        context_date.year,
        context_date.month,
        context_date.day,
        hour,
        minute,
        whole_second,
        microsecond,
        tzinfo=UTC,
    )


FIX_QUALITY = {
    0: "invalid",
    1: "gps",
    2: "dgps",
    4: "rtk-fixed",
    5: "rtk-float",
    6: "estimated",
}

