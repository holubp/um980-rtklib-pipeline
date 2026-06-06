import argparse
import math
import struct
from pathlib import Path

import pytest

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.files import classify_rinex_file, detect_rinex_nav_systems
from um980_rtklib_pipeline.rinex_nav import extract_rover_nav, rover_nav_files
from um980_rtklib_pipeline.stream import parse_stream, unicore_binary_crc32, unicore_crc32


def _ascii_record(body: bytes) -> bytes:
    crc = unicore_crc32(body)
    return b"#" + body + f"*{crc:08X}\r\n".encode("ascii")


def test_legacy_rinex2_gps_nav_suffix_is_detected_as_gps(tmp_path: Path) -> None:
    nav = tmp_path / "auto1570.26n"
    nav.write_text(
        "     2.11           NAVIGATION DATA                         RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        " 1 26  6  6  0  0  0.0 0.0 0.0 0.0\n",
        encoding="ascii",
    )

    assert classify_rinex_file(nav) == "nav"
    assert detect_rinex_nav_systems(nav) == {"G"}


GPS_LINE = _ascii_record(
    b"GPSEPHA,40,GPS,UNKNOWN,1,1000,0,0,18,8;"
    b"9,57600.0,0,102,102,2419,2419,64800.0,2.656100068e+07,4.199103481e-09,"
    b"-6.360265004e-01,3.4293958452e-03,2.0365883709e+00,-1.421198249e-06,"
    b"1.144409180e-05,1.60781250e+02,-2.62812500e+01,-1.341104507e-07,"
    b"2.793967724e-08,9.6581392822e-01,6.178828802e-10,-2.530286309e+00,"
    b"-7.69210612e-09,102,64800.0,4.656612873e-10,7.4896030e-04,"
    b"-4.4337867e-12,0.0000000e+00,TRUE,1.458528010e-04,4.00000000e+00"
)

GLO_LINE = _ascii_record(
    b"GLOEPHA,85,GPS,FINE,2419,113218100,0,0,18,9;"
    b"51,0,1,0,2419,112518000,10782,869,0,0,41,0,2.301446044921875e+07,"
    b"8.457608886718750e+06,7.004804199218750e+06,9.020080566406250e+02,"
    b"3.420991897583008e+02,-3.387286186218262e+03,-0.000002793967724,"
    b"2.793967723846436e-06,9.313225746154785e-07,-3.252550959587097e-05,"
    b"5.587935448e-09,0.000000000000000e+00,37590,2,2,0,12"
)

BD3_LINE = _ascii_record(
    b"BD3EPHA,77,GPS,FINE,2211,180091000,0,0,18,4;"
    b"44,0,3,15,21,21,2211,2211,176400.0,176400.0,-1.423828125e+01,"
    b"1.108884811e-02,3.726583799e-09,-1.069685670e-13,1.309681137e+00,"
    b"8.019023808e-04,6.109550176e-01,2.244487405e-07,8.259899914e-06,"
    b"1.940156250e+02,6.187500000e+00,1.210719347e-08,7.450580597e-09,"
    b"9.593903595e-01,-4.500187451e-11,1.952617584e+00,6.803497679e-09,"
    b"176400.0,-2.153683454e-09,-1.199077815e-08,0.000000000e+00,"
    b"0.000000000e+00,0.000000000e+00,-2.910383046e-10,6.693656906e-04,"
    b"1.219113699e-11,0.000000000e+00,588,0,27,0,7,0,0,1"
)


def _binary_frame(message_id: int, payload: bytes, week: int = 2419, tow_ms: int = 64800000) -> bytes:
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    struct.pack_into("<H", header, 4, message_id)
    struct.pack_into("<H", header, 6, len(payload))
    struct.pack_into("<H", header, 10, week)
    struct.pack_into("<I", header, 12, tow_ms)
    body = bytes(header) + payload
    return body + unicore_binary_crc32(body).to_bytes(4, "little")


