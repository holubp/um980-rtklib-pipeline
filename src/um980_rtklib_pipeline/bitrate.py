"""Serial bitrate estimation for UM980 logging profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DEFAULT_NMEA_BYTES: dict[str, int] = {
    "GNGGA": 95,
    "GNRMC": 95,
    "GNGST": 90,
    "GNGNS": 105,
    "GNGLL": 75,
    "GPGRS": 120,
    "GNGSA": 85,
    "GPGSA": 85,
    "GAGSA": 85,
    "GLGSA": 85,
    "GBGSA": 85,
    "GPGSV": 90,
    "GAGSV": 90,
    "GLGSV": 90,
    "GBGSV": 90,
    "GNGSV": 90,
    "PPPNAVA": 180,
    "ADRNAVA": 180,
    "TROPINFOA": 160,
    "TROPINFOB": 160,
    "GPSIONA": 120,
    "GPSIONB": 120,
    "BDSIONA": 120,
    "BDSIONB": 120,
    "BD3IONA": 120,
    "BD3IONB": 120,
    "GALIONA": 120,
    "GALIONB": 120,
}

GSV_LINES_PER_EPOCH: dict[str, int] = {
    "GPGSV": 4,
    "GAGSV": 3,
    "GLGSV": 2,
    "GBGSV": 5,
    "GNGSV": 12,
}

DEFAULT_EPH_BYTES: dict[str, int] = {
    # ASCII line lengths include receiver header, payload, checksum, and CRLF.
    # GPS/GLO values are measured from private UM980 captures. The remaining
    # values are conservative estimates from RTKLIB-ex binary payload sizes with
    # ASCII float expansion.
    "GPSEPHA": 455,
    "GLOEPHA": 380,
    "GALEPHA": 460,
    "BDSEPHA": 500,
    "BD3EPHA": 500,
    "QZSSEPHA": 455,
    # Binary ephemeris frame sizes use RTKLIB-ex Unicore payload structures plus
    # the fixed UM980 binary header and CRC.
    "GPSEPHB": 256,
    "GLOEPHB": 184,
    "GALEPHB": 260,
    "BDSEPHB": 268,
    "BD3EPHB": 268,
    "QZSSEPHB": 256,
}
DEFAULT_EPH_RECORDS_PER_PERIOD: dict[str, int] = {
    "GPSEPHA": 32,
    "GLOEPHA": 14,
    "GALEPHA": 32,
    "BDSEPHA": 40,
    "BD3EPHA": 40,
    "QZSSEPHA": 4,
    "GPSEPHB": 32,
    "GLOEPHB": 14,
    "GALEPHB": 32,
    "BDSEPHB": 40,
    "BD3EPHB": 40,
    "QZSSEPHB": 4,
}


@dataclass(frozen=True)
class BitrateEstimate:
    """Estimated UM980 serial payload and 8N1 line utilisation.

    Attributes:
        baud: Configured serial baud rate in bits per second.
        nmea_bytes_per_s: Estimated average NMEA payload bytes per second.
        raw_bytes_per_s: Estimated average raw-observation payload bytes per
            second.
        ephemeris_bytes_per_s: Estimated average ephemeris payload bytes per
            second.
    """

    baud: int
    nmea_bytes_per_s: float
    raw_bytes_per_s: float
    ephemeris_bytes_per_s: float

    @property
    def total_bytes_per_s(self) -> float:
        return self.nmea_bytes_per_s + self.raw_bytes_per_s + self.ephemeris_bytes_per_s

    @property
    def payload_capacity_bytes_per_s(self) -> float:
        return self.baud / 10.0

    @property
    def line_rate_bits_per_s(self) -> float:
        return self.total_bytes_per_s * 10.0

    @property
    def utilisation(self) -> float:
        if self.payload_capacity_bytes_per_s == 0:
            return float("inf")
        return self.total_bytes_per_s / self.payload_capacity_bytes_per_s

    @property
    def assessment(self) -> str:
        util = self.utilisation
        if util < 0.70:
            return "OK"
        if util < 0.85:
            return "WARNING near limit"
        if util < 1.0:
            return "WARNING high risk of gaps"
        return "ERROR over capacity"

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "baud": self.baud,
            "nmea_bytes_per_s": self.nmea_bytes_per_s,
            "raw_bytes_per_s": self.raw_bytes_per_s,
            "ephemeris_bytes_per_s": self.ephemeris_bytes_per_s,
            "total_bytes_per_s": self.total_bytes_per_s,
            "serial_payload_capacity_bytes_per_s": self.payload_capacity_bytes_per_s,
            "line_rate_bits_per_s": self.line_rate_bits_per_s,
            "utilisation": self.utilisation,
            "assessment": self.assessment,
        }


def raw_epoch_bytes(raw_format: str, nobs: int) -> int:
    """Return a conservative byte estimate for one raw observation epoch.

    Args:
        raw_format: UM980 raw observation message family, such as `obsvma`,
            `obsvmb`, `obsvmcmpb`, or `none`.
        nobs: Expected observations in one epoch.

    Returns:
        Estimated bytes emitted for one raw observation epoch.

    Raises:
        ValueError: If `raw_format` is not supported.
    """

    fmt = raw_format.lower()
    if fmt == "none":
        return 0
    if fmt == "obsvma":
        return 300 + nobs * 54
    if fmt == "obsvmb":
        return 24 + 4 + nobs * 40 + 4
    if fmt == "obsvmcmpb":
        return 24 + 4 + nobs * 24 + 4
    raise ValueError(f"unsupported raw format: {raw_format}")


def nmea_payload_rate(nmea_rates_hz: Mapping[str, float]) -> float:
    """Estimate NMEA bytes per second for a message-rate mapping.

    Args:
        nmea_rates_hz: Mapping from NMEA/diagnostic message name to output rate
            in hertz.

    Returns:
        Estimated average bytes per second for all enabled messages.
    """

    total = 0.0
    for message, hz in nmea_rates_hz.items():
        if hz <= 0:
            continue
        msg = message.upper()
        lines = GSV_LINES_PER_EPOCH.get(msg, 1)
        total += DEFAULT_NMEA_BYTES.get(msg, 100) * lines * hz
    return total


def ephemeris_payload_rate(ephemeris_periods_s: Mapping[str, float | str]) -> float:
    """Estimate average ephemeris bytes per second from period settings.

    Args:
        ephemeris_periods_s: Mapping from UM980 ephemeris message name to
            either a numeric period in seconds or `ONCHANGED`.

    Returns:
        Estimated average bytes per second contributed by ephemeris logging.
        Each enabled message is multiplied by the expected number of satellite
        ephemeris records emitted during one period.
    """

    total = 0.0
    for message, period in ephemeris_periods_s.items():
        msg = message.upper()
        if isinstance(period, str):
            if period.upper() == "ONCHANGED":
                seconds = 300.0
            else:
                continue
        else:
            seconds = float(period)
        if seconds > 0:
            total += (
                DEFAULT_EPH_BYTES.get(msg, 450)
                * DEFAULT_EPH_RECORDS_PER_PERIOD.get(msg, 1)
                / seconds
            )
    return total


def estimate_bitrate(
    *,
    baud: int,
    nmea_rates_hz: Mapping[str, float],
    raw_format: str,
    raw_hz: float,
    expected_obs_per_epoch: int = 100,
    ephemeris_periods_s: Mapping[str, float | str] | None = None,
) -> BitrateEstimate:
    """Estimate payload and line utilisation for one logging profile.

    Args:
        baud: Serial baud rate in bits per second.
        nmea_rates_hz: Mapping from NMEA message name to output rate in hertz.
        raw_format: Raw observation format (`none`, `obsvma`, `obsvmb`, or
            `obsvmcmpb`).
        raw_hz: Raw observation output rate in hertz.
        expected_obs_per_epoch: Expected observation count in each raw epoch.
        ephemeris_periods_s: Optional mapping of ephemeris message periods.

    Returns:
        A bitrate estimate with payload and serial-line utilisation fields.
    """

    nmea = nmea_payload_rate(nmea_rates_hz)
    raw = raw_epoch_bytes(raw_format, expected_obs_per_epoch) * max(raw_hz, 0.0)
    eph = ephemeris_payload_rate(ephemeris_periods_s or {})
    return BitrateEstimate(
        baud=baud,
        nmea_bytes_per_s=nmea,
        raw_bytes_per_s=raw,
        ephemeris_bytes_per_s=eph,
    )
