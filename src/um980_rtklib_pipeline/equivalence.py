"""Helpers for comparing one-shot and composed pipeline outputs."""

from __future__ import annotations

from pathlib import Path


_VOLATILE_MARKERS = (
    "PGM / RUN BY / DATE",
    "COMMENT",
    "generated_at",
    "elapsed_s",
    "trace_parse_elapsed_s",
    "stat_parse_elapsed_s",
)


def normalize_for_equivalence(path: Path | str) -> str:
    """Return text normalised for step-equivalence comparisons.

    The helper intentionally removes only volatile report/header lines. It does
    not attempt semantic GNSS parsing; tests that need stricter comparison can
    layer format-aware checks on top.
    """

    source = Path(path)
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line.rstrip() for line in lines if not any(marker in line for marker in _VOLATILE_MARKERS)]
    return "\n".join(kept).strip() + ("\n" if kept else "")
