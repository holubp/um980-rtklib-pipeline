# UM980 RTKLIB Pipeline — Codex Implementation Specification

Repository: `https://github.com/holubp/um980-rtklib-pipeline`  
Preferred Python package: `um980_rtklib_pipeline`  
Preferred CLI command: `um980-ppk`  
Alternative CLI command: `um980-rtklib-pipeline`  
Target language: Python 3.11+  
Primary operating environments: Linux, Cygwin/Windows, Termux where practical  
Primary receiver: Unicore UM980 / Nebulas IV protocol family  
Primary RTK engine: RTKLIB / RTKLIB demo5, especially `rnx2rtkp`

---

## 1. Purpose

Implement a complete Python workflow for Unicore UM980 rover logs and RTKLIB post-processing.

The workflow shall:

1. Generate UM980 initialisation command scripts from a user-specified logging profile.
2. Estimate expected serial data rate and warn when the requested logging profile is likely to overload the serial link.
3. Read one mixed UM980 serial log containing:
   - NMEA records,
   - Unicore ASCII records,
   - Unicore binary records,
   - Unicore compressed binary records,
   - possible binary garbage/noise at connection initialisation.
4. Extract the receiver’s internal solution to:
   - clean NMEA,
   - solution-only NMEA,
   - CSV,
   - GPX with rich UM980 extensions.
5. Extract and decode raw observations from:
   - ASCII `OBSVMA`,
   - binary `OBSVMB`,
   - compressed binary `OBSVMCMPB`.
6. Convert decoded raw observations into RTKLIB-compatible RINEX observation files.
7. Extract rover-side broadcast ephemerides where present, at minimum:
   - `GPSEPHA` → RINEX GPS NAV.
   but optimally for all the constellations supported by UM980 and RTKLIB
8. Download EUREF/EPN base-station observation data by user-specified station code and rover time window.
9. Resolve navigation data from:
   - explicit user NAV files,
   - base-station or downloaded broadcast navigation,
   - base RTCM3 conversion outputs,
   - rover-extracted ephemerides,
   using base/downloaded navigation preferentially and rover navigation as fallback.
10. Run RTKLIB post-processing via `rnx2rtkp`.
11. Produce verbose quality metrics and machine-readable JSON analysis.

---

## 2. Non-goals and constraints

The project shall not:

1. Depend on RTKLIB `convbin` for rover UM980 `.unc` conversion. Many RTKLIB builds do not include Unicore support or do not recognise `-r unc`.
2. Fabricate NAV data from raw observations. Observations contain measurements; broadcast navigation must come from ephemeris messages, downloaded broadcast NAV, base-derived NAV, SP3/CLK, or user-supplied files.
3. Delete raw input files or downloaded cache files by default.
4. Silently pass unresolved shell wildcards to `rnx2rtkp`.
5. Silently ignore missing navigation for constellations present in rover observations.
6. Use shell string concatenation for subprocess invocation where a list of arguments can be used.
7. Require an online connection for local extraction/conversion; online access is required only for explicit download steps.

---

## 3. High-level workflow

Typical end-to-end pipeline:

```text
UM980 init generator
  -> receiver command file
  -> UM980 mixed serial log

UM980 mixed log
  -> byte-level demultiplexer
  -> clean NMEA
  -> internal solution CSV/NMEA/GPX
  -> decoded raw observations CSV
  -> direct RINEX rover OBS
  -> rover-extracted NAV where available

Rover time window
  -> EUREF/EPN base OBS download
  -> decompression and Hatanaka conversion
  -> NAV download or NAV source resolution

Rover RINEX OBS + base RINEX OBS + NAV/SP3/CLK
  -> rnx2rtkp
  -> RTK/float/fix solution
```

---

## 4. Repository structure

Use this structure unless there is a strong reason to change it:

```text
um980-rtklib-pipeline/
  pyproject.toml
  README.md
  LICENSE
  CHANGELOG.md
  docs/
    um980_logging.md
    rtklib_pipeline.md
    euref_download.md
    rinex_mapping.md
  examples/
    um980-init-230400-2hz-binary.yaml
    um980-init-230400-2hz-compressed.yaml
    rtkpost-normal.conf.example
  src/
    um980_rtklib_pipeline/
      __init__.py
      cli.py
      initgen.py
      bitrate.py
      stream.py
      nmea.py
      unicore_ascii.py
      unicore_binary.py
      obs_decode.py
      solution.py
      rinex_obs.py
      rinex_nav.py
      euref.py
      nav_resolver.py
      rtklib.py
      quality.py
      timeutil.py
      files.py
      config.py
      logging_config.py
  tests/
    test_initgen.py
    test_bitrate.py
    test_stream_demux.py
    test_nmea.py
    test_unicore_ascii.py
    test_unicore_binary.py
    test_obsvma_decode.py
    test_obsvmb_decode.py
    test_obsvmcmpb_decode.py
    test_rinex_obs.py
    test_rinex_nav_gps.py
    test_euref_url_generation.py
    test_nav_resolver.py
    test_rtklib_command.py
    fixtures/
      README.md
```

`src/` layout is preferred.

examples-private can be used for development and testing locally, but must not be committed into the github.

---

## 5. Codex agent specification

The project shall be implemented through a multi-agent Codex workflow. Agents are logical roles; they may be separate Codex sessions or explicitly separated task phases in one Codex session. Each agent must leave clear notes in commits, issue comments, or implementation notes.

### 5.1 Agent A — Product/Workflow Architect

Role:

- Maintain the end-to-end workflow design.
- Ensure CLI commands are coherent and user-facing behaviour is predictable.
- Keep modules decoupled and testable.
- Ensure all downstream modules know which files they consume and produce.

Responsibilities:

- Define CLI command hierarchy.
- Define file naming conventions.
- Define configuration file format.
- Define pipeline orchestration.
- Maintain this specification as implementation evolves.

Quality gates:

- Every command must have documented inputs, outputs, and failure modes.
- Every generated file must have a deterministic name unless user overrides it.
- The pipeline must be runnable step-by-step as well as end-to-end.

### 5.2 Agent B — UM980 Protocol Agent

Role:

- Implement and verify UM980/Nebulas IV parsing and command generation.

Responsibilities:

- Implement stream demultiplexer for NMEA, Unicore ASCII, binary and compressed binary.
- Implement `OBSVMA`, `OBSVMB`, and `OBSVMCMPB` decoding.
- Implement `GPSEPHA` to RINEX NAV conversion.
- Add stubs and tests for future `GLOEPHA`, `GALEPHA`, `BDSEPHA`, `BD3EPHA`, `QZSSEPHA`.
- Implement UM980 initialisation command generator.

