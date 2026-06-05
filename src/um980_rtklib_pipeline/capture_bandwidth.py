"""UM980 runtime capture bandwidth matrix runner.

The matrix is intentionally evidence-first: it records per-cell structural
metrics and only uses conservative labels.  A result is never called
``SAFE`` unless repeated captures and boundary evidence are available.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .capture_profiles import CaptureProfile, parse_capture_profile
from .capture_termux import CaptureUsbOptions, run_capture_usb
from .capture_validate import validate_capture_file


SMOKE_BAUDS = (115200, 230400, 460800, 921600)
EVIDENCE_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
PROFILE_DIR = Path("tools/um980_profiles/runtime/bandwidth")
PROFILE_ORDER = (
    "passive_current",
    "ascii_nmea_minimal_1hz",
    "ascii_unicore_solution_1hz",
    "binary_solution_1hz",
    "binary_solution_5hz",
    "ascii_nmea_navigation_5hz",
    "ascii_unicore_solution_5hz",
    "ascii_nmea_navigation_10hz",
    "binary_rawobs_solution_1hz",
    "binary_rawobs_solution_5hz",
    "binary_rawobs_solution_10hz",
    "mixed_nmea_minimal_binary_solution_1hz",
    "mixed_nmea_minimal_binary_solution_5hz",
    "mixed_nmea_minimal_binary_rawobs_5hz",
    "mixed_nmea_navigation_binary_rawobs_10hz",
    "mixed_ascii_unicore_binary_rawobs_10hz",
    "binary_rawobs_solution_20hz",
    "mixed_nmea_binary_rawobs_20hz",
    "ascii_full_navigation_10hz_or_20hz",
)


@dataclass(frozen=True)
class MatrixPlan:
    """Resolved bandwidth matrix execution plan."""

    stage: str
    bauds: tuple[int, ...]
    duration_s: float
    repeats: int


def plan_for_stage(stage: str, *, duration_s: float | None, repeats: int | None) -> MatrixPlan:
    """Return default baud/duration/repeat settings for a matrix stage."""

    if stage == "smoke":
        return MatrixPlan(stage, SMOKE_BAUDS, duration_s or 25.0, repeats or 1)
    if stage == "evidence":
        return MatrixPlan(stage, EVIDENCE_BAUDS, duration_s or 120.0, repeats or 3)
    if stage == "boundary":
        return MatrixPlan(stage, EVIDENCE_BAUDS, duration_s or 300.0, repeats or 2)
    raise ValueError(f"unsupported bandwidth stage: {stage}")


def enabled_profiles(profile_dir: Path = PROFILE_DIR, *, include_stress: bool = False) -> list[CaptureProfile]:
    """Load enabled profiles, optionally including stress profiles."""

    profiles: list[CaptureProfile] = []
    for path in sorted(profile_dir.glob("*.um980"), key=_profile_path_sort_key):
        profile = parse_capture_profile(path)
        stress = profile.metadata.get("stress", "").lower() == "true"
        if stress and not include_stress:
            continue
        profiles.append(profile)
    return profiles


def disabled_profile_rows(profile_dir: Path = PROFILE_DIR, *, include_stress: bool = False) -> list[dict[str, object]]:
    """Return NOT_TESTED rows for disabled profiles."""

    rows: list[dict[str, object]] = []
    for path in sorted(profile_dir.glob("*.um980"), key=_profile_path_sort_key):
        profile = parse_capture_profile(path)
        stress = profile.metadata.get("stress", "").lower() == "true"
        if profile.enabled and (include_stress or not stress):
            continue
        reason = profile.metadata.get("disabled_reason") or (
            "stress profile not enabled" if stress and not include_stress else "command syntax not verified"
        )
        rows.append(
            {
                "profile": path.stem,
                "profile_path": str(path),
                "baud": None,
                "repeat": None,
                "classification": "NOT_TESTED",
                "reasons": [reason],
                "stage": "profile_inventory",
            }
        )
    return rows


def render_profile(profile: CaptureProfile, baud: int, out_dir: Path) -> Path:
    """Render a profile template for one baudrate into the ignored run folder."""

    rendered_dir = out_dir / "rendered_profiles"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    text = profile.path.read_text(encoding="utf-8").replace("{baud}", str(baud))
    rendered = rendered_dir / f"{profile.path.stem}-{baud}.um980"
    rendered.write_text(text, encoding="utf-8")
    return rendered


def classify_cell(row: dict[str, object]) -> tuple[str, list[str]]:
    """Classify a single profile/baud/repeat cell conservatively."""

    reasons: list[str] = []
    if row.get("capture_error"):
        return "UNSAFE", [str(row["capture_error"])]
    if not row.get("validation_passed"):
        reasons.append("validation failed")
    if not row.get("extract_check_passed"):
        reasons.append("extract-check failed")
    if row.get("expected_messages_missing"):
        reasons.append("expected messages missing: " + ",".join(row["expected_messages_missing"]))  # type: ignore[arg-type]
    if reasons:
        return "UNSAFE", reasons
    bytes_total = float(row.get("bytes_total") or 0)
    if bytes_total <= 0:
        return "UNSAFE", ["capture empty"]
    binary_crc_bad = int(row.get("binary_crc_bad") or 0)
    nmea_bad = int(row.get("nmea_checksum_bad") or 0)
    resync = int(row.get("binary_resynchronisation_events") or 0)
    unknown = float(row.get("unknown_bytes") or 0)
    ratio = float(row.get("measured_vs_uart_payload_ratio") or 0)
    if binary_crc_bad > 0 or nmea_bad > 0:
        reasons.append("checksum/frame errors present")
    if resync > 2:
        reasons.append("parser resynchronisation elevated")
    if bytes_total and unknown / bytes_total > 0.02:
        reasons.append("unknown-byte ratio elevated")
    if ratio >= 0.7:
        reasons.append("throughput near serial 8N1 payload limit")
    if reasons:
        return "MARGINAL", reasons
    return "PROVISIONALLY_SAFE", ["cell passed; requires repeated evidence before SAFE recommendation"]


def run_cell(
    *,
    profile: CaptureProfile,
    rendered_profile: Path,
    baud: int,
    repeat: int,
    duration_s: float,
    out_dir: Path,
    termux_device: str,
    native_helper: Path,
    command_timeout_s: float,
) -> dict[str, object]:
    """Run one matrix cell and return metrics."""

    stem = f"{profile.path.stem}-baud{baud}-r{repeat}"
    capture_path = out_dir / f"{stem}.unc"
    analysis_path = out_dir / f"{stem}.analysis.json"
    expected = _csv(profile.metadata.get("expected_messages", ""))
    row: dict[str, object] = {
        "profile": profile.path.stem,
        "profile_path": str(profile.path),
        "rendered_profile": str(rendered_profile),
        "baud": baud,
        "repeat": repeat,
        "duration_requested_s": duration_s,
        "stage": out_dir.name,
        "expected_messages": expected,
        "mode": profile.mode,
        "family": profile.metadata.get("family"),
        "rate_hz": _float_or_none(profile.metadata.get("rate_hz")),
        "capture": str(capture_path),
        "analysis_json": str(analysis_path),
    }
    started = time.perf_counter()
    try:
        result = run_capture_usb(
            CaptureUsbOptions(
                termux_device=termux_device,
                duration_s=duration_s,
                out=capture_path,
                native_helper=native_helper,
                profile=rendered_profile,
                analysis_json=analysis_path,
                validate=True,
                extract_check=True,
                expect_mode=profile.metadata.get("expect_mode", profile.mode),
                expect_messages=tuple(expected),
                serial_baud=baud,
                command_timeout_s=command_timeout_s,
                verbose=False,
            )
        )
        row["runtime_s"] = round(time.perf_counter() - started, 3)
        usb = result.usb_analysis or {}
        validation = result.validation.as_dict() if result.validation else {}
        extract = result.extract_check or {}
        row.update(_flatten_usb(usb, baud))
        row.update(_flatten_validation(validation))
        row["extract_check_passed"] = bool(extract.get("passed"))
    except Exception as exc:  # noqa: BLE001 - matrix must continue across cells
        row["runtime_s"] = round(time.perf_counter() - started, 3)
        row["capture_error"] = str(exc)
        if capture_path.exists():
            row["bytes_total"] = capture_path.stat().st_size
            try:
                expected_validation = validate_capture_file(
                    capture_path,
                    expect_mode=profile.metadata.get("expect_mode", profile.mode),  # type: ignore[arg-type]
                    expected_messages=expected,
                )
                row.update(_flatten_validation(expected_validation.as_dict()))
            except Exception as validation_exc:  # noqa: BLE001
                row["validation_errors"] = [str(validation_exc)]
            try:
                passive_validation = validate_capture_file(capture_path, expect_mode="passive")
                row["passive_structural_metrics"] = passive_validation.as_dict()
            except Exception:
                pass
    classification, reasons = classify_cell(row)
    row["classification"] = classification
    row["reasons"] = reasons
    return row


def run_matrix(args: argparse.Namespace) -> dict[str, object]:
    """Run the requested bandwidth matrix stages."""

    stages = ("smoke", "evidence", "boundary") if args.stage == "all" else (args.stage,)
    out_dir = args.out_dir or Path("captures") / f"bandwidth-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = args.profile_dir
    include_stress = bool(args.stress)
    all_rows: list[dict[str, object]] = disabled_profile_rows(profile_dir, include_stress=include_stress)
    runnable_profiles = [p for p in enabled_profiles(profile_dir, include_stress=include_stress) if p.enabled]
    if args.profile:
        wanted = set(args.profile)
        runnable_profiles = [p for p in runnable_profiles if p.path.stem in wanted]
    estimate = estimate_runtime(stages, runnable_profiles, args.duration, args.repeat)
    print(f"Estimated cells: {estimate['cells']}")
    print(f"Estimated runtime: {estimate['runtime_human']} plus overhead")
    stage_pass_profiles: set[str] | None = None
    for stage in stages:
        plan = plan_for_stage(stage, duration_s=args.duration, repeats=args.repeat)
        stage_dir = out_dir / stage
        stage_dir.mkdir(parents=True, exist_ok=True)
        profiles = runnable_profiles
        if stage != "smoke" and stage_pass_profiles is not None:
            profiles = [p for p in runnable_profiles if p.path.stem in stage_pass_profiles]
        stage_rows: list[dict[str, object]] = []
        for profile in profiles:
            for baud in plan.bauds:
                rendered = render_profile(profile, baud, stage_dir)
                for repeat in range(1, plan.repeats + 1):
                    print(f"{stage}: {profile.path.stem} baud={baud} repeat={repeat}/{plan.repeats}")
                    row = run_cell(
                        profile=profile,
                        rendered_profile=rendered,
                        baud=baud,
                        repeat=repeat,
                        duration_s=plan.duration_s,
                        out_dir=stage_dir,
                        termux_device=args.termux_device,
                        native_helper=args.native_helper,
                        command_timeout_s=max(args.command_timeout_s or 0.0, plan.duration_s + 120.0),
                    )
                    stage_rows.append(row)
                    all_rows.append(row)
                    time.sleep(args.cooldown_s)
        if stage == "smoke":
            stage_pass_profiles = {
                str(row["profile"])
                for row in stage_rows
                if row.get("classification") in {"PROVISIONALLY_SAFE", "MARGINAL"}
            }
    summary = build_summary(all_rows, stages=stages, out_dir=out_dir, include_stress=include_stress)
    (out_dir / "bandwidth_matrix_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "bandwidth_recommendations.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def build_summary(rows: list[dict[str, object]], *, stages: Iterable[str], out_dir: Path, include_stress: bool) -> dict[str, object]:
    """Aggregate matrix rows into recommendation-oriented JSON."""

    by_profile: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_profile.setdefault(str(row["profile"]), []).append(row)
    recommendations = []
    for profile, profile_rows in sorted(by_profile.items()):
        tested = [row for row in profile_rows if isinstance(row.get("baud"), int)]
        if not tested:
            recommendations.append({"profile": profile, "classification": "NOT_TESTED", "minimum_recommended_baud": None, "notes": _join_reasons(profile_rows)})
            continue
        passing = [row for row in tested if row.get("classification") == "PROVISIONALLY_SAFE"]
        marginal = [row for row in tested if row.get("classification") == "MARGINAL"]
        unsafe = [row for row in tested if row.get("classification") == "UNSAFE"]
        if passing:
            min_baud = min(int(row["baud"]) for row in passing)
            evidence_repeats = len([row for row in passing if int(row["baud"]) == min_baud])
            label = "PROVISIONALLY_SAFE"
            if evidence_repeats >= 3 and any(row.get("stage") == "boundary" for row in passing):
                label = "SAFE"
            notes = "passed structural checks; repeat/boundary evidence required for recommended-safe classification"
        elif marginal:
            min_baud = min(int(row["baud"]) for row in marginal)
            label = "MARGINAL"
            notes = _join_reasons(marginal)
        elif unsafe:
            min_baud = None
            label = "UNSAFE"
            notes = _join_reasons(unsafe)
        else:
            min_baud = None
            label = "INCONCLUSIVE"
            notes = "no conclusive tested cells"
        recommendations.append(
            {
                "profile": profile,
                "classification": label,
                "minimum_recommended_baud": min_baud,
                "measured_bytes_per_second_median": _median([row.get("bytes_per_second") for row in tested]),
                "evidence_cells": len(tested),
                "notes": notes,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(out_dir),
        "stages": list(stages),
        "stress_enabled": include_stress,
        "rows": rows,
        "recommendations": recommendations,
        "usb_line_coding_conclusion": _line_coding_conclusion(rows),
        "ascii_binary_comparisons": _comparison_rows(rows),
    }


def render_markdown(summary: dict[str, object]) -> str:
    """Render the generated recommendation report."""

    recs = summary["recommendations"]  # type: ignore[index]
    comparisons = summary["ascii_binary_comparisons"]  # type: ignore[index]
    lines = [
        "# UM980 Bandwidth Recommendations",
        "",
        "## Executive Summary",
        "",
        f"- USB line-coding conclusion: {summary['usb_line_coding_conclusion']}",
        "- `SAFE` is used only when repeated and boundary evidence exists; otherwise passing cells are provisional.",
        "- Captures are local ignored artifacts; no persistent receiver configuration is saved.",
        "",
        "## Safe Configurations",
        "",
        "| Profile | Classification | Minimum baud | Median B/s | Evidence cells | Notes |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for rec in recs:  # type: ignore[assignment]
        if rec["classification"] in {"SAFE", "PROVISIONALLY_SAFE"}:
            lines.append(_rec_row(rec))
    lines.extend(["", "## Marginal Configurations", "", "| Profile | Classification | Minimum baud | Median B/s | Evidence cells | Notes |", "|---|---:|---:|---:|---:|---|"])
    for rec in recs:  # type: ignore[assignment]
        if rec["classification"] == "MARGINAL":
            lines.append(_rec_row(rec))
    lines.extend(["", "## Unsafe / Not Tested Configurations", "", "| Profile | Classification | Notes |", "|---|---:|---|"])
    for rec in recs:  # type: ignore[assignment]
        if rec["classification"] in {"UNSAFE", "INCONCLUSIVE", "NOT_TESTED"}:
            lines.append(f"| {rec['profile']} | {rec['classification']} | {rec['notes']} |")
    lines.extend(
        [
            "",
            "## ASCII Versus Binary Comparison",
            "",
            "| Pair | Left B/s | Right B/s | Ratio | Interpretation |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for row in comparisons:  # type: ignore[assignment]
        lines.append(
            f"| {row['pair']} | {_fmt(row.get('left_bps'))} | {_fmt(row.get('right_bps'))} | {_fmt(row.get('ratio'))} | {row['interpretation']} |"
        )
    lines.extend(
        [
            "",
            "## Mixed-Stream Parser Robustness",
            "",
            "Mixed profiles are assessed by validation/extract-check success, checksum/frame errors, resynchronisation counts, and unknown-byte ratio.",
            "",
            "## Raw-Observation Recommendations",
            "",
            "Prefer binary raw-observation profiles that pass repeated evidence. Avoid 20 Hz stress profiles unless explicitly measuring failure thresholds.",
            "",
            "## USB Line-Coding Conclusion",
            "",
            str(summary["usb_line_coding_conclusion"]),
            "",
            "## Practical Recommendations",
            "",
            "- Treat smoke-only results as provisional, not recommended-safe.",
            "- Prefer binary profiles when ASCII and binary equivalents have similar information content but ASCII shows higher bandwidth or parser stress.",
            "- For RTKLIB input, prefer binary raw observations plus binary ephemerides once the profile passes at the intended baudrate.",
            "",
            "## Limitations",
            "",
            "- Static sky and current constellation count affect raw-observation size.",
            "- Results apply to this UM980 / Redmi Pad Pro / Termux / FTDI bridge setup until repeated elsewhere.",
            "- Disabled profiles are not tested; stress profiles are off by default.",
        ]
    )
    return "\n".join(lines) + "\n"


def estimate_runtime(stages: Iterable[str], profiles: list[CaptureProfile], duration: float | None, repeats: int | None) -> dict[str, object]:
    """Estimate matrix runtime."""

    cells = 0
    seconds = 0.0
    for stage in stages:
        plan = plan_for_stage(stage, duration_s=duration, repeats=repeats)
        stage_cells = len(profiles) * len(plan.bauds) * plan.repeats
        cells += stage_cells
        seconds += stage_cells * plan.duration_s
    return {"cells": cells, "runtime_s": seconds, "runtime_human": _human_seconds(seconds)}


def main(argv: list[str] | None = None) -> int:
    """Run the bandwidth matrix CLI."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--termux-device", required=True)
    parser.add_argument("--stage", choices=["smoke", "evidence", "boundary", "all"], default=os.environ.get("UM980_BANDWIDTH_STAGE", "smoke"))
    parser.add_argument("--duration", type=float, default=float(os.environ["UM980_BANDWIDTH_DURATION"]) if os.environ.get("UM980_BANDWIDTH_DURATION") else None)
    parser.add_argument("--repeat", type=int, default=int(os.environ["UM980_BANDWIDTH_REPEAT"]) if os.environ.get("UM980_BANDWIDTH_REPEAT") else None)
    parser.add_argument("--cooldown-s", type=float, default=2.0)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--profile", action="append", help="Limit to one profile stem; repeatable.")
    parser.add_argument("--native-helper", type=Path, default=Path("tools/termux/um980-usb-fd"))
    parser.add_argument("--command-timeout-s", type=float, help="Per-cell timeout; defaults to duration + 120 seconds.")
    parser.add_argument("--stress", action="store_true", default=os.environ.get("UM980_BANDWIDTH_STRESS") == "1")
    run_matrix(parser.parse_args(argv))
    return 0


