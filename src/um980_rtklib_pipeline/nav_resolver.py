"""Navigation source discovery and selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Sequence

from .files import classify_rinex_file, detect_rinex_nav_systems


NavRole = Literal["explicit", "base", "rover", "external"]
LegacyNavSource = Literal["explicit", "base_rtcm", "downloaded_brdc", "downloaded_station", "rover"]
FileType = Literal["nav", "sp3", "clk", "sbs", "unknown"]

PRIORITY = {
    "explicit": 0,
    "base": 10,
    "rover": 20,
    "external": 30,
}

SYSTEM_ALIASES = {
    "GPS": "G",
    "GLONASS": "R",
    "GALILEO": "E",
    "GAL": "E",
    "BDS": "C",
    "BEIDOU": "C",
    "QZSS": "J",
    "NAVIC": "I",
    "IRNSS": "I",
    "SBAS": "S",
}


@dataclass
class NavCandidate:
    """Candidate navigation-like input considered for RTKLIB.

    Attributes:
        path: Local path to the candidate file.
        role: How the candidate was supplied.
        priority: Selection priority; smaller values win.
        systems: RINEX GNSS system codes inferred from content/type.
        time_start: Optional start of validity window.
        time_end: Optional end of validity window.
        rinex_version: Optional RINEX version if known.
        file_type: Classified file type.
        usable: True when the file is safe to pass to RTKLIB.
        notes: Human-readable validation notes.
    """

    path: Path
    role: NavRole
    priority: int
    systems: set[str]
    provider: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    rinex_version: str | None = None
    file_type: FileType = "unknown"
    usable: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def source(self) -> NavRole:
        """Backward-compatible alias for callers that still use ``source``."""

        return self.role


@dataclass
class NavResolution:
    """Result of navigation source selection.

    Attributes:
        candidates: All candidates considered.
        selected: Candidates selected according to priority and merge policy.
        missing_systems: Observed systems not covered by selected candidates.
        selected_systems: Systems covered by selected candidates.
        system_sources: Best selected candidate for each selected system.
        rover_obs_systems: Systems advertised by the rover OBS input.
        base_obs_systems: Systems advertised by the base OBS inputs.
        usable_rtk_systems: Selected NAV systems present in rover and base OBS.
        nav_systems_not_useful: Selected NAV systems missing from rover/base
            OBS intersection and therefore not useful for relative RTK.
        warnings: User-facing warnings explaining missing or fallback data.
    """

    candidates: list[NavCandidate]
    selected: list[NavCandidate]
    missing_systems: set[str]
    selected_systems: set[str] = field(default_factory=set)
    system_sources: dict[str, NavCandidate] = field(default_factory=dict)
    rover_obs_systems: set[str] = field(default_factory=set)
    base_obs_systems: set[str] = field(default_factory=set)
    usable_rtk_systems: set[str] = field(default_factory=set)
    nav_systems_not_useful: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation of the resolution."""

        def cand(candidate: NavCandidate) -> dict[str, object]:
            return {
                "path": str(candidate.path),
                "role": candidate.role,
                "source": candidate.source,
                "priority": candidate.priority,
                "systems": sorted(candidate.systems),
                "provider": candidate.provider,
                "file_type": candidate.file_type,
                "usable": candidate.usable,
                "notes": candidate.notes,
            }

        return {
            "candidates": [cand(item) for item in self.candidates],
            "selected": [cand(item) for item in self.selected],
            "missing_systems": sorted(self.missing_systems),
            "selected_systems": sorted(self.selected_systems),
            "rover_obs_systems": sorted(self.rover_obs_systems),
            "base_obs_systems": sorted(self.base_obs_systems),
            "usable_rtk_systems": sorted(self.usable_rtk_systems),
            "nav_systems_not_useful": sorted(self.nav_systems_not_useful),
            "warnings": self.warnings,
        }


def infer_nav_systems(path: Path) -> set[str]:
    """Infer GNSS systems covered by a navigation-like file.

    Args:
        path: Candidate file path.

    Returns:
        Set of RINEX GNSS system codes inferred from content, extension, and
        filename.
    """

    return detect_rinex_nav_systems(path) or {"Unknown"}


