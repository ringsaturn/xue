# The indicators product

Agriculture indicators are crop-area-weighted daily weather quantities over
the main soybean regions of the United States, Brazil and Argentina,
computed from the forecast runs Xue already publishes. They are not a
raster and not a new source: `xue indicators-build`
(`xuebuild/indicators/`) reads each source's newest run through the
public stores, exactly as any outside reader does
([`zarr-profile.md`](zarr-profile.md), "Reading"), aggregates it over each
region with fixed weights, and appends one line per run to a static file.
The product holds physical quantities only: temperatures, precipitation,
area fractions and degree days. This document is the normative
description of that product, schema v1.

## 1. Delivery

Everything is a static file under `indicators/` at the data root.

```text
indicators/
  index.json                         the one mutable entry point (§5)
  features/<source>/<yyyy-mm>.jsonl  one line per run, append-only (§4.1)
  season/<region>.<season>.json      in-season totals, rewritten per GFS run (§4.2)
  context/oni.json                   the Oceanic Niño Index (§6)
  weights/<grid>.zarr/               the crop-area weights, a Zarr v3 store (§7)
```

- `index.json` is served with caching disabled. Every other object is read
  as `<path>?v=<crc32>`, with the CRC-32 (zlib, eight lowercase hex digits)
  the index gives for it.
- A month file only grows. A new run is one line appended to the file of
  its run time's UTC month; bytes already written never change, so every
  offset the index has ever published stays valid. Appending changes the
  file's CRC-32 and so its `?v=`: the new length is a new URL, and an edge
  cache holding the old URL holds a consistent prefix of the new file. A
  reader takes `?v=` from the index it just read; a range past the end of
  a stale cached copy answers 416, which means the index read was older
  than the file.
- **Write order.** Month files, then season files, then `index.json` last.
  An object exists before anything names it.
- **Reading one run.** The index row for a run gives `offset` and `length`
  into its month file; `Range: bytes=<offset>-<offset + length − 1>`
  returns exactly that run's line, its newline included, the same way the
  point products' JSONL is read (`docs/contribution/point-products.md`).
  A whole month file is newline-delimited JSON, one run per line in the
  order they were appended.
- **Serialisation.** Every object is compact JSON with ASCII escapes, keys
  in the order this document lists them, so a file's CRC-32 is a function
  of its content (`pointproduct.encode_json`). A line is that encoding
  followed by `\n`.
- The product is kept forever; nothing prunes it.

## 2. Regions

Three country composites and thirteen sub-regions. A region's weight on a
grid cell is the soybean physical area on that cell inside the region's
admin-1 units (§7). Local days are taken at a **fixed** UTC offset per
region, never a time-zone table: the boundary does not move with daylight
saving (in the United States summer, local civil midnight is 05:00 UTC,
the indicators' day begins at 06:00 UTC).

| id | name | ISO 3166-2 units | UTC offset |
|---|---|---|---|
| `us-soy` | United States soybean belt | US-IA, US-IL, US-MN, US-IN, US-NE, US-OH, US-MO, US-SD, US-ND, US-KS, US-WI, US-MI, US-AR | −6 |
| `us-ia` `us-il` `us-mn` `us-in` `us-ne` | Iowa, Illinois, Minnesota, Indiana, Nebraska | one each | −6 |
| `br-soy` | Brazil soybean states | BR-MT, BR-PR, BR-RS, BR-GO, BR-MS, BR-MG, BR-BA, BR-MA, BR-TO, BR-PI | −3 |
| `br-mt` `br-pr` `br-rs` `br-go` `br-ms` | Mato Grosso, Paraná, Rio Grande do Sul, Goiás, Mato Grosso do Sul | one each | −3 |
| `ar-soy` | Argentina soybean provinces | AR-B, AR-X, AR-S, AR-E, AR-L | −3 |
| `ar-ba` `ar-cb` `ar-sf` | Buenos Aires, Córdoba, Santa Fe | one each | −3 |

The index's `regions[]` lists each with `id`, `name`, `country` (ISO
3166-1 alpha-2), `subdivisions`, `utcOffsetHours` and `parent` (the
composite a sub-region belongs to, `null` on a composite).

## 3. Crop calendar

Each region has a **season** window, over which §4.2 accumulates, and a
**flowering** window inside it (flowering and pod set). Windows are
month-day ranges, inclusive at both ends. A season inside one calendar
year is named by its year (`2027`); one that runs over the new year by
both (`2026-27`).

