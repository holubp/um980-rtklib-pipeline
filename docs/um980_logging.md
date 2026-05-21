# UM980 Logging

Use `um980-ppk init generate` to produce a command script for the receiver. The
generator estimates serial utilisation for NMEA, raw observations, and
ephemeris bursts. Profiles above the configured baud capacity fail in strict
mode unless `--allow-overload` is supplied.

Compressed binary observations (`OBSVMCMPB`) are usually the safest choice on
low baud rates. ASCII observations (`OBSVMA`) are useful for debugging but can
overload a 230400 baud serial link at multi-Hz rates.

