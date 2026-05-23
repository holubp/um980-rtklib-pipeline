# RINEX Mapping

The signal mapping is explicit and conservative. `OBSVMA`, documented binary
`OBSVMB`, and compressed binary `OBSVMCMPB` tracking-status bits are decoded for
the documented constellation field and for common GPS, GLONASS, Galileo, BDS,
QZSS, SBAS, and IRNSS signal types. RINEX output writes separate
code/pseudorange (`C`), carrier phase (`L`), Doppler (`D`), and C/N0 (`S`)
observation fields for each decoded signal.

Unknown tracking-status signal types are not guessed. They keep the raw
tracking status in CSV/analysis output and are written with a conservative
fallback RINEX code plus a warning.

`--rinex-compat convbin` applies a stricter writer profile for RTKLIB
interoperability. It orders observation types by signal as RTKLIB `convbin`
does, emits RTKLIB-readable extended single-line RINEX 3 satellite records, and
excludes observations with unknown satellite systems, implausible GPS weeks,
missing pseudorange, or receiver time status other than `FINE`.

Rover navigation extraction supports the documented UM980 ASCII and binary
ephemeris records for GPS (`GPSEPHA`/`GPSEPHB`), GLONASS
(`GLOEPHA`/`GLOEPHB`), Galileo (`GALEPHA`/`GALEPHB`), BeiDou-2
(`BDSEPHA`/`BDSEPHB`), BeiDou-3 binary (`BD3EPHB`), QZSS
(`QZSSEPHA`/`QZSSEPHB`), and IRNSS binary (`IRNSSEPHB`). The pipeline writes
RTKLIB-facing sidecars as `.nav`, `.gnav`, `.lnav`, `.cnav`, `.inav`, and
`.sbs` when valid records are present. BDS-3 frequency variants with the same
satellite and epoch are collapsed for RTKLIB RINEX NAV compatibility, and the
analysis JSON/logs report that explicitly.
