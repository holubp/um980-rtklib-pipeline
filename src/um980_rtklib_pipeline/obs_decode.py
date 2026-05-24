"""UM980 raw observation decoding."""

from __future__ import annotations

import csv
import logging
import struct
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Literal

from .stream import StreamRecord
from .timeutil import gps_week_tow_to_datetime


SystemName = Literal["GPS", "GLONASS", "Galileo", "BDS", "QZSS", "SBAS", "IRNSS", "Unknown"]

RINEX_SYSTEM_PREFIX = {
    "GPS": "G",
    "GLONASS": "R",
    "Galileo": "E",
    "BDS": "C",
    "QZSS": "J",
    "SBAS": "S",
    "IRNSS": "I",
    "Unknown": "U",
}

SYSTEM_ALIASES = {
    "GPS": "GPS",
    "GLO": "GLONASS",
    "GLONASS": "GLONASS",
    "GAL": "Galileo",
    "GALILEO": "Galileo",
    "BDS": "BDS",
    "BD": "BDS",
    "BEIDOU": "BDS",
    "QZSS": "QZSS",
    "SBAS": "SBAS",
    "IRNSS": "IRNSS",
}

UM980_SYSTEM_IDS = {
    0: "GPS",
    1: "GLONASS",
    2: "SBAS",
    3: "Galileo",
    4: "BDS",
    5: "QZSS",
    6: "IRNSS",
    # Observed UM980 captures use extended IDs for some constellations.
    8: "Galileo",
    9: "BDS",
}

TRACKING_STATUS_SYSTEM_IDS = {
    0: "GPS",
    1: "GLONASS",
    2: "SBAS",
    3: "Galileo",
    4: "BDS",
    5: "QZSS",
    6: "IRNSS",
}

DEFAULT_SIGNAL_CODES = {
    ("GPS", "L1"): "1C",
    ("GPS", "L1CA"): "1C",
    ("GPS", "L2"): "2L",
    ("GPS", "L5"): "5Q",
    ("Galileo", "E1"): "1C",
    ("Galileo", "E5A"): "5Q",
    ("Galileo", "E5B"): "7Q",
    ("Galileo", "E6"): "6C",
    ("GLONASS", "G1"): "1C",
    ("GLONASS", "G2"): "2C",
    ("GLONASS", "G3"): "3Q",
    ("BDS", "B1I"): "2I",
    ("BDS", "B1C"): "1P",
    ("BDS", "B2I"): "7I",
    ("BDS", "B2A"): "5P",
    ("BDS", "B3I"): "6I",
    ("SBAS", "L1"): "1C",
}

TRACKING_SIGNAL_CODES = {
    "GPS": {
        0: ("L1 C/A", "1C"),
        3: ("L1C pilot", "1L"),
        6: ("L5 data", "5I"),
        9: ("L2 P(Y)", "2W"),
        11: ("L1C data", "1S"),
        14: ("L5 pilot", "5Q"),
        17: ("L2C L", "2L"),
    },
    "GLONASS": {
        0: ("G1 C/A", "1C"),
        5: ("G2 C/A", "2C"),
        6: ("G3 I", "3I"),
        7: ("G3 Q", "3Q"),
    },
    "Galileo": {
        1: ("E1 B", "1B"),
        2: ("E1 C", "1C"),
        12: ("E5a pilot", "5Q"),
        17: ("E5b pilot", "7Q"),
        18: ("E6 B", "6B"),
        22: ("E6 C", "6C"),
    },
    "BDS": {
        0: ("B1I", "2I"),
        4: ("B1Q", "2Q"),
        5: ("B2Q", "7Q"),
        6: ("B3Q", "6Q"),
        8: ("B1C pilot", "1P"),
        12: ("B2a pilot", "5P"),
        13: ("B2b I", "7P"),
        17: ("B2I", "7I"),
        21: ("B3I", "6I"),
        23: ("B1C data", "1D"),
        28: ("B2a data", "5D"),
    },
    "QZSS": {
        0: ("L1 C/A", "1C"),
        3: ("L1C pilot", "1L"),
        6: ("L5 data", "5I"),
        9: ("L2 P(Y)", "2X"),
        11: ("L1C data", "1S"),
        14: ("L5 pilot", "5Q"),
    },
    "SBAS": {
        0: ("L1 C/A", "1C"),
    },
    "IRNSS": {
        6: ("L5 data", "5A"),
        14: ("L5 pilot", "5B"),
    },
}

