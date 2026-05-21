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
    "GPSIONB": 120,
}

GSV_LINES_PER_EPOCH: dict[str, int] = {
    "GPGSV": 4,
    "GAGSV": 3,
    "GLGSV": 2,
    "GBGSV": 5,
    "GNGSV": 12,
}

DEFAULT_EPH_BYTES: dict[str, int] = {
    "GPSEPHA": 350,
    "GLOEPHA": 250,
    "GALEPHA": 400,
    "BDSEPHA": 400,
    "BD3EPHA": 400,
    "QZSSEPHA": 350,
}


@dataclass(frozen=True)
class BitrateEstimate:
    """Estimated UM980 serial payload and 8N1 line utilisation."""

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
    """Return a conservative byte estimate for one raw observation epoch."""

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
    """Estimate NMEA bytes per second for a message-rate mapping."""

    total = 0.0
    for message, hz in nmea_rates_hz.items():
        if hz <= 0:
            continue
        msg = message.upper()
        lines = GSV_LINES_PER_EPOCH.get(msg, 1)
        total += DEFAULT_NMEA_BYTES.get(msg, 100) * lines * hz
    return total


def ephemeris_payload_rate(ephemeris_periods_s: Mapping[str, float | str]) -> float:
    """Estimate average ephemeris bytes per second from period settings."""

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
            total += DEFAULT_EPH_BYTES.get(msg, 350) / seconds
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
    """Estimate payload and line utilisation for one logging profile."""

    nmea = nmea_payload_rate(nmea_rates_hz)
    raw = raw_epoch_bytes(raw_format, expected_obs_per_epoch) * max(raw_hz, 0.0)
    eph = ephemeris_payload_rate(ephemeris_periods_s or {})
    return BitrateEstimate(
        baud=baud,
        nmea_bytes_per_s=nmea,
        raw_bytes_per_s=raw,
        ephemeris_bytes_per_s=eph,
    )