| regions | season | flowering |
|---|---|---|
| `us-*` | 05-01 → 09-30 | 07-01 → 08-31 |
| `br-soy` | 10-01 → 03-31 | 11-15 → 02-28 |
| `br-mt` | 10-01 → 03-31 | 11-15 → 01-31 |
| `br-pr`, `br-go`, `br-ms` | 10-01 → 03-31 | 12-01 → 02-15 |
| `br-rs` | 10-01 → 03-31 | 12-15 → 02-28 |
| `ar-*` | 10-15 → 04-30 | 01-01 → 02-28 |

Sources: USDA NASS Agricultural Handbook 628 (United States planting and
harvest dates), CONAB crop progress (Brazil), Bolsa de Comercio de Rosario
and Bolsa de Cereales de Buenos Aires (Argentina). The index publishes the
table as `calendar` with its `version` (§8).

## 4. Features

### 4.1 One run: `features/<source>/<yyyy-mm>.jsonl`

Sources: `gfs`, `ecmwf`, `aifs` (0.25° grid) and `ifshres` (0.1° grid),
each read from its live pointer. Inputs are the `tmp2m` (°C) and `prate`
(mm/h) bundles. A temperature frame is an instant; a rate frame is taken to
hold over the step that ends at it (`(previous offset, offset]`, the first
from the run time). GFS's rate is instantaneous and the others' a mean
over that step; the indicators treat both alike.

A line:

```json
{"schemaVersion":1,"source":"gfs","run":"2026100806","runTime":"2026-10-08T06:00:00Z",
 "manifest":{"path":"gfs.2026100806/manifest.json","crc32":"1a2b3c4d"},
 "weights":{"version":"1","grid":"0p25","crc32":"…"},"calendar":{"version":"1"},
 "horizon_days":10.0,"frame_step_hours":[[120,1],[240,3]],
 "regions":{"us-soy":{"days":[{"date":"2026-10-08","t2m_mean":12.31,…,"complete":true},…],
                      "summary":{"days":9,"precip_sum":14.2,"gdd_sum":21.07,"hot35_days":0.0}},…}}
```

| field | meaning |
|---|---|
| `source`, `run`, `runTime` | the run, `run` as `YYYYMMDDHH` |
| `manifest` | the run's manifest path under the data root and the `?v=` it was read at (`null` for a run read by name, which has no pointer) |
| `weights` | the weights file's version, grid and CRC-32 |
| `calendar` | the calendar version |
| `horizon_days` | the last temperature frame's lead, in days |
| `frame_step_hours` | the temperature axis as `[through_hour, step_hours]` segments: the daily maximum and minimum are over these frames, not true extremes |
| `regions` | per region present in the weights, in §2 order: `days` and `summary` |

Each day, one object per local calendar date the run's temperature frames
reach:

| field | unit | definition |
|---|---|---|
| `date` | — | the local date, `YYYY-MM-DD` |
| `t2m_mean` | °C | mean over the day's frames of the area-weighted 2 m temperature |
| `t2m_max` | °C | area-weighted mean of each cell's highest 2 m temperature among the day's frames |
| `t2m_min` | °C | area-weighted mean of each cell's lowest 2 m temperature among the day's frames |
| `precip` | mm | area-weighted precipitation total over the local day |
| `dry_frac` | 1 | crop-area fraction whose daily precipitation is below 1 mm |
| `hot30_frac` | 1 | crop-area fraction whose daily highest 2 m temperature is above 30 °C |
| `hot35_frac` | 1 | crop-area fraction whose daily highest 2 m temperature is above 35 °C |
| `gdd` | °C d | growing degree days, `max(0, min(t2m_mean, 30) − 10)` |
| `complete` | boolean | every frame the source's axis places in the day is present |

The operation order is part of the output:

1. A temperature frame belongs to the local day containing its valid
   time; a frame exactly at local midnight opens the new day.
2. Per day, in axis order: each frame's area-weighted mean `Σ w·T`
   (float64); `t2m_mean` is the plain mean of those; per cell the maximum
   and minimum over the day's frames, then their area-weighted means;
   the hot fractions are the weight of the cells whose daily maximum
   exceeds the threshold.
3. Each rate frame's step is split over the local days it overlaps in
   proportion to the seconds in each, `rate × hours` per cell, accumulated
   in axis order; `precip` is its area-weighted mean and `dry_frac` the
   weight of the cells below 1 mm.
