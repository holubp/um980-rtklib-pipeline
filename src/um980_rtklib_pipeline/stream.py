"""Byte-level UM980 mixed stream demultiplexer."""

from __future__ import annotations

import binascii
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal


RecordKind = Literal["nmea", "unicore_ascii", "unicore_binary", "noise"]
BINARY_MESSAGE_TYPES = {
    4: "BDSIONB",
    8: "GPSIONB",
    9: "GALIONB",
    12: "OBSVMB",
    19: "GPSUTCB",
    20: "GALUTCB",
    21: "BD3IONB",
    22: "BD3UTCB",
    106: "GPSEPHB",
    107: "GLOEPHB",
    108: "BDSEPHB",
    109: "GALEPHB",
    110: "QZSSEPHB",
    112: "IRNSSEPHB",
    138: "OBSVMCMPB",
    2012: "BDSUTCB",
    2118: "BESTNAVB",
    2999: "BD3EPHB",
}
KNOWN_NMEA_SENTENCE_TYPES = {
    "DTM",
    "GBS",
    "GGA",
    "GLL",
    "GNS",
    "GRS",
    "GSA",
    "GST",
    "GSV",
    "RMC",
    "THS",
    "VTG",
    "ZDA",
}
KNOWN_PROPRIETARY_NMEA_TYPES = {"ADRNAVA", "PPPNAVA"}
NMEA_LINE_RE = re.compile(rb"^\$([A-Z0-9]{5}|P[A-Z0-9]{3,}|ADRNAVA|PPPNAVA)(?:,[ -~]*)?(?:\*[0-9A-Fa-f]{2})?\r?\n?$")
UNICORE_ASCII_LINE_RE = re.compile(rb"^#[A-Z0-9]+(?:,[ -~]*)?;[ -~]*(?:\*[0-9A-Fa-f]{8})?\r?\n?$")
UNICORE_BINARY_CRC32_POLY = 0xEDB88320


@dataclass(frozen=True)
class StreamRecord:
    """One demultiplexed record from a mixed UM980 serial stream.

    Attributes:
        kind: Record family (`nmea`, `unicore_ascii`, `unicore_binary`, or
            `noise`).
        offset: Byte offset in the input stream.
        raw: Original bytes for the record.
        text: Decoded text for ASCII records.
        msg_type: Message type when one can be inferred.
        checksum_ok: Checksum result, or `None` when not applicable.
    """

    kind: RecordKind
    offset: int
    raw: bytes
    text: str | None
    msg_type: str | None
    checksum_ok: bool | None


