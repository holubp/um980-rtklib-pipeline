from __future__ import annotations

from pathlib import Path

import pytest

from um980_rtklib_pipeline.nav_resolver import resolve_nav_sources


def _write_nav(path: Path, systems: str) -> Path:
    lines = [
        "     3.04           NAVIGATION DATA     M                   RINEX VERSION / TYPE\n",
        "                                                            END OF HEADER\n",
    ]
    for system in systems:
        lines.append(f"{system}01 2026 05 20 00 00 00 0.0 0.0 0.0\n")
    path.write_text("".join(lines), encoding="ascii")
    return path


def test_auto_prefers_base_nav_over_rover_nav_for_same_system(tmp_path: Path) -> None:
    base = _write_nav(tmp_path / "base.rnx", "G")
    rover = _write_nav(tmp_path / "rover.rnx", "G")

    resolution = resolve_nav_sources(base=[base], rover=[rover], nav_source="auto")

    assert [candidate.path for candidate in resolution.selected] == [base]
    assert resolution.selected[0].role == "base"


def test_explicit_nav_file_outranks_base_nav(tmp_path: Path) -> None:
    explicit = _write_nav(tmp_path / "explicit.rnx", "G")
    base = _write_nav(tmp_path / "base.rnx", "G")

    resolution = resolve_nav_sources(explicit=[explicit], base=[base], nav_source="auto")

    assert [candidate.path for candidate in resolution.selected] == [explicit]
    assert resolution.selected[0].role == "explicit"


@pytest.mark.parametrize(
    ("nav_source", "expected_role"),
    [
        ("rover", "rover"),
        ("base", "base"),
        ("explicit", "explicit"),
        ("external", "external"),
    ],
)
def test_strict_nav_source_selects_only_requested_role(
    tmp_path: Path, nav_source: str, expected_role: str
) -> None:
    explicit = _write_nav(tmp_path / "explicit.rnx", "G")
    base = _write_nav(tmp_path / "base.rnx", "G")
    rover = _write_nav(tmp_path / "rover.rnx", "G")
    external = _write_nav(tmp_path / "BRDC00IGS_R_20261400000_01D_MN.rnx", "G")

    resolution = resolve_nav_sources(
        explicit=[explicit],
        base=[base],
        rover=[rover],
        downloaded=[external],
        nav_source=nav_source,
    )

    assert {candidate.role for candidate in resolution.selected} == {expected_role}


def test_best_per_system_fills_missing_systems_from_lower_priority_sources(tmp_path: Path) -> None:
    base = _write_nav(tmp_path / "base.rnx", "GE")
    rover = _write_nav(tmp_path / "rover.rnx", "GEC")
    external = _write_nav(tmp_path / "BRDC00IGS_R_20261400000_01D_MN.rnx", "GERC")

    resolution = resolve_nav_sources(
        base=[base],
        rover=[rover],
        downloaded=[external],
        nav_source="auto",
        merge_policy="best-per-system",
    )

    selected = {(candidate.role, tuple(sorted(candidate.systems))) for candidate in resolution.selected}
    assert ("base", ("E", "G")) in selected
    assert ("rover", ("C", "E", "G")) in selected
    assert ("external", ("C", "E", "G", "J", "R")) in selected
    assert resolution.system_sources["G"].role == "base"
    assert resolution.system_sources["E"].role == "base"
    assert resolution.system_sources["C"].role == "rover"
    assert resolution.system_sources["R"].role == "external"


def test_merge_all_keeps_all_valid_nav_candidates(tmp_path: Path) -> None:
    base = _write_nav(tmp_path / "base.rnx", "G")
    rover = _write_nav(tmp_path / "rover.rnx", "E")
    external = _write_nav(tmp_path / "BRDC00IGS_R_20261400000_01D_MN.rnx", "C")

    resolution = resolve_nav_sources(
        base=[base],
        rover=[rover],
        downloaded=[external],
        nav_source="auto",
        merge_policy="all",
    )

    assert [candidate.path for candidate in resolution.selected] == [base, rover, external]


def test_nav_systems_missing_from_base_obs_are_reported_not_useful(tmp_path: Path) -> None:
    base_nav = _write_nav(tmp_path / "base.rnx", "GEC")

    resolution = resolve_nav_sources(
        base=[base_nav],
        nav_source="base",
        rover_obs_systems={"G", "E", "C"},
        base_obs_systems={"G", "E"},
    )

    assert resolution.usable_rtk_systems == {"G", "E"}
    assert resolution.nav_systems_not_useful == {"C"}
    assert any("not useful" in warning for warning in resolution.warnings)


def test_nav_source_none_selects_no_candidates(tmp_path: Path) -> None:
    rover = _write_nav(tmp_path / "rover.rnx", "G")

    resolution = resolve_nav_sources(rover=[rover], nav_source="none")

    assert not resolution.selected
    assert resolution.candidates == []