Quality gates:

- Parser must recover from garbage bytes and malformed records.
- Parser must never abort the whole file because one record is malformed.
- Binary parser must resynchronise on `AA 44 B5`.
- Decoded observation fields must include raw tracking status for debugging.
- Unsupported binary/compressed fields must be explicitly marked unsupported, never guessed.

### 5.3 Agent C — RINEX/RTKLIB Agent

Role:

- Ensure generated RINEX files and RTKLIB invocations are technically valid.

Responsibilities:

- Implement RINEX 3.04 observation writer.
- Implement RINEX GPS NAV writer for rover `GPSEPHA`.
- Implement `rnx2rtkp` command generation and execution.
- Validate file classification before calling RTKLIB.
- Support optional base RTCM3 conversion with `convbin -r rtcm3`.

Quality gates:

- No call to `rnx2rtkp` without at least rover OBS, base OBS, and NAV/SP3/CLK when required.
- No unresolved wildcard may be passed to `rnx2rtkp`.
- Subprocesses must use argument lists, not interpolated shell strings.
- RINEX header observation types must match body column order.
- Generated shell wrappers must contain resolved paths and be reproducible.

### 5.4 Agent D — EUREF/EPN Data Agent

Role:

- Implement base-station and navigation-data download logic.

Responsibilities:

- Derive rover time window.
- Resolve 4-character station code to RINEX 3 long station marker where configured.
- Implement BEV NRT RINEX 3 hourly provider.
- Implement BKG EUREF NRT RINEX 3 hourly fallback.
- Implement optional BKG high-rate RINEX 3 15-minute provider.
- Implement decompression and Hatanaka conversion.
- Implement mixed broadcast NAV download hooks and user-configurable URL templates.

Quality gates:

- Download only the time-window-overlapping files by default.
- Support `--whole-day` explicitly.
- Preserve downloaded files in cache by default.
- Detect and report whether files are OBS or NAV.
- Do not assume a station alias if not configured.

### 5.5 Agent E — NAV Resolution Agent

Role:

- Ensure correct and transparent selection of navigation data.

Responsibilities:

- Implement `NavSourceResolver`.
- Prefer explicit NAV, then base-derived/downloaded NAV, then rover-extracted NAV.
- Merge NAV sources by constellation.
- Produce verbose report explaining which NAV files were selected or rejected.
- Warn when observations exist for constellations without corresponding NAV.

Quality gates:

- Never claim full multi-GNSS post-processing when only GPS NAV is available.
- Rover NAV may be used as fallback but must be clearly identified as lower priority.
- If no NAV exists, abort before RTKLIB with a precise error.

### 5.6 Agent F — Quality/Metrics Agent

Role:

- Implement quality metrics and validation reports.

Responsibilities:

- Compute cadence statistics for NMEA and raw observations.
- Detect duplicates, gaps, missing raw epochs, malformed records, and noise bytes.
- Count constellations, bands, signals, and RINEX observation codes.
- Emit JSON analysis.
- Ensure `-v/--verbose` prints actionable diagnostics.

Quality gates:

- Metrics must be reproducible.
- Frequency metrics must include min, median, mean, max and large-gap counts.
- Warnings must be specific and actionable.
- JSON schema must be stable enough for future regression testing.

### 5.7 Agent G — Test/CI Agent

Role:

- Guarantee implementation quality through tests and CI.

Responsibilities:

- Add unit tests for every parser and writer.
- Add integration tests using small fixtures.
- Add golden-output tests for command generation and bitrate estimation.
- Add linting and type checking.

Recommended tools:

```text
pytest
ruff
mypy or pyright
coverage
pre-commit
```

Quality gates:

- No parser or writer module without tests.
- CI must run on Linux.
- Windows/Cygwin compatibility concerns must be documented.
- Test fixtures must be small; large real captures should not be committed unless explicitly allowed.

### 5.8 Agent H — Security/Safety Agent

Role:

- Prevent unsafe code and operational mistakes.

Responsibilities:

- Review all subprocess and filesystem operations.
- Ensure no shell injection through station codes, glob patterns, filenames, or URL templates.
- Ensure downloaded files are written only inside configured cache/output directories.
- Ensure raw logs are never deleted by default.
- Ensure network downloads are explicit and logged.
- Ensure serial `init apply` is optional and supports `--dry-run`.

Quality gates:

- No `shell=True` unless justified and reviewed.
- No destructive cleanup unless user passes `--cleanup`.
- No automatic firmware-changing or reset commands unless explicitly requested.
- Generated UM980 init scripts must include warnings when bitrate is unsafe.

### 5.9 Agent I — Documentation Agent

Role:

- Make the workflow usable.

Responsibilities:

- Write README.
- Write examples for:
  - UM980 command generation,
  - extraction only,
  - direct RINEX generation,
  - EUREF download,
  - full RTKLIB post-processing.
- Document limitations.
- Document known UM980 logging recommendations.

Quality gates:

- Every CLI command must have one realistic example.
- Error messages must be documented where common.
- README must explain why rover conversion avoids `convbin`.

---

## 6. CLI specification

### 6.1 Top-level commands

Implement:

```bash
um980-ppk init generate [options]
um980-ppk init apply [options]              # optional, later
um980-ppk analyze ROVER_LOG [options]
um980-ppk extract ROVER_LOG [options]
um980-ppk rinex ROVER_LOG [options]
um980-ppk download-base ROVER_LOG --station STATION [options]
um980-ppk postprocess ROVER_LOG [options]
um980-ppk pipeline ROVER_LOG --station STATION [options]
```

### 6.2 Common options

```text
-v, --verbose
--out-dir DIR
--basename NAME
--analysis-json
--config FILE
--dry-run
--log-file FILE
```

### 6.3 Extraction options

```text
--solution {all,csv,gpx,nmea,none}
--track-source {auto,nmea,ppp,adr,gga}
--obs-csv
--raw-output {none,ascii,binary,all}
--rinex-version 3.04
```

### 6.4 EUREF/base options

```text
--station CPAR
--station-long CPAR00CZE
--base-provider {auto,bev-nrt,bkg-euref-nrt,bkg-euref-highrate,custom}
--base-rate {30s,1s}
--base-template URL_TEMPLATE
--base-dir DIR
--cache-dir DIR
--time-margin 300
--whole-day
--offline
```

### 6.5 NAV options

```text
--nav-file FILE
--nav-glob GLOB
--nav-provider {auto,custom,none}
--nav-template URL_TEMPLATE
--download-nav / --no-download-nav
--use-rover-nav / --no-use-rover-nav
--nav-merge {best-per-system,all}
```

