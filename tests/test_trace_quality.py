from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from um980_rtklib_pipeline import rtklib
from um980_rtklib_pipeline.rtklib import run_rnx2rtkp
from um980_rtklib_pipeline.trace_quality import analyze_rtklib_trace


def _minimal_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    rover = tmp_path / "rover.obs"
    base = tmp_path / "base.obs"
    nav = tmp_path / "nav.rnx"
    conf = tmp_path / "rtk.conf"
    output = tmp_path / "out.pos"
    obs_header = "     3.04           OBSERVATION DATA    M                   RINEX VERSION / TYPE\n"
    rover.write_text(obs_header, encoding="ascii")
    base.write_text(obs_header, encoding="ascii")
    nav.write_text(
        "     3.04           NAVIGATION DATA     G                   RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n"
        "G01 2026 05 20 00 00 00 0.0 0.0 0.0\n",
        encoding="ascii",
    )
    conf.write_text("pos1-posmode =kinematic\n", encoding="ascii")
    return rover, base, nav, conf, output


def test_trace_parser_streams_and_counts_events(tmp_path: Path):
    trace = tmp_path / "rnx2rtkp.trace"
    trace.write_text(
        "\n".join(
            [
                "resamb: ratio=2.8 thres=3.0",
                "lambda ambiguity fix",
                "ambiguity hold",
                "ambiguity reset",
                "cycle slip detected G01",
                "LLI lock reset",
                "reject large residual postfit",
                "no common satellite between rover and base",
                "no ephemeris G12",
                "time difference dt=0.5 base",
                "warning: interpolate base obs",
            ]
        ),
        encoding="ascii",
    )

    summary = analyze_rtklib_trace(trace, max_example_lines=1)

    assert summary["available"] is True
    counters = summary["counters"]
    assert counters["ar_ratio_lines"] == 1
    assert counters["lambda_lines"] == 1
    assert counters["ambiguity_fix_lines"] == 1
    assert counters["ambiguity_hold_lines"] == 1
    assert counters["ambiguity_reset_lines"] == 1
    assert counters["cycle_slip_lines"] == 1
    assert counters["observation_rejection_lines"] == 1
    assert counters["missing_ephemeris_lines"] == 1
    assert summary["numeric"]["ar_ratio"]["lt_3_0"] == 1
    assert len(summary["examples"]["cycle_slip_lines"]) == 1


def test_trace_parser_extracts_typed_timestamped_events(tmp_path: Path):
    trace = tmp_path / "typed.trace"
    trace.write_text(
        "\n".join(
            [
                "2026/05/30 05:02:10.000 resamb: ratio=2.8 thres=3.0 G01 L1",
                "2026/05/30 05:02:10.000 cycle slip detected G01 L1",
                "2026/05/30 05:02:11.000 reject large residual postfit G02 C1C",
                "2026/05/30 05:02:12.000 time difference dt=1.5 base",
            ]
        ),
        encoding="ascii",
    )

    summary = analyze_rtklib_trace(trace)
    events = summary["events"]

    assert events["counts_by_type"]["ar_ratio"] == 1
    assert events["counts_by_type"]["cycle_slip"] == 1
    assert events["counts_by_type"]["observation_rejection"] == 1
    assert events["counts_by_type"]["residual_outlier"] == 1
    assert events["counts_by_type"]["base_rover_time_issue"] == 1
    assert events["timestamped_event_times"] == 3
    first = events["event_time_aggregates"][0]
    assert first["ar_ratio_min"] == 2.8
    assert first["ar_threshold"] == 3.0
    assert first["sats"]["G01"] >= 1


def test_trace_parser_extracts_time_of_day_events(tmp_path: Path):
    trace = tmp_path / "tod.trace"
    trace.write_text(
        "\n".join(
            [
                "2 05:10:30.40: ambiguity validation failed (nb=12 ratio=2.1 thres=3.0 s1=1.2 s2=2.3)",
                "2 05:10:31.00: slip detected GF jump G12 L1 dGF=0.24",
            ]
        ),
        encoding="ascii",
    )

    summary = analyze_rtklib_trace(trace)
    events = summary["events"]

    assert events["timestamped_event_times"] == 2
    assert events["counts_by_type"]["ambiguity_validation_failed"] == 1
    assert events["counts_by_type"]["cycle_slip"] == 1
    first = events["event_time_aggregates"][0]
    assert first["time_basis"] == "time_of_day"
    assert first["nb"] == 12
    assert first["ar_ratio_min"] == 2.1
    assert first["ar_threshold"] == 3.0


def test_trace_parser_reads_full_file_by_default_and_marks_caps(tmp_path: Path):
    trace = tmp_path / "large.trace"
    trace.write_text("".join("resamb: ratio=3.2\n" for _ in range(1200)), encoding="ascii")
    crlf_trace = tmp_path / "crlf.trace"
    crlf_trace.write_bytes(b"resamb: ratio=3.2\r\n" * 10)

    full = analyze_rtklib_trace(trace)
    capped = analyze_rtklib_trace(trace, max_bytes=100)
    crlf = analyze_rtklib_trace(crlf_trace)

    assert full["trace_lines_read"] == 1200
    assert full["trace_truncated"] is False
    assert full["trace_bytes_read"] == full["trace_file_size_bytes"]
    assert full["trace_raw_bytes_read"] == full["trace_file_size_bytes"]
    assert full["trace_decoded_chars_read"] == full["trace_file_size_bytes"]
    assert full["trace_parse_elapsed_s"] >= 0.0
    assert capped["trace_truncated"] is True
    assert capped["trace_bytes_read"] <= 100
    assert crlf["trace_raw_bytes_read"] == crlf_trace.stat().st_size
    assert crlf["trace_lines_read"] == 10


