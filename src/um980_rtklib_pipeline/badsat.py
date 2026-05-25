"""RTKLIB `.stat` based satellite quality classification."""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SatObservation:
    """One `$SAT` row from an RTKLIB solution-status file.

    Args:
        tow: GPS time of week in seconds.
        sat: RINEX satellite identifier, for example `G26`.
        frq: RTKLIB frequency slot.
        az: Satellite azimuth in degrees.
        el: Satellite elevation in degrees.
        resp: Code residual in meters.
        resc: Carrier-phase residual in meters.
        vsat: RTKLIB valid-satellite flag.
        snr: Signal-to-noise value from RTKLIB.
        fix: Ambiguity/fix state field.
        slip: Cycle-slip flag field.
        lock: Lock counter.
        outc: Outage counter.
        slipc: Slip counter.
        rejc: Rejection counter.
    """

    tow: float
    sat: str
    frq: int
    az: float
    el: float
    resp: float
    resc: float
    vsat: int
    snr: int
    fix: int
    slip: int
    lock: int
    outc: int
    slipc: int
    rejc: int


@dataclass(frozen=True)
class SatMetrics:
    """Aggregated satellite quality evidence from RTKLIB `$SAT` rows."""

    sat: str
    epochs: int
    rows: int
    mean_el: float
    low_el25_rate: float
    low_el30_rate: float
    mean_snr: float | None
    low_snr35_rate: float | None
    invalid_rate: float
    code_p95: float
    phase_p95: float
    combined_p95: float
    residual_gt2: int
    residual_gt5: int
    rej_delta: int
    slipc_delta: int
    hard_score: float
    watch_score: float
    reasons: list[str]


@dataclass(frozen=True)
class BadSatConfig:
    """Safety limits and thresholds for automatic satellite QC."""

    max_auto_exclude: int = 4
    max_high_el_exclude: int = 1
    max_low_el_exclude: int = 3
    enable_auto_elmask: bool = True
    min_auto_elmask: float = 15.0
    max_auto_elmask: float = 30.0
    preferred_cluster_elmask: float = 28.0
    min_remaining_sats: int = 9
    min_remaining_constellations: int = 2


@dataclass(frozen=True)
class BadSatDecision:
    """Selected automatic QC actions and retained evidence."""

    exclude_sats: list[str]
    watch_sats: list[str]
    recommended_elmask: float | None
    metrics: list[SatMetrics]
    blocked_reasons: dict[str, list[str]]


def percentile(values: list[float], q: float) -> float:
    """Return percentile `q` using linear interpolation."""

    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


def parse_rtklib_stat(path: Path) -> list[SatObservation]:
    """Parse RTKLIB `$SAT` rows from a `.stat` file.

    Malformed or unrelated rows are ignored so a partial status file still
    yields usable evidence.
    """

    observations: list[SatObservation] = []
    with path.open("r", newline="", errors="replace") as file:
        reader = csv.reader(file)
        for row in reader:
            if not row or row[0] != "$SAT":
                continue
            try:
                observations.append(
                    SatObservation(
                        tow=float(row[2]),
                        sat=row[3],
                        frq=int(row[4]),
                        az=float(row[5]),
                        el=float(row[6]),
                        resp=float(row[7]),
                        resc=float(row[8]),
                        vsat=int(row[9]),
                        snr=int(row[10]),
                        fix=int(row[11]),
                        slip=int(row[12]),
                        lock=int(row[13]),
                        outc=int(row[14]),
                        slipc=int(row[15]),
                        rejc=int(row[16]),
                    )
                )
            except (IndexError, ValueError):
                continue
    return observations


