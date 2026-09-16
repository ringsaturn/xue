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
   "path": "airport.202609161440/index.json", "byteLength": 591100, "crc32": "9d70a57b"}
  ```

  `path` is relative to the pointer and always
  `airport.<round>/index.json`; `byteLength` and `crc32` (zlib CRC32 of the
  file's bytes, eight lowercase hex digits) are the index's, so it can be
  requested as `<path>?v=<crc32>` through an immutable cache.
- `airport.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the product was aggregated at, always a multiple of ten. It holds
  `index.json` and nothing else. Directories are pruned after three hours
  (`make prune-r2-airport`, `AIRPORT_KEEP=18`); nothing reads an older
  round, because the history is not in the round.
- `airport-shards/<XX>-<crc32>.json` is where the history is. A shard
  carries every station whose ICAO id begins with `XX` (§3), with its last
  24 hours of observations and its current forecast. The file name carries
  the CRC32 of the file's own bytes, so **the name is the version**: a
  round that changes forty shards writes forty new objects and names the
  others, unchanged, by the names they already have. A shard is requested
  by the `path` its index gives, which already carries the CRC; no `?v=`
  is needed and appending one is harmless.

  This is the one extension of the delivery contract. "Everything the
  pointer names is immutable" still holds; what no longer holds is that
  every immutable object lives inside the directory the pointer names. The
  consequences are two:

  - **Reading.** A reader resolves a shard's `path` relative to the
    index's URL (`../airport-shards/…`), exactly as it resolves a run's
    artifacts relative to its manifest.
  - **Recycling.** `make prune-r2-airport-shards` deletes the shard
    objects that no index among the newest `AIRPORT_KEEP` rounds names any
    more, and refuses to delete anything if the bucket listing or any of
    those indexes cannot be read. A reader holding an index older than
    that may ask for a shard that has been recycled and get a 404; it
    should then re-read the pointer, the way a stale tab re-reads a
    manifest.

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

The files are written compactly with ASCII escaping, and a file's identity
is its bytes: the CRC32 in the index — and, for a shard, in its own name —
is of the file exactly as served. Given the same inputs, a round is
byte-identical; nothing in it is a function of when the build ran except
the index's `generated`.

## 3. Identity and sharding

A station is its ICAO id, uppercase (`RJTT`, `KABQ`, `ZBAA`). Nothing is
inferred: a station the station table does not know still enters the
product under the id its report carries, with a null name and the report's
own position.

Which shard a station is in follows from the id alone:

- the first **two** characters, normally (`RJTT` → `RJ`, `ZBAA` → `ZB`);
- the first **three** when the id begins with `K` (`KABQ` → `KAB`), since
  the contiguous United States is about half of every round and a single
  `K` shard would be rewritten by every observation anywhere in it.

A round names about a thousand shards; the shards a reader wants are the
ones its stations are in, and the index (§4) says where every station is
by saying which shards exist. A reader that wants one airport fetches the
index and one shard.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-09-16T14:40:00Z",
  "generated": "2026-09-16T14:46:20Z",
  "shards": {
    "RJ": {"path": "../airport-shards/RJ-73f8249f.json", "byteLength": 19419, "crc32": "73f8249f"}
  },
  "stations": [
    ["RJTT", 35.553, 139.781, 5.0, "2026-09-16T14:30:00Z", 21.0, 20.0, 20, 6.7, null, 7000, 1016.9, "MVFR", 1]
  ],
  "sources": [ … ]
}
```

- `issued` is the round, `generated` when the build ran.
- `shards` is keyed by shard (§3). Each entry names the file, its length
  and its CRC32, and `path` is always
  `../airport-shards/<shard>-<crc32>.json` with the same CRC32 the entry
  carries — an index cannot point at bytes it did not measure.
- `stations[]` is one compact row per station, sorted by ICAO id and
  unique, in this order:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|---|
  | `icao` | `lat` | `lon` | `elev` | `obsTime` | `t` | `td` | `wd` | `ws` | `gust` | `vis` | `qnh` | `category` | `tafPresent` |

  The values from index 4 on are the station's **newest** observation; the
  whole of it, and its 23 hours of predecessors, are in the shard.
  `category` is the service's flight category (`VFR`, `MVFR`, `IFR`,
  `LIFR`) or `null`; `tafPresent` is 1 when the shard carries a current
  TAF for the station, else 0. A row's station belongs to a shard the
  index names.
- `sources[]` is §6.

The index is what a map layer reads: about 600 KB, every station's position
and current conditions, and no history.

## 5. `airport-shards/<XX>-<crc32>.json`

```
{
  schemaVersion: 1,
  shard: "RJ",
  stations: {
    "RJTT": {
      name: "Tokyo/Haneda Intl" | null,
      lat, lon, elev,
      iata: "HND" | null,
      wmo: "47671" | null,
      metars: [ Metar, … ],          — the last 24 hours, newest first
      taf:    Taf | null             — the current forecast
    }
  }
}

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
24 hours, the station leaves until it reports again.

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
round's observations over the previous round's shard keyed by (station,
observation time), the newer report winning — so a correction replaces what
it corrects — and drops what is older than 24 hours before the round. A
station that did not report keeps its history untouched, and its shard
keeps its name.

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
`tests/prepare_airport_golden.py`. Admission is structural: a station id, a
shard key, a sky cover or a weather string a reader has never seen is not
an error. A reader validates the same shapes on read and rejects a
`schemaVersion` above the one it implements.
