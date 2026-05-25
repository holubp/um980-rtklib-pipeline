"""Reports for RTKLIB `.stat` based satellite QC."""

from __future__ import annotations

import json
from pathlib import Path

from .badsat import BadSatDecision, decision_to_json_dict


def write_badsat_markdown_report(path: Path, decision: BadSatDecision) -> None:
    """Write a human-readable satellite QC report."""

    excluded = set(decision.exclude_sats)
    lines: list[str] = [
        "# RTKLIB Satellite QC Report",
        "",
        "## Automatic Actions",
        "",
        f"- Excluded satellites: `{ ' '.join(decision.exclude_sats) or 'none' }`",
        (
            f"- Derived `pos1-elmask`: `{decision.recommended_elmask:g}`"
            if decision.recommended_elmask is not None
            else "- Derived `pos1-elmask`: unchanged"
        ),
        "",
        "## Watch List",
        "",
        f"`{ ' '.join(decision.watch_sats) or 'none' }`",
        "",
        "## Per-Satellite Evidence",
        "",
        "| Sat | Mean el | Code p95 | Phase p95 | Combined p95 | >5 m | Invalid | Rej delta | Reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for metric in decision.metrics:
        sat = f"**{metric.sat}**" if metric.sat in excluded else metric.sat
        reasons = [*metric.reasons, *decision.blocked_reasons.get(metric.sat, [])]
        lines.append(
            f"| {sat} | {metric.mean_el:.1f} | {metric.code_p95:.2f} | {metric.phase_p95:.3f} "
            f"| {metric.combined_p95:.2f} | {metric.residual_gt5} | {metric.invalid_rate:.0%} "
            f"| {metric.rej_delta} | {'; '.join(reasons)} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_badsat_json_report(path: Path, decision: BadSatDecision) -> None:
    """Write a machine-readable satellite QC report."""

    path.write_text(json.dumps(decision_to_json_dict(decision), indent=2, sort_keys=True) + "\n", encoding="utf-8")