def _gps_like_payload(prn: int = 9, week: int = 2419, health: int = 0) -> bytes:
    payload = bytearray(224)
    for offset, fmt, value in (
        (0, "<I", prn),
        (4, "<d", 57600.0),
        (12, "<I", health),
        (16, "<I", 102),
        (20, "<I", 102),
        (24, "<I", week),
        (28, "<I", week),
        (32, "<d", 64800.0),
        (40, "<d", 2.656100068e7),
        (48, "<d", 4.199103481e-9),
        (56, "<d", -6.360265004e-1),
        (64, "<d", 3.4293958452e-3),
        (72, "<d", 2.0365883709),
        (80, "<d", -1.421198249e-6),
        (88, "<d", 1.144409180e-5),
        (96, "<d", 160.78125),
        (104, "<d", -26.28125),
        (112, "<d", -1.341104507e-7),
        (120, "<d", 2.793967724e-8),
        (128, "<d", 9.6581392822e-1),
        (136, "<d", 6.178828802e-10),
        (144, "<d", -2.530286309),
        (152, "<d", -7.69210612e-9),
        (160, "<I", 102),
        (164, "<d", 64800.0),
        (172, "<d", 4.656612873e-10),
        (180, "<d", 7.4896030e-4),
        (188, "<d", -4.4337867e-12),
        (196, "<d", 0.0),
        (204, "<I", 1),
        (208, "<d", 1.458528010e-4),
        (216, "<d", 4.0),
    ):
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def _glonass_payload() -> bytes:
    payload = bytearray(144)
    for offset, fmt, value in (
        (0, "<H", 51),
        (2, "<H", 7),
        (6, "<H", 2419),
        (8, "<I", 64800000),
        (12, "<I", 0),
        (24, "<I", 0),
        (28, "<d", 2.301446044921875e7),
        (36, "<d", 8.45760888671875e6),
        (44, "<d", 7.00480419921875e6),
        (52, "<d", 902.008056640625),
        (60, "<d", 342.0991897583008),
        (68, "<d", -3387.286186218262),
        (76, "<d", -2.793967724e-6),
        (84, "<d", 2.793967724e-6),
        (92, "<d", 9.313225746e-7),
        (100, "<d", -3.252550959e-5),
        (108, "<d", 5.587935448e-9),
        (116, "<d", 0.0),
        (124, "<I", 64800),
        (136, "<I", 12),
    ):
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def _galileo_payload() -> bytes:
    payload = bytearray(220)
    for offset, fmt, value in (
        (0, "<I", 11),
        (8, "<I", 1),
        (18, "<B", 1),
        (20, "<I", 77),
        (24, "<I", 64800),
        (28, "<d", math.sqrt(2.96e7)),
        (36, "<d", 4.0e-9),
        (44, "<d", -0.6),
        (52, "<d", 0.01),
        (60, "<d", 2.0),
        (68, "<d", -1.0e-6),
        (76, "<d", 1.0e-5),
        (84, "<d", 160.0),
        (92, "<d", -26.0),
        (100, "<d", -1.0e-7),
        (108, "<d", 2.0e-8),
        (116, "<d", 0.96),
        (124, "<d", 6.0e-10),
        (132, "<d", -2.5),
        (140, "<d", -7.0e-9),
        (176, "<I", 64800),
        (180, "<d", 7.0e-4),
        (188, "<d", -4.0e-12),
        (196, "<d", 0.0),
        (204, "<d", 1.0e-9),
        (212, "<d", 2.0e-9),
    ):
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def _bds_payload(prn: int = 12) -> bytes:
    payload = bytearray(232)
    payload[:172] = _gps_like_payload(prn=prn, week=2419)[:172]
    for offset, fmt, value in (
        (172, "<d", 4.0e-10),
        (180, "<d", 5.0e-10),
        (188, "<d", 7.0e-4),
        (196, "<d", -4.0e-12),
        (204, "<d", 0.0),
        (212, "<I", 1),
        (216, "<d", 1.0e-4),
        (224, "<d", 4.0),
    ):
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def _bd3_payload(freq_type: int, prn: int = 24) -> bytes:
    payload = bytearray(264)
    for offset, fmt, value in (
        (0, "<B", prn),
        (2, "<B", 3),
        (3, "<B", 2),
        (4, "<H", 40),
        (6, "<H", 41),
        (8, "<H", 2419),
        (10, "<H", 2419),
        (12, "<d", 57600.0),
        (20, "<d", 64800.0),
        (28, "<d", 0.0),
        (44, "<d", 4.0e-9),
        (60, "<d", -0.6),
        (68, "<d", 0.01),
        (76, "<d", 2.0),
        (84, "<d", -1.0e-6),
        (92, "<d", 1.0e-5),
        (100, "<d", 160.0),
        (108, "<d", -26.0),
        (116, "<d", -1.0e-7),
        (124, "<d", 2.0e-8),
        (132, "<d", 0.96),
        (140, "<d", 6.0e-10),
        (148, "<d", -2.5),
        (156, "<d", -7.0e-9),
        (164, "<d", 64800.0),
        (172, "<d", 1.0e-9),
        (180, "<d", 2.0e-9),
        (188, "<d", 3.0e-9),
        (196, "<d", 4.0e-9),
        (220, "<d", 7.0e-4),
        (228, "<d", -4.0e-12),
        (244, "<i", 0),
        (248, "<B", 2),
        (249, "<B", 2),
        (250, "<B", 2),
        (251, "<B", 2),
        (260, "<I", freq_type),
    ):
        struct.pack_into(fmt, payload, offset, value)
    return bytes(payload)


def test_ascii_ephemeris_writes_nav_and_gnav(tmp_path: Path):
    output = tmp_path / "rover.rover-gps.nav"
    records, _ = parse_stream(GPS_LINE + GLO_LINE)
    report = extract_rover_nav(records, output)

    gnav = tmp_path / "rover.rover-glo.gnav"
    assert report.found["GPSEPHA"] == 1
    assert report.found["GLOEPHA"] == 1
    assert report.converted["GPSEPHA"] == 1
    assert report.converted["GLOEPHA"] == 1
    assert output.exists()
    assert gnav.exists()
    assert classify_rinex_file(output) == "nav"
    assert classify_rinex_file(gnav) == "nav"
    assert "G09 2026 05 17 18 00 00" in output.read_text(encoding="ascii")
    assert "R14 2026 05 18 07 15 00" in gnav.read_text(encoding="ascii")
    assert rover_nav_files(output) == [output, gnav]


def test_rinex_command_writes_nav_even_when_obs_is_unavailable(tmp_path: Path):
    rover = tmp_path / "rover.unc"
    rover.write_bytes(GPS_LINE)
    args = argparse.Namespace(
        rover_log=str(rover),
        out_dir=str(tmp_path / "out"),
        basename="rover",
        obs_csv=False,
        rinex_compat="native",
        rinex_version="3.04",
        analysis_json=True,
        verbose=False,
        log_file=None,
    )

    with pytest.raises(ValueError, match="no observations decoded"):
        cli.cmd_rinex(args)

    assert (tmp_path / "out" / "rover.rover-gps.nav").exists()
    assert (tmp_path / "out" / "rover.analysis.json").exists()


def test_ascii_ephemeris_reports_convertible_records_without_writing():
    records, _ = parse_stream(GPS_LINE + GLO_LINE)
    report = extract_rover_nav(records)
    assert report.converted["GPSEPHA"] == 1
    assert report.converted["GLOEPHA"] == 1
    assert not any("no valid GPS RINEX NAV records" in warning for warning in report.warnings)


def test_ascii_bd3ephemeris_writes_cnav_sidecar(tmp_path: Path):
    output = tmp_path / "rover.rover-gps.nav"
    records, _ = parse_stream(BD3_LINE)
    report = extract_rover_nav(records, output)
    cnav = tmp_path / "rover.rover-bds.cnav"

    assert report.found["BD3EPHA"] == 1
    assert report.converted["BD3EPHA"] == 1
    assert cnav.exists()
    assert "C44 2022 05 24 01 00 00" in cnav.read_text(encoding="ascii")
    assert not any("BD3EPHA records found; conversion not yet implemented" in warning for warning in report.warnings)


def test_malformed_gpsepha_does_not_write_empty_nav_file(tmp_path: Path):
    nav_path = tmp_path / "rover.rover-gps.nav"
    records, _ = parse_stream(_ascii_record(b"GPSEPHA,COM1,0,0;dummy"))
    report = extract_rover_nav(records, nav_path)
    assert report.found["GPSEPHA"] == 1
    assert report.converted["GPSEPHA"] == 0
    assert not nav_path.exists()
    assert any("no valid GPS RINEX NAV records" in warning for warning in report.warnings)


