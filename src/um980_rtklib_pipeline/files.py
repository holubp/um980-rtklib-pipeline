"""Filesystem and file classification helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


WILDCARD_CHARS = set("*?[]{}")


@dataclass(frozen=True)
class RinexObsTimeSpan:
    """Time coverage parsed from a RINEX observation file.

    Attributes:
        start: First observation epoch, when it can be determined.
        end: Last observation epoch, when it can be determined.
    """

    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class RinexObsCapabilities:
    """Observation capabilities advertised by a RINEX OBS header.

    Attributes:
        path: Source RINEX observation file.
        observation_types: Mapping from RINEX system code to observation type
            strings, for example ``{"G": ("C1C", "L1C")}``.
        rinex_version: Header RINEX version when parsed.
        rinex_file_system: Header file-system code, such as ``M`` or ``G``.
    """

    path: Path
    observation_types: dict[str, tuple[str, ...]]
    rinex_version: str | None = None
    rinex_file_system: str | None = None

    @property
    def systems(self) -> set[str]:
        """Return RINEX systems with advertised observation types."""

        return set(self.observation_types)

    def bands_by_system(self) -> dict[str, set[str]]:
        """Return frequency-band digits advertised for each system."""

        return {
            system: {
                obs_type[1]
                for obs_type in obs_types
                if len(obs_type) >= 2 and obs_type[0] in {"C", "L", "D", "S"} and obs_type[1].isdigit()
            }
            for system, obs_types in self.observation_types.items()
        }


def ensure_out_dir(path: str | Path | None) -> Path:
    """Create and return an output directory.

    Args:
        path: Requested output directory, or `None` to use the current working
            directory.

    Returns:
        Existing or newly created output directory path.
    """

    out_dir = Path(path) if path else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def basename_for(input_path: str | Path, override: str | None = None) -> str:
    """Return the output basename for an input path.

    Args:
        input_path: Source file path used when no override is supplied.
        override: Optional explicit basename.

    Returns:
        `override` when provided, otherwise the input path stem.
    """

    return override or Path(input_path).stem


def has_unresolved_wildcard(path: str | Path) -> bool:
    """Return true when a path string still contains shell wildcard syntax.

    Args:
        path: Path-like value to inspect.

    Returns:
        True if the value includes wildcard characters that should have been
        expanded before RTKLIB execution.
    """

    return any(char in str(path) for char in WILDCARD_CHARS)


def classify_rinex_file(path: str | Path) -> str:
    """Classify a RINEX-like text file.

    Args:
        path: File path to classify.

    Returns:
        One of `obs`, `nav`, `sp3`, `clk`, `sbs`, or `unknown`.
    """

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".sp3":
        return "sp3"
    if suffix == ".clk":
        return "clk"
    if suffix == ".sbs":
        return "sbs"
    if suffix in {".gnav", ".lnav", ".hnav", ".qnav", ".cnav", ".inav"}:
        return "nav"
    if not p.exists() or not p.is_file():
        return "unknown"
    try:
        head = p.read_text(encoding="ascii", errors="ignore")[:4096]
    except OSError:
        return "unknown"
    if "OBSERVATION DATA" in head:
        return "obs"
    if "NAVIGATION DATA" in head or "NAV DATA" in head or "NAV MSG DATA" in head:
        return "nav"
    if "SBAS" in head and "DATA" in head:
        return "sbs"
    return "unknown"


RINEX_SYSTEM_CODES = {"G", "R", "E", "C", "J", "I", "S"}


def detect_rinex_nav_systems(path: str | Path) -> set[str]:
    """Return RINEX system codes present in a navigation-like file.

    The helper is intentionally lightweight: it recognises common RINEX 3 mixed
    NAV records by the first body-line character and falls back to extension and
    broadcast-product filename hints for older or sidecar files.

    Args:
        path: Candidate RINEX NAV, SBAS, or constellation-specific sidecar.

    Returns:
        RINEX constellation codes such as ``{"G", "E", "C"}``.
    """

    p = Path(path)
    name = p.name.upper()
    suffix = p.suffix.lower()
    if suffix == ".gnav":
        return {"R"}
    if suffix == ".lnav":
        return {"E"}
    if suffix == ".cnav":
        return {"C"}
    if suffix == ".qnav":
        return {"J"}
    if suffix == ".inav":
        return {"I"}
    if suffix == ".sbs":
        return {"S"}
    if re.fullmatch(r"\.\d{2}n", suffix):
        return {"G"}
    if any(token in name for token in ("BRDC", "BRDM", "BRD4")) or "_MN" in name:
        return {"G", "R", "E", "C", "J"}

    systems: set[str] = set()
    in_body = False
    try:
        with p.open("r", encoding="ascii", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not in_body:
                    if "RINEX VERSION / TYPE" in line:
                        header_system = line[40:41].strip()
                        if header_system in RINEX_SYSTEM_CODES:
                            systems.add(header_system)
                    if "END OF HEADER" in line:
                        in_body = True
                    continue
                if line and line[0] in RINEX_SYSTEM_CODES:
                    systems.add(line[0])
    except OSError:
        return set()
    if systems:
        return systems
    if suffix in {".nav", ".rnx"}:
        return {"G"}
    return set()


def detect_rinex_obs_systems(path: str | Path) -> set[str]:
    """Return RINEX system codes advertised by an observation file.

    Args:
        path: RINEX OBS file.

    Returns:
        RINEX constellation codes. Ambiguous RINEX 2 mixed ``M`` headers are not
        expanded because the exact per-system coverage is not known.
    """

    systems = read_rinex_obs_capabilities(path).systems
    return {system for system in systems if system in RINEX_SYSTEM_CODES}


def _parse_rinex_datetime(parts: list[str]) -> datetime | None:
    if len(parts) < 6:
        return None
    try:
        year = int(parts[0])
        if year < 100:
            year += 2000 if year < 80 else 1900
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])
        second_float = float(parts[5])
    except ValueError:
        return None
    second = int(second_float)
    microsecond = int(round((second_float - second) * 1_000_000))
    if microsecond >= 1_000_000:
        second += 1
        microsecond -= 1_000_000
    try:
        return datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return None


def read_rinex_obs_time_span(path: str | Path) -> RinexObsTimeSpan:
    """Read the time span covered by a RINEX observation file.

    Header `TIME OF FIRST OBS` and `TIME OF LAST OBS` records are preferred.
    If the last-observation header is missing, body epoch records are scanned as
    a fallback. Returned datetimes are naive and intended only for comparing
    coverage between files that use the same GNSS time scale.

    Args:
        path: RINEX observation file to inspect.

    Returns:
        Parsed first and last observation times. Either value can be `None` when
        the source file does not expose it clearly.
    """

    start: datetime | None = None
    end: datetime | None = None
    epoch_start: datetime | None = None
    epoch_end: datetime | None = None
    in_body = False
    try:
        with Path(path).open("r", encoding="ascii", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not in_body:
                    if "TIME OF FIRST OBS" in line:
                        start = _parse_rinex_datetime(line[:43].split())
                    elif "TIME OF LAST OBS" in line:
                        end = _parse_rinex_datetime(line[:43].split())
                    elif "END OF HEADER" in line:
                        in_body = True
                    continue
                epoch = _parse_rinex_epoch_line(line)
                if epoch is None:
                    continue
                if epoch_start is None:
                    epoch_start = epoch
                epoch_end = epoch
    except OSError:
        return RinexObsTimeSpan(None, None)
    return RinexObsTimeSpan(start or epoch_start, end or epoch_end)


def read_rinex_obs_capabilities(path: str | Path) -> RinexObsCapabilities:
    """Read systems, observation codes, and bands from a RINEX OBS header.

    RINEX 3 ``SYS / # / OBS TYPES`` records are parsed per constellation.
    RINEX 2 ``# / TYPES OF OBSERV`` records are exposed under the header file
    system when it is specific, or ``G`` for the common GPS-only case. Mixed
    RINEX 2 files cannot describe per-constellation capabilities precisely, so
    they are reported under ``M`` and should be treated as informational.

    Args:
        path: RINEX observation file to inspect.

    Returns:
        Parsed observation capability metadata. Missing or unreadable headers
        return an empty capability object instead of raising.
    """

    p = Path(path)
    observation_types: dict[str, list[str]] = {}
    rinex_version: str | None = None
    rinex_file_system: str | None = None
    rinex2_types: list[str] = []
    try:
        with p.open("r", encoding="ascii", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                label = line[60:].strip() if len(line) >= 60 else ""
                if label == "RINEX VERSION / TYPE":
                    rinex_version = line[:9].strip() or None
                    rinex_file_system = line[40:41].strip() or None
                elif label == "SYS / # / OBS TYPES":
                    system = line[:1].strip()
                    if system:
                        observation_types.setdefault(system, []).extend(_parse_obs_type_tokens(line[7:60]))
                elif label == "# / TYPES OF OBSERV":
                    rinex2_types.extend(_parse_obs_type_tokens(line[:60]))
                elif "END OF HEADER" in line:
                    break
    except OSError:
        return RinexObsCapabilities(path=p, observation_types={})
    if rinex2_types and not observation_types:
        system = rinex_file_system if rinex_file_system and rinex_file_system != " " else "G"
        observation_types[system or "G"] = rinex2_types
    return RinexObsCapabilities(
        path=p,
        observation_types={system: tuple(types) for system, types in observation_types.items()},
        rinex_version=rinex_version,
        rinex_file_system=rinex_file_system,
    )


def _parse_obs_type_tokens(text: str) -> list[str]:
    """Return RINEX observation-code tokens from a header payload segment."""

    return [token for token in text.split() if len(token) == 3 and token[0] in {"C", "L", "D", "S"}]


def _parse_rinex_epoch_line(line: str) -> datetime | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith(">"):
        return _parse_rinex_datetime(stripped[1:].split())
    parts = stripped.split()
    if len(parts) < 6:
        return None
    try:
        first = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])
    except ValueError:
        return None
    if not (0 <= first <= 99 or 1900 <= first <= 2099):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return _parse_rinex_datetime(parts)


def filter_rinex_obs_by_overlap(rover_obs: str | Path, base_obs: list[Path]) -> tuple[list[Path], list[str]]:
    """Filter base observation files that cannot overlap rover observations.

    Files with unknown timing are kept, but a warning is returned so callers can
    make the ambiguity visible. Files with known timing outside the rover span
    are removed because RTKLIB can otherwise fail to use later overlapping base
    files predictably.

    Args:
        rover_obs: Rover RINEX observation file.
        base_obs: Candidate base RINEX observation files.

    Returns:
        A tuple of retained base files and warning strings.

    Raises:
        ValueError: If all base observation files are known to be outside the
            rover observation time span.
    """

    rover_span = read_rinex_obs_time_span(rover_obs)
    warnings: list[str] = []
    if rover_span.start is None or rover_span.end is None:
        warnings.append(
            f"could not determine rover observation time span from {rover_obs}; "
            "base observation overlap was not checked"
        )
        return base_obs, warnings

    retained: list[Path] = []
    dropped: list[Path] = []
    for path in base_obs:
        base_span = read_rinex_obs_time_span(path)
        if base_span.start is None or base_span.end is None:
            warnings.append(
                f"could not determine base observation time span from {path}; "
                "keeping it for RTKLIB but the rover/base overlap is unverified"
            )
            retained.append(path)
            continue
        if base_span.end < rover_span.start or base_span.start > rover_span.end:
            warnings.append(
                f"base observation file has no rover overlap and will not be passed to RTKLIB: "
                f"{path} ({base_span.start.isoformat()} to {base_span.end.isoformat()} vs rover "
                f"{rover_span.start.isoformat()} to {rover_span.end.isoformat()})"
            )
            dropped.append(path)
            continue
        retained.append(path)

    if not retained and dropped:
        raise ValueError(
            "no base observation files overlap the rover observation time span "
            f"{rover_span.start.isoformat()} to {rover_span.end.isoformat()}"
        )
    return retained, warnings
