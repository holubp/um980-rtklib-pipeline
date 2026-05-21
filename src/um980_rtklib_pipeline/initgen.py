"""UM980 receiver initialisation command generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .bitrate import BitrateEstimate, estimate_bitrate


NMEA_PRESETS: dict[str, dict[str, float]] = {
    "solution-20hz": {
        "GNGGA": 20,
        "GNRMC": 20,
        "GNGST": 1,
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GNGLL": 0,
        "GNGNS": 1,
        "GPGRS": 0.0333,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "solution-10hz": {
        "GNGGA": 10,
        "GNRMC": 10,
        "GNGST": 1,
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GNGLL": 0,
        "GNGNS": 1,
        "GPGRS": 0.0333,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "minimal": {
        "GNGGA": 1,
        "GNRMC": 1,
        "GNGST": 1,
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GNGLL": 0,
        "GNGNS": 0,
        "GPGRS": 0,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "survey": {
        "GNGGA": 1,
        "GNRMC": 1,
        "GNGST": 1,
        "GNGSA": 1,
        "GNGSV": 1,
    },
    "none": {},
}

EPHEMERIS_MESSAGES: dict[str, str] = {
    "gps": "GPSEPHA",
    "glo": "GLOEPHA",
    "gal": "GALEPHA",
    "bds": "BDSEPHA",
    "bd3": "BD3EPHA",
    "qzss": "QZSSEPHA",
}


@dataclass(frozen=True)
class InitProfile:
    port: str = "COM1"
    baud: int = 230400
    mode: str = "rover"
    base_lat: float | None = None
    base_lon: float | None = None
    base_height: float | None = None
    nmea: dict[str, float] = field(default_factory=lambda: dict(NMEA_PRESETS["minimal"]))
    raw_format: str = "none"
    raw_hz: float = 0.0
    expected_obs_per_epoch: int = 100
    ephemeris: dict[str, float | str] = field(default_factory=dict)
    ppp: str = "none"
    ppp_datum: str = "WGS84"
    ppp_timeout: int | None = None
    ppp_converge: tuple[int, int] | None = None
    include_tropinfo: bool = False
    include_gpsion: bool = False
    save_config: bool = False


def parse_rate(value: str) -> tuple[str, float]:
    """Parse `MSG=HZ` or `MSG@PERIODs` NMEA rate syntax."""

    text = value.strip()
    if "@" in text:
        msg, period_text = text.split("@", 1)
        period_text = period_text.strip().removesuffix("s")
        period = float(period_text)
        return msg.strip().upper(), 0.0 if period == 0 else 1.0 / period
    if "=" not in text:
        raise ValueError(f"invalid NMEA rate: {value}")
    msg, hz = text.split("=", 1)
    return msg.strip().upper(), float(hz)


def parse_nmea_overrides(values: list[str] | None) -> dict[str, float]:
    """Parse repeated or comma-separated NMEA override arguments."""

    parsed: dict[str, float] = {}
    for item in values or []:
        for part in item.split(","):
            part = part.strip()
            if part:
                msg, hz = parse_rate(part)
                parsed[msg] = hz
    return parsed


def hz_to_period_text(hz: float) -> str:
    if hz <= 0:
        return "0"
    period = 1.0 / hz
    if abs(period - round(period)) < 0.0001:
        return str(int(round(period)))
    return f"{period:.4f}".rstrip("0").rstrip(".")


def ephemeris_policy(policy: str, systems: list[str]) -> dict[str, float | str]:
    if policy in {"off", "none"}:
        return {}
    value: float | str
    if policy == "onchanged":
        value = "ONCHANGED"
    elif policy.startswith("every="):
        value = float(policy.split("=", 1)[1])
    else:
        raise ValueError(f"invalid ephemeris policy: {policy}")
    return {EPHEMERIS_MESSAGES[system]: value for system in systems if system in EPHEMERIS_MESSAGES}


def bitrate_comparison(profile: InitProfile) -> dict[str, BitrateEstimate]:
    return {
        fmt: estimate_bitrate(
            baud=profile.baud,
            nmea_rates_hz=profile.nmea,
            raw_format=fmt,
            raw_hz=profile.raw_hz,
            expected_obs_per_epoch=profile.expected_obs_per_epoch,
            ephemeris_periods_s=profile.ephemeris,
        )
        for fmt in ("obsvma", "obsvmb", "obsvmcmpb")
    }


def validate_profile(profile: InitProfile) -> None:
    if profile.mode not in {"rover", "base"}:
        raise ValueError(f"unsupported mode: {profile.mode}")
    if profile.mode == "base" and (
        profile.base_lat is None or profile.base_lon is None or profile.base_height is None
    ):
        raise ValueError("mode base requires --base-lat, --base-lon and --base-height")
    if profile.raw_format not in {"none", "obsvma", "obsvmb", "obsvmcmpb"}:
        raise ValueError(f"unsupported raw format: {profile.raw_format}")


def render_init_script(
    profile: InitProfile,
    *,
    strict_bitrate: bool = False,
    allow_overload: bool = False,
) -> tuple[str, BitrateEstimate]:
    """Generate UM980 commands and return the bitrate estimate."""

    validate_profile(profile)
    estimate = estimate_bitrate(
        baud=profile.baud,
        nmea_rates_hz=profile.nmea,
        raw_format=profile.raw_format,
        raw_hz=profile.raw_hz,
        expected_obs_per_epoch=profile.expected_obs_per_epoch,
        ephemeris_periods_s=profile.ephemeris,
    )
    if strict_bitrate and estimate.utilisation >= 1.0 and not allow_overload:
        raise ValueError(
            f"requested configuration is estimated at {estimate.utilisation:.0%} of "
            f"{profile.baud} bps 8N1 capacity. Suggested alternatives: use OBSVMB or "
            "OBSVMCMPB, reduce high-rate NMEA messages, or increase baud."
        )

    lines = [
        "# Generated by um980-ppk init",
        f"# Port: {profile.port}",
        f"# Baud: {profile.baud}",
        f"# Estimated payload: {estimate.total_bytes_per_s / 1000:.1f} kB/s",
        f"# Estimated 8N1 line rate: {estimate.line_rate_bits_per_s / 1000:.0f} kbps",
        f"# Utilisation: {estimate.utilisation:.0%}",
        f"# Assessment: {estimate.assessment}",
    ]
    if estimate.utilisation >= 1.0:
        lines.append("# WARNING: requested logging profile exceeds estimated serial capacity")
    lines.extend(["", f"CONFIG {profile.port} {profile.baud}", ""])

    if profile.ppp != "none":
        lines.append(f"CONFIG PPP ENABLE {profile.ppp.upper()}")
        lines.append(f"CONFIG PPP DATUM {profile.ppp_datum}")
        if profile.ppp_timeout is not None:
            lines.append(f"CONFIG PPP TIMEOUT {profile.ppp_timeout}")
        if profile.ppp_converge is not None:
            lines.append(f"CONFIG PPP CONVERGE {profile.ppp_converge[0]} {profile.ppp_converge[1]}")
        lines.append("")

    if profile.mode == "base":
        lines.append(
            f"MODE BASE {profile.base_lat:.10f} {profile.base_lon:.10f} {profile.base_height:.4f}"
        )
    else:
        lines.append("MODE ROVER")
    lines.append("")

    if profile.raw_format != "none" and profile.raw_hz > 0:
        lines.append(f"{profile.raw_format.upper()} {profile.port} {hz_to_period_text(profile.raw_hz)}")
        lines.append("")

    for message in sorted(profile.nmea):
        lines.append(f"{message} {hz_to_period_text(profile.nmea[message])}")
    if profile.nmea:
        lines.append("")

    for message in ("GPSEPHA", "GLOEPHA", "GALEPHA", "BDSEPHA", "BD3EPHA", "QZSSEPHA"):
        if message not in profile.ephemeris:
            continue
        period = profile.ephemeris[message]
        period_text = period if isinstance(period, str) else f"{period:g}"
        lines.append(f"{message} {profile.port} {period_text}")
    if profile.ephemeris:
        lines.append("")

    if profile.include_tropinfo:
        lines.append("TROPINFOA ONCHANGED")
    if profile.include_gpsion:
        lines.append("GPSIONB ONCHANGED")
    if profile.include_tropinfo or profile.include_gpsion:
        lines.append("")

    if profile.save_config:
        lines.append("SAVECONFIG")

    return "\n".join(lines).rstrip() + "\n", estimate


def write_json_report(path: Path, profile: InitProfile, estimate: BitrateEstimate) -> None:
    payload = {
        "requested_configuration": {
            "port": profile.port,
            "baud": profile.baud,
            "raw_format": profile.raw_format,
            "raw_hz": profile.raw_hz,
            "expected_obs_per_epoch": profile.expected_obs_per_epoch,
        },
        "estimated_payload": estimate.as_dict(),
        "format_comparison_at_requested_rate": {
            name: value.as_dict() for name, value in bitrate_comparison(profile).items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

