import argparse
import logging
from pathlib import Path

from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.rtklib_summary import format_rtklib_solution_summary, summarize_rtklib_solution


def test_rtklib_pos_summary_counts_percentages_time_and_track(tmp_path: Path):
    output = tmp_path / "run.pos"
    output.write_text(
        "%  GPST latitude(deg) longitude(deg) height(m) Q ns\n"
        "2026/05/23 05:29:41.000 50.0000000 14.0000000 250.0 5 20\n"
        "2026/05/23 05:29:42.000 50.0001000 14.0000000 250.0 2 20\n"
        "2026/05/23 05:29:43.000 50.0002000 14.0000000 250.0 1 20\n",
        encoding="ascii",
    )

    summary = summarize_rtklib_solution(output)

    assert summary is not None
    assert summary.sample_count == 3
    assert summary.duration_s == 2.0
    assert summary.distance_m > 20.0
    assert summary.quality_system == "rtklib_q"
    assert [(bucket.quality, bucket.count, round(bucket.percent, 1)) for bucket in summary.buckets] == [
        (1, 1, 33.3),
        (2, 1, 33.3),
        (5, 1, 33.3),
    ]
    assert {bucket.quality: bucket.duration_s for bucket in summary.buckets} == {1: 1.0, 2: 1.0, 5: 0.0}


def test_rtklib_nmea_summary_uses_gga_quality_and_fractional_time(tmp_path: Path):
    output = tmp_path / "run.nmea"
    output.write_text(
        "$GNGGA,161708.50,5000.0000,N,01400.0000,E,5,20,0.7,250.0,M,45.0,M,0.5,0001*00\n"
        "$GNGGA,161709.00,5000.0010,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001*00\n"
        "$GNGGA,161709.50,5000.0020,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001*00\n",
        encoding="ascii",
    )

    summary = summarize_rtklib_solution(output)

    assert summary is not None
    assert summary.sample_count == 3
    assert summary.duration_s == 1.0
    assert summary.quality_system == "nmea_gga"
    assert [(bucket.quality, bucket.count) for bucket in summary.buckets] == [(4, 2), (5, 1)]
    assert {bucket.quality: bucket.duration_s for bucket in summary.buckets} == {4: 1.0, 5: 0.0}


def test_rtklib_summary_format_includes_epoch_percent_duration_and_track(tmp_path: Path):
    output = tmp_path / "run.pos"
    output.write_text(
        "2026/05/23 05:29:41.000 50.0000000 14.0000000 250.0 5 20\n"
        "2026/05/23 05:29:42.000 50.0001000 14.0000000 250.0 1 20\n",
        encoding="ascii",
    )

    summary = summarize_rtklib_solution(output)

    assert summary is not None
    lines = format_rtklib_solution_summary(summary)
    assert lines[0].startswith("RTKLIB solution summary: epochs=2 duration=1s track=")
    assert "RTKLIB Q=1 (fixed): 1 epochs (50.0%), duration=1s, track=" in lines[1]
    assert "RTKLIB Q=5 (single): 1 epochs (50.0%), duration=0s, track=0.0 m" == lines[2]


def test_rtklib_nmea_summary_format_uses_gga_quality_labels(tmp_path: Path):
    output = tmp_path / "run.nmea"
    output.write_text(
        "$GNGGA,161708.50,5000.0000,N,01400.0000,E,5,20,0.7,250.0,M,45.0,M,0.5,0001*00\n"
        "$GNGGA,161709.00,5000.0010,N,01400.0000,E,4,20,0.7,250.0,M,45.0,M,0.5,0001*00\n",
        encoding="ascii",
    )

    summary = summarize_rtklib_solution(output)

    assert summary is not None
    lines = format_rtklib_solution_summary(summary)
    assert "NMEA GGA quality=4 (rtk fixed): 1 epochs (50.0%)" in lines[1]
    assert "NMEA GGA quality=5 (rtk float): 1 epochs (50.0%)" in lines[2]


def test_cli_logs_rtklib_summary_only_in_verbose_mode(tmp_path: Path, caplog):
    output = tmp_path / "run.pos"
    output.write_text(
        "2026/05/23 05:29:41.000 50.0000000 14.0000000 250.0 5 20\n"
        "2026/05/23 05:29:42.000 50.0001000 14.0000000 250.0 1 20\n",
        encoding="ascii",
    )

    caplog.set_level(logging.INFO)
    cli._log_rtklib_solution_summary(argparse.Namespace(verbose=False, debug=False, dry_run=False), output)
    assert "RTKLIB solution summary" not in caplog.text

    cli._log_rtklib_solution_summary(argparse.Namespace(verbose=True, debug=False, dry_run=False), output)
    assert "RTKLIB solution summary: epochs=2" in caplog.text
    assert "RTKLIB Q=1 (fixed): 1 epochs (50.0%)" in caplog.text
