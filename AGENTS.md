# Repository Guardrails

- Keep private receiver captures in `examples-private/`; do not commit `.unc`
  logs or generated output products.
- Unsupported UM980 records must be surfaced in warnings and analysis JSON.
  Never silently drop them when they affect downstream products.
- Do not write placeholder NAV files. Empty or header-only NAV inputs must be
  rejected before RTKLIB is invoked.
- RINEX signal mappings must be explicit and tested. When using placeholder
  mappings, warn that the file is not production-ready for multi-band RTK.
- RTKLIB-facing RINEX OBS must contain only standard RINEX 3 system codes. Do
  not emit unknown-system `U` rows or satellites; keep those observations in
  CSV/analysis instead.
- Cygwin support must preserve the distinction between local Unix paths and
  Windows paths passed to Windows RTKLIB executables.
- EUREF base-data selection must log unavailable high-rate or legacy RINEX 2
  products before falling back. Keep legacy test archives and downloaded base
  files local-only.
- Hatanaka conversion must run non-interactively. Use overwrite/timeout
  safeguards for `crx2rnx` so existing `.rnx` files cannot trigger hidden
  prompts or stalled downloads.
