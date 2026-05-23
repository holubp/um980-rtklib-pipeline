import pytest

from um980_rtklib_pipeline.bitrate import (
    DEFAULT_EPH_BYTES,
    DEFAULT_EPH_RECORDS_PER_PERIOD,
    ephemeris_payload_rate,
    estimate_bitrate,
    raw_epoch_bytes,
)
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


def test_ascii_ephemeris_contributes_to_line_utilisation():
    no_eph = estimate_bitrate(
        baud=230400,
        nmea_rates_hz=NMEA_PRESETS["minimal"],
        raw_format="obsvmcmpb",
        raw_hz=1,
        expected_obs_per_epoch=100,
    )
    with_eph = estimate_bitrate(
        baud=230400,
        nmea_rates_hz=NMEA_PRESETS["minimal"],
        raw_format="obsvmcmpb",
        raw_hz=1,
        expected_obs_per_epoch=100,
        ephemeris_periods_s={name: 300 for name in DEFAULT_EPH_BYTES},
    )
    expected = sum(
        DEFAULT_EPH_BYTES[name] * DEFAULT_EPH_RECORDS_PER_PERIOD[name]
        for name in DEFAULT_EPH_BYTES
    ) / 300
    assert with_eph.ephemeris_bytes_per_s == pytest.approx(expected)
    assert with_eph.total_bytes_per_s > no_eph.total_bytes_per_s
    assert with_eph.line_rate_bits_per_s > no_eph.line_rate_bits_per_s


def test_binary_ephemeris_uses_satellite_record_counts():
    rate = ephemeris_payload_rate({"GPSEPHB": 300})
    assert rate == pytest.approx(
        DEFAULT_EPH_BYTES["GPSEPHB"] * DEFAULT_EPH_RECORDS_PER_PERIOD["GPSEPHB"] / 300
    )
