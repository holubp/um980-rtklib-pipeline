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
- BESTNAV may be used as an explicit solution-track source for CSV/GPX/NMEA
  exports. Preserve native multi-Hz epochs by default and generate standard
  GGA/RMC/VTG when writing BESTNAV-derived `solution.nmea`.
- BESTNAV-derived solution exports must skip non-`SOL_COMPUTED`/`NONE` epochs
  and must sanitize optional text fields such as station IDs before generating
  ASCII NMEA.
- Mixed binary logs can contain arbitrary `$` bytes. NMEA extraction must
  validate ASCII sentence shape and checksum-bearing lines before accepting
  them as receiver NMEA.
- Mixed binary logs can also contain arbitrary Unicore-looking sync or `#`
  bytes. Binary frames must pass Unicore CRC before decoding, and ASCII records
  must pass printable record-shape checks before downstream parsers see them.
- Unicore ASCII record checksums use the receiver/NovAtel-style CRC32:
  `zlib.crc32(body, 0xFFFFFFFF) ^ 0xFFFFFFFF` over bytes after `#` and before
  `*`. Do not replace it with Python's default `binascii.crc32(body)`.
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
- Recorded real-time base RTCM is an alternative base source, not an archive
  fallback. `--base-rtcm` must be converted with RTKLIB `convbin -r rtcm3` and
  must not be combined silently with `--download-base`.
- Base-candidate advisory must remain non-destructive. It may rank stations,
  refresh/cache station catalogues, and probe archive availability, but it must
  not automatically change the normal pipeline's base selection.
- Archive probing in advisory mode must stay lightweight by default. Prefer
  cached metadata, directory listings, or HTTP `HEAD` checks; do not download
  full observation bodies for `--download-headers-only`.
- Optimizer execution must be explicitly bounded. Default to planning/dry-run,
  require an execution flag for subprocess runs, preserve per-run commands and
  logs, and enforce `--max-variants`/`--max-runs`.
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
- RTKLIB trace diagnostics must remain opt-in because trace files can be huge.
  Generate traces only through explicit quality-trace modes, parse them as
  bounded streaming summaries, and delete temporary traces by default.
- RTKLIB generated trace discovery must prefer `<solution-output>.trace` over
  small trace files in an isolated temporary cwd. Temporary cleanup reports must
  reflect actual filesystem deletion, not inferred policy.
- Trace diagnostics may produce global counters, but QC confidence may only use
  trace evidence after timestamp alignment to solution epochs. Keep
  trace-specific reasons separate from STAT reasons.
- The standalone RTK quality command is `quality`; `quality-analyze` is only a
  deprecated compatibility alias. The pipeline flag remains `--quality-analyze`.
- Never delete RTKLIB `.stat` files by default. `.stat` cleanup is only allowed
  after successful quality extraction for files generated by the current
  pipeline/postprocess run; standalone user-supplied `.stat` files must be kept.
- CLI diagnostics for repeated single-value options must preserve argparse
  last-value-wins behavior while warning in verbose/debug logs about previous
  values. RTK QC confidence must stay separate from raw RTK state; incomplete
  residual/slip alignment should produce unknown/limited confidence, not a hard
  false-fix conclusion.
- Large RTKLIB `.stat` analysis must remain indexed and bounded: never perform
  a linear nearest-solution-epoch scan inside loops over `$SAT` rows or raw slip
  flags. Deduplicate slip evidence first, align unique STAT epochs with a
  bisect-backed index, and keep rerun scripts/commands available for manual
  reproduction of RTKLIB and quality steps.
