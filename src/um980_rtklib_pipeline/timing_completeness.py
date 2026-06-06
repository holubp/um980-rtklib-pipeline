"""Timing-completeness metrics for UM980 capture validation.

This module turns parsed receiver stream records into per-message cadence
metrics.  It is deliberately conservative: messages without receiver
timestamps are reported as unsupported instead of being treated as complete.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .capture_profiles import CaptureProfile, parse_capture_profile
from .nmea import float_or_none, parse_hhmmss, parse_sentence, sentence_type
from .stream import StreamRecord


NMEA_TIME_FIELD_INDEX = {
    "GGA": 0,
    "GLL": 4,
    "GNS": 0,
    "GRS": 0,
    "GST": 0,
    "RMC": 0,
}
NMEA_CONTEXT_TIMED_TYPES = {"GSA", "GSV", "VTG"}
SUPPORTED_UNICORE_ASCII_TIMING = {
    "ADRNAVA",
    "BESTNAVA",
    "GPSIONA",
    "PPPNAVA",
    "TROPINFOA",
}
SUPPORTED_BINARY_HEADER_TIMING = {
    "ADRNAVB",
    "BESTNAVB",
    "GPSIONB",
    "OBSVMB",
    "OBSVMCMPB",
    "PPPNAVB",
    "TROPINFOB",
}
CONTROL_COMMANDS = {"CONFIG", "MODE", "UNLOG", "VERSIONB"}
DEFAULT_MIN_INTERVALS_FOR_HIGH_CONFIDENCE = 5


@dataclass(frozen=True)
class TimingEvent:
    """One receiver-timed observation from a stream record."""

    message_family: str
    message_name: str
    receiver_time_s: float | None
    receiver_time_source: str
    host_offset_s: float | None
    raw_offset: int | None
    constellation: str | None
    sentence_index: int | None
    sentence_total: int | None
    valid_checksum_or_crc: bool | None


@dataclass(frozen=True)
class TimingExpectation:
    """Expected cadence for one receiver message."""

    message_name: str
    expected_rate_hz: float | None
    expected_interval_s: float | None
    expectation_source: str
    periodic: bool
    event_driven: bool
    timing_supported: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_rate_hz": self.expected_rate_hz,
            "expected_interval_s": self.expected_interval_s,
            "expectation_source": self.expectation_source,
            "periodic": self.periodic,
            "event_driven": self.event_driven,
            "timing_supported": self.timing_supported,
        }


@dataclass
class TimingCompletenessResult:
    """Complete timing summary for a capture/profile pair."""

    profile_name: str | None
    profile_family: str | None
    messages: dict[str, dict[str, object]]
    overall_timing_passed: bool
    overall_timing_confidence: str
    overall_timing_status: str
    timing_summary_flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "profile_family": self.profile_family,
            "messages": self.messages,
            "overall_timing_passed": self.overall_timing_passed,
            "overall_timing_confidence": self.overall_timing_confidence,
            "overall_timing_status": self.overall_timing_status,
            "timing_summary_flags": self.timing_summary_flags,
        }


def analyze_timing_completeness(
    records: list[StreamRecord],
    *,
    profile: CaptureProfile | None = None,
    profile_path: Path | None = None,
    expected_messages: Iterable[str] = (),
    capture_duration_s: float | None = None,
) -> TimingCompletenessResult:
    """Compute per-message timing completeness for parsed stream records.

    Args:
        records: Parsed UM980 stream records.
        profile: Optional parsed capture profile.
        profile_path: Optional profile path to parse when ``profile`` is not
            supplied.
        expected_messages: Expected message names from the caller.
        capture_duration_s: Optional host capture duration used only for
            confidence notes; receiver timestamps remain the primary source.

    Returns:
        Timing completeness summary with stable JSON-compatible message fields.
    """

    if profile is None and profile_path is not None:
        profile = parse_capture_profile(profile_path)
    expectations = expected_rate_model(profile=profile, expected_messages=expected_messages)
    events = extract_timing_events(records)
    events_by_name: dict[str, list[TimingEvent]] = defaultdict(list)
    for event in events:
        events_by_name[event.message_name].append(event)
        short = _short_message_name(event.message_name)
        if short != event.message_name:
            events_by_name[short].append(event)

    message_names = sorted(set(expectations) | {event.message_name for event in events})
    messages: dict[str, dict[str, object]] = {}
    statuses: list[str] = []
    confidences: list[str] = []
    flags: list[str] = []
    for name in message_names:
        expectation = expectations.get(name) or TimingExpectation(
            message_name=name,
            expected_rate_hz=None,
            expected_interval_s=None,
            expectation_source="observed",
            periodic=False,
            event_driven=False,
            timing_supported=bool(events_by_name.get(name)),
        )
        evs = events_by_name.get(name, [])
        metric = _message_metrics(name, evs, expectation, capture_duration_s=capture_duration_s)
        messages[name] = metric
        statuses.append(str(metric["timing_status"]))
        confidences.append(str(metric["confidence"]))
        for error in metric.get("errors", []):  # type: ignore[union-attr]
            flags.append(f"{name}: {error}")
        for warning in metric.get("warnings", []):  # type: ignore[union-attr]
            flags.append(f"{name}: {warning}")

    overall_status = _overall_status(statuses)
    overall_confidence = _overall_confidence(confidences)
    return TimingCompletenessResult(
        profile_name=profile.path.stem if profile else None,
        profile_family=profile.metadata.get("family") if profile else None,
        messages=messages,
        overall_timing_passed=overall_status in {"pass", "not_applicable"},
        overall_timing_confidence=overall_confidence,
        overall_timing_status=overall_status,
        timing_summary_flags=list(dict.fromkeys(flags))[:50],
    )


def expected_rate_model(
    *,
    profile: CaptureProfile | None = None,
    expected_messages: Iterable[str] = (),
) -> dict[str, TimingExpectation]:
    """Build expected-rate declarations from profile metadata and commands."""

    expectations: dict[str, TimingExpectation] = {}
    for raw_name in expected_messages:
        name = raw_name.strip()
        if not name:
            continue
        expectations.setdefault(
            name,
            TimingExpectation(name, None, None, "unknown", periodic=False, event_driven=False),
        )
    if profile is None:
        return expectations

    for name, rate in _parse_expected_rate_metadata(profile.metadata.get("expected_rate_hz")).items():
        expectations[name] = TimingExpectation(
            message_name=name,
            expected_rate_hz=rate,
            expected_interval_s=1.0 / rate if rate > 0 else None,
            expectation_source="profile_metadata",
            periodic=rate > 0,
            event_driven=False,
        )
    event_driven = set(_csv_list(profile.metadata.get("event_driven")))
    for name in event_driven:
        expectations[name] = TimingExpectation(
            message_name=name,
            expected_rate_hz=None,
            expected_interval_s=None,
            expectation_source="profile_metadata",
            periodic=False,
            event_driven=True,
        )
    for command in profile.commands:
        inferred = _expectation_from_command(command)
        if inferred is None:
            continue
        if inferred.message_name in expectations and expectations[inferred.message_name].expectation_source == "profile_metadata":
            continue
        expectations[inferred.message_name] = inferred
    return expectations


def extract_timing_events(records: list[StreamRecord]) -> list[TimingEvent]:
    """Extract receiver timestamp observations from parsed stream records."""

    events: list[TimingEvent] = []
    last_nmea_time: float | None = None
    last_nmea_raw_offset: int | None = None
    for record in records:
        if record.kind == "nmea" and record.text:
            event = _nmea_timing_event(record, last_nmea_time=last_nmea_time)
            if event:
                if event.receiver_time_s is not None:
                    last_nmea_time = event.receiver_time_s
                    last_nmea_raw_offset = record.offset
                elif (
                    event.message_name[-3:] in NMEA_CONTEXT_TIMED_TYPES
                    and last_nmea_time is not None
                    and last_nmea_raw_offset is not None
                    and record.offset - last_nmea_raw_offset < 4096
                ):
                    event = TimingEvent(
                        **{
                            **event.__dict__,
                            "receiver_time_s": last_nmea_time,
                            "receiver_time_source": "nmea_context",
                        }
                    )
                events.append(event)
            continue
        if record.kind == "unicore_ascii" and record.text:
            event = _unicore_ascii_timing_event(record)
            if event:
                events.append(event)
            continue
        if record.kind == "unicore_binary":
            event = _binary_timing_event(record)
            if event:
                events.append(event)
    return events


def _message_metrics(
    name: str,
    events: list[TimingEvent],
    expectation: TimingExpectation,
    *,
    capture_duration_s: float | None,
) -> dict[str, object]:
    timestamps = [event.receiver_time_s for event in events if event.receiver_time_s is not None]
    timing_supported = bool(timestamps)
    base = expectation.as_dict()
    base["timing_supported"] = timing_supported
    base.update(
        {
            "confidence": "unsupported",
            "observed_count": len(events),
            "first_receiver_time_s": min(timestamps) if timestamps else None,
            "last_receiver_time_s": max(timestamps) if timestamps else None,
            "receiver_time_span_s": (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0.0 if timestamps else None,
            "observed_rate_hz": None,
            "expected_count_min": None,
            "expected_count_max": None,
            "missing_epoch_count": None,
            "missing_epoch_rate": None,
            "duplicate_epoch_count": None,
            "duplicate_epoch_rate": None,
            "max_receiver_time_gap_s": None,
            "p95_receiver_time_gap_s": None,
            "median_receiver_time_gap_s": None,
            "jitter_s_median_abs_dev": None,
            "timing_passed": True,
            "timing_status": "not_applicable",
            "warnings": [],
            "errors": [],
        }
    )
    if expectation.event_driven:
        base["confidence"] = "high" if timing_supported else "medium"
        base["timing_status"] = "pass"
        return base
    if not expectation.periodic:
        if events and not timing_supported:
            base["confidence"] = "unsupported"
            base["timing_status"] = "unsupported"
            base["warnings"] = ["message observed but receiver timing is unsupported"]
        elif events:
            base["confidence"] = "low"
            base["timing_status"] = "pass"
        return base
    if expectation.expected_interval_s is None or expectation.expected_interval_s <= 0:
        base["confidence"] = "unsupported" if not timing_supported else "low"
        base["timing_status"] = "unsupported"
        base["warnings"] = ["periodic expectation has no known interval"]
        return base
    if not timing_supported:
        base["confidence"] = "unsupported"
        base["timing_status"] = "unsupported"
        base["timing_passed"] = False
        base["errors"] = ["periodic message has no supported receiver timestamps"]
        return base

    if name.endswith("GSV") or name == "GSV":
        gsv_extras = _gsv_extras(events, expectation.expected_interval_s)
        base.update(gsv_extras)
        timestamps = list(gsv_extras.get("_gsv_group_times", []))  # type: ignore[arg-type]

    unique_times, duplicate_count = _unique_times(timestamps, expectation.expected_interval_s)
    if name.endswith("GSA") or name == "GSA":
        duplicate_count = 0
    intervals = [right - left for left, right in zip(unique_times, unique_times[1:]) if right > left]
    span = unique_times[-1] - unique_times[0] if len(unique_times) >= 2 else 0.0
    expected_count = _expected_count(unique_times, expectation.expected_interval_s)
    missing = max(0, expected_count - len(unique_times))
    missing_rate = missing / expected_count if expected_count else 0.0
    duplicate_rate = duplicate_count / len(timestamps) if timestamps else 0.0
    observed_rate = (len(unique_times) - 1) / span if span > 0 and len(unique_times) >= 2 else None
    max_gap = max(intervals) if intervals else None
    median_gap = statistics.median(intervals) if intervals else None
    p95_gap = _percentile(intervals, 95) if intervals else None
    jitter = _median_abs_deviation(intervals, expectation.expected_interval_s) if intervals else None
    errors: list[str] = []
    warnings: list[str] = []
    if missing_rate > _missing_rate_limit(expectation.expected_interval_s):
        message = f"missing epoch rate {missing_rate:.4f} exceeds limit"
        if missing_rate > 0.01 or expectation.expected_interval_s >= 1.0:
            errors.append(message)
        else:
            warnings.append(message)
    if duplicate_rate > 0.001:
        warnings.append(f"duplicate epoch rate {duplicate_rate:.4f} exceeds limit")
    if max_gap is not None and max_gap > expectation.expected_interval_s * 3.05:
        errors.append(f"max receiver gap {max_gap:.3f}s exceeds 3x expected interval")
    elif max_gap is not None and max_gap > expectation.expected_interval_s * 2.05:
        warnings.append(f"max receiver gap {max_gap:.3f}s exceeds 2x expected interval")
    gsv_incomplete_rate = base.get("incomplete_gsv_group_rate")
    if isinstance(gsv_incomplete_rate, float) and gsv_incomplete_rate > 0:
        errors.append(f"incomplete GSV group rate {gsv_incomplete_rate:.4f} exceeds limit")

    confidence = _confidence(unique_count=len(unique_times), interval=expectation.expected_interval_s, span=span, capture_duration_s=capture_duration_s)
    status = "fail" if errors else "marginal" if warnings or confidence in {"low", "unsupported"} else "pass"
    base.update(
        {
            "confidence": confidence,
            "observed_count": len(events),
            "observed_epoch_count": len(unique_times),
            "receiver_time_span_s": span,
            "observed_rate_hz": observed_rate,
            "expected_count_min": max(0, expected_count - 1),
            "expected_count_max": expected_count + 1,
            "missing_epoch_count": missing,
            "missing_epoch_rate": missing_rate,
            "duplicate_epoch_count": duplicate_count,
            "duplicate_epoch_rate": duplicate_rate,
            "max_receiver_time_gap_s": max_gap,
            "p95_receiver_time_gap_s": p95_gap,
            "median_receiver_time_gap_s": median_gap,
            "jitter_s_median_abs_dev": jitter,
            "timing_passed": not errors,
            "timing_status": status,
            "warnings": warnings,
            "errors": errors,
        }
    )
    base.pop("_gsv_group_times", None)
    return base


def _nmea_timing_event(record: StreamRecord, *, last_nmea_time: float | None) -> TimingEvent | None:
    parsed = parse_sentence(record.text or "", record.checksum_ok)
    if parsed is None:
        return None
    msg_type = sentence_type(parsed.talker_type)
    receiver_time_s = None
    source = "unsupported"
    if parsed.talker_type in {"PPPNAVA", "ADRNAVA"}:
        week_tow = _header_week_tow([parsed.talker_type, *parsed.fields])
        if week_tow:
            receiver_time_s = _week_tow_to_seconds(*week_tow)
            source = "proprietary_nmea_week_tow"
    elif msg_type in NMEA_TIME_FIELD_INDEX and len(parsed.fields) > NMEA_TIME_FIELD_INDEX[msg_type]:
        receiver_time_s = _seconds_of_day(parsed.fields[NMEA_TIME_FIELD_INDEX[msg_type]])
        source = "nmea_time_of_day" if receiver_time_s is not None else "unsupported"
    elif msg_type in NMEA_CONTEXT_TIMED_TYPES and last_nmea_time is not None:
        receiver_time_s = last_nmea_time
        source = "nmea_context"
    sentence_total = sentence_index = None
    if msg_type == "GSV" and len(parsed.fields) >= 2:
        sentence_total = _int_text(parsed.fields[0])
        sentence_index = _int_text(parsed.fields[1])
    return TimingEvent(
        message_family="nmea",
        message_name=parsed.talker_type,
        receiver_time_s=receiver_time_s,
        receiver_time_source=source,
        host_offset_s=None,
        raw_offset=record.offset,
        constellation=parsed.talker_type[:2] if len(parsed.talker_type) >= 5 else None,
        sentence_index=sentence_index,
        sentence_total=sentence_total,
        valid_checksum_or_crc=record.checksum_ok,
    )


def _unicore_ascii_timing_event(record: StreamRecord) -> TimingEvent | None:
    if record.msg_type not in SUPPORTED_UNICORE_ASCII_TIMING:
        return None
    body = (record.text or "")[1:].split("*", 1)[0]
    header_text = body.split(";", 1)[0]
    header = next(csv.reader([header_text], skipinitialspace=True))
    week_tow = _header_week_tow(header)
    return TimingEvent(
        message_family="unicore_ascii",
        message_name=record.msg_type or header[0],
        receiver_time_s=_week_tow_to_seconds(*week_tow) if week_tow else None,
        receiver_time_source="unicore_ascii_header" if week_tow else "unsupported",
        host_offset_s=None,
        raw_offset=record.offset,
        constellation=None,
        sentence_index=None,
        sentence_total=None,
        valid_checksum_or_crc=record.checksum_ok,
    )


def _binary_timing_event(record: StreamRecord) -> TimingEvent | None:
    if not record.msg_type or (record.msg_type not in SUPPORTED_BINARY_HEADER_TIMING and record.msg_type.startswith("binary:")):
        return None
    if len(record.raw) < 16:
        receiver_time = None
        source = "unsupported"
    else:
        time_status = record.raw[9]
        week = int.from_bytes(record.raw[10:12], "little", signed=False)
        tow_ms = int.from_bytes(record.raw[12:16], "little", signed=False)
        if time_status == 0 or week == 0:
            receiver_time = None
            source = "unsupported"
        else:
            receiver_time = _week_tow_to_seconds(week, tow_ms / 1000.0)
            source = "unicore_binary_header"
    return TimingEvent(
        message_family="unicore_binary",
        message_name=record.msg_type,
        receiver_time_s=receiver_time,
        receiver_time_source=source,
        host_offset_s=None,
        raw_offset=record.offset,
        constellation=None,
        sentence_index=None,
        sentence_total=None,
        valid_checksum_or_crc=True,
    )


def _expectation_from_command(command: str) -> TimingExpectation | None:
    tokens = command.strip().split()
    if not tokens:
        return None
    name = tokens[0].strip().upper()
    if name in CONTROL_COMMANDS:
        return None
    if any(token.upper() == "ONCHANGED" for token in tokens[1:]):
        return TimingExpectation(name, None, None, "profile_command", periodic=False, event_driven=True)
    interval = None
    for token in reversed(tokens[1:]):
        interval = _float_text(token)
        if interval is not None:
            break
    if interval is None or interval <= 0:
        return TimingExpectation(name, None, None, "unknown", periodic=False, event_driven=False)
    return TimingExpectation(
        name,
        1.0 / interval,
        interval,
        "profile_command",
        periodic=True,
        event_driven=False,
    )


def _parse_expected_rate_metadata(text: str | None) -> dict[str, float]:
    rates: dict[str, float] = {}
    for item in _csv_list(text):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        rate = _float_text(value)
        if rate is not None and rate > 0:
            rates[name.strip().upper()] = rate
    return rates


def _csv_list(text: str | None) -> list[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def _seconds_of_day(text: str) -> float | None:
    parsed = parse_hhmmss(text)
    if parsed is None:
        return None
    hour, minute, second = parsed
    return hour * 3600.0 + minute * 60.0 + second


def _header_week_tow(header: list[str]) -> tuple[int, float] | None:
    for index, token in enumerate(header[:-1]):
        week = _int_text(token)
        tow = _float_text(header[index + 1])
        if week is None or tow is None:
            continue
        if 1024 <= week <= 4096 and 0 <= tow <= 604800000:
            return week, tow / 1000.0 if tow > 604800 else tow
    return None


def _week_tow_to_seconds(week: int, tow: float) -> float:
    return week * 604800.0 + tow


def _int_text(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _float_text(text: str) -> float | None:
    return float_or_none(text)


def _short_message_name(name: str) -> str:
    return name[-3:] if len(name) == 5 and name[-3:].isalpha() else name


def _unique_times(timestamps: list[float], interval: float) -> tuple[list[float], int]:
    if not timestamps:
        return [], 0
    ordered = sorted(timestamps)
    tolerance = max(interval * 0.35, 1e-6)
    groups: list[list[float]] = []
    for value in ordered:
        if not groups or abs(value - groups[-1][-1]) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [statistics.median(group) for group in groups], sum(max(0, len(group) - 1) for group in groups)


def _expected_count(unique_times: list[float], interval: float) -> int:
    if not unique_times:
        return 0
    if len(unique_times) == 1:
        return 1
    span = unique_times[-1] - unique_times[0]
    return int(round(span / interval)) + 1


def _missing_rate_limit(interval: float) -> float:
    return 0.001 if interval <= 0.1 else 0.0


def _confidence(*, unique_count: int, interval: float, span: float, capture_duration_s: float | None) -> str:
    if unique_count <= 0:
        return "unsupported"
    if interval >= 10 and span < interval * 2:
        return "medium"
    if unique_count >= DEFAULT_MIN_INTERVALS_FOR_HIGH_CONFIDENCE + 1:
        return "high"
    if capture_duration_s is not None and capture_duration_s < interval * 2:
        return "medium"
    return "medium"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _median_abs_deviation(values: list[float], centre: float) -> float:
    return statistics.median(abs(value - centre) for value in values)


def _gsv_extras(events: list[TimingEvent], interval: float) -> dict[str, object]:
    groups: dict[float, list[TimingEvent]] = defaultdict(list)
    for event in events:
        if event.receiver_time_s is not None:
            key = round(event.receiver_time_s / max(interval, 1e-6)) * max(interval, 1e-6)
            groups[key].append(event)
    incomplete = 0
    duplicates = 0
    for group in groups.values():
        expected = max((event.sentence_total or 0) for event in group) or None
        observed_indexes = [event.sentence_index for event in group if event.sentence_index is not None]
        unique_indexes = set(observed_indexes)
        duplicates += max(0, len(observed_indexes) - len(unique_indexes))
        if expected is not None and len(unique_indexes) < expected:
            incomplete += 1
    group_times = sorted(groups)
    return {
        "gsv_sentence_count": len(events),
        "gsv_epoch_count": len(groups),
        "incomplete_gsv_groups": incomplete,
        "incomplete_gsv_group_rate": incomplete / len(groups) if groups else 0.0,
        "duplicate_gsv_sentence_count": duplicates,
        "_gsv_group_times": group_times,
    }


def _overall_status(statuses: list[str]) -> str:
    relevant = [status for status in statuses if status != "not_applicable"]
    if not relevant:
        return "not_applicable"
    if any(status == "fail" for status in relevant):
        return "fail"
    if any(status == "unsupported" for status in relevant):
        return "unsupported"
    if any(status == "marginal" for status in relevant):
        return "marginal"
    return "pass"


def _overall_confidence(confidences: list[str]) -> str:
    relevant = [confidence for confidence in confidences if confidence != "unsupported"]
    if not relevant:
        return "unsupported"
    if "low" in relevant:
        return "low"
    if "medium" in relevant:
        return "medium"
    return "high"
