from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from um980_rtklib_pipeline.annotation import (
    AnnotationSegment,
    AnnotationTrack,
    RecordingAnnotation,
    RtkAnnotationRun,
    update_annotation_markdown,
    write_annotation_gpx,
)
from um980_rtklib_pipeline import cli
from um980_rtklib_pipeline.nmea import make_sentence
from um980_rtklib_pipeline.solution import SolutionPoint


def _point(seconds: float, lat: float, lon: float) -> SolutionPoint:
    whole = int(seconds)
    micros = int(round((seconds - whole) * 1_000_000))
    return SolutionPoint(
        time_utc=datetime(2026, 5, 30, 5, 20, whole, micros, tzinfo=timezone.utc),
        source="GGA",
        lat=lat,
        lon=lon,
        h_msl=250.0,
        fix_quality=4,
        fix_quality_text="rtk-fixed",
    )


def test_annotation_gpx_is_plain_multitrack_and_preserves_fractional_times(tmp_path: Path) -> None:
    out = tmp_path / "annotations.gpx"
    tracks = [
        AnnotationTrack("00 whole in-device rover_20260530050210", [_point(0.0, 50.0, 14.0)]),
        AnnotationTrack("30050210-052000-052600 in-device", [_point(0.2, 50.0001, 14.0001)]),
    ]

    write_annotation_gpx(out, tracks)

    text = out.read_text(encoding="utf-8")
    assert "<extensions" not in text
    assert "xmlns:um980" not in text
    assert "2026-05-30T05:20:00.200000Z" in text

    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.fromstring(text)
    assert [name.text for name in root.findall("g:trk/g:name", ns)] == [
        "00 whole in-device rover_20260530050210",
        "30050210-052000-052600 in-device",
    ]


def test_markdown_contains_codex_context_and_user_placeholders() -> None:
    markdown = update_annotation_markdown(
        None,
        recordings=[
            RecordingAnnotation(
                recording_id="rover_20260530050210",
                title="rover_20260530050210",
                codex_context="Moto route with substantial forest/canopy; expected weak fragmented fixed.",
            )
        ],
        segments=[
            AnnotationSegment(
                segment_id="30050210-052000-052600",
                recording_id="rover_20260530050210",
                start_time=datetime(2026, 5, 30, 5, 20, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 30, 5, 26, tzinfo=timezone.utc),
                label="weak/provisional forest moto",
                codex_context="Expected fragmented fixed and stressed diagnostics.",
            )
        ],
        rtk_runs=[],
    )

    assert "## Whole Recording: rover_20260530050210" in markdown
    assert "### Codex/GPT Generated Annotation" in markdown
    assert "Moto route with substantial forest/canopy" in markdown
    assert "### User Subjective Annotation" in markdown
    assert "- Actual capture conditions:" in markdown
    assert "## Segment: 30050210-052000-052600" in markdown
    assert "Expected fragmented fixed" in markdown


def test_markdown_rerun_updates_codex_block_without_touching_user_annotation() -> None:
    first = update_annotation_markdown(
        None,
        recordings=[
            RecordingAnnotation(
                recording_id="rover_20260531063148",
                title="rover_20260531063148",
                codex_context="Initial Codex context.",
            )
        ],
        segments=[],
        rtk_runs=[],
    )
    edited = first.replace("- Actual capture conditions:\n", "- Actual capture conditions: highway, open sky\n")

    second = update_annotation_markdown(
        edited,
        recordings=[
            RecordingAnnotation(
                recording_id="rover_20260531063148",
                title="rover_20260531063148",
                codex_context="Updated Codex context.",
            )
        ],
        segments=[],
        rtk_runs=[],
    )

    assert "Updated Codex context." in second
    assert "Initial Codex context." not in second
    assert "- Actual capture conditions: highway, open sky" in second


def test_running_without_rtk_runs_does_not_create_rtk_annotation_blocks() -> None:
    markdown = update_annotation_markdown(
        None,
        recordings=[
            RecordingAnnotation(
                recording_id="rover_20260531095025",
                title="rover_20260531095025",
                codex_context="Car/highway route.",
            )
        ],
        segments=[],
        rtk_runs=[],
    )

    assert "RTKLIB run" not in markdown


