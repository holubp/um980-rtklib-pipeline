"""Structured message-family statistics for UM980 mixed captures."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from .bestnav import BestNavExtraction
from .obs_decode import ObservationExtraction
from .rinex_nav import NavExtractionReport
from .solution import SolutionExtraction
from .stream import StreamDiagnostics, StreamRecord

ION_MESSAGES = {"GPSIONA", "BDSIONA", "BD3IONA", "GALIONA", "GPSIONB", "BDSIONB", "BD3IONB", "GALIONB"}
UTC_MESSAGES = {"GPSUTCA", "BDSUTCA", "BD3UTCA", "GALUTCA", "GPSUTCB", "BDSUTCB", "BD3UTCB", "GALUTCB"}
TROPO_MESSAGES = {"TROPINFOA", "TROPINFOB"}
SOLUTION_MESSAGES = {"BESTNAVA", "BESTNAVB", "PPPNAVA", "ADRNAVA"}
OBS_MESSAGES = {"OBSVMA", "OBSVMB", "OBSVMCMPB"}


@dataclass
class MessageStats:
    """Counters summarising parsed receiver-message families."""

    total_records: int = 0
    total_bytes: int = 0
    nmea_records: Counter[str] = field(default_factory=Counter)
    unicore_ascii_records: Counter[str] = field(default_factory=Counter)
    unicore_binary_records: Counter[str] = field(default_factory=Counter)
    solution_records: Counter[str] = field(default_factory=Counter)
    bestnav_records: Counter[str] = field(default_factory=Counter)
    bestnav_valid_epochs: int = 0
    raw_observation_records: Counter[str] = field(default_factory=Counter)
    raw_observation_epochs: int = 0
    raw_observations: int = 0
    ephemeris_records: Counter[str] = field(default_factory=Counter)
    ephemeris_converted: Counter[str] = field(default_factory=Counter)
    ephemeris_unsupported: Counter[str] = field(default_factory=Counter)
    ionosphere_records: Counter[str] = field(default_factory=Counter)
    ionosphere_converted: Counter[str] = field(default_factory=Counter)
    ionosphere_present_not_converted: Counter[str] = field(default_factory=Counter)
    utc_records: Counter[str] = field(default_factory=Counter)
    utc_converted: Counter[str] = field(default_factory=Counter)
    utc_present_not_converted: Counter[str] = field(default_factory=Counter)
    tropo_records: Counter[str] = field(default_factory=Counter)
    tropo_converted: Counter[str] = field(default_factory=Counter)
    malformed_records: Counter[str] = field(default_factory=Counter)
    unsupported_records: Counter[str] = field(default_factory=Counter)
    checksum_failures: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly statistics."""

        return {
            "stream": {
                "total_records": self.total_records,
                "total_bytes": self.total_bytes,
                "nmea": sum(self.nmea_records.values()),
                "unicore_ascii": sum(self.unicore_ascii_records.values()),
                "unicore_binary": sum(self.unicore_binary_records.values()),
            },
            "nmea_records": dict(self.nmea_records),
            "unicore_ascii_records": dict(self.unicore_ascii_records),
            "unicore_binary_records": dict(self.unicore_binary_records),
            "solutions": dict(self.solution_records),
            "bestnav": {
                "records": dict(self.bestnav_records),
                "valid_epochs": self.bestnav_valid_epochs,
            },
            "raw_observations": {
                "records": dict(self.raw_observation_records),
                "epochs": self.raw_observation_epochs,
                "observations": self.raw_observations,
            },
            "ephemerides": {
                "records": dict(self.ephemeris_records),
                "converted": dict(self.ephemeris_converted),
                "unsupported": dict(self.ephemeris_unsupported),
            },
            "ionosphere": {
                "records": dict(self.ionosphere_records),
                "converted": dict(self.ionosphere_converted),
                "present_not_converted": dict(self.ionosphere_present_not_converted),
            },
            "utc": {
                "records": dict(self.utc_records),
                "converted": dict(self.utc_converted),
                "present_not_converted": dict(self.utc_present_not_converted),
            },
            "troposphere": {
                "records": dict(self.tropo_records),
                "converted": dict(self.tropo_converted),
                "diagnostic_only": dict(self.tropo_records),
            },
            "unsupported": dict(self.unsupported_records),
            "malformed": dict(self.malformed_records),
            "checksum_failures": dict(self.checksum_failures),
            "warnings": self.warnings,
        }


