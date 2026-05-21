"""Time conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)


def gps_week_tow_to_datetime(gps_week: int, tow: float) -> datetime:
    return GPS_EPOCH + timedelta(weeks=gps_week, seconds=tow)


def parse_hhmmss(value: str) -> tuple[int, int, float] | None:
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

