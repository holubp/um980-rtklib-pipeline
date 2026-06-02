"""Streaming RTKLIB trace diagnostics.

The parser intentionally extracts only bounded aggregate evidence from RTKLIB
trace files.  Trace formats vary by RTKLIB version and trace level, so unknown
lines are ignored and ambiguous numeric fields are not interpreted.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Iterable

TRACE_COUNTERS = (
    "ar_ratio_lines",
    "ambiguity_fix_lines",
    "ambiguity_hold_lines",
    "ambiguity_reset_lines",
    "lambda_lines",
    "cycle_slip_lines",
    "lli_lines",
    "lock_reset_lines",
    "observation_rejection_lines",
    "residual_outlier_lines",
    "no_common_satellite_lines",
    "missing_observation_lines",
    "missing_ephemeris_lines",
    "base_rover_time_issue_lines",
    "interpolation_lines",
    "filter_reset_lines",
    "warning_or_error_lines",
)
MAX_TRACE_EVENT_TIME_BUCKETS = 50_000

_RATIO_RE = re.compile(r"\bratio\s*(?:=|:)\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
_THRESHOLD_RE = re.compile(r"\b(?:thres|threshold)\s*(?:=|:)\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
_DT_RE = re.compile(r"\bdt\s*=\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)
_SAT_RE = re.compile(r"\b([GRECJIS]\d{2})\b")
_OBS_RE = re.compile(r"\b([CLDS]\d[A-Z]?)\b")
_TIMESTAMP_RE = re.compile(r"\b(\d{4})[/-](\d{2})[/-](\d{2})[ T](\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)")

_CATEGORY_TO_EVENT = {
    "ar_ratio_lines": "ar_ratio",
    "ambiguity_fix_lines": "ambiguity_fix",
    "ambiguity_hold_lines": "ambiguity_hold",
    "ambiguity_reset_lines": "ambiguity_reset",
    "lambda_lines": "lambda",
    "cycle_slip_lines": "cycle_slip",
    "lli_lines": "lli",
    "lock_reset_lines": "lock_reset",
    "observation_rejection_lines": "observation_rejection",
    "residual_outlier_lines": "residual_outlier",
    "no_common_satellite_lines": "no_common_satellite",
    "missing_observation_lines": "missing_observation",
    "missing_ephemeris_lines": "missing_ephemeris",
    "base_rover_time_issue_lines": "base_rover_time_issue",
    "interpolation_lines": "interpolation",
    "filter_reset_lines": "filter_reset",
    "warning_or_error_lines": "warning_or_error",
}


def analyze_rtklib_trace(path: Path, *, max_example_lines: int = 20, max_bytes: int = 0) -> dict[str, object]:
    """Return bounded aggregate diagnostics for an RTKLIB trace file.

    Args:
        path: RTKLIB trace file.
        max_example_lines: Maximum stored example lines per category.
        max_bytes: Optional maximum bytes to parse. Zero means parse the full
            file with streaming reads.

    Returns:
        Stable JSON-compatible trace summary.
    """

    counters = {name: 0 for name in TRACE_COUNTERS}
    examples: dict[str, list[str]] = {}
    ratios: list[float] = []
    lines_read = 0
    raw_bytes_read = 0
    decoded_chars_read = 0
    truncated = False
    parser_warnings: list[str] = []
    event_counts: dict[str, int] = {}
    events_by_time: dict[str, dict[str, object]] = {}
    top_satellites: dict[str, int] = {}
    top_systems: dict[str, int] = {}
    top_observables: dict[str, int] = {}
    started = time.perf_counter()
    try:
        stat = path.stat()
    except OSError as exc:
        return {
            "available": False,
            "source": None,
            "generated_temporarily": False,
            "retained": None,
            "path": str(path),
            "bytes_read": 0,
            "lines_read": 0,
            "parser_warnings": [f"trace file unavailable: {exc}"],
            "counters": counters,
            "numeric": {"ar_ratio": _ratio_summary([])},
            "examples": {},
        }

    with path.open("rb") as handle:
        for raw_bytes in handle:
            raw_len = len(raw_bytes)
            if max_bytes > 0 and raw_bytes_read + raw_len > max_bytes:
                remaining = max_bytes - raw_bytes_read
                if remaining > 0:
                    raw_bytes = raw_bytes[:remaining]
                    raw_bytes_read += len(raw_bytes)
                    raw_line = raw_bytes.decode("ascii", errors="ignore")
                    decoded_chars_read += len(raw_line)
                    if raw_line:
                        lines_read += 1
                        line = raw_line.strip()
                        if line:
                            _process_line(
                                line,
                                counters,
                                examples,
                                ratios,
                                event_counts,
                                events_by_time,
                                top_satellites,
                                top_systems,
                                top_observables,
                                max_example_lines,
                            )
                truncated = True
                break
            raw_bytes_read += raw_len
            raw_line = raw_bytes.decode("ascii", errors="ignore")
            decoded_chars_read += len(raw_line)
            lines_read += 1
            line = raw_line.strip()
            if not line:
                continue
            _process_line(
                line,
                counters,
                examples,
                ratios,
                event_counts,
                events_by_time,
                top_satellites,
                top_systems,
                top_observables,
                max_example_lines,
            )

    elapsed = time.perf_counter() - started
    if not events_by_time:
        parser_warnings.append("Trace events counted globally but no timestamps were recognised for solution-epoch alignment.")
    elif len(events_by_time) >= MAX_TRACE_EVENT_TIME_BUCKETS:
        parser_warnings.append(
            f"Trace timestamp aggregation capped at {MAX_TRACE_EVENT_TIME_BUCKETS} unique event times; global counters remain complete."
        )
    if truncated:
        parser_warnings.append(f"Trace parsing stopped at --quality-trace-max-bytes={max_bytes}.")
    return {
        "available": True,
        "source": None,
        "generated_temporarily": False,
        "retained": None,
        "path": str(path),
        "trace_file_size_bytes": stat.st_size,
        "trace_raw_bytes_read": raw_bytes_read,
        "trace_decoded_chars_read": decoded_chars_read,
        "trace_bytes_read": raw_bytes_read,
        "trace_lines_read": lines_read,
        "trace_truncated": truncated,
        "trace_parse_elapsed_s": elapsed,
        "trace_parse_rate_mb_s": (raw_bytes_read / 1_000_000.0 / elapsed) if elapsed > 0 else None,
        "bytes_read": raw_bytes_read,
        "lines_read": lines_read,
        "parser_warnings": parser_warnings,
        "counters": counters,
        "numeric": {"ar_ratio": _ratio_summary(ratios)},
        "examples": examples,
        "events": {
            "counts_by_type": event_counts,
            "top_satellites": _top_counts(top_satellites),
            "top_systems": _top_counts(top_systems),
            "top_observables": _top_counts(top_observables),
            "timestamped_event_times": len(events_by_time),
            "event_time_aggregates": list(events_by_time.values()),
        },
    }


def _process_line(
    line: str,
    counters: dict[str, int],
    examples: dict[str, list[str]],
    ratios: list[float],
    event_counts: dict[str, int],
    events_by_time: dict[str, dict[str, object]],
    top_satellites: dict[str, int],
    top_systems: dict[str, int],
    top_observables: dict[str, int],
    max_example_lines: int,
) -> None:
    """Classify one decoded trace line and update bounded aggregates."""

    lower = line.lower()
    matched = _classify_line(lower)
    ratio_values = list(_extract_ratios(line))
    if ratio_values:
        matched.add("ar_ratio_lines")
    timestamp = _extract_timestamp(line)
    sat = _extract_satellite(line)
    observable = _extract_observable(line)
    threshold = _first_float(_THRESHOLD_RE, line)
    dt = _first_float(_DT_RE, line)
    for category in matched:
        counters[category] += 1
        _add_example(examples, category, line, max_example_lines)
        event_type = _CATEGORY_TO_EVENT.get(category, category)
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if sat:
            top_satellites[sat] = top_satellites.get(sat, 0) + 1
            top_systems[sat[0]] = top_systems.get(sat[0], 0) + 1
        if observable:
            top_observables[observable] = top_observables.get(observable, 0) + 1
        if timestamp is not None and (timestamp.isoformat() in events_by_time or len(events_by_time) < MAX_TRACE_EVENT_TIME_BUCKETS):
            bucket = events_by_time.setdefault(
                timestamp.isoformat(),
                {
                    "time": timestamp.isoformat(),
                    "counts": {},
                    "sats": {},
                    "observables": {},
                    "ar_ratio_min": None,
                    "ar_ratio_max": None,
                    "ar_threshold": None,
                    "base_rover_dt_s": None,
                    "examples": [],
                },
            )
            counts = bucket["counts"]
            if isinstance(counts, dict):
                counts[event_type] = int(counts.get(event_type, 0)) + 1
            sats = bucket["sats"]
            if sat and isinstance(sats, dict):
                sats[sat] = int(sats.get(sat, 0)) + 1
            observables = bucket["observables"]
            if observable and isinstance(observables, dict):
                observables[observable] = int(observables.get(observable, 0)) + 1
            examples_bucket = bucket["examples"]
            if isinstance(examples_bucket, list) and len(examples_bucket) < 3:
                examples_bucket.append(line[:240])
            if threshold is not None:
                bucket["ar_threshold"] = threshold
            if dt is not None:
                bucket["base_rover_dt_s"] = dt
    for value in ratio_values:
        ratios.append(value)
        if timestamp is not None:
            bucket = events_by_time.get(timestamp.isoformat())
            if bucket is not None:
                current_min = bucket.get("ar_ratio_min")
                current_max = bucket.get("ar_ratio_max")
                bucket["ar_ratio_min"] = value if current_min is None else min(float(current_min), value)
                bucket["ar_ratio_max"] = value if current_max is None else max(float(current_max), value)


def _classify_line(lower: str) -> set[str]:
    categories: set[str] = set()
    if "lambda" in lower:
        categories.add("lambda_lines")
    if any(token in lower for token in ("resamb", "ambiguity", "amb ")):
        if "fix" in lower:
            categories.add("ambiguity_fix_lines")
        if "hold" in lower:
            categories.add("ambiguity_hold_lines")
        if "reset" in lower or "rej" in lower:
            categories.add("ambiguity_reset_lines")
    if "cycle slip" in lower or "slip" in lower:
        categories.add("cycle_slip_lines")
    if "lli" in lower:
        categories.add("lli_lines")
    if "lock" in lower and ("reset" in lower or "outc" in lower):
        categories.add("lock_reset_lines")
    if "reject" in lower or "rejected" in lower or "rejc" in lower:
        categories.add("observation_rejection_lines")
    if "outlier" in lower or "large residual" in lower or "innovation" in lower or "postfit" in lower or "prefit" in lower:
        categories.add("residual_outlier_lines")
    if "no common" in lower:
        categories.add("no_common_satellite_lines")
    if "no obs" in lower or "missing observation" in lower:
        categories.add("missing_observation_lines")
    if "no ephemeris" in lower or "no eph" in lower or "missing ephemeris" in lower:
        categories.add("missing_ephemeris_lines")
    if "time difference" in lower or "dt=" in lower or ("base" in lower and "time" in lower):
        categories.add("base_rover_time_issue_lines")
    if "interpolate" in lower or "interpolation" in lower:
        categories.add("interpolation_lines")
    if "filter" in lower and ("reset" in lower or "state" in lower or "covariance" in lower or "solq" in lower or "q=" in lower):
        categories.add("filter_reset_lines")
    if "warning" in lower or "error" in lower:
        categories.add("warning_or_error_lines")
    return categories


def _extract_ratios(line: str) -> Iterable[float]:
    for match in _RATIO_RE.finditer(line):
        try:
            yield float(match.group(1))
        except ValueError:
            continue


def _extract_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(line)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        second_float = float(second)
        whole_second = int(second_float)
        microsecond = int(round((second_float - whole_second) * 1_000_000))
        if microsecond >= 1_000_000:
            whole_second += 1
            microsecond = 0
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            whole_second,
            microsecond,
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _extract_satellite(line: str) -> str | None:
    match = _SAT_RE.search(line)
    return match.group(1) if match else None


def _extract_observable(line: str) -> str | None:
    match = _OBS_RE.search(line)
    return match.group(1) if match else None


def _first_float(pattern: re.Pattern[str], line: str) -> float | None:
    match = pattern.search(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _ratio_summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p05": None,
            "median": None,
            "p95": None,
            "max": None,
            "lt_3_0": 0,
            "gte_3_0_lt_3_5": 0,
        }
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0],
        "p05": _percentile(ordered, 0.05),
        "median": median(ordered),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1],
        "lt_3_0": sum(1 for value in values if value < 3.0),
        "gte_3_0_lt_3_5": sum(1 for value in values if 3.0 <= value < 3.5),
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = fraction * (len(ordered) - 1)
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    weight = index - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _add_example(examples: dict[str, list[str]], category: str, line: str, max_example_lines: int) -> None:
    bucket = examples.setdefault(category, [])
    if len(bucket) < max_example_lines:
        bucket.append(line[:240])


def _top_counts(values: dict[str, int], *, limit: int = 10) -> list[dict[str, object]]:
    return [{"key": key, "count": count} for key, count in sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]]
