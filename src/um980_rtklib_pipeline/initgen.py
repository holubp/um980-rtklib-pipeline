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
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GPGRS": 1 / 30,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "solution-10hz": {
        "GNGGA": 10,
        "GNRMC": 10,
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GPGRS": 1 / 30,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "minimal": {
        "GNGGA": 1,
        "GNRMC": 1,
        "GNGSA": 0.2,
        "GNGSV": 0.2,
        "GPGRS": 0,
        "PPPNAVA": 0.1,
        "ADRNAVA": 0.1,
    },
    "survey": {
        "GNGGA": 1,
        "GNRMC": 1,
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
BINARY_EPHEMERIS_MESSAGES: dict[str, str] = {
    "gps": "GPSEPHB",
    "glo": "GLOEPHB",
    "gal": "GALEPHB",
    "bds": "BDSEPHB",
    "bd3": "BD3EPHB",
    "qzss": "QZSSEPHB",
}
EPHEMERIS_MESSAGE_ORDER = (
    "GPSEPHA",
    "GLOEPHA",
    "GALEPHA",
    "BDSEPHA",
    "BD3EPHA",
    "QZSSEPHA",
    "GPSEPHB",
    "GLOEPHB",
    "GALEPHB",
    "BDSEPHB",
    "BD3EPHB",
    "QZSSEPHB",
)
TROPINFO_MESSAGES = {
    "ascii": "TROPINFOA",
    "binary": "TROPINFOB",
}
ION_MESSAGES: dict[str, dict[str, str]] = {
    "gps": {"ascii": "GPSIONA", "binary": "GPSIONB"},
    "bds": {"ascii": "BDSIONA", "binary": "BDSIONB"},
    "bd3": {"ascii": "BD3IONA", "binary": "BD3IONB"},
    "gal": {"ascii": "GALIONA", "binary": "GALIONB"},
}
UTC_MESSAGES: dict[str, dict[str, str]] = {
    "gps": {"ascii": "GPSUTCA", "binary": "GPSUTCB"},
    "bds": {"ascii": "BDSUTCA", "binary": "BDSUTCB"},
    "bd3": {"ascii": "BD3UTCA", "binary": "BD3UTCB"},
    "gal": {"ascii": "GALUTCA", "binary": "GALUTCB"},
}
SBAS_MODES = {"off", "auto", "egnos", "waas", "msas", "gagan", "sdcm", "kass", "bds", "asecna", "span"}
DEFAULT_PPP_TIMEOUT_S = 120
DEFAULT_PPP_CONVERGE = (15, 30)
DEBUG_ASCII_EPHEMERIS_SYSTEMS = ("gps", "glo", "gal", "bds", "bd3", "qzss")
DEBUG_ASCII_EPHEMERIS_PERIOD_S = 300.0
ASCII_EPHEMERIS_WARNING = (
    "WARNING: ASCII ephemeris debug logging can produce large .unc files; "
    "use it only for short diagnostic captures."
)


@dataclass(frozen=True)
class InitProfile:
    """Receiver logging profile used to render UM980 init commands.

    Attributes:
        port: UM980 output port, for example `COM1`.
        baud: Serial baud rate configured for `port`.
        mode: Receiver mode, either `rover` or `base`.
        base_lat: Base latitude in decimal degrees for fixed-base mode.
        base_lon: Base longitude in decimal degrees for fixed-base mode.
        base_height: Base ellipsoidal height in meters for fixed-base mode.
        nmea: Mapping of NMEA/diagnostic message names to output rates in hertz.
        raw_format: Raw observation format (`none`, `obsvma`, `obsvmb`, or
            `obsvmcmpb`).
        raw_hz: Raw observation output rate in hertz.
        bestnav_format: Receiver-solution BESTNAV logging format (`none`,
            `ascii`, or `binary`).
        bestnav_hz: BESTNAV output rate in hertz.
        expected_obs_per_epoch: Expected observations per raw epoch for bitrate
            estimation.
        ephemeris: Mapping of ASCII ephemeris message names to periods in
            seconds or `ONCHANGED`.
        debug_ascii_ephemeris: True when all ASCII ephemeris messages are
            intentionally enabled for diagnostic data collection.
        ppp: Optional PPP mode name.
        ppp_datum: PPP datum setting.
        ppp_timeout: Optional PPP timeout in seconds. PPP mode defaults to
            120 seconds when omitted.
        ppp_converge: Optional PPP convergence threshold pair. PPP mode
            defaults to `(15, 30)` when omitted.
        include_tropinfo: Include the selected-format TROPINFO message as
            `ONCE` then `ONCHANGED`. This requires PPP to be enabled.
        diagnostic_format: `ascii` emits `...A` TROPINFO/ION diagnostics;
            `binary` emits `...B`.
        ion_messages: Ionosphere parameter families to include (`gps`, `bds`,
            `bd3`, `gal`). Each selected family is emitted as `ONCHANGED` and,
            when `ion_period_s` is set, with that periodic interval.
        ion_period_s: Optional periodic repeat interval in seconds for selected
            ionosphere parameter families. When set, the generated commands
            include `MSG PERIOD` in addition to `ONCHANGED`.
        utc_messages: UTC/time-system parameter families to include (`gps`,
            `bds`, `bd3`, `gal`). Each selected family is emitted as
            `ONCHANGED` and, when `utc_period_s` is set, with that periodic
            interval.
        utc_period_s: Optional periodic repeat interval in seconds for selected
            UTC/time-system parameter families.
        sbas: SBAS receiver mode. `off` emits `CONFIG SBAS DISABLE`; other
            values emit `CONFIG SBAS ENABLE <MODE>`.
        sbas_timeout_s: Optional SBAS timeout in seconds. The UM980 command
            accepts `0` to disable SBAS, or 120..1800 seconds on supported
            firmware.
        include_gpsion: Backwards-compatible shortcut for adding `gps` to
            `ion_messages`.
        save_config: Append `SAVECONFIG`.
    """

    port: str = "COM1"
    baud: int = 230400
    mode: str = "rover"
    base_lat: float | None = None
    base_lon: float | None = None
    base_height: float | None = None
    nmea: dict[str, float] = field(default_factory=lambda: dict(NMEA_PRESETS["minimal"]))
    raw_format: str = "none"
    raw_hz: float = 0.0
    bestnav_format: str = "none"
    bestnav_hz: float = 0.0
    expected_obs_per_epoch: int = 100
    ephemeris: dict[str, float | str] = field(default_factory=dict)
    ephemeris_format: str = "ascii"
    debug_ascii_ephemeris: bool = False
    ppp: str = "none"
    ppp_datum: str = "WGS84"
    ppp_timeout: int | None = None
    ppp_converge: tuple[int, int] | None = None
    include_tropinfo: bool = False
    diagnostic_format: str = "ascii"
    ion_messages: tuple[str, ...] = ()
    ion_period_s: float | None = None
    utc_messages: tuple[str, ...] = ()
    utc_period_s: float | None = None
    sbas: str = "off"
    sbas_timeout_s: int | None = None
    include_gpsion: bool = False
    save_config: bool = False


def parse_rate(value: str) -> tuple[str, float]:
    """Parse `MSG=HZ` or `MSG@PERIODs` NMEA rate syntax.

    Args:
        value: CLI rate expression such as `GNGGA=10` or `GNGGA@0.1s`.

    Returns:
        Uppercase message name and rate in hertz.

    Raises:
        ValueError: If the expression is not in a supported form.
    """

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
    """Parse repeated or comma-separated NMEA override arguments.

    Args:
        values: Repeated CLI values, each containing one or more comma-separated
            `MSG=HZ` or `MSG@PERIODs` expressions.

    Returns:
        Mapping from uppercase message name to output rate in hertz.
    """

    parsed: dict[str, float] = {}
    for item in values or []:
        for part in item.split(","):
            part = part.strip()
            if part:
                msg, hz = parse_rate(part)
                parsed[msg] = hz
    return parsed


def hz_to_period_text(hz: float) -> str:
    """Convert a rate in hertz to UM980 period text.

    Args:
        hz: Output rate in hertz.

    Returns:
        UM980 command period text, or `0` for disabled output.
    """

    if hz <= 0:
        return "0"
    period = 1.0 / hz
    if abs(period - round(period)) < 0.0001:
        return str(int(round(period)))
    return f"{period:.4f}".rstrip("0").rstrip(".")


def ephemeris_policy(
    policy: str,
    systems: list[str],
    *,
    message_format: str = "ascii",
) -> dict[str, float | str]:
    """Build an ASCII ephemeris command policy.

    Args:
        policy: `off`, `none`, `onchanged`, or `every=SECONDS`.
        systems: Short constellation names such as `gps`, `glo`, and `gal`.
        message_format: `ascii` for `...A` messages or `binary` for `...B`
            messages.

    Returns:
        Mapping from UM980 ASCII ephemeris message name to period setting.

    Raises:
        ValueError: If `policy` is not supported.
    """

    if policy in {"off", "none"}:
        return {}
    value: float | str
    if policy == "onchanged":
        value = "ONCHANGED"
    elif policy.startswith("every="):
        value = float(policy.split("=", 1)[1])
    else:
        raise ValueError(f"invalid ephemeris policy: {policy}")
    messages = BINARY_EPHEMERIS_MESSAGES if message_format == "binary" else EPHEMERIS_MESSAGES
    return {messages[system]: value for system in systems if system in messages}


def debug_ascii_ephemeris_policy() -> dict[str, float | str]:
    """Return the all-constellation ASCII ephemeris policy for debugging.

    Returns:
        Mapping that emits `GPSEPHA`, `GLOEPHA`, `GALEPHA`, `BDSEPHA`,
        `BD3EPHA`, and `QZSSEPHA` every 300 seconds.
    """

    return ephemeris_policy(
        f"every={DEBUG_ASCII_EPHEMERIS_PERIOD_S:g}",
        list(DEBUG_ASCII_EPHEMERIS_SYSTEMS),
    )


def is_debug_ascii_ephemeris_policy(ephemeris: dict[str, float | str]) -> bool:
    """Return true when all ASCII ephemeris debug messages are enabled.

    Args:
        ephemeris: Mapping from UM980 ASCII ephemeris message to period setting.

    Returns:
        True when the policy includes every debug ephemeris message, regardless
        of the exact period value.
    """

    return set(ephemeris) >= set(EPHEMERIS_MESSAGES.values())


def is_binary_ephemeris_policy(ephemeris: dict[str, float | str]) -> bool:
    """Return true when any binary ephemeris message is enabled."""

    return bool(set(ephemeris) & set(BINARY_EPHEMERIS_MESSAGES.values()))


def _profile_ion_messages(profile: InitProfile) -> tuple[str, ...]:
    """Return unique ionosphere families enabled for a profile."""

    return tuple(dict.fromkeys((*profile.ion_messages, *(("gps",) if profile.include_gpsion else ()))))


def _ion_message_name(family: str, diagnostic_format: str) -> str:
    """Return the UM980 ionosphere command name for a family and format."""

    return ION_MESSAGES[family][diagnostic_format]


def _utc_message_name(family: str, diagnostic_format: str) -> str:
    """Return the UM980 UTC/time-system command name for a family and format."""

    return UTC_MESSAGES[family][diagnostic_format]


def _bestnav_message_name(profile: InitProfile) -> str | None:
    """Return the selected BESTNAV command name, if enabled."""

    if profile.bestnav_format == "ascii":
        return "BESTNAVA"
    if profile.bestnav_format == "binary":
        return "BESTNAVB"
    return None


def _effective_nmea_rates(profile: InitProfile) -> dict[str, float]:
    """Return NMEA-like periodic rates used for serial-capacity estimates."""

    rates = dict(profile.nmea)
    bestnav_message = _bestnav_message_name(profile)
    if bestnav_message and profile.bestnav_hz > 0:
        rates[bestnav_message] = profile.bestnav_hz
    if profile.ion_period_s and profile.ion_period_s > 0:
        for family in _profile_ion_messages(profile):
            rates[_ion_message_name(family, profile.diagnostic_format)] = 1.0 / profile.ion_period_s
    if profile.utc_period_s and profile.utc_period_s > 0:
        for family in profile.utc_messages:
            rates[_utc_message_name(family, profile.diagnostic_format)] = 1.0 / profile.utc_period_s
    return rates


def bitrate_comparison(profile: InitProfile) -> dict[str, BitrateEstimate]:
    """Estimate bitrate for all supported raw formats at the profile rate.

    Args:
        profile: Init profile whose NMEA, raw rate, observation count, and
            ephemeris settings are reused for each raw format.

    Returns:
        Mapping from raw format name to bitrate estimate.
    """

    return {
        fmt: estimate_bitrate(
            baud=profile.baud,
            nmea_rates_hz=_effective_nmea_rates(profile),
            raw_format=fmt,
            raw_hz=profile.raw_hz,
            expected_obs_per_epoch=profile.expected_obs_per_epoch,
            ephemeris_periods_s=profile.ephemeris,
        )
        for fmt in ("obsvma", "obsvmb", "obsvmcmpb")
    }


def validate_profile(profile: InitProfile) -> None:
    """Validate an init profile before rendering commands.

    Args:
        profile: Candidate receiver logging profile.

    Raises:
        ValueError: If the mode, base coordinates, or raw format are invalid.
    """

    if profile.mode not in {"rover", "base"}:
        raise ValueError(f"unsupported mode: {profile.mode}")
    if profile.mode == "base" and (
        profile.base_lat is None or profile.base_lon is None or profile.base_height is None
    ):
        raise ValueError("mode base requires --base-lat, --base-lon and --base-height")
    if profile.raw_format not in {"none", "obsvma", "obsvmb", "obsvmcmpb"}:
        raise ValueError(f"unsupported raw format: {profile.raw_format}")
    if profile.bestnav_format not in {"none", "ascii", "binary"}:
        raise ValueError(f"unsupported BESTNAV format: {profile.bestnav_format}")
    if profile.bestnav_format != "none" and profile.bestnav_hz <= 0:
        raise ValueError("BESTNAV frequency must be greater than zero when BESTNAV logging is enabled")
    if profile.ephemeris_format not in {"ascii", "binary"}:
        raise ValueError(f"unsupported ephemeris format: {profile.ephemeris_format}")
    if profile.diagnostic_format not in {"ascii", "binary"}:
        raise ValueError(f"unsupported diagnostic format: {profile.diagnostic_format}")
    if profile.include_tropinfo and profile.ppp == "none":
        raise ValueError("TROPINFOA/TROPINFOB logging requires PPP to be enabled")
    invalid_ion = sorted(set(profile.ion_messages) - set(ION_MESSAGES))
    if invalid_ion:
        raise ValueError(f"unsupported ionosphere message families: {', '.join(invalid_ion)}")
    if profile.ion_period_s is not None and profile.ion_period_s <= 0:
        raise ValueError("ionosphere repeat period must be greater than zero seconds")
    invalid_utc = sorted(set(profile.utc_messages) - set(UTC_MESSAGES))
    if invalid_utc:
        raise ValueError(f"unsupported UTC/time-system message families: {', '.join(invalid_utc)}")
    if profile.utc_period_s is not None and profile.utc_period_s <= 0:
        raise ValueError("UTC/time-system repeat period must be greater than zero seconds")
    if profile.sbas not in SBAS_MODES:
        raise ValueError(f"unsupported SBAS mode: {profile.sbas}")
    if profile.sbas_timeout_s is not None and (
        profile.sbas_timeout_s < 0 or (0 < profile.sbas_timeout_s < 120) or profile.sbas_timeout_s > 1800
    ):
        raise ValueError("SBAS timeout must be 0 or in the 120..1800 second range")


def render_init_script(
    profile: InitProfile,
    *,
    strict_bitrate: bool = False,
    allow_overload: bool = False,
) -> tuple[str, BitrateEstimate]:
    """Generate UM980 commands and return the bitrate estimate.

    Args:
        profile: Receiver logging profile to render.
        strict_bitrate: Raise when estimated utilisation is at or above serial
            capacity.
        allow_overload: Permit over-capacity profiles even when
            `strict_bitrate` is true.

    Returns:
        Tuple of init script text and the computed bitrate estimate.

    Raises:
        ValueError: If profile validation fails or strict bitrate checking
            rejects the profile.
    """

    validate_profile(profile)
    estimate = estimate_bitrate(
        baud=profile.baud,
        nmea_rates_hz=_effective_nmea_rates(profile),
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
        f"# Estimated NMEA payload: {estimate.nmea_bytes_per_s / 1000:.1f} kB/s",
        f"# Estimated raw payload: {estimate.raw_bytes_per_s / 1000:.1f} kB/s",
        f"# Estimated ephemeris payload: {estimate.ephemeris_bytes_per_s / 1000:.2f} kB/s",
        f"# Estimated 8N1 line rate: {estimate.line_rate_bits_per_s / 1000:.0f} kbps",
        f"# Utilisation: {estimate.utilisation:.0%}",
        f"# Assessment: {estimate.assessment}",
    ]
    if estimate.utilisation >= 1.0:
        lines.append("# WARNING: requested logging profile exceeds estimated serial capacity")
    if profile.debug_ascii_ephemeris or is_debug_ascii_ephemeris_policy(profile.ephemeris):
        lines.append(f"# {ASCII_EPHEMERIS_WARNING}")
    if is_binary_ephemeris_policy(profile.ephemeris):
        lines.append(
            "# Binary ephemeris logging requested. Verify receiver command responses; "
            "older init scripts that used GPSEPH/GLOEPH/... without B are invalid."
        )
    lines.extend(["", f"CONFIG {profile.port} {profile.baud}", ""])

    if profile.ppp != "none":
        lines.append(f"CONFIG PPP ENABLE {profile.ppp.upper()}")
        lines.append(f"CONFIG PPP DATUM {profile.ppp_datum}")
        ppp_timeout = profile.ppp_timeout or DEFAULT_PPP_TIMEOUT_S
        ppp_converge = profile.ppp_converge or DEFAULT_PPP_CONVERGE
        lines.append(f"CONFIG PPP TIMEOUT {ppp_timeout}")
        lines.append(f"CONFIG PPP CONVERGE {ppp_converge[0]} {ppp_converge[1]}")
        lines.append("")

    if profile.mode == "base":
        lines.append(
            f"MODE BASE {profile.base_lat:.10f} {profile.base_lon:.10f} {profile.base_height:.4f}"
        )
    else:
        lines.append("MODE ROVER")
    lines.append("")

    if profile.sbas == "off":
        lines.append("CONFIG SBAS DISABLE")
    else:
        lines.append(f"CONFIG SBAS ENABLE {profile.sbas.upper()}")
    if profile.sbas_timeout_s is not None:
        lines.append(f"CONFIG SBAS TIMEOUT {profile.sbas_timeout_s:g}")
    lines.append("")

    bestnav_message = _bestnav_message_name(profile)
    if bestnav_message and profile.bestnav_hz > 0:
        lines.append(f"{bestnav_message} {profile.port} {hz_to_period_text(profile.bestnav_hz)}")
        lines.append("")

    if profile.raw_format != "none" and profile.raw_hz > 0:
        lines.append(f"{profile.raw_format.upper()} {profile.port} {hz_to_period_text(profile.raw_hz)}")
        lines.append("")

    for message in sorted(profile.nmea):
        if profile.nmea[message] <= 0:
            continue
        lines.append(f"{message} {hz_to_period_text(profile.nmea[message])}")
    if profile.nmea:
        lines.append("")

    for message in EPHEMERIS_MESSAGE_ORDER:
        if message not in profile.ephemeris:
            continue
        period = profile.ephemeris[message]
        period_text = period if isinstance(period, str) else f"{period:g}"
        lines.append(f"{message} {profile.port} {period_text}")
    if profile.ephemeris:
        lines.append("")

    if profile.include_tropinfo:
        message = TROPINFO_MESSAGES[profile.diagnostic_format]
        lines.append(f"{message} ONCE")
        lines.append(f"{message} ONCHANGED")
    ion_messages = _profile_ion_messages(profile)
    for family in ion_messages:
        message = _ion_message_name(family, profile.diagnostic_format)
        lines.append(f"{message} ONCHANGED")
        if profile.ion_period_s:
            lines.append(f"{message} {profile.ion_period_s:g}")
    for family in profile.utc_messages:
        message = _utc_message_name(family, profile.diagnostic_format)
        lines.append(f"{message} ONCHANGED")
        if profile.utc_period_s:
            lines.append(f"{message} {profile.utc_period_s:g}")
    if profile.include_tropinfo or ion_messages or profile.utc_messages:
        lines.append("")

    if profile.save_config:
        lines.append("SAVECONFIG")

    return "\n".join(lines).rstrip() + "\n", estimate


def write_json_report(path: Path, profile: InitProfile, estimate: BitrateEstimate) -> None:
    """Write a JSON report for a generated init script.

    Args:
        path: Destination report path.
        profile: Receiver logging profile used to generate the script.
        estimate: Bitrate estimate returned by `render_init_script`.
    """

    payload = {
        "requested_configuration": {
            "port": profile.port,
            "baud": profile.baud,
            "raw_format": profile.raw_format,
            "raw_hz": profile.raw_hz,
            "bestnav_format": profile.bestnav_format,
            "bestnav_hz": profile.bestnav_hz,
            "expected_obs_per_epoch": profile.expected_obs_per_epoch,
            "ephemeris_format": profile.ephemeris_format,
            "debug_ascii_ephemeris": profile.debug_ascii_ephemeris,
            "ppp": profile.ppp,
            "ppp_timeout": profile.ppp_timeout or (
                DEFAULT_PPP_TIMEOUT_S if profile.ppp != "none" else None
            ),
            "ppp_converge": profile.ppp_converge or (
                DEFAULT_PPP_CONVERGE if profile.ppp != "none" else None
            ),
            "include_tropinfo": profile.include_tropinfo,
            "diagnostic_format": profile.diagnostic_format,
            "ion_messages": list(profile.ion_messages),
            "ion_period_s": profile.ion_period_s,
            "utc_messages": list(profile.utc_messages),
            "utc_period_s": profile.utc_period_s,
            "sbas": profile.sbas,
            "sbas_timeout_s": profile.sbas_timeout_s,
        },
        "estimated_payload": estimate.as_dict(),
        "format_comparison_at_requested_rate": {
            name: value.as_dict() for name, value in bitrate_comparison(profile).items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
