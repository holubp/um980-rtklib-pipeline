# Repository Guardrails

- Keep private receiver captures in `examples-private/`; do not commit `.unc`
  logs or generated output products.
- Unsupported UM980 records must be surfaced in warnings and analysis JSON.
  Never silently drop them when they affect downstream products.
- Position-only NMEA outputs must preserve original receiver sentences and
  multi-Hz fractional timestamps. Prefer GGA/GNS over RMC only when choosing one
  best original position sentence for the same timestamp.
- BESTNAV-derived NMEA is a receiver-solution export product, not RTKLIB raw
  input. Keep it separate from live NMEA extraction and never feed BESTNAV into
  RTKLIB estimation in place of OBSVM*/NAV/base observations.
- ION/UTC/TROPINFO records must be counted and preserved in analysis output.
  Do not write them into RINEX NAV headers until the exact family mapping is
  verified against RINEX syntax and RTKLIB parser behavior.
- RTKLIB solution summaries must distinguish RTKLIB `.pos`/`.llh` `Q` values
  from NMEA GGA fix-quality codes; their labels are not interchangeable.
  Cross-reference them explicitly as Q=1/2/4/5 to GGA quality 4/5/2/1.
- Multiple RTKLIB output formats must invoke `rnx2rtkp` separately per format;
  do not fake NMEA or other formats by only changing file suffixes.
- Do not write placeholder NAV files. Empty or header-only NAV inputs must be
  rejected before RTKLIB is invoked.
- RINEX signal mappings must be explicit and tested. When using placeholder
  mappings, warn that the file is not production-ready for multi-band RTK.
- RTKLIB-facing RINEX OBS must contain only standard RINEX 3 system codes. Do
  not emit unknown-system `U` rows or satellites; keep those observations in
  CSV/analysis instead.
- Rover/base RINEX OBS capability diagnostics must be directional. Warn when
  the base lacks rover constellations or frequency bands; do not warn when the
  base has additional constellations, bands, or observation codes.
- Cygwin support must preserve the distinction between local Unix paths and
  Windows paths passed to Windows RTKLIB executables. Windows RTKLIB binaries
  on Cygwin may be PE files without an `.exe` suffix; do not rely only on file
  extension for path-style detection.
- Cygwin wildcard arguments must preserve literal `*` characters for RTKLIB.
  Do not run the full wildcard path through `cygpath`; convert the parent
  directory and append the wildcard filename unchanged.
- Termux executable mirroring must only run under Termux/Android. Never rewrite
  Cygwin or desktop RTKLIB executable paths into `/data/data/com.termux/...`.
- Explicit RTKLIB helper paths, such as `--crx2rnx ./crx2rnx.exe`, must be
  honored before `--rtklib-dir` search logic. Only bare tool names should be
  combined with configured install directories.
- EUREF base-data selection must log unavailable high-rate or legacy RINEX 2
  products before falling back. Keep legacy test archives and downloaded base
  files local-only.
- `--base-resolution high` must attempt and validate high-rate 1 s base files
  before low-rate candidates. Low-rate `01H_30S`/`_30S_` files may only be used
  after explicit fallback handling and must be logged as fallback, never as a
  successful high-rate selection.
- EUREF provider URL templates must be verified against real public directory
  listings and sample files before being added. For BKG high-rate products,
  preflight the directory index so missing station/rate combinations produce
  one clear fallback warning instead of per-file download noise.
- When base observations span multiple files, validate each concrete file but
  pass RTKLIB one base observation argument, usually a staged wildcard, so later
  base files are not misinterpreted as navigation files.
- Treat generated `rnx2rtkp` command-line options as a conservative fallback.
  Prefer explicit RTKLIB-ex configs such as `um980.conf` for quality-sensitive
  comparisons, and keep the selected config visible in verbose logs.
- Automatic satellite QC must remain explicitly opt-in. Always write and keep
  visible the pass-1 `.stat`, derived RTKLIB config, and Markdown/JSON reports
  so run-specific satellite exclusions are reproducible and inspectable.
- Hatanaka conversion must run non-interactively. Use overwrite/timeout
  safeguards for `crx2rnx` so existing `.rnx` files cannot trigger hidden
  prompts or stalled downloads.
- A zero-exit RTKLIB run is not complete unless the requested solution output
  exists. If RTKLIB writes solution data to stdout, preserve it as the requested
  output; otherwise raise a clear error with command and log paths.