BINARY_EPHEMERIS_TYPES = {
    "GPSEPHB",
    "GLOEPHB",
    "GALEPHB",
    "BDSEPHB",
    "BD3EPHB",
    "QZSSEPHB",
    "IRNSSEPHB",
}
OBSVMB_RECORD_BYTES = 40
OBSVMB_HEADER_BYTES = 24
OBSVMB_TIME_UNKNOWN = 201
OBSVMCMPB_RECORD_BYTES = 24
OBSVMCMPB_DOPPLER_SCALE = 256.0
OBSVMCMPB_PSEUDORANGE_SCALE = 128.0
OBSVMCMPB_ADR_SCALE = 256.0
OBSVMCMPB_LOCK_TIME_SCALE = 32.0


@dataclass
class Observation:
    """One decoded raw GNSS observation.

    Attributes:
        gps_week: GPS week number.
        tow: GPS seconds of week.
        sat_system: GNSS constellation name.
        sv: Satellite vehicle number in RINEX numbering.
        rinex_sat: RINEX satellite identifier.
        signal_name: Receiver or decoded signal name.
        rinex_code: RINEX observation code suffix.
        band: RINEX frequency band digit.
        pseudorange_m: Code pseudorange in meters.
        carrier_phase_cycles: Carrier phase in cycles.
        doppler_hz: Doppler in hertz.
        cn0_dbhz: Carrier-to-noise density in dB-Hz.
        lock_time_s: Lock time in seconds.
        half_cycle: Half-cycle ambiguity flag when known.
        lli: RINEX loss-of-lock indicator.
        raw_tracking_status: Original UM980 tracking status word.
    """

    gps_week: int
    tow: float
    sat_system: SystemName
    sv: int
    rinex_sat: str
    signal_name: str
    rinex_code: str
    band: str
    pseudorange_m: float | None
    carrier_phase_cycles: float | None
    doppler_hz: float | None
    cn0_dbhz: float | None
    lock_time_s: float | None
    half_cycle: bool | None
    lli: int
    raw_tracking_status: int


@dataclass
class ObservationExtraction:
    """Decoded observation extraction result.

    Attributes:
        observations: Decoded observations.
        unsupported_records: Counts of records that could not be decoded.
        metrics: Aggregate observation metrics.
        warnings: User-facing extraction warnings.
    """

    observations: list[Observation]
    unsupported_records: dict[str, int]
    metrics: dict[str, object]
    warnings: list[str]


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _int_auto(value: str) -> int | None:
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return _int(value)


def _system(value: str) -> SystemName:
    return SYSTEM_ALIASES.get(value.strip().upper(), "Unknown")  # type: ignore[return-value]


def _system_from_id(value: int | None) -> SystemName:
    if value is None:
        return "Unknown"
    return UM980_SYSTEM_IDS.get(value, "Unknown")  # type: ignore[return-value]


def _tracking_system(tracking: int) -> SystemName:
    return TRACKING_STATUS_SYSTEM_IDS.get((tracking >> 16) & 0x7, "Unknown")  # type: ignore[return-value]


def _tracking_signal(system: SystemName, tracking: int) -> tuple[str, str, bool]:
    signal_type = (tracking >> 21) & 0x1F
    l2c = bool(tracking & 0x04000000)
    if system in {"GPS", "QZSS"} and signal_type == 9 and l2c:
        return "L2C", "2L", True
    mapping = TRACKING_SIGNAL_CODES.get(system, {})
    if signal_type in mapping:
        name, code = mapping[signal_type]
        return name, code, True
    return f"TRACK_{tracking:08x}", _rinex_code(system, "L1"), False


