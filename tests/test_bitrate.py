from um980_rtklib_pipeline.bitrate import estimate_bitrate, raw_epoch_bytes
from um980_rtklib_pipeline.initgen import NMEA_PRESETS


def test_raw_format_sizes_ordered():
    assert raw_epoch_bytes("obsvma", 100) > raw_epoch_bytes("obsvmb", 100)
    assert raw_epoch_bytes("obsvmb", 100) > raw_epoch_bytes("obsvmcmpb", 100)


def test_binary_profiles_lower_than_ascii():
    ascii_est = estimate_bitrate(
        baud=230400,
        nmea_rates_hz=NMEA_PRESETS["solution-20hz"],
        raw_format="obsvma",
        raw_hz=2,
        expected_obs_per_epoch=100,
    )
    binary_est = estimate_bitrate(
        baud=230400,
        nmea_rates_hz=NMEA_PRESETS["solution-20hz"],
        raw_format="obsvmb",
        raw_hz=2,
        expected_obs_per_epoch=100,
    )
    compressed_est = estimate_bitrate(
        baud=230400,
        nmea_rates_hz=NMEA_PRESETS["solution-20hz"],
        raw_format="obsvmcmpb",
        raw_hz=2,
        expected_obs_per_epoch=100,
    )
    assert ascii_est.utilisation > binary_est.utilisation > compressed_est.utilisation

