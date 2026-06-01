from __future__ import annotations

import binascii
import struct
from dataclasses import replace

import pytest

from um980_rtklib_pipeline.bestnav import (
    bestnav_records_to_nmea,
    extract_bestnav_records,
    filter_bestnav_records,
    parse_bestnava,
    parse_bestnavb,
    parse_bestnav_sentences,
)
from um980_rtklib_pipeline.nmea import parse_sentence
from um980_rtklib_pipeline.stream import parse_stream


BESTNAVA = (
    "#BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
    "SOL_COMPUTED,NARROW_INT,50.0,14.0,250.5,45.1,WGS84,0.1,0.1,0.2,"
    "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_INT,5.144444,90.0,0.0,0.1,0.1,0.2\n"
)


def _ascii_record(body: str) -> str:
    crc = binascii.crc32(body.encode("ascii")) & 0xFFFFFFFF
    return f"#{body}*{crc:08X}\n"


def test_parse_bestnava_and_generate_checksummed_nmea() -> None:
    record = parse_bestnava(BESTNAVA.strip())

    assert record.source == "BESTNAVA"
    assert record.pos_type == "NARROW_INT"
    assert record.lat_deg == 50.0
    assert record.lon_deg == 14.0
    assert record.satellites_used == 20

    lines = bestnav_records_to_nmea([record], sentences=("GGA", "RMC", "VTG"), talk_id="GN")
    parsed = [parse_sentence(line) for line in lines]

    assert all(item is not None and item.checksum_ok is None for item in parsed)
    assert lines[0].startswith("$GNGGA,")
    assert ",5000.0000000,N,01400.0000000,E,4,20,,250.500,M,45.100,M,1.5,0001*" in lines[0]
    assert lines[1].startswith("$GNRMC,")
    assert ",10.000,90.000," in lines[1]
    assert lines[1].endswith(",R*" + lines[1].split("*", 1)[1])
    assert lines[2].startswith("$GNVTG,90.000,T,,M,10.000,N,18.520,K,R*")


@pytest.mark.parametrize(
    ("pos_type", "expected_gga", "expected_mode"),
    [
        ("SINGLE", "1", "A"),
        ("PSRDIFF", "2", "D"),
        ("NARROW_FLOAT", "5", "F"),
        ("NARROW_INT", "4", "R"),
        ("INS", "6", "E"),
    ],
)
def test_bestnav_quality_mapping(pos_type: str, expected_gga: str, expected_mode: str) -> None:
    record = replace(parse_bestnava(BESTNAVA.strip()), pos_type=pos_type)

    gga, rmc, vtg = bestnav_records_to_nmea([record], sentences=("GGA", "RMC", "VTG"))

    assert f",E,{expected_gga},20," in gga
    assert rmc.split("*", 1)[0].endswith("," + expected_mode)
    assert vtg.split("*", 1)[0].endswith("," + expected_mode)


def test_bestnav_rate_decimation_uses_timestamps() -> None:
    base = parse_bestnava(BESTNAVA.strip())
    records = [replace(base, tow_s=base.tow_s + index * 0.05) for index in range(10)]

    selected = filter_bestnav_records(records, rate_hz=5)

    assert [round(record.tow_s - base.tow_s, 2) for record in selected] == [0.0, 0.2, 0.4]


def test_extract_bestnav_counts_malformed_without_crashing() -> None:
    bestnav_body = BESTNAVA.strip()[1:]
    records, _ = parse_stream((_ascii_record(bestnav_body) + _ascii_record("BESTNAVA,COM1;BROKEN")).encode("ascii"))

    extracted = extract_bestnav_records(records)

    assert len(extracted.records) == 1
    assert extracted.present["BESTNAVA"] == 2
    assert extracted.malformed["BESTNAVA"] == 1