def _obsvmb_payload(raw: bytes) -> bytes:
    """Return the declared OBSVMB payload from a Unicore binary frame."""

    if len(raw) < OBSVMB_HEADER_BYTES + 4 + 4:
        raise ValueError("OBSVMB frame is shorter than the binary header, count, and CRC")
    payload_length = int.from_bytes(raw[6:8], "little", signed=False)
    if len(raw) < OBSVMB_HEADER_BYTES + payload_length + 4:
        raise ValueError(
            f"OBSVMB frame has {len(raw) - OBSVMB_HEADER_BYTES - 4} payload bytes, "
            f"declares {payload_length}"
        )
    return raw[OBSVMB_HEADER_BYTES : OBSVMB_HEADER_BYTES + payload_length]


def _signed_bits(value: int, bits: int) -> int:
    """Interpret a fixed-width two's-complement integer.

    Args:
        value: Unsigned integer value.
        bits: Number of significant bits.

    Returns:
        Signed integer represented by `value`.

    Raises:
        ValueError: If `bits` is not positive.
    """

    if bits <= 0:
        raise ValueError(f"bit width must be positive, got {bits}")
    sign = 1 << (bits - 1)
    mask = (1 << bits) - 1
    value &= mask
    return value - (1 << bits) if value & sign else value


def _binary_payload(raw: bytes, msg_type: str) -> bytes:
    """Return the declared payload from a fixed-header Unicore binary frame.

    Args:
        raw: Raw frame bytes including header and CRC.
        msg_type: Message type for actionable errors.

    Returns:
        Declared payload bytes.

    Raises:
        ValueError: If the frame is too short or declares unavailable bytes.
    """

    if len(raw) < OBSVMB_HEADER_BYTES + 4 + 4:
        raise ValueError(f"{msg_type} frame is shorter than the binary header, count, and CRC")
    payload_length = int.from_bytes(raw[6:8], "little", signed=False)
    if len(raw) < OBSVMB_HEADER_BYTES + payload_length + 4:
        raise ValueError(
            f"{msg_type} frame has {len(raw) - OBSVMB_HEADER_BYTES - 4} payload bytes, "
            f"declares {payload_length}"
        )
    return raw[OBSVMB_HEADER_BYTES : OBSVMB_HEADER_BYTES + payload_length]


def _binary_time(record: StreamRecord) -> tuple[int, float] | None:
    """Return GPS week and seconds-of-week from a Unicore binary header.

    Args:
        record: Parsed binary stream record.

    Returns:
        `(gps_week, tow_seconds)` when the receiver time is usable, otherwise
        `None`.
    """

    if not record.raw:
        return None
    time_status = record.raw[9] if len(record.raw) > 9 else OBSVMB_TIME_UNKNOWN
    week = int.from_bytes(record.raw[10:12], "little", signed=False) if len(record.raw) >= 12 else 0
    tow_ms = int.from_bytes(record.raw[12:16], "little", signed=False) if len(record.raw) >= 16 else 0
    if time_status == OBSVMB_TIME_UNKNOWN or week == 0:
        return None
    return week, tow_ms / 1000.0


