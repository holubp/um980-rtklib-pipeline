"""RINEX 3 observation writer."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC
from pathlib import Path

from .obs_decode import Observation
from .timeutil import gps_week_tow_to_datetime


SYSTEM_ORDER = ["G", "R", "E", "C", "J", "S", "I", "U"]
OBS_KIND_ORDER = ["C", "L", "D", "S"]


def _obs_types(observations: list[Observation]) -> dict[str, list[str]]:
    types: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        system = obs.rinex_sat[0]
        for prefix in OBS_KIND_ORDER:
            types[system].add(prefix + obs.rinex_code)
    return {
        system: sorted(values, key=lambda code: (code[1], OBS_KIND_ORDER.index(code[0]), code[2:]))
        for system, values in types.items()
    }


def _format_header_line(value: str, label: str) -> str:
    return f"{value:<60}{label:>20}"


def _format_obs_value(value: float | None, lli: int | None = None) -> str:
    if value is None:
        return " " * 16
    return f"{value:14.3f}{lli or 0:1d} "


def write_rinex_obs(
    path: Path,
    observations: list[Observation],
    *,
    marker_name: str = "UM980_ROVER",
    rinex_version: str = "3.04",
    approx_position: tuple[float, float, float] | None = None,
) -> None:
    if not observations:
        raise ValueError("RINEX writer emitted no observation types: no observations decoded")
    obs_types = _obs_types(observations)
    if not obs_types:
        raise ValueError("RINEX writer emitted no observation types")

    lines = [
        _format_header_line(
            f"{float(rinex_version):9.2f}           OBSERVATION DATA    M: MIXED",
            "RINEX VERSION / TYPE",
        ),
        _format_header_line("um980-ppk          um980-ppk", "PGM / RUN BY / DATE"),
        _format_header_line(marker_name, "MARKER NAME"),
        _format_header_line("UNKNOWN             UNKNOWN", "REC # / TYPE / VERS"),
        _format_header_line("UNKNOWN             UNKNOWN", "ANT # / TYPE"),
    ]
    if approx_position is None:
        approx_position = (0.0, 0.0, 0.0)
    lines.append(
        _format_header_line(
            f"{approx_position[0]:14.4f}{approx_position[1]:14.4f}{approx_position[2]:14.4f}",
            "APPROX POSITION XYZ",
        )
    )
    for system in SYSTEM_ORDER:
        codes = obs_types.get(system)
        if not codes:
            continue
        first = f"{system}{len(codes):5d} " + " ".join(f"{code:>3s}" for code in codes[:13])
        lines.append(_format_header_line(first, "SYS / # / OBS TYPES"))
        for offset in range(13, len(codes), 13):
            cont = f"{system}{'':5s} " + " ".join(f"{code:>3s}" for code in codes[offset : offset + 13])
            lines.append(_format_header_line(cont, "SYS / # / OBS TYPES"))
    lines.append(_format_header_line("", "END OF HEADER"))

    by_epoch: dict[tuple[int, float], list[Observation]] = defaultdict(list)
    for obs in observations:
        by_epoch[(obs.gps_week, obs.tow)].append(obs)

    for key in sorted(by_epoch):
        epoch_obs = sorted(by_epoch[key], key=lambda item: item.rinex_sat)
        dt = gps_week_tow_to_datetime(*key).astimezone(UTC)
        lines.append(
            f"> {dt.year:04d} {dt.month:02d} {dt.day:02d} {dt.hour:02d} {dt.minute:02d} "
            f"{dt.second + dt.microsecond / 1_000_000:10.7f}  0 {len(epoch_obs):3d}"
        )
        by_sat: dict[str, list[Observation]] = defaultdict(list)
        for obs in epoch_obs:
            by_sat[obs.rinex_sat].append(obs)
        for sat in sorted(by_sat):
            values_by_code: dict[str, tuple[float | None, int]] = {}
            for obs in by_sat[sat]:
                values_by_code["C" + obs.rinex_code] = (obs.pseudorange_m, obs.lli)
                values_by_code["L" + obs.rinex_code] = (obs.carrier_phase_cycles, obs.lli)
                values_by_code["D" + obs.rinex_code] = (obs.doppler_hz, obs.lli)
                values_by_code["S" + obs.rinex_code] = (obs.cn0_dbhz, obs.lli)
            row = sat
            for code in obs_types.get(sat[0], []):
                value, lli = values_by_code.get(code, (None, 0))
                row += _format_obs_value(value, lli)
            lines.append(row.rstrip())

    path.write_text("\n".join(lines) + "\n", encoding="ascii")

