# RTKLIB Pipeline

The pipeline writes direct rover observation products and validates all RTKLIB
input paths before invoking `rnx2rtkp`. It never passes unresolved shell
wildcards to RTKLIB and does not use `shell=True`.

Navigation data must come from explicit NAV/SP3/CLK files, downloaded or
base-derived data, or receiver ephemeris logs. Raw observations alone are not
navigation data.

The tool deliberately refuses ambiguous or empty navigation inputs:

- `GPSEPHA` records are counted and reported, but no header-only NAV file is
  written until field mapping is implemented;
- header-only NAV files are not selected for RTKLIB;
- unsupported observation formats and incomplete RINEX signal mapping are written
  as warnings in analysis JSON and logged by the CLI.

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
  --nav-file BRDC00WRD_R_20261380000_01D_MN.rnx \
  --rtkconf examples/rtkpost-normal.conf.example \
  --run-rtklib
```

`--base-resolution low` selects hourly 30 s EUREF data. `high` selects 1 s
high-rate chunks and falls back to low-rate data with a warning when the
high-rate files are unavailable. `--base-rinex-version 2` enables compact RINEX
2/Hatanaka EUREF names, and `auto` tries v3 before v2.

The pipeline still requires explicit NAV input. It does not silently use
header-only rover NAV placeholders.

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

On Android/Termux shared storage, the binaries in `build-tools/RTKLIB-ex-bin/`
may be readable but not executable. In that case the pipeline copies the chosen
tool to Termux-private temporary storage and runs the mirrored executable.
