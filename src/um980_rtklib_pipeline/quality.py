"""Analysis JSON assembly."""

from __future__ import annotations

import json
from pathlib import Path

from .obs_decode import ObservationExtraction
from .rinex_nav import NavExtractionReport
from .solution import SolutionExtraction
from .stream import StreamDiagnostics


def build_analysis(
    *,
    stream: StreamDiagnostics,
    solutions: SolutionExtraction,
    observations: ObservationExtraction,
    rover_nav: NavExtractionReport | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble analysis JSON data.

    Args:
        stream: Stream parser diagnostics.
        solutions: Solution extraction result.
        observations: Observation extraction result.
        rover_nav: Optional rover navigation extraction report.
        extra: Optional extra top-level fields.

    Returns:
        JSON-friendly analysis dictionary.
    """

    analysis: dict[str, object] = {
        "stream": stream.as_dict(),
        "solution_points": len(solutions.solution_points),
        "nmea_cadence": solutions.nmea_cadence,
        "raw_observations": observations.metrics,
        "observation_decode": {
            "unsupported_records": observations.unsupported_records,
            "time_unknown_reasons": observations.time_unknown_reasons,
            "skipped_non_observation_records": observations.skipped_records,
        },
        "unsupported_observation_records": observations.unsupported_records,
        "warnings": list(dict.fromkeys([*solutions.warnings, *observations.warnings])),
    }
    if rover_nav is not None:
        analysis["ephemeris"] = rover_nav.as_dict()
        analysis["warnings"].extend(rover_nav.warnings)  # type: ignore[index,union-attr]
    if extra:
        analysis.update(extra)
    analysis["warnings"] = list(dict.fromkeys(analysis.get("warnings", [])))  # type: ignore[arg-type]
    return analysis


def write_analysis_json(path: Path, analysis: dict[str, object]) -> None:
    """Write analysis JSON to disk.

    Args:
        path: Destination JSON path.
        analysis: Analysis dictionary to serialise.
    """

    path.write_text(json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
