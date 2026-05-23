"""RINEX navigation extraction from UM980 ASCII and binary ephemeris records."""

from __future__ import annotations

import math
import struct
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stream import StreamRecord

EPHEMERIS_TYPES = ("GPSEPHA", "GLOEPHA", "GALEPHA", "BDSEPHA", "BD3EPHA", "QZSSEPHA")
BINARY_EPHEMERIS_TYPES = (
    "GPSEPHB",
    "GLOEPHB",
    "GALEPHB",
    "BDSEPHB",
    "BD3EPHB",
    "QZSSEPHB",
    "IRNSSEPHB",
)
SBAS_MESSAGE_TYPES = ("SBSMSGA", "SBSMSG", "SBASMSGA", "RAWSBASA")
GPS_EPOCH = datetime(1980, 1, 6)
BDT_EPOCH = datetime(2006, 1, 1)
GPS_UTC_OFFSET_S = 18
BDS_WEEK_OFFSET = 1356
BDS3_GEO_IGSO_AREF_M = 42162200.0
BDS3_MEO_AREF_M = 27906100.0
URA_EPH = (2.4, 3.4, 4.85, 6.85, 9.65, 13.65, 24.0, 48.0, 96.0, 192.0, 384.0, 768.0, 1536.0, 3072.0, 6144.0)
URA_NOMINAL = (2.0, 2.8, 4.0, 5.7, 8.0, 11.3, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0, 4096.0, 8192.0)


