# The aq product

An air-quality monitoring station reports an hourly mean of a handful of
pollutants. A national network is a few dozen to a few thousand such
stations, a megabyte or two a day — not a raster — so air quality is a
point product published beside the runs, the surface stations
(`docs/synop.md`), the airports (`docs/airport.md`) and the soundings
(`docs/sounding.md`). Each network enters through an adapter that reads it
into one set of elements and units, and the product carries every network
in the same shape. The networks are aggregated twice an hour by
`xue aq-build` (`xuebuild/aq/`) and taken live by the same pointer →
immutable directory contract the other point products use. This document
is the normative description of that product, schema v1.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the other point products' pointers.

- `latest-aq.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "aq", "issued": "2026-10-31T15:45:00Z",
   "path": "aq.202610311545/index.json", "byteLength": 1311, "crc32": "1c0ffee5"}
  ```

  `path` is relative to the pointer and always `aq.<round>/index.json`;
  `byteLength` and `crc32` (zlib CRC32 of the file's bytes, eight lowercase
  hex digits) are the index's, so it can be requested as
  `<path>?v=<crc32>` through an immutable cache.
- `aq.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the product was aggregated at, always a multiple of five. It
  holds `index.json` (§4), per network a `<network>.stations.json` (§5) and
  a `<network>.jsonl` (§6), and `item.json`, the round's STAC Item
  (`docs/stac.md` §"Point products"). Nothing in it changes once the
  pointer names it. The rounds sit flat under the root: each carries the
  whole window and lives three hours (`make prune-r2-aq`, `AQ_KEEP=6`), so
  there is no archive to file by day.
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

Three ways to read:

- **A map.** Fetch the index, then the `<network>.stations.json` of each
  network to show: every station's position, agency and current values,
  no history.
- **One station.** Take the station's row in its network's stations file,
  which ends in the byte `offset` and `length` of its line in the
  network's `.jsonl`, and issue
  `GET aq.<round>/<network>.jsonl?v=<crc32>` with
  `Range: bytes=<offset>-<offset + length - 1>`. The bytes are that
  station's JSON object exactly, without the newline, so they parse on
  their own.
- **A whole network.** Fetch `<network>.jsonl` and read it line by line:
  one station per line, sorted by id.

The publisher builds at a quarter past and a quarter to each hour
(`publish-aq.yml`): the networks post an hour between ten and fifty-five
minutes after it ends. Each network is asked for what it holds after its
newest time in the previous round's index, so a network that was
unreachable catches up, on its next answer, as far back as its own
retention reaches. A network that fails is recorded (§7) and keeps its
history; the round publishes with the rest. The pointer is withheld only
when *no* network's observations arrived, in which case the previous round
stays live. A round the pointer already names is finished; the workflow's
`force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A new network is a new
entry in the index and two new files, which a v1 reader admits (§8); a
`schemaVersion` bump follows the runs' rule (ship the shell first).

## 2. Elements and units

Every value is an **hourly mean**, and its time is the **end** of the hour
it averages. Every network is converted to one table of elements at the
adapter. The index lists them in this order (`elements`):

| key | quantity | unit | type |
|---|---|---|---|
| `pm25` | PM2.5 mass concentration | µg/m³, to a tenth | number, −5 … 10000 |
| `pm10` | PM10 mass concentration | µg/m³, to a tenth | number, −5 … 10000 |
| `o3` | ozone | ppb, to a tenth | number, −5 … 1000 |
| `no2` | nitrogen dioxide | ppb, to a tenth | number, −5 … 2000 |
| `so2` | sulphur dioxide | ppb, to a tenth | number, −5 … 5000 |
| `co` | carbon monoxide | ppm, to a hundredth | number, −0.5 … 100 |

- Gases are mixing ratios, particles mass concentrations. A network that
  reports a gas as a mass concentration is converted with the molar volume
  at 20 °C and 1 atm, 24.04 L/mol:

  ```
  ppb = µg/m³ × 24.04 / M          M: O3 48.00, NO2 46.01, SO2 64.07 g/mol
  ppm (co) = µg/m³ × 24.04 / (28.01 × 1000)
  ```

  then rounded to the element's precision. A network that reports in the
  product's units is published as reported.
- Small negative values are kept. An analyser near zero drifts below it,
  and a network that publishes such an hour (AirNow does) is not
  corrected; below the range's floor a value is `null`.
- A network publishes the elements it measures and leaves the others
  `null`. Nothing is derived: no AQI, AQHI or other index, no 8-hour or
  24-hour averages — a reader computes those from the 24-hour window with
  the scale of its choice. Observations only.
- A value the network marks missing, one in a unit other than the one
  expected, or one outside the range above, is `null`.