def _flatten_usb(usb: dict[str, object], baud: int) -> dict[str, object]:
    bytes_per_second = float(usb.get("bytes_per_second") or 0.0)
    theoretical = baud / 10.0
    return {
        "duration_actual_s": usb.get("duration_actual_s"),
        "bytes_written": usb.get("bytes_written"),
        "bytes_per_second": bytes_per_second,
        "payload_bits_per_second": bytes_per_second * 8,
        "uart_8n1_equivalent_bps": bytes_per_second * 10,
        "theoretical_uart_payload_Bps": theoretical,
        "measured_vs_uart_payload_ratio": bytes_per_second / theoretical if theoretical else None,
        "requested_baud": baud,
        "line_coding_attempted": baud > 0,
        "line_coding_succeeded": bool(usb.get("ftdi_serial_mode")),
        "interface_type": "vendor-specific-ftdi" if str(usb.get("id_vendor")) == "0x0403" else "unknown",
        "read_timeouts": usb.get("read_timeouts"),
        "read_errors": usb.get("read_errors"),
        "endpoint_in": usb.get("endpoint_in"),
        "endpoint_out": usb.get("endpoint_out"),
    }


def _flatten_validation(validation: dict[str, object]) -> dict[str, object]:
    return {
        "validation_passed": bool(validation.get("mode_expectation_passed")) and not validation.get("errors"),
        "bytes_total": validation.get("bytes_total"),
        "nmea_records": validation.get("nmea_records"),
        "nmea_checksum_ok": validation.get("nmea_checksum_ok"),
        "nmea_checksum_bad": validation.get("nmea_checksum_bad"),
        "unicore_ascii_records": validation.get("unicore_ascii_records"),
        "unicore_binary_frames": validation.get("unicore_binary_frames"),
        "binary_crc_ok": validation.get("binary_crc_ok"),
        "binary_crc_bad": validation.get("binary_crc_bad"),
        "binary_resynchronisation_events": validation.get("binary_resynchronisation_events"),
        "unknown_bytes": validation.get("unknown_bytes"),
        "message_counts": validation.get("message_counts"),
        "expected_messages_missing": validation.get("expected_messages_missing"),
        "first_timestamp": validation.get("first_timestamp"),
        "last_timestamp": validation.get("last_timestamp"),
        "validation_errors": validation.get("errors"),
        "validation_warnings": validation.get("warnings"),
    }


