"""Navigation source discovery and selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from .files import classify_rinex_file


NavSource = Literal["explicit", "base_rtcm", "downloaded_brdc", "downloaded_station", "rover"]
FileType = Literal["nav", "sp3", "clk", "sbs", "unknown"]

PRIORITY = {
    "explicit": 100,
    "base_rtcm": 90,
    "downloaded_brdc": 80,
    "downloaded_station": 75,
    "rover": 50,
}


@dataclass
class NavCandidate:
    path: Path
    source: NavSource
    priority: int
    systems: set[str]
    time_start: datetime | None = None
    time_end: datetime | None = None
    rinex_version: str | None = None
    file_type: FileType = "unknown"
    usable: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class NavResolution:
    candidates: list[NavCandidate]
    selected: list[NavCandidate]
    missing_systems: set[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        def cand(candidate: NavCandidate) -> dict[str, object]:
            return {
                "path": str(candidate.path),
                "source": candidate.source,
                "priority": candidate.priority,
                "systems": sorted(candidate.systems),
                "file_type": candidate.file_type,
                "usable": candidate.usable,
                "notes": candidate.notes,
            }

        return {
            "candidates": [cand(item) for item in self.candidates],
            "selected": [cand(item) for item in self.selected],
            "missing_systems": sorted(self.missing_systems),
            "warnings": self.warnings,
        }


def infer_nav_systems(path: Path) -> set[str]:
    name = path.name.upper()
    systems: set[str] = set()
    if "BRDC" in name or "MN" in name:
        return {"GPS", "GLONASS", "Galileo", "BDS", "QZSS"}
    suffix = path.suffix.lower()
    if suffix in {".gnav"}:
        systems.add("GLONASS")
    elif suffix in {".cnav"}:
        systems.add("BDS")
    elif suffix in {".lnav"}:
        systems.add("Galileo")
    elif suffix in {".qnav"}:
        systems.add("QZSS")
    elif suffix in {".nav", ".rnx"}:
        systems.add("GPS")
    return systems or {"Unknown"}


def has_rinex_body_records(path: Path) -> bool:
    """Return true when a RINEX-like file has non-header data records."""

    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except OSError:
        return False
    in_body = False
    for line in lines:
        if in_body and line.strip():
            return True
        if "END OF HEADER" in line:
            in_body = True
    return False


def build_candidate(path: str | Path, source: NavSource) -> NavCandidate:
    p = Path(path)
    ftype = classify_rinex_file(p)
    systems = infer_nav_systems(p) if ftype in {"nav", "sp3", "clk", "sbs"} else set()
    notes: list[str] = []
    if not p.exists():
        notes.append("file does not exist")
    if ftype == "unknown":
        notes.append("file type could not be classified")
    if ftype == "nav" and p.exists() and not has_rinex_body_records(p):
        notes.append("NAV file has no data records after END OF HEADER")
    return NavCandidate(
        path=p,
        source=source,
        priority=PRIORITY[source],
        systems=systems,
        file_type=ftype,  # type: ignore[arg-type]
        usable=p.exists() and ftype in {"sp3", "clk", "sbs"} or (p.exists() and ftype == "nav" and has_rinex_body_records(p)),
        notes=notes,
    )


def resolve_nav_sources(
    explicit: list[str | Path] | None = None,
    downloaded: list[str | Path] | None = None,
    rover: list[str | Path] | None = None,
    observed_systems: set[str] | None = None,
    merge_policy: str = "best-per-system",
) -> NavResolution:
    candidates: list[NavCandidate] = []
    candidates.extend(build_candidate(path, "explicit") for path in explicit or [])
    candidates.extend(build_candidate(path, "downloaded_brdc") for path in downloaded or [])
    candidates.extend(build_candidate(path, "rover") for path in rover or [])
    usable = [candidate for candidate in candidates if candidate.usable]
    warnings: list[str] = []
    if not usable:
        warnings.append(
            "no NAV data available. Provide --nav-file, enable --download-nav, provide "
            "--base-rtcm, or log receiver ephemerides."
        )
        return NavResolution(candidates, [], observed_systems or set(), warnings)

    if merge_policy == "all":
        selected = sorted(usable, key=lambda item: (-item.priority, str(item.path)))
    else:
        selected_by_system: dict[str, NavCandidate] = {}
        for candidate in sorted(usable, key=lambda item: (-item.priority, str(item.path))):
            for system in candidate.systems:
                if system not in selected_by_system:
                    selected_by_system[system] = candidate
        selected = []
        for candidate in selected_by_system.values():
            if candidate not in selected:
                selected.append(candidate)

    covered = set().union(*(candidate.systems for candidate in selected)) if selected else set()
    observed = observed_systems or set()
    missing = {system for system in observed if system not in covered and system != "Unknown"}
    for system in sorted(missing):
        warnings.append(f"{system} observations present but no matching NAV source exists")
    if selected and all(candidate.source == "rover" for candidate in selected):
        warnings.append("rover NAV used because no explicit/base/download NAV was available")
    return NavResolution(candidates, selected, missing, warnings)