def _obsvmb_observations(record: StreamRecord) -> list[Observation]:
    """Decode a documented UM980 OBSVMB binary observation frame.

    Args:
        record: Parsed binary `OBSVMB` stream record.

    Returns:
        Decoded observations for one receiver epoch.

    Raises:
        ValueError: If the payload length is inconsistent with the observation
            count.
    """

    binary_time = _binary_time(record)
    if binary_time is None:
        return []
    week, tow = binary_time
    payload = _obsvmb_payload(record.raw)
    nobs = struct.unpack_from("<I", payload, 0)[0]
    expected = 4 + nobs * OBSVMB_RECORD_BYTES
    if len(payload) < expected:
        raise ValueError(f"OBSVMB payload has {len(payload)} bytes, expected {expected} for {nobs} observations")
    observations: list[Observation] = []
    for index in range(nobs):
        offset = 4 + index * OBSVMB_RECORD_BYTES
        glo_frequency = struct.unpack_from("<H", payload, offset)[0]
        raw_prn = struct.unpack_from("<H", payload, offset + 2)[0]
        pseudorange = struct.unpack_from("<d", payload, offset + 4)[0]
        adr = struct.unpack_from("<d", payload, offset + 12)[0]
        doppler = struct.unpack_from("<f", payload, offset + 24)[0]
        cn0_raw = struct.unpack_from("<H", payload, offset + 28)[0]
        lock_time = struct.unpack_from("<f", payload, offset + 32)[0]
        tracking = struct.unpack_from("<I", payload, offset + 36)[0]

        system = _tracking_system(tracking)
        signal, code, signal_known = _tracking_signal(system, tracking)
        phase_valid = bool(tracking & 0x00000400)
        pseudorange_valid = bool(tracking & 0x00001000)
        rinex_sv = _rinex_sv(system, raw_prn)
        prefix = RINEX_SYSTEM_PREFIX[system]
        observations.append(
            Observation(
                gps_week=week,
                tow=tow,
                sat_system=system,
                sv=rinex_sv,
                rinex_sat=f"{prefix}{rinex_sv:02d}" if prefix != "U" else f"U{rinex_sv:02d}",
                signal_name=signal if system != "GLONASS" else f"{signal} FCN={glo_frequency}",
                rinex_code=code,
                band=code[0],
                pseudorange_m=pseudorange if pseudorange_valid else None,
                carrier_phase_cycles=adr if phase_valid else None,
                doppler_hz=doppler if phase_valid else None,
                cn0_dbhz=cn0_raw / 100.0,
                lock_time_s=lock_time,
                half_cycle=None,
                lli=0,
                raw_tracking_status=tracking if signal_known else tracking,
            )
        )
    return observations


def _obsvmcmpb_observations(record: StreamRecord) -> list[Observation]:
    """Decode a documented UM980 OBSVMCMPB compressed observation frame.

    Args:
        record: Parsed binary `OBSVMCMPB` stream record.

    Returns:
        Decoded observations for one receiver epoch.

    Raises:
        ValueError: If the payload length is inconsistent with the compressed
            observation count.
    """

    binary_time = _binary_time(record)
    if binary_time is None:
        return []
    week, tow = binary_time
    payload = _binary_payload(record.raw, "OBSVMCMPB")
    nobs = struct.unpack_from("<I", payload, 0)[0]
    expected = 4 + nobs * OBSVMCMPB_RECORD_BYTES
    if len(payload) < expected:
        raise ValueError(f"OBSVMCMPB payload has {len(payload)} bytes, expected {expected} for {nobs} observations")

    observations: list[Observation] = []
    for index in range(nobs):
        offset = 4 + index * OBSVMCMPB_RECORD_BYTES
        packed = int.from_bytes(payload[offset : offset + OBSVMCMPB_RECORD_BYTES], "little", signed=False)
        tracking = packed & 0xFFFFFFFF
        doppler_raw = _signed_bits((packed >> 32) & ((1 << 28) - 1), 28)
        pseudorange_raw = (packed >> 60) & ((1 << 36) - 1)
        adr_raw = _signed_bits((packed >> 96) & 0xFFFFFFFF, 32)
        raw_prn = (packed >> 136) & 0xFF
        lock_time_raw = (packed >> 144) & ((1 << 21) - 1)
        cn0_raw = (packed >> 165) & 0x1F
        glo_frequency = (packed >> 170) & 0x3F

        system = _tracking_system(tracking)
        signal, code, signal_known = _tracking_signal(system, tracking)
        phase_valid = bool(tracking & 0x00000400)
        pseudorange_valid = bool(tracking & 0x00001000)
        rinex_sv = _rinex_sv(system, raw_prn)
        prefix = RINEX_SYSTEM_PREFIX[system]
        observations.append(
            Observation(
                gps_week=week,
                tow=tow,
                sat_system=system,
                sv=rinex_sv,
                rinex_sat=f"{prefix}{rinex_sv:02d}" if prefix != "U" else f"U{rinex_sv:02d}",
                signal_name=signal if system != "GLONASS" else f"{signal} FCN={glo_frequency}",
                rinex_code=code,
                band=code[0],
                pseudorange_m=pseudorange_raw / OBSVMCMPB_PSEUDORANGE_SCALE if pseudorange_valid else None,
                carrier_phase_cycles=adr_raw / OBSVMCMPB_ADR_SCALE if phase_valid else None,
                doppler_hz=doppler_raw / OBSVMCMPB_DOPPLER_SCALE if phase_valid else None,
                cn0_dbhz=20.0 + cn0_raw,
                lock_time_s=lock_time_raw / OBSVMCMPB_LOCK_TIME_SCALE,
                half_cycle=None,
                lli=0,
                raw_tracking_status=tracking if signal_known else tracking,
            )
        )
    return observations


