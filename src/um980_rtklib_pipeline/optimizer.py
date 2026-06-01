"""Bounded RTKLIB settings/base optimisation planning."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .rtklib_summary import RtklibSolutionSummary, summarize_rtklib_solution
from .time_window import ProcessingWindow

OptimizerFormat = Literal["table", "markdown", "json"]


@dataclass(frozen=True)
class OptimizerSample:
    """One bounded time sample selected for optimiser evaluation."""

    label: str
    start: datetime | None
    end: datetime | None
    duration_s: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly sample details."""

        return {
            "label": self.label,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "duration_s": self.duration_s,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OptimizerVariant:
    """One bounded optimiser variant."""

    name: str
    config: str | None
    base: str | None
    base_resolution: str
    nav_source: str
    sbas_source: str
    emit_ion_utc: str

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly variant details."""

        return {
            "name": self.name,
            "config": self.config,
            "base": self.base,
            "base_resolution": self.base_resolution,
            "nav_source": self.nav_source,
            "sbas_source": self.sbas_source,
            "emit_ion_utc": self.emit_ion_utc,
        }


@dataclass(frozen=True)
class OptimizerRun:
    """One planned sample/variant execution."""

    rover_file: str
    sample: str
    variant: str
    start: str | None = None
    end: str | None = None
    out_dir: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly run details."""

        return {
            "rover_file": self.rover_file,
            "sample": self.sample,
            "variant": self.variant,
            "start": self.start,
            "end": self.end,
            "out_dir": self.out_dir,
        }


@dataclass(frozen=True)
class OptimizerRunResult:
    """Executed optimiser run result."""

    run: OptimizerRun
    status: str
    command: list[str]
    returncode: int | None
    stdout_log: str
    stderr_log: str
    metrics: dict[str, object] | None
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly result details."""

        return {
            "run": self.run.as_dict(),
            "status": self.status,
            "command": self.command,
            "returncode": self.returncode,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "metrics": self.metrics,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class OptimizerPlan:
    """Resource-bounded optimiser plan."""

    dry_run: bool
    rover_files: list[str]
    processing_window: dict[str, object]
    samples: list[OptimizerSample]
    variants: list[OptimizerVariant]
    runs: list[OptimizerRun]
    warnings: list[str]
    results: list[OptimizerRunResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Return JSON-friendly plan details."""

        return {
            "dry_run": self.dry_run,
            "rover_files": self.rover_files,
            "processing_window": self.processing_window,
            "samples": [sample.as_dict() for sample in self.samples],
            "variants": [variant.as_dict() for variant in self.variants],
            "runs": [run.as_dict() for run in self.runs],
            "warnings": self.warnings,
            "results": [result.as_dict() for result in self.results],
            "limits": {
                "sample_count": len(self.samples),
                "variant_count": len(self.variants),
                "run_count": len(self.runs),
            },
        }


def parse_duration_seconds(value: str | int | float) -> float:
    """Parse compact durations such as ``120s``, ``5m`` or ``1h``."""

    if isinstance(value, int | float):
        seconds = float(value)
    else:
        text = value.strip().lower()
        if not text:
            raise ValueError("duration must not be empty")
        suffix = text[-1]
        multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0}.get(suffix)
        if multiplier is None:
            number = text
            multiplier = 1.0
        else:
            number = text[:-1]
        try:
            seconds = float(number) * multiplier
        except ValueError as exc:
            raise ValueError(f"invalid duration: {value}") from exc
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def load_base_list(path: Path) -> list[str]:
    """Load comma/newline separated base station IDs from a small text file."""

    bases: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        bases.append(line.split(",", maxsplit=1)[0].strip())
    return [base for base in bases if base]


def load_bases_from_candidates(path: Path, *, top_bases: int) -> list[str]:
    """Load top station markers from a base-candidates JSON report."""

    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("base-candidates JSON does not contain a candidates list")
    bases: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        marker = item.get("marker") or item.get("station")
        if marker:
            bases.append(str(marker))
        if len(bases) >= top_bases:
            break
    return bases


