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

Header-only RINEX NAV files are rejected. `GPSEPHA` records may be counted and
reported, but no rover NAV file should be written until the ephemeris fields are
mapped into valid RINEX records.

## RTKLIB Paths

Validate local files with local Python paths. Convert only the argument strings
passed to RTKLIB tools. Cygwin with Windows `.exe` tools usually requires
Windows-style input paths, while native Linux/Cygwin builds require Unix paths.
Keep `--rtklib-path-style` behavior covered by tests.

