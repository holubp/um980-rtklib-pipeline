from pathlib import Path
from dataclasses import replace

from um980_rtklib_pipeline.bestnav import parse_bestnava
from um980_rtklib_pipeline.nmea import checksum
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.solution import (
    bestnav_records_to_solution_extraction,
    extract_solutions,
    position_nmea_records,
    write_gpx,
    write_solution_csv,
    write_solution_nmea,
)
from um980_rtklib_pipeline.stream import parse_stream


BESTNAVA = (
    "#BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
    "SOL_COMPUTED,NARROW_INT,50.0,14.0,250.5,45.1,WGS84,0.1,0.1,0.2,"
    "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_INT,5.144444,90.0,0.0,0.1,0.1,0.2\n"
)


def test_solution_extracts_rmc_and_gga(tmp_path: Path):
    rmc = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    gga = make_sentence("GNGGA,120000.00,5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    records, _ = parse_stream((rmc + "\r\n" + gga + "\r\n").encode())
    extracted = extract_solutions(records)
    assert extracted.all_nmea == [rmc, gga]
    assert len(extracted.solution_points) == 2
    assert extracted.solution_points[1].fix_quality_text == "rtk-fixed"
    csv_path = tmp_path / "solution.csv"
    gpx_path = tmp_path / "solution.gpx"
    write_solution_csv(csv_path, extracted.solution_points)
    write_gpx(gpx_path, extracted.solution_points)
    assert "time_utc" in csv_path.read_text()
    assert "<trkpt" in gpx_path.read_text()


def test_position_nmea_best_keeps_highest_quality_per_fractional_epoch():
    rmc_a = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    gga_a = make_sentence("GNGGA,120000.00,5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    rmc_b = make_sentence("GNRMC,120000.20,A,5000.0001,N,01400.0001,E,0.0,0.0,200526,,,A")
    gga_b = make_sentence("GNGGA,120000.20,5000.0001,N,01400.0001,E,5,20,0.7,250.0,M,45.0,M,0.5,0001")
    gsv = make_sentence("GPGSV,1,1,01,01,10,100,40")

    assert position_nmea_records([rmc_a, gga_a, gsv, rmc_b, gga_b], "best") == [gga_a, gga_b]


def test_position_nmea_best_does_not_merge_repeated_time_after_intervening_epoch():
    first = make_sentence("GNGGA,120000.00,5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    second = make_sentence("GNGGA,120001.00,5000.0001,N,01400.0001,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    repeated = make_sentence("GNGGA,120000.00,5000.0002,N,01400.0002,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")

    assert position_nmea_records([first, second, repeated], "best") == [first, second, repeated]


def test_position_nmea_all_keeps_valid_position_sentences_only():
    valid_rmc = make_sentence("GNRMC,120000.00,A,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,A")
    invalid_rmc = make_sentence("GNRMC,120000.10,V,5000.0000,N,01400.0000,E,0.0,0.0,200526,,,N")
    valid_gga = make_sentence("GNGGA,120000.20,5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    invalid_gga = make_sentence("GNGGA,120000.30,5000.0000,N,01400.0000,E,0,00,99.9,0.0,M,0.0,M,,")
    valid_gns = make_sentence("GNGNS,120000.40,5000.0000,N,01400.0000,E,AAA,20,0.7,250.0,45.0,0.5,0001")
    gsa = make_sentence("GNGSA,A,3,01,02,03,,,,,,,,,,1.0,0.7,0.7")

    assert position_nmea_records([valid_rmc, invalid_rmc, valid_gga, invalid_gga, valid_gns, gsa], "all") == [
        valid_rmc,
        valid_gga,
        valid_gns,
    ]


def test_pppnava_is_preserved_but_not_timestamped_with_host_time():
    records, _ = parse_stream(b"$PPPNAVA,OK,PPP,50.0,14.0,250.0\r\n")
    extracted = extract_solutions(records)
    assert not extracted.solution_points
    assert any("PPPNAVA records are preserved" in warning for warning in extracted.warnings)
    assert extracted.all_rows[0]["type"] == "PPPNAVA"


def test_bestnav_solution_source_preserves_native_20hz_outputs(tmp_path: Path):
    base = parse_bestnava(BESTNAVA.strip())
    records = [
        replace(base, tow_s=base.tow_s + index * 0.05, time_utc=base.time_utc.replace(microsecond=index * 50_000))
        for index in range(20)
    ]

    extracted = bestnav_records_to_solution_extraction(records, source="auto", talk_id="GN")

    assert len(extracted.solution_points) == 20
    assert len([line for line in extracted.solution_nmea if line.startswith("$GNGGA,")]) == 20
    assert len([line for line in extracted.solution_nmea if line.startswith("$GNRMC,")]) == 20
    assert len([line for line in extracted.solution_nmea if line.startswith("$GNVTG,")]) == 20
    assert extracted.solution_points[0].fix_quality == 4
    assert extracted.solution_points[0].fix_quality_text == "rtk-fixed"
    for line in extracted.solution_nmea:
        body, expected = line[1:].split("*", 1)
        assert checksum(body) == expected

    csv_path = tmp_path / "solution.csv"
    gpx_path = tmp_path / "solution.gpx"
    nmea_path = tmp_path / "solution.nmea"
    write_solution_csv(csv_path, extracted.solution_points)
    write_gpx(gpx_path, extracted.solution_points)
    write_solution_nmea(nmea_path, extracted.solution_points, extracted.solution_nmea)

    assert len(csv_path.read_text(encoding="utf-8").splitlines()) > 1
    assert "<trkpt" in gpx_path.read_text(encoding="utf-8")
    assert nmea_path.read_text(encoding="ascii").count("$GNGGA,") == 20


def test_bestnav_solution_source_maps_rtk_float_to_gga_quality_5():
    base = parse_bestnava(BESTNAVA.strip())
    extracted = bestnav_records_to_solution_extraction([replace(base, pos_type="NARROW_FLOAT")])

    assert extracted.solution_points[0].fix_quality == 5
    assert ",5,20," in next(line for line in extracted.solution_nmea if line.startswith("$GNGGA,"))


def test_stream_rejects_binary_garbage_that_contains_dollar_fragments():
    valid = make_sentence("GNGGA,120000.00,5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    records, diagnostics = parse_stream(b"\x01\x02$GNGGA,\xff\x00not-nmea\n" + (valid + "\r\n").encode("ascii"))

    assert diagnostics.valid_nmea_records == 1
    assert [record.text for record in records if record.kind == "nmea"] == [valid]
