from pathlib import Path

from um980_rtklib_pipeline.obs_decode import decode_observations, write_observations_csv
from um980_rtklib_pipeline.rinex_obs import write_rinex_obs
from um980_rtklib_pipeline.stream import parse_stream


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
