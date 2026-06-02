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


def test_trace_parser_reads_full_file_by_default_and_marks_caps(tmp_path: Path):
    trace = tmp_path / "large.trace"
    trace.write_text("".join("resamb: ratio=3.2\n" for _ in range(1200)), encoding="ascii")

    full = analyze_rtklib_trace(trace)
    capped = analyze_rtklib_trace(trace, max_bytes=100)

    assert full["trace_lines_read"] == 1200
    assert full["trace_truncated"] is False
    assert full["trace_bytes_read"] == full["trace_file_size_bytes"]
    assert capped["trace_truncated"] is True
    assert capped["trace_bytes_read"] <= 100


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
