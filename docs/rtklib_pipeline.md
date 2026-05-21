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
