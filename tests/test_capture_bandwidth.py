from pathlib import Path

from um980_rtklib_pipeline.capture_bandwidth import (
    PROFILE_FAMILY_DIRS,
    classify_cell,
    disabled_profile_rows,
    enabled_profiles,
    estimate_runtime,
    plan_for_stage,
    render_profile,
)
from um980_rtklib_pipeline.capture_profiles import parse_capture_profile


def test_plan_defaults_are_staged() -> None:
    smoke = plan_for_stage("smoke", duration_s=None, repeats=None)
    evidence = plan_for_stage("evidence", duration_s=None, repeats=None)

    assert smoke.duration_s == 25.0
    assert smoke.repeats == 1
    assert evidence.duration_s == 120.0
    assert evidence.repeats == 3


def test_disabled_profiles_are_reported_not_tested() -> None:
    rows = disabled_profile_rows(Path("tools/um980_profiles/runtime/bandwidth"))

    assert any(row["profile"] == "binary_rawobs_solution_20hz" for row in rows)
    assert all(row["classification"] == "NOT_TESTED" for row in rows)


def test_enabled_profiles_exclude_stress_by_default() -> None:
    profiles = enabled_profiles(Path("tools/um980_profiles/runtime/bandwidth"))
    names = {profile.path.stem for profile in profiles}

    assert "binary_solution_1hz" in names
    assert "mixed_nmea_navigation_binary_rawobs_10hz" not in names


def test_enabled_profiles_follow_staged_run_order() -> None:
    profiles = enabled_profiles(Path("tools/um980_profiles/runtime/bandwidth"))
    names = [profile.path.stem for profile in profiles]

    assert names[:4] == [
        "passive_current",
        "ascii_nmea_minimal_1hz",
        "ascii_unicore_solution_1hz",
        "binary_solution_1hz",
    ]
    assert names.index("binary_rawobs_solution_5hz") < names.index("mixed_nmea_minimal_binary_rawobs_5hz")


def test_ppp_has_profile_family_is_available() -> None:
    profiles = enabled_profiles(PROFILE_FAMILY_DIRS["ppp_has"])
    names = {profile.path.stem for profile in profiles}

    assert {"ppp_has_ascii_baseline", "ppp_has_binary_baseline", "ppp_has_mixed_baseline"} <= names
    ascii_profile = next(profile for profile in profiles if profile.path.stem == "ppp_has_ascii_baseline")
    assert "GNGGA=20" in ascii_profile.metadata["expected_rate_hz"]
    assert "TROPINFOA" in ascii_profile.metadata["event_driven"]


def test_render_profile_substitutes_baud(tmp_path: Path) -> None:
    profile = parse_capture_profile(Path("tools/um980_profiles/runtime/bandwidth/binary_solution_1hz.um980"))

    rendered = render_profile(profile, 230400, tmp_path)

    assert "CONFIG COM1 230400" in rendered.read_text(encoding="utf-8")


def test_classifier_is_conservative() -> None:
    label, reasons = classify_cell(
        {
            "validation_passed": True,
            "extract_check_passed": True,
            "expected_messages_missing": [],
            "bytes_total": 100000,
            "binary_crc_bad": 0,
            "nmea_checksum_bad": 0,
            "binary_resynchronisation_events": 0,
            "unknown_bytes": 0,
            "measured_vs_uart_payload_ratio": 0.2,
        }
    )

    assert label == "PROVISIONALLY_SAFE"
    assert "requires repeated evidence" in reasons[0]


def test_classifier_flags_errors() -> None:
    label, reasons = classify_cell({"capture_error": "device disconnected"})

    assert label == "UNSAFE"
    assert reasons == ["device disconnected"]


def test_classifier_uses_timing_failures() -> None:
    label, reasons = classify_cell(
        {
            "validation_passed": True,
            "extract_check_passed": True,
            "expected_messages_missing": [],
            "bytes_total": 100000,
            "binary_crc_bad": 0,
            "nmea_checksum_bad": 0,
            "binary_resynchronisation_events": 0,
            "unknown_bytes": 0,
            "measured_vs_uart_payload_ratio": 0.2,
            "timing_overall_status": "fail",
        }
    )

    assert label == "UNSAFE"
    assert "timing completeness failed" in reasons


def test_classifier_keeps_unsupported_timing_inconclusive() -> None:
    label, reasons = classify_cell(
        {
            "validation_passed": True,
            "extract_check_passed": True,
            "expected_messages_missing": [],
            "bytes_total": 100000,
            "binary_crc_bad": 0,
            "nmea_checksum_bad": 0,
            "binary_resynchronisation_events": 0,
            "unknown_bytes": 0,
            "measured_vs_uart_payload_ratio": 0.2,
            "timing_overall_status": "unsupported",
        }
    )

    assert label == "INCONCLUSIVE"
    assert "timing completeness unsupported" in reasons[0]


def test_runtime_estimate_counts_cells() -> None:
    profiles = enabled_profiles(Path("tools/um980_profiles/runtime/bandwidth"))[:2]

    estimate = estimate_runtime(("smoke",), profiles, 1.0, 1)

    assert estimate["cells"] == 8
    assert estimate["runtime_s"] == 8.0
