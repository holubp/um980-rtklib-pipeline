from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.stream import parse_stream


def test_stream_recovers_noise_and_nmea():
    sentence = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    records, diag = parse_stream(b"\x00\xff" + sentence.encode() + b"\r\njunk")
    assert diag.noise_bytes == 6
    assert diag.valid_nmea_records == 1
    assert records[1].kind == "nmea"
    assert records[1].msg_type == "GNRMC"


def test_stream_detects_unicore_ascii():
    records, diag = parse_stream(b"#OBSVMA,COM1,0,0;OBSVMA,2400,1.0,GPS,1,L1,1,2,3,40,0,7*00000000\r\n")
    assert diag.unicore_ascii_records == 1
    assert records[0].msg_type == "OBSVMA"

