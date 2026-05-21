"""Filesystem and file classification helpers."""

from __future__ import annotations

from pathlib import Path


WILDCARD_CHARS = set("*?[]{}")


def ensure_out_dir(path: str | Path | None) -> Path:
    out_dir = Path(path) if path else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def basename_for(input_path: str | Path, override: str | None = None) -> str:
    return override or Path(input_path).stem


def has_unresolved_wildcard(path: str | Path) -> bool:
    return any(char in str(path) for char in WILDCARD_CHARS)


def classify_rinex_file(path: str | Path) -> str:
    """Classify a RINEX-like text file as obs/nav/sp3/clk/sbs/unknown."""

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".sp3":
        return "sp3"
    if suffix == ".clk":
        return "clk"
    if not p.exists() or not p.is_file():
        return "unknown"
    try:
        head = p.read_text(encoding="ascii", errors="ignore")[:4096]
    except OSError:
        return "unknown"
    if "OBSERVATION DATA" in head:
        return "obs"
    if "NAVIGATION DATA" in head:
        return "nav"
    if "SBAS" in head and "DATA" in head:
        return "sbs"
    return "unknown"

