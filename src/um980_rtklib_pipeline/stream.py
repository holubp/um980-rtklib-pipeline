"""Byte-level UM980 mixed stream demultiplexer."""

from __future__ import annotations

import binascii
from collections import Counter
from dataclasses import dataclass
from typing import Literal


RecordKind = Literal["nmea", "unicore_ascii", "unicore_binary", "noise"]


@dataclass(frozen=True)
class StreamRecord:
    kind: RecordKind
    offset: int
    raw: bytes
    text: str | None
    msg_type: str | None
    checksum_ok: bool | None


@dataclass
class StreamDiagnostics:
    input_bytes: int = 0
    valid_nmea_records: int = 0
    invalid_nmea_records: int = 0
    unicore_ascii_records: int = 0
    unicore_binary_records: int = 0
    invalid_unicore_binary_records: int = 0
    noise_bytes: int = 0
    nmea_types: Counter[str] = None  # type: ignore[assignment]
    unicore_types: Counter[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.nmea_types is None:
            self.nmea_types = Counter()
        if self.unicore_types is None:
            self.unicore_types = Counter()

    def as_dict(self) -> dict[str, object]:
        return {
            "input_bytes": self.input_bytes,
            "valid_nmea_records": self.valid_nmea_records,
            "invalid_nmea_records": self.invalid_nmea_records,
            "unicore_ascii_records": self.unicore_ascii_records,
            "unicore_binary_records": self.unicore_binary_records,
            "invalid_unicore_binary_records": self.invalid_unicore_binary_records,
            "noise_bytes": self.noise_bytes,
            "nmea_types": dict(self.nmea_types),
            "unicore_types": dict(self.unicore_types),
        }


def nmea_checksum_ok(line: bytes) -> bool | None:
    if not line.startswith(b"$") or b"*" not in line:
        return None
    body, checksum = line[1:].split(b"*", 1)
    checksum = checksum.strip()[:2]
    try:
        expected = int(checksum.decode("ascii"), 16)
    except ValueError:
        return False
    actual = 0
    for byte in body:
        actual ^= byte
    return actual == expected


def unicore_ascii_checksum_ok(line: bytes) -> bool | None:
    if not line.startswith(b"#") or b"*" not in line:
        return None
    body, checksum = line[1:].split(b"*", 1)
    checksum = checksum.strip()[:8]
    try:
        expected = int(checksum.decode("ascii"), 16)
    except ValueError:
        return False
    return binascii.crc32(body) & 0xFFFFFFFF == expected


def record_message_type(text: str, prefix: str) -> str | None:
    if not text.startswith(prefix):
        return None
    body = text[1:].split("*", 1)[0]
    first = body.split(",", 1)[0].split(";", 1)[0]
    return first or None


def _binary_frame_length(data: bytes, pos: int) -> int | None:
    # Unicore/OEM-style frames commonly encode header length at byte 3 and
    # payload length as little-endian uint16 at bytes 8..9. Validate cheaply.
    if pos + 12 > len(data):
        return None
    header_length = data[pos + 3]
    if header_length < 12 or header_length > 128:
        return None
    if pos + header_length > len(data):
        return None
    payload_length = int.from_bytes(data[pos + 8 : pos + 10], "little", signed=False)
    total = header_length + payload_length + 4
    if total <= 0 or pos + total > len(data):
        return None
    return total


def parse_stream(data: bytes) -> tuple[list[StreamRecord], StreamDiagnostics]:
    """Parse a mixed UM980 serial stream into record objects."""

    diagnostics = StreamDiagnostics(input_bytes=len(data))
    records: list[StreamRecord] = []
    pos = 0
    noise_start: int | None = None

    def flush_noise(end: int) -> None:
        nonlocal noise_start
        if noise_start is not None and end > noise_start:
            raw = data[noise_start:end]
            diagnostics.noise_bytes += len(raw)
            records.append(StreamRecord("noise", noise_start, raw, None, None, None))
        noise_start = None

    while pos < len(data):
        byte = data[pos : pos + 1]
        if byte == b"$":
            end = data.find(b"\n", pos)
            if end == -1:
                if noise_start is None:
                    noise_start = pos
                break
            raw = data[pos : end + 1]
            flush_noise(pos)
            text = raw.decode("ascii", errors="replace").strip()
            ok = nmea_checksum_ok(raw)
            msg_type = record_message_type(text, "$")
            if ok is False:
                diagnostics.invalid_nmea_records += 1
            else:
                diagnostics.valid_nmea_records += 1
                if msg_type:
                    diagnostics.nmea_types[msg_type] += 1
            records.append(StreamRecord("nmea", pos, raw, text, msg_type, ok))
            pos = end + 1
            continue

        if byte == b"#":
            end = data.find(b"\n", pos)
            if end == -1:
                if noise_start is None:
                    noise_start = pos
                break
            raw = data[pos : end + 1]
            flush_noise(pos)
            text = raw.decode("ascii", errors="replace").strip()
            msg_type = record_message_type(text, "#")
            ok = unicore_ascii_checksum_ok(raw)
            diagnostics.unicore_ascii_records += 1
            if msg_type:
                diagnostics.unicore_types[msg_type] += 1
            records.append(StreamRecord("unicore_ascii", pos, raw, text, msg_type, ok))
            pos = end + 1
            continue

        if data[pos : pos + 3] == b"\xaa\x44\xb5":
            frame_len = _binary_frame_length(data, pos)
            if frame_len is not None:
                flush_noise(pos)
                raw = data[pos : pos + frame_len]
                msg_id = int.from_bytes(raw[4:6], "little", signed=False) if len(raw) >= 6 else 0
                msg_type = f"binary:{msg_id}"
                diagnostics.unicore_binary_records += 1
                diagnostics.unicore_types[msg_type] += 1
                records.append(StreamRecord("unicore_binary", pos, raw, None, msg_type, None))
                pos += frame_len
                continue
            diagnostics.invalid_unicore_binary_records += 1

        if noise_start is None:
            noise_start = pos
        pos += 1

    flush_noise(len(data))
    return records, diagnostics

