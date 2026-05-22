# RINEX Mapping

The signal mapping is explicit and conservative. `OBSVMA` tracking-status bits
are decoded for the documented constellation field and for common GPS, GLONASS,
Galileo, BDS, QZSS, SBAS, and IRNSS signal types. RINEX output writes separate
code/pseudorange (`C`), carrier phase (`L`), Doppler (`D`), and C/N0 (`S`)
observation fields for each decoded signal.

Unknown tracking-status signal types are not guessed. They keep the raw
tracking status in CSV/analysis output and are written with a conservative
fallback RINEX code plus a warning.
