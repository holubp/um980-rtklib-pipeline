import pytest

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.initgen import (
    ASCII_EPHEMERIS_WARNING,
    InitProfile,
    NMEA_PRESETS,
    UTC_MESSAGES,
    debug_ascii_ephemeris_policy,
    ephemeris_policy,
    render_init_script,
)


def test_init_generate_rover_ascii_ephemeris():
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
    assert "GPGRS 30" in script
    assert "GPGRS 30.03" not in script
    assert "SAVECONFIG" in script
    assert estimate.raw_bytes_per_s > 0


def test_init_generate_binary_ephemeris_uses_valid_message_names():
    profile = InitProfile(
        port="COM1",
        raw_format="obsvmcmpb",
        raw_hz=1,
        ephemeris=ephemeris_policy(
            "every=300",
            ["gps", "glo", "gal", "bds", "bd3", "qzss"],
            message_format="binary",
        ),
        ephemeris_format="binary",
    )
    script, estimate = render_init_script(profile)
    for message in ("GPSEPHB", "GLOEPHB", "GALEPHB", "BDSEPHB", "BD3EPHB", "QZSSEPHB"):
        assert f"{message} COM1 300" in script
    assert "GPSEPH COM1" not in script
    assert "without B are invalid" in script
    assert estimate.ephemeris_bytes_per_s > 0


def test_debug_ascii_ephemeris_generates_all_commands_and_warning():
    profile = InitProfile(
        port="COM1",
        baud=230400,
        mode="rover",
        nmea=NMEA_PRESETS["minimal"],
        raw_format="obsvmcmpb",
        raw_hz=1,
        ephemeris=debug_ascii_ephemeris_policy(),
        debug_ascii_ephemeris=True,
    )
    script, estimate = render_init_script(profile)
    for message in ("GPSEPHA", "GLOEPHA", "GALEPHA", "BDSEPHA", "BD3EPHA", "QZSSEPHA"):
        assert f"{message} COM1 300" in script
    assert "Estimated ephemeris payload:" in script
    assert ASCII_EPHEMERIS_WARNING in script
    assert estimate.ephemeris_bytes_per_s > 0


def test_cli_debug_ascii_ephemeris_shortcut_selects_all_systems():
    parser = cli.build_parser()
    args = parser.parse_args(["init", "generate", "--debug-ascii-ephemeris"])
    profile = cli._profile_from_args(args)
    assert profile.debug_ascii_ephemeris
    assert profile.ephemeris == debug_ascii_ephemeris_policy()


def test_cli_binary_ephemeris_format_selects_b_messages():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["init", "generate", "--ephemeris", "every=300", "--ephemeris-format", "binary"]
    )
    profile = cli._profile_from_args(args)
    assert profile.ephemeris_format == "binary"
    assert "GPSEPHB" in profile.ephemeris
    assert "GPSEPHA" not in profile.ephemeris


def test_ppp_defaults_and_tropinfo_emit_selected_ascii_format():
    profile = InitProfile(ppp="e6-has", include_tropinfo=True)
    script, _ = render_init_script(profile)
    assert "CONFIG PPP TIMEOUT 120" in script
    assert "CONFIG PPP CONVERGE 15 30" in script
    assert "TROPINFOA ONCE" in script
    assert "TROPINFOA ONCHANGED" in script
    assert "TROPINFOB ONCE" not in script
    assert "TROPINFOB ONCHANGED" not in script


def test_binary_diagnostics_use_b_suffix_only():
    profile = InitProfile(
        ppp="e6-has",
        include_tropinfo=True,
        diagnostic_format="binary",
        ion_messages=("gps", "gal"),
        ion_period_s=1,
    )
    script, _ = render_init_script(profile)
    assert "TROPINFOB ONCE" in script
    assert "TROPINFOB ONCHANGED" in script
    assert "TROPINFOA" not in script
    assert "GPSIONB ONCHANGED" in script
    assert "GPSIONB 1" in script
    assert "GALIONB ONCHANGED" in script
    assert "GALIONB 1" in script
    assert "GPSIONA" not in script


def test_tropinfo_requires_ppp():
    with pytest.raises(ValueError, match="requires PPP"):
        render_init_script(InitProfile(include_tropinfo=True))


