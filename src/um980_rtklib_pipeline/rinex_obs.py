"""RINEX 3 observation writer."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC
from pathlib import Path
from typing import Literal

from .obs_decode import Observation
from .timeutil import gps_week_tow_to_datetime


SYSTEM_ORDER = ["G", "R", "E", "C", "J", "S", "I"]
SUPPORTED_RINEX_SYSTEMS = set(SYSTEM_ORDER)
OBS_KIND_ORDER = ["C", "L", "D", "S"]
CONVBIN_SIGNAL_ORDER = {
    "G": ["1C", "1L", "2W", "2L", "5Q"],
    "R": ["1C", "2C", "3Q"],
    "E": ["1C", "7Q", "5Q", "6C"],
    "C": ["2I", "7P", "5P", "6I", "1P"],
    "J": ["1C", "1L", "2W", "2L", "5Q", "6Z", "6E"],
    "S": ["1C", "5I"],
    "I": ["5A", "5B"],
}
RINEX_OBS_VALUES_PER_LINE = 4
RinexCompatibility = Literal["native", "convbin"]


def _obs_types(
    observations: list[Observation],
    *,
    compatibility: RinexCompatibility = "native",
) -> dict[str, list[str]]:
    types: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        system = obs.rinex_sat[0]
        for prefix in OBS_KIND_ORDER:
            types[system].add(prefix + obs.rinex_code)
    if compatibility == "convbin":
        return {
            system: sorted(values, key=lambda code: _convbin_obs_sort_key(system, code))
            for system, values in types.items()
        }
    return {
        system: sorted(values, key=lambda code: (code[1], OBS_KIND_ORDER.index(code[0]), code[2:]))
        for system, values in types.items()
    }


def _convbin_obs_sort_key(system: str, code: str) -> tuple[int, int, str]:
    """Return RTKLIB `convbin`-style observation type priority."""

    signal_order = CONVBIN_SIGNAL_ORDER.get(system, [])
    signal = code[1:]
    try:
        signal_index = signal_order.index(signal)
    except ValueError:
        signal_index = len(signal_order)
    return signal_index, OBS_KIND_ORDER.index(code[0]), code


def observations_for_rinex(
    observations: list[Observation],
    *,
    compatibility: RinexCompatibility = "native",
) -> list[Observation]:
    """Filter decoded observations for the requested RINEX compatibility mode.

    Args:
        observations: Decoded observations.
        compatibility: `native` preserves all decoded observations. `convbin`
            drops records that RTKLIB `convbin` would not emit safely, including
            unknown-system satellites and implausible pre-modern GPS weeks.

    Returns:
        Observations safe to write for the requested compatibility mode.

    Raises:
        ValueError: If `compatibility` is unsupported.
    """

    if compatibility == "native":
        return [obs for obs in observations if _is_supported_rinex_observation(obs)]
    if compatibility != "convbin":
        raise ValueError(f"unsupported RINEX compatibility mode: {compatibility}")
    return [
        obs
        for obs in observations
        if _is_supported_rinex_observation(obs)
        and obs.sat_system != "Unknown"
        and obs.gps_week >= 1024
        and obs.pseudorange_m is not None
    ]


def _is_supported_rinex_observation(obs: Observation) -> bool:
    return bool(obs.rinex_sat) and obs.rinex_sat[0] in SUPPORTED_RINEX_SYSTEMS


def _format_header_line(value: str, label: str) -> str:
    return f"{value:<60}{label:>20}"


def _format_time_of_obs_label(dt, label: str) -> str:
    return _format_header_line(
        f"  {dt.year:04d}    {dt.month:02d}    {dt.day:02d}    {dt.hour:02d}    {dt.minute:02d}"
        f"   {dt.second + dt.microsecond / 1_000_000:10.7f}     GPS",
        label,
    )


def _format_obs_value(value: float | None, lli: int | None = None) -> str:
    if value is None:
        return " " * 16
    lli_char = " " if lli in (None, 0) else str(lli)
    for precision in range(3, -1, -1):
        numeric = f"{value:14.{precision}f}"
        if len(numeric) <= 14:
            return f"{numeric}{lli_char:1s} "
    raise ValueError(f"RINEX observation value is too large for fixed-width output: {value}")


def _format_satellite_observation_rows(sat: str, values: list[str], *, wrap: bool) -> list[str]:
    """Return RINEX observation rows for one satellite, wrapped at 80 columns.

    Args:
        sat: RINEX satellite identifier such as `G16`.
        values: Already formatted 16-character observation fields.
        wrap: When true, split rows into 80-column physical lines. When false,
            emit the RTKLIB `convbin` style extended single-line RINEX 3 record.

    Returns:
        One or more RINEX 3 observation lines. Wrapped continuation lines keep
        the three-character satellite field blank.

    Raises:
        ValueError: If a formatted observation field is not exactly 16
            characters, because that would corrupt fixed-width RINEX output.
    """

    bad_lengths = [len(value) for value in values if len(value) != 16]
    if bad_lengths:
        raise ValueError(f"RINEX observation values must be 16 characters, got lengths {bad_lengths}")
    if not wrap:
        return [sat + "".join(values)]
    rows: list[str] = []
    for offset in range(0, len(values), RINEX_OBS_VALUES_PER_LINE):
        prefix = sat if offset == 0 else " " * 3
        rows.append((prefix + "".join(values[offset : offset + RINEX_OBS_VALUES_PER_LINE])).rstrip())
    return rows


def write_rinex_obs(
    path: Path,
    observations: list[Observation],
    *,
    marker_name: str = "UM980_ROVER",
    rinex_version: str = "3.04",
    approx_position: tuple[float, float, float] | None = None,
    compatibility: RinexCompatibility = "native",
    progress: bool = False,
) -> None:
    """Write decoded observations as a RINEX 3 observation file.

    Args:
        path: Destination RINEX observation path.
        observations: Decoded observations to write.
        marker_name: RINEX marker name.
        rinex_version: RINEX version string.
        approx_position: Optional approximate receiver ECEF XYZ in meters.
        compatibility: Output profile. `convbin` produces a stricter RTKLIB
            `convbin`-style RINEX OBS file.
        progress: Emit coarse write-progress messages through logging.

    Raises:
        ValueError: If no observations or observation types are available.
    """

    observations = observations_for_rinex(observations, compatibility=compatibility)
    if not observations:
        raise ValueError("RINEX writer emitted no observation types: no observations decoded")
    obs_types = _obs_types(observations, compatibility=compatibility)
    if not obs_types:
        raise ValueError("RINEX writer emitted no observation types")
    by_epoch: dict[tuple[int, float], list[Observation]] = defaultdict(list)
    for index, obs in enumerate(observations, start=1):
        if progress and index % 500_000 == 0:
            logging.info("grouped %d/%d observations for RINEX OBS", index, len(observations))
        by_epoch[(obs.gps_week, obs.tow)].append(obs)
    epochs = sorted(by_epoch)

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
    first_obs_time = gps_week_tow_to_datetime(*epochs[0]).astimezone(UTC)
    last_obs_time = gps_week_tow_to_datetime(*epochs[-1]).astimezone(UTC)
    lines.append(_format_time_of_obs_label(first_obs_time, "TIME OF FIRST OBS"))
    lines.append(_format_time_of_obs_label(last_obs_time, "TIME OF LAST OBS"))
    for system in SYSTEM_ORDER:
        codes = obs_types.get(system)
        if not codes:
            continue
        first = f"{system}{len(codes):5d} " + " ".join(f"{code:>3s}" for code in codes[:13])
        lines.append(_format_header_line(first, "SYS / # / OBS TYPES"))
        for offset in range(13, len(codes), 13):
            cont = " " * 7 + " ".join(f"{code:>3s}" for code in codes[offset : offset + 13])
            lines.append(_format_header_line(cont, "SYS / # / OBS TYPES"))
    lines.append(_format_header_line("", "END OF HEADER"))
    for index, key in enumerate(epochs, start=1):
        if progress and index % 5_000 == 0:
            logging.info("formatted %d/%d epochs for RINEX OBS", index, len(epochs))
        epoch_obs = sorted(by_epoch[key], key=lambda item: item.rinex_sat)
        dt = gps_week_tow_to_datetime(*key).astimezone(UTC)
        by_sat: dict[str, list[Observation]] = defaultdict(list)
        for obs in epoch_obs:
            by_sat[obs.rinex_sat].append(obs)
        lines.append(
            f"> {dt.year:04d} {dt.month:02d} {dt.day:02d} {dt.hour:02d} {dt.minute:02d} "
            f"{dt.second + dt.microsecond / 1_000_000:10.7f}  0{len(by_sat):3d}"
        )
        for sat in sorted(by_sat):
            values_by_code: dict[str, tuple[float | None, int]] = {}
            for obs in by_sat[sat]:
                values_by_code["C" + obs.rinex_code] = (obs.pseudorange_m, obs.lli)
                values_by_code["L" + obs.rinex_code] = (obs.carrier_phase_cycles, obs.lli)
                values_by_code["D" + obs.rinex_code] = (obs.doppler_hz, obs.lli)
                values_by_code["S" + obs.rinex_code] = (obs.cn0_dbhz, obs.lli)
            values: list[str] = []
            for code in obs_types.get(sat[0], []):
                value, lli = values_by_code.get(code, (None, 0))
                values.append(_format_obs_value(value, lli))
            lines.extend(_format_satellite_observation_rows(sat, values, wrap=compatibility != "convbin"))

    path.write_text("\n".join(lines) + "\n", encoding="ascii")
