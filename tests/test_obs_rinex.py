import struct
from pathlib import Path

from um980_rtklib_pipeline.obs_decode import Observation, decode_observations, write_observations_csv
from um980_rtklib_pipeline.rinex_obs import _format_obs_value, observations_for_rinex, write_rinex_obs
from um980_rtklib_pipeline.stream import parse_stream


def _binary_frame(message_id: int, payload: bytes, week: int = 2419, tow_ms: int = 132572000) -> bytes:
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    header[9] = 0
    struct.pack_into("<H", header, 4, message_id)
    struct.pack_into("<H", header, 6, len(payload))
    struct.pack_into("<H", header, 10, week)
    struct.pack_into("<I", header, 12, tow_ms)
    return bytes(header) + payload + bytes(4)


def _obsvmb_payload(entries: list[tuple[int, int, float, float, float, float, float, int]]) -> bytes:
    payload = bytearray(4 + len(entries) * 40)
    struct.pack_into("<I", payload, 0, len(entries))
    for index, (glo_freq, prn, psr, adr, doppler, cn0, lock_time, tracking) in enumerate(entries):
        offset = 4 + index * 40
        struct.pack_into("<H", payload, offset, glo_freq)
        struct.pack_into("<H", payload, offset + 2, prn)
        struct.pack_into("<d", payload, offset + 4, psr)
        struct.pack_into("<d", payload, offset + 12, adr)
        struct.pack_into("<H", payload, offset + 20, 50)
        struct.pack_into("<H", payload, offset + 22, 20)
        struct.pack_into("<f", payload, offset + 24, doppler)
        struct.pack_into("<H", payload, offset + 28, int(cn0 * 100))
        struct.pack_into("<H", payload, offset + 30, 0)
        struct.pack_into("<f", payload, offset + 32, lock_time)
        struct.pack_into("<I", payload, offset + 36, tracking)
    return bytes(payload)


def _twos_complement(value: int, bits: int) -> int:
    if value < 0:
        value += 1 << bits
    return value


def _obsvmcmpb_payload(entries: list[tuple[int, int, float, float, float, float, float, int]]) -> bytes:
    payload = bytearray(4 + len(entries) * 24)
    struct.pack_into("<I", payload, 0, len(entries))
    for index, (glo_freq, prn, psr, adr, doppler, cn0, lock_time, tracking) in enumerate(entries):
        doppler_raw = _twos_complement(round(doppler * 256), 28)
        psr_raw = round(psr * 128)
        adr_raw = _twos_complement(round(adr * 256), 32)
        lock_raw = round(lock_time * 32)
        packed = (
            tracking
            | (doppler_raw << 32)
            | (psr_raw << 60)
            | (adr_raw << 96)
            | (4 << 128)
            | (3 << 132)
            | (prn << 136)
            | (lock_raw << 144)
            | (round(cn0 - 20) << 165)
            | (glo_freq << 170)
        )
        payload[4 + index * 24 : 4 + (index + 1) * 24] = packed.to_bytes(24, "little")
    return bytes(payload)


def _observation(
    *,
    rinex_sat: str = "G01",
    sat_system: str = "GPS",
    week: int = 2419,
    tow: float = 132572.0,
) -> Observation:
    return Observation(
        gps_week=week,
        tow=tow,
        sat_system=sat_system,  # type: ignore[arg-type]
        sv=1,
        rinex_sat=rinex_sat,
        signal_name="L1 C/A",
        rinex_code="1C",
        band="1",
        pseudorange_m=20200000.0,
        carrier_phase_cycles=100.5,
        doppler_hz=-1200.0,
        cn0_dbhz=45.0,
        lock_time_s=10.0,
        half_cycle=None,
        lli=0,
        raw_tracking_status=0,
    )


