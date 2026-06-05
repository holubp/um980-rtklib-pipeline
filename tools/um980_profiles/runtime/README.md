# UM980 Runtime Capture Profiles

These files are reviewed inputs for `um980-ppk capture-usb` and the Termux
hardware matrix.  They are line-oriented receiver runtime profiles, not saved
configuration.

Safety rules:

- Active profiles must contain `# enabled: true`.
- Comments and blank lines are ignored.
- Active command lines are sent with CRLF.
- `SAVECONFIG`, reset, flash/NVM, baud/USB/COM, factory/default, update,
  erase/format, and shell metacharacters are rejected by both Python and the
  native helper.
- No profile in this directory saves persistent receiver configuration.

Current state:

- `passive.um980` is enabled and sends no commands.
- ASCII, binary, raw-observation, and mixed profiles are placeholders and remain
  disabled until exact UM980 runtime-only command syntax is verified on the real
  hardware interface.

Power-cycle the UM980 after active runtime tests if you need to restore the
saved receiver output configuration.  No reset or reboot command is sent by the
matrix script.
