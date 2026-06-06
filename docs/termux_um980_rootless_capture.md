# Rootless Termux UM980 USB Capture

This workflow captures UM980/Unicore receiver bytes on Android/Termux without
root.  It is intended for the Redmi Pad Pro OTG setup where Android grants USB
permission and Termux passes an already-authorised file descriptor to a native
helper.

## Architecture

```text
Android USB permission
-> termux-usb
-> authorised USB file descriptor
-> tools/termux/um980-usb-fd
-> libusb_wrap_sys_device()
-> descriptor probe / endpoint selection / bulk read-write
-> raw .unc capture
-> existing um980-ppk extract/validation pipeline
```

No `su`, `adb root`, udev rules, direct chmod of `/dev/bus/usb`, direct open of
`/dev/bus/usb/...`, or pyserial-on-device-path access is used.

`lsusb` may fail in this rootless Termux environment with messages such as
`unable to initialize usb spec` or `unable to initialize libusb: -1`.  That is
expected and is not part of this workflow.

## Prerequisites

Install rootless Termux packages:

```sh
pkg install -y termux-api clang make pkg-config libusb jq python git
```

Grant Android USB permission:

```sh
termux-usb -l
termux-usb -r "/dev/bus/usb/002/002"
```

The exact device path can change after reconnecting the receiver.

## Build And Probe

Build the native helper:

```sh
/data/data/com.termux/files/usr/bin/sh tools/termux/build-um980-usb-fd.sh
```

Android shared storage may be mounted `noexec`.  The Python wrapper mirrors the
native helper into Termux-private executable temporary storage before invoking
`termux-usb`; direct execution from shared storage may fail with `Permission
denied`.

Descriptor probe:

```sh
termux-usb -e "tools/termux/um980-usb-fd --probe --verbose" "/dev/bus/usb/002/002"
```

If shared storage is `noexec`, prefer the integrated CLI probe because it uses
the mirrored helper:

```sh
PYTHONPATH=src python -m um980_rtklib_pipeline.cli capture-usb \
  --termux-device "/dev/bus/usb/002/002" \
  --probe \
  -v
```

On the tested Redmi Pad Pro setup the USB adapter reports as an FTDI-style
bridge (`idVendor=0x0403`, `idProduct=0x6015`) with vendor-specific bulk
endpoints.  For that interface the helper must set the serial line rate and
strip the FTDI two-byte status header from incoming packets.  Use
`--serial-baud 230400` for the normal UM980 `COM1 230400` runtime profile, or
let the bandwidth matrix set the requested baud for each cell.

## Passive Capture

Preferred suffix for new UM980/Unicore raw captures is `.unc`:

```sh
PYTHONPATH=src python -m um980_rtklib_pipeline.cli capture-usb \
  --termux-device "/dev/bus/usb/002/002" \
  --duration 20 \
  --out captures/test.unc \
  --analysis-json captures/test.analysis.json \
  --serial-baud 230400 \
  --validate \
  --extract-check \
  --expect-mode passive \
  -v
```

When a runtime profile is sent before capture, use a short post-profile drain so
bytes already buffered under the previous receiver output configuration do not
appear at the start of the saved `.unc` file:

```sh
PYTHONPATH=src python -m um980_rtklib_pipeline.cli capture-usb \
  --termux-device "/dev/bus/usb/002/002" \
  --duration 20 \
  --profile tools/um980_profiles/runtime/binary_rawobs_solution.um980 \
  --discard-after-profile-ms 2000 \
  --out captures/profiled-clean.unc \
  --analysis-json captures/profiled-clean.analysis.json \
  --serial-baud 230400 \
  --validate \
  --extract-check \
  --expect-mode binary \
  -v
```

`--capture-after-profile-delay-ms` only sleeps.  `--discard-after-profile-ms`
actively reads and discards startup/profile-transition output before the capture
file is opened.  `--discard-after-profile-bytes` can be added when a device has
a larger backlog to drain.

The native helper can also be invoked directly through `termux-usb` when the
helper path is executable:

```sh
termux-usb -e "tools/termux/um980-usb-fd --read-passive --duration 20 --serial-baud 230400 --out captures/test.unc --analysis-json captures/test.usb.json --verbose" "/dev/bus/usb/002/002"
```

## Suffix Policy

- `.unc` is the preferred suffix for new UM980/Unicore raw captures.
- `.ubx` is accepted as a legacy suffix from earlier u-blox-style naming.
- Both are raw stream containers.
- The parser validates content and does not infer protocol family from suffix.

## Runtime Profiles

Reviewed profiles live under `tools/um980_profiles/runtime/`.

Current profiles cover:

- passive/current stream;
- ASCII NMEA low/medium-load runtime profiles;
- ASCII Unicore solution/navigation runtime profiles;
- binary solution runtime profiles;
- binary raw observations plus binary ephemerides;
- mixed ASCII plus binary raw/solution profiles.

Only profiles containing `# enabled: true` are executable by the hardware
matrix.  Non-passive profiles are enabled only where the runtime-only UM980
command syntax has been reviewed.  Stress profiles and unverified syntax remain
disabled and are reported as `NOT_TESTED`.

Safety checks reject active command lines containing persistent or risky tokens
including `SAVECONFIG`, `SAVE`, reset/factory/default commands, flash/NVM,
firmware update, USB mode changes, and shell metacharacters.  Comments are
ignored before these checks.  The normal reviewed runtime profiles use COM1 and
baud commands only when the profile explicitly declares
`allow_reviewed_port_commands: true`; this remains non-persistent and does not
save receiver configuration.