def build_optimizer_plan(
    *,
    rover_files: list[Path],
    config: Path | None,
    bases: list[str],
    base_resolution: str,
    nav_source: str,
    sbas_source: str,
    emit_ion_utc: str,
    window: ProcessingWindow,
    sample_count: int,
    sample_duration_s: float,
    max_variants: int,
    max_runs: int,
    dry_run: bool,
) -> OptimizerPlan:
    """Create a bounded optimiser plan without running RTKLIB."""

    if sample_count <= 0:
        raise ValueError("--sample-count must be positive")
    if max_variants <= 0:
        raise ValueError("--max-variants must be positive")
    if max_runs <= 0:
        raise ValueError("--max-runs must be positive")
    warnings: list[str] = []
    if not dry_run:
        warnings.append("execution mode will run bounded pipeline subprocesses; keep --max-runs small")
    samples = _select_samples(window, sample_count=sample_count, sample_duration_s=sample_duration_s, warnings=warnings)
    variants = _build_variants(
        config=config,
        bases=bases,
        base_resolution=base_resolution,
        nav_source=nav_source,
        sbas_source=sbas_source,
        emit_ion_utc=emit_ion_utc,
        max_variants=max_variants,
        warnings=warnings,
    )
    runs: list[OptimizerRun] = []
    for rover in rover_files:
        for sample in samples:
            for variant in variants:
                if len(runs) >= max_runs:
                    warnings.append(f"planned runs capped at --max-runs={max_runs}")
                    return OptimizerPlan(
                        dry_run=dry_run,
                        rover_files=[str(path) for path in rover_files],
                        processing_window=window.as_dict(),
                        samples=samples,
                        variants=variants,
                        runs=runs,
                        warnings=_dedupe(warnings),
                    )
                runs.append(
                    OptimizerRun(
                        rover_file=str(rover),
                        sample=sample.label,
                        variant=variant.name,
                        start=_fmt_dt(sample.start) or None,
                        end=_fmt_dt(sample.end) or None,
                    )
                )
    return OptimizerPlan(
        dry_run=dry_run,
        rover_files=[str(path) for path in rover_files],
        processing_window=window.as_dict(),
        samples=samples,
        variants=variants,
        runs=runs,
        warnings=_dedupe(warnings),
    )