def compute_sat_metrics(observations: Iterable[SatObservation]) -> list[SatMetrics]:
    """Aggregate RTKLIB satellite residual evidence per satellite."""

    by_sat: dict[str, list[SatObservation]] = defaultdict(list)
    for observation in observations:
        by_sat[observation.sat].append(observation)

    metrics: list[SatMetrics] = []
    for sat, rows in by_sat.items():
        n = len(rows)
        code_res = [abs(row.resp) for row in rows if abs(row.resp) > 0]
        phase_res = [abs(row.resc) for row in rows if abs(row.resc) > 0]
        combined_res = [max(abs(row.resp), abs(row.resc)) for row in rows if max(abs(row.resp), abs(row.resc)) > 0]
        snr_nonzero = [row.snr for row in rows if row.snr > 0]

        code_p95 = percentile(code_res, 0.95)
        phase_p95 = percentile(phase_res, 0.95)
        combined_p95 = percentile(combined_res, 0.95)
        residual_gt2 = sum(1 for row in rows if max(abs(row.resp), abs(row.resc)) > 2.0)
        residual_gt5 = sum(1 for row in rows if max(abs(row.resp), abs(row.resc)) > 5.0)
        mean_el = statistics.fmean(row.el for row in rows)
        low_el25_rate = sum(1 for row in rows if row.el < 25.0) / n
        low_el30_rate = sum(1 for row in rows if row.el < 30.0) / n
        invalid_rate = sum(1 for row in rows if row.vsat == 0) / n
        mean_snr = statistics.fmean(snr_nonzero) if snr_nonzero else None
        low_snr35_rate = sum(1 for snr in snr_nonzero if snr < 35) / len(snr_nonzero) if snr_nonzero else None
        rej_delta = _counter_delta(rows, "rejc")
        slipc_delta = _counter_delta(rows, "slipc")
        hard_score, watch_score, reasons = _score_satellite(
            mean_el=mean_el,
            low_el30_rate=low_el30_rate,
            invalid_rate=invalid_rate,
            phase_p95=phase_p95,
            combined_p95=combined_p95,
            residual_gt5=residual_gt5,
            rej_delta=rej_delta,
        )
        metrics.append(
            SatMetrics(
                sat=sat,
                epochs=len({row.tow for row in rows}),
                rows=n,
                mean_el=mean_el,
                low_el25_rate=low_el25_rate,
                low_el30_rate=low_el30_rate,
                mean_snr=mean_snr,
                low_snr35_rate=low_snr35_rate,
                invalid_rate=invalid_rate,
                code_p95=code_p95,
                phase_p95=phase_p95,
                combined_p95=combined_p95,
                residual_gt2=residual_gt2,
                residual_gt5=residual_gt5,
                rej_delta=rej_delta,
                slipc_delta=slipc_delta,
                hard_score=hard_score,
                watch_score=watch_score,
                reasons=reasons,
            )
        )
    return sorted(metrics, key=lambda item: (item.hard_score, item.watch_score, item.combined_p95), reverse=True)