def _normalise_system(system: str) -> str:
    value = system.strip().upper()
    if len(value) == 1:
        return value
    return SYSTEM_ALIASES.get(value, value)


def _normalise_systems(systems: Iterable[str] | None) -> set[str]:
    return {_normalise_system(system) for system in systems or set() if system and system != "Unknown"}


def _role_from_legacy(source: NavRole | LegacyNavSource) -> NavRole:
    if source == "base_rtcm":
        return "base"
    if source in {"downloaded_brdc", "downloaded_station"}:
        return "external"
    return source  # type: ignore[return-value]


def has_rinex_body_records(path: Path) -> bool:
    """Return true when a RINEX-like file has non-header data records.

    Args:
        path: RINEX-like file path.

    Returns:
        True when at least one non-empty body line follows `END OF HEADER`.
    """

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


def build_candidate(path: str | Path, source: NavRole | LegacyNavSource, provider: str | None = None) -> NavCandidate:
    """Build and validate one navigation candidate.

    Args:
        path: Candidate file path.
        source: Source category used for priority selection.
        provider: Optional provider label for external/downloaded products.

    Returns:
        Candidate metadata including usability and validation notes.
    """

    p = Path(path)
    role = _role_from_legacy(source)
    ftype = classify_rinex_file(p)
    systems = infer_nav_systems(p) if ftype in {"nav", "sp3", "clk", "sbs"} else set()
    notes: list[str] = []
    if not p.exists():
        notes.append("file does not exist")
    if ftype == "unknown":
        notes.append("file type could not be classified")
    if ftype == "nav" and p.exists() and not has_rinex_body_records(p):
        notes.append("NAV file has no data records after END OF HEADER")
    if ftype == "sbs" and p.exists() and p.stat().st_size == 0:
        notes.append("SBAS message file is empty")
    usable = p.exists() and (
        ftype in {"sp3", "clk"}
        or (ftype == "sbs" and p.stat().st_size > 0)
        or (ftype == "nav" and has_rinex_body_records(p))
    )
    return NavCandidate(
        path=p,
        role=role,
        priority=PRIORITY[role],
        systems=systems,
        provider=provider,
        file_type=ftype,  # type: ignore[arg-type]
        usable=usable,
        notes=notes,
    )


