import binascii

from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.stream import parse_stream, unicore_ascii_checksum_ok, unicore_binary_crc32, unicore_crc32


def _binary_frame(message_id: int, payload: bytes) -> bytes:
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    header[4:6] = message_id.to_bytes(2, "little")
    header[6:8] = len(payload).to_bytes(2, "little")
    body = bytes(header) + payload
    return body + unicore_binary_crc32(body).to_bytes(4, "little")


def _ascii_record(body: str) -> bytes:
    crc = unicore_crc32(body.encode("ascii"))
    return f"#{body}*{crc:08X}\r\n".encode("ascii")


def test_stream_recovers_noise_and_nmea():
    sentence = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    records, diag = parse_stream(b"\x00\xff" + sentence.encode() + b"\r\njunk")
    assert diag.noise_bytes == 6
    assert diag.valid_nmea_records == 1
    assert records[1].kind == "nmea"
    assert records[1].msg_type == "GNRMC"


def test_command_response_is_not_invalid_nmea():
    records, diag = parse_stream(b"$command,CONFIG COM1 230400,response: OK*21\r\n")

    assert len(records) == 1
    assert records[0].kind == "command_response"
    assert diag.command_response_records == 1
    assert diag.invalid_nmea_records == 0


def test_stream_detects_unicore_ascii():
    records, diag = parse_stream(_ascii_record("OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7"))
    assert diag.unicore_ascii_records == 1
    assert records[0].msg_type == "OBSVMA"


def test_unicore_ascii_crc_uses_receiver_algorithm():
    line = (
        b"#BD3EPHA,85,GPS,FINE,2420,537000000,0,0,18,11;27,0,3,1,0,0,2420,2420,536983.0,"
        b"536400.0,4.020019531e+02,2.065658569e-03,4.007666935e-09,-1.212034303e-14,"
        b"-2.300111513e-01,7.274733507e-04,1.570437934e+00,4.580244422e-06,"
        b"1.070089638e-06,3.341679688e+02,9.366015625e+01,-5.867332220e-08,"
        b"-1.862645149e-08,9.484101602e-01,8.446780414e-11,1.070358689e+00,"
        b"-7.127975480e-09,536400.0,0.000000000e+00,0.000000000e+00,-8.556526154e-09,"
        b"0.000000000e+00,0.000000000e+00,0.000000000e+00,6.391645293e-04,"
        b"4.780176255e-12,0.000000000e+00,1788,0,30,7,7,0,0,2*caef92a9\r\n"
    )
    body, checksum = line[1:].split(b"*", 1)
    expected = int(checksum[:8], 16)

    assert unicore_crc32(body) == expected
    assert binascii.crc32(body) & 0xFFFFFFFF != expected
    assert unicore_ascii_checksum_ok(line) is True


def test_stream_rejects_binary_hash_fragment_as_unicore_ascii():
    records, diag = parse_stream(
        b"\x00#not-ascii,\xff\x00\n"
        + _ascii_record("OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7")
    )

    assert diag.unicore_ascii_records == 1
    assert records[1].kind == "unicore_ascii"
    assert records[1].msg_type == "OBSVMA"


def test_stream_preserves_epha_ascii_between_binary_frames():
    data = (
        _binary_frame(138, b"before")
        + _ascii_record("GPSEPHA,COM1,0,0;1")
        + _ascii_record("GLOEPHA,COM1,0,0;1")
        + _ascii_record("GALEPHA,COM1,0,0;1")
        + _ascii_record("BDSEPHA,COM1,0,0;1")
        + _ascii_record("BD3EPHA,COM1,0,0;1")
        + b"#GPSEPHA,COM1,0,0;bad*00000000\r\n"
        + _binary_frame(2118, b"after")
    )

    records, diag = parse_stream(data)

    assert diag.unicore_binary_records == 2
    assert diag.unicore_ascii_records == 5
    assert diag.invalid_unicore_ascii_records == 1
    assert diag.invalid_unicore_ascii_types["GPSEPHA"] == 1
    assert [record.msg_type for record in records if record.kind == "unicore_ascii"] == [
        "GPSEPHA",
        "GLOEPHA",
        "GALEPHA",
        "BDSEPHA",
        "BD3EPHA",
    ]


def test_unicore_ascii_can_end_with_bare_carriage_return_before_noise():
    raw = _ascii_record("GALEPHA,COM1,0,0;1").rstrip(b"\n") + b"trailing-noise\n"

    records, diag = parse_stream(raw)

    assert diag.unicore_ascii_records == 1
    assert records[0].kind == "unicore_ascii"
    assert records[0].msg_type == "GALEPHA"


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
    assert diag.invalid_unicore_ascii_types["OBSVMA"] == 1


def test_stream_rejects_unicore_ascii_without_checksum():
    records, diag = parse_stream(b"#OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7\r\n")

    assert records[0].kind == "noise"
    assert diag.unicore_ascii_records == 0
    assert diag.invalid_unicore_ascii_by_reason["missing_checksum"] == 1
