import binascii

from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.stream import parse_stream, unicore_binary_crc32


def _binary_frame(message_id: int, payload: bytes) -> bytes:
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    header[4:6] = message_id.to_bytes(2, "little")
    header[6:8] = len(payload).to_bytes(2, "little")
    body = bytes(header) + payload
    return body + unicore_binary_crc32(body).to_bytes(4, "little")


def _ascii_record(body: str) -> bytes:
    crc = binascii.crc32(body.encode("ascii")) & 0xFFFFFFFF
    return f"#{body}*{crc:08X}\r\n".encode("ascii")


def test_stream_recovers_noise_and_nmea():
    sentence = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    records, diag = parse_stream(b"\x00\xff" + sentence.encode() + b"\r\njunk")
    assert diag.noise_bytes == 6
    assert diag.valid_nmea_records == 1
    assert records[1].kind == "nmea"
    assert records[1].msg_type == "GNRMC"


def test_stream_detects_unicore_ascii():
    records, diag = parse_stream(_ascii_record("OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7"))
    assert diag.unicore_ascii_records == 1
    assert records[0].msg_type == "OBSVMA"


def test_stream_rejects_binary_hash_fragment_as_unicore_ascii():
    records, diag = parse_stream(
        b"\x00#not-ascii,\xff\x00\n"
        + _ascii_record("OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7")
    )

    assert diag.unicore_ascii_records == 1
    assert records[1].kind == "unicore_ascii"
    assert records[1].msg_type == "OBSVMA"


def test_stream_detects_fixed_header_unicore_binary_with_named_message_type():
    payload = b"$not,nmea,because,binary\r\n"
    records, diag = parse_stream(_binary_frame(138, payload))
    assert diag.unicore_binary_records == 1
    assert diag.valid_nmea_records == 0
    assert records[0].kind == "unicore_binary"
    assert records[0].msg_type == "OBSVMCMPB"


def test_stream_rejects_unicore_binary_with_invalid_crc():
    frame = bytearray(_binary_frame(138, b"payload"))
    frame[-1] ^= 0xFF

    records, diag = parse_stream(bytes(frame))

    assert diag.unicore_binary_records == 0
    assert diag.invalid_unicore_binary_records == 1
    assert diag.invalid_unicore_binary_by_reason["crc_failure"] == 1
    assert diag.binary_resynchronisation_events == 1
    assert not any(record.kind == "unicore_binary" for record in records)


def test_stream_reports_unknown_binary_message_id():
    records, diag = parse_stream(_binary_frame(65000, b"payload"))

    assert records[0].kind == "unicore_binary"
    assert records[0].msg_type == "binary:65000"
    assert diag.unknown_binary_message_ids[65000] == 1


def test_stream_rejects_unicore_ascii_with_invalid_checksum():
    records, diag = parse_stream(b"#OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7*00000000\r\n")

    assert records[0].kind == "noise"
    assert diag.unicore_ascii_records == 0
    assert diag.invalid_unicore_ascii_records == 1
    assert diag.invalid_unicore_ascii_by_reason["checksum_failure"] == 1


def test_stream_rejects_unicore_ascii_without_checksum():
    records, diag = parse_stream(b"#OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7\r\n")

    assert records[0].kind == "noise"
    assert diag.unicore_ascii_records == 0
    assert diag.invalid_unicore_ascii_by_reason["missing_checksum"] == 1
