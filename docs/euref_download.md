# EUREF/EPN Download

`download-base` derives the rover time window from NMEA solution timestamps and
then builds provider URLs for overlapping hourly or high-rate station files.

Configured station aliases include CPAR, KUNZ, TUBO, GOPE, GOP7, and GRAZ. A
four-character station code outside this alias map requires `--station-long`.