4. `gdd` comes from the unrounded `t2m_mean` (base 10 °C, cap 30 °C).
5. Every number is rounded once, at the end, to two decimals.

`complete` is true when the run starts at or before the day, every
temperature frame the source's published axis places in `[start, end)` is
present, and every rate frame it places in `(start, close]` is present,
where `close` is the first axis offset at or after the day's end. The first
and last days of a run are usually incomplete; a reader filters on it.

`summary` sums the first 14 complete days: `days` (how many there were),
`precip_sum`, `gdd_sum` and `hot35_days` (the sum of `hot35_frac`), from
the rounded daily values.

A line stores raw values only. The change between two runs for the same
date is the reader's subtraction of two lines.

### 4.2 In-season totals: `season/<region>.<season>.json`

From GFS only, rewritten whenever a GFS run is added. Each local day of the
season is taken from the newest GFS run that started at or before the day
began and has that day `complete`: normally the cycle within six hours
before it, the first-day forecast standing in for an analysis. When that
cycle is missing an older one supplies the day and the day is `partial`. A
day is counted once the newest indexed GFS run is that cycle or a later
one, and is replaced only by a newer qualifying run.

| field | meaning |
|---|---|
| `region`, `season`, `source` | the file's identity; `source` is `gfs` |
| `calendar`, `weights`, `windows` | versions, and the region's `season` and `flowering` windows as `["MM-DD", "MM-DD"]` |
| `throughRun` | the newest GFS run the file reflects |
| `days_counted`, `days_partial` | rows in `daily`, and how many are partial |
| `precip_total` (mm), `gdd_total` (°C d), `hot35_days` | sums over `daily` (`hot35_days` sums `hot35_frac`) |
| `cdd_current`, `cdd_max` | consecutive days with `precip` below 1 mm, at the last row and the longest; a date missing from `daily` ends a spell |
| `days_counted_window`, `precip_window`, `hot35_days_window` | the same over the rows inside the flowering window |
| `daily` | one row per counted date: `date`, `run`, `lead_hours` (from the run to the day's start), `partial`, `flowering`, then the §4.1 day fields |

The series exists from the day the product went live; no source used here
is archived, so it is never backfilled.

## 5. `index.json`

| field | meaning |
|---|---|
| `schemaVersion` | `1` |
| `product` | `indicators` |
| `note` | a fixed sentence: what the numbers are and that no agency issued them |
| `attribution` | `{name, data, license}` per upstream (§9) |
| `regions` | §2 |
| `features` | `{id, unit, definition}` per day field, the table of §4.1 |
| `calendar` | `{version, source, regions: {<id>: {season, flowering}}}`, §3 |
| `weights` | `{version, spam, files: [{grid, path}]}` |
| `sources` | per source: `id`, `grid`, `cadence`, `latestRun` (the newest indexed run, or `null`), `ok`, `error` (`null` when ok) |
| `files` | every published object but the index and the weights, sorted by path: `{path, byteLength, crc32}`, and for a month file also `runs: [{run, offset, length}]`, which tile the file from offset 0 |

A source that fails (an unreadable pointer, manifest, store or weights
file) reports `ok: false` with its error and adds nothing; the others go
on. A run already in `files` is not computed again.

## 6. `context/oni.json`

NOAA CPC's Oceanic Niño Index (`oni.ascii.txt`): the three-month running
mean of the Niño 3.4 sea surface temperature (`total`, °C) and its
departure from CPC's moving 30-year base (`anomaly`, °C).

`{schemaVersion, id: "oni", name, unit, source: {name, url, license},
fetches: [{fetched, rows, crc32}], rows: [{season, year, total,
anomaly}]}`. `rows` is the newest table whole, since CPC revises its last
months; `fetches` records every fetch that changed it, with the CRC-32 of
the text as served. `xue indicators-context` refreshes it.

## 7. Weights: `weights/<grid>.zarr/`

One Zarr v3 group per grid (`0p25` for GFS, ECMWF and AIFS, `0p1` for IFS
HRES, `t126` for CFSv2), built once offline from SPAM 2020 v2r2 soybean
physical area (`SOYB_A`) and Natural Earth admin-1 polygons, with exact
area overlap between the SPAM cells and the published cells. Any Zarr v3
client opens it (`xr.open_zarr(url, consolidated=False)`); the build reads
it with NumPy alone, as below.

```text
<grid>.zarr/
  zarr.json            group; the attributes below
  weights/zarr.json    float32 [region, lat, lon]
  weights/c/<r>/0/0    region r's shard
  region/  lat/  lon/  coordinates, one chunk each (c/0)
```

Group attributes:

| attribute | meaning |
|---|---|
| `version` | the weights version (§8) |
| `spam` | the SPAM release and layer, `SPAM 2020 v2r2 SOYB_A` |
| `grid` | the grid id |
| `regions` | region id → `{"rows": [row0, row1], "cols": [col0, col1]}`, the half-open box of the region's non-zero cells |
| `lon_convention` | how the grid points are laid out, in words: `Grid points at cell centres: longitudes ascend from -180, latitudes run north to south.` on the global grids |

Arrays:

- `weights`: `float32`, shape `[regions, ny, nx]` over the **whole**
  published grid, zero outside the region; each region sums to 1.
  `dimension_names` `["region", "lat", "lon"]`, `fill_value` 0. One
  `sharding_indexed` codec with one shard per region: the outer chunk is
  `[1, ⌈ny/180⌉·180, ⌈nx/180⌉·180]` (`[1, 900, 1440]` for `0p25`; Zarr
  requires whole inner chunks, and the cells past the grid are padding),
  the inner chunk `[1, 180, 180]` under `[bytes little-endian, zstd]` with
  no content checksum, and the index at the end under `[bytes
  little-endian, crc32c]`, as in [`zarr-profile.md`](zarr-profile.md).
  Inner chunks that are all zero are never written (the index pair
  `2^64 − 1`) and read as zero.
- `region`: the region ids in the order of the `weights` axis, Zarr's
  `fixed_length_utf32` (what zarr-python writes for a NumPy `<U6`).
- `lat`, `lon`: `float64` cell centres. Rows and columns are the bundle
  stores' own: `lon[i] = firstLongitude + i · longitudeStep`, `lat[j] =
  firstLatitude + j · latitudeStep`, north to south. `t126` is the Gaussian
  grid taken as equidistant, as its bundles describe it.

Reading one region: the two documents and the coordinates, then the
region's shard index as the suffix range `bytes=-N` (CRC-32C checked),
then only the inner chunks overlapping its box, neighbouring ones merged
into one range. A reader holds the box to summing to 1 (so nothing lies
outside it) and applies the weights only to a bundle store whose grid has
the same shape and cell centres. The `weights.crc32` a line records is the
CRC-32 of the group and array documents followed by every region's boxed
weights as read (float32, little-endian, in axis order).

## 8. Versions

- `schemaVersion` is the layout of this document. A field added within it
  is a widening: readers ignore what they do not know, old lines are not
  rewritten.
- `calendar.version` changes with any window; `weights.version` with any
  weight. Every line and season file records the versions it was computed
  under, and lines already written are never recomputed under new ones.
- A line is reproducible: the same run, weights and calendar give the same
  bytes (`xue indicators-build --force` checks this instead of rewriting).

## 9. Validation and vocabulary

Before anything is written, the index, every line and every season file
are validated (`xuebuild/indicators/schema.py`): temperatures −60 … 60 °C,
daily precipitation 0 … 500 mm, fractions 0 … 1, degree days 0 … 20,
`t2m_min ≤ t2m_mean ≤ t2m_max`, `hot35_frac ≤ hot30_frac`, dates
ascending, the run spans tiling each month file. A line that fails is
dropped whole and its source reports the error.

### Words that never appear

The product describes the weather over farmland and nothing else. No key
and no string value in any file contains, as a word, `signal`, `alert`,
`warning`, `watch`, `bull`, `bear`, `long`, `short`, `price`, `contract`,
`ticker`, `trade`, `hedge`, `position` or `yield` (nor their plurals), and
the files carry no market data, no company and no direction. Thresholds
are named by their numbers (`hot35_frac`), never by a level of concern.
The validators enforce the list on every write.

## 10. Attribution

The index carries the credit for each upstream:

- NOAA NCEP — GFS forecasts; public domain (U.S. Government work).
- ECMWF — IFS and AIFS open data; CC BY 4.0.
- Open-Meteo — ECMWF IFS HRES as forwarded by Open-Meteo; CC BY 4.0.
- IFPRI — SPAM 2020 v2r2, doi:10.7910/DVN/SWPENT; CC BY 4.0.
- Natural Earth — admin-1 boundaries; public domain.
- NOAA CPC — Oceanic Niño Index; public domain.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