@dataclass
class NavExtractionReport:
    """Summary of rover navigation extraction.

    Attributes:
        found: Count of matching navigation records by message family.
        converted: Count of records converted into RTKLIB-readable output.
        warnings: User-facing conversion warnings.
        written: Mapping from output kind to written file path.
    """

    found: dict[str, int]
    converted: dict[str, int]
    warnings: list[str]
    written: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable report."""

        return {
            "found": self.found,
            "converted": self.converted,
            "warnings": self.warnings,
            "written": self.written,
        }


@dataclass(frozen=True)
class BroadcastEphemeris:
    """Broadcast ephemeris values for GPS-like RINEX NAV records.

    Attributes:
        system: RINEX satellite system prefix, for example `G` or `E`.
        prn: Satellite PRN number within the system.
        toc_week: GPS week used for clock epoch.
        toc_s: Time of clock in seconds of week.
        toe_s: Time of ephemeris in seconds of week.
        sqrt_a: Square root of the semi-major axis.
        deln: Mean motion difference.
        m0: Mean anomaly at reference time.
        ecc: Orbital eccentricity.
        omega: Argument of perigee.
        cuc: Cosine harmonic correction to argument of latitude.
        cus: Sine harmonic correction to argument of latitude.
        crc: Cosine harmonic correction to orbit radius.
        crs: Sine harmonic correction to orbit radius.
        cic: Cosine harmonic correction to inclination.
        cis: Sine harmonic correction to inclination.
        i0: Inclination angle at reference time.
        idot: Rate of inclination angle.
        omg0: Longitude of ascending node.
        omgd: Rate of right ascension.
        f0: Satellite clock bias.
        f1: Satellite clock drift.
        f2: Satellite clock drift rate.
        iode: Issue of data, ephemeris.
        iodc: Issue of data, clock.
        code: RINEX data-source/code flags.
        svh: Satellite health.
        sva: Satellite accuracy value.
        tgd0: First group delay/bias value.
        tgd1: Second group delay/bias value where the constellation uses one.
        flag: Fit interval or constellation-specific flag.
        ttr_week: Optional transmission GPS week.
        ttr_s: Optional transmission time in seconds of week.
        source_message: UM980 message family used to create this record.
    """

    system: str
    prn: int
    toc_week: int
    toc_s: float
    toe_s: float
    sqrt_a: float
    deln: float
    m0: float
    ecc: float
    omega: float
    cuc: float
    cus: float
    crc: float
    crs: float
    cic: float
    cis: float
    i0: float
    idot: float
    omg0: float
    omgd: float
    f0: float
    f1: float
    f2: float
    iode: int
    iodc: int
    code: int
    svh: int
    sva: float
    tgd0: float
    tgd1: float = 0.0
    flag: int = 0
    ttr_week: int | None = None
    ttr_s: float | None = None
    source_message: str = ""


@dataclass(frozen=True)
class GlonassEphemeris:
    """GLONASS ephemeris values for RINEX GNAV records.

    Attributes:
        prn: GLONASS slot number.
        frq: GLONASS frequency channel.
        toe_week: GPS week used for the ephemeris epoch.
        toe_s: Time of ephemeris in GPS seconds of week.
        tof_s: Message frame time in GPS seconds of week.
        pos: ECEF position tuple in meters.
        vel: ECEF velocity tuple in meters per second.
        acc: ECEF acceleration tuple in meters per second squared.
        taun: GLONASS clock bias.
        dtaun: GLONASS L1/L2 delay difference.
        gamn: Relative frequency bias.
        svh: Satellite health flag.
        age: Age of operation information.
        source_message: UM980 message family used to create this record.
    """

    prn: int
    frq: int
    toe_week: int
    toe_s: float
    tof_s: float
    pos: tuple[float, float, float]
    vel: tuple[float, float, float]
    acc: tuple[float, float, float]
    taun: float
    dtaun: float
    gamn: float
    svh: int
    age: int
    source_message: str = ""


def extract_rover_nav(records: list[StreamRecord], output_path: Path | None = None) -> NavExtractionReport:
    """Extract rover ephemeris records into RTKLIB-readable files.

    Args:
        records: Parsed mixed-stream records from a UM980 rover log.
        output_path: Base `.nav` path. When provided, sibling `.gnav`, `.lnav`,
            `.cnav`, `.inav`, and `.sbs` paths are derived from it.

    Returns:
        Extraction report with counts, warnings, and written file paths.
    """

    counts: Counter[str] = Counter()
    for record in records:
        msg_type = (record.msg_type or "").upper()
        if record.kind == "unicore_ascii" and msg_type in {*EPHEMERIS_TYPES, *SBAS_MESSAGE_TYPES}:
            counts[msg_type] += 1
        elif record.kind == "unicore_binary" and msg_type in BINARY_EPHEMERIS_TYPES:
            counts[msg_type] += 1
    converted = {name: 0 for name in (*EPHEMERIS_TYPES, *BINARY_EPHEMERIS_TYPES, "SBSMSG")}
    warnings: list[str] = []
    written: dict[str, str] = {}
    paths = _nav_output_paths(output_path) if output_path else {}

    gps: list[BroadcastEphemeris] = []
    qzss: list[BroadcastEphemeris] = []
    glo: list[GlonassEphemeris] = []
    gal: list[BroadcastEphemeris] = []
    bds: list[BroadcastEphemeris] = []
    bd3: list[BroadcastEphemeris] = []
    irn: list[BroadcastEphemeris] = []
    sbs_messages: list[str] = []
    parse_errors: list[str] = []

    for record in records:
        msg_type = (record.msg_type or "").upper()
        try:
            if record.kind == "unicore_ascii" and record.text:
                header, payload = _ascii_fields(record.text)
                if msg_type == "GPSEPHA":
                    gps.append(_parse_gpsepha(payload))
                elif msg_type == "GLOEPHA":
                    glo.append(_parse_gloepha(payload))
                elif msg_type == "GALEPHA":
                    gal.append(_parse_galepha(header, payload))
                elif msg_type == "BDSEPHA":
                    bds.append(_parse_bdsepha(payload))
                elif msg_type == "QZSSEPHA":
                    qzss.append(_parse_qzssepha(payload))
                elif msg_type in SBAS_MESSAGE_TYPES:
                    rendered = _parse_sbas_message(payload)
                    if rendered:
                        sbs_messages.append(rendered)
            elif record.kind == "unicore_binary":
                if msg_type == "GPSEPHB":
                    gps.append(_parse_gpsephb(record.raw))
                elif msg_type == "QZSSEPHB":
                    qzss.append(_parse_qzssephb(record.raw))
                elif msg_type == "GLOEPHB":
                    glo.append(_parse_gloephb(record.raw))
                elif msg_type == "GALEPHB":
                    gal.append(_parse_galephb(record.raw))
                elif msg_type == "BDSEPHB":
                    bds.append(_parse_bdsephb(record.raw))
                elif msg_type == "BD3EPHB":
                    bd3.append(_parse_bd3ephb(record.raw))
                elif msg_type == "IRNSSEPHB":
                    irn.append(_parse_irnssephb(record.raw))
        except (IndexError, TypeError, ValueError, struct.error) as exc:
            parse_errors.append(f"{msg_type} at offset {record.offset}: {exc}")

    gps = _unique_broadcast(gps)
    qzss = _unique_broadcast(qzss)
    glo = _unique_glonass(glo)
    gal = _unique_broadcast(gal)
    bds = _unique_broadcast(bds)
    bd3 = _unique_broadcast(bd3)
    irn = _unique_broadcast(irn)
    sbs_messages = list(dict.fromkeys(sbs_messages))

    converted["GPSEPHA"] = _count_source(gps, "GPSEPHA")
    converted["GPSEPHB"] = _count_source(gps, "GPSEPHB")
    converted["QZSSEPHA"] = _count_source(qzss, "QZSSEPHA")
    converted["QZSSEPHB"] = _count_source(qzss, "QZSSEPHB")
    converted["GLOEPHA"] = _count_source(glo, "GLOEPHA")
    converted["GLOEPHB"] = _count_source(glo, "GLOEPHB")
    converted["GALEPHA"] = _count_source(gal, "GALEPHA")
    converted["GALEPHB"] = _count_source(gal, "GALEPHB")
    converted["BDSEPHA"] = _count_source(bds, "BDSEPHA")
    converted["BDSEPHB"] = _count_source(bds, "BDSEPHB")
    converted["BD3EPHB"] = _count_source(bd3, "BD3EPHB")
    converted["IRNSSEPHB"] = _count_source(irn, "IRNSSEPHB")
    converted["SBSMSG"] = len(sbs_messages)

    nav_records = _unique_broadcast([*gps, *qzss])
    bds_records = _unique_broadcast([*bds, *bd3])
    if nav_records and "nav" in paths:
        _write_broadcast_nav(paths["nav"], _nav_type_for_broadcast(nav_records), nav_records)
        written["nav"] = str(paths["nav"])
    if glo and "gnav" in paths:
        _write_glonass_nav(paths["gnav"], glo)
        written["gnav"] = str(paths["gnav"])
    if gal and "lnav" in paths:
        _write_broadcast_nav(paths["lnav"], "E: Galileo", gal)
        written["lnav"] = str(paths["lnav"])
    if bds_records and "cnav" in paths:
        _write_broadcast_nav(paths["cnav"], "C: BeiDou", bds_records)
        written["cnav"] = str(paths["cnav"])
    if irn and "inav" in paths:
        _write_broadcast_nav(paths["inav"], "I: IRNSS", irn)
        written["inav"] = str(paths["inav"])
    if sbs_messages and "sbs" in paths:
        paths["sbs"].write_text("".join(sbs_messages), encoding="ascii")
        written["sbs"] = str(paths["sbs"])

    for key, path in paths.items():
        if key not in written and path.exists():
            path.unlink()

    if counts.get("GPSEPHA", 0) and converted["GPSEPHA"] == 0:
        warnings.append("GPSEPHA records found, but no valid GPS RINEX NAV records were converted.")
    if counts.get("GLOEPHA", 0) and converted["GLOEPHA"] == 0:
        warnings.append("GLOEPHA records found, but no valid GLONASS RINEX GNAV records were converted.")
    if counts.get("GALEPHA", 0) and converted["GALEPHA"] == 0:
        warnings.append("GALEPHA records found, but no valid Galileo RINEX LNAV records were converted.")
    if counts.get("BDSEPHA", 0) and converted["BDSEPHA"] == 0:
        warnings.append("BDSEPHA records found, but no valid BeiDou RINEX CNAV records were converted.")
    if counts.get("BD3EPHB", 0) and 0 < converted["BD3EPHB"] < counts["BD3EPHB"]:
        warnings.append(
            f"BD3EPHB contained {counts['BD3EPHB']} records but {converted['BD3EPHB']} "
            "RTKLIB-compatible BDS-3 ephemerides were written; frequency variants for the same "
            "satellite and epoch were collapsed because RTKLIB RINEX NAV keeps one broadcast "
            "ephemeris per satellite/epoch."
        )
    for name in ("BD3EPHA",):
        if counts.get(name, 0):
            warnings.append(f"{name} records found; conversion not yet implemented")
    for name in BINARY_EPHEMERIS_TYPES:
        if counts.get(name, 0) and converted[name] == 0:
            warnings.append(f"{name} binary records found, but no valid RINEX NAV records were converted.")
    sbs_found = sum(counts.get(name, 0) for name in SBAS_MESSAGE_TYPES)
    if sbs_found and converted["SBSMSG"] == 0:
        warnings.append("SBAS message records found, but no valid RTKLIB .sbs messages were converted.")
    if not sbs_found:
        warnings.append("SBAS message records missing; no rover .sbs file was written.")
    equivalent_families = {
        "GPSEPHA": ("GPSEPHA", "GPSEPHB"),
        "GLOEPHA": ("GLOEPHA", "GLOEPHB"),
        "GALEPHA": ("GALEPHA", "GALEPHB"),
        "BDSEPHA": ("BDSEPHA", "BDSEPHB"),
        "BD3EPHA": ("BD3EPHA", "BD3EPHB"),
        "QZSSEPHA": ("QZSSEPHA", "QZSSEPHB"),
    }
    for name, equivalents in equivalent_families.items():
        if not any(counts.get(equivalent, 0) for equivalent in equivalents):
            warnings.append(f"{name} or equivalent binary records missing")
    for error in parse_errors[:10]:
        warnings.append(f"navigation conversion skipped malformed {error}")
    if len(parse_errors) > 10:
        warnings.append(f"{len(parse_errors) - 10} additional malformed navigation records were skipped")

    return NavExtractionReport(
        found={
            **{name: counts.get(name, 0) for name in EPHEMERIS_TYPES},
            **{name: counts.get(name, 0) for name in BINARY_EPHEMERIS_TYPES},
            "SBSMSG": sbs_found,
        },
        converted=converted,
        warnings=warnings,
        written=written,
    )


def rover_nav_files(output_path: Path) -> list[Path]:
    """Return the generated rover NAV/GNAV/LNAV/SBS paths that currently exist.

    Args:
        output_path: Base `.nav` path used for rover navigation extraction.

    Returns:
        Existing non-empty generated navigation sidecar paths.
    """

    return [path for path in _nav_output_paths(output_path).values() if path.exists() and path.stat().st_size > 0]


def _nav_output_paths(output_path: Path) -> dict[str, Path]:
    stem = output_path.name
    suffix = ".rover-gps.nav"
    if stem.endswith(suffix):
        base = stem[: -len(suffix)]
        return {
            "nav": output_path,
            "gnav": output_path.with_name(f"{base}.rover-glo.gnav"),
            "lnav": output_path.with_name(f"{base}.rover-gal.lnav"),
            "cnav": output_path.with_name(f"{base}.rover-bds.cnav"),
            "sbs": output_path.with_name(f"{base}.rover-sbas.sbs"),
            "inav": output_path.with_name(f"{base}.rover-irn.inav"),
        }
    return {
        "nav": output_path,
        "gnav": output_path.with_suffix(".gnav"),
        "lnav": output_path.with_suffix(".lnav"),
        "cnav": output_path.with_suffix(".cnav"),
        "sbs": output_path.with_suffix(".sbs"),
        "inav": output_path.with_suffix(".inav"),
    }


def _ascii_fields(text: str) -> tuple[list[str], list[str]]:
    body = text.split("*", 1)[0]
    if ";" not in body:
        raise ValueError("record has no payload separator")
    header, payload = body.split(";", 1)
    return (
        [field.strip() for field in header.lstrip("#").split(",")],
        [field.strip() for field in payload.split(",")],
    )


def _count_source(records: list[BroadcastEphemeris] | list[GlonassEphemeris], source_message: str) -> int:
    """Return how many decoded records came from a UM980 message family."""

    return sum(1 for record in records if record.source_message == source_message)


def _nav_type_for_broadcast(records: list[BroadcastEphemeris]) -> str:
    """Return the RINEX header navigation type for broadcast records."""

    systems = {record.system for record in records}
    if systems == {"G"}:
        return "G: GPS"
    if systems == {"J"}:
        return "J: QZSS"
    if len(systems) > 1:
        return "M: Mixed"
    return f"{next(iter(systems), 'M')}: GNSS"


def _payload(raw: bytes, min_len: int, msg_type: str) -> bytes:
    if len(raw) < 24 + min_len + 4:
        raise ValueError(f"{msg_type} payload has {max(0, len(raw) - 28)} bytes, expected at least {min_len}")
    declared = int.from_bytes(raw[6:8], "little", signed=False)
    if declared < min_len:
        raise ValueError(f"{msg_type} declared payload has {declared} bytes, expected at least {min_len}")
    return raw[24 : 24 + declared]


def _header_week(raw: bytes) -> int:
    return struct.unpack_from("<H", raw, 10)[0]


def _header_tow_s(raw: bytes) -> float:
    return struct.unpack_from("<I", raw, 12)[0] / 1000.0


def _u1(payload: bytes, offset: int) -> int:
    return payload[offset]


def _u2(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<H", payload, offset)[0]


def _u4(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<I", payload, offset)[0]


def _i4(payload: bytes, offset: int) -> int:
    return struct.unpack_from("<i", payload, offset)[0]


def _r8(payload: bytes, offset: int) -> float:
    return struct.unpack_from("<d", payload, offset)[0]


def _parse_gpsepha(fields: list[str]) -> BroadcastEphemeris:
    _require_fields(fields, 32, "GPSEPHA")
    week = _as_int(fields[5])
    return BroadcastEphemeris(
        system="G",
        prn=_as_int(fields[0]),
        toc_week=week,
        toc_s=_as_float(fields[24]),
        toe_s=_as_float(fields[7]),
        sqrt_a=math.sqrt(_as_float(fields[8])),
        deln=_as_float(fields[9]),
        m0=_as_float(fields[10]),
        ecc=_as_float(fields[11]),
        omega=_as_float(fields[12]),
        cuc=_as_float(fields[13]),
        cus=_as_float(fields[14]),
        crc=_as_float(fields[15]),
        crs=_as_float(fields[16]),
        cic=_as_float(fields[17]),
        cis=_as_float(fields[18]),
        i0=_as_float(fields[19]),
        idot=_as_float(fields[20]),
        omg0=_as_float(fields[21]),
        omgd=_as_float(fields[22]),
        f0=_as_float(fields[26]),
        f1=_as_float(fields[27]),
        f2=_as_float(fields[28]),
        iode=_as_int(fields[3]),
        iodc=_as_int(fields[3]),
        code=0,
        svh=_as_int(fields[2]),
        sva=_ura_value(_as_float(fields[31])),
        tgd0=_as_float(fields[25]),
        ttr_week=week,
        ttr_s=_as_float(fields[1]),
        source_message="GPSEPHA",
    )


def _parse_gpsephb(raw: bytes) -> BroadcastEphemeris:
    payload = _payload(raw, 224, "GPSEPHB")
    prn = _u4(payload, 0)
    if not 1 <= prn <= 32:
        raise ValueError(f"GPSEPHB PRN {prn} outside GPS range 1..32")
    week = _header_week(raw)
    return BroadcastEphemeris(
        system="G",
        prn=prn,
        toc_week=week,
        toc_s=_r8(payload, 164),
        toe_s=_r8(payload, 32),
        sqrt_a=math.sqrt(_r8(payload, 40)),
        deln=_r8(payload, 48),
        m0=_r8(payload, 56),
        ecc=_r8(payload, 64),
        omega=_r8(payload, 72),
        cuc=_r8(payload, 80),
        cus=_r8(payload, 88),
        crc=_r8(payload, 96),
        crs=_r8(payload, 104),
        cic=_r8(payload, 112),
        cis=_r8(payload, 120),
        i0=_r8(payload, 128),
        idot=_r8(payload, 136),
        omg0=_r8(payload, 144),
        omgd=_r8(payload, 152),
        f0=_r8(payload, 180),
        f1=_r8(payload, 188),
        f2=_r8(payload, 196),
        iode=_u4(payload, 16),
        iodc=_u4(payload, 16),
        code=0,
        svh=_u4(payload, 12) & 0x3F,
        sva=_ura_value(_r8(payload, 216)),
        tgd0=_r8(payload, 172),
        ttr_week=week,
        ttr_s=_header_tow_s(raw),
        source_message="GPSEPHB",
    )


def _parse_gloepha(fields: list[str]) -> GlonassEphemeris:
    _require_fields(fields, 28, "GLOEPHA")
    prn = _as_int(fields[0]) - 37
    week = _as_int(fields[4])
    tow = round(_as_float(fields[5]) / 1000.0)
    toff = _as_float(fields[6])
    tof = _as_float(fields[24]) - toff + math.floor(tow / 86400.0) * 86400.0
    if tof < tow - 43200.0:
        tof += 86400.0
    elif tof > tow + 43200.0:
        tof -= 86400.0
    return GlonassEphemeris(
        prn=prn,
        frq=_as_int(fields[1]) - 7,
        toe_week=week,
        toe_s=float(tow),
        tof_s=tof,
        pos=(_as_float(fields[12]), _as_float(fields[13]), _as_float(fields[14])),
        vel=(_as_float(fields[15]), _as_float(fields[16]), _as_float(fields[17])),
        acc=(_as_float(fields[18]), _as_float(fields[19]), _as_float(fields[20])),
        taun=_as_float(fields[21]),
        dtaun=_as_float(fields[22]),
        gamn=_as_float(fields[23]),
        svh=0 if _as_int(fields[11]) < 4 else 1,
        age=_as_int(fields[27]),
        source_message="GLOEPHA",
    )


def _parse_gloephb(raw: bytes) -> GlonassEphemeris:
    payload = _payload(raw, 144, "GLOEPHB")
    prn = _u2(payload, 0) - 37
    week = _u2(payload, 6)
    tow = round(_u4(payload, 8) / 1000.0)
    toff = _u4(payload, 12)
    tof = _u4(payload, 124) - toff + math.floor(tow / 86400.0) * 86400.0
    if tof < tow - 43200.0:
        tof += 86400.0
    elif tof > tow + 43200.0:
        tof -= 86400.0
    return GlonassEphemeris(
        prn=prn,
        frq=_u2(payload, 2) - 7,
        toe_week=week,
        toe_s=float(tow),
        tof_s=float(tof),
        pos=(_r8(payload, 28), _r8(payload, 36), _r8(payload, 44)),
        vel=(_r8(payload, 52), _r8(payload, 60), _r8(payload, 68)),
        acc=(_r8(payload, 76), _r8(payload, 84), _r8(payload, 92)),
        taun=_r8(payload, 100),
        dtaun=_r8(payload, 108),
        gamn=_r8(payload, 116),
        svh=0 if _u4(payload, 24) < 4 else 1,
        age=_u4(payload, 136),
        source_message="GLOEPHB",
    )


def _parse_galepha(header: list[str], fields: list[str]) -> BroadcastEphemeris:
    _require_fields(header, 6, "GALEPHA header")
    _require_fields(fields, 38, "GALEPHA")
    rcv_fnav = _as_int(fields[1]) & 1
    set_fnav = bool(rcv_fnav)
    svh_e1b = _as_int(fields[3]) & 3
    svh_e5a = _as_int(fields[4]) & 3
    svh_e5b = _as_int(fields[5]) & 3
    dvs_e1b = _as_int(fields[6]) & 1
    dvs_e5a = _as_int(fields[7]) & 1
    dvs_e5b = _as_int(fields[8]) & 1
    week = _as_int(header[4])
    toc_index = 28 if set_fnav else 32
    af_index = 29 if set_fnav else 33
    return BroadcastEphemeris(
        system="E",
        prn=_as_int(fields[0]),
        toc_week=week,
        toc_s=_as_float(fields[toc_index]),
        toe_s=_as_float(fields[12]),
        sqrt_a=_as_float(fields[13]),
        deln=_as_float(fields[14]),
        m0=_as_float(fields[15]),
        ecc=_as_float(fields[16]),
        omega=_as_float(fields[17]),
        cuc=_as_float(fields[18]),
        cus=_as_float(fields[19]),
        crc=_as_float(fields[20]),
        crs=_as_float(fields[21]),
        cic=_as_float(fields[22]),
        cis=_as_float(fields[23]),
        i0=_as_float(fields[24]),
        idot=_as_float(fields[25]),
        omg0=_as_float(fields[26]),
        omgd=_as_float(fields[27]),
        f0=_as_float(fields[af_index]),
        f1=_as_float(fields[af_index + 1]),
        f2=_as_float(fields[af_index + 2]),
        iode=_as_int(fields[11]),
        iodc=_as_int(fields[11]),
        code=(1 << 1) + (1 << 8) if set_fnav else (1 << 0) + (1 << 2) + (1 << 9),
        svh=(svh_e5b << 7) | (dvs_e5b << 6) | (svh_e5a << 4) | (dvs_e5a << 3) | (svh_e1b << 1) | dvs_e1b,
        sva=_galileo_sisa_value(_as_float(fields[9])),
        tgd0=_as_float(fields[36]),
        tgd1=_as_float(fields[37]),
        ttr_week=week,
        ttr_s=_as_float(fields[12]),
        source_message="GALEPHA",
    )


def _parse_galephb(raw: bytes) -> BroadcastEphemeris:
    payload = _payload(raw, 220, "GALEPHB")
    week = _header_week(raw)
    rcv_fnav = _u4(payload, 4) & 1
    set_fnav = bool(rcv_fnav)
    svh_e1b = _u1(payload, 12) & 3
    svh_e5a = _u1(payload, 13) & 3
    svh_e5b = _u1(payload, 14) & 3
    dvs_e1b = _u1(payload, 15) & 1
    dvs_e5a = _u1(payload, 16) & 1
    dvs_e5b = _u1(payload, 17) & 1
    toc_s = float(_u4(payload, 148 if set_fnav else 176))
    af_offset = 152 if set_fnav else 180
    return BroadcastEphemeris(
        system="E",
        prn=_u4(payload, 0),
        toc_week=week,
        toc_s=toc_s,
        toe_s=float(_u4(payload, 24)),
        sqrt_a=_r8(payload, 28),
        deln=_r8(payload, 36),
        m0=_r8(payload, 44),
        ecc=_r8(payload, 52),
        omega=_r8(payload, 60),
        cuc=_r8(payload, 68),
        cus=_r8(payload, 76),
        crc=_r8(payload, 84),
        crs=_r8(payload, 92),
        cic=_r8(payload, 100),
        cis=_r8(payload, 108),
        i0=_r8(payload, 116),
        idot=_r8(payload, 124),
        omg0=_r8(payload, 132),
        omgd=_r8(payload, 140),
        f0=_r8(payload, af_offset),
        f1=_r8(payload, af_offset + 8),
        f2=_r8(payload, af_offset + 16),
        iode=_u4(payload, 20),
        iodc=_u4(payload, 20),
        code=(1 << 1) + (1 << 8) if set_fnav else (1 << 0) + (1 << 2) + (1 << 9),
        svh=(svh_e5b << 7) | (dvs_e5b << 6) | (svh_e5a << 4) | (dvs_e5a << 3) | (svh_e1b << 1) | dvs_e1b,
        sva=_galileo_sisa_value(_u1(payload, 18)),
        tgd0=_r8(payload, 204),
        tgd1=_r8(payload, 212),
        ttr_week=week,
        ttr_s=_header_tow_s(raw),
        source_message="GALEPHB",
    )


def _parse_bdsepha(fields: list[str]) -> BroadcastEphemeris:
    _require_fields(fields, 33, "BDSEPHA")
    bdt_week = _as_int(fields[5]) - 1356
    return BroadcastEphemeris(
        system="C",
        prn=_as_int(fields[0]),
        toc_week=bdt_week,
        toc_s=_as_float(fields[24]),
        toe_s=_as_float(fields[7]),
        sqrt_a=math.sqrt(_as_float(fields[8])),
        deln=_as_float(fields[9]),
        m0=_as_float(fields[10]),
        ecc=_as_float(fields[11]),
        omega=_as_float(fields[12]),
        cuc=_as_float(fields[13]),
        cus=_as_float(fields[14]),
        crc=_as_float(fields[15]),
        crs=_as_float(fields[16]),
        cic=_as_float(fields[17]),
        cis=_as_float(fields[18]),
        i0=_as_float(fields[19]),
        idot=_as_float(fields[20]),
        omg0=_as_float(fields[21]),
        omgd=_as_float(fields[22]),
        f0=_as_float(fields[27]),
        f1=_as_float(fields[28]),
        f2=_as_float(fields[29]),
        iode=_as_int(fields[3]),
        iodc=_as_int(fields[23]),
        code=0,
        svh=_as_int(fields[2]),
        sva=_ura_value(_as_float(fields[32])),
        tgd0=_as_float(fields[25]),
        tgd1=_as_float(fields[26]),
        ttr_week=bdt_week,
        ttr_s=_as_float(fields[1]),
        source_message="BDSEPHA",
    )


def _parse_bdsephb(raw: bytes) -> BroadcastEphemeris:
    payload = _payload(raw, 232, "BDSEPHB")
    bdt_week = _u4(payload, 24) - BDS_WEEK_OFFSET
    return BroadcastEphemeris(
        system="C",
        prn=_u4(payload, 0),
        toc_week=bdt_week,
        toc_s=_r8(payload, 164),
        toe_s=_r8(payload, 32),
        sqrt_a=math.sqrt(_r8(payload, 40)),
        deln=_r8(payload, 48),
        m0=_r8(payload, 56),
        ecc=_r8(payload, 64),
        omega=_r8(payload, 72),
        cuc=_r8(payload, 80),
        cus=_r8(payload, 88),
        crc=_r8(payload, 96),
        crs=_r8(payload, 104),
        cic=_r8(payload, 112),
        cis=_r8(payload, 120),
        i0=_r8(payload, 128),
        idot=_r8(payload, 136),
        omg0=_r8(payload, 144),
        omgd=_r8(payload, 152),
        f0=_r8(payload, 188),
        f1=_r8(payload, 196),
        f2=_r8(payload, 204),
        iode=_u4(payload, 16),
        iodc=_u4(payload, 160),
        code=0,
        svh=_u4(payload, 12),
        sva=_ura_value(_r8(payload, 224)),
        tgd0=_r8(payload, 172),
        tgd1=_r8(payload, 180),
        ttr_week=bdt_week,
        ttr_s=_header_tow_s(raw),
        source_message="BDSEPHB",
    )


def _parse_bd3ephb(raw: bytes) -> BroadcastEphemeris:
    payload = _payload(raw, 264, "BD3EPHB")
    sat_type = _u1(payload, 2)
    aref = BDS3_MEO_AREF_M if sat_type == 3 else BDS3_GEO_IGSO_AREF_M
    bdt_week = _u2(payload, 8) - BDS_WEEK_OFFSET
    freq_type = _u4(payload, 260)
    tgd0, tgd1 = _bd3_group_delays(payload, freq_type)
    return BroadcastEphemeris(
        system="C",
        prn=_u1(payload, 0),
        toc_week=bdt_week,
        toc_s=_r8(payload, 164),
        toe_s=_r8(payload, 20),
        sqrt_a=math.sqrt(aref + _r8(payload, 28)),
        deln=_r8(payload, 44),
        m0=_r8(payload, 60),
        ecc=_r8(payload, 68),
        omega=_r8(payload, 76),
        cuc=_r8(payload, 84),
        cus=_r8(payload, 92),
        crc=_r8(payload, 100),
        crs=_r8(payload, 108),
        cic=_r8(payload, 116),
        cis=_r8(payload, 124),
        i0=_r8(payload, 132),
        idot=_r8(payload, 140),
        omg0=_r8(payload, 148),
        omgd=_r8(payload, 156),
        f0=_r8(payload, 220),
        f1=_r8(payload, 228),
        f2=_r8(payload, 236),
        iode=_u2(payload, 4),
        iodc=_u2(payload, 6),
        code=int(freq_type),
        svh=_u1(payload, 1),
        sva=_bd3_accuracy(payload),
        tgd0=tgd0,
        tgd1=tgd1,
        ttr_week=bdt_week,
        ttr_s=_header_tow_s(raw),
        source_message="BD3EPHB",
    )


def _bd3_group_delays(payload: bytes, freq_type: int) -> tuple[float, float]:
    tgdb1cp = _r8(payload, 172)
    tgdb2ap = _r8(payload, 180)
    tgdb2bi = _r8(payload, 188)
    tgdb2bq = _r8(payload, 196)
    if freq_type == 1:
        return tgdb1cp, tgdb2ap
    if freq_type == 2:
        return tgdb1cp, tgdb2bi or tgdb2bq
    return tgdb1cp, tgdb2ap


def _bd3_accuracy(payload: bytes) -> float:
    # SISMAI is the direct BDS-3 signal-in-space monitoring accuracy index.
    # Preserve it as the RINEX accuracy value because the receiver does not
    # output an old-style URA variance for BD3EPH.
    return float(_u1(payload, 3))


def _parse_qzssepha(fields: list[str]) -> BroadcastEphemeris:
    eph = _parse_gpsepha(fields)
    return BroadcastEphemeris(
        **{
            **eph.__dict__,
            "system": "J",
            "prn": eph.prn + 192,
            "source_message": "QZSSEPHA",
        }
    )


def _parse_qzssephb(raw: bytes) -> BroadcastEphemeris:
    eph = _parse_gpsephb(raw)
    return BroadcastEphemeris(
        **{
            **eph.__dict__,
            "system": "J",
            "prn": eph.prn + 192,
            "source_message": "QZSSEPHB",
        }
    )


def _parse_irnssephb(raw: bytes) -> BroadcastEphemeris:
    payload = _payload(raw, 224, "IRNSSEPHB")
    week = _u4(payload, 24)
    return BroadcastEphemeris(
        system="I",
        prn=_u4(payload, 0),
        toc_week=week,
        toc_s=_r8(payload, 164),
        toe_s=_r8(payload, 32),
        sqrt_a=math.sqrt(_r8(payload, 40)),
        deln=_r8(payload, 48),
        m0=_r8(payload, 56),
        ecc=_r8(payload, 64),
        omega=_r8(payload, 72),
        cuc=_r8(payload, 80),
        cus=_r8(payload, 88),
        crc=_r8(payload, 96),
        crs=_r8(payload, 104),
        cic=_r8(payload, 112),
        cis=_r8(payload, 120),
        i0=_r8(payload, 128),
        idot=_r8(payload, 136),
        omg0=_r8(payload, 144),
        omgd=_r8(payload, 152),
        f0=_r8(payload, 180),
        f1=_r8(payload, 188),
        f2=_r8(payload, 196),
        iode=_u4(payload, 16),
        iodc=_u4(payload, 16),
        code=0,
        svh=((_u4(payload, 12) & 1) << 1) | (_u4(payload, 20) & 1),
        sva=_ura_value(_r8(payload, 216)),
        tgd0=_r8(payload, 172),
        tgd1=0.0,
        ttr_week=week,
        ttr_s=_header_tow_s(raw),
        source_message="IRNSSEPHB",
    )


def _parse_sbas_message(fields: list[str]) -> str | None:
    # RTKLIB .sbs records are: week, tow, prn, type, 29-byte hex payload.
    # UM980 SBAS ASCII fixtures are not currently available, so accept only an
    # already unambiguous RTKLIB-compatible payload shape.
    if len(fields) < 4:
        return None
    week = _as_int(fields[0])
    tow = _as_int(fields[1])
    prn = _as_int(fields[2])
    msg_hex = fields[-1].strip()
    if len(msg_hex) != 58 or any(ch not in "0123456789abcdefABCDEF" for ch in msg_hex):
        return None
    msg_type = int(msg_hex[2:4], 16) >> 2
    return f"{week:4d} {tow:6d} {prn:3d} {msg_type:2d} : {msg_hex.upper()}\n"


def _write_broadcast_nav(path: Path, nav_type: str, records: list[BroadcastEphemeris]) -> None:
    lines = [_rinex_nav_header(nav_type)]
    for eph in records:
        lines.extend(_broadcast_nav_lines(eph))
    path.write_text("".join(lines), encoding="ascii")


def _write_glonass_nav(path: Path, records: list[GlonassEphemeris]) -> None:
    lines = [_rinex_nav_header("R: GLONASS")]
    for geph in records:
        lines.extend(_glonass_nav_lines(geph))
    path.write_text("".join(lines), encoding="ascii")


def _rinex_nav_header(nav_type: str) -> str:
    return (
        f"{3.04:9.2f}           {'N: GNSS NAV DATA':<20}{nav_type:<20}{'RINEX VERSION / TYPE':<20}\n"
        f"{'um980-ppk':<20}{'UM980':<20}{_header_time():<20}{'PGM / RUN BY / DATE':<20}\n"
        f"{'':60}{'END OF HEADER':<20}\n"
    )


def _broadcast_nav_lines(eph: BroadcastEphemeris) -> list[str]:
    epoch = _broadcast_time(eph)
    ttr_s = eph.ttr_s if eph.ttr_s is not None else eph.toc_s
    ttr_week = eph.ttr_week if eph.ttr_week is not None else eph.toc_week
    ttr = ttr_s + (ttr_week - eph.toc_week) * 604800.0
    return [
        f"{eph.system}{eph.prn:02d} {_epoch_fields(epoch)}{_navf(eph.f0)}{_navf(eph.f1)}{_navf(eph.f2)}\n",
        f"    {_navf(eph.iode)}{_navf(eph.crs)}{_navf(eph.deln)}{_navf(eph.m0)}\n",
        f"    {_navf(eph.cuc)}{_navf(eph.ecc)}{_navf(eph.cus)}{_navf(eph.sqrt_a)}\n",
        f"    {_navf(eph.toe_s)}{_navf(eph.cic)}{_navf(eph.omg0)}{_navf(eph.cis)}\n",
        f"    {_navf(eph.i0)}{_navf(eph.crc)}{_navf(eph.omega)}{_navf(eph.omgd)}\n",
        f"    {_navf(eph.idot)}{_navf(eph.code)}{_navf(eph.toc_week)}{_navf(eph.flag)}\n",
        f"    {_navf(eph.sva)}{_navf(eph.svh)}{_navf(eph.tgd0)}{_navf(eph.tgd1 if eph.system in {'E', 'C'} else eph.iodc)}\n",
        f"    {_navf(ttr)}{_navf(eph.iodc if eph.system == 'C' else 0.0)}\n",
    ]


def _glonass_nav_lines(geph: GlonassEphemeris) -> list[str]:
    epoch = _gps_time(geph.toe_week, geph.toe_s) - timedelta(seconds=GPS_UTC_OFFSET_S)
    tof = geph.tof_s - GPS_UTC_OFFSET_S
    return [
        f"R{geph.prn:02d} {_epoch_fields(epoch)}{_navf(-geph.taun)}{_navf(geph.gamn)}{_navf(tof)}\n",
        f"    {_navf(geph.pos[0] / 1e3)}{_navf(geph.vel[0] / 1e3)}{_navf(geph.acc[0] / 1e3)}{_navf(geph.svh & 1)}\n",
        f"    {_navf(geph.pos[1] / 1e3)}{_navf(geph.vel[1] / 1e3)}{_navf(geph.acc[1] / 1e3)}{_navf(geph.frq)}\n",
        f"    {_navf(geph.pos[2] / 1e3)}{_navf(geph.vel[2] / 1e3)}{_navf(geph.acc[2] / 1e3)}{_navf(geph.age)}\n",
    ]


def _unique_broadcast(records: list[BroadcastEphemeris]) -> list[BroadcastEphemeris]:
    unique: dict[tuple[str, int, int, float, float, int, int, int], BroadcastEphemeris] = {}
    for record in records:
        code = 0 if record.source_message == "BD3EPHB" else record.code
        key = (record.system, record.prn, record.toc_week, record.toe_s, record.toc_s, record.iode, record.iodc, code)
        if key not in unique:
            unique[key] = record
    return list(unique.values())


def _unique_glonass(records: list[GlonassEphemeris]) -> list[GlonassEphemeris]:
    unique: dict[tuple[int, int, float, int], GlonassEphemeris] = {}
    for record in records:
        key = (record.prn, record.toe_week, record.toe_s, record.svh)
        if key not in unique:
            unique[key] = record
    return list(unique.values())


def _gps_time(week: int, tow_s: float) -> datetime:
    return GPS_EPOCH + timedelta(weeks=week, seconds=tow_s)


def _bdt_time(week: int, tow_s: float) -> datetime:
    return BDT_EPOCH + timedelta(weeks=week, seconds=tow_s)


def _broadcast_time(eph: BroadcastEphemeris) -> datetime:
    if eph.system == "C":
        return _bdt_time(eph.toc_week, eph.toc_s)
    return _gps_time(eph.toc_week, eph.toc_s)


def _epoch_fields(value: datetime) -> str:
    sec = value.second + value.microsecond / 1_000_000.0
    return f"{value.year:04d} {value.month:02d} {value.day:02d} {value.hour:02d} {value.minute:02d} {sec:02.0f}"


def _navf(value: float | int) -> str:
    return f"{float(value):19.12E}".replace("E", "D")


def _header_time() -> str:
    return datetime.now(UTC).strftime("%Y%m%d %H%M%S UTC")


def _ura_value(value: float) -> float:
    for idx, threshold in enumerate(URA_EPH):
        if threshold >= value:
            return URA_NOMINAL[idx]
    return URA_NOMINAL[-1]


def _galileo_sisa_value(index: float) -> float:
    """Convert a Galileo SISA index to the RINEX SV accuracy value in meters."""

    idx = int(index)
    if idx < 0:
        return 0.0
    if idx <= 49:
        return idx * 0.01
    if idx <= 74:
        return 0.5 + (idx - 50) * 0.02
    if idx <= 99:
        return 1.0 + (idx - 75) * 0.04
    if idx <= 125:
        return 2.0 + (idx - 100) * 0.16
    return 0.0


def _as_float(value: str) -> float:
    return float(value)


def _as_int(value: str) -> int:
    if value.upper() in {"TRUE", "FALSE"}:
        return 1 if value.upper() == "TRUE" else 0
    return int(float(value))


def _require_fields(fields: list[str], count: int, msg_type: str) -> None:
    if len(fields) < count:
        raise ValueError(f"{msg_type} payload has {len(fields)} fields, expected at least {count}")