def _rinex_sv(system: SystemName, prn: int) -> int:
    if system == "GLONASS" and 38 <= prn <= 61:
        return prn - 37
    if system == "SBAS" and prn >= 100:
        return prn - 100
    if system == "QZSS" and prn >= 193:
        return prn - 192
    return prn


def _rinex_code(system: SystemName, signal: str) -> str:
    signal_key = signal.upper().replace("/", "").replace("-", "")
    return DEFAULT_SIGNAL_CODES.get((system, signal_key), "1C")


def _observation_from_tokens(tokens: list[str]) -> Observation | None:
    # Conservative CSV-like OBSVMA subset:
    # OBSVMA,week,tow,system,sv,signal,pseudorange,phase,doppler,cn0,lock,tracking
    if len(tokens) < 10:
        return None
    week = _int(tokens[1])
    tow = _float(tokens[2])
    system = _system(tokens[3])
    sv = _int(tokens[4])
    if week is None or tow is None or sv is None:
        return None
    signal = tokens[5].strip() or "L1"
    code = _rinex_code(system, signal)
    prefix = RINEX_SYSTEM_PREFIX[system]
    rinex_sat = f"{prefix}{sv:02d}" if prefix != "U" else f"U{sv:02d}"
    tracking = _int(tokens[11]) if len(tokens) > 11 else 0
    return Observation(
        gps_week=week,
        tow=tow,
        sat_system=system,
        sv=sv,
        rinex_sat=rinex_sat,
        signal_name=signal,
        rinex_code=code,
        band=code[0],
        pseudorange_m=_float(tokens[6]),
        carrier_phase_cycles=_float(tokens[7]),
        doppler_hz=_float(tokens[8]),
        cn0_dbhz=_float(tokens[9]),
        lock_time_s=_float(tokens[10]) if len(tokens) > 10 else None,
        half_cycle=None,
        lli=0,
        raw_tracking_status=tracking or 0,
    )


def _is_real_obsvma_payload(header_tokens: list[str], payload_tokens: list[str]) -> bool:
    """Return true when an OBSVMA record has the UM980 grouped payload shape."""

    return len(header_tokens) >= 10 and bool(payload_tokens) and _int(payload_tokens[0]) is not None


def _obsvma_payload_observations(header_tokens: list[str], payload_tokens: list[str]) -> list[Observation]:
    if len(header_tokens) < 6:
        return []
    time_status = header_tokens[3].strip().upper() if len(header_tokens) > 3 else ""
    if time_status != "FINE":
        return []
    week = _int(header_tokens[4])
    tow_ms = _float(header_tokens[5])
    if week is None or tow_ms is None:
        return []
    tow = tow_ms / 1000.0
    tokens = [token for token in payload_tokens if token != ""]
    if not tokens:
        return []
    declared_count = _int(tokens[0])
    if declared_count is not None:
        tokens = tokens[1:]

    observations: list[Observation] = []
    group_size = 11
    for offset in range(0, len(tokens) - group_size + 1, group_size):
        group = tokens[offset : offset + group_size]
        sv = _int(group[1])
        if sv is None:
            continue
        tracking = _int_auto(group[10]) or 0
        system = _tracking_system(tracking) if tracking else "Unknown"
        signal, code, signal_known = _tracking_signal(system, tracking) if tracking else ("TRACK_00000000", "1C", False)
        prefix = RINEX_SYSTEM_PREFIX[system]
        cn0_raw = _float(group[7])
        rinex_sv = _rinex_sv(system, sv)
        phase_valid = bool(tracking & 0x00000400)
        pseudorange_valid = bool(tracking & 0x00001000)
        observations.append(
            Observation(
                gps_week=week,
                tow=tow,
                sat_system=system,
                sv=rinex_sv,
                rinex_sat=f"{prefix}{rinex_sv:02d}" if prefix != "U" else f"U{rinex_sv:02d}",
                signal_name=signal,
                rinex_code=code,
                band=code[0],
                pseudorange_m=_float(group[2]) if pseudorange_valid else None,
                carrier_phase_cycles=_float(group[3]) if phase_valid else None,
                doppler_hz=_float(group[6]),
                cn0_dbhz=cn0_raw / 100.0 if cn0_raw is not None and cn0_raw > 100 else cn0_raw,
                lock_time_s=_float(group[9]),
                half_cycle=None,
                lli=0,
                raw_tracking_status=tracking if signal_known else tracking,
            )
        )
    if declared_count is not None and observations and abs(declared_count - len(observations)) > 5:
        # The payload count differs across firmware variants; do not reject the
        # decoded observations, but keep parsing conservative by group size.
        return observations
    return observations