def test_ion_messages_emit_suffixed_onchanged_without_once():
    profile = InitProfile(ion_messages=("gps", "bds", "bd3", "gal"))
    script, _ = render_init_script(profile)
    for message in ("GPSIONA", "BDSIONA", "BD3IONA", "GALIONA"):
        assert f"{message} ONCE" not in script
        assert f"{message} ONCHANGED" in script


def test_ion_period_adds_repeat_commands_and_bitrate():
    without_period = InitProfile(ion_messages=("gps",))
    with_period = InitProfile(ion_messages=("gps",), ion_period_s=300)
    script, estimate = render_init_script(with_period)
    _, baseline = render_init_script(without_period)
    assert "GPSIONA ONCE" not in script
    assert "GPSIONA ONCHANGED" in script
    assert "GPSIONA 300" in script
    assert estimate.nmea_bytes_per_s > baseline.nmea_bytes_per_s


def test_bestnav_and_utc_messages_are_rendered_with_selected_format():
    profile = InitProfile(
        nmea={},
        raw_format="obsvmcmpb",
        raw_hz=5,
        bestnav_format="binary",
        bestnav_hz=20,
        diagnostic_format="binary",
        utc_messages=("gps", "bds", "bd3", "gal"),
    )

    script, estimate = render_init_script(profile)

    assert "BESTNAVB COM1 0.05" in script
    assert "OBSVMCMPB COM1 0.2" in script
    for family in UTC_MESSAGES:
        assert f"{UTC_MESSAGES[family]['binary']} ONCHANGED" in script
    assert estimate.nmea_bytes_per_s > 0


def test_sbas_config_commands_are_rendered():
    profile = InitProfile(sbas="egnos", sbas_timeout_s=600)
    script, _ = render_init_script(profile)
    assert "CONFIG SBAS ENABLE EGNOS" in script
    assert "CONFIG SBAS TIMEOUT 600" in script


def test_sbas_off_is_explicit_default():
    script, _ = render_init_script(InitProfile())
    assert "CONFIG SBAS DISABLE" in script


def test_cli_sbas_options_set_profile():
    parser = cli.build_parser()
    args = parser.parse_args(["init", "generate", "--sbas", "egnos", "--sbas-timeout", "600"])
    profile = cli._profile_from_args(args)
    assert profile.sbas == "egnos"
    assert profile.sbas_timeout_s == 600


def test_invalid_sbas_timeout_fails():
    with pytest.raises(ValueError, match="SBAS timeout"):
        render_init_script(InitProfile(sbas="auto", sbas_timeout_s=30))


def test_invalid_ion_period_fails():
    with pytest.raises(ValueError, match="ionosphere repeat period"):
        render_init_script(InitProfile(ion_messages=("gps",), ion_period_s=0))


def test_cli_solution_hz_sets_gga_and_rmc_periods():
    parser = cli.build_parser()
    args = parser.parse_args(["init", "generate", "--nmea-preset", "none", "--solution-hz", "5"])
    profile = cli._profile_from_args(args)
    script, _ = render_init_script(profile)
    assert profile.nmea["GNGGA"] == 5
    assert profile.nmea["GNRMC"] == 5
    assert "GNGGA 0.2" in script
    assert "GNRMC 0.2" in script


def test_cli_ion_all_selects_all_families():
    parser = cli.build_parser()
    args = parser.parse_args(["init", "generate", "--include-ion", "--ion-period", "300"])
    profile = cli._profile_from_args(args)
    assert set(profile.ion_messages) == {"gps", "bds", "bd3", "gal"}
    assert profile.ion_period_s == 300


def test_cli_diagnostic_format_selects_binary_suffixes():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "init",
            "generate",
            "--ppp",
            "e6-has",
            "--include-tropinfo",
            "--include-ion",
            "--diagnostic-format",
            "binary",
        ]
    )
    profile = cli._profile_from_args(args)
    script, _ = render_init_script(profile)
    assert "TROPINFOB ONCE" in script
    assert "GPSIONB ONCHANGED" in script
    assert "TROPINFOA" not in script
    assert "GPSIONA" not in script


def test_presets_do_not_emit_receiver_rejected_disabled_nmea():
    profile = InitProfile(nmea=NMEA_PRESETS["solution-20hz"])
    script, _ = render_init_script(profile)
    assert "GNGLL" not in script
    assert "GNGNS" not in script
    assert "GNGST" not in script
    assert "GPGRS 30" in script


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
