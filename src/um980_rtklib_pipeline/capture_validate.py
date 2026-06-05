"""Lightweight structural validation for UM980 raw capture files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .solution import extract_solutions
from .um980_stream import parse_file


CaptureMode = Literal["passive", "ascii", "binary", "mixed"]


@dataclass(frozen=True)
class CaptureValidationResult:
    """Machine-readable structural validation result for a raw receiver stream."""

    path: Path
    suffix: str
    bytes_total: int
    ascii_bytes_estimated: int | None
    binary_bytes_estimated: int | None
    nmea_records: int
    nmea_checksum_ok: int
    nmea_checksum_bad: int
    unicore_ascii_records: int
    unicore_binary_frames: int
    binary_crc_ok: int | None
    binary_crc_bad: int | None
    unknown_bytes: int | None
    first_timestamp: str | None
    last_timestamp: str | None
    message_counts: dict[str, int]
    expected_messages_missing: list[str]
    mode_expectation: str
    mode_expectation_passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""

        return {
            "path": str(self.path),
            "suffix": self.suffix,
            "bytes_total": self.bytes_total,
            "ascii_bytes_estimated": self.ascii_bytes_estimated,
            "binary_bytes_estimated": self.binary_bytes_estimated,
            "nmea_records": self.nmea_records,
            "nmea_checksum_ok": self.nmea_checksum_ok,
            "nmea_checksum_bad": self.nmea_checksum_bad,
            "unicore_ascii_records": self.unicore_ascii_records,
            "unicore_binary_frames": self.unicore_binary_frames,
            "binary_crc_ok": self.binary_crc_ok,
            "binary_crc_bad": self.binary_crc_bad,
            "unknown_bytes": self.unknown_bytes,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "message_counts": self.message_counts,
            "expected_messages_missing": self.expected_messages_missing,
            "mode_expectation": self.mode_expectation,
            "mode_expectation_passed": self.mode_expectation_passed,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def validate_capture_file(
    path: Path,
    *,
    expect_mode: CaptureMode = "passive",
    expected_messages: list[str] | None = None,
) -> CaptureValidationResult:
    """Validate a raw UM980 capture structurally without inferring protocol from suffix."""

    if expect_mode not in {"passive", "ascii", "binary", "mixed"}:
        raise ValueError(f"unsupported capture mode expectation: {expect_mode}")
    expected_messages = expected_messages or []
    errors: list[str] = []
    warnings: list[str] = []
    size = path.stat().st_size
    suffix = path.suffix.lower()
    if suffix not in {".unc", ".ubx"}:
        warnings.append(f"capture suffix {suffix or '<none>'!r} is accepted but not the preferred .unc or legacy .ubx suffix")
    if size == 0:
        errors.append("capture file is empty")
        return _empty_result(path, suffix, expect_mode, expected_messages, errors=errors, warnings=warnings)

    parsed = parse_file(path)
    diagnostics = parsed.diagnostics
    records = parsed.records
    ascii_bytes = sum(len(record.raw) for record in records if record.kind in {"nmea", "unicore_ascii", "command_response"})
    binary_bytes = sum(len(record.raw) for record in records if record.kind == "unicore_binary")
    message_counts: dict[str, int] = {}
    for name, count in diagnostics.nmea_types.items():
        message_counts[name] = message_counts.get(name, 0) + int(count)
        if len(name) == 5 and name[0].isalpha() and name[-3:].isalpha():
            short_name = name[-3:]
            message_counts[short_name] = message_counts.get(short_name, 0) + int(count)
    for name, count in diagnostics.unicore_types.items():
        message_counts[name] = int(count)
    expected_missing = [name for name in expected_messages if message_counts.get(name, 0) <= 0]
    if expected_missing:
        errors.append(f"expected messages missing: {', '.join(expected_missing)}")

    solutions = extract_solutions(records)
    timestamps = [point.time_utc.isoformat().replace("+00:00", "Z") for point in solutions.solution_points]
    mode_ok = _mode_passed(
        expect_mode,
        size=size,
        ascii_records=diagnostics.valid_nmea_records + diagnostics.unicore_ascii_records + diagnostics.command_response_records,
        binary_records=diagnostics.unicore_binary_records,
    )
    if not mode_ok:
        errors.append(f"capture does not satisfy expected {expect_mode!r} structural mode")

    return CaptureValidationResult(
        path=path,
        suffix=suffix,
        bytes_total=size,
        ascii_bytes_estimated=ascii_bytes,
        binary_bytes_estimated=binary_bytes,
        nmea_records=diagnostics.valid_nmea_records,
        nmea_checksum_ok=diagnostics.valid_nmea_records,
        nmea_checksum_bad=diagnostics.invalid_nmea_records,
        unicore_ascii_records=diagnostics.unicore_ascii_records,
        unicore_binary_frames=diagnostics.unicore_binary_records,
        binary_crc_ok=diagnostics.unicore_binary_records,
        binary_crc_bad=diagnostics.invalid_unicore_binary_records,
        unknown_bytes=diagnostics.noise_bytes,
        first_timestamp=timestamps[0] if timestamps else None,
        last_timestamp=timestamps[-1] if timestamps else None,
        message_counts=message_counts,
        expected_messages_missing=expected_missing,
        mode_expectation=expect_mode,
        mode_expectation_passed=mode_ok,
        warnings=warnings,
        errors=errors,
    )


def write_capture_validation_json(path: Path, result: CaptureValidationResult) -> None:
    """Write capture validation JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mode_passed(expect_mode: str, *, size: int, ascii_records: int, binary_records: int) -> bool:
    if size <= 0:
        return False
    if expect_mode == "passive":
        return True
    if expect_mode == "ascii":
        return ascii_records > 0 and binary_records == 0
    if expect_mode == "binary":
        return binary_records > 0 and ascii_records == 0
    if expect_mode == "mixed":
        return ascii_records > 0 and binary_records > 0
    return False


def _empty_result(
    path: Path,
    suffix: str,
    expect_mode: str,
    expected_messages: list[str],
    *,
    errors: list[str],
    warnings: list[str],
) -> CaptureValidationResult:
    return CaptureValidationResult(
        path=path,
        suffix=suffix,
        bytes_total=0,
        ascii_bytes_estimated=0,
        binary_bytes_estimated=0,
        nmea_records=0,
        nmea_checksum_ok=0,
        nmea_checksum_bad=0,
        unicore_ascii_records=0,
        unicore_binary_frames=0,
        binary_crc_ok=None,
        binary_crc_bad=None,
        unknown_bytes=0,
        first_timestamp=None,
        last_timestamp=None,
        message_counts={},
        expected_messages_missing=list(expected_messages),
        mode_expectation=expect_mode,
        mode_expectation_passed=False,
        warnings=warnings,
        errors=errors,
    )
