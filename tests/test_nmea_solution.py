from pathlib import Path

from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.solution import extract_solutions, position_nmea_records, write_gpx, write_solution_csv
from um980_rtklib_pipeline.stream import parse_stream


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
    records, _ = parse_stream(b"$PPPNAVA,OK,PPP,50.0,14.0,250.0*00\r\n")
    extracted = extract_solutions(records)
    assert not extracted.solution_points
    assert any("PPPNAVA records are preserved" in warning for warning in extracted.warnings)
    assert extracted.all_rows[0]["type"] == "PPPNAVA"
