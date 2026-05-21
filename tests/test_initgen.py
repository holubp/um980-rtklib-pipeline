import pytest

from um980_rtklib_pipeline.initgen import InitProfile, NMEA_PRESETS, ephemeris_policy, render_init_script


def test_init_generate_rover_binary_ephemeris():
    profile = InitProfile(
        port="COM1",
        baud=230400,
        mode="rover",
        nmea=NMEA_PRESETS["solution-20hz"],
        raw_format="obsvmb",
        raw_hz=2,
        ephemeris=ephemeris_policy("every=300", ["gps", "glo", "gal"]),
        save_config=True,
    )
    script, estimate = render_init_script(profile)
    assert "MODE ROVER" in script
    assert "OBSVMB COM1 0.5" in script
    assert "GPSEPHA COM1 300" in script
    assert "GLOEPHA COM1 300" in script
    assert "GALEPHA COM1 300" in script
    assert "SAVECONFIG" in script
    assert estimate.raw_bytes_per_s > 0


def test_strict_overload_fails():
    profile = InitProfile(
        baud=230400,
        nmea=NMEA_PRESETS["solution-20hz"],
        raw_format="obsvma",
        raw_hz=5,
    )
    with pytest.raises(ValueError, match="estimated"):
        render_init_script(profile, strict_bitrate=True)


def test_base_mode_requires_coordinates():
    profile = InitProfile(mode="base")
    with pytest.raises(ValueError, match="base requires"):
        render_init_script(profile)

