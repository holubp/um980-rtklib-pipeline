# Development Notes

## Validation

Use the local source tree when running tests before the package is installed:

```bash
PYTHONPATH=src pytest -q
```

Compile changed Python modules before committing:

```bash
python -m py_compile src/um980_rtklib_pipeline/*.py
```

## Protocol Safety

Do not guess UM980/Nebulas IV binary layouts or ephemeris field mappings. If a
message is detected but not fully validated, report it as unsupported or warn
clearly in analysis output. A plausible but wrong RINEX file is worse than a
clear failure.

Keep original NMEA sentence filtering structured. Position-only NMEA outputs
should parse sentence fields, preserve original lines, keep fractional
timestamps for multi-Hz data, and prefer GGA/GNS over RMC only when selecting a
single best sentence for the same timestamp.

Keep BESTNAV receiver-solution export separate from both live NMEA extraction
and RTKLIB raw processing. `BESTNAVA`/documented `BESTNAVB` may generate
checksummed app-readable NMEA, but RTKLIB estimation still requires raw
observations plus NAV/base inputs. When adding new BESTNAV fields, validate the
message layout with the Unicore manual and tests before emitting generated
sentences.

ION, UTC, and TROPINFO records should flow into message statistics and analysis
JSON even when they are not yet consumed downstream. Only enrich RINEX NAV
headers after verifying the exact RINEX header syntax and RTKLIB parser support
for the specific message family. Until then, mark them present-not-converted
rather than creating plausible but unsafe header lines.

Keep RTKLIB post-processing summaries source-aware. Standard `.pos`/`.llh`
outputs expose RTKLIB `Q` values, while NMEA outputs expose GGA fix-quality
codes with different meanings for values such as 4 and 5. Do not reuse RTKLIB
`Q` labels for NMEA quality summaries. When showing cross-references, use the
explicit mapping Q=1/2/4/5 to GGA quality 4/5/2/1.

Multiple RTKLIB output formats must share the same prepared rover/base/NAV
inputs but run `rnx2rtkp` separately per format. Output-format options such as
NMEA `-n` are command-level RTKLIB settings, not post-hoc filename conversions.

Header-only RINEX NAV files are rejected. Rover ASCII `GPSEPHA`, `GLOEPHA`, and
validated `GALEPHA` records may be written as non-empty RTKLIB-readable sidecar
files. Missing, malformed, or unsupported ephemeris records must stay visible as
warnings in logs and analysis JSON; never create placeholder NAV/GNAV/LNAV/SBS
files.

## RTKLIB Paths

Validate local files with local Python paths. Convert only the argument strings
passed to RTKLIB tools. Cygwin with Windows `.exe` tools usually requires
Windows-style input paths, and some Windows RTKLIB builds are PE binaries
without an `.exe` suffix. Native Linux/Cygwin builds require Unix paths. Keep
`--rtklib-path-style` behavior covered by tests.

Cygwin `cygpath -w` rewrites literal wildcard characters such as `*` into
private-use Unicode code points. For RTKLIB wildcard arguments, convert only the
parent directory and append the wildcard filename unchanged so RTKLIB receives a
real `*`.

Explicit tool paths must win over search directories. In particular,
`--crx2rnx ./crx2rnx.exe` and other relative or absolute paths must be resolved
as local paths before considering `--rtklib-dir`; only bare tool names should be
combined with RTKLIB install directories.

Do not treat `rnx2rtkp` exit code 0 as success unless the requested output file
exists. Some builds may write solution data to stdout despite `-o`; preserve
captured stdout as the requested output file, otherwise raise a clear runtime
error with the command, wrapper, and log paths.

The non-executable binary mirror is only for Android/Termux shared-storage
constraints. Never route Cygwin or desktop RTKLIB executables through
`/data/data/com.termux/...`; Cygwin should keep the selected executable path and
apply path conversion only to RTKLIB input/output/config arguments.

## RTKLIB Post-Processing

Keep generated `rnx2rtkp` options as a portable fallback. Quality-sensitive
work should use explicit config files such as `um980.conf` or
`um980-autoqc-baseline.conf`.

Before invoking RTKLIB, log the rover/base RINEX OBS capability comparison in
the directional form that matters for RTK: rover constellations and frequency
bands must be covered by the base, while extra base capability is acceptable.
Keep exact observation-code gaps at debug level so verbose logs stay readable.

The optional two-pass satellite QC path is split into reusable modules:

- `badsat.py` parses RTKLIB `.stat` `$SAT` rows and selects conservative
  exclusions/watch-list entries.
- `rtklib_config_patch.py` writes the exact derived RTKLIB config used for pass
  2.
- `badsat_report.py` writes Markdown and JSON reports.

Do not make automatic satellite QC implicit. It must require `--auto-sat-qc`,
write pass-1 stat evidence, and keep the derived config inspectable.

## EUREF Fixtures

Keep `test-euref.zip`, downloaded station observations, and RTKLIB-ex source/bin
trees out of git. Use the archived helper scripts only to verify URL naming
conventions; live servers may not publish every legacy RINEX 2 or high-rate
product for a given station/time. Missing products must warn with the attempted
URLs before fallback or failure.

BKG URL templates must be verified against public directory listings and sample
files before being added. BKG high-rate downloads preflight the directory index
so missing station/time combinations fall back with one clear warning rather
than one failed download per 15-minute file and mirror.

Base-resolution selection is part of the processing contract. High-rate
requests must first plan and try generic high-rate 1 s archive products and
must validate selected filenames before RTKLIB runs. Low-rate cached
`01H_30S`/`_30S_` products must not satisfy the high-rate attempt; they are only
valid after the high-rate group fails and fallback is enabled. Recorded
real-time base RTCM is a separate source: convert it with `convbin -r rtcm3`
and reject ambiguous `--base-rtcm` plus `--download-base` combinations.

## Advisory And Optimizer Modes

`base-candidates` is advisory-only. It can use BESTNAV/live NMEA solution
points, cached or refreshed EPN SSC station catalogues, and lightweight archive
probes to rank stations, but it must not mutate normal pipeline defaults or
auto-select a base. Keep catalogue and archive probe caches explicit in JSON so
recommendations are reproducible. Header/probe logic should use small metadata
requests such as HTTP `HEAD`; do not fetch full observation bodies for an
advisory run.

`optimize-settings` must stay resource-bounded. The default path is dry-run
planning; execution requires `--execute`, caps variants/runs, stores per-run
commands/logs, and parses compact metrics from RTKLIB outputs. Do not introduce
unbounded RTKLIB setting mutation or high trace output as a default optimiser
behavior.