def test_obsvma_subset_to_csv_and_rinex(tmp_path: Path):
    data = (
        b"#OBSVMA,COM1,0,0;OBSVMA,2400,1000.0,GPS,1,L1,20200000.0,100.5,-1200.0,45.0,10,7|"
        b"OBSVMA,2400,1000.0,Galileo,11,E1,23200000.0,200.5,-1000.0,42.0,9,8*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert len(decoded.observations) == 2
    assert not decoded.warnings
    csv_path = tmp_path / "obs.csv"
    rinex_path = tmp_path / "rover.obs"
    write_observations_csv(csv_path, decoded.observations)
    write_rinex_obs(rinex_path, decoded.observations)
    rinex = rinex_path.read_text()
    assert "OBSERVATION DATA" in rinex
    assert "SYS / # / OBS TYPES" in rinex
    assert "G01" in rinex
    assert "E11" in rinex


def test_rinex_writer_inverts_um980_carrier_phase_sign(tmp_path: Path):
    """RINEX carrier phase range should move with pseudorange range."""

    observations = [
        _observation(tow=132572.0),
        _observation(tow=132572.5),
    ]
    observations[0].pseudorange_m = 22580543.906
    observations[0].carrier_phase_cycles = -1221049.086
    observations[1].pseudorange_m = 22580205.539
    observations[1].carrier_phase_cycles = -1219271.090

    rinex_path = tmp_path / "phase-sign.obs"
    write_rinex_obs(rinex_path, observations, compatibility="convbin")
    lines = rinex_path.read_text(encoding="ascii").splitlines()
    observation_lines = [line for line in lines if line.startswith("G01")]
    code_values = [float(line[3:17]) for line in observation_lines]
    carrier_values = [float(line[19:33]) for line in observation_lines]

    assert len(code_values) == 2
    assert carrier_values == [1221049.086, 1219271.090]
    assert (carrier_values[1] - carrier_values[0]) * 0.190293672798 < 0
    assert code_values[1] - code_values[0] < 0


def test_native_rinex_filters_unknown_system_and_updates_epoch_count(tmp_path: Path):
    observations = [
        _observation(rinex_sat="G01", sat_system="GPS"),
        _observation(rinex_sat="U01", sat_system="Unknown"),
    ]
    assert [obs.rinex_sat for obs in observations_for_rinex(observations)] == ["G01"]

    rinex_path = tmp_path / "rover.obs"
    write_rinex_obs(rinex_path, observations)
    rinex = rinex_path.read_text(encoding="ascii")

    assert "U    " not in rinex
    assert "\nU01" not in rinex
    assert "> 2026 05 18 12 49 32.0000000  0  1" in rinex


def test_rinex_writer_adds_time_bounds_and_approx_position(tmp_path: Path):
    observations = [
        _observation(tow=132572.0),
        _observation(tow=132573.5),
    ]
    rinex_path = tmp_path / "rover.obs"
    write_rinex_obs(rinex_path, observations, approx_position=(1.0, 2.0, 3.0))
    rinex = rinex_path.read_text(encoding="ascii")

    assert "        1.0000        2.0000        3.0000" in rinex
    assert "APPROX POSITION XYZ" in rinex
    assert "2026    05    18    12    49   32.0000000     GPS" in rinex
    assert "TIME OF FIRST OBS" in rinex
    assert "2026    05    18    12    49   33.5000000     GPS" in rinex
    assert "TIME OF LAST OBS" in rinex


def test_rinex_observation_formatter_keeps_large_values_fixed_width():
    field = _format_obs_value(-1234567890.123, 0)
    assert len(field) == 16
    assert field == "-1234567890.12  "


def test_real_shape_obsvma_warns_when_signal_mapping_is_placeholder():
    data = (
        b"#OBSVMA,1,GPS,FINE,2419,132572000,0,0,18,1;"
        b"1,0,1,17661005.670,-92809209.627852,6,50,2037.974,5000,0,16.010,20181c23"
        b"*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert len(decoded.observations) == 1
    assert decoded.observations[0].rinex_sat == "G01"
    assert decoded.observations[0].rinex_code == "1C"
    assert decoded.observations[0].pseudorange_m == 17661005.670
    assert not any("tracking-status to RINEX signal mapping is incomplete" in warning for warning in decoded.warnings)


def test_obsvma_rejects_non_fine_receiver_time():
    data = (
        b"#OBSVMA,1,GPS,UNKNOWN,1,132572000,0,0,18,1;"
        b"1,0,1,17661005.670,-92809209.627852,6,50,2037.974,5000,0,16.010,20181c23"
        b"*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert decoded.observations == []
    assert decoded.unsupported_records["OBSVMA_TIME_UNKNOWN"] == 1


def test_binary_ephemeris_is_not_reported_as_undecoded_observation():
    header = bytearray(24)
    header[:3] = b"\xaa\x44\xb5"
    header[4:6] = (106).to_bytes(2, "little")
    header[6:8] = (224).to_bytes(2, "little")
    records, _ = parse_stream(bytes(header) + bytes(224) + bytes(4))
    decoded = decode_observations(records)
    assert decoded.unsupported_records == {}
    assert decoded.warnings == ["no raw observations were decoded"]


def test_obsvmb_binary_observations_decode_to_rinex(tmp_path: Path):
    phase_valid = 1 << 10
    pseudorange_valid = 1 << 12
    gps_l1 = phase_valid | pseudorange_valid | (0 << 16) | (0 << 21)
    glo_l2 = phase_valid | pseudorange_valid | (1 << 16) | (5 << 21)
    payload = _obsvmb_payload(
        [
            (0, 16, 20814342.474, -109380108.947785, 2648.551, 49.29, 11.71, gps_l1),
            (7, 52, 18851471.802, -100736546.989518, 626.918, 47.60, 9.20, glo_l2),
        ]
    )
    records, _ = parse_stream(_binary_frame(12, payload))
    decoded = decode_observations(records)
    assert decoded.unsupported_records == {}
    assert decoded.warnings == []
    assert [(obs.rinex_sat, obs.rinex_code) for obs in decoded.observations] == [("G16", "1C"), ("R15", "2C")]
    assert decoded.observations[0].pseudorange_m == 20814342.474
    assert decoded.observations[0].carrier_phase_cycles == -109380108.947785
    assert decoded.observations[0].doppler_hz == 2648.551025390625

    rinex_path = tmp_path / "obsvmb.obs"
    write_rinex_obs(rinex_path, decoded.observations, compatibility="convbin")
    rinex = rinex_path.read_text(encoding="ascii")
    assert "G16" in rinex
    assert "  109380108.948" in rinex
    assert "R15" in rinex


def test_obsvmcmpb_compressed_observations_decode_to_rinex(tmp_path: Path):
    phase_valid = 1 << 10
    pseudorange_valid = 1 << 12
    gps_l1 = phase_valid | pseudorange_valid | (0 << 16) | (0 << 21)
    glo_l2 = phase_valid | pseudorange_valid | (1 << 16) | (5 << 21)
    payload = _obsvmcmpb_payload(
        [
            (0, 18, 22580544.09375, -1219049.0859375, 3555.9375, 43.0, 13.625, gps_l1),
            (7, 48, 19030998.3125, -1032583.87890625, 654.1328125, 51.0, 13.1875, glo_l2),
        ]
    )
    records, _ = parse_stream(_binary_frame(138, payload, tow_ms=538181000))
    decoded = decode_observations(records)
    assert decoded.unsupported_records == {}
    assert decoded.warnings == []
    assert [(obs.rinex_sat, obs.rinex_code) for obs in decoded.observations] == [("G18", "1C"), ("R11", "2C")]
    assert decoded.observations[0].pseudorange_m == 22580544.09375
    assert decoded.observations[0].carrier_phase_cycles == -1219049.0859375
    assert decoded.observations[0].doppler_hz == 3555.9375
    assert decoded.observations[1].signal_name == "G2 C/A FCN=7"

    rinex_path = tmp_path / "obsvmcmpb.obs"
    write_rinex_obs(rinex_path, decoded.observations, compatibility="convbin")
    rinex = rinex_path.read_text(encoding="ascii")
    assert "G18" in rinex
    assert "    1219049.086" in rinex
    assert "R11" in rinex


def test_obsvma_tracking_status_keeps_multiple_signals_for_same_satellite(tmp_path: Path):
    data = (
        b"#OBSVMA,1,GPS,FINE,2419,132572000,0,0,18,1;"
        b"2,"
        b"0,16,20814342.474,-109380108.947785,28,50,2648.551,4929,0,11.710,20181c23,"
        b"0,16,20814337.133,-85231236.201565,227,534,2063.807,3272,0,6.760,21301c22"
        b"*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert [(obs.rinex_sat, obs.rinex_code) for obs in decoded.observations] == [
        ("G16", "1C"),
        ("G16", "2W"),
    ]
    rinex_path = tmp_path / "rover.obs"
    write_rinex_obs(rinex_path, decoded.observations)
    rinex = rinex_path.read_text()
    assert "G    8 C1C L1C D1C S1C C2W L2W D2W S2W" in rinex
    assert "  20814342.474" in rinex
    assert "  20814337.133" in rinex


def test_obsvma_tracking_status_decodes_glonass_offset_prn():
    data = (
        b"#OBSVMA,1,GPS,FINE,2419,132572000,0,0,18,1;"
        b"1,7,52,18851471.802,-100736546.989518,18,52,626.918,4760,0,9.200,00191c43"
        b"*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert len(decoded.observations) == 1
    assert decoded.observations[0].sat_system == "GLONASS"
    assert decoded.observations[0].rinex_sat == "R15"
    assert decoded.observations[0].rinex_code == "1C"


def test_convbin_compatibility_filters_unknown_and_orders_observation_types(tmp_path: Path):
    data = (
        b"#OBSVMA,1,GPS,FINE,2419,132572000,0,0,18,2;"
        b"2,"
        b"0,16,20814342.474,-109380108.947785,28,50,2648.551,4929,0,11.710,20181c23,"
        b"0,16,20814337.133,-85231236.201565,227,534,2063.807,3272,0,6.760,21301c22"
        b"*00000000\r\n"
    )
    records, _ = parse_stream(data)
    decoded = decode_observations(records)
    assert len(observations_for_rinex(decoded.observations, compatibility="convbin")) == 2
    rinex_path = tmp_path / "convbin.obs"
    write_rinex_obs(rinex_path, decoded.observations, compatibility="convbin")
    rinex = rinex_path.read_text()
    assert "G    8 C1C L1C D1C S1C C2W L2W D2W S2W" in rinex
    assert "> 2026 05 18 12 49 32.0000000  0  1" in rinex


def test_convbin_compatibility_orders_bds_like_rtklib(tmp_path: Path):
    phase_valid = 1 << 10
    pseudorange_valid = 1 << 12
    bds_system = 4 << 16
    payload = _obsvmb_payload(
        [
            (0, 24, 23705582.141, -123441188.198, -2394.975, 48.61, 10.0, phase_valid | pseudorange_valid | bds_system | (0 << 21)),
            (0, 24, 23705575.015, -95452520.030, -1851.951, 48.82, 10.0, phase_valid | pseudorange_valid | bds_system | (13 << 21)),
            (0, 24, 23705574.707, -93025766.391, -1804.952, 44.86, 10.0, phase_valid | pseudorange_valid | bds_system | (12 << 21)),
            (0, 24, 23705575.462, -100306045.312, -1946.250, 47.19, 10.0, phase_valid | pseudorange_valid | bds_system | (21 << 21)),
            (0, 24, 23705578.030, -124573653.404, -2416.996, 46.53, 10.0, phase_valid | pseudorange_valid | bds_system | (8 << 21)),
        ]
    )
    records, _ = parse_stream(_binary_frame(12, payload))
    decoded = decode_observations(records)
    assert [obs.rinex_code for obs in decoded.observations] == ["2I", "7P", "5P", "6I", "1P"]

    rinex_path = tmp_path / "bds.obs"
    write_rinex_obs(rinex_path, decoded.observations, compatibility="convbin")
    rinex = rinex_path.read_text(encoding="ascii")
    assert "C   20 C2I L2I D2I S2I C7P L7P D7P S7P C5P L5P D5P S5P C6I" in rinex
    assert "       L6I D6I S6I C1P L1P D1P S1P" in rinex
    assert max(len(line) for line in rinex.splitlines()) == 323
    assert "C24  23705582.141   123441188.198" in rinex

    native_path = tmp_path / "bds-native.obs"
    write_rinex_obs(native_path, decoded.observations)
    assert all(len(line) <= 80 for line in native_path.read_text(encoding="ascii").splitlines())
