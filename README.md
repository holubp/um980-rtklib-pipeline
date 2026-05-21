# UM980 RTKLIB Pipeline

Python tools for Unicore UM980 mixed serial logs and RTKLIB post-processing.

The CLI command is `um980-ppk`. It can generate UM980 logging scripts, analyse
mixed logs, extract clean NMEA and solution tracks, create observation CSV files,
write RINEX 3 observation scaffolds, resolve NAV inputs, and assemble safe
`rnx2rtkp` invocations.

This project intentionally does not depend on RTKLIB `convbin` for rover UM980
`.unc` conversion. Many RTKLIB builds do not include Unicore support, and raw
observations must not be treated as navigation data.

## Examples

Generate a receiver command script:

```bash
um980-ppk init generate \
  --port COM1 \
  --baud 230400 \
  --mode rover \
  --raw-format obsvmb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --ephemeris every=300 \
  --ppp e6-has \
  --save-config \
  --out um980-init.cmd \
  -v
```

Extract solution products and observations:

```bash
um980-ppk extract rover.unc -v --analysis-json --obs-csv --solution all
```

Create rover RINEX observation output:

```bash
um980-ppk rinex rover.unc --obs-csv -v
```

Download base data URLs for a rover time window without fetching:

```bash
um980-ppk download-base rover.unc --station CPAR --offline -v
```

Run post-processing with explicit inputs:

```bash
um980-ppk postprocess rover.unc \
  --rover-obs rover.direct.obs \
  --base-obs CPAR.obs \
  --nav-file BRDC00WRD_R_20261380000_01D_MN.rnx \
  --rnx2rtkp rnx2rtkp \
  --rtkconf rtkpost-normal.conf
```

On Cygwin, Windows RTKLIB `.exe` tools usually need Windows-style paths for
input files even though the Python pipeline sees Unix paths. `postprocess`
auto-detects Cygwin plus `.exe` and passes Windows paths to RTKLIB while still
validating local files with Unix paths:

```bash
um980-ppk postprocess rover.unc \
  --rover-obs /cygdrive/c/ppk/rover.direct.obs \
  --base-obs /cygdrive/c/ppk/CPAR.obs \
  --nav-file /cygdrive/c/ppk/BRDC00WRD_R_20261400000_01D_MN.rnx \
  --rtklib-dir /cygdrive/c/RTKLIB/bin \
  --rnx2rtkp rnx2rtkp.exe \
  --rtkconf /cygdrive/c/ppk/rtkpost-normal.conf
```

Use `--rtklib-path-style unix` for a native Cygwin/Linux RTKLIB build, or
`--rtklib-path-style windows` to force Windows argument paths.

## Current Limitations

- `OBSVMA` ASCII observation decoding supports a conservative token-based subset.
- `OBSVMB` and `OBSVMCMPB` are detected but not decoded yet; unsupported payloads
  are reported explicitly instead of guessed.
- RINEX output from real UM980 `OBSVMA` payloads currently warns when the
  tracking-status to RINEX signal mapping is incomplete. Those files should not
  be used for production multi-band RTK until the mapping is validated.
- Rover NAV conversion reports `GPSEPHA` records but does not write an empty
  placeholder NAV file. Provide external NAV data before RTKLIB post-processing
  until full GPS ephemeris field mapping is implemented.
- Network download code is explicit and opt-in. Offline mode prints planned URLs.
- `pipeline` currently performs extraction/RINEX generation and optional base
  download planning. It logs a warning and requires `postprocess` for explicit
  RTKLIB execution with rover OBS, base OBS, NAV, and RTK config inputs.
- RTKLIB commands are assembled with argument lists, and generated shell wrappers
  are for reproducibility only.