- Times in the network files are **epoch seconds, UTC**; times in the
  index and the stations files are ISO 8601 UTC with a `Z` suffix.
- Positions are decimal degrees to four places; `elev` is metres above
  sea level, or `null` where the network does not give it.

The files are written compactly with ASCII escaping, and a file's identity
is its bytes: the CRC32 in the index, and the pointer's, is of the file
exactly as served. Given the same inputs a round is byte-identical; only
the index's `generated` and the sources' `fetched` depend on when the
build ran.

## 3. Identity

A station is `<network>:<the network's own station identifier>`
(`airnow:060371103` is Los Angeles – N. Main Street, `hkepd:causeway-bay`
the Causeway Bay roadside station). The network id is lowercase,
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`; the station part matches
`[0-9A-Za-z_.-]+`. The id is the key everywhere: each stations file's rows
are sorted by it, each network file's lines are sorted by it, and every
line repeats it as `id` so a slice read by range identifies itself.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-10-31T15:45:00Z",
  "generated": "2026-10-31T15:46:10Z",
  "networks": [
    {"id": "airnow", "name": "AirNow", "cadence": 3600, "avgPeriod": 3600, "preliminary": true,
     "attribution": "AirNow (U.S. EPA) and the reporting agencies; preliminary data",
     "license": "AirNow-DEG", "url": "https://www.airnow.gov/",
     "latest": "2026-10-31T15:00:00Z",
     "file":     {"path": "airnow.jsonl",         "byteLength": 1296859, "crc32": "9f060241"},
     "stations": {"path": "airnow.stations.json", "byteLength": 334561,  "crc32": "3a1401de", "count": 2028}}
  ],
  "elements": ["pm25", "pm10", "o3", "no2", "so2", "co"],
  "sources": [ … ]
}
```

- `issued` is the round, `generated` when the build ran.
- `networks[]` is one entry per network that has a station in the window,
  in the publisher's order: its id and display `name`; `cadence`, the
  seconds between a station's reports; `avgPeriod`, the seconds each value
  averages over, ending at its time; `preliminary`, true when the network
  publishes unvalidated data, which a display must then say; the
  `attribution` a display must show, the `license` the network's data is
  reused under (§9 spells out each) and the network's `url`; `latest`, the
  newest observation time in the network's file; `file`, the network file's
  name (always `<id>.jsonl`), length and CRC32; and `stations`, the
  stations file's name (always `<id>.stations.json`), length, CRC32 and
  row `count`.
- `elements` is the element order of every stations file's rows, which
  repeats it. A reader indexes a row by position: `8 + elements.indexOf(key)`.
- `sources[]` is §7.

## 5. `<network>.stations.json`

```json
{"schemaVersion": 1, "network": "airnow", "issued": "2026-10-31T15:45:00Z",
 "elements": ["pm25", "pm10", "o3", "no2", "so2", "co"],
 "stations": [
   ["airnow:060371103", 34.0664, -118.2268, 30.5, 1, "Los Angeles - N. Main Street",
    "South Coast AQMD", "2026-10-31T15:00:00Z", 5.3, 14.0, 13.0, 11.0, 0.2, 0.3, 4180, 902]
 ]}
```

`issued` and `elements` equal the index's. `stations[]` is one compact row
per station, sorted by id and unique, `count` of them:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 … | last two |
|---|---|---|---|---|---|---|---|---|---|
| `id` | `lat` | `lon` | `elev` | `rank` | `name` | `agency` | `obsTime` | one value per `elements` | `offset`, `length` |

- Every id's prefix is the file's `network`.
- `rank` is how prominent the station is, which a map thins by: `0` a
  principal or background station, `1` a general station, `2` a roadside
  station.
- `name` is the station's name in Latin script, or `null`. `agency` is the
  agency that operates the station and owns its data, or `null`; where the
  network's terms ask for the agency to be credited (AirNow), a display
  shows it with the station's values.
- `obsTime` is the end of the station's newest hour.
- The element values are the station's **current** values: for each
  element, its newest non-null value no more than an hour before
  `obsTime`. A network's newest hour arrives a little short and fills in
  over the next hours, and a station's analysers do not always report
  together; the hour of grace keeps a late element from blinking out.
- `offset` and `length` are the byte span of the station's line in
  `<network>.jsonl`, the JSON object alone. The spans are in row order, do
  not overlap, and lie inside that file's `byteLength`.

## 6. `<network>.jsonl`

One line per station of that network, sorted by id, each the compact JSON
object below followed by `\n`:

```
{"id": "hkepd:causeway-bay", "name": "Causeway Bay" | null,
 "names": {"zh-Hant": "銅鑼灣"} | null,
 "lat": 22.2801, "lon": 114.1851, "elev": null, "rank": 2, "agency": "EPD" | null,
 "time": [1793376000, 1793379600, …],     — epoch seconds UTC, the end of each hour, strictly increasing
 "obs": {"pm25": [33.7, 31.7, …], "co": [0.33, null, …]}}
```

- `names` maps BCP 47 language tags to the network's own spellings;
  a display picks the viewer's language and falls back to `name`.
- `time` is the station's window, oldest first: every hour ending in the
  24 hours up to the round at which the station reported anything.
- `obs` holds one array per element the station reported at least once in
  the window, each as long as `time`, `null` where that element was not
  reported in that hour. An element the station never reported in the
  window is absent, and reads as all `null`.

A station's window is a merge, not a snapshot. Each round lays the
network's new hours over the previous round's file keyed by (station,
time), the new one replacing the old whole — a network that revises an
hour, as AirNow does for days afterwards, is believed, and an hour it
rewrites without a value withdraws the value — and drops what ended more
than 24 hours before the round. A station that did not report keeps its
history; one with nothing left in the window leaves the files until it
reports again. Station metadata is the newest the network gave. A network
with no station left is not listed.

## 7. `sources[]`

One entry per source, every network's in turn, in build order:

```json
{"id": "airnow-hourly", "ok": true, "fetched": "2026-10-31T15:46:01Z",
 "url": "https://files.airnowtech.org/airnow", "files": 3, "missing": 0, "reports": 5980, "unplaced": 6}
{"id": "epd-24pc", "ok": false, "fetched": "2026-10-31T15:46:02Z",
 "url": "https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml", "error": "HTTP Error 503"}
```

`ok: false` always carries `error`. `reports` is what the source
contributed: station-hours with at least one value. `unplaced` is how many
station-hours (AirNow) or station names (EPD) had no usable position or
were not in the station table and were dropped. A network's own fields
are described in its section.

## 8. Validation

`xuebuild/aq/schema.py` validates every file as it is written, and
`tests/test_aq.py` holds two consecutive rounds built from the fetched
fixture (`tests/fixtures/aq/aq.202610311515/` and `…1545/`) to the
committed golden (`tests/fixtures/aq/expected/`), regenerated by
`tests/prepare_aq_golden.py`; every stations-file row's span is checked
against the network file it addresses. Admission is structural: a
network, a station or an element key a reader has never seen is not an
error — the row width follows the index's own `elements` — and values of
the elements in §2 are held to their ranges. A reader validates the same
shapes on read and rejects a `schemaVersion` above the one it implements.

## 9. Networks

### 9.1 `airnow` — US EPA AirNow

The US Environmental Protection Agency's AirNow: about 2 000 stations a
given hour across the United States, Canada and Mexico, run by federal,
state, local and tribal agencies and their Canadian and Mexican
counterparts, and a handful of US diplomatic posts abroad. `cadence` and
`avgPeriod` are 3600; `preliminary` is true.

Source. `airnow-hourly` —
`https://files.airnowtech.org/airnow/<YYYY>/<YYYYMMDD>/HourlyAQObs_<YYYYMMDDHH>.dat`,
one quoted CSV per hour with a row per site, about 1 MB (the dated
archive; the file is not under `today/`). The hour in the name and in
`ValidDate`/`ValidTime` is the **start** of the averaging hour in UTC, so
a value's time is that plus 3600 s. AirNow posts an hour about 35 minutes
after it ends and rewrites the files of the previous days every hour as
late data arrive, so a round reads the newest three hours that have ended
by the round every time, plus any older hour that ends after the
network's previous `latest`, at most the 24 hours of the window. `files`
is how many hours were read, `missing` how many were not there yet
(HTTP 404).

Reading. The file is decoded as UTF-8 (Latin-1 if it is not). Each row
is a station-hour:

| AirNow column | element | conversion |
|---|---|---|
| `PM25` | `pm25` | — (`PM25_Unit` must be `UG/M3`) |
| `PM10` | `pm10` | — (`UG/M3`) |
| `OZONE` | `o3` | — (`PPB`) |
| `NO2` | `no2` | — (`PPB`) |
| `SO2` | `so2` | — (`PPB`) |
| `CO` | `co` | — (`PPM`) |

These are the hourly concentrations, not AirNow's NowCast; the `*_AQI`
columns are not published. A blank cell, or a unit column that does not
state the expected unit, is `null`. Values arrive to a tenth and are
published unchanged.