def build_message_stats(
    *,
    records: list[StreamRecord],
    stream: StreamDiagnostics,
    solutions: SolutionExtraction,
    observations: ObservationExtraction,
    rover_nav: NavExtractionReport,
    bestnav: BestNavExtraction,
) -> MessageStats:
    """Build central message statistics from existing extraction results."""

    stats = MessageStats(total_records=len(records), total_bytes=stream.input_bytes)
    stats.nmea_records.update(stream.nmea_types)
    for record in records:
        msg_type = record.msg_type or "unknown"
        if record.kind == "unicore_ascii":
            stats.unicore_ascii_records[msg_type] += 1
        elif record.kind == "unicore_binary":
            stats.unicore_binary_records[msg_type] += 1
        if record.checksum_ok is False:
            stats.checksum_failures[msg_type] += 1
        if msg_type in SOLUTION_MESSAGES or msg_type[-3:] in {"GGA", "GNS", "RMC"}:
            stats.solution_records[msg_type] += 1
        if msg_type in OBS_MESSAGES:
            stats.raw_observation_records[msg_type] += 1
        if msg_type in ION_MESSAGES:
            stats.ionosphere_records[msg_type] += 1
        if msg_type in UTC_MESSAGES:
            stats.utc_records[msg_type] += 1
        if msg_type in TROPO_MESSAGES:
            stats.tropo_records[msg_type] += 1

    stats.bestnav_records.update(bestnav.present)
    stats.bestnav_valid_epochs = len(bestnav.records)
    stats.malformed_records.update(bestnav.malformed)
    stats.raw_observation_epochs = int(observations.metrics.get("epochs", 0) or 0)
    stats.raw_observations = int(observations.metrics.get("observations", 0) or 0)
    stats.unsupported_records.update(observations.unsupported_records)
    stats.ephemeris_records.update(rover_nav.found)
    stats.ephemeris_converted.update(rover_nav.converted)
    for message, found in rover_nav.found.items():
        converted = rover_nav.converted.get(message, 0)
        if found > converted:
            stats.ephemeris_unsupported[message] += found - converted
    stats.ionosphere_present_not_converted.update(stats.ionosphere_records)
    stats.utc_present_not_converted.update(stats.utc_records)
    stats.malformed_records["nmea_checksum"] += stream.invalid_nmea_records
    stats.malformed_records["unicore_binary_frame"] += stream.invalid_unicore_binary_records

    if stats.ionosphere_records:
        stats.warnings.append(
            "ION messages are decoded as present diagnostics but are not written to RINEX NAV headers until "
            "each family mapping is verified against RTKLIB parser expectations."
        )
    if stats.utc_records:
        stats.warnings.append(
            "UTC/time-system messages are decoded as present diagnostics but are not written to RINEX NAV headers "
            "until each family mapping is verified."
        )
    if stats.tropo_records:
        stats.warnings.append("TROPINFO is receiver/PPP diagnostic information and is not passed to rnx2rtkp.")
    stats.warnings.extend(bestnav.warnings)
    return stats


def log_message_stats(stats: MessageStats, *, debug: bool = False) -> None:
    """Log concise INFO statistics and optional DEBUG details."""

    logging.info("Input message summary:")
    logging.info(
        "  stream: total_records=%d nmea=%d unicore_ascii=%d unicore_binary=%d",
        stats.total_records,
        sum(stats.nmea_records.values()),
        sum(stats.unicore_ascii_records.values()),
        sum(stats.unicore_binary_records.values()),
    )
    logging.info(
        "  receiver solutions: BESTNAV=%d valid_epochs=%d live_nmea_position=%d",
        sum(stats.bestnav_records.values()),
        stats.bestnav_valid_epochs,
        sum(count for msg, count in stats.nmea_records.items() if msg[-3:] in {"GGA", "GNS", "RMC"}),
    )
    logging.info(
        "  raw observations: records=%d epochs=%d observations=%d",
        sum(stats.raw_observation_records.values()),
        stats.raw_observation_epochs,
        stats.raw_observations,
    )
    logging.info(
        "  ephemerides: records=%d converted=%d unsupported=%d",
        sum(stats.ephemeris_records.values()),
        sum(stats.ephemeris_converted.values()),
        sum(stats.ephemeris_unsupported.values()),
    )
    logging.info(
        "  ionosphere=%d utc=%d troposphere=%d unsupported=%d malformed=%d",
        sum(stats.ionosphere_records.values()),
        sum(stats.utc_records.values()),
        sum(stats.tropo_records.values()),
        sum(stats.unsupported_records.values()),
        sum(stats.malformed_records.values()),
    )
    for warning in stats.warnings:
        logging.warning("%s", warning)
    if not debug:
        return
    logging.debug("message stats nmea=%s", dict(stats.nmea_records))
    logging.debug("message stats unicore_ascii=%s", dict(stats.unicore_ascii_records))
    logging.debug("message stats unicore_binary=%s", dict(stats.unicore_binary_records))
    logging.debug("message stats bestnav=%s valid_epochs=%d", dict(stats.bestnav_records), stats.bestnav_valid_epochs)
    logging.debug("message stats ionosphere present_not_converted=%s", dict(stats.ionosphere_present_not_converted))
    logging.debug("message stats utc present_not_converted=%s", dict(stats.utc_present_not_converted))
