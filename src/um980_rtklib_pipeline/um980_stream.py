"""Public UM980 mixed-stream parser API.

This module provides a stable, documented facade over the byte-level parser in
``stream.py``.  It is intentionally small: semantic decoders still live in the
observation, navigation, diagnostic, and solution modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Literal

from .stream import StreamDiagnostics, StreamRecord, parse_stream


@dataclass(frozen=True)
class ParseOptions:
    """Options controlling mixed-stream parsing."""

    progress: bool = False


@dataclass(frozen=True)
class ParseWarning:
    """Machine-readable parser warning.

    Attributes:
        severity: Warning severity.
        family: Record family such as ``nmea`` or ``unicore_binary``.
        reason: Short reason code.
        offset_start: First byte offset for the rejected candidate.
        offset_end: Optional last byte offset.
        msg_type: Message type when known.
        sample_hex: Bounded hexadecimal sample.
        sample_text: Bounded text sample.
        recovery_action: Parser recovery action.
    """

    severity: Literal["info", "warning", "error"]
    family: str
    reason: str
    offset_start: int
    offset_end: int | None = None
    msg_type: str | None = None
    sample_hex: str | None = None
    sample_text: str | None = None
    recovery_action: str = "resynchronised"


@dataclass(frozen=True)
class ParseError:
    """Reserved public parse-error shape for future fail-fast modes."""

    severity: Literal["error"]
    family: str
    reason: str
    offset_start: int
    offset_end: int | None = None
    msg_type: str | None = None
    sample_hex: str | None = None
    sample_text: str | None = None
    recovery_action: str = "stopped"


@dataclass(frozen=True)
class ParseResult:
    """Parsed UM980 stream with diagnostics."""

    records: list[StreamRecord]
    diagnostics: StreamDiagnostics
    warnings: list[ParseWarning] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)


@dataclass(frozen=True)
class MessageRegistryEntry:
    """Known Unicore message family naming and decoder status."""

    root_name: str
    ascii_name: str
    binary_name: str
    message_id: int | None
    role: str
    decoder_status: Literal["supported", "diagnostic", "known_unsupported"]


NmeaRecord = StreamRecord
UnicoreAsciiRecord = StreamRecord
UnicoreBinaryRecord = StreamRecord
CommandResponseRecord = StreamRecord
NoiseRecord = StreamRecord


UNICORE_MESSAGE_REGISTRY: tuple[MessageRegistryEntry, ...] = (
    MessageRegistryEntry("OBSVM", "OBSVMA", "OBSVMB", 12, "observation", "supported"),
    MessageRegistryEntry("OBSVMCMP", "OBSVMCMPA", "OBSVMCMPB", 138, "observation", "supported"),
    MessageRegistryEntry("GPSEPH", "GPSEPHA", "GPSEPHB", 106, "navigation", "supported"),
    MessageRegistryEntry("GLOEPH", "GLOEPHA", "GLOEPHB", 107, "navigation", "supported"),
    MessageRegistryEntry("GALEPH", "GALEPHA", "GALEPHB", 109, "navigation", "supported"),
    MessageRegistryEntry("BDSEPH", "BDSEPHA", "BDSEPHB", 108, "navigation", "supported"),
    MessageRegistryEntry("BD3EPH", "BD3EPHA", "BD3EPHB", 2999, "navigation", "known_unsupported"),
    MessageRegistryEntry("QZSSEPH", "QZSSEPHA", "QZSSEPHB", 110, "navigation", "known_unsupported"),
    MessageRegistryEntry("IRNSSEPH", "IRNSSEPHA", "IRNSSEPHB", 112, "navigation", "known_unsupported"),
)


def parse_bytes(data: bytes, options: ParseOptions | None = None) -> ParseResult:
    """Parse UM980 mixed-stream bytes.

    Args:
        data: Receiver byte stream.
        options: Optional parser options.

    Returns:
        Parse result containing records and aggregate diagnostics.
    """

    opts = options or ParseOptions()
    records, diagnostics = parse_stream(data, progress=opts.progress)
    return ParseResult(records=records, diagnostics=diagnostics, warnings=_warnings_from_diagnostics(diagnostics))


def parse_file(path: Path, options: ParseOptions | None = None) -> ParseResult:
    """Parse a UM980 capture file."""

    return parse_bytes(path.read_bytes(), options=options)


def iter_records(source: BinaryIO, options: ParseOptions | None = None) -> Iterator[StreamRecord]:
    """Yield records from a binary source.

    The current parser keeps a bounded public API while delegating to the
    byte-level parser.  Callers that need semantic products should consume the
    returned ``StreamRecord`` objects through the specialised decoder modules.
    """

    yield from parse_bytes(source.read(), options=options).records


def summarize_records(records: list[StreamRecord]) -> StreamDiagnostics:
    """Summarise already-parsed stream records."""

    diagnostics = StreamDiagnostics(input_bytes=sum(len(record.raw) for record in records))
    for record in records:
        if record.kind == "nmea":
            diagnostics.valid_nmea_records += 1
            if record.msg_type:
                diagnostics.nmea_types[record.msg_type] += 1
        elif record.kind == "command_response":
            diagnostics.command_response_records += 1
        elif record.kind == "unicore_ascii":
            diagnostics.unicore_ascii_records += 1
            if record.msg_type:
                diagnostics.unicore_types[record.msg_type] += 1
        elif record.kind == "unicore_binary":
            diagnostics.unicore_binary_records += 1
            if record.msg_type:
                diagnostics.unicore_types[record.msg_type] += 1
        elif record.kind == "noise":
            diagnostics.noise_bytes += len(record.raw)
    return diagnostics


def _warnings_from_diagnostics(diagnostics: StreamDiagnostics) -> list[ParseWarning]:
    warnings: list[ParseWarning] = []
    for example in diagnostics.rejection_examples:
        warnings.append(
            ParseWarning(
                severity="warning",
                family=str(example.get("family", "unknown")),
                reason=str(example.get("reason", "unknown")),
                offset_start=int(example.get("offset", 0) or 0),
                sample_hex=str(example.get("sample_hex", "")),
                sample_text=str(example.get("sample_text", "")),
            )
        )
    return warnings