Station identity: `airnow:<AQSID>` (nine characters, or twelve with a
three-digit country prefix). `name` is `SiteName`, `agency` is
`DataSource`, the reporting agency. `lat`, `lon` and `elev` are the row's
own; AirNow writes an elevation of `0.0` where it has none (half the
stations, Baku's at −28 m among them), so an exact zero is `null`. A row
without a position, or at 0°, 0°, is dropped and counted as `unplaced`.
The file has no station type, so every station is rank 1. Rows of sites
that report no value (AirNow lists the Japanese Ministry of the
Environment's stations without data) leave no station in the product.

Terms. AirNow data carry no open licence; their use is governed by the
EPA's *AirNow Data Exchange Guidelines* (August 2025,
<https://docs.airnowapi.org/docs/DataUseGuidelines.pdf>), named by the
`license` string `AirNow-DEG`. The conditions this product and any display
of it meet:

- The observations are not fully verified or validated, are subject to
  change and are **preliminary**. A display or product built on them must
  say so (`preliminary: true`), and they are not for regulatory use, trends
  or decision-making; official regulatory data are in the EPA's Air
  Quality System (AQS).
- Credit goes first to the federal, state, local and tribal agencies that
  own the data, and to the EPA AirNow program. The `attribution` string
  carries the programme credit; each station's `agency` names its owner,
  and a display shows it with the station's values.
- Values are not altered and are disseminated as received: no conversion,
  no correction, no clipping of small negatives.
- Users of the data should receive the most current data available; the
  half-hourly round and the 24-hour rewrite window serve that.
- Observed values should be shown in accordance with the AQI and its
  colours where an index is shown; v1 carries no index, and a display that
  derives one follows the EPA's Technical Assistance Document.
- Products relying on the data must be made known to the agencies and the
  AirNow program, and the user's contact details kept current: whoever
  operates a deployment of this product returns the guidelines' form to
  the AirNow Data Management Center.

### 9.2 `hkepd` — Hong Kong Environmental Protection Department

The Hong Kong Environmental Protection Department's air quality
monitoring network: 15 general and 3 roadside stations. `cadence` and
`avgPeriod` are 3600; `preliminary` is true: this is the real-time feed,
ahead of the department's validated records.

Source. `epd-24pc` — `https://www.aqhi.gov.hk/epd/ddata/html/out/24pc_Eng.xml`,
the past 24 hours of every station's hourly concentrations, about 100 KB,
rewritten about ten minutes after each hour. Every round reads the whole
file, so a missed round costs nothing while the next one comes within a
day; there is no other backfill.

Time. Each `PollutantConcentration` carries a `DateTime` in Hong Kong
time (`Sat, 31 Oct 2026 22:00:00 +0800`). It is the **end** of the
averaging hour: the file written at 22:10 HKT already lists 22:00 as its
newest hour, which can only be the hour that has just ended (observed
2026-10-31: `Last-Modified` 14:10:15 UTC, newest `DateTime` 22:00 +0800 =
14:00 UTC). It is published as it stands, in UTC. `lastBuildDate` runs
half an hour ahead of the file and is not an observation time.

Reading. Every concentration is in µg/m³, CO included; `-` is no value:

| EPD element | element | conversion |
|---|---|---|
| `PM2.5` | `pm25` | — |
| `PM10` | `pm10` | — |
| `O3` | `o3` | × 24.04 / 48.00 |
| `NO2` | `no2` | × 24.04 / 46.01 |
| `SO2` | `so2` | × 24.04 / 64.07 |
| `CO` | `co` | × 24.04 / 28 010 |

Station identity: `hkepd:<slug>`, the English station name lower-cased
with every run of other characters a hyphen (`Central/Western` →
`hkepd:central-western`). The XML carries names only; positions come from
the committed table `xuebuild/aq/hkepd_stations.json`, recorded once from
the network's CSDI WFS layer (`csdi:AQMN`, *Air Quality Monitoring Network
of Hong Kong*) with the English names, traditional Chinese names
(`names["zh-Hant"]`), positions, station types and the department's own
station number (`mid`). The layer gives no elevation, so `elev` is
`null`. Causeway Bay, Central and Mong Kok are roadside stations, rank 2;
the others rank 1. `agency` is `EPD`. A station name the table does not
know is dropped and counted as `unplaced`; a new station is a reviewed
change to the table.

Terms. The file is a DATA.GOV.HK resource, reusable free of charge for
commercial and non-commercial purposes under the DATA.GOV.HK Terms and
Conditions (<https://data.gov.hk/en/terms-and-conditions>), named by the
`license` string `DATA.GOV.HK`, on condition that the source is identified
clearly, the Government's and the department's ownership of the
intellectual property is acknowledged, and attribution is given to the
Government, the department and DATA.GOV.HK — the `attribution` string
`Environmental Protection Department, HKSAR Government (DATA.GOV.HK)`,
which a display shows with the data. The data are provided as is.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