def _csv(text: str | None) -> list[str]:
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def _float_or_none(text: str | None) -> float | None:
    try:
        return float(text) if text is not None and text != "" else None
    except ValueError:
        return None


def _median(values: Iterable[object]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return statistics.median(usable) if usable else None


def _join_reasons(rows: list[dict[str, object]]) -> str:
    reasons: list[str] = []
    for row in rows:
        for reason in row.get("reasons", []) or []:  # type: ignore[union-attr]
            text = str(reason)
            if text not in reasons:
                reasons.append(text)
    return "; ".join(reasons[:5]) if reasons else ""


def _line_coding_conclusion(rows: list[dict[str, object]]) -> str:
    tested = [row for row in rows if isinstance(row.get("baud"), int) and row.get("classification") in {"PROVISIONALLY_SAFE", "MARGINAL"}]
    if not tested:
        return "inconclusive: no passing baud/profile cells"
    by_profile: dict[str, set[int]] = {}
    for row in tested:
        by_profile.setdefault(str(row["profile"]), set()).add(int(row["baud"]))
    if any(len(bauds) > 1 for bauds in by_profile.values()):
        return "measured across multiple requested baudrates; compare per-profile throughput ratios before concluding whether line coding limits this interface"
    return "inconclusive: insufficient baud variation"


def _comparison_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    pairs = [
        ("ascii_nmea_minimal_1hz", "binary_solution_1hz"),
        ("ascii_unicore_solution_5hz", "binary_solution_5hz"),
        ("ascii_nmea_navigation_5hz", "mixed_nmea_minimal_binary_solution_5hz"),
        ("binary_rawobs_solution_5hz", "mixed_nmea_minimal_binary_rawobs_5hz"),
        ("binary_rawobs_solution_10hz", "mixed_nmea_navigation_binary_rawobs_10hz"),
    ]
    out = []
    for left, right in pairs:
        left_bps = _median(row.get("bytes_per_second") for row in rows if row.get("profile") == left and row.get("classification") != "UNSAFE")
        right_bps = _median(row.get("bytes_per_second") for row in rows if row.get("profile") == right and row.get("classification") != "UNSAFE")
        ratio = (left_bps / right_bps) if left_bps and right_bps else None
        out.append(
            {
                "pair": f"{left} vs {right}",
                "left_bps": left_bps,
                "right_bps": right_bps,
                "ratio": ratio,
                "interpretation": "insufficient measured evidence" if ratio is None else "compare measured overhead and parser stress",
            }
        )
    return out


def _rec_row(rec: dict[str, object]) -> str:
    return (
        f"| {rec['profile']} | {rec['classification']} | {rec.get('minimum_recommended_baud') or 'n/a'} | "
        f"{_fmt(rec.get('measured_bytes_per_second_median'))} | {rec.get('evidence_cells')} | {rec.get('notes', '')} |"
    )


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _human_seconds(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {sec}s"


def _profile_path_sort_key(path: Path) -> tuple[int, str]:
    try:
        return (PROFILE_ORDER.index(path.stem), path.stem)
    except ValueError:
        return (len(PROFILE_ORDER), path.stem)


if __name__ == "__main__":
    raise SystemExit(main())