@dataclass
class StreamDiagnostics:
    """Counters describing mixed-stream parsing results.

    Attributes:
        input_bytes: Total input byte count.
        valid_nmea_records: NMEA records with valid or absent checksums.
        invalid_nmea_records: NMEA records with invalid checksums.
        unicore_ascii_records: Unicore ASCII records found.
        unicore_binary_records: Unicore binary frames found.
        invalid_unicore_binary_records: Binary sync candidates rejected.
        noise_bytes: Bytes not assigned to a known record.
        nmea_types: Counts by NMEA sentence type.
        unicore_types: Counts by Unicore message type.
    """

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
        """Initialise mutable counters omitted by the caller."""

        if self.nmea_types is None:
            self.nmea_types = Counter()
        if self.unicore_types is None:
            self.unicore_types = Counter()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable diagnostics dictionary."""

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
    """Validate a NMEA checksum.

    Args:
        line: Raw NMEA sentence bytes.

    Returns:
        True for a valid checksum, False for an invalid checksum, or `None`
        when the line is not checksum-bearing NMEA.
    """

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


def is_plausible_nmea_line(line: bytes) -> bool:
    """Return true when a byte line is structurally plausible NMEA text.

    Mixed UM980 binary logs may contain arbitrary ``$`` bytes inside binary
    payloads. This guard prevents those fragments from being treated as live
    NMEA by requiring ASCII text, a normal talker/sentence family, and a valid
    checksum when a checksum is present.
    """

    if not line.startswith(b"$") or len(line) > 1024 or not NMEA_LINE_RE.match(line):
        return False
    try:
        text = line.decode("ascii").strip()
    except UnicodeDecodeError:
        return False
    msg_type = record_message_type(text, "$")
    if not msg_type:
        return False
    if msg_type in KNOWN_PROPRIETARY_NMEA_TYPES:
        return True
    if msg_type.startswith("P"):
        return True
    if len(msg_type) != 5:
        return False
    if msg_type[-3:] not in KNOWN_NMEA_SENTENCE_TYPES:
        return False
    ok = nmea_checksum_ok(line)
    return ok is not False


def unicore_ascii_checksum_ok(line: bytes) -> bool | None:
    """Validate a Unicore ASCII CRC32 checksum.

    Args:
        line: Raw Unicore ASCII record bytes.

    Returns:
        True for a valid CRC, False for an invalid CRC, or `None` when the line
        is not a checksum-bearing Unicore ASCII record.
    """

    if not line.startswith(b"#") or b"*" not in line:
        return None
    body, checksum = line[1:].split(b"*", 1)
    checksum = checksum.strip()[:8]
    try:
        expected = int(checksum.decode("ascii"), 16)
    except ValueError:
        return False
    return binascii.crc32(body) & 0xFFFFFFFF == expected


def is_plausible_unicore_ascii_line(line: bytes) -> bool:
    """Return true when a byte line looks like a Unicore ASCII record.

    Mixed logs contain arbitrary ``#`` bytes inside binary payloads. A real
    Unicore ASCII record is printable ASCII, starts with an uppercase message
    name, has a header/payload ``;`` separator, and may end with an eight-digit
    CRC. Checksum failures are left to downstream diagnostics because captures
    and tests may intentionally contain placeholder CRC values.
    """

    if not line.startswith(b"#") or len(line) > 65536 or not UNICORE_ASCII_LINE_RE.match(line):
        return False
    try:
        line.decode("ascii")
    except UnicodeDecodeError:
        return False
    return True


def unicore_binary_crc32(data: bytes) -> int:
    """Return the CRC32 used by Unicore binary receiver frames.

    Unicore binary messages use the same little-endian CRC implementation as
    RTKLIB's ``rtk_crc32`` helper: initial value zero, reflected polynomial
    ``0xEDB88320``, and no final XOR. The checksum is calculated over the sync
    bytes, fixed header, and payload, excluding the trailing four-byte CRC
    field.
    """

    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = ((crc >> 1) ^ UNICORE_BINARY_CRC32_POLY) & 0xFFFFFFFF
            else:
                crc = (crc >> 1) & 0xFFFFFFFF
    return crc


def unicore_binary_checksum_ok(frame: bytes) -> bool:
    """Return true when a complete Unicore binary frame has a valid CRC."""

    if len(frame) < 28 or frame[:3] != b"\xaa\x44\xb5":
        return False
    expected = int.from_bytes(frame[-4:], "little", signed=False)
    return unicore_binary_crc32(frame[:-4]) == expected


def record_message_type(text: str, prefix: str) -> str | None:
    """Extract a message type from NMEA or Unicore ASCII text.

    Args:
        text: Decoded record text.
        prefix: Expected record prefix, usually `$` or `#`.

    Returns:
        Message type or `None` when the text does not match the prefix.
    """

    if not text.startswith(prefix):
        return None
    body = text[1:].split("*", 1)[0]
    first = body.split(",", 1)[0].split(";", 1)[0]
    return first or None


def _binary_frame_length(data: bytes, pos: int) -> int | None:
    # Unicore UM980 frames use a fixed 24-byte header and encode payload length
    # as little-endian uint16 at bytes 6..7. RTKLIB-ex uses the same layout in
    # src/rcv/unicore.c (HLEN=24, len=U2(buff+6)+HLEN).
    header_length = 24
    if pos + header_length + 4 > len(data):
        return None
    payload_length = int.from_bytes(data[pos + 6 : pos + 8], "little", signed=False)
    total = header_length + payload_length + 4
    if total <= header_length + 4 or pos + total > len(data):
        return None
    return total


def parse_stream(data: bytes, *, progress: bool = False) -> tuple[list[StreamRecord], StreamDiagnostics]:
    """Parse a mixed UM980 serial stream into record objects.

    Args:
        data: Raw bytes from a receiver capture.
        progress: Emit coarse byte-progress messages through logging.

    Returns:
        Parsed records and aggregate diagnostics.
    """

    diagnostics = StreamDiagnostics(input_bytes=len(data))
    records: list[StreamRecord] = []
    pos = 0
    noise_start: int | None = None
    progress_step = 10 * 1024 * 1024
    next_progress = progress_step

    def flush_noise(end: int) -> None:
        nonlocal noise_start
        if noise_start is not None and end > noise_start:
            raw = data[noise_start:end]
            diagnostics.noise_bytes += len(raw)
            records.append(StreamRecord("noise", noise_start, raw, None, None, None))
        noise_start = None

    while pos < len(data):
        if progress and len(data) >= progress_step and pos >= next_progress:
            logging.info("parsed %.1f%% of rover byte stream (%d/%d bytes)", pos * 100.0 / len(data), pos, len(data))
            next_progress += progress_step
        byte = data[pos : pos + 1]
        if byte == b"$":
            end = data.find(b"\n", pos)
            if end == -1:
                if noise_start is None:
                    noise_start = pos
                break
            raw = data[pos : end + 1]
            if not is_plausible_nmea_line(raw):
                if noise_start is None:
                    noise_start = pos
                pos += 1
                continue
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
            if not is_plausible_unicore_ascii_line(raw):
                if noise_start is None:
                    noise_start = pos
                pos += 1
                continue
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
                raw = data[pos : pos + frame_len]
                if not unicore_binary_checksum_ok(raw):
                    diagnostics.invalid_unicore_binary_records += 1
                    if noise_start is None:
                        noise_start = pos
                    pos += 1
                    continue
                flush_noise(pos)
                msg_id = int.from_bytes(raw[4:6], "little", signed=False) if len(raw) >= 6 else 0
                msg_type = BINARY_MESSAGE_TYPES.get(msg_id, f"binary:{msg_id}")
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
