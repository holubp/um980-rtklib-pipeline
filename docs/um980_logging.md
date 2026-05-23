# UM980 Logging

Use `um980-ppk init generate` to produce a command script for the receiver. The
generator estimates serial utilisation for NMEA, raw observations, and
ephemeris bursts. Profiles above the configured baud capacity fail in strict
mode unless `--allow-overload` is supplied.

Binary observations (`OBSVMB`) and compressed binary observations (`OBSVMCMPB`)
are the supported compact Python-decoded raw observation formats. ASCII
observations (`OBSVMA`) are useful for debugging but can overload a 230400 baud
serial link at multi-Hz rates.

Use `--solution-hz` to set the primary NMEA solution cadence for `GNGGA` and
`GNRMC`. It accepts arbitrary positive frequencies, including 1, 2, 4, 5, 10,
and 20 Hz. Individual messages can still be overridden with repeated `--nmea`
arguments.

Use `--debug-ascii-ephemeris` only for short diagnostic captures. It emits all
ASCII ephemeris messages every 300 seconds:

```text
GPSEPHA COM1 300
GLOEPHA COM1 300
GALEPHA COM1 300
BDSEPHA COM1 300
BD3EPHA COM1 300
QZSSEPHA COM1 300
```

This deliberately creates larger `.unc` files so rover-side NAV/GNAV/LNAV/SBS
parsers can be tested. The init generator prints a warning in the generated
script and includes the average ASCII ephemeris payload in the serial-line
utilisation estimate.

For binary ephemeris collection, use `--ephemeris-format binary` with
`--ephemeris every=300`. The generator emits:

```text
GPSEPHB COM1 300
GLOEPHB COM1 300
GALEPHB COM1 300
BDSEPHB COM1 300
BD3EPHB COM1 300
QZSSEPHB COM1 300
```

The unsuffixed command names `GPSEPH`, `GLOEPH`, `GALEPH`, `BDSEPH`,
`BD3EPH`, and `QZSSEPH` are invalid for the tested receiver firmware and are
expected to return `PARSING FAILED NO MATCHING FUNC`.

## Convbin Trial Capture

RTKLIB-ex `convbin -r unicore` reads Unicore binary frames, not UM980/UM890
ASCII observation logs. For a convbin trial, configure the receiver to log a
binary-only stream on the captured port:

```bash
um980-ppk init generate \
  --port COM1 \
  --baud 921600 \
  --mode rover \
  --raw-format obsvmb \
  --raw-hz 1 \
  --nmea-preset none \
  --ephemeris every=300 \
  --ephemeris-format binary \
  --save-config \
  --out um890-convbin-unicore.cmd
```

The important properties are:

- use `OBSVMB`, because the checked RTKLIB-ex Unicore decoder handles message
  ID 12;
- use binary ephemeris commands with the `B` suffix, because `convbin` decodes
  the binary ephemeris frame IDs;
- avoid `OBSVMA`, ASCII ephemeris debug output, NMEA solution messages, PPP
  diagnostics, ionosphere text messages, and command-response chatter on the
  same stream;
- start the field logger after the init commands have completed, or capture
  init traffic separately from the `.unc` file passed to `convbin`.

The generated command body should look like this:

```text
CONFIG COM1 921600
MODE ROVER
OBSVMB COM1 1
GPSEPHB COM1 300
GLOEPHB COM1 300
GALEPHB COM1 300
BDSEPHB COM1 300
BD3EPHB COM1 300
QZSSEPHB COM1 300
SAVECONFIG
```

Run RTKLIB-ex conversion with explicit Unicore input format and all navigation
sidecars:

```bash
convbin -r unicore -v 3.04 -od -os -oi -ot \
  -o rover.obs \
  -n rover.nav \
  -g rover.gnav \
  -l rover.lnav \
  -b rover.cnav \
  -s rover.sbs \
  rover.unc
```

If a recorder must include initialization chatter or NMEA text, create a
convbin trial file by copying only valid Unicore binary frames into a separate
`.unc`: scan for sync bytes `AA 44 B5`, validate the 24-byte header, use the
payload length from bytes 6..7, keep the frame CRC, and discard all text between
frames. This cleanup must not invent frames or NAV files; if no valid binary
ephemeris frames remain, the convbin run should be treated as missing NAV data.

Do not use `OBSVMCMPB` for the first convbin path. This project's Python parser
decodes compressed observation ID 138, but the checked RTKLIB-ex `convbin`
source dispatches `OBSVMB`, `GPSEPHB`, `GLOEPHB`, `GALEPHB`, `BDSEPHB`,
`QZSSEPHB`, and `IRNSSEPHB`; it does not dispatch compressed observation ID 138.
`BD3EPHB` is useful to log for this project's parser work, but the checked
RTKLIB-ex decoder does not currently dispatch BDS-3 ephemeris ID 2999.

## PPP Diagnostics

When `--ppp` is enabled, the generated script includes these defaults unless
the user overrides them:

```text
CONFIG PPP TIMEOUT 120
CONFIG PPP CONVERGE 15 30
```

`--ppp-timeout` changes the timeout in seconds. `--ppp-converge` accepts two
comma-separated thresholds, for example `--ppp-converge 15,30`.

`--include-tropinfo` requires PPP and emits one selected diagnostic format once
at start and again whenever the receiver reports a change. The default
`--diagnostic-format ascii` emits:

```text
TROPINFOA ONCE
TROPINFOA ONCHANGED
```

Use `--diagnostic-format binary` to emit `TROPINFOB ONCE` and
`TROPINFOB ONCHANGED` instead.

Ionosphere parameters can be emitted with `--ion gps,bds,bd3,gal` or all at
once with `--include-ion`. Each selected family is emitted as `ONCHANGED` using
the selected diagnostic format; `ONCE` is not valid for these messages. The
broadcast ionosphere models normally change slowly, usually with navigation-data
updates rather than epoch-by-epoch receiver motion, but a repeat cycle is useful
for partial logs and missed change events. Use `--ion-period 300` for diagnostic
captures where each log slice should contain fresh ionosphere parameters.

```text
GPSIONA ONCHANGED
BDSIONA ONCHANGED
BD3IONA ONCHANGED
GALIONA ONCHANGED
```

With `--ion-period 300`, the generator also emits:

```text
GPSIONA 300
BDSIONA 300
BD3IONA 300
GALIONA 300
```

With `--diagnostic-format binary`, the same commands use `GPSIONB`, `BDSIONB`,
`BD3IONB`, and `GALIONB`.

The ephemeris estimate is per satellite record, not one line per constellation.
GPS and GLONASS ASCII line sizes are measured from UM980 private captures;
Galileo, BeiDou, BeiDou-3, and QZSS ASCII sizes use conservative estimates
derived from RTKLIB-ex Unicore binary payload sizes with ASCII expansion. Binary
ephemeris estimates use the fixed UM980 binary header, payload structures, and
CRC per satellite record.