### 6.6 RTKLIB options

```text
--rtklib-dir DIR
--rnx2rtkp PATH
--convbin PATH
--crx2rnx PATH
--rtkconf FILE
--output-format {nmea,pos,llh}
--navsys {gps,gps-glo,gps-glo-gal-bds,all}
```

---

## 7. UM980 initialisation generator

### 7.1 Purpose

Generate ready-to-paste UM980 command scripts based on a user-specified logging profile.

The generator shall configure:

- serial port and baud rate;
- rover/base mode;
- PPP options;
- NMEA outputs;
- raw observation output format and rate;
- ephemeris output policy;
- diagnostics such as `TROPINFOA` and `GPSIONB`;
- `SAVECONFIG` if requested.

It shall also estimate expected serial bitrate and warn when the requested configuration is unsafe.

### 7.2 CLI

```bash
um980-ppk init generate \
  --port COM1 \
  --baud 230400 \
  --mode rover \
  --raw-format obsvmb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --ephemeris every=300 \
  --ppp e6-has \
  --ppp-converge 15,30 \
  --save-config \
  --out um980-init.cmd \
  -v
```

### 7.3 Supported options

```text
--port COM1
--baud 115200|230400|460800|921600
--mode rover|base
--base-lat LAT
--base-lon LON
--base-height HEIGHT
--nmea MSG=HZ[,MSG=HZ...]
--nmea-preset minimal|solution-20hz|solution-10hz|survey|none
--raw-format none|obsvma|obsvmb|obsvmcmpb
--raw-hz FLOAT
--raw-period FLOAT
--expected-sats INT
--expected-obs-per-epoch INT
--ephemeris off|onchanged|every=SECONDS
--ephemeris-systems gps,glo,gal,bds,bd3,qzss
--ppp none|e6-has|b2b-ppp|ssr-rx
--ppp-datum WGS84|PPPORIGINAL
--ppp-timeout SECONDS
--ppp-converge HORIZONTAL_CM,VERTICAL_CM
--include-tropinfo
--include-gpsion
--save-config / --no-save-config
--strict-bitrate / --allow-overload
--out FILE
--json FILE
-v, --verbose
```

`--raw-hz` and `--raw-period` are mutually exclusive.

### 7.4 NMEA rate interpretation

Accept:

```text
GNGGA=20       -> 20 Hz -> command period 0.05
GNGGA=1        -> 1 Hz  -> command period 1
GNGGA=0.2      -> 0.2 Hz -> command period 5
GNGGA=0        -> disable if supported by receiver command syntax
GNGGA@0.05s    -> direct period syntax
```

### 7.5 NMEA presets

#### `solution-20hz`

```text
GNGGA=20
GNRMC=20
GNGST=1
GNGSA=0.2
GNGSV=0.2
GNGLL=0
GNGNS=1
GPGRS=0.0333
PPPNAVA=0.1
ADRNAVA=0.1
```

#### `solution-10hz`

```text
GNGGA=10
GNRMC=10
GNGST=1
GNGSA=0.2
GNGSV=0.2
GNGLL=0
GNGNS=1
GPGRS=0.0333
PPPNAVA=0.1
ADRNAVA=0.1
```

#### `minimal`

```text
GNGGA=1
GNRMC=1
GNGST=1
GNGSA=0.2
GNGSV=0.2
GNGLL=0
GNGNS=0
GPGRS=0
PPPNAVA=0.1
ADRNAVA=0.1
```

User-provided `--nmea` overrides presets.

### 7.6 Generated command order

Generate commands in this order:

1. Comments with generated settings and bitrate estimate.
2. Serial configuration.
3. PPP configuration.
4. Receiver mode.
5. Raw observations.
6. NMEA messages.
7. Solution diagnostics.
8. Ephemerides.
9. Ionosphere/troposphere diagnostics.
10. `SAVECONFIG`, if requested.

Example:

```text
# Generated by um980-ppk init
# Port: COM1
# Baud: 230400
# Estimated payload: 16.8 kB/s
# Estimated 8N1 line rate: 168 kbps
# Utilisation: 73%
# Assessment: OK

CONFIG COM1 230400

CONFIG PPP ENABLE E6-HAS
CONFIG PPP DATUM WGS84
CONFIG PPP TIMEOUT 120
CONFIG PPP CONVERGE 15 30

MODE ROVER

OBSVMB COM1 0.5

GNGGA 0.05
GNRMC 0.05
GNGST 1
GNGSA 5
GNGSV 5
GNGLL 0
GNGNS 1
GPGRS 30
PPPNAVA 10
ADRNAVA 10

GPSEPHA  COM1 300
GLOEPHA  COM1 300
GALEPHA  COM1 300
BDSEPHA  COM1 300
BD3EPHA  COM1 300
QZSSEPHA COM1 300

TROPINFOA ONCHANGED
GPSIONB ONCHANGED

SAVECONFIG
```

Raw observation commands:

```text
OBSVMA COM1 0.5      # ASCII
OBSVMB COM1 0.5      # binary
OBSVMCMPB COM1 0.5   # compressed binary
```

Ephemeris policies:

```text
GPSEPHA COM1 ONCHANGED
GPSEPHA COM1 300
```

### 7.7 Bitrate estimation

Implement `bitrate.py`.

Serial capacity for 8N1:

```python
payload_capacity_bytes_per_s = baud / 10.0
line_rate_bits_per_s = payload_bytes_per_s * 10.0
utilisation = estimated_payload_bytes_per_s / payload_capacity_bytes_per_s
```

Thresholds:

```text
utilisation < 0.70           OK
0.70 <= utilisation < 0.85   WARNING: near limit
0.85 <= utilisation < 1.00   WARNING: high risk of gaps
utilisation >= 1.00          ERROR unless --allow-overload
```

Default raw observation sizing:

```python
def raw_epoch_bytes(format, nobs):
    if format == "obsvma":
        return 300 + nobs * 54
    if format == "obsvmb":
        return 24 + 4 + nobs * 40 + 4
    if format == "obsvmcmpb":
        return 24 + 4 + nobs * 24 + 4
```

Default expected observation count:

```text
expected_obs_per_epoch = 100
```

Allow:

```text
--expected-obs-per-epoch INT
--expected-sats INT
```

NMEA byte-size defaults:

```python
DEFAULT_NMEA_BYTES = {
    "GNGGA": 95,
    "GNRMC": 95,
    "GNGST": 90,
    "GNGNS": 105,
    "GNGLL": 75,
    "GPGRS": 120,
    "GNGSA": 85,
    "GPGSA": 85,
    "GAGSA": 85,
    "GLGSA": 85,
    "GBGSA": 85,
    "GPGSV": 90,
    "GAGSV": 90,
    "GLGSV": 90,
    "GBGSV": 90,
    "GNGSV": 90,
    "PPPNAVA": 180,
    "ADRNAVA": 180,
    "TROPINFOA": 160,
    "GPSIONB": 120,
}
```

For `GNGSV`, account for multiple physical GSV sentences per reporting epoch:

```python
GSV_LINES_PER_EPOCH = {
    "GPGSV": 4,
    "GAGSV": 3,
    "GLGSV": 2,
    "GBGSV": 5,
    "GNGSV": 12,
}
```

Ephemeris burst estimates:

```python
DEFAULT_EPH_BYTES = {
    "GPSEPHA": 350,
    "GLOEPHA": 250,
    "GALEPHA": 400,
    "BDSEPHA": 400,
    "BD3EPHA": 400,
    "QZSSEPHA": 350,
}
```

Verbose output must include:

```text
requested_configuration:
  port=COM1
  baud=230400
  raw_format=obsvmb
  raw_hz=2
  expected_obs_per_epoch=100

estimated_payload:
  nmea=8.9 kB/s
  raw=8.1 kB/s
  ephemeris_average=0.2 kB/s
  total_average=17.2 kB/s
  serial_payload_capacity=23.0 kB/s
  utilisation=74.8%
  assessment=WARNING near limit

format_comparison_at_requested_rate:
  OBSVMA ASCII:      11.4 kB/s raw, total 20.5 kB/s, utilisation 89%
  OBSVMB binary:      8.1 kB/s raw, total 17.2 kB/s, utilisation 75%
  OBSVMCMPB binary:   4.9 kB/s raw, total 14.0 kB/s, utilisation 61%
```

### 7.8 Safety behaviour

If `--strict-bitrate` is active and utilisation is too high, abort:

```text
ERROR: requested configuration is estimated at 112% of 230400 bps 8N1 capacity.
Suggested alternatives:
  - use OBSVMB instead of OBSVMA;
  - use OBSVMCMPB instead of OBSVMB;
  - reduce GNGGA/GNRMC from 20 Hz to 10 Hz;
  - reduce GNGSV/GNGSA/GNGNS/GNGST rates;
  - increase baud to 460800 or 921600.
```

If `--allow-overload` is used, generate commands but include warnings in comments.

### 7.9 YAML configuration

Support:

```yaml
port: COM1
baud: 230400
mode: rover
ppp:
  mode: e6-has
  datum: WGS84
  timeout: 120
  converge: [15, 30]
raw:
  format: obsvmb
  hz: 2
  expected_obs_per_epoch: 100
nmea:
  GNGGA: 20
  GNRMC: 20
  GNGST: 1
  GNGSA: 0.2
  GNGSV: 0.2
  GNGLL: 0
  GNGNS: 1
  GPGRS: 0.0333
  PPPNAVA: 0.1
  ADRNAVA: 0.1
ephemeris:
  policy: every
  period: 300
  systems: [gps, glo, gal, bds, bd3, qzss]
diagnostics:
  tropinfo: true
  gpsion: true
save_config: true
```

Command:

```bash
um980-ppk init generate --config um980-init.yaml --out um980-init.cmd -v
```

### 7.10 Optional serial apply

Optional later command:

```bash
um980-ppk init apply --device /dev/ttyUSB0 --baud 230400 --config um980-init.yaml
```

Requirements:

- send one command per line;
- wait for receiver response;
- log responses;
- abort on `ERROR` unless `--continue-on-error`;
- support `--dry-run`.

---

## 8. Mixed-stream parser

### 8.1 Record types

Implement a byte-level parser that recognises:

```text
$...*hh\r\n         NMEA
#...*hhhhhhhh\r\n   Unicore ASCII
AA 44 B5 ...        Unicore binary frame
noise               discard/resynchronise
```

Do not implement this as a line-only parser, because binary messages may be interleaved with NMEA.

### 8.2 Data model

```python
@dataclass
class StreamRecord:
    kind: Literal["nmea", "unicore_ascii", "unicore_binary", "noise"]
    offset: int
    raw: bytes
    text: str | None
    msg_type: str | None
    checksum_ok: bool | None
```

### 8.3 Parser requirements

The parser must:

- keep byte offsets;
- validate NMEA XOR checksum when present;
- validate Unicore ASCII checksum/CRC where feasible;
- validate Unicore binary CRC where implemented;
- recover after malformed records;
- count discarded noise bytes;
- not abort a full file because of one malformed record;
- provide detailed diagnostic counters.

---

## 9. NMEA extraction and receiver solution

### 9.1 Outputs

For `um980-ppk extract ROVER.unc --solution all`, produce:

```text
<base>.clean.nmea
<base>.solution.nmea
<base>.solution.csv
<base>.solution.gpx
<base>.solution_all_records.csv
```

### 9.2 NMEA types

Parse at least:

```text
GGA / GNS: position and fix quality
RMC: time, date, speed, course
GST: error estimates
GSA: DOP and fix mode
GSV: satellite visibility summary
GRS: residuals, optional
```

Clean NMEA must preserve original valid NMEA records in original order.

### 9.3 Solution model

```python
@dataclass
class SolutionPoint:
    time_utc: datetime
    source: Literal["GGA", "GNS", "RMC", "PPPNAVA", "ADRNAVA"]
    lat: float
    lon: float
    h_ell: float | None
    h_msl: float | None
    fix_quality: int | None
    fix_quality_text: str | None
    pos_type: str | None
    sol_status: str | None
    num_sats: int | None
    hdop: float | None
    vdop: float | None
    pdop: float | None
    sigma_e: float | None
    sigma_n: float | None
    sigma_u: float | None
    speed_mps: float | None
    course_deg: float | None
    age_diff: float | None
```

### 9.4 PPP/ADR preservation

If `PPPNAVA` or `ADRNAVA` are present, preserve their information in:

- CSV columns,
- GPX extensions,
- a proprietary NMEA sentence in generated solution-only NMEA.

Suggested proprietary sentence:

```text
$PUM980Q,<source>,<sol_status>,<pos_type>,<nsat>,<sigma_e>,<sigma_n>,<sigma_u>,<age>*hh
```

### 9.5 GPX extensions

GPX output must include:

```xml
<extensions>
  <um980:source>PPPNAVA</um980:source>
  <um980:fixQuality>...</um980:fixQuality>
  <um980:positionType>PPP_CONVERGING</um980:positionType>
  <um980:solutionStatus>SOL_COMPUTED</um980:solutionStatus>
  <um980:numSats>...</um980:numSats>
  <um980:sigmaE>...</um980:sigmaE>
  <um980:sigmaN>...</um980:sigmaN>
  <um980:sigmaU>...</um980:sigmaU>
</extensions>
```