def resolve_nav_sources(
    explicit: Sequence[str | Path] | None = None,
    base: Sequence[str | Path] | None = None,
    base_rtcm: Sequence[str | Path] | None = None,
    downloaded: Sequence[str | Path] | None = None,
    rover: Sequence[str | Path] | None = None,
    observed_systems: set[str] | None = None,
    rover_obs_systems: set[str] | None = None,
    base_obs_systems: set[str] | None = None,
    nav_source: str = "auto",
    merge_policy: str = "best-per-system",
) -> NavResolution:
    """Select navigation inputs for RTKLIB.

    Args:
        explicit: User-provided NAV/SP3/CLK/SBS files.
        base: NAV files supplied with base observations.
        base_rtcm: NAV files converted from a recorded base RTCM stream.
        downloaded: Downloaded broadcast or station navigation files.
        rover: Navigation files extracted from the rover receiver log.
        observed_systems: Backward-compatible observation-system set used when
            rover/base OBS sets are not available.
        rover_obs_systems: Systems present in rover observations.
        base_obs_systems: Systems present in base observations.
        nav_source: Source policy: `auto`, `explicit`, `base`, `rover`,
            `external`, or `none`.
        merge_policy: `off` to choose one candidate, `all` to pass every usable
            candidate, or `best-per-system` to select the preferred candidate
            per system.

    Returns:
        Navigation resolution with selected files and warnings.
    """

    all_candidates: list[NavCandidate] = []
    all_candidates.extend(build_candidate(path, "explicit") for path in explicit or [])
    all_candidates.extend(build_candidate(path, "base") for path in base or [])
    all_candidates.extend(build_candidate(path, "base_rtcm") for path in base_rtcm or [])
    all_candidates.extend(build_candidate(path, "external") for path in downloaded or [])
    all_candidates.extend(build_candidate(path, "rover") for path in rover or [])

    if nav_source == "none":
        candidates: list[NavCandidate] = []
    elif nav_source == "auto":
        candidates = all_candidates
    elif nav_source in {"explicit", "base", "rover", "external"}:
        candidates = [candidate for candidate in all_candidates if candidate.role == nav_source]
    else:
        raise ValueError(f"unsupported NAV source policy: {nav_source}")

    usable = [candidate for candidate in candidates if candidate.usable]
    warnings: list[str] = []
    if not usable:
        warnings.append(
            "no NAV data available. Provide --nav-file, enable --download-nav, provide "
            "--base-rtcm, or log receiver ephemerides."
        )
        return _resolution(
            candidates=candidates,
            selected=[],
            observed_systems=observed_systems,
            rover_obs_systems=rover_obs_systems,
            base_obs_systems=base_obs_systems,
            warnings=warnings,
        )

    if merge_policy == "all":
        selected = sorted(usable, key=lambda item: (item.priority, str(item.path)))
        system_sources = _best_system_sources(selected)
    elif merge_policy == "off":
        selected = [sorted(usable, key=lambda item: (item.priority, str(item.path)))[0]]
        system_sources = _best_system_sources(selected)
    elif merge_policy == "best-per-system":
        system_sources = _best_system_sources(usable)
        selected = []
        for candidate in system_sources.values():
            if candidate not in selected:
                selected.append(candidate)
    else:
        raise ValueError(f"unsupported NAV merge policy: {merge_policy}")

    return _resolution(
        candidates=candidates,
        selected=selected,
        observed_systems=observed_systems,
        rover_obs_systems=rover_obs_systems,
        base_obs_systems=base_obs_systems,
        warnings=warnings,
        system_sources=system_sources,
    )


def _best_system_sources(candidates: Sequence[NavCandidate]) -> dict[str, NavCandidate]:
    selected_by_system: dict[str, NavCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: (item.priority, str(item.path))):
        for system in sorted(candidate.systems):
            if system != "Unknown" and system not in selected_by_system:
                selected_by_system[system] = candidate
    return selected_by_system


def _resolution(
    *,
    candidates: list[NavCandidate],
    selected: list[NavCandidate],
    observed_systems: set[str] | None,
    rover_obs_systems: set[str] | None,
    base_obs_systems: set[str] | None,
    warnings: list[str],
    system_sources: dict[str, NavCandidate] | None = None,
) -> NavResolution:
    selected_systems = set().union(*(candidate.systems for candidate in selected)) if selected else set()
    selected_systems.discard("Unknown")
    rover_systems = _normalise_systems(rover_obs_systems)
    base_systems = _normalise_systems(base_obs_systems)
    observed = _normalise_systems(observed_systems)
    if rover_systems and base_systems:
        obs_intersection = rover_systems & base_systems
    else:
        obs_intersection = observed
    missing = {system for system in observed if system not in selected_systems and system != "Unknown"}
    usable_rtk_systems = selected_systems & obs_intersection if obs_intersection else set()
    nav_systems_not_useful = selected_systems - obs_intersection if obs_intersection else set()
    for system in sorted(missing):
        warnings.append(f"{system} observations present but no matching NAV source exists")
    if nav_systems_not_useful:
        warnings.append(
            "selected NAV systems not useful for relative RTK because they are missing from rover/base OBS "
            f"intersection: {','.join(sorted(nav_systems_not_useful))}"
        )
    if selected and all(candidate.role == "rover" for candidate in selected):
        warnings.append("rover NAV used because no explicit/base/download NAV was available")
    return NavResolution(
        candidates=candidates,
        selected=selected,
        missing_systems=missing,
        selected_systems=selected_systems,
        system_sources=system_sources or _best_system_sources(selected),
        rover_obs_systems=rover_systems,
        base_obs_systems=base_systems,
        usable_rtk_systems=usable_rtk_systems,
        nav_systems_not_useful=nav_systems_not_useful,
        warnings=warnings,
    )
