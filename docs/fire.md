# The fire product

An active fire is a point: a satellite pixel hotter than its surroundings,
with a position, a time and a radiative power. A day of the world's fires
is about 180 000 such points from the polar orbiters — a few megabytes,
not a raster — so active fires are a point product published beside the
runs and the station products (`docs/synop.md`). The detections are NASA
FIRMS's near-real-time VIIRS and MODIS products, aggregated every hour by
`xue fire-build` (`xuebuild/fire/`) into a seven-day window and taken live
by the same pointer → immutable directory contract the other point
products use. Xue retrieves nothing itself: no fire detection, no FRP, no
re-gridding. This document is the normative description of that product,
schema v1.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the other point products' pointers.

- `latest-fire.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "fire", "issued": "2026-10-12T05:00:00Z",
   "path": "fire.202610120500/index.json", "byteLength": 61234, "crc32": "1c0ffee5"}
  ```

  `path` is relative to the pointer and always `fire.<round>/index.json`;
  `byteLength` and `crc32` (zlib CRC32 of the file's bytes, eight
  lowercase hex digits) are the index's, so it can be requested as
  `<path>?v=<crc32>` through an immutable cache.
- `fire.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  hour the product was aggregated at, so the minute is always `00`. It
  holds `index.json` (§4), one `<sensor>.jsonl` per sensor (§5) and
  `item.json`, the round's STAC Item (`docs/stac.md` §"Point products").
  Nothing in it changes once the pointer names it. The rounds sit flat
  under the root: each carries the whole window and lives six hours
  (`make prune-r2-fire`, `FIRE_KEEP=6`).
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

Two ways to read a sensor:

- **A time span.** The index's `hours[]` rows (§4) are sorted by sensor,
  then hour, and each ends in the byte `offset` and `length` of that
  sensor-hour's line. The rows of one sensor address consecutive lines, so
  any span of hours of one sensor is one request:
  `GET fire.<round>/<sensor>.jsonl?v=<crc32>` with
  `Range: bytes=<first offset>-<last offset + last length - 1>`. The bytes
  are the lines of that span, separated by `\n` and without the last one.
- **A whole sensor.** Fetch `<sensor>.jsonl` and read it line by line: one
  UTC hour per line, oldest first.

The publisher builds every hour (`publish-fire.yml`). FIRMS refreshes its
files as granules arrive, about three hours after the overpass, so every
round fetches only the last-24-hour files and merges them into the
previous round's window (§5). A source that fails is recorded (§6) and its
sensors keep their history; the round publishes with the rest. The pointer
is withheld only when *no* source answered, in which case the previous
round stays live. A round the pointer already names is finished; the
workflow's `force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A new sensor is a new
entry and a new file, which a v1 reader admits (§7); a `schemaVersion`
bump follows the runs' rule (ship the shell first).

## 2. Detections

Every sensor is converted to one set of per-detection fields:

| key | quantity | encoding | range |
|---|---|---|---|
| `t` | acquisition time | seconds after the line's `hour`, a multiple of 60 | 0 … 3540 |
| `lat` | pixel centre latitude | integer, 1e-4 degrees | −900000 … 900000 |
| `lon` | pixel centre longitude | integer, 1e-4 degrees | −1800000 … 1800000 |
| `frp` | fire radiative power | integer, 0.1 MW | ≥ 0 |
| `bt` | brightness temperature of the fire channel (VIIRS I4 3.74 µm, MODIS band 21/22 3.96 µm) | integer, 0.1 K | 2000 … 6000 |
| `conf` | detection confidence | one character: `l` low, `n` nominal, `h` high | |
| `dn` | day or night overpass | one character: `D` or `N` | |

- VIIRS confidence is FIRMS's own class. MODIS confidence is FIRMS's
  0–100 percentage, classed as FIRMS's documentation does: below 30 `l`,
  30–79 `n`, 80 and above `h`.
- The acquisition time is FIRMS's `acq_date` + `acq_time` (UTC, to the
  minute), the start of the granule, not the pixel's own time.
- A row whose position, time or day/night flag does not parse, or whose
  value falls outside the ranges above, is dropped and counted (§6,
  `rejected`). An `frp` or `bt` that is missing in the CSV drops the row.
- Times in the index are ISO 8601 UTC with a `Z` suffix; times in the
  sensor files are epoch seconds, UTC.

The files are written compactly with ASCII escaping, and a file's identity
is its bytes: the CRC32 in the index, and the pointer's, is of the file
exactly as served. Given the same inputs a round is byte-identical; only
the index's `generated` and the sources' `fetched` depend on when the
build ran.

## 3. Sensors

