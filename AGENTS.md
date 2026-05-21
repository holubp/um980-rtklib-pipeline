# Repository Guardrails

- Keep private receiver captures in `examples-private/`; do not commit `.unc`
  logs or generated output products.
- Unsupported UM980 records must be surfaced in warnings and analysis JSON.
  Never silently drop them when they affect downstream products.
- Do not write placeholder NAV files. Empty or header-only NAV inputs must be
  rejected before RTKLIB is invoked.
- RINEX signal mappings must be explicit and tested. When using placeholder
  mappings, warn that the file is not production-ready for multi-band RTK.
- Cygwin support must preserve the distinction between local Unix paths and
  Windows paths passed to Windows RTKLIB executables.

