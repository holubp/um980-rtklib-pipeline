"""Processing time-window helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ProcessingWindow:
    """Optional UTC processing interval selected by the user."""

    start: datetime | None = None
    end: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Return true when either window bound is active."""

        return self.start is not None or self.end is not None

    def contains(self, value: datetime) -> bool:
        """Return true when a timestamp falls inside the selected interval."""

        current = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if self.start is not None and current < self.start:
            return False
        if self.end is not None and current > self.end:
            return False
        return True

    def as_dict(self) -> dict[str, str | bool | None]:
        """Return a JSON-friendly representation."""

        return {
            "enabled": self.enabled,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "timezone_assumption": "UTC for naive input datetimes",
        }


def parse_datetime_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime and normalise it to UTC.

    Naive datetimes are interpreted as UTC. A trailing ``Z`` is accepted.
    """

    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def processing_window_from_values(start: str | None, end: str | None) -> ProcessingWindow:
    """Build and validate a processing window from CLI strings."""

    window = ProcessingWindow(parse_datetime_utc(start), parse_datetime_utc(end))
    if window.start is not None and window.end is not None and window.end <= window.start:
        raise ValueError("--end-time must be later than --start-time")
    return window
