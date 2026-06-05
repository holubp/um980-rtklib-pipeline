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
            "Moto route with standard town/city riding at the beginning and end, and "
            "substantial hilly forest/canopy in the middle. Expected weak and fragmented "
            "fixed coverage in the forest, high diagnostic stress, and non-highway motion profile."
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
        "30050210-051014-051130",
        "rover_20260530050210",
        datetime(2026, 5, 30, 5, 10, 14, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 5, 11, 30, tzinfo=timezone.utc),
        "reasonably open view, town/city driving with smaller buildings, mildly hilly",
        "Reasonably open view with no major obstruction or canopy; useful as an in-device open-view reference.",
    ),
    AnnotationSegment(
        "30050210-052000-052600",
        "rover_20260530050210",
        datetime(2026, 5, 30, 5, 20, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 5, 26, tzinfo=timezone.utc),
        "weak/provisional forest moto with some scrub segments",
        (
            "Weak/provisional forest moto segment with some straight scrub/grass sections "
            "that may provide a better solution than the denser forest; expected fragmented "
            "fixed and stressed diagnostics."
        ),
    ),
    AnnotationSegment(
        "30050210-062052-062203",
        "rover_20260530050210",
        datetime(2026, 5, 30, 6, 20, 52, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 6, 22, 3, tzinfo=timezone.utc),
        "reasonably open view, town/city driving with smaller buildings, mildly hilly",
        "Reasonably open view with no major obstruction or canopy; useful as a second open-view reference.",
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
        "31063148-063900-064113",
        "rover_20260531063148",
        datetime(2026, 5, 31, 6, 39, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 6, 41, 13, tzinfo=timezone.utc),
        "long tunnel",
        "First part of the earlier recovery/transition segment; contains a long tunnel.",
    ),
    AnnotationSegment(
        "31063148-064113-064600",
        "rover_20260531063148",
        datetime(2026, 5, 31, 6, 41, 13, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 6, 46, tzinfo=timezone.utc),
        "relatively open sky after tunnel",
        "Second part of the earlier recovery/transition segment; relatively open sky after the tunnel.",
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
        "31095025-103530-104221",
        "rover_20260531095025",
        datetime(2026, 5, 31, 10, 35, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 10, 42, 21, tzinfo=timezone.utc),
        "high fixed distance but stressed/mixed before tunnel",
        "High fixed-distance but stressed/mixed segment before the tunnel; raw fixed distance alone should not be over-rewarded.",
    ),
    AnnotationSegment(
        "31095025-104221-104330",
        "rover_20260531095025",
        datetime(2026, 5, 31, 10, 42, 21, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 10, 43, 30, tzinfo=timezone.utc),
        "same tunnel, different tube",
        "Tunnel part of the earlier stressed/mixed segment; same tunnel as the 31063148 split but a different tube.",
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
        "31184035-185804-185942",
        "rover_20260531184035",
        datetime(2026, 5, 31, 18, 58, 4, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 18, 59, 42, tzinfo=timezone.utc),
        "typical forest road/street",
        "Typical road or street through a forest; useful for forest-road in-device annotation.",
    ),
    AnnotationSegment(
        "31184035-191511-191555",
        "rover_20260531184035",
        datetime(2026, 5, 31, 19, 15, 11, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 19, 15, 55, tzinfo=timezone.utc),
        "typical forest road/street",
        "Typical road or street through a forest; short forest-road comparison segment.",
    ),
    AnnotationSegment(
        "31184035-191603-191653",
        "rover_20260531184035",
        datetime(2026, 5, 31, 19, 16, 3, tzinfo=timezone.utc),
        datetime(2026, 5, 31, 19, 16, 53, tzinfo=timezone.utc),
        "tree alley road",
        "Typical road in a tree alley; useful for distinguishing alley effects from denser canopy.",
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

    segments_by_recording: dict[str, list[AnnotationSegment]] = {}
    for segment in segments:
        segments_by_recording.setdefault(segment.recording_id, []).append(segment)
    for grouped_segments in segments_by_recording.values():
        grouped_segments.sort(key=lambda item: (item.start_time, item.segment_id))

    for recording in sorted(recordings, key=lambda item: item.recording_id):
        lines.extend([f"## Recording: {recording.recording_id}", ""])
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

        recording_segments = segments_by_recording.pop(recording.recording_id, [])
        if recording_segments:
            lines.extend(["### Segments", ""])
        for segment in recording_segments:
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

    for recording_id, recording_segments in sorted(segments_by_recording.items()):
        lines.extend([f"## Recording: {recording_id}", "", "### Segments", ""])
        for segment in recording_segments:
            lines.extend(_segment_block(segment))
            key = f"segment={segment.segment_id}"
            block = preserved_user_blocks.get(key) or _default_user_block(key, "segment")
            used_user_keys.add(key)
            lines.extend([block, ""])

    unused = [
        block
        for key, block in preserved_user_blocks.items()
        if key not in used_user_keys and _user_block_has_notes(block)
    ]
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
        f"- Generated capture context: {recording.codex_context}",
        f"<!-- codex-generated:end {key} -->",
        "",
    ]


