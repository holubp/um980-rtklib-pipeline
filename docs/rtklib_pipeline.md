# RTKLIB Pipeline

This project bridges the gap between UM980 mixed receiver logs and RTKLIB's
file-based post-processing model. A rover `.unc` capture is not a RTKLIB input
by itself: it must be parsed, decoded into RINEX observations, paired with
navigation data, matched with base-station observations, and passed to
RTKLIB with paths that make sense on the current platform.

```text
UM980 .unc
   |
   v
parse mixed NMEA + Unicore records
   |--------> diagnostics / warnings
   v
rover RINEX OBS + generated rover NAV/GNAV/LNAV/SBS
        + EUREF/base RINEX OBS + external NAV/SP3/CLK
   |
   v
validated rnx2rtkp command
   |
   v
RTK/PPK solution
```

The pipeline writes direct rover observation products from `OBSVMA`, documented
binary `OBSVMB`, and compressed binary `OBSVMCMPB` records, then validates all
RTKLIB input paths before invoking `rnx2rtkp`. It never passes unresolved shell
wildcards to RTKLIB and does not use `shell=True`.

Navigation data must come from explicit NAV/SP3/CLK files, downloaded or
base-derived data, or receiver ephemeris logs. Raw observations alone are not
navigation data. When ASCII rover ephemeris records are present, the RINEX step
writes RTKLIB-readable sidecar files:

- `*.rover-gps.nav` from `GPSEPHA`;
- `*.rover-glo.gnav` from `GLOEPHA`;
- `*.rover-gal.lnav` from `GALEPHA`;
- `*.rover-bds.cnav` from `BDSEPHA`;
- `*.rover-sbas.sbs` from unambiguous RTKLIB-shaped SBAS message records.

The tool deliberately refuses ambiguous or empty navigation inputs:

- missing, malformed, or unsupported rover ephemeris records are warnings, not
  silent drops;
- no header-only NAV/GNAV/LNAV/SBS files are written;
- header-only NAV files are not selected for RTKLIB;
- unsupported observation formats and incomplete RINEX signal mapping are written
  as warnings in analysis JSON and logged by the CLI.

## Python Tool Installation

Install the CLI into the system Python 3.11+ environment when you want
`um980-ppk` available as a normal machine-wide command. On Linux this is
typically:

```bash
sudo -H python3 -m pip install .
```

On platforms without `sudo`, use the equivalent administrator/root shell, or the
active Python environment if it is already the system environment:

```bash
python3 -m pip install .
```

Use optional extras when needed:

```bash
python3 -m pip install '.[config]'
python3 -m pip install '.[config,test]'
```

For testing without touching the system Python installation, use either an
editable virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[config,test]'
pytest -q
```

or run directly from the repository checkout:

```bash
PYTHONPATH=src python -m um980_rtklib_pipeline.cli --help
PYTHONPATH=src python -m um980_rtklib_pipeline.cli pipeline rover.unc --out-dir out
PYTHONPATH=src pytest -q
```

## Integrated Pipeline

`um980-ppk pipeline` now runs the full local flow when RTKLIB inputs are
available: extraction, rover RINEX OBS generation, optional EUREF base download,
base position resolution, NAV validation, and `rnx2rtkp` execution.

```bash
um980-ppk pipeline rover.unc \
  --download-base \
  --station CPAR \
  --base-resolution high \
  --base-rinex-version 3 \
  --crx2rnx ~/RTKLIB-ex-bin/bin/crx2rnx \
  --run-rtklib
```

The pipeline does not assume a non-existent RTKLIB config. If `--rtkconf` is
provided, it is passed to `rnx2rtkp` with `-k` and must exist. If `--rtkconf` is
omitted, the command is generated from CLI options: kinematic mode,
L1/L2/L5, GPS+GLONASS+Galileo+BeiDou+QZSS, 10 degree elevation mask, and
combined post-processing by default. Override those defaults with
`--rtk-pos-mode`, `--rtk-frequency`, `--navsys`, `--rtk-navsys`,
`--rtk-elevation-mask`, `--rtk-soltype`, `--rtk-ar-mode`, or repeated
`--rnx2rtkp-option=TOKEN` arguments.

`--output-format pos` is the conventional `.pos` suffix for RTKLIB's normal
latitude/longitude/height content; in RTKLIB config files that content is
called `out-solformat=llh`. `--output-format nmea` is an RTKLIB output-format
request, not just a filename suffix. The pipeline adds `rnx2rtkp -n`, which is
the command-line equivalent of `out-solformat=nmea`, even when `--rtkconf` is
also supplied.

Use `--rtklib-trace-level 4 --rtklib-stat-level 2` to produce the usual
debugging equivalent of `rnx2rtkp -x 4 -y 2`. These named options are passed as
command-line overrides in both generated-option mode and `--rtkconf` mode.

## Two-Pass Satellite QC

The pipeline supports an explicit opt-in two-pass mode:

```bash
um980-ppk pipeline rover.unc \
  --download-base \
  --station CPAR \
  --rtkconf um980-autoqc-baseline.conf \
  --auto-sat-qc \
  --run-rtklib
