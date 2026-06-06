from __future__ import annotations

from pathlib import Path

from um980_rtklib_pipeline.capture_profiles import parse_capture_profile_text
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.stream import parse_stream
from um980_rtklib_pipeline.timing_completeness import analyze_timing_completeness


def _records(lines: list[str]):
    data = "\r\n".join(lines).encode("ascii") + b"\r\n"
    return parse_stream(data)[0]


def _profile(text: str):
    return parse_capture_profile_text("# enabled: true\n" + text, path=Path("synthetic.um980"))


def _time_text(seconds: float) -> str:
    whole = int(seconds)
    frac = seconds - whole
    hour = whole // 3600
    minute = (whole % 3600) // 60
    sec = whole % 60 + frac
    return f"{hour:02d}{minute:02d}{sec:05.2f}"


def _gga(seconds: float) -> str:
    return make_sentence(f"GNGGA,{_time_text(seconds)},5000.0000,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,,")


def _rmc(seconds: float) -> str:
    return make_sentence(f"GNRMC,{_time_text(seconds)},A,5000.0000,N,01400.0000,E,0.0,0.0,050626,,,A")


def _gst(seconds: float) -> str:
    return make_sentence(f"GNGST,{_time_text(seconds)},0.1,0.2,0.3,0.0,0.1,0.1,0.2")


def test_perfect_20hz_gga_stream_has_no_timing_loss() -> None:
    records = _records([_gga(index * 0.05) for index in range(200)])
    profile = _profile("expected_rate_hz: GNGGA=20\n")

    result = analyze_timing_completeness(records, profile=profile)
    gga = result.messages["GNGGA"]

    assert result.overall_timing_status == "pass"
    assert abs(gga["observed_rate_hz"] - 20.0) < 0.01
    assert gga["missing_epoch_count"] == 0
    assert gga["duplicate_epoch_count"] == 0
    assert 0.049 <= gga["max_receiver_time_gap_s"] <= 0.051


def test_20hz_gga_missing_one_epoch_is_marginal() -> None:
    records = _records([_gga(index * 0.05) for index in range(200) if index != 100])
    profile = _profile("expected_rate_hz: GNGGA=20\n")

    result = analyze_timing_completeness(records, profile=profile)
    gga = result.messages["GNGGA"]

    assert result.overall_timing_status == "marginal"
    assert gga["missing_epoch_count"] == 1
    assert 0.099 <= gga["max_receiver_time_gap_s"] <= 0.101


def test_20hz_gga_repeated_timestamp_counts_duplicate() -> None:
    lines = [_gga(index * 0.05) for index in range(20)]
    lines.insert(10, _gga(0.5))
    records = _records(lines)
    profile = _profile("expected_rate_hz: GNGGA=20\n")

    result = analyze_timing_completeness(records, profile=profile)
    gga = result.messages["GNGGA"]

    assert gga["duplicate_epoch_count"] == 1
    assert result.overall_timing_status == "marginal"


def test_mixed_gga_rmc_gst_20hz_are_measured_separately() -> None:
    lines: list[str] = []
    for index in range(40):
        t = index * 0.05
        lines.extend([_gga(t), _rmc(t), _gst(t)])
    records = _records(lines)
    profile = _profile("expected_rate_hz: GNGGA=20,GNRMC=20,GNGST=20\n")

    result = analyze_timing_completeness(records, profile=profile)

    assert result.overall_timing_status == "pass"
    assert abs(result.messages["GNGGA"]["observed_rate_hz"] - 20.0) < 0.01
    assert abs(result.messages["GNRMC"]["observed_rate_hz"] - 20.0) < 0.01
    assert abs(result.messages["GNGST"]["observed_rate_hz"] - 20.0) < 0.01


def _gsv(seconds: float, total: int = 3, omit: int | None = None) -> list[str]:
    lines = [_gga(seconds)]
    for index in range(1, total + 1):
        if index == omit:
            continue
        lines.append(make_sentence(f"GNGSV,{total},{index},10,01,20,100,35,02,30,120,36"))
    return lines


def test_gsv_multisentence_bursts_are_grouped_not_duplicates() -> None:
    records = _records([line for epoch in range(5) for line in _gsv(epoch)])
    profile = _profile("expected_rate_hz: GNGSV=1\n")

    result = analyze_timing_completeness(records, profile=profile)
    gsv = result.messages["GNGSV"]

    assert gsv["gsv_sentence_count"] == 15
    assert gsv["gsv_epoch_count"] == 5
    assert gsv["duplicate_epoch_count"] == 0
    assert gsv["incomplete_gsv_groups"] == 0


def test_gsv_incomplete_group_is_reported() -> None:
    records = _records([*_gsv(0), *_gsv(1, omit=2), *_gsv(2)])
    profile = _profile("expected_rate_hz: GNGSV=1\n")

    result = analyze_timing_completeness(records, profile=profile)
    gsv = result.messages["GNGSV"]

    assert gsv["incomplete_gsv_groups"] == 1
    assert result.overall_timing_status == "fail"


def test_gsa_same_epoch_constellation_rows_are_not_duplicate_epochs() -> None:
    lines: list[str] = []
    for epoch in range(3):
        lines.append(_gga(epoch))
        lines.append(make_sentence("GNGSA,A,3,01,02,03,,,,,,,,,,1.0,0.7,0.7"))
        lines.append(make_sentence("GNGSA,A,3,11,12,13,,,,,,,,,,1.1,0.8,0.8"))
    records = _records(lines)
    profile = _profile("expected_rate_hz: GNGSA=1\n")

    result = analyze_timing_completeness(records, profile=profile)
    gsa = result.messages["GNGSA"]

    assert gsa["duplicate_epoch_count"] == 0
    assert gsa["missing_epoch_count"] == 0


def test_low_frequency_grs_short_capture_is_medium_confidence_not_failure() -> None:
    records = _records([make_sentence("GPGRS,000000.00,1,0.1,0.2"), make_sentence("GPGRS,000030.00,1,0.1,0.2")])
    profile = _profile("expected_rate_hz: GPGRS=0.033333333\n")

    result = analyze_timing_completeness(records, profile=profile, capture_duration_s=30)
    grs = result.messages["GPGRS"]

    assert grs["confidence"] in {"medium", "high"}
    assert grs["missing_epoch_count"] == 0
    assert result.overall_timing_status == "pass"


def test_event_driven_absence_does_not_fail() -> None:
    records = _records([_gga(0), _gga(1)])
    profile = _profile("event_driven: GPSIONB,TROPINFOA\n")

    result = analyze_timing_completeness(records, profile=profile)

    assert result.messages["GPSIONB"]["event_driven"] is True
    assert result.messages["GPSIONB"]["timing_status"] == "pass"
    assert result.overall_timing_status == "pass"


def test_pppnav_adrnav_10s_interval_is_measured() -> None:
    lines = []
    for index in range(4):
        tow_s = index * 10
        lines.append(make_sentence(f"PPPNAVA,2421,{tow_s},SOL_COMPUTED,50.0,14.0"))
        lines.append(make_sentence(f"ADRNAVA,2421,{tow_s},SOL_COMPUTED,50.0,14.0"))
    records = _records(lines)
    profile = _profile("expected_rate_hz: PPPNAVA=0.1,ADRNAVA=0.1\n")

    result = analyze_timing_completeness(records, profile=profile)

    assert abs(result.messages["PPPNAVA"]["observed_rate_hz"] - 0.1) < 0.001
    assert abs(result.messages["ADRNAVA"]["observed_rate_hz"] - 0.1) < 0.001