def test_rtk_run_blocks_can_be_scoped_to_current_recording() -> None:
    markdown = update_annotation_markdown(
        None,
        recordings=[
            RecordingAnnotation("rec-a", "rec-a", "context a"),
            RecordingAnnotation("rec-b", "rec-b", "context b"),
        ],
        segments=[
            AnnotationSegment(
                "seg-a",
                "rec-a",
                datetime(2026, 5, 30, 5, 20, tzinfo=timezone.utc),
                datetime(2026, 5, 30, 5, 21, tzinfo=timezone.utc),
                "a",
                "context",
            ),
            AnnotationSegment(
                "seg-b",
                "rec-b",
                datetime(2026, 5, 30, 5, 20, tzinfo=timezone.utc),
                datetime(2026, 5, 30, 5, 21, tzinfo=timezone.utc),
                "b",
                "context",
            ),
        ],
        rtk_runs=[RtkAnnotationRun("tubo-el28", Path("run.nmea"))],
        rtk_recording_id="rec-a",
    )

    assert "recording=rec-a rtk-run=tubo-el28" in markdown
    assert "segment=seg-a rtk-run=tubo-el28" in markdown
    assert "recording=rec-b rtk-run=tubo-el28" not in markdown
    assert "segment=seg-b rtk-run=tubo-el28" not in markdown


def test_markdown_malformed_user_sentinel_fails_clearly() -> None:
    malformed = "<!-- user-annotation:start recording=x -->\nmissing end\n"

    try:
        update_annotation_markdown(
            malformed,
            recordings=[
                RecordingAnnotation(
                    recording_id="x",
                    title="x",
                    codex_context="context",
                )
            ],
            segments=[],
            rtk_runs=[],
        )
    except ValueError as exc:
        assert "malformed user annotation sentinel" in str(exc)
    else:
        raise AssertionError("malformed user sentinel should fail")


def test_annotation_gpx_cli_generates_markdown_and_local_gpx_without_rtk(tmp_path: Path) -> None:
    rover = tmp_path / "rover_20260530050210.ubx"
    rmc = make_sentence("GNRMC,052000.000,A,5000.0000,N,01400.0000,E,0.0,0.0,300526,,,A")
    gga = make_sentence("GNGGA,052000.200,5000.0001,N,01400.0001,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    rover.write_text(rmc + "\r\n" + gga + "\r\n", encoding="ascii")
    annotations = tmp_path / "annotations.md"
    gpx = tmp_path / "annotations.gpx"

    rc = cli.main(
        [
            "annotation-gpx",
            str(rover),
            "--annotations",
            str(annotations),
            "--out-gpx",
            str(gpx),
            "--segment",
            "seg-a,2026-05-30T05:20:00Z,2026-05-30T05:20:01Z,short check",
        ]
    )

    assert rc == 0
    markdown = annotations.read_text(encoding="utf-8")
    assert "## Whole-Recording Annotations" in markdown
    assert "## Segment Annotations" in markdown
    assert "seg-a in-device" in gpx.read_text(encoding="utf-8")
    assert "RTKLIB run" not in markdown


def test_annotation_gpx_cli_preserves_user_annotation_on_rerun(tmp_path: Path) -> None:
    rover = tmp_path / "rover_20260531063148.ubx"
    rmc = make_sentence("GNRMC,070330.000,A,5000.0000,N,01400.0000,E,0.0,0.0,310526,,,A")
    gga = make_sentence("GNGGA,070330.200,5000.0001,N,01400.0001,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    rover.write_text(rmc + "\r\n" + gga + "\r\n", encoding="ascii")
    annotations = tmp_path / "annotations.md"
    gpx = tmp_path / "annotations.gpx"
    args = [
        "annotation-gpx",
        str(rover),
        "--annotations",
        str(annotations),
        "--out-gpx",
        str(gpx),
        "--segment",
        "seg-b,2026-05-31T07:03:30Z,2026-05-31T07:03:31Z,highway check",
    ]

    assert cli.main(args) == 0
    edited = annotations.read_text(encoding="utf-8").replace(
        "- Actual capture conditions:\n",
        "- Actual capture conditions: user says open-sky highway\n",
    )
    annotations.write_text(edited, encoding="utf-8")

    assert cli.main(args) == 0

    assert "user says open-sky highway" in annotations.read_text(encoding="utf-8")


def test_annotation_gpx_cli_can_seed_default_validation_segments(tmp_path: Path) -> None:
    rover = tmp_path / "rover_20260530050210.ubx"
    rmc = make_sentence("GNRMC,052000.000,A,5000.0000,N,01400.0000,E,0.0,0.0,300526,,,A")
    gga = make_sentence("GNGGA,052000.200,5000.0001,N,01400.0001,E,4,20,0.7,250.0,M,45.0,M,0.5,0001")
    rover.write_text(rmc + "\r\n" + gga + "\r\n", encoding="ascii")
    annotations = tmp_path / "annotations.md"
    gpx = tmp_path / "annotations.gpx"

    rc = cli.main(
        [
            "annotation-gpx",
            str(rover),
            "--annotations",
            str(annotations),
            "--out-gpx",
            str(gpx),
            "--use-default-segments",
        ]
    )

    assert rc == 0
    markdown = annotations.read_text(encoding="utf-8")
    assert "rover_20260531184035" in markdown
    assert "30050210-052000-052600 in-device" in gpx.read_text(encoding="utf-8")