def choose_bad_sats(metrics: list[SatMetrics], config: BadSatConfig = BadSatConfig()) -> BadSatDecision:
    """Choose conservative satellite exclusions and watch-list entries."""

    low_candidates = [metric for metric in metrics if metric.hard_score > 0 and metric.low_el30_rate >= 0.8]
    high_candidates = [metric for metric in metrics if metric.hard_score > 0 and metric.low_el30_rate < 0.2]
    low_candidates.sort(key=lambda item: (item.combined_p95, item.residual_gt5, item.invalid_rate), reverse=True)
    high_candidates.sort(key=lambda item: (item.combined_p95, item.residual_gt5, item.phase_p95, item.rej_delta), reverse=True)

    exclude: list[str] = []
    blocked: dict[str, list[str]] = defaultdict(list)
    for metric in high_candidates:
        if len([sat for sat in exclude if _metric_by_sat(metrics, sat).low_el30_rate < 0.2]) >= config.max_high_el_exclude:
            blocked[metric.sat].append(f"blocked_by_max_high_el_exclude={config.max_high_el_exclude}")
            continue
        exclude.append(metric.sat)

    for metric in low_candidates:
        if len(exclude) >= config.max_auto_exclude:
            blocked[metric.sat].append(f"blocked_by_max_auto_exclude={config.max_auto_exclude}")
            continue
        if len([sat for sat in exclude if _metric_by_sat(metrics, sat).low_el30_rate >= 0.8]) >= config.max_low_el_exclude:
            blocked[metric.sat].append(f"blocked_by_max_low_el_exclude={config.max_low_el_exclude}")
            continue
        if metric.sat not in exclude:
            exclude.append(metric.sat)

    recommended_elmask = None
    if config.enable_auto_elmask and len(low_candidates) >= 2:
        recommended_elmask = min(config.max_auto_elmask, max(config.min_auto_elmask, config.preferred_cluster_elmask))

    remaining = [metric for metric in metrics if metric.sat not in set(exclude)]
    remaining_constellations = {_constellation(metric.sat) for metric in remaining}
    if len(remaining) < config.min_remaining_sats or len(remaining_constellations) < config.min_remaining_constellations:
        for sat in exclude[config.max_high_el_exclude :]:
            blocked[sat].append(
                "blocked_by_geometry_guard="
                f"remaining_sats:{len(remaining)},remaining_constellations:{len(remaining_constellations)}"
            )
        exclude = exclude[: config.max_high_el_exclude]

    excluded = set(exclude)
    watch = [
        metric.sat
        for metric in metrics
        if metric.sat not in excluded and (metric.watch_score >= 4.0 or metric.sat in blocked)
    ]
    return BadSatDecision(
        exclude_sats=exclude,
        watch_sats=watch,
        recommended_elmask=recommended_elmask,
        metrics=metrics,
        blocked_reasons=dict(blocked),
    )


def decision_to_json_dict(decision: BadSatDecision) -> dict[str, object]:
    """Return a JSON-serialisable representation of a QC decision."""

    return asdict(decision)


def _counter_delta(rows: list[SatObservation], field: str) -> int:
    by_frq: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        by_frq[row.frq].append(getattr(row, field))
    return sum(max(0, max(values) - min(values)) for values in by_frq.values() if values)


def _score_satellite(
    *,
    mean_el: float,
    low_el30_rate: float,
    invalid_rate: float,
    phase_p95: float,
    combined_p95: float,
    residual_gt5: int,
    rej_delta: int,
) -> tuple[float, float, list[str]]:
    hard_score = 0.0
    watch_score = 0.0
    reasons: list[str] = []
    if combined_p95 >= 3.0:
        watch_score += 2.0
        reasons.append(f"combined_p95={combined_p95:.2f}m")
    if residual_gt5 >= 30:
        watch_score += 2.0
        reasons.append(f"residual_gt5={residual_gt5}")
    if invalid_rate >= 0.4:
        watch_score += 1.0
        reasons.append(f"invalid_rate={invalid_rate:.0%}")
    if low_el30_rate >= 0.8:
        watch_score += 1.0
        reasons.append(f"low_el30_rate={low_el30_rate:.0%}")
    if phase_p95 >= 0.20:
        watch_score += 1.5
        reasons.append(f"phase_p95={phase_p95:.3f}m")
    if rej_delta >= 8:
        watch_score += 1.5
        reasons.append(f"rej_delta={rej_delta}")
    if low_el30_rate >= 0.8 and combined_p95 >= 3.0 and residual_gt5 >= 30:
        hard_score += 5.0
    if (
        mean_el >= 35.0
        and low_el30_rate < 0.2
        and combined_p95 >= 3.5
        and residual_gt5 >= 40
        and (phase_p95 >= 0.20 or rej_delta >= 8)
    ):
        hard_score += 4.0
    return hard_score, watch_score, reasons


def _constellation(sat: str) -> str:
    return sat[0] if sat else "?"


def _metric_by_sat(metrics: list[SatMetrics], sat: str) -> SatMetrics:
    for metric in metrics:
        if metric.sat == sat:
            return metric
    raise KeyError(sat)