```

This mode requires `--rtkconf`; use `um980-autoqc-baseline.conf` as the pass-1
baseline unless intentionally testing another profile. Pass 1 enables RTKLIB
solution-status residual output with `-y 2`, parses `$SAT` rows from the
generated `.stat`, and applies conservative satellite QC rules. Pass 2 runs
with an inspectable derived config. For an output stem `run`, the expected
artefacts are:

```text
run.pass1.pos
run.pass1.stat
run.autoqc.derived.conf
run.autoqc.report.md
run.autoqc.report.json
run.pos
run.stat
```

The derived config may add `pos1-exclsats` and/or raise `pos1-elmask`. Safety
limits default to `--max-auto-exclude 4`, `--max-high-el-exclude 1`,
`--max-low-el-exclude 3`, `--min-remaining-sats 9`, and
`--min-remaining-constellations 2`. Satellites that look suspicious but are
blocked by caps or geometry protection are listed in the watch list instead of
being excluded.

`--base-resolution low` selects hourly 30 s EUREF data. `high` selects 1 s
high-rate chunks and falls back to low-rate data with a warning when the
high-rate files are unavailable. `--base-rinex-version 2` enables compact RINEX
2/Hatanaka EUREF names, and `auto` tries v3 before v2.
Base downloads are planned from the generated rover RINEX observation span and
include every base product that overlaps or touches that span. The default
`--time-margin 0` avoids fetching adjacent non-overlapping products; set a
positive margin only when that extra coverage is intentional.
Downloads reuse cached archives, decompressed files, and converted RINEX
products by default. Use `--force-download` only when the source archive should
be fetched again.
When more than one base observation file survives overlap filtering, the
pipeline stages those exact files into `<basename>.rtklib-base/` and passes
`<basename>.rtklib-base/base-*.rnx` or the matching suffix as one argument to
`rnx2rtkp`. This wildcard is quoted in the generated wrapper and subprocess
argv, so the shell does not expand it; RTKLIB expands it internally as the
single base observation input.

The integrated pipeline defaults to `--rinex-compat convbin` for the rover OBS
file passed to RTKLIB. Standalone `rinex` keeps the broader native ordering by
default, but both modes suppress non-standard unknown-system `U` satellites so
RTKLIB does not reject the RINEX 3 header with an invalid system code.

The pipeline automatically includes generated rover `.nav`, `.gnav`, `.lnav`,
and `.sbs` files in the `rnx2rtkp` command when those files are non-empty. Add
`--nav-file` for external BRDC, precise orbit/clock, or other navigation inputs
when the receiver log does not contain complete coverage for the observed
systems. This is common for Galileo/BDS/QZSS/SBAS if the receiver was not
configured to log the matching ephemeris messages.

Use `-d` or `--debug` when RTKLIB exits unexpectedly. Debug mode includes
verbose progress and logs the exact shell-quoted `rnx2rtkp` command, the
generated wrapper script, and stdout/stderr log paths before execution, so the
same command can be rerun manually.

Generate a receiver init script with all ASCII ephemeris messages enabled for
debugging:

```bash
um980-ppk init generate \
  --raw-format obsvmcmpb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --debug-ascii-ephemeris \
  --out um980-debug-ephem.cmd