---

## 10. Raw observation decoding

### 10.1 Supported observation logs

Support:

```text
OBSVMA      ASCII master-antenna observations
OBSVMB      binary master-antenna observations
OBSVMCMPB   compressed binary master-antenna observations
```

### 10.2 Normalised observation model

```python
@dataclass
class Observation:
    gps_week: int
    tow: float
    sat_system: Literal["GPS","GLONASS","Galileo","BDS","QZSS","SBAS","IRNSS","Unknown"]
    sv: int
    rinex_sat: str
    signal_name: str
    rinex_code: str
    band: str
    pseudorange_m: float | None
    carrier_phase_cycles: float | None
    doppler_hz: float | None
    cn0_dbhz: float | None
    lock_time_s: float | None
    half_cycle: bool | None
    lli: int
    raw_tracking_status: int
```

### 10.3 CSV output

Produce:

```text
<base>.observations.csv
```

Columns:

```text
epoch_index,gps_week,tow,datetime_utc,rinex_sat,system,sv,signal_name,rinex_code,band,
pseudorange_m,carrier_phase_cycles,doppler_hz,cn0_dbhz,lock_time_s,lli,tracking_status
```

### 10.4 Signal mapping

Implement a mapping from UM980 tracking status to RINEX 3 observation codes.

At minimum support the signals observed in the user’s captures:

```text
GPS:
  L1 C/A      -> C1C L1C D1C S1C
  L1C pilot   -> C1L L1L D1L S1L
  L2C(L)      -> C2L L2L D2L S2L
  L2P(Y)      -> C2W L2W D2W S2W
  L5 pilot    -> C5Q L5Q D5Q S5Q

Galileo:
  E1C         -> C1C L1C D1C S1C
  E5a pilot   -> C5Q L5Q D5Q S5Q
  E5b pilot   -> C7Q L7Q D7Q S7Q
  E6C         -> C6C L6C D6C S6C

GLONASS:
  G1 C/A      -> C1C L1C D1C S1C
  G2 C/A      -> C2C L2C D2C S2C
  G3Q         -> C3Q L3Q D3Q S3Q

BDS:
  B1I         -> C2I L2I D2I S2I, or configurable if validation shows another preferred mapping
  B1C pilot   -> C1P L1P D1P S1P
  B2I         -> C7I L7I D7I S7I
  B2a pilot   -> C5P L5P D5P S5P
  B2b(I)      -> configurable, default C7I/L7I/D7I/S7I until validated
  B3I         -> C6I L6I D6I S6I

SBAS:
  L1 C/A      -> C1C L1C D1C S1C
```

Make the mapping explicit, documented, and easy to edit. Where the RINEX code is uncertain, mark it in comments and allow override through configuration.

---

## 11. RINEX observation writer

### 11.1 Purpose

Convert decoded observations to RINEX 3.04 observation files suitable for RTKLIB.

Input:

```text
<base>.observations.csv
```

Output:

```text
<base>.direct.obs
```

### 11.2 Requirements

The writer must:

- group observations by epoch;
- group epoch observations by satellite;
- support mixed constellation file type `M`;
- write `SYS / # / OBS TYPES` per constellation;
- write blank fields for missing observables;
- preserve pseudorange, carrier phase, Doppler and SNR;
- include approximate receiver position if available;
- include marker name, receiver type, antenna type as configurable fields;
- use deterministic ordering of systems, satellites and observation codes.

Header example:

```text
G   12 C1C L1C D1C S1C C2L L2L D2L S2L C5Q L5Q D5Q S5Q  SYS / # / OBS TYPES
E   12 C1C L1C D1C S1C C5Q L5Q D5Q S5Q C7Q L7Q D7Q S7Q  SYS / # / OBS TYPES
R    8 C1C L1C D1C S1C C2C L2C D2C S2C                  SYS / # / OBS TYPES
C   16 C2I L2I D2I S2I C1P L1P D1P S1P C5P L5P D5P S5P C6I L6I D6I S6I SYS / # / OBS TYPES
```

Tests must verify that the body columns match the declared header order.

---

## 12. Ephemeris extraction and RINEX NAV

### 12.1 Scope

Minimum required:

```text
GPSEPHA ASCII -> RINEX GPS NAV
```

Future expansion:

```text
GLOEPHA       -> GLONASS NAV
GALEPHA       -> Galileo NAV
BDSEPHA       -> BeiDou NAV
BD3EPHA       -> BeiDou-3 NAV
QZSSEPHA      -> QZSS NAV
```

### 12.2 Output

```text
<base>.rover-gps.nav
```

### 12.3 Reporting

Verbose output must state:

```text
GPSEPHA records found: N, converted: M
GLOEPHA records found: N, conversion not yet implemented
GALEPHA records missing
BDSEPHA/BD3EPHA records missing
QZSSEPHA records missing
```

Never derive NAV from observations alone.

---

## 13. EUREF/EPN base data download

### 13.1 User’s existing download patterns

The implementation shall replicate and generalise the user’s current shell-script logic.

Primary current RINEX 3 hourly BEV pattern:

```bash
wget ftp://gnss.bev.gv.at/pub/nrt/${DAY}/${i}/${STATION}_R_${YEAR}${DAY}${i}00_01H_30S_MO.crx.gz
gunzip *.gz
for i in *.crx; do ../crx2rnx.exe "$i"; done
rm *.crx
```

BKG EUREF NRT fallback pattern:

```bash
wget ftp://igs.bkg.bund.de/EUREF/nrt/${DAY}/${i}/${STATION}_R_${YEAR}${DAY}${i}00_01H_30S_MO.crx.gz
```

BKG high-rate RINEX 3 pattern:

```bash
wget ftp://igs.bkg.bund.de/EUREF/highrate/${YEAR}/${DAY}/${hour_letter}/${STATION}_S_${YEAR}${DAY}${HH}${MM}_15M_01S_MO.crx.gz
```

Legacy RINEX 2 patterns may be supported later.

### 13.2 Station code handling

Accept:

```text
--station CPAR
--station CPAR00CZE
```

If 4-character code is supplied, resolve using configured aliases:

```yaml
station_aliases:
  CPAR: CPAR00CZE
  KUNZ: KUNZ00CZE
  TUBO: TUBO00CZE
  GOPE: GOPE00CZE
  GOP7: GOP700CZE
  GRAZ: GRAZ00AUT
```

Do not guess unconfigured station long names. Allow explicit override:

```text
--station-long CPAR00CZE
```

### 13.3 Rover time window

Derive time window from:

1. NMEA RMC date+time;
2. GGA/GNS time plus date inferred from RMC or filename;
3. OBSVM GPS week + TOW;
4. user-supplied `--date YYYY-MM-DD`.

Apply margin:

```text
--time-margin 300
```

Download all overlapping hourly or high-rate files.

### 13.4 Providers

#### BEV NRT RINEX 3 hourly

```python
Provider(
    name="bev_nrt_v3_hourly",
    kind="obs",
    templates=[
        "ftp://gnss.bev.gv.at/pub/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
        "ftp://gnss.bev.gv.at/pub/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.rnx.gz",
    ],
)
```

#### BKG EUREF NRT RINEX 3 hourly

```python
Provider(
    name="bkg_euref_nrt_v3_hourly",
    kind="obs",
    templates=[
        "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.crx.gz",
        "ftp://igs.bkg.bund.de/EUREF/nrt/{doy}/{hh}/{station_long}_R_{yyyy}{doy}{hh}00_01H_30S_MO.rnx.gz",
    ],
)
```

#### BKG high-rate RINEX 3

```python
Provider(
    name="bkg_euref_highrate_v3",
    kind="obs",
    templates=[
        "ftp://igs.bkg.bund.de/EUREF/highrate/{yyyy}/{doy}/{hour_letter}/{station_long}_S_{yyyy}{doy}{hh}{minute}_15M_01S_MO.crx.gz",
    ],
)
```

Map hours to letters:

```text
00=a, 01=b, 02=c, ..., 23=x
```

Minutes:

```text
00, 15, 30, 45
```

For `--base-rate 1s`, download all 15-minute chunks overlapping the rover window.

### 13.5 Base-rate warning

If rover raw observations are about 2 Hz and base OBS interval is 30 s, warn:

```text
WARNING: base observation interval appears to be 30 s while rover raw observations are ~2 Hz. Kinematic RTK/PPK quality will be limited. Prefer 1 s high-rate base data if available.
```

### 13.6 Decompression and Hatanaka conversion

Implement:

```python
def normalise_rinex_file(path: Path) -> Path:
    """
    Converts .gz, .Z, .crx, .d, .crx.gz, .d.gz to usable RINEX file where needed.
    Returns final RINEX file path.
    """
```

Rules:

- `.gz`: Python `gzip` or external `gunzip -k`;
- `.Z`: `uncompress`, `gzip -d`, or `7z`;
- `.crx`: `crx2rnx`;
- RINEX 2 Hatanaka `.d`: `crx2rnx`;
- preserve original compressed files by default;
- do not delete `.crx` unless `--cleanup`.

---

## 14. Navigation data resolution

### 14.1 Principle

Navigation data shall be obtained from all available sources, with preference order:

1. User-provided explicit NAV/SP3/CLK files.
2. Base-derived NAV:
   - from base RTCM3 conversion,
   - from downloaded broadcast/mixed NAV,
   - from station/provider NAV if available.
3. Rover-side NAV extracted from UM980 ephemeris logs.
4. Abort if no NAV source exists.

Base-side or downloaded NAV is preferred over rover-side NAV for quality/completeness. Rover-side NAV is a fallback and for self-contained logs.

### 14.2 Data model

```python
@dataclass
class NavCandidate:
    path: Path
    source: Literal["explicit", "base_rtcm", "downloaded_brdc", "downloaded_station", "rover"]
    priority: int
    systems: set[str]
    time_start: datetime | None
    time_end: datetime | None
    rinex_version: str | None
    file_type: Literal["nav", "sp3", "clk", "sbs", "unknown"]
    usable: bool
    notes: list[str]
```

Priorities:

```text
explicit NAV/SP3/CLK         100
base-derived NAV              90
downloaded BRDC/mixed NAV     80
downloaded station NAV        75
rover-extracted NAV           50
```

Default merge policy:

```text
--nav-merge best-per-system
```

Alternative:

```text
--nav-merge all
```

### 14.3 Selection examples

If external mixed BRDC NAV contains GPS+GLO+GAL+BDS, select it and reject rover GPS NAV as redundant.

If no external NAV exists but rover has `GPSEPHA`, use rover GPS NAV and warn:

```text
WARNING: only GPS NAV available; Galileo/BDS/GLONASS observations will be ignored by RTKLIB unless external NAV is supplied.
```

If Galileo observations exist but no Galileo NAV source exists:

```text
WARNING: Galileo observations present but no Galileo NAV from explicit/base/download/rover sources.
```

If BDS observations exist but no BDS NAV source exists:

```text
WARNING: BeiDou observations present but no BDS NAV from explicit/base/download/rover sources.
```

### 14.4 Base RTCM3 support

If user supplies:

```text
--base-rtcm base.rtcm3
```

Run:

```bash
convbin -r rtcm3 -od -os -oi -ot -f 3 base.rtcm3
```

Collect outputs:

```text
base.obs
base.nav
base.gnav
base.lnav
base.cnav
base.qnav
base.sbs
```

Treat these NAV files as priority 90.

Do not use `convbin` as the primary path for rover UM980 logs.

---

## 15. RTKLIB integration

### 15.1 Command assembly

Build `rnx2rtkp` input list:

```python
rtklib_inputs = [
    rover_direct_obs,
    *base_obs_files,
    *selected_nav_files,
    *selected_precise_files,
]
```

Run:

```python
subprocess.run([
    rnx2rtkp,
    "-k", rtkconf,
    "-o", output_file,
    rover_direct_obs,
    *base_obs_files,
    *selected_nav_files,
])
```

No `shell=True` unless explicitly reviewed.

### 15.2 Validation before running

Validate:

- rover observation file exists and contains `OBSERVATION DATA`;
- at least one base observation file exists and contains `OBSERVATION DATA`;
- at least one NAV/SP3/CLK/SBS file exists where required;
- no path contains unresolved wildcard characters;
- RTKLIB executable exists or is resolvable in `PATH`.

### 15.3 Output files

Produce:

```text
<base>-rtk.nmea
<base>-rtk.pos
<base>-rtk.stat, if RTKLIB produces it
<base>.rtkpost-wrapper.sh
<base>.rtklib.stdout.log
<base>.rtklib.stderr.log
```

Generated wrapper must include all resolved input files.

---

## 16. Quality metrics and verbose output

### 16.1 Required top-level metrics

`-v/--verbose` must print:

```text
input_bytes
valid_nmea_records
invalid_nmea_records
unicore_ascii_records
unicore_binary_records
noise_bytes
nmea_types=...
unicore_types=...
solution_points=...
```