def test_rtklib_sbs_message_shape_writes_sbs_file(tmp_path: Path):
    nav_path = tmp_path / "rover.rover-gps.nav"
    raw = _ascii_record(
        b"SBSMSGA,GPS,FINE,2419,0;2419,64800,123,0000000000000000000000000000000000000000000000000000000000"
    )
    records, _ = parse_stream(raw)
    report = extract_rover_nav(records, nav_path)
    sbs = tmp_path / "rover.rover-sbas.sbs"
    assert report.found["SBSMSG"] == 1
    assert report.converted["SBSMSG"] == 1
    assert sbs.exists()
    assert classify_rinex_file(sbs) == "sbs"


def test_sbas_source_off_filters_sbs_sidecars(tmp_path: Path):
    nav = tmp_path / "rover.nav"
    sbs = tmp_path / "rover.sbs"
    nav.write_text("     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n", encoding="ascii")
    sbs.write_text("2419  64800 123  0 : 0000000000000000000000000000000000000000000000000000000000\n", encoding="ascii")

    filtered = cli._apply_sbas_source_policy(argparse.Namespace(sbas_source="off", sbas_file=None), [nav, sbs])
    assert filtered == [nav]


def test_sbas_source_external_adds_explicit_sbs_file(tmp_path: Path):
    nav = tmp_path / "rover.nav"
    sbs = tmp_path / "external.sbs"
    nav.write_text("     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n", encoding="ascii")
    sbs.write_text("2419  64800 123  0 : 0000000000000000000000000000000000000000000000000000000000\n", encoding="ascii")

    selected = cli._apply_sbas_source_policy(
        argparse.Namespace(sbas_source="external", sbas_file=[str(sbs)]),
        [nav],
    )
    assert selected == [nav, sbs]


def test_binary_gpsephb_writes_nav_file(tmp_path: Path):
    output = tmp_path / "rover.rover-gps.nav"
    records, _ = parse_stream(_binary_frame(106, _gps_like_payload()))
    report = extract_rover_nav(records, output)
    assert report.found["GPSEPHB"] == 1
    assert report.converted["GPSEPHB"] == 1
    assert output.exists()
    assert "G09 2026 05 17 18 00 00" in output.read_text(encoding="ascii")
    assert not any("GPSEPHB binary records found" in warning for warning in report.warnings)


def test_all_binary_ephemeris_families_write_nav_sidecars(tmp_path: Path):
    output = tmp_path / "rover.rover-gps.nav"
    data = b"".join(
        (
            _binary_frame(106, _gps_like_payload(prn=9)),
            _binary_frame(110, _gps_like_payload(prn=1)),
            _binary_frame(107, _glonass_payload()),
            _binary_frame(109, _galileo_payload()),
            _binary_frame(108, _bds_payload()),
            _binary_frame(2999, _bd3_payload(freq_type=1)),
            _binary_frame(112, _gps_like_payload(prn=5)),
        )
    )
    records, _ = parse_stream(data)
    report = extract_rover_nav(records, output)

    assert report.converted["GPSEPHB"] == 1
    assert report.converted["QZSSEPHB"] == 1
    assert report.converted["GLOEPHB"] == 1
    assert report.converted["GALEPHB"] == 1
    assert report.converted["BDSEPHB"] == 1
    assert report.converted["BD3EPHB"] == 1
    assert report.converted["IRNSSEPHB"] == 1
    assert output.exists()
    assert (tmp_path / "rover.rover-glo.gnav").exists()
    assert (tmp_path / "rover.rover-gal.lnav").exists()
    assert (tmp_path / "rover.rover-bds.cnav").exists()
    assert (tmp_path / "rover.rover-irn.inav").exists()
    assert "J193 2026 05 17 18 00 00" in output.read_text(encoding="ascii")


def test_bd3_binary_frequency_variants_are_collapsed_for_rtklib_nav(tmp_path: Path):
    output = tmp_path / "rover.rover-gps.nav"
    records, _ = parse_stream(
        _binary_frame(2999, _bd3_payload(freq_type=1)) + _binary_frame(2999, _bd3_payload(freq_type=2))
    )
    report = extract_rover_nav(records, output)
    cnav = tmp_path / "rover.rover-bds.cnav"
    assert report.found["BD3EPHB"] == 2
    assert report.converted["BD3EPHB"] == 1
    assert cnav.read_text(encoding="ascii").count("C24 2026 05 17 18 00 00") == 1
    assert any("frequency variants" in warning for warning in report.warnings)
