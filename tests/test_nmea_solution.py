from pathlib import Path

from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.solution import extract_solutions, write_gpx, write_solution_csv
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


def test_pppnava_is_preserved_but_not_timestamped_with_host_time():
    records, _ = parse_stream(b"$PPPNAVA,OK,PPP,50.0,14.0,250.0*00\r\n")
    extracted = extract_solutions(records)
    assert not extracted.solution_points
    assert any("PPPNAVA records are preserved" in warning for warning in extracted.warnings)
    assert extracted.all_rows[0]["type"] == "PPPNAVA"