def _segment_block(segment: AnnotationSegment) -> list[str]:
    key = f"segment={segment.segment_id}"
    return [
        f"#### Segment: {segment.segment_id}",
        "",
        f"<!-- codex-generated:start {key} -->",
        "### Codex/GPT Generated Annotation",
        "",
        f"- Segment ID: `{segment.segment_id}`",
        f"- Recording ID: `{segment.recording_id}`",
        f"- Time window UTC: `{_format_markdown_time(segment.start_time)}` to `{_format_markdown_time(segment.end_time)}`",
        f"- Label: {segment.label}",
        f"- Generated capture context: {segment.codex_context}",
        f"- GPX in-device track: `{segment.segment_id} in-device`",
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
        "- Annotation status: needs-user-review",
    ]
    if kind.startswith("rtk-"):
        prompts.extend(
            [
                "- Run-specific solution quality:",
                "- Reviewer/date:",
                "- Notes:",
            ]
        )
    else:
        prompts.extend(
            [
                "- Capture context and conditions:",
                "- In-device solution quality:",
                "- Reviewer/date:",
                "- Notes:",
            ]
        )
    prompts.append(f"<!-- user-annotation:end {key} -->")
    return "\n".join(prompts)


def _collect_user_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in _USER_BLOCK_RE.finditer(text):
        key = match.group("key")
        if key in blocks:
            raise ValueError(f"duplicate user annotation block for {key}")
        blocks[key] = _normalise_user_block(key, match.group(0))
    starts = text.count("<!-- user-annotation:start ")
    ends = text.count("<!-- user-annotation:end ")
    if starts != len(blocks) or ends != len(blocks):
        raise ValueError("malformed user annotation sentinel structure")
    return blocks


def _normalise_user_block(key: str, block: str) -> str:
    """Migrate old user-owned prompt labels without discarding their values."""

    if "rtk-run=" in key:
        return block.replace("- RTKLIB solution quality:", "- Run-specific solution quality:")

    lines = block.splitlines()
    migrated: list[str] = []
    carry_to_notes: list[str] = []
    has_capture_context = any(line.strip().startswith("- Capture context and conditions:") for line in lines)
    has_annotation_status = any(line.strip().startswith("- Annotation status:") for line in lines)
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("- Actual capture conditions:"):
            value = stripped.split(":", 1)[1].strip()
            if has_capture_context:
                if value:
                    carry_to_notes.append(f"Previous capture conditions: {value}")
                continue
            migrated.append(raw_line.replace("- Actual capture conditions:", "- Capture context and conditions:", 1))
            has_capture_context = True
            continue
        if stripped.startswith("- RTKLIB solution quality:") or stripped.startswith("- Map/trajectory observations:"):
            field, value = stripped[2:].split(":", 1)
            value = value.strip()
            if value and value.lower() not in {"not-available", "n/a", "none"}:
                carry_to_notes.append(f"Previous {field}: {value}")
            continue
        migrated.append(raw_line)

    if not has_annotation_status:
        insert_at = _line_after_annotation_kind(migrated)
        migrated.insert(insert_at, "- Annotation status: needs-user-review")

    if not has_capture_context:
        insert_at = _line_after_annotation_kind(migrated)
        if not has_annotation_status:
            insert_at += 1
        migrated.insert(insert_at, "- Capture context and conditions:")

    if carry_to_notes:
        note_text = "; ".join(carry_to_notes)
        for index, raw_line in enumerate(migrated):
            if raw_line.strip().startswith("- Notes:"):
                prefix, value = raw_line.split(":", 1)
                existing = value.strip()
                migrated[index] = f"{prefix}: {existing}; {note_text}" if existing else f"{prefix}: {note_text}"
                break
        else:
            migrated.insert(-1, f"- Notes: {note_text}")
    return "\n".join(migrated)


def _line_after_annotation_kind(lines: list[str]) -> int:
    for index, raw_line in enumerate(lines):
        if raw_line.strip().startswith("- Annotation kind:"):
            return index + 1
    return max(0, len(lines) - 1)


def _user_block_has_notes(block: str) -> bool:
    """Return true when a user-owned block contains non-placeholder notes."""

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        if key.strip() == "Annotation kind":
            continue
        if key.strip() == "Annotation status" and value.strip() == "needs-user-review":
            continue
        if value.strip():
            return True
    return False


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
