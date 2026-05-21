"""Rover ephemeris extraction status reporting."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .stream import StreamRecord

EPHEMERIS_TYPES = ("GPSEPHA", "GLOEPHA", "GALEPHA", "BDSEPHA", "BD3EPHA", "QZSSEPHA")


@dataclass
class NavExtractionReport:
    found: dict[str, int]
    converted: dict[str, int]
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "converted": self.converted,
            "warnings": self.warnings,
        }


def extract_rover_nav(records: list[StreamRecord], output_path: Path | None = None) -> NavExtractionReport:
    counts = Counter(
        (record.msg_type or "").upper()
        for record in records
        if record.kind == "unicore_ascii" and (record.msg_type or "").upper() in EPHEMERIS_TYPES
    )
    converted = {name: 0 for name in EPHEMERIS_TYPES}
    warnings: list[str] = []
    if counts.get("GPSEPHA", 0):
        warnings.append(
            "GPSEPHA records found, but full UM980-to-RINEX NAV conversion is not implemented; "
            "no rover NAV file was written. Provide external NAV data before RTKLIB post-processing."
        )
        if output_path is not None and output_path.exists():
            output_path.unlink()
    for name in ("GLOEPHA", "GALEPHA", "BDSEPHA", "BD3EPHA", "QZSSEPHA"):
        if counts.get(name, 0):
            warnings.append(f"{name} records found; conversion not yet implemented")
    for name in EPHEMERIS_TYPES:
        if not counts.get(name, 0):
            warnings.append(f"{name} records missing")
    return NavExtractionReport(
        found={name: counts.get(name, 0) for name in EPHEMERIS_TYPES},
        converted=converted,
        warnings=warnings,
    )
