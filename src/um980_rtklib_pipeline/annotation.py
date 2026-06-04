"""Manual annotation GPX and Markdown helpers."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .solution import SolutionPoint


@dataclass(frozen=True)
class RecordingAnnotation:
    """Generated annotation context for one receiver recording."""

    recording_id: str
    title: str
    codex_context: str


@dataclass(frozen=True)
class AnnotationSegment:
    """One manually reviewed time span."""

    segment_id: str
    recording_id: str
    start_time: datetime
    end_time: datetime
    label: str
    codex_context: str


@dataclass(frozen=True)
class RtkAnnotationRun:
    """One RTKLIB base/config solution run available for annotation."""

    run_id: str
    solution_path: Path


@dataclass(frozen=True)
class AnnotationTrack:
    """One plain GPX track."""

    name: str
    points: list[SolutionPoint]
    description: str | None = None


DEFAULT_RECORDING_ANNOTATIONS: tuple[RecordingAnnotation, ...] = (
    RecordingAnnotation(
        recording_id="rover_20260530050210",
        title="rover_20260530050210",
        codex_context=(
            "Moto route with substantial forest/canopy. Expected weak and fragmented fixed "
            "coverage, high diagnostic stress, and non-highway motion profile."
        ),
    ),
    RecordingAnnotation(
        recording_id="rover_20260531063148",
        title="rover_20260531063148",
        codex_context=(
            "Car/highway route. Expected good moving fixed coverage; large terminal static/fixed "
            "tail should be quarantined from moving-route quality metrics."
        ),
    ),
    RecordingAnnotation(
        recording_id="rover_20260531095025",
        title="rover_20260531095025",
        codex_context=(
            "Car/highway route with high fixed distance but more stressed/pathological regions "
            "than rover_20260531063148; stationary/noisy interval is expected near 10:50-10:59."
        ),
    ),
    RecordingAnnotation(
        recording_id="rover_20260531184035",
        title="rover_20260531184035",
        codex_context=(
            "Moto/forest-road route. Expected moderate fixed support and a stationary noisy "
            "under-canopy interval near 19:03-19:09."
        ),
    ),
)


DEFAULT_SEGMENT_ANNOTATIONS: tuple[AnnotationSegment, ...] = (
    AnnotationSegment(
        "30050210-052000-052600",
        "rover_20260530050210",
        datetime(2026, 5, 30, 5, 20, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 5, 26, tzinfo=timezone.utc),
        "weak/provisional forest moto",
        "Weak/provisional forest moto segment; expected fragmented fixed and stressed diagnostics.",
    ),
    AnnotationSegment(
        "31063148-070330-070930",
        "rover_20260531063148",
        datetime(2026, 5, 31, 7, 3, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 7, 9, 30, tzinfo=timezone.utc),
        "clean supported highway fixed",
        "Clean supported highway fixed segment; expected useful long moving fixed continuity.",
    ),
    AnnotationSegment(
        "31063148-063900-064600",
        "rover_20260531063148",
        datetime(2026, 5, 31, 6, 39, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 6, 46, tzinfo=timezone.utc),
        "highway recovery/transition",
        "Highway recovery/transition segment; expected reacquisition or transition behavior.",
    ),
    AnnotationSegment(
        "31095025-101230-101930",
        "rover_20260531095025",
        datetime(2026, 5, 31, 10, 12, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 10, 19, 30, tzinfo=timezone.utc),
        "clean supported highway fixed",
        "Clean supported highway fixed segment; expected strong moving-route continuity.",
    ),
    AnnotationSegment(
        "31095025-103530-104330",
        "rover_20260531095025",
        datetime(2026, 5, 31, 10, 35, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 10, 43, 30, tzinfo=timezone.utc),
        "high fixed distance but stressed/mixed",
        "High fixed-distance but stressed/mixed segment; raw fixed distance alone should not be over-rewarded.",
    ),
    AnnotationSegment(
        "31184035-185230-185730",
        "rover_20260531184035",
        datetime(2026, 5, 31, 18, 52, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 18, 57, 30, tzinfo=timezone.utc),
        "usable mixed forest-road fixed",
        "Usable mixed forest-road fixed segment; expected moderate support under harder conditions.",
    ),
    AnnotationSegment(
        "31063148-073630-074100",
        "rover_20260531063148",
        datetime(2026, 5, 31, 7, 36, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 7, 41, tzinfo=timezone.utc),
        "terminal static fixed",
        "Terminal static fixed segment; should be excluded from moving-route headline metrics.",
    ),
    AnnotationSegment(
        "31095025-105000-105940",
        "rover_20260531095025",
        datetime(2026, 5, 31, 10, 50, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 10, 59, 40, tzinfo=timezone.utc),
        "stationary/noisy float",
        "Stationary/noisy float interval; should be reported separately, not as moving-route failure.",
    ),
    AnnotationSegment(
        "31184035-190315-190945",
        "rover_20260531184035",
        datetime(2026, 5, 31, 19, 3, 15, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 19, 9, 45, tzinfo=timezone.utc),
        "stationary noisy under canopy",
        "Stationary noisy under-canopy interval; should remain a middle stationary episode.",
    ),
    AnnotationSegment(
        "30050210-062300-062600",
        "rover_20260530050210",
        datetime(2026, 5, 30, 6, 23, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 6, 26, tzinfo=timezone.utc),
        "tail terminal/suspicious end handling",
        "Tail terminal/suspicious end segment; expected quarantine or suspicious-end classification.",
    ),
)


_USER_BLOCK_RE = re.compile(
    r"<!-- user-annotation:start (?P<key>[^>]+?) -->.*?<!-- user-annotation:end (?P=key) -->",
    re.DOTALL,
)


def write_annotation_gpx(path: Path, tracks: list[AnnotationTrack]) -> None:
    """Write simple GPX 1.1 tracks without custom extensions."""

    ET.register_namespace("", "http://www.topografix.com/GPX/1/1")
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "um980-ppk annotation-gpx",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )
    for track in tracks:
        trk = ET.SubElement(root, "trk")
        ET.SubElement(trk, "name").text = track.name
        if track.description:
            ET.SubElement(trk, "desc").text = track.description
        seg = ET.SubElement(trk, "trkseg")
        for point in track.points:
            trkpt = ET.SubElement(seg, "trkpt", {"lat": f"{point.lat:.10f}", "lon": f"{point.lon:.10f}"})
            height = point.h_ell if point.h_ell is not None else point.h_msl
            if height is not None:
                ET.SubElement(trkpt, "ele").text = f"{height:.4f}"
            ET.SubElement(trkpt, "time").text = _format_gpx_time(point.time_utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def update_annotation_markdown(
    existing_markdown: str | None,
    *,
    recordings: Iterable[RecordingAnnotation],
    segments: Iterable[AnnotationSegment],
    rtk_runs: Iterable[RtkAnnotationRun],
    rtk_recording_id: str | None = None,
) -> str:
    """Create or update the human-editable annotation Markdown."""

    preserved_user_blocks = _collect_user_blocks(existing_markdown or "")
    used_user_keys: set[str] = set()
    recordings = list(recordings)
    segments = list(segments)
    rtk_runs = list(rtk_runs)
    lines: list[str] = [
        "# UM980 Manual Ground-Truth Annotations",
        "",
        "This file is maintained by `um980-ppk annotation-gpx`.",
        "",
        "Edit only `User Subjective Annotation` sections. Do not manually edit "
        "`Codex/GPT Generated Annotation` sections; rerunning the tool may replace them.",
        "",
    ]

    if recordings:
        lines.extend(["## Whole-Recording Annotations", ""])
    for recording in sorted(recordings, key=lambda item: item.recording_id):
        lines.extend(_recording_block(recording))
        key = f"recording={recording.recording_id}"
        block = preserved_user_blocks.get(key) or _default_user_block(key, "recording")
        used_user_keys.add(key)
        lines.extend([block, ""])
        if rtk_recording_id is not None and recording.recording_id != rtk_recording_id:
            continue
        for run in rtk_runs:
            rtk_key = f"recording={recording.recording_id} rtk-run={run.run_id}"
            rtk_block = preserved_user_blocks.get(rtk_key) or _default_user_block(rtk_key, "rtk-recording", run.run_id)
            used_user_keys.add(rtk_key)
            lines.extend(_rtk_generated_block(recording.recording_id, run))
            lines.extend([rtk_block, ""])

    if segments:
        lines.extend(["## Segment Annotations", ""])
    for segment in sorted(segments, key=lambda item: (item.recording_id, item.start_time, item.segment_id)):
        lines.extend(_segment_block(segment))
        key = f"segment={segment.segment_id}"
        block = preserved_user_blocks.get(key) or _default_user_block(key, "segment")
        used_user_keys.add(key)
        lines.extend([block, ""])
        if rtk_recording_id is not None and segment.recording_id != rtk_recording_id:
            continue
        for run in rtk_runs:
            rtk_key = f"segment={segment.segment_id} rtk-run={run.run_id}"
            rtk_block = preserved_user_blocks.get(rtk_key) or _default_user_block(rtk_key, "rtk-segment", run.run_id)
            used_user_keys.add(rtk_key)
            lines.extend(_rtk_generated_block(segment.segment_id, run))
            lines.extend([rtk_block, ""])

    unused = [block for key, block in preserved_user_blocks.items() if key not in used_user_keys]
    if unused:
        lines.extend(["## Preserved Unmatched User Annotation Blocks", ""])
        lines.extend(unused)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def points_in_window(points: Iterable[SolutionPoint], start: datetime, end: datetime) -> list[SolutionPoint]:
    """Return points whose timestamps are inside an inclusive UTC interval."""

    start = _as_utc(start)
    end = _as_utc(end)
    return [point for point in points if start <= _as_utc(point.time_utc) <= end]


def build_in_device_tracks(
    recording_id: str,
    points: list[SolutionPoint],
    segments: Iterable[AnnotationSegment],
) -> list[AnnotationTrack]:
    """Build whole-recording and per-segment in-device tracks."""

    tracks = [AnnotationTrack(name=f"00 whole in-device {recording_id}", points=points)]
    for segment in segments:
        if segment.recording_id != recording_id:
            continue
        tracks.append(
            AnnotationTrack(
                name=f"{segment.segment_id} in-device",
                points=points_in_window(points, segment.start_time, segment.end_time),
                description=segment.label,
            )
        )
    return tracks


def parse_segment_arg(value: str, *, recording_id: str) -> AnnotationSegment:
    """Parse `ID,START,END,LABEL` from a CLI argument."""

    parts = value.split(",", 3)
    if len(parts) != 4:
        raise ValueError("--segment must have the form ID,START,END,LABEL")
    segment_id, start, end, label = [part.strip() for part in parts]
    if not segment_id or not label:
        raise ValueError("--segment ID and LABEL must be non-empty")
    start_dt = _parse_cli_datetime(start)
    end_dt = _parse_cli_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("--segment END must be after START")
    return AnnotationSegment(
        segment_id=segment_id,
        recording_id=recording_id,
        start_time=start_dt,
        end_time=end_dt,
        label=label,
        codex_context=f"User-selected candidate segment: {label}.",
    )


def _recording_block(recording: RecordingAnnotation) -> list[str]:
    key = f"recording={recording.recording_id}"
    return [
        f"### Whole Recording: {recording.title}",
        "",
        f"<!-- codex-generated:start {key} -->",
        "### Codex/GPT Generated Annotation",
        "",
        f"- Recording ID: `{recording.recording_id}`",
        f"- Expected context: {recording.codex_context}",
        "- Annotation status: needs-user-review",
        f"<!-- codex-generated:end {key} -->",
        "",
    ]


def _segment_block(segment: AnnotationSegment) -> list[str]:
    key = f"segment={segment.segment_id}"
    return [
        f"### Segment: {segment.segment_id}",
        "",
        f"<!-- codex-generated:start {key} -->",
        "### Codex/GPT Generated Annotation",
        "",
        f"- Segment ID: `{segment.segment_id}`",
        f"- Recording ID: `{segment.recording_id}`",
        f"- Time window UTC: `{_format_markdown_time(segment.start_time)}` to `{_format_markdown_time(segment.end_time)}`",
        f"- Label: {segment.label}",
        f"- Expected context: {segment.codex_context}",
        f"- GPX in-device track: `{segment.segment_id} in-device`",
        "- Annotation status: needs-user-review",
        f"<!-- codex-generated:end {key} -->",
        "",
    ]


def _rtk_generated_block(owner_id: str, run: RtkAnnotationRun) -> list[str]:
    key = f"owner={owner_id} rtk-run={run.run_id}"
    return [
        f"<!-- codex-generated:start {key} -->",
        f"### Codex/GPT Generated Annotation For RTKLIB Run `{run.run_id}`",
        "",
        f"- RTKLIB run ID: `{run.run_id}`",
        f"- Solution path: `{run.solution_path}`",
        "- Annotation status: needs-user-review",
        f"<!-- codex-generated:end {key} -->",
        "",
    ]


def _default_user_block(key: str, kind: str, run_id: str | None = None) -> str:
    heading = "### User Subjective Annotation"
    if run_id:
        heading = f"### User Subjective Annotation For RTKLIB Run `{run_id}`"
    prompts = [
        f"<!-- user-annotation:start {key} -->",
        heading,
        "",
        f"- Annotation kind: {kind}",
        "- Actual capture conditions:",
        "- In-device solution quality:",
        "- RTKLIB solution quality:",
        "- Map/trajectory observations:",
        "- Reviewer/date:",
        "- Notes:",
        f"<!-- user-annotation:end {key} -->",
    ]
    return "\n".join(prompts)


def _collect_user_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in _USER_BLOCK_RE.finditer(text):
        key = match.group("key")
        if key in blocks:
            raise ValueError(f"duplicate user annotation block for {key}")
        blocks[key] = match.group(0)
    starts = text.count("<!-- user-annotation:start ")
    ends = text.count("<!-- user-annotation:end ")
    if starts != len(blocks) or ends != len(blocks):
        raise ValueError("malformed user annotation sentinel structure")
    return blocks


def _parse_cli_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return _as_utc(dt)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_gpx_time(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_markdown_time(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
