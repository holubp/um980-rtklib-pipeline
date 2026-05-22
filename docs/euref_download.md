# EUREF/EPN Download

`download-base` derives the rover time window from NMEA solution timestamps and
then builds provider URLs for overlapping hourly or high-rate station files.

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

BKG EUREF NRT and high-rate downloads use HTTPS `root_ftp` URLs. BEV NRT
downloads still use the BEV FTP service.

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

RINEX 3 is the default source format. `--base-rinex-version 2` switches URL
planning to compact RINEX 2/Hatanaka names. Low-rate BEV v2 names follow the
archived helper script pattern, for example `tubo138h.26d.gz`; high-rate BKG v2
names follow `tubo138h15.26d.Z`. `.Z` compression is decompressed with `gzip`,
and Hatanaka `.d`/`.YYd` files still require `--crx2rnx`.
`--base-rinex-version auto` tries RINEX 3 before RINEX 2.

The legacy `pfa2` short code appears in the archived v2 helper script and can be
used for RINEX 2 URL planning. No current EPN RINEX 3 long marker was found for
that code, so RINEX 3 use requires an explicit `--station-long` value.