A sensor is one instrument on one platform. The id is lowercase,
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`. v1 publishes five, in this order:

| id | name | FIRMS file | `satellite` column | resolution |
|---|---|---|---|---|
| `viirs-snpp` | VIIRS S-NPP | `suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv` | `N` | 375 m |
| `viirs-noaa20` | VIIRS NOAA-20 | `noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv` | `N20` | 375 m |
| `viirs-noaa21` | VIIRS NOAA-21 | `noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv` | `N21` | 375 m |
| `modis-terra` | MODIS Terra | `modis-c6.1/csv/MODIS_C6_1_Global_24h.csv` | `T` | 1000 m |
| `modis-aqua` | MODIS Aqua | the same file | `A` | 1000 m |

All files are under `https://firms.modaps.eosdis.nasa.gov/data/active_fire/`
and need no key. The VIIRS columns are
`latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight`;
MODIS names the fire channel `brightness` and the background `bright_t31`,
and gives `confidence` as a number. A row whose `satellite` is not the
sensor's is dropped and counted. A sensor with no detection in the window
is not listed.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-10-12T05:00:00Z",
  "generated": "2026-10-12T05:23:40Z",
  "window": {"start": "2026-10-05T05:00:00Z", "end": "2026-10-12T05:00:00Z"},
  "sensors": [
    {"id": "viirs-noaa20", "name": "VIIRS NOAA-20", "resolution": 375,
     "attribution": "NASA FIRMS (LANCE NRT), VIIRS 375 m C2",
     "license": "NASA-open-data", "url": "https://firms.modaps.eosdis.nasa.gov/",
     "latest": "2026-10-12T02:24:00Z", "count": 381204,
     "file": {"path": "viirs-noaa20.jsonl", "byteLength": 8913420, "crc32": "3b0b746f"}}
  ],
  "hours": [
    [0, "2026-10-05T05:00:00Z", 1834, 412.6, 0, 41377]
  ],
  "sources": [ … ]
}
```

- `issued` is the round, `generated` when the build ran.
- `window` is the span the files hold: `start` is seven days before
  `issued`. A detection is kept when its acquisition time is at or after
  `start`; nothing later is cut, so a granule acquired before the build ran
  but after the round's hour is published.
- `sensors[]` is one entry per sensor with a detection in the window, in
  §3's order: its id, display `name` and nominal `resolution` in metres;
  the `attribution` a display must show, the `license` the data is reused
  under and the product `url`; `latest`, the newest acquisition time in
  the file; `count`, its number of detections; and `file`, the sensor
  file's name (always `<id>.jsonl`), length and CRC32.
- `hours[]` is one row per sensor-hour that has a detection, sorted by
  sensor (its position in `sensors[]`), then hour:

  | 0 | 1 | 2 | 3 | 4 | 5 |
  |---|---|---|---|---|---|
  | `sensor` | `hour` | `count` | `frpMax` | `offset` | `length` |

  `sensor` is the index into `sensors[]`; `hour` the UTC hour the line
  covers; `count` its detections; `frpMax` the largest FRP among them in
  MW, to a tenth; `offset` and `length` the byte span of the line in the
  sensor's file, the JSON object alone. The spans of one sensor are in row
  order, consecutive (each starts one byte — the newline — after the
  previous ends), and lie inside the file's `byteLength`.
- `sources[]` is §6.

The index is a few tens of kilobytes: enough for a reader to pick the
hours it wants and draw a per-hour activity strip without touching the
detections.

## 5. `<sensor>.jsonl`

One line per UTC hour with at least one detection, oldest first, each the
compact JSON object below followed by `\n`:

```
{"sensor": "viirs-noaa20", "hour": 1791090000, "n": 3,
 "t": [1440, 1440, 1500], "lat": [480123, 480131, 471002], "lon": [1191250, 1191262, 1203310],
 "frp": [52, 118, 9], "bt": [3401, 3672, 3104], "conf": "nhl", "dn": "DDD"}
```

- `hour` is epoch seconds of the hour, `n` the number of detections; every
  array and both strings are `n` long, one position per detection.
- Detections are sorted by (`t`, `lat`, `lon`), and unique on that key.
- A line repeats `sensor` and `hour` so a slice read by range identifies
  itself.

A sensor's window is a merge, not a snapshot. Each round lays the newest
24-hour file's detections over the previous round's file, keyed by
(sensor, acquisition time, `lat`, `lon`) in the integer encoding of §2,
the new one replacing the old whole — FIRMS revising a detection is
believed — and drops what is before the window's `start`. The previous
round's file is read only when its CRC32 matches the previous index; a
mismatch starts that sensor's window from the new file alone.

## 6. `sources[]`

One entry per FIRMS file, in §3's order:

```json
{"id": "firms-noaa-20-viirs-c2", "ok": true, "fetched": "2026-10-12T05:23:12Z",
 "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv",
 "reports": 54730, "rejected": 0}
{"id": "firms-modis-c6-1", "ok": false, "fetched": "2026-10-12T05:23:12Z", "error": "request failed for …: HTTP Error 503"}
```

A source's id is `firms-` and FIRMS's directory name with any dot
spelt as a hyphen (`modis-c6.1` → `firms-modis-c6-1`), since a source id
is `^[a-z][a-z0-9-]*$`. `ok: false` always carries `error`. `reports` is the rows read, `rejected`
the rows dropped by §2 and §3.

## 7. Validation

`xuebuild/fire/schema.py` validates every file as it is written, and
`tests/test_fire.py` holds two consecutive rounds built from the fetched
fixture (`tests/fixtures/fire/fire.202610120400/` and `…0500/`) to the
committed golden (`tests/fixtures/fire/expected/`), regenerated by
`tests/prepare_fire_golden.py`; every `hours[]` span is checked against the
sensor file it addresses. Admission is structural: a sensor a reader has
never seen is not an error, and the fields of §2 are held to their ranges.
A reader validates the same shapes on read and rejects a `schemaVersion`
above the one it implements.

## 8. Terms

FIRMS data are NASA Earth science data, free to use and redistribute
without restriction (NASA Earth Science Data and Information Policy).
NASA asks for the acknowledgement: "We acknowledge the use of data and
imagery from LANCE FIRMS operated by NASA's Earth Science Data and
Information System (ESDIS) with funding provided by NASA Headquarters."
The `attribution` string carries the short form, which a display shows
with the data; the STAC Collection carries the full acknowledgement.
Near-real-time detections are not the science-quality archive and are not
replaced by it here.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