def execute_optimizer_plan(plan: OptimizerPlan, *, out_dir: Path, keep_intermediate: bool) -> OptimizerPlan:
    """Execute a bounded plan by invoking the existing pipeline CLI per run."""

    out_dir.mkdir(parents=True, exist_ok=True)
    variant_by_name = {variant.name: variant for variant in plan.variants}
    results: list[OptimizerRunResult] = []
    for index, run in enumerate(plan.runs, start=1):
        variant = variant_by_name[run.variant]
        run_dir = out_dir / f"run-{index:03d}-{_safe_name(run.variant)}-{_safe_name(run.sample)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        command = _pipeline_command(run=run, variant=variant, run_dir=run_dir)
        stdout_log = run_dir / "pipeline.stdout.log"
        stderr_log = run_dir / "pipeline.stderr.log"
        stdout_file = stdout_log.open("w", encoding="utf-8")
        stderr_file = stderr_log.open("w", encoding="utf-8")
        try:
            completed = subprocess.run(command, check=False, stdout=stdout_file, stderr=stderr_file, text=True)
        finally:
            stdout_file.close()
            stderr_file.close()
        solution = _first_solution_file(run_dir)
        metrics = _metrics_from_solution(solution) if solution else None
        status = "ok" if completed.returncode == 0 and metrics is not None else "failed"
        warnings = [] if metrics is not None else ["no parseable RTKLIB solution metrics were produced"]
        results.append(
            OptimizerRunResult(
                run=OptimizerRun(
                    rover_file=run.rover_file,
                    sample=run.sample,
                    variant=run.variant,
                    start=run.start,
                    end=run.end,
                    out_dir=str(run_dir),
                ),
                status=status,
                command=command,
                returncode=completed.returncode,
                stdout_log=str(stdout_log),
                stderr_log=str(stderr_log),
                metrics=metrics,
                warnings=warnings,
            )
        )
    result_plan = OptimizerPlan(
        dry_run=False,
        rover_files=plan.rover_files,
        processing_window=plan.processing_window,
        samples=plan.samples,
        variants=plan.variants,
        runs=plan.runs,
        warnings=plan.warnings,
        results=results,
    )
    (out_dir / "optimizer-results.json").write_text(json.dumps(result_plan.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not keep_intermediate:
        # Keep run folders for reproducibility in this first backend. The flag is
        # accepted, but automatic cleanup is intentionally deferred.
        pass
    return result_plan


def format_optimizer_plan(plan: OptimizerPlan, output_format: OptimizerFormat) -> str:
    """Render an optimiser dry-run plan."""

    if output_format == "json":
        return json.dumps(plan.as_dict(), indent=2, sort_keys=True)
    lines = [
        f"optimizer dry_run={str(plan.dry_run).lower()} rovers={len(plan.rover_files)} samples={len(plan.samples)} variants={len(plan.variants)} runs={len(plan.runs)}"
    ]
    if plan.results:
        ok = sum(1 for result in plan.results if result.status == "ok")
        lines.append(f"executed_results: ok={ok} failed={len(plan.results) - ok}")
    if plan.warnings:
        lines.extend(f"warning: {warning}" for warning in plan.warnings)
    if output_format == "markdown":
        lines.append("")
        lines.append("| sample | start | end | duration_s | reason |")
        lines.append("| --- | --- | --- | --- | --- |")
        for sample in plan.samples:
            lines.append(
                f"| {sample.label} | {_fmt_dt(sample.start)} | {_fmt_dt(sample.end)} | {sample.duration_s:g} | {sample.reason} |"
            )
        lines.append("")
        lines.append("| variant | config | base | base_resolution | nav_source | sbas_source | emit_ion_utc |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for variant in plan.variants:
            lines.append(
                f"| {variant.name} | {variant.config or ''} | {variant.base or ''} | {variant.base_resolution} | {variant.nav_source} | {variant.sbas_source} | {variant.emit_ion_utc} |"
            )
        return "\n".join(lines)
    lines.append("samples:")
    for sample in plan.samples:
        lines.append(f"  {sample.label}: {_fmt_dt(sample.start)} .. {_fmt_dt(sample.end)} ({sample.reason})")
    lines.append("variants:")
    for variant in plan.variants:
        lines.append(
            f"  {variant.name}: config={variant.config or 'none'} base={variant.base or 'none'} "
            f"base_resolution={variant.base_resolution} nav_source={variant.nav_source} "
            f"sbas_source={variant.sbas_source} emit_ion_utc={variant.emit_ion_utc}"
        )
    return "\n".join(lines)


def _pipeline_command(*, run: OptimizerRun, variant: OptimizerVariant, run_dir: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "um980_rtklib_pipeline.cli",
        "pipeline",
        run.rover_file,
        "--out-dir",
        str(run_dir),
        "--basename",
        "optimizer",
        "--run-rtklib",
        "--base-resolution",
        variant.base_resolution,
        "--nav-source",
        _nav_source_for_pipeline(variant.nav_source),
        "--sbas-source",
        variant.sbas_source,
        "--emit-ion-utc",
        variant.emit_ion_utc,
    ]
    if run.start:
        command.extend(["--start-time", run.start])
    if run.end:
        command.extend(["--end-time", run.end])
    if variant.config:
        command.extend(["--rtkconf", variant.config])
    if variant.base:
        base_path = Path(variant.base)
        if base_path.exists():
            command.extend(["--base-obs", variant.base])
        else:
            command.extend(["--download-base", "--station", variant.base])
    return command


def _nav_source_for_pipeline(source: str) -> str:
    if source in {"auto-prefer-base", "merge"}:
        return source
    return source


def _first_solution_file(run_dir: Path) -> Path | None:
    for pattern in ("*-rtk.pos", "*-rtk.nmea", "*-rtk.llh"):
        matches = sorted(run_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _metrics_from_solution(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    summary = summarize_rtklib_solution(path)
    if summary is None:
        return None
    return _summary_to_metrics(summary)


def _summary_to_metrics(summary: RtklibSolutionSummary) -> dict[str, object]:
    distribution = {str(bucket.quality): bucket.count for bucket in summary.buckets}
    fixed = _bucket_count(summary, 1 if summary.quality_system == "rtklib_q" else 4)
    float_count = _bucket_count(summary, 2 if summary.quality_system == "rtklib_q" else 5)
    single = _bucket_count(summary, 5 if summary.quality_system == "rtklib_q" else 1)
    total = summary.sample_count or 1
    return {
        "quality_system": summary.quality_system,
        "epochs_total": summary.sample_count,
        "epochs_fixed": fixed,
        "epochs_float": float_count,
        "epochs_single": single,
        "pct_fixed": 100.0 * fixed / total,
        "pct_float": 100.0 * float_count / total,
        "pct_single": 100.0 * single / total,
        "duration_s": summary.duration_s,
        "track_m": summary.distance_m,
        "q_distribution": distribution,
    }


def _bucket_count(summary: RtklibSolutionSummary, quality: int) -> int:
    for bucket in summary.buckets:
        if bucket.quality == quality:
            return bucket.count
    return 0


def _safe_name(text: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in text)


def _select_samples(
    window: ProcessingWindow,
    *,
    sample_count: int,
    sample_duration_s: float,
    warnings: list[str],
) -> list[OptimizerSample]:
    labels = ["typical", "well", "medium", "bad"]
    if window.start is None or window.end is None:
        warnings.append("no selected datetime window supplied; dry-run samples are placeholders until baseline classification is available")
        return [
            OptimizerSample(
                label=labels[index % len(labels)] if sample_count <= len(labels) else f"sample-{index + 1}",
                start=None,
                end=None,
                duration_s=sample_duration_s,
                reason="placeholder; provide --start-time/--end-time or run baseline classification",
            )
            for index in range(sample_count)
        ]
    total_s = (window.end - window.start).total_seconds()
    if total_s <= 0:
        raise ValueError("processing window must have positive duration")
    duration_s = min(sample_duration_s, total_s)
    if duration_s < sample_duration_s:
        warnings.append("sample duration was clipped to the selected processing window")
    if sample_count == 1:
        offsets = [(total_s - duration_s) / 2.0]
    else:
        max_offset = max(0.0, total_s - duration_s)
        offsets = [max_offset * index / (sample_count - 1) for index in range(sample_count)]
    samples: list[OptimizerSample] = []
    for index, offset in enumerate(offsets):
        start = window.start + timedelta(seconds=offset)
        end = start + timedelta(seconds=duration_s)
        label = labels[index] if index < len(labels) else f"sample-{index + 1}"
        samples.append(
            OptimizerSample(
                label=label,
                start=start,
                end=end,
                duration_s=duration_s,
                reason="evenly spaced dry-run sample; baseline Q classification not executed",
            )
        )
    return samples


def _build_variants(
    *,
    config: Path | None,
    bases: list[str],
    base_resolution: str,
    nav_source: str,
    sbas_source: str,
    emit_ion_utc: str,
    max_variants: int,
    warnings: list[str],
) -> list[OptimizerVariant]:
    selected_bases = bases or [None]
    variants: list[OptimizerVariant] = []
    for index, base in enumerate(selected_bases):
        if len(variants) >= max_variants:
            warnings.append(f"variants capped at --max-variants={max_variants}")
            break
        suffix = base or "no-base"
        variants.append(
            OptimizerVariant(
                name="baseline" if index == 0 else f"base-{suffix}",
                config=str(config) if config else None,
                base=base,
                base_resolution=base_resolution,
                nav_source=nav_source,
                sbas_source=sbas_source,
                emit_ion_utc=emit_ion_utc,
            )
        )
    return variants


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _dedupe(items: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique
