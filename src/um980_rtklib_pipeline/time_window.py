"""Processing time-window helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True, init=False)
class ProcessingWindow:
    """Optional UTC processing interval selected by the user."""

    start_time_utc: datetime | None = None
    end_time_utc: datetime | None = None
    inclusive_start: bool = True
    inclusive_end: bool = True
    source: Literal["cli", "manifest", "none"] = "none"

    def __init__(
        self,
        start_time_utc: datetime | None = None,
        end_time_utc: datetime | None = None,
        inclusive_start: bool = True,
        inclusive_end: bool = True,
        source: Literal["cli", "manifest", "none"] = "none",
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        """Create a processing window.

        `start` and `end` are accepted as compatibility aliases for existing
        callers that used the earlier dataclass field names.
        """

        if start is not None:
            start_time_utc = start
        if end is not None:
            end_time_utc = end
        object.__setattr__(self, "start_time_utc", start_time_utc)
        object.__setattr__(self, "end_time_utc", end_time_utc)
        object.__setattr__(self, "inclusive_start", inclusive_start)
        object.__setattr__(self, "inclusive_end", inclusive_end)
        object.__setattr__(self, "source", source)
        self.__post_init__()

    def __post_init__(self) -> None:
        start = _normalise_datetime(self.start_time_utc)
        end = _normalise_datetime(self.end_time_utc)
        source: Literal["cli", "manifest", "none"] = self.source
        if source == "none" and (start is not None or end is not None):
            source = "cli"
        if start is not None and end is not None and end <= start:
            raise ValueError("--end-time must be later than --start-time")
        object.__setattr__(self, "start_time_utc", start)
        object.__setattr__(self, "end_time_utc", end)
        object.__setattr__(self, "source", source)

    @property
    def start(self) -> datetime | None:
        """Backward-compatible start bound alias."""

        return self.start_time_utc

    @property
    def end(self) -> datetime | None:
        """Backward-compatible end bound alias."""

        return self.end_time_utc

    @property
    def enabled(self) -> bool:
        """Return true when either window bound is active."""

        return self.start_time_utc is not None or self.end_time_utc is not None

    def contains(self, value: datetime) -> bool:
        """Return true when a timestamp falls inside the selected interval."""

        current = _normalise_datetime(value)
        if current is None:
            return False
        if self.start_time_utc is not None and (
            current < self.start_time_utc or (current == self.start_time_utc and not self.inclusive_start)
        ):
            return False
        if self.end_time_utc is not None and (
            current > self.end_time_utc or (current == self.end_time_utc and not self.inclusive_end)
        ):
            return False
        return True

    def overlaps(self, start: datetime | None, end: datetime | None) -> bool:
        """Return true when an interval overlaps this window."""

        clamped = self.clamp_interval(start, end)
        return clamped is not None

    def clamp_interval(
        self,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[datetime | None, datetime | None] | None:
        """Return `start`/`end` clamped to the processing window, or `None`."""

        current_start = _normalise_datetime(start)
        current_end = _normalise_datetime(end)
        if current_start is not None and current_end is not None and current_end < current_start:
            return None
        if self.start_time_utc is not None and (current_end is None or current_end < self.start_time_utc):
            return None
        if self.end_time_utc is not None and (current_start is None or current_start > self.end_time_utc):
            return None
        if self.start_time_utc is not None and (current_start is None or current_start < self.start_time_utc):
            current_start = self.start_time_utc
        if self.end_time_utc is not None and (current_end is None or current_end > self.end_time_utc):
            current_end = self.end_time_utc
        return current_start, current_end

    def to_cli_args(self) -> list[str]:
        """Return canonical CLI arguments for this window."""

        args: list[str] = []
        if self.start_time_utc is not None:
            args.extend(["--start-time", self.start_time_utc.isoformat()])
        if self.end_time_utc is not None:
            args.extend(["--end-time", self.end_time_utc.isoformat()])
        return args

    def to_json(self) -> dict[str, str | bool | None]:
        """Return a JSON-friendly representation."""

        return {
            "enabled": self.enabled,
            "start": self.start_time_utc.isoformat() if self.start_time_utc else None,
            "end": self.end_time_utc.isoformat() if self.end_time_utc else None,
            "start_time": self.start_time_utc.isoformat() if self.start_time_utc else None,
            "end_time": self.end_time_utc.isoformat() if self.end_time_utc else None,
            "start_time_utc": self.start_time_utc.isoformat() if self.start_time_utc else None,
            "end_time_utc": self.end_time_utc.isoformat() if self.end_time_utc else None,
            "inclusive_start": self.inclusive_start,
            "inclusive_end": self.inclusive_end,
            "source": self.source,
            "timezone_assumption": "UTC for naive input datetimes",
        }

    def as_dict(self) -> dict[str, str | bool | None]:
        """Backward-compatible alias for `to_json`."""

        return self.to_json()


def _normalise_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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

    source: Literal["cli", "none"] = "cli" if start is not None or end is not None else "none"
    return ProcessingWindow(parse_datetime_utc(start), parse_datetime_utc(end), source=source)
