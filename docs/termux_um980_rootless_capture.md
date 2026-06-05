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

## Passive Capture

Preferred suffix for new UM980/Unicore raw captures is `.unc`:

```sh
PYTHONPATH=src python -m um980_rtklib_pipeline.cli capture-usb \
  --termux-device "/dev/bus/usb/002/002" \
  --duration 20 \
  --out captures/test.unc \
  --analysis-json captures/test.analysis.json \
  --validate \
  --extract-check \
  --expect-mode passive \
  -v
```

The native helper can also be invoked directly through `termux-usb` when the
helper path is executable:

```sh
termux-usb -e "tools/termux/um980-usb-fd --read-passive --duration 20 --out captures/test.unc --analysis-json captures/test.usb.json --verbose" "/dev/bus/usb/002/002"
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
- ASCII NMEA placeholder;
- ASCII Unicore solution/navigation placeholder;
- binary solution placeholder;
- binary raw observations plus solution placeholder;
- mixed ASCII plus binary raw/solution placeholder.

Only profiles containing `# enabled: true` are executable by the hardware
matrix.  At present only `passive.um980` is enabled.  Non-passive profiles are
disabled until exact UM980 runtime-only command syntax is verified safe for this
USB interface.

Safety checks reject active command lines containing persistent or risky tokens
including `SAVECONFIG`, `SAVE`, reset/factory/default commands, flash/NVM,
firmware update, baud/USB/COM changes, and shell metacharacters.  Comments are
ignored before these checks.

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
