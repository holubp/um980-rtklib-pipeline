"""Time conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
GPS_UTC_OFFSETS = (
    (datetime(1981, 7, 1, tzinfo=UTC), 1),
    (datetime(1982, 7, 1, tzinfo=UTC), 2),
    (datetime(1983, 7, 1, tzinfo=UTC), 3),
    (datetime(1985, 7, 1, tzinfo=UTC), 4),
    (datetime(1988, 1, 1, tzinfo=UTC), 5),
    (datetime(1990, 1, 1, tzinfo=UTC), 6),
    (datetime(1991, 1, 1, tzinfo=UTC), 7),
    (datetime(1992, 7, 1, tzinfo=UTC), 8),
    (datetime(1993, 7, 1, tzinfo=UTC), 9),
    (datetime(1994, 7, 1, tzinfo=UTC), 10),
    (datetime(1996, 1, 1, tzinfo=UTC), 11),
    (datetime(1997, 7, 1, tzinfo=UTC), 12),
    (datetime(1999, 1, 1, tzinfo=UTC), 13),
    (datetime(2006, 1, 1, tzinfo=UTC), 14),
    (datetime(2009, 1, 1, tzinfo=UTC), 15),
    (datetime(2012, 7, 1, tzinfo=UTC), 16),
    (datetime(2015, 7, 1, tzinfo=UTC), 17),
    (datetime(2017, 1, 1, tzinfo=UTC), 18),
)


def gps_week_tow_to_datetime(gps_week: int, tow: float) -> datetime:
    """Convert GPS week and time-of-week to an aware UTC datetime.

    Args:
        gps_week: GPS week number.
        tow: GPS seconds of week.

    Returns:
        UTC datetime corresponding to the GPS time value.
    """

    return GPS_EPOCH + timedelta(weeks=gps_week, seconds=tow)


def gps_week_tow_to_utc_datetime(gps_week: int, tow: float) -> datetime:
    """Convert GPS week/TOW to UTC using the built-in leap-second table.

    Args:
        gps_week: GPS week number.
        tow: GPS seconds of week.

    Returns:
        UTC datetime. For May 2026 logs this applies GPS-UTC = 18 s.
    """

    gps_time = gps_week_tow_to_datetime(gps_week, tow)
    return gps_time - timedelta(seconds=gps_utc_offset_seconds(gps_time))


def gps_utc_offset_seconds(gps_time: datetime) -> int:
    """Return GPS-UTC seconds for a GPS-time datetime."""

    value = 0
    for effective_utc, offset in GPS_UTC_OFFSETS:
        effective_gps = effective_utc + timedelta(seconds=offset)
        if gps_time >= effective_gps:
            value = offset
    return value


def parse_hhmmss(value: str) -> tuple[int, int, float] | None:
    """Parse an NMEA `hhmmss.sss` time field.

    Args:
        value: Time text from a NMEA sentence.

    Returns:
        `(hour, minute, second)` or `None` when parsing fails.
    """

    if len(value) < 6:
        return None
    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        second = float(value[4:])
    except ValueError:
        return None
    return hour, minute, second


def parse_ddmmyy(value: str) -> tuple[int, int, int] | None:
    """Parse an NMEA `ddmmyy` date field.

    Args:
        value: Date text from a NMEA sentence.

    Returns:
        `(year, month, day)` or `None` when parsing fails.
    """

    if len(value) != 6:
        return None
    try:
        day = int(value[0:2])
        month = int(value[2:4])
        yy = int(value[4:6])
    except ValueError:
        return None
    year = 2000 + yy if yy < 80 else 1900 + yy
    return year, month, day
