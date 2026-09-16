# The airport product

An airport's weather is a line of text: a METAR every half hour, a TAF
every six. Five thousand of them are a few megabytes of JSON, not a
raster, so the airports are a third product published beside the runs and
beside the tropical cyclones (`docs/tc.md`) — aggregated every ten minutes
by `xue airport-build` (`xuebuild/airport/`) from the NOAA Aviation Weather
Center's decoded cache files, and taken live by the same pointer →
immutable directory contract the runs use. This document is the normative
description of that product, schema v1. The design it implements is in the
project's private notes; what is here is the contract a reader can hold the
publisher to.

## 1. Delivery

Everything is a static file under the data root, the same root as the run
pointers (`latest.json`, `latest-<model>.json`), `latest-tc.json` and
`showcase.json`.

- `latest-airport.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "airport", "issued": "2026-09-16T14:40:00Z",
   "path": "airport.202609161440/index.json", "byteLength": 594322, "crc32": "9d70a57b"}
  ```

  `path` is relative to the pointer and always
  `airport.<round>/index.json`; `byteLength` and `crc32` (zlib CRC32 of the
  file's bytes, eight lowercase hex digits) are the index's, so it can be
  requested as `<path>?v=<crc32>` through an immutable cache.
- `airport.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the product was aggregated at, always a multiple of ten. It holds
  `index.json` (§4) and `history.jsonl` (§5), and nothing in it changes
  once the pointer names it. Every file is addressed by its CRC, so a cache
  never serves one round's bytes under another's key. Directories are
  pruned after three hours (`make prune-r2-airport`, `AIRPORT_KEEP=18`);
  nothing reads an older round, because each round carries the whole
  history.
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

Two ways to read the history, which is why it is one file:

- **One airport.** Take the station's row in the index, which ends in the
  byte `offset` and `length` of its line, and issue
  `GET airport.<round>/history.jsonl?v=<crc32>` with
  `Range: bytes=<offset>-<offset + length - 1>`. The bytes that come back
  are that station's JSON object exactly — the span stops before the
  newline — so they parse on their own. One request, a few kilobytes.
- **Everything.** Fetch `history.jsonl` and read it line by line: one
  station per line, sorted by ICAO id, each a complete JSON object. No
  listing, no thousand small objects, and the file streams.

The publisher builds every ten minutes (`publish-airport.yml`). A source
that is late or unreachable is recorded (see `sources`) and the round
publishes without it. The pointer is withheld only when *both* the METARs
and the TAFs failed, in which case the previous round stays live. A round
the pointer already names is finished: a run that resolves to it (a
dispatch landed in the same ten minutes, or the schedule drifted) builds
nothing, and the workflow's `force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so a
shell that does not know it never asks for it. A `schemaVersion` bump
follows the runs' rule (ship the shell first); an added optional field does
not bump the version.

## 2. Units and conventions

SI throughout, converted at the parser:

| quantity | unit | note |
|---|---|---|
| temperature, dew point (`t`, `td`) | °C, to a tenth | as the service decodes them |
| wind speed and gust (`ws`, `gust`) | m/s, to a tenth | knots × 0.514444 |
| wind direction (`wd`) | degrees true, 0–360 | integer; `null` when the direction is variable |
| visibility (`vis`) | metres, to a hundred | statute miles × 1609.344 |
| pressure (`qnh`, `slp`) | hPa, to a tenth | `qnh` is the altimeter setting, inHg × 33.8639; `slp` the reported sea-level pressure |
| cloud base (`cloud`) | metres above ground, to ten | feet × 0.3048 |
| elevation (`elev`) | metres | from the station table where there is one |
| time | ISO 8601, UTC, `Z` suffix | |

`vis` has one convention: the service codes "ten kilometres or more" as
`6+`, `10+` or `P6SM` rather than a distance, and all of those are written
as exactly **`10000`**. A `vis` of 10000 therefore means *at least* ten
kilometres, not ten kilometres. Every other value is the reported distance
converted, which is why a report of 7 statute miles reads 11300 — a
measured 11.3 km, not the coded ceiling.

`wd` has one too. A **variable** direction (`VRB04KT`, and a TAF group's
`VRB`) is `null` with the speed kept: the direction is not known, and
writing the zero the CSV decodes it to would publish a north wind. `wd: 0`
therefore means due north, and `wd: 0` with `ws: 0` is calm.

`qnh` comes from the altimeter setting in inches of mercury, which is
itself a conversion of the `Qnnnn` hectopascals most of the world reports;
it can differ from the hectopascals in `raw` by a tenth. `raw` is the
report verbatim and is the authority.

`cloud` is `[[cover, base], …]` in the order reported, at most four layers,
with `cover` the three-letter code as it arrives (`FEW`, `SCT`, `BKN`,
`OVC`, `OVX`, and in a TAF also `NSC`, `SKC`, `CAVOK`). A layer that
reports no base — the sky-clear and no-significant-cloud codes, and an
obscured sky whose vertical visibility is in `raw` — has `base: null`.

Numbers are JSON numbers; a value not reported is `null`. A value outside
the range the contract admits is *also* `null`: the service codes some
unknowns as numbers (`-99.99` for a position it does not have), and one bad
cell must not cost a round. A report with no usable position at all is not
published, since the product could not place it.

The files are written compactly with ASCII escaping — a history line is one
such object and carries no whitespace of its own — and a file's identity is
its bytes: the CRC32 in the index, and the pointer's, is of the file
exactly as served. Given the same inputs, a round is byte-identical;
nothing in it is a function of when the build ran except the index's
`generated`.

## 3. Identity

A station is its ICAO id, uppercase (`RJTT`, `KABQ`, `ZBAA`). Nothing is
inferred: a station the station table does not know still enters the
product under the id its report carries, with a null name and the report's
own position. The id is the key everywhere — the index's rows and the
history's lines are both sorted by it, and each history line repeats it as
`icao` so a slice read by range identifies itself.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-09-16T14:40:00Z",
  "generated": "2026-09-16T14:46:20Z",
  "history": {"path": "history.jsonl", "byteLength": 114742, "crc32": "c34fab6d"},
  "stations": [
    ["RJTT", 35.553, 139.781, 5.0, "2026-09-16T14:30:00Z", 21.0, 20.0, 20, 6.7, null, 7000, 1016.9, "MVFR", 1, 76142, 1633]
  ],
  "sources": [ … ]
}
```

- `issued` is the round, `generated` when the build ran.
- `history` names the file beside the index, its length and the CRC32 of
  the whole file — the `?v=` a reader requests it under.
- `stations[]` is one compact row per station, sorted by ICAO id and
  unique, in this order:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
  |---|---|---|---|---|---|---|---|
  | `icao` | `lat` | `lon` | `elev` | `obsTime` | `t` | `td` | `wd` |

  | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
  |---|---|---|---|---|---|---|---|
  | `ws` | `gust` | `vis` | `qnh` | `category` | `tafPresent` | `offset` | `length` |

  The values from index 4 to 12 are the station's **newest** observation;
  the whole of it, and its 23 hours of predecessors, are in the history
  file. `category` is the service's flight category (`VFR`, `MVFR`, `IFR`,
  `LIFR`) or `null`; `tafPresent` is 1 when the station has a current TAF,
  else 0. `offset` and `length` are the byte span of the station's line in
  `history.jsonl`, the JSON object alone: the spans are in row order, they
  do not overlap, and each lies inside `history.byteLength`.
- `sources[]` is §6.

The index is what a map layer reads: about 600 KB for five thousand
stations, every position and current condition, and no history.

## 5. `history.jsonl`

One line per station, sorted by ICAO id, each the compact JSON object
below followed by `\n`:

```
{"icao": "RJTT", "name": "Tokyo/Haneda Intl" | null,
 "lat": 35.553, "lon": 139.781, "elev": 5.0,
 "iata": "HND" | null, "wmo": "47671" | null,
 "metars": [ Metar, … ],          — the last 24 hours, newest first
 "taf":    Taf | null}            — the current forecast

