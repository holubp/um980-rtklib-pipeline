"""Diagnostic UM980 ION/UTC/TROPINFO record extraction.

The records in this module are preserved for analysis and future RINEX NAV
header enrichment. They are intentionally not fed to RTKLIB until each
message-family mapping is verified against RINEX and RTKLIB parser semantics.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from .stream import StreamRecord

DiagnosticKind = Literal["ionosphere", "utc", "troposphere"]

ION_MESSAGE_SYSTEMS = {
    "GPSIONA": "GPS",
    "BDSIONA": "BDS",
    "BD3IONA": "BD3",
    "GALIONA": "GAL",
    "GPSIONB": "GPS",
    "BDSIONB": "BDS",
    "BD3IONB": "BD3",
    "GALIONB": "GAL",
}
UTC_MESSAGE_SYSTEMS = {
    "GPSUTCA": "GPS",
    "BDSUTCA": "BDS",
    "BD3UTCA": "BD3",
    "GALUTCA": "GAL",
    "GPSUTCB": "GPS",
    "BDSUTCB": "BDS",
    "BD3UTCB": "BD3",
    "GALUTCB": "GAL",
}
TROPO_MESSAGES = {"TROPINFOA", "TROPINFOB"}


@dataclass(frozen=True)
class DiagnosticRecord:
    """One preserved UM980 diagnostic record."""

    kind: DiagnosticKind
    system: str | None
    source: str
    parameters: dict[str, float | int | str]
    raw_record_index: int | None
    converted_to_rinex: bool = False
    conversion_note: str = "present_not_converted: RINEX/RTKLIB mapping is not verified"


@dataclass
class DiagnosticExtraction:
    """Diagnostic records plus malformed and conversion counters."""

    records: list[DiagnosticRecord] = field(default_factory=list)
    present: Counter[str] = field(default_factory=Counter)
    malformed: Counter[str] = field(default_factory=Counter)
    present_not_converted: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly diagnostic details."""

        return {
            "present": dict(self.present),
            "malformed": dict(self.malformed),
            "present_not_converted": dict(self.present_not_converted),
            "records": [
                {
                    "kind": record.kind,
                    "system": record.system,
                    "source": record.source,
                    "parameters": record.parameters,
                    "raw_record_index": record.raw_record_index,
                    "converted_to_rinex": record.converted_to_rinex,
                    "conversion_note": record.conversion_note,
                }
                for record in self.records
            ],
        }


def extract_diagnostics(records: list[StreamRecord]) -> DiagnosticExtraction:
    """Extract ION/UTC/TROPINFO diagnostics without claiming RTKLIB conversion."""

    result = DiagnosticExtraction()
    for index, record in enumerate(records):
        msg = record.msg_type or ""
        kind, system = _classify(msg)
        if kind is None:
            continue
        result.present[msg] += 1
        result.present_not_converted[msg] += 1
        if record.kind == "unicore_ascii":
            try:
                parameters = _ascii_parameters(record.text or "")
            except ValueError:
                result.malformed[msg] += 1
                continue
        else:
            parameters = {"payload_bytes": max(len(record.raw) - 28, 0)}
        result.records.append(
            DiagnosticRecord(
                kind=kind,
                system=system,
                source=msg,
                parameters=parameters,
                raw_record_index=index,
            )
        )
    return result


def _classify(msg_type: str) -> tuple[DiagnosticKind | None, str | None]:
    if msg_type in ION_MESSAGE_SYSTEMS:
        return "ionosphere", ION_MESSAGE_SYSTEMS[msg_type]
    if msg_type in UTC_MESSAGE_SYSTEMS:
        return "utc", UTC_MESSAGE_SYSTEMS[msg_type]
    if msg_type in TROPO_MESSAGES:
        return "troposphere", None
    return None, None


def _ascii_parameters(text: str) -> dict[str, float | int | str]:
    body = text.strip()[1:].split("*", 1)[0]
    _, sep, payload = body.partition(";")
    if not sep:
        raise ValueError("diagnostic record has no payload separator")
    values = next(csv.reader([payload], skipinitialspace=True))
    return {f"field_{index}": _typed_value(value) for index, value in enumerate(values)}


def _typed_value(value: str) -> float | int | str:
    text = value.strip().strip('"')
    try:
        integer = int(text)
    except ValueError:
        pass
    else:
        return integer
    try:
        return float(text)
    except ValueError:
        return text
