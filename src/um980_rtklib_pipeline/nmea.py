"""NMEA parsing and sentence generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isnan

from .timeutil import parse_ddmmyy, parse_hhmmss


@dataclass(frozen=True)
class NmeaRecord:
    """Parsed NMEA sentence.

    Attributes:
        talker_type: Combined talker and sentence type, for example `GNGGA`.
        fields: Comma-separated sentence fields after the type.
        text: Original sentence text.
        checksum_ok: Optional checksum validation result.
    """

    talker_type: str
    fields: list[str]
    text: str
    checksum_ok: bool | None


def parse_sentence(text: str, checksum_ok: bool | None = None) -> NmeaRecord | None:
    """Parse a NMEA sentence into fields.

    Args:
        text: Sentence text beginning with `$`.
        checksum_ok: Optional checksum validation result from stream parsing.

    Returns:
        Parsed record, or `None` when the text is not a valid NMEA sentence.
    """

    if not text.startswith("$"):
        return None
    body = text.strip()[1:].split("*", 1)[0]
    parts = body.split(",")
    if not parts or len(parts[0]) < 3:
        return None
    return NmeaRecord(parts[0], parts[1:], text.strip(), checksum_ok)


def sentence_type(talker_type: str) -> str:
    """Return the three-letter NMEA sentence type.

    Args:
        talker_type: Combined talker/type string such as `GNGGA`.

    Returns:
        Last three characters of the talker/type string.
    """

    return talker_type[-3:]


def checksum(body: str) -> str:
    """Compute a NMEA checksum.

    Args:
        body: Sentence body without `$` or `*hh`.

    Returns:
        Two-digit uppercase hexadecimal checksum.
    """

    value = 0
    for char in body.encode("ascii"):
        value ^= char
    return f"{value:02X}"


def make_sentence(body: str) -> str:
    """Build a NMEA sentence with checksum.

    Args:
        body: Sentence body without `$` or checksum.

    Returns:
        Full NMEA sentence text.
    """

    return f"${body}*{checksum(body)}"


def dm_to_decimal(value: str, hemisphere: str) -> float | None:
    """Convert NMEA degrees/minutes coordinates to decimal degrees.

    Args:
        value: NMEA coordinate field.
        hemisphere: Hemisphere field (`N`, `S`, `E`, or `W`).

    Returns:
        Decimal degrees, or `None` when parsing fails.
    """

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
    """Parse a float field.

    Args:
        value: Text field.

    Returns:
        Float value, or `None` for blank, invalid, or NaN input.
    """

    if value == "":
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return None if isnan(result) else result


def int_or_none(value: str) -> int | None:
    """Parse an integer field.

    Args:
        value: Text field.

    Returns:
        Integer value, or `None` for blank or invalid input.
    """

    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def datetime_from_time_date(time_text: str, date_text: str | None) -> datetime | None:
    """Build a UTC datetime from NMEA time and date fields.

    Args:
        time_text: NMEA `hhmmss.sss` time.
        date_text: NMEA `ddmmyy` date.

    Returns:
        UTC datetime, or `None` when parsing fails.
    """

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
    """Build a UTC datetime from NMEA time and an existing date.

    Args:
        time_text: NMEA `hhmmss.sss` time.
        context_date: Date source for year/month/day.

    Returns:
        UTC datetime, or `None` when parsing fails.
    """

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