def decode_observations(records: list[StreamRecord], *, progress: bool = False) -> ObservationExtraction:
    """Decode supported UM980 raw observation records.

    Args:
        records: Parsed mixed-stream records.
        progress: Emit coarse record-progress messages through logging.

    Returns:
        Decoded observations, metrics, unsupported record counts, and warnings.
    """

    observations: list[Observation] = []
    unsupported: dict[str, int] = defaultdict(int)
    warnings: list[str] = []
    progress_step = 100_000
    for index, record in enumerate(records, start=1):
        if progress and index % progress_step == 0:
            logging.info("scanned %d/%d records for raw observations", index, len(records))
        msg_type = (record.msg_type or "").upper()
        if record.kind == "unicore_ascii" and msg_type == "OBSVMA" and record.text:
            body = record.text[1:].split("*", 1)[0]
            header, _, payload = body.partition(";")
            header_tokens = [token.strip() for token in header.split(",")]
            payload_tokens = [token.strip() for token in payload.replace("|", ",").split(",")]
            parsed_payload = _obsvma_payload_observations(header_tokens, payload_tokens)
            if parsed_payload:
                observations.extend(parsed_payload)
                continue
            if _is_real_obsvma_payload(header_tokens, payload_tokens):
                unsupported[f"OBSVMA_TIME_{(header_tokens[3] or 'UNKNOWN').upper()}"] += 1
                continue
            payload_text = payload or header
            for chunk in payload_text.replace("\r", "").split("|"):
                tokens = [token.strip() for token in chunk.split(",")]
                if tokens and tokens[0].upper() != "OBSVMA":
                    tokens.insert(0, "OBSVMA")
                obs = _observation_from_tokens(tokens)
                if obs is not None:
                    observations.append(obs)
                else:
                    unsupported["OBSVMA"] += 1
        elif record.kind == "unicore_binary" and msg_type == "OBSVMB":
            try:
                parsed_obsvmb = _obsvmb_observations(record)
            except ValueError:
                unsupported["OBSVMB_MALFORMED"] += 1
            else:
                if parsed_obsvmb:
                    observations.extend(parsed_obsvmb)
                else:
                    unsupported["OBSVMB_TIME_UNKNOWN"] += 1
        elif record.kind == "unicore_binary" and msg_type == "OBSVMCMPB":
            try:
                parsed_obsvmcmpb = _obsvmcmpb_observations(record)
            except ValueError:
                unsupported["OBSVMCMPB_MALFORMED"] += 1
            else:
                if parsed_obsvmcmpb:
                    observations.extend(parsed_obsvmcmpb)
                else:
                    unsupported["OBSVMCMPB_TIME_UNKNOWN"] += 1
        elif record.kind == "unicore_binary" and msg_type not in BINARY_EPHEMERIS_TYPES:
            unsupported[record.msg_type or "unicore_binary"] += 1

    if unsupported:
        warnings.append(
            "some raw observation records were not decoded: "
            + ", ".join(f"{name}={count}" for name, count in sorted(unsupported.items()))
        )
    if observations and any(obs.signal_name.startswith("TRACK_") for obs in observations):
        warnings.append(
            "some UM980 tracking-status signal types are not mapped to RINEX yet; affected "
            "observations use a conservative fallback RINEX code. Check analysis JSON signal "
            "counts before production multi-band RTK."
        )
    if observations and any(obs.sat_system == "Unknown" for obs in observations):
        warnings.append("some observations have unknown constellation IDs and may be ignored by RTKLIB")
    if not observations:
        warnings.append("no raw observations were decoded")

    return ObservationExtraction(observations, dict(unsupported), observation_metrics(observations), warnings)