Metar  = { time, raw, t, td, wd, ws, gust, vis, qnh, slp, wx, cloud, category, auto, type }
Taf    = { issued, from, to, raw, amended, periods: [ Period, … ] }
Period = { from, to, change, prob, wd, ws, gust, vis, wx, cloud }
```

Metar. `time` is the observation time, and the entries are strictly
decreasing in it: `metars[0]` is the newest. `raw` is the report as
transmitted. `wx` is the present-weather string verbatim (`-SHRA BR`) or
`null`; `auto` says the report came from an automatic station; `type` is
`METAR` or `SPECI` (an off-schedule special). A station in the product has
at least one observation in the window; when its last one falls out of the
24 hours, the station leaves the index and the history until it reports
again.

Taf. `issued` is the bulletin's issue time, `from` and `to` its validity,
`amended` true for an amendment or a correction (`AMD` or `COR` in the
bulletin's remarks). `periods` are the decoded groups sorted by `from` and
then `to`. `change` is the group's change indicator — `FM`, `BECMG`,
`TEMPO`, `PROB` — and is `null` on the forecast's first, prevailing group;
a `PROB40 TEMPO` group is `change: "TEMPO"` with `prob: 40`. A period
carries only what its group states: a field the group does not restate is
`null` and the reader inherits it from the prevailing group, as the TAF
itself is read. The `BECMG` completion time is not carried in v1.

A station's history is a merge, not a snapshot. Each round lays that
round's observations over the previous round's history keyed by (station,
observation time), the newer report winning — so a correction replaces what
it corrects — and drops what is older than 24 hours before the round. A
station that did not report keeps its history untouched.

Only stations that have observed are in the product: a TAF for a station
with no observation in the window is not published, since there would be
nothing to hang it on.

## 6. `sources[]`

One entry per source, in build order:

```json
{"id": "awc-metars", "ok": true, "fetched": "2026-09-16T14:45:32Z",
 "url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz", "reports": 5189}
{"id": "awc-tafs", "ok": false, "fetched": "2026-09-16T14:45:32Z", "error": "request failed for …: HTTP Error 503"}
```

`ok: false` always carries `error`. `reports` is what the source
contributed: METARs read, TAFs read, stations in the table.

Source ids in v1, all from the Aviation Weather Center's cache
(<https://aviationweather.gov/data/cache/>), a work of the United States
government and in the public domain:

- `awc-metars` — `metars.cache.csv.gz`, the world's decoded METARs of the
  last ninety minutes, rewritten every minute.
- `awc-tafs` — `tafs.cache.xml.gz`, every current TAF, rewritten every ten
  minutes.
- `awc-stations` — `stations.cache.json`, the station table (name, IATA and
  WMO identifiers, position, elevation), rewritten daily and fetched at
  most once a day with `If-Modified-Since`. When the request fails over a
  copy already on disk the source stays `ok` with the error noted: a
  day-old table names the same airports.

The service asks for at most a hundred requests a minute and a custom
`User-Agent` on every request; the publisher sends
`xue/<version> (+https://github.com/ringsaturn/xue)` and makes three
requests per round. The observations themselves are the world's
meteorological services', exchanged under WMO arrangements and
redistributed by NOAA/NWS; attribution names both.

## 7. Validation

`xuebuild/airport/schema.py` validates every file as it is written, and
`tests/test_airport.py` holds two consecutive rounds built from the fetched
fixture (`tests/fixtures/airport/airport.202609161430/` and `…1440/`) to
the committed golden (`tests/fixtures/airport/expected/`), regenerated by
`tests/prepare_airport_golden.py`; every index row's span is checked
against the history file it addresses. Admission is structural: a station
id, a sky cover or a weather string a reader has never seen is not an
error. A reader validates the same shapes on read and rejects a
`schemaVersion` above the one it implements.
