# Data Collection Plan

This plan describes the receiver captures needed to finish and regression-test
the UM980 RTKLIB pipeline. Keep all captures in `examples-private/` or another
private folder. Do not commit `.unc` logs, generated RTKLIB outputs, downloaded
station data, or local RTKLIB source/bin trees.

## Goals

- Validate ASCII, binary, and compressed-binary UM980 raw observation parsing.
- Finish NAV conversion for every logged rover ephemeris family.
- Measure realistic serial link utilisation for NMEA, raw observations, and
  ASCII ephemeris output.
- Exercise RTKLIB-ex post-processing with rover-generated NAV/GNAV/LNAV/SBS and
  EUREF base observations.
- Capture predictable failure cases: missing ephemeris, saturated serial link,
  high-rate base fallback, and unsupported record warnings.

## General Capture Rules

- Record the receiver model, firmware version, antenna, baud rate, port, exact
  init script, date/time, location, and sky conditions beside every `.unc`.
- Use a clear-sky location with as many GPS, GLONASS, Galileo, BeiDou, and SBAS
  satellites as possible. QZSS may not be visible from Central Europe; collect a
  QZSS sample only if you are in a region where QZSS is actually tracked.
- Use `921600` baud for diagnostic captures unless the test explicitly measures
  lower baud saturation. This avoids mistaking serial drops for parser bugs.
- Start each capture at least 2 minutes after applying the init script, unless
  the test is specifically about startup behavior.
- For every capture, save the generated init script and JSON bitrate report.

## Required Init Profiles

Generate commands from the checkout without installing:

```bash
PYTHONPATH=src python -m um980_rtklib_pipeline.cli init generate \
  --port COM1 \
  --baud 921600 \
  --mode rover \
  --raw-format obsvmcmpb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --debug-ascii-ephemeris \
  --include-ion \
  --ion-period 300 \
  --save-config \
  --out examples-private/init-debug-ephem-compressed-2hz.cmd \
  --json examples-private/init-debug-ephem-compressed-2hz.json
```

`--debug-ascii-ephemeris` must generate:

```text
GPSEPHA COM1 300
GLOEPHA COM1 300
GALEPHA COM1 300
BDSEPHA COM1 300
BD3EPHA COM1 300
QZSSEPHA COM1 300
```

The script will warn that this is a debug-only profile and can create large log
files.

For binary ephemeris fixtures, do not use the unsuffixed command names
`GPSEPH`, `GLOEPH`, `GALEPH`, `BDSEPH`, `BD3EPH`, or `QZSSEPH`; tested UM980
firmware rejects them. Generate the corrected binary command script with:

```bash
PYTHONPATH=src python -m um980_rtklib_pipeline.cli init generate \
  --port COM1 \
  --baud 921600 \
  --mode rover \
  --raw-format obsvmcmpb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --ephemeris every=300 \
  --ephemeris-format binary \
  --include-ion \
  --ion-period 300 \
  --save-config \
  --out examples-private/init-binary-ephem-compressed-2hz.cmd \
  --json examples-private/init-binary-ephem-compressed-2hz.json
```

## Capture Matrix

| ID | Purpose | Raw format | Rate | Baud | Duration |
| --- | --- | --- | --- | --- | --- |
| A1 | ASCII observation parser fixture | `obsvma` | 1 Hz | 921600 | 20 min |
| A2 | ASCII observation saturation estimate | `obsvma` | 2 Hz | 921600 | 10 min |
| B1 | Binary observation parser fixture | `obsvmb` | 2 Hz | 921600 | 30 min |
| B2 | Binary high-rate capacity fixture | `obsvmb` | 5 Hz | 921600 | 10 min |
| C1 | Compressed parser fixture | `obsvmcmpb` | 2 Hz | 921600 | 30 min |
| C2 | Compressed high-rate capacity fixture | `obsvmcmpb` | 5 Hz | 921600 | 10 min |
| C3 | Compressed stress fixture | `obsvmcmpb` | 10 Hz | 921600 | 5 min |
| D1 | Low-baud link capacity check | `obsvmcmpb` | 2 Hz | 230400 | 20 min |
| D2 | Expected overload behavior | `obsvma` | 2 Hz | 230400 | 5 min |
| E1 | Minimal rover NAV fixture | `none` | 0 Hz | 921600 | 30 min |
| F1 | Field RTK workflow fixture | `obsvmcmpb` | 2 Hz | 921600 | 45 min |

Enable `--debug-ascii-ephemeris` for A1, B1, C1, E1, and F1. It is optional for
the short capacity tests if file size becomes a problem. Thirty minutes gives at
least six 300-second ephemeris cycles and enough satellite geometry changes to
catch record variants without creating multi-hour debug logs.

For one B1 or C1 repeat, use `--ephemeris every=300 --ephemeris-format binary`
instead of `--debug-ascii-ephemeris` so the binary ephemeris demux and future
conversion path can be tested.

## Base Data

For F1, collect or download matching EUREF base observations for at least two
nearby stations:

- CPAR00CZE
- TUBO00CZE0
- KUNZ00CZE0
- GOP00CZE0 if available

For each station, collect or download:

- RINEX 3 low-rate hourly 30 s observations.
- RINEX 3 high-rate 15 minute 1 s observations.
- RINEX 2 low-rate legacy files where available.
- RINEX 2 high-rate legacy files where available.

Keep the downloaded files private. Record which URLs were unavailable so fallback
logic can be tested with real attempted paths.

## Ephemeris Coverage Checklist

For each debug capture, run:

```bash
PYTHONPATH=src python -m um980_rtklib_pipeline.cli analyze examples-private/<capture>.unc \
  --analysis-json \
  --out-dir examples-private/analysis \
  -v
```

Confirm the analysis JSON reports counts for:

- `GPSEPHA`
- `GLOEPHA`
- `GALEPHA`
- `BDSEPHA`
- `BD3EPHA`
- `QZSSEPHA`
- `GPSEPHB`, `GLOEPHB`, `GALEPHB`, `BDSEPHB`, `BD3EPHB`, and `QZSSEPHB` for
  binary ephemeris fixture repeats
- SBAS message records, if the receiver has a documented SBAS message output

If a constellation is tracked in observations but the matching ephemeris record
is missing, keep the capture anyway and note it. That is useful for warning and
fallback tests.

## Link Capacity Measurements

For each capture, preserve:

- Init JSON report with estimated NMEA/raw/ephemeris bytes per second.
- Actual `.unc` byte size and capture duration.
- Analysis JSON metrics: epoch count, cadence, gaps, observation count, and
  unsupported record counts.

The implementation will compare:

- estimated total payload bytes per second vs actual file bytes per second;
- expected raw epoch size vs observed record lengths;
- ephemeris byte contribution from the 300-second ASCII messages, including
  record counts per visible satellite rather than one line per constellation;
- warning thresholds at 70%, 85%, and 100% 8N1 utilisation.

## Success Criteria

- ASCII, binary, and compressed-binary captures parse without silent record loss.
- Generated RINEX OBS contains code, carrier, Doppler, and SNR for all observed
  L1/L2/L5-capable systems represented in the raw records.
- Rover-generated `.nav`, `.gnav`, `.lnav`, `.cnav`, and `.sbs` files are
  written only when non-empty valid records exist.
- RTKLIB-ex receives every generated rover NAV sidecar plus any explicit external
  NAV files.
- Missing or unsupported records are visible in logs and analysis JSON.
- Serial utilisation estimates are close enough to real file-rate measurements to
  decide whether a profile is safe before field use.