def observation_metrics(observations: list[Observation]) -> dict[str, object]:
    """Compute aggregate metrics for decoded observations.

    Args:
        observations: Decoded observation list.

    Returns:
        JSON-friendly metrics for epochs, rates, constellations, and signals.
    """

    by_epoch: dict[tuple[int, float], list[Observation]] = defaultdict(list)
    constellations: dict[str, int] = defaultdict(int)
    bands: dict[str, int] = defaultdict(int)
    signals: dict[str, int] = defaultdict(int)
    codes: dict[str, int] = defaultdict(int)
    for obs in observations:
        by_epoch[(obs.gps_week, obs.tow)].append(obs)
        constellations[obs.sat_system] += 1
        bands[obs.band] += 1
        signals[obs.signal_name] += 1
        codes[obs.rinex_code] += 1
    epochs = sorted(by_epoch)
    intervals = [right[1] - left[1] for left, right in zip(epochs, epochs[1:]) if right[1] > left[1]]
    obs_counts = [len(v) for v in by_epoch.values()]
    metrics: dict[str, object] = {
        "epochs": len(epochs),
        "observations": len(observations),
        "constellations": dict(constellations),
        "bands": dict(bands),
        "signals": dict(signals),
        "rinex_observation_codes": dict(codes),
    }
    if intervals:
        hz = [1.0 / interval for interval in intervals if interval > 0]
        med = median(intervals)
        metrics.update(
            {
                "mean_hz": mean(hz),
                "median_hz": median(hz),
                "min_hz": min(hz),
                "max_hz": max(hz),
                "interval_median_s": med,
                "interval_max_s": max(intervals),
                "missing_est": sum(max(0, round(interval / med) - 1) for interval in intervals),
                "large_gaps": sum(1 for interval in intervals if interval > med * 3),
            }
        )
    if obs_counts:
        metrics.update(
            {
                "epoch_observations_min": min(obs_counts),
                "epoch_observations_mean": mean(obs_counts),
                "epoch_observations_median": median(obs_counts),
                "epoch_observations_max": max(obs_counts),
            }
        )
    return metrics


def write_observations_csv(path: Path, observations: list[Observation]) -> None:
    """Write decoded observations as CSV.

    Args:
        path: Destination CSV path.
        observations: Decoded observations to write.
    """

    fields = [
        "epoch_index",
        "gps_week",
        "tow",
        "datetime_utc",
        "rinex_sat",
        "system",
        "sv",
        "signal_name",
        "rinex_code",
        "band",
        "pseudorange_m",
        "carrier_phase_cycles",
        "doppler_hz",
        "cn0_dbhz",
        "lock_time_s",
        "lli",
        "tracking_status",
    ]
    epochs = {key: idx for idx, key in enumerate(sorted({(o.gps_week, o.tow) for o in observations}))}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for obs in observations:
            row = asdict(obs)
            writer.writerow(
                {
                    "epoch_index": epochs[(obs.gps_week, obs.tow)],
                    "gps_week": obs.gps_week,
                    "tow": obs.tow,
                    "datetime_utc": gps_week_tow_to_datetime(obs.gps_week, obs.tow).isoformat(),
                    "rinex_sat": obs.rinex_sat,
                    "system": obs.sat_system,
                    "sv": obs.sv,
                    "signal_name": obs.signal_name,
                    "rinex_code": obs.rinex_code,
                    "band": obs.band,
                    "pseudorange_m": row["pseudorange_m"],
                    "carrier_phase_cycles": row["carrier_phase_cycles"],
                    "doppler_hz": row["doppler_hz"],
                    "cn0_dbhz": row["cn0_dbhz"],
                    "lock_time_s": row["lock_time_s"],
                    "lli": row["lli"],
                    "tracking_status": row["raw_tracking_status"],
                }
            )