def test_bestnav_analysis_accounts_native_emission_and_drops() -> None:
    base = parse_bestnava(BESTNAVA.strip())
    invalid = replace(base, tow_s=base.tow_s + 0.05, pos_sol_status="INSUFFICIENT_OBS", pos_type="NONE")
    duplicate = replace(base, tow_s=base.tow_s)
    extracted = extract_bestnav_records([])
    extracted.records.extend([base, invalid, duplicate])

    summary = extracted.as_dict()

    assert summary["decoded"] == 3
    assert summary["valid_position"] == 2
    assert summary["emitted_solution_points_native"] == 1
    assert summary["dropped_by_reason"]["dropped_bad_status"] == 1
    assert summary["dropped_by_reason"]["dropped_duplicate_time"] == 1


def test_parse_bestnavb_payload_and_generate_nmea() -> None:
    payload = bytearray(120)
    struct.pack_into("<II", payload, 0, 0, 50)
    struct.pack_into("<ddd", payload, 8, 50.0, 14.0, 250.5)
    struct.pack_into("<fIfff", payload, 32, 45.1, 61, 0.1, 0.1, 0.2)
    payload[52:56] = b"0001"
    struct.pack_into("<ff", payload, 56, 1.5, 0.0)
    payload[64] = 30
    payload[65] = 20
    struct.pack_into("<II", payload, 72, 0, 8)
    struct.pack_into("<ff", payload, 80, 0.0, 1.5)
    struct.pack_into("<ddd", payload, 88, 5.144444, 90.0, 0.0)
    header = bytearray(24)
    header[0:3] = b"\xaa\x44\xb5"
    struct.pack_into("<H", header, 4, 2118)
    struct.pack_into("<H", header, 6, len(payload))
    struct.pack_into("<H", header, 10, 2419)
    struct.pack_into("<I", header, 12, 132572000)
    raw = bytes(header) + bytes(payload) + b"\x00\x00\x00\x00"

    record = parse_bestnavb(raw)
    lines = bestnav_records_to_nmea([record], sentences=("GGA", "RMC", "VTG"), talk_id="GN")

    assert record.source == "BESTNAVB"
    assert record.pos_type == "NARROW_INT"
    assert lines[0].startswith("$GNGGA,")
    assert ",4,20," in lines[0]


def test_bestnavb_invalid_station_id_does_not_break_generated_nmea() -> None:
    payload = bytearray(120)
    struct.pack_into("<II", payload, 0, 0, 50)
    struct.pack_into("<ddd", payload, 8, 50.0, 14.0, 250.5)
    struct.pack_into("<fIfff", payload, 32, 45.1, 61, 0.1, 0.1, 0.2)
    payload[52:56] = b"\xfft\t\x80"
    payload[64] = 30
    payload[65] = 20
    struct.pack_into("<II", payload, 72, 0, 8)
    struct.pack_into("<ddd", payload, 88, 1.0, 90.0, 0.0)
    header = bytearray(24)
    header[0:3] = b"\xaa\x44\xb5"
    struct.pack_into("<H", header, 4, 2118)
    struct.pack_into("<H", header, 6, len(payload))
    struct.pack_into("<H", header, 10, 2419)
    struct.pack_into("<I", header, 12, 132572000)

    record = parse_bestnavb(bytes(header) + bytes(payload) + b"\x00\x00\x00\x00")
    lines = bestnav_records_to_nmea([record], sentences=("GGA",), talk_id="GN")

    assert record.station_id is None
    assert lines[0].startswith("$GNGGA,")


def test_bestnav_nmea_skips_non_computed_records() -> None:
    record = replace(parse_bestnava(BESTNAVA.strip()), pos_sol_status="INSUFFICIENT_OBS", pos_type="NONE")

    assert bestnav_records_to_nmea([record], sentences=("GGA", "RMC", "VTG"), talk_id="GN") == []


def test_parse_bestnav_sentences_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unsupported BESTNAV NMEA sentence"):
        parse_bestnav_sentences("GGA,GLL")