### 16.2 NMEA frequency metrics

For each relevant NMEA type:

```text
records
unique_timestamps
mean_hz
median_hz
min_hz
max_hz
interval_median_s
interval_max_s
duplicates
missing_est
large_gaps
```

### 16.3 Raw observation metrics

```text
raw_type=OBSVMA/OBSVMB/OBSVMCMPB
epochs
valid_time_epochs
mean_hz
median_hz
min_hz
max_hz
interval_median_s
interval_max_s
missing_est
large_gaps
epoch_observations_min
epoch_observations_mean
epoch_observations_median
epoch_observations_max
```

### 16.4 GNSS content metrics

```text
constellations=GPS:...,Galileo:...,GLONASS:...,BDS:...,SBAS:...
bands=...
signals=...
rinex_observation_codes=...
```

### 16.5 Ephemeris metrics

```text
GPSEPHA:N
GLOEPHA:N
GALEPHA:N
BDSEPHA:N
BD3EPHA:N
QZSSEPHA:N
```

### 16.6 Base/NAV metrics

```text
base_download:
  station_input=CPAR
  station_long=CPAR00CZE
  provider=bev_nrt_v3_hourly
  requested_window=...
  downloaded_files=...
  normalised_obs_files=...
  obs_interval_detected=30.0

nav_resolution:
  explicit_candidates=...
  base_rtcm_candidates=...
  downloaded_candidates=...
  rover_candidates=...
  selected_nav_files=...
  missing_systems=...
```

### 16.7 Warnings

Examples:

```text
WARNING: raw observation mean frequency below 95% of expected.
WARNING: raw observation large timestamp gap detected.
WARNING: duplicate GGA/RMC timestamps detected.
WARNING: Galileo observations present but GALEPHA missing in rover log.
WARNING: BeiDou observations present but BDSEPHA/BD3EPHA missing in rover log.
WARNING: no external mixed NAV file found.
WARNING: rover GPS NAV used because no base/download GPS NAV was available.
WARNING: rnx2rtkp would fail: no nav data.
```

### 16.8 JSON analysis

Produce:

```text
<base>.analysis.json
```

It must include all metrics in machine-readable form.

---

## 17. Configuration file

Support one project configuration file, for example:

```yaml
receiver:
  port: COM1
  baud: 230400
  mode: rover

raw:
  format: obsvmb
  hz: 2
  expected_obs_per_epoch: 100

nmea:
  preset: solution-20hz
  overrides:
    GNGSV: 0.2
    GNGSA: 0.2

ephemeris:
  policy: every
  period: 300
  systems: [gps, glo, gal, bds, bd3, qzss]

ppp:
  mode: e6-has
  datum: WGS84
  timeout: 120
  converge: [15, 30]

base:
  station: CPAR
  station_long: CPAR00CZE
  provider: bev-nrt
  rate: 30s
  cache_dir: test-euref

nav:
  download: true
  merge: best-per-system
  use_rover_fallback: true

rtklib:
  rnx2rtkp: ../../rnx2rtkp_win64.exe
  convbin: ../../convbin
  crx2rnx: ../crx2rnx.exe
  config: ../../rtkpost-normal.conf
```

---

## 18. Example commands

### 18.1 Generate UM980 init script

```bash
um980-ppk init generate \
  --port COM1 \
  --baud 230400 \
  --mode rover \
  --raw-format obsvmb \
  --raw-hz 2 \
  --nmea-preset solution-20hz \
  --ephemeris every=300 \
  --ppp e6-has \
  --save-config \
  --out um980-init.cmd \
  -v
```

### 18.2 Analyse and extract only

```bash
um980-ppk extract rover_20260518124931.unc \
  -v \
  --analysis-json \
  --obs-csv \
  --solution all \
  --raw-output all
```

### 18.3 Generate rover RINEX observation

```bash
um980-ppk rinex rover_20260518124931.unc \
  --obs-csv \
  --rinex-version 3.04 \
  -v
```

### 18.4 Download base observations

```bash
um980-ppk download-base rover_20260518124931.unc \
  --station CPAR \
  --base-provider bev-nrt \
  --base-rate 30s \
  --time-margin 300 \
  -v
```

### 18.5 Full pipeline

```bash
um980-ppk pipeline rover_20260518124931.unc \
  --station CPAR \
  --base-provider bev-nrt \
  --download-nav \
  --rtklib-dir ../.. \
  --rnx2rtkp ../../rnx2rtkp_win64.exe \
  --rtkconf ../../rtkpost-normal.conf \
  -v
```

### 18.6 Use explicit NAV file

```bash
um980-ppk pipeline rover_20260518124931.unc \
  --station CPAR \
  --nav-file BRDC00WRD_R_20261380000_01D_MN.rnx \
  --rnx2rtkp ../../rnx2rtkp_win64.exe \
  --rtkconf ../../rtkpost-normal.conf \
  -v
```

---

## 19. Error handling

Abort with precise, actionable messages for:

```text
No valid observation epochs decoded.
No rover time window could be determined.
Base station files could not be downloaded or found.
Station code is unknown and no --station-long was supplied.
Base files are NAV instead of OBS.
No NAV files available.
Galileo/BDS observations present but no matching NAV source exists.
rnx2rtkp executable missing.
crx2rnx required but unavailable.
RINEX writer emitted no observation types.
Generated UM980 logging profile exceeds selected baud rate.
```

Examples:

```text
ERROR: no NAV data available.
Provide --nav-file, enable --download-nav, provide --base-rtcm, or log GPSEPHA/GALEPHA/BDSEPHA from the rover.
```

```text
ERROR: station CPAR could not be resolved to a RINEX 3 marker name.
Use --station-long CPAR00CZE or add an alias to the configuration.
```

---

## 20. Acceptance criteria

### 20.1 General

The implementation is complete when:

1. One command can turn a UM980 mixed log into solution GPX/NMEA/CSV and rover RINEX OBS.
2. The tool supports ASCII, binary and compressed binary observation streams, or has clearly marked decoder stubs and failing tests for unsupported modes until implemented.
3. The tool downloads or locates EUREF/EPN base observation files by station code and rover time window.
4. The tool downloads, locates, derives, or selects navigation files.
5. `rnx2rtkp` is invoked with actual existing file paths only.
6. Verbose output reports cadence, missing epochs, duplicate NMEA timestamps, constellations, signals and ephemeris completeness.
7. Failure modes are explicit and actionable.
8. The UM980 init generator can produce a ready-to-paste command script and bitrate report.

### 20.2 Known sample acceptance

Using `rover_20260518124931.unc`, analyser should approximately report:

```text
input_bytes=13820655
valid_nmea_records=56754
unicore_ascii_records=2146
OBSVMA=1334
solution_points≈13630
GNGGA mean≈19.329 Hz, median≈20 Hz
GNRMC mean≈19.440 Hz, median≈20 Hz
OBSVMA mean≈1.922 Hz, median≈2 Hz
constellations include GPS, Galileo, GLONASS, BDS, SBAS
signals include GPS L1/L2/L5, Galileo E1/E5a/E5b/E6, GLONASS G1/G2/G3, BDS B1/B2/B3
warnings include missing GALEPHA and BDSEPHA/BD3EPHA if no such records are present
```

Generated files:

```text
rover_20260518124931.clean.nmea
rover_20260518124931.solution.nmea
rover_20260518124931.solution.csv
rover_20260518124931.solution.gpx
rover_20260518124931.solution_all_records.csv
rover_20260518124931.observations.csv
rover_20260518124931.direct.obs
rover_20260518124931.analysis.json
rover_20260518124931.rtkpost-wrapper.sh
```

If `GPSEPHA` is present:

```text
rover_20260518124931.rover-gps.nav
```

### 20.3 Init generator tests

1. `solution-20hz + OBSVMA 5 Hz + 230400` must report utilisation above capacity and fail in strict mode.
2. `solution-20hz + OBSVMA 2 Hz + 230400` must report high utilisation / near-limit.
3. `solution-20hz + OBSVMB 2 Hz + 230400` must report lower utilisation than ASCII.
4. `solution-20hz + OBSVMCMPB 2 Hz + 230400` must report lower utilisation than binary.
5. `--ephemeris every=300` must generate all selected ephemeris commands with period `300`.
6. `--ephemeris onchanged` must generate all selected ephemeris commands with `ONCHANGED`.
7. `--raw-format none` must not generate `OBSVM*` commands.
8. `--mode base` without base coordinates must fail clearly.
9. `--mode rover` must generate `MODE ROVER`.
10. JSON report must contain NMEA, raw, ephemeris, total and utilisation fields.

---

## 21. Coding standards

### 21.1 Python

- Python 3.11+.
- Use dataclasses and type hints.
- Use `pathlib.Path`.
- Use `logging`, not bare `print`, except CLI final output.
- Use `argparse` or `typer`; prefer minimal dependencies if uncertain.
- Functions should be small and testable.
- Binary parsing must use `struct` with explicit endianness.

### 21.2 Subprocess

- Use `subprocess.run([...], check=False, capture_output=True, text=True)`.
- Do not use `shell=True` for RTKLIB, `crx2rnx`, `gzip`, `wget`, etc.
- If a shell wrapper is generated, it is for user reproducibility, not the internal execution path.
- Always log command, stdout and stderr.

### 21.3 Filesystem

- Write outputs to `--out-dir`.
- Use `--cache-dir` for downloads.
- Never delete raw logs.
- Preserve compressed downloads unless `--cleanup`.
- Avoid overwriting unless `--force`.

### 21.4 Network

- Download only when user runs download or pipeline commands that imply download.
- Print URLs in verbose mode.
- Support retries.
- Support offline mode.
- Verify downloaded file is non-empty and classifiable.

---

## 22. Implementation phases for Codex

### Phase 1 — Package and CLI skeleton

Deliver:

- package structure;
- CLI with stub commands;
- configuration loading;
- logging setup;
- CI baseline.

### Phase 2 — UM980 init generator and bitrate model

Deliver:

- `initgen.py`;
- `bitrate.py`;
- YAML input;
- command script output;
- tests.

### Phase 3 — Stream parser and NMEA extraction

Deliver:

- `stream.py`;
- `nmea.py`;
- clean NMEA output;
- solution CSV/NMEA/GPX;
- metrics for NMEA cadence.

### Phase 4 — ASCII raw observations

Deliver:

- `OBSVMA` parser;
- observations CSV;
- quality metrics;
- RINEX OBS writer;
- tests using known sample.

### Phase 5 — Binary and compressed binary observations

Deliver:

- binary frame parser;
- `OBSVMB` decoder;
- `OBSVMCMPB` decoder;
- tests with synthetic frames and real captures when available.

### Phase 6 — Rover NAV extraction

Deliver:

- `GPSEPHA` parser;
- GPS RINEX NAV writer;
- reporting for unsupported ephemeris types.

### Phase 7 — EUREF/EPN download

Deliver:

- BEV NRT provider;
- BKG EUREF NRT fallback;
- optional BKG high-rate provider;
- station alias configuration;
- decompression and `crx2rnx`;
- file classification.

### Phase 8 — NAV resolver

Deliver:

- `NavCandidate`;
- best-per-system merge;
- detailed verbose report;
- warnings for missing constellation NAV.

### Phase 9 — RTKLIB runner

Deliver:

- RTKLIB input validation;
- `rnx2rtkp` runner;
- reproducible shell wrapper;
- stdout/stderr logs.

### Phase 10 — Full integration and documentation

Deliver:

- end-to-end `pipeline`;
- README;
- examples;
- regression tests;
- known limitations.

---

## 23. Known limitations to document

1. Rover-extracted NAV is only as complete as ephemerides logged by UM980.
2. GPS `GPSEPHA` extraction is first priority; GLONASS/Galileo/BDS/QZSS NAV extraction may be implemented later.
3. 30 s base data is suboptimal for 2 Hz kinematic rover raw data; 1 s high-rate base data is preferred where available.
4. RINEX code mapping for some BeiDou signals may require validation and must remain configurable.
5. Compressed binary `OBSVMCMPB` parsing must be carefully verified with real captures before production use.
6. RTKLIB solution quality depends heavily on antenna metadata, base distance, multipath, base interval, NAV completeness and configuration file.

---

## 24. Definition of done

The project is done when:

- `um980-ppk init generate` creates safe receiver init scripts with bitrate estimates.
- `um980-ppk extract` creates clean NMEA, internal solution GPX/NMEA/CSV and observation CSV.
- `um980-ppk rinex` creates a usable RINEX 3 observation file from UM980 raw observations.
- `um980-ppk download-base` downloads and normalises EUREF/EPN base observations for the rover time window.
- `um980-ppk pipeline` resolves NAV from explicit/base/download/rover sources and runs `rnx2rtkp`.
- The pipeline fails early and clearly when NAV is missing.
- Verbose output gives enough metrics to diagnose serial overload, dropped raw epochs, missing ephemerides and poor base-data interval.
- Unit and integration tests cover parser, writer, downloader, NAV resolver, init generator and RTKLIB command assembly.
