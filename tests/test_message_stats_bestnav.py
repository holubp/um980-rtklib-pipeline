from __future__ import annotations

from argparse import Namespace

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.stream import parse_stream, unicore_binary_crc32


def _binary_frame(message_id: int, payload: bytes) -> bytes:
    header = bytearray(24)
    header[0:3] = b"\xaa\x44\xb5"
    header[4:6] = message_id.to_bytes(2, "little")
    header[6:8] = len(payload).to_bytes(2, "little")
    body = bytes(header) + payload
    return body + unicore_binary_crc32(body).to_bytes(4, "little")


def test_extract_writes_bestnav_nmea_and_analysis_stats(tmp_path):
    rover = tmp_path / "bestnav.unc"
    rover.write_text(
        "#BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
        "SOL_COMPUTED,NARROW_FLOAT,50.0,14.0,250.5,45.1,WGS84,0.1,0.1,0.2,"
        "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2\n"
        "#GPSIONA,COM1,0,0,FINE,2419,132572000,0,0,18,0;1,2,3,4,5,6,7,8\n"
        "#GPSUTCA,COM1,0,0,FINE,2419,132572000,0,0,18,0;1,2,3,4,5,6\n",
        encoding="ascii",
    )

    args = Namespace(
        rover_log=str(rover),
        verbose=False,
        debug=False,
        out_dir=str(tmp_path),
        basename="bestnav",
        analysis_json=True,
        config=None,
        dry_run=False,
        log_file=None,
        solution="none",
        position_nmea="none",
        track_source="auto",
        obs_csv=False,
        raw_output="none",
        rinex_version="3.04",
        rinex_compat="native",
        bestnav_nmea=str(tmp_path / "bestnav.generated.nmea"),
        bestnav_nmea_sentences="GGA,RMC,VTG",
        bestnav_nmea_rate="native",
        bestnav_nmea_source="auto",
        bestnav_nmea_talk_id="GN",
    )

    assert cli.cmd_extract(args) == 0

    nmea = (tmp_path / "bestnav.generated.nmea").read_text(encoding="ascii")
    assert "$GNGGA" in nmea
    assert "$GNRMC" in nmea
    assert "$GNVTG" in nmea
    analysis = (tmp_path / "bestnav.analysis.json").read_text(encoding="utf-8")
    assert '"message_stats"' in analysis
    assert '"diagnostics"' in analysis
    assert '"GPSIONA"' in analysis
    assert '"GPSUTCA"' in analysis
    assert '"field_0": 1' in analysis


def test_extract_auto_uses_bestnav_when_live_nmea_positions_are_absent(tmp_path):
    rover = tmp_path / "bestnav-only.unc"
    rover.write_text(
        "#BESTNAVA,COM1,0,0,FINE,2419,132572000,0,0,18,0;"
        "SOL_COMPUTED,NARROW_FLOAT,50.0,14.0,250.5,45.1,WGS84,0.1,0.1,0.2,"
        "0001,1.5,0.0,30,20,0,0,0,SOL_COMPUTED,NARROW_FLOAT,1.0,45.0,0.0,0.1,0.1,0.2\n",
        encoding="ascii",
    )
    args = Namespace(
        rover_log=str(rover),
        verbose=False,
        debug=False,
        out_dir=str(tmp_path),
        basename="bestnav-only",
        analysis_json=False,
        config=None,
        dry_run=False,
        log_file=None,
        solution="all",
        position_nmea="best",
        track_source="auto",
        obs_csv=False,
        raw_output="none",
        rinex_version="3.04",
        rinex_compat="native",
        bestnav_nmea=None,
        bestnav_nmea_sentences="GGA,RMC,VTG",
        bestnav_nmea_rate="native",
        bestnav_nmea_source="auto",
        bestnav_nmea_talk_id="GN",
    )

    assert cli.cmd_extract(args) == 0

    assert len((tmp_path / "bestnav-only.solution.csv").read_text(encoding="utf-8").splitlines()) > 1
    assert "<trkpt" in (tmp_path / "bestnav-only.solution.gpx").read_text(encoding="utf-8")
    solution_nmea = (tmp_path / "bestnav-only.solution.nmea").read_text(encoding="ascii")
    assert "$GNGGA" in solution_nmea
    assert "$GNRMC" in solution_nmea
    assert "$GNVTG" in solution_nmea
    assert (tmp_path / "bestnav-only.all.nmea").read_text(encoding="ascii") == ""


def test_binary_ion_utc_ids_are_named_for_statistics():
    records, diagnostics = parse_stream(_binary_frame(8, b"abcd") + _binary_frame(19, b"abcd"))

    assert [record.msg_type for record in records] == ["GPSIONB", "GPSUTCB"]
    assert diagnostics.unicore_types["GPSIONB"] == 1
    assert diagnostics.unicore_types["GPSUTCB"] == 1