def test_trace_parser_ignores_unknown_and_malformed_lines(tmp_path: Path):
    trace = tmp_path / "trace.log"
    trace.write_bytes(b"\xff\xfeunknown\nratio=not-a-number\n")

    summary = analyze_rtklib_trace(trace)

    assert summary["available"] is True
    assert summary["numeric"]["ar_ratio"]["count"] == 0


def test_traced_rnx2rtkp_adds_trace_level_and_uses_temp_cwd(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")
        Path(kwargs["cwd"], "rnx2rtkp.trace").write_text(
            "".join("resamb: ratio=3.2\n" for _ in range(1001)) + "cycle slip\n",
            encoding="ascii",
        )
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="temporary",
        dry_run=False,
    )

    assert "-x" in command.args
    assert command.args[command.args.index("-x") + 1] == "3"
    assert seen["cwd"] is not None
    assert command.trace_generated_temporarily is True
    assert command.trace_retained is False
    assert command.trace_summary["counters"]["cycle_slip_lines"] == 1
    assert command.trace_summary["trace_lines_read"] == 1002
    assert command.trace_summary["trace_truncated"] is False
    assert not Path(seen["cwd"]).exists()


def test_temporary_trace_prefers_solution_default_over_temp_cwd_and_deletes_it(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        Path(kwargs["cwd"], "rnx2rtkp.trace").write_text("cycle slip tiny temp\n", encoding="ascii")
        Path(str(output) + ".trace").write_text("".join("resamb: ratio=3.2\n" for _ in range(1000)), encoding="ascii")
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="temporary",
        dry_run=False,
    )

    summary = command.trace_summary
    assert summary["selected_trace_source"] == "solution-default"
    assert summary["parsed_trace_path"] == str(Path(str(output) + ".trace").resolve())
    assert summary["trace_lines_read"] == 1000
    assert summary["counters"]["ar_ratio_lines"] == 1000
    assert summary["trace_deleted"] is True
    assert not Path(str(output) + ".trace").exists()
    assert not Path(seen["cwd"]).exists()


def test_temporary_trace_reports_failed_delete(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)

    def fake_run(args, **kwargs):
        Path(str(output) + ".trace").write_text("resamb: ratio=3.2\n", encoding="ascii")
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    def fake_delete(path):
        return False, "permission denied"

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)
    monkeypatch.setattr(rtklib, "_delete_trace_path", fake_delete)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="temporary",
        dry_run=False,
    )

    summary = command.trace_summary
    assert summary["trace_deleted"] is False
    assert summary["trace_cleanup_failed_paths"][str(Path(str(output) + ".trace").resolve())] == "permission denied"
    assert Path(str(output) + ".trace").exists()


def test_temporary_trace_does_not_delete_unmodified_preexisting_trace(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)
    preexisting = Path(str(output) + ".trace")
    preexisting.write_text("resamb: ratio=3.2\n", encoding="ascii")

    def fake_run(args, **kwargs):
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="temporary",
        dry_run=False,
    )

    summary = command.trace_summary
    assert summary["trace_deleted"] is False
    assert summary["trace_cleanup_attempted_paths"] == []
    assert summary["trace_cleanup_skipped_paths"][str(preexisting.resolve())] == "pre-existing trace was not modified by this run"
    assert preexisting.exists()


def test_keep_trace_retains_requested_file(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)
    retained = tmp_path / "kept.trace"

    def fake_run(args, **kwargs):
        Path(kwargs["cwd"], "rnx2rtkp.trace").write_text("ratio=4.1\n", encoding="ascii")
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="keep",
        trace_level=2,
        trace_file=retained,
        dry_run=False,
    )

    assert retained.exists()
    assert command.trace_retained is True
    assert command.trace_file == retained
    assert command.trace_effective_level == 2


def test_keep_trace_moves_solution_default_when_requested(tmp_path: Path, monkeypatch):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)
    retained = tmp_path / "retained-real.trace"

    def fake_run(args, **kwargs):
        Path(kwargs["cwd"], "rnx2rtkp.trace").write_text("cycle slip tiny temp\n", encoding="ascii")
        Path(str(output) + ".trace").write_text("".join("resamb: ratio=4.1\n" for _ in range(20)), encoding="ascii")
        output.write_text("%  GPST latitude(deg) longitude(deg) height(m) Q ns\n", encoding="ascii")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rtklib.subprocess, "run", fake_run)

    command = run_rnx2rtkp(
        rnx2rtkp="/bin/sh",
        rtkconf=conf,
        output_file=output,
        rover_obs=rover,
        base_obs=[base],
        nav_files=[nav],
        trace_mode="keep",
        trace_file=retained,
        dry_run=False,
    )

    assert command.trace_summary["selected_trace_source"] == "solution-default"
    assert command.trace_summary["parsed_trace_path"] == str(retained.resolve())
    assert command.trace_summary["trace_lines_read"] == 20
    assert retained.exists()
    assert not Path(str(output) + ".trace").exists()


def test_trace_level_zero_rejected_before_rtklib(tmp_path: Path):
    rover, base, nav, conf, output = _minimal_inputs(tmp_path)

    with pytest.raises(ValueError, match="greater than 0"):
        run_rnx2rtkp(
            rnx2rtkp="/bin/sh",
            rtkconf=conf,
            output_file=output,
            rover_obs=rover,
            base_obs=[base],
            nav_files=[nav],
            trace_mode="temporary",
            trace_level=0,
        )