No persistent receiver configuration is saved.  If active runtime profiles are
enabled later, power-cycle the UM980 to restore the saved receiver output
configuration.  The matrix does not send reset or reboot commands.

## Hardware Matrix

Run the repeatable hardware matrix only when the real UM980 is attached:

```sh
export UM980_HW_TEST=1
export UM980_TERMUX_DEVICE="/dev/bus/usb/002/002"
/data/data/com.termux/files/usr/bin/sh scripts/termux_um980_capture_matrix.sh
```

Optional durations:

```sh
export UM980_CAPTURE_DURATION=20
export UM980_CAPTURE_LONG_DURATION=60
```

The matrix writes to `captures/termux-YYYYMMDD-HHMMSS/`:

- probe log;
- passive `.unc` capture;
- per-case logs and analysis JSON;
- `hw_matrix_summary.md`;
- `hw_matrix_summary.json`.

The matrix stops before runtime profiles if passive capture fails.  Disabled
profiles are reported as skipped.  Captures and logs are ignored by Git.

## Bandwidth Safety Matrix

For practical baudrate and ASCII/binary recommendations, use the staged
bandwidth matrix:

```sh
export UM980_HW_TEST=1
export UM980_TERMUX_DEVICE="/dev/bus/usb/002/002"
/data/data/com.termux/files/usr/bin/sh scripts/termux_um980_bandwidth_matrix.sh
```

Default stage is `smoke`: all enabled non-stress profiles are tried once at
115200, 230400, 460800, and 921600 baud for a short capture.  The generated
report is written under `captures/bandwidth-*/bandwidth_recommendations.md`.

Longer evidence collection is explicit:

```sh
export UM980_BANDWIDTH_STAGE=evidence
export UM980_BANDWIDTH_DURATION=120
export UM980_BANDWIDTH_REPEAT=3
/data/data/com.termux/files/usr/bin/sh scripts/termux_um980_bandwidth_matrix.sh
```

Boundary confirmation is explicit as well:

```sh
export UM980_BANDWIDTH_STAGE=boundary
/data/data/com.termux/files/usr/bin/sh scripts/termux_um980_bandwidth_matrix.sh
```

Stress profiles are skipped unless requested:

```sh
export UM980_BANDWIDTH_STRESS=1
```

The report labels per-profile results as `PROVISIONALLY_SAFE`, `MARGINAL`,
`UNSAFE`, `INCONCLUSIVE`, or `NOT_TESTED`.  It only uses `SAFE` when repeated
and boundary evidence is available.  Smoke-only results must be treated as
early evidence, not final recommendations.

Each cell now includes timing-completeness metrics, not only bytes per second
and parser success.  For periodic messages with supported receiver timestamps
the matrix records observed Hz, missing epochs, duplicate epochs, robust
receiver-time gaps, and a timing confidence/status.  A profile cannot be
recommended safe when key periodic timing is failed or unsupported.

Generated outputs include:

- `bandwidth_matrix_summary.json` with full per-message timing details;
- `bandwidth_matrix_rows.csv` with bounded key timing columns for comparisons;
- `bandwidth_recommendations.md` with parser, throughput, and timing status.

Committed bandwidth profiles live in
`tools/um980_profiles/runtime/bandwidth/`.  They are runtime-only and contain no
`SAVECONFIG`.  Profiles that require stress testing or unverified syntax remain
disabled and appear as `NOT_TESTED`.

The matrix records the requested baud, whether FTDI line coding was attempted,
whether it succeeded, measured bytes per second, and the measured load relative
to an equivalent UART 8N1 payload budget.  This is evidence for whether the USB
COM line-coding value matters on the observed interface; it is not assumed in
advance.

### PPP/HAS Timing Profiles

Curated PPP/HAS runtime profiles live in
`tools/um980_profiles/runtime/ppp_has/`.  They declare exact timing
expectations for the baseline family:

- `GNGGA`, `GNRMC`, and `GNGST` at 20 Hz;
- `GNGSV` as grouped 1 Hz bursts;
- `GNGSA`, `GPGLL`, and `GPGNS` at 1 Hz;
- `GPGRS` at 1/30 Hz;
- `PPPNAVA`/`ADRNAVA` or `PPPNAVB`/`ADRNAVB` at 0.1 Hz;
- `TROPINFO*` and `GPSION*` as `ONCHANGED` event-driven messages.

Run a PPP/HAS smoke matrix with:

```sh
export UM980_HW_TEST=1
export UM980_TERMUX_DEVICE="/dev/bus/usb/002/002"
export UM980_BANDWIDTH_PROFILE_FAMILY=ppp_has
export UM980_BANDWIDTH_STAGE=smoke
export UM980_BANDWIDTH_DURATION=30
export UM980_BANDWIDTH_REPEAT=1
/data/data/com.termux/files/usr/bin/sh scripts/termux_um980_bandwidth_matrix.sh
```

Event-driven messages are reported when observed, but absence is not counted as
timing loss.  Binary PPP/HAS recommendations remain inconclusive unless the
stream parser recognises the relevant binary message IDs and can extract
receiver timestamps.

## Limitations

- Some UM980 USB interfaces may require `--interface`, `--altsetting`,
  `--ep-in`, or `--ep-out` overrides based on probe output.
- If no safe OUT endpoint exists, runtime profile sending is unsupported for
  that interface; passive capture remains usable.
- Binary validation is structural when the current parser cannot identify every
  message family.
- GNSS fix is not required for parser/extract validation.
- High-rate raw-observation profiles should remain disabled unless known safe
  and explicitly reviewed.
