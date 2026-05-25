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
