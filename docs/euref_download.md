# EUREF/EPN Download

`download-base` derives the rover time window from NMEA solution timestamps.
The integrated `pipeline` command uses the generated rover RINEX observation
span. Both paths build provider URLs for every hourly or high-rate station file
that overlaps or touches the recorded rover interval.

Configured station aliases include CPAR, KUNZ, TUBO, GOPE, GOP7, and GRAZ. A
four-character station code outside this alias map requires `--station-long`.

`postprocess --base-station` resolves the same station aliases to current
EPN/EUREF ETRF2000 ECEF coordinates from the EPN coordinate service:

```text
https://www.epncb.oma.be/_productsservices/coordinates/crd4station.php?station=<MARKER>
```

Use `--base-position-cache-dir` to cache the downloaded coordinate page for
repeatable/offline reruns. If EPN lookup is not requested or fails in auto mode,
the pipeline falls back to the base RINEX `APPROX POSITION XYZ` header. Explicit
`--base-ecef X Y Z` and `--base-llh LAT LON HEIGHT` always take precedence.

BKG EUREF NRT and high-rate downloads use the current BKG GDC public archive
paths. HTTPS under `https://igs.bkg.bund.de/root_ftp/` is planned first because
BKG documents it as the preferred download access; anonymous FTP under
`ftp://igs-ftp.bkg.bund.de/` is planned as a fallback mirror for the same BKG
directory layout. High-rate BKG RINEX 3 planning uses verified `_S_` 15-minute
stream-derived names. For example, on 2026 day 143 the BKG HTTPS and FTP
mirrors both contain `CPAR00CZE_S_20261430530_15M_01S_MO.crx.gz`, while TUBO
is absent from that high-rate interval and must fall back to low-rate data.
BKG URLs are preflighted against the public directory index before download, so
missing station/rate combinations are reported once instead of producing one
download warning per missing 15-minute file and mirror.
BEV NRT downloads keep using the BEV FTP service for hourly
Hatanaka-compressed `.crx.gz` products.

## Rate and RINEX Version Selection

Use `--base-resolution low` for hourly 30 s files and `--base-resolution high`
for 15 minute 1 s files:

```bash
um980-ppk download-base rover.unc --station CPAR --base-resolution high
```

High-rate EUREF files are not always published for every station and interval.
When high-rate data is requested, the CLI logs the failed provider/URLs and
falls back to low-rate data by default. Use `--no-base-fallback` when a missing
high-rate file should be a hard failure.

Downloads are cache-first. Existing source archives, decompressed files, and
converted `.rnx`/`.YYo` products in `--base-dir` or `--cache-dir` are reused;
only missing products are downloaded. Use `--force-download` to refresh planned
source archives from the provider.

By default, `--time-margin` is `0`: only base products overlapping or touching
the recorded interval are requested. Set `--time-margin SECONDS` only when you
intentionally want neighboring products included for troubleshooting or
receiver-clock uncertainty.

RINEX 3 is the default source format. `--base-rinex-version 2` switches URL
planning to compact RINEX 2/Hatanaka names. Low-rate BEV v2 names follow the
archived helper script pattern, for example `tubo138h.26d.gz`; high-rate BKG v2
names follow `tubo138h15.26d.Z`. `.Z` compression is decompressed with `gzip`,
and Hatanaka `.crx`, `.d`, and `.YYd` files require `crx2rnx`. The CLI
preflights this before extracting any downloaded Hatanaka files, so a missing
converter fails without leaving a partially extracted cache. `crx2rnx` is
resolved from `--crx2rnx`, `--rtklib-dir`, the current directory,
`~/RTKLIB-ex-bin/bin`, `build-tools/RTKLIB-ex-bin/bin`, or PATH. Both
`crx2rnx` and `crx2rnx.exe` are considered when the converter is discovered
automatically. Conversion is run non-interactively with force-overwrite and
timeout safeguards, so an existing `.rnx` file cannot hide an overwrite prompt
inside the pipeline.
`--base-rinex-version auto` tries RINEX 3 before RINEX 2.

The legacy `pfa2` short code appears in the archived v2 helper script and can be
used for RINEX 2 URL planning. No current EPN RINEX 3 long marker was found for
that code, so RINEX 3 use requires an explicit `--station-long` value.
