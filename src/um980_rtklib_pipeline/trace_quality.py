"""Streaming RTKLIB trace diagnostics.

The parser intentionally extracts only bounded aggregate evidence from RTKLIB
trace files.  Trace formats vary by RTKLIB version and trace level, so unknown
lines are ignored and ambiguous numeric fields are not interpreted.
"""

from __future__ import annotations

import re
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

_RATIO_RE = re.compile(r"\bratio\s*(?:=|:)\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)


def analyze_rtklib_trace(path: Path, *, max_example_lines: int = 20) -> dict[str, object]:
    """Return bounded aggregate diagnostics for an RTKLIB trace file.

    Args:
        path: RTKLIB trace file.
        max_example_lines: Maximum stored example lines per category.

    Returns:
        Stable JSON-compatible trace summary.
    """

    counters = {name: 0 for name in TRACE_COUNTERS}
    examples: dict[str, list[str]] = {}
    ratios: list[float] = []
    lines_read = 0
    parser_warnings: list[str] = []
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

    with path.open("r", encoding="ascii", errors="ignore") as handle:
        for raw_line in handle:
            lines_read += 1
            line = raw_line.strip()
            if not line:
                continue
            lower = line.lower()
            matched = _classify_line(lower)
            for category in matched:
                counters[category] += 1
                _add_example(examples, category, line, max_example_lines)
            for value in _extract_ratios(line):
                ratios.append(value)
                counters["ar_ratio_lines"] += 1
                _add_example(examples, "ar_ratio_lines", line, max_example_lines)

    parser_warnings.append("Trace events counted globally but not time-aligned to solution epochs.")
    return {
        "available": True,
        "source": None,
        "generated_temporarily": False,
        "retained": None,
        "path": str(path),
        "bytes_read": stat.st_size,
        "lines_read": lines_read,
        "parser_warnings": parser_warnings,
        "counters": counters,
        "numeric": {"ar_ratio": _ratio_summary(ratios)},
        "examples": examples,
    }


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