```

The generated script warns that this profile can create large `.unc` files and
shows the ASCII ephemeris contribution to the estimated 8N1 line utilisation.
Use `--ephemeris every=300 --ephemeris-format binary` when collecting binary
ephemeris fixtures. Valid binary ephemeris records are converted into rover NAV
sidecars for RTKLIB; malformed or unsupported records are logged and kept out of
the RTKLIB invocation.

`--rinex-compat convbin` makes the direct RINEX observation output stricter for
RTKLIB compatibility. It orders observation types like RTKLIB `convbin` and
uses the same extended single-line observation records that RTKLIB-ex reads
from `convbin` output. It excludes records that convbin would not safely emit,
including unknown-system satellites and OBSVMA epochs captured before the
receiver reports `FINE` time.

## Cygwin and Windows RTKLIB Tools

Cygwin is a mixed environment: Python and the filesystem often use Unix paths
such as `/cygdrive/c/ppk/rover.obs`, while Windows RTKLIB executables receive
their command-line arguments as Windows processes and usually require paths such
as `C:\ppk\rover.obs`.

`um980-ppk postprocess` separates these concerns:

- local validation uses the paths supplied to the Python process;
- command arguments passed to RTKLIB are converted according to
  `--rtklib-path-style`;
- `auto` uses Windows argument paths when running on Cygwin with a `.exe`
  `rnx2rtkp`;
- `unix` is for native Cygwin/Linux RTKLIB builds;
- `windows` forces Windows argument paths.

`--rtklib-dir` can be used with bare tool names:

```bash
um980-ppk postprocess rover.unc \
  --rtklib-dir /cygdrive/c/RTKLIB/bin \
  --rnx2rtkp rnx2rtkp.exe \
  --rtklib-path-style auto \
  --rover-obs /cygdrive/c/ppk/rover.direct.obs \
  --base-obs /cygdrive/c/ppk/base.obs \
  --nav-file /cygdrive/c/ppk/brdc.nav \
  --rtkconf /cygdrive/c/ppk/rtkpost-normal.conf
```

## Base Reference Position

Relative RTKLIB runs need a usable base reference position. `postprocess`
supports three deterministic options:

```bash
um980-ppk postprocess rover.unc \
  --rover-obs rover.direct.obs \
  --base-obs CPAR.obs \
  --nav-file brdc.rnx \
  --base-station CPAR \
  --base-position-cache-dir euref-cache/coordinates \
  --rtkconf rtkpost-normal.conf
```

- `--base-ecef X Y Z` passes RTKLIB `-r X Y Z`.
- `--base-llh LAT LON HEIGHT` passes RTKLIB `-l LAT LON HEIGHT`.
- `--base-station` resolves EPN/EUREF ETRF2000 ECEF coordinates and passes
  `-r X Y Z`.
- In `auto` mode, if no station is supplied or EPN lookup fails, the tool uses
  the first base OBS file's `APPROX POSITION XYZ` header.
- `--base-position-source=none` disables this and leaves base position handling
  entirely to the RTKLIB config.

## Local RTKLIB-ex Install

The repository supports a local, untracked RTKLIB-ex install tree:

```text
~/RTKLIB-ex-bin/bin/rnx2rtkp
~/RTKLIB-ex-bin/bin/convbin
~/RTKLIB-ex-bin/bin/str2str
~/RTKLIB-ex-bin/bin/pos2kml
~/RTKLIB-ex-bin/bin/rtkrcv

build-tools/RTKLIB-ex-bin/bin/rnx2rtkp
build-tools/RTKLIB-ex-bin/bin/convbin
build-tools/RTKLIB-ex-bin/bin/str2str
build-tools/RTKLIB-ex-bin/bin/pos2kml
build-tools/RTKLIB-ex-bin/bin/rtkrcv
```

`~/RTKLIB-ex-bin/bin/` is preferred on Android/Termux because `$HOME` supports
direct execution. Both `build-tools/RTKLIB-ex/` and `build-tools/RTKLIB-ex-bin/`
are local-only and must not be committed. `postprocess` resolves RTKLIB tools in
this order:

1. explicit user-provided executable path;
2. `--rtklib-dir`;
3. `~/RTKLIB-ex-bin/bin/`;
4. `build-tools/RTKLIB-ex-bin/bin/`;
5. system `PATH`.

For downloaded Hatanaka base files, `crx2rnx` discovery also checks the current
directory and considers `crx2rnx.exe` when no explicit `--crx2rnx` path is
provided.

On Android/Termux shared storage, the binaries in `build-tools/RTKLIB-ex-bin/`
may be readable but not executable. In that case the pipeline copies the chosen
tool to Termux-private temporary storage and runs the mirrored executable.
