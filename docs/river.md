# River flow in a showcase case

A flood is told by its rivers: how much water each reach carried, hour by
hour, against what it usually carries, and what the gauges on it measured.
A showcase case may carry that beside its weather field, in a `river/`
directory of its own named by the case's `river` block
([`showcase/README.md`](../showcase/README.md)). There are two kinds. A
**network** case has a hydrological model's output on a river network —
one value per reach per frame, the National Water Model on the Guadalupe
in July 2025 — and publishes the reaches as GeoParquet and each track as a
[network store](zarr-profile.md#network-store). A **grid** case has its
model's discharge as the case's own gridded field (EFAS on the Valencia
DANA of October 2024, built by the ordinary case pipeline), and its
`river/` directory carries only what the field does not: the gauges'
observed series, their reported peaks and the mapped flood extents. Xue
models nothing itself; it carries what the model, the gauge operators and
the mapping service published. This document is the normative description
of that directory, version 1.

## 1. Delivery

Everything is a static file inside the case's directory,
`showcase/<id>/river/`, built once (`python -m xuebuild.river build
showcase/river-cases/<id>.json`) and never rewritten.

- `index.json` is the directory's entry point (§2). The catalog row's
  `river` block names it and carries its `byteLength` and `crc32` (zlib
  CRC-32 of its bytes, eight lowercase hex digits), so it is requested as
  `river/index.json?v=<crc32>`.
- Every other path is in the index, relative to it: no leading `/`, no
  `..`, no `.` component. Request each as `<path>?v=<crc32>` with the CRC
  the index carries; a store's CRC is that of its group `zarr.json`
  ([`zarr-profile.md`](zarr-profile.md), "Attributes") and is the `?v=` of
  every object in the store.
- A network case holds `reaches.parquet` (the GeoParquet), one
  `<track id>.zarr` per track, `gauges.jsonl` and `index.json`; a grid
  case `gauges.jsonl`, an `extent/` directory of GeoJSON files and
  `index.json`.

## 2. `index.json`

```json
{"version": 1, "kind": "network",
 "geometry": {"path": "reaches.parquet", "byteLength": 1114663, "crc32": "de943f65",
              "featureCount": 10940, "rowGroupRows": 4096},
 "tracks": [{"id": "analysis", "kind": "analysis", "runTime": "2025-07-03T00:00:00Z",
             "store": {"path": "analysis.zarr", "byteLength": 286352, "crc32": "499f2cb1"}},
            {"id": "short_range-2025070409", "kind": "forecast", "runTime": "2025-07-04T09:00:00Z",
             "store": {"path": "short_range-2025070409.zarr", "byteLength": 173890, "crc32": "3cfe6713"}}],
 "gauges": [{"id": "08165500", "name": "Guadalupe River at Hunt, TX", "lon": -99.321712, "lat": 30.069931,
             "source": "USGS NWIS", "unit": "m3/s", "feature": 5366,
             "series": {"path": "gauges.jsonl", "offset": 0, "length": 30080},
             "peaks": [{"time": "2025-07-04T10:05:00Z", "value": 8919.8, "note": "USGS, estimated"}]}],
 "extent": [],
 "credits": ["NOAA National Water Model v3.0 (NOAA Open Data Dissemination, noaa-nwm-pds)", "…"],
 "disclaimer": "National Water Model output is a simulation, not an observation or a warning. …"}
```

- `version` is `1`; a reader refuses any other. `kind` is `network` or
  `grid`. No key outside those this section names may appear.
- `geometry` (network only, required there): the GeoParquet
  ([`zarr-profile.md`](zarr-profile.md), "Network store"): its `path`
  (a `.parquet` file), `byteLength` and `crc32`, its row count
  `featureCount` and its row group length `rowGroupRows`. Every store of
  the case names the same file with the same four numbers.
- `tracks` (network only, required there, non-empty): one network store
  each, in the order the viewer offers them. `id` is a unique slug
  (lowercase letters, digits, `_`, `-`, `.`) and the store is
  `<id>.zarr` by convention; `kind` is `analysis` (the model's best
  estimate of what happened, one window) or `forecast` (one cycle); its
  `runTime` equals the store's own `runTime` — the cycle of a forecast, the
  first frame of an analysis. A forecast's frames start where its product
  does (the NWM short range at +1 h). `store` is `{path, byteLength,
  crc32}`: the store's path (a `.zarr` directory), the sum of its objects'
  lengths and the CRC-32 of its group document.
- `gauges` (optional, default empty): observation points, each with a
  unique `id` (the operator's own), a `name`, `lon` and `lat` (WGS84
  degrees), the `source` operator, the `unit` of its values (`m3/s`), and:
  - `feature` (network only, required there): the GeoParquet row of the
    reach the gauge sits on, which is also the store's feature index.
    A grid gauge has none: the viewer samples the case's own field.
  - `sample` (grid only, optional): `[lon, lat]` of the model cell the
    gauge was snapped to, where the viewer samples the field instead of
    at the gauge itself; a gauge on a narrow river can fall a cell off it.
  - `series` (required): `{path, offset, length}`, the byte span of the
    gauge's line in `gauges.jsonl` (§3). `path` is always `gauges.jsonl`.
  - `outflow` (optional): the same span for a second line, the outflow of
    a gauge on a dam (the Forata reservoir): its line's `id` is the
    gauge's followed by `.outflow`.
  - `peaks` (optional): reported peak values, each `{time?, value,
    unit?, note?}`: the time as ISO 8601 UTC ending in `Z` where the
    report gives one, the value, the unit where it is not the gauge's (a
    stage in `m` or `ft`), and a short note saying what the value is and
    who reported it (`USGS, estimated`; `peak inflow (CHJ SAIH)`).
- `extent` (optional, default empty): mapped flood extents, each
  `{path, byteLength, crc32, at, credit}`: a GeoJSON FeatureCollection of
  polygons in WGS84 (a `.geojson` file), the time of the imagery it was
  mapped from (`at`), and the credit line its producer requires.
- `credits`: the attribution lines the case must show, as given by the
  data's publishers. `disclaimer` (optional): one line the viewer shows
  beside the river data.
- A grid index with neither gauges nor extents says nothing and is
  refused.

## 3. `gauges.jsonl`

One file per case, one gauge series per line, each line one JSON object
in the compact serialisation of the point products (no spaces, ASCII,
keys as below):

```json
{"id":"08165500","time":["2025-07-02T00:00:00Z","2025-07-02T00:05:00Z","…"],"value":[0.142,0.149,"…"],"stage":[7.56,7.57,"…"],"stageUnit":"ft"}
```

- `time`: strictly increasing ISO 8601 UTC times ending in `Z`; `value`:
  one number or `null` (no reading) per time, in the gauge's `unit`.
- `stage` and `stageUnit` (together or not at all): the water level, one
  number or `null` per time. The times are the union of the discharge and
  stage readings, so a time with only one of them has `null` in the other.
- A gauge's `series` span in the index is its line's JSON object exactly,
  without the newline (the synop contract, [`synop.md`](synop.md)): issue
  `Range: bytes=<offset>-<offset + length − 1>` against
  `gauges.jsonl?v=<crc32 of the index>` — the file is immutable with the
  directory — and parse the bytes on their own. The lines are in the
  index's gauge order.

The series are what the operators published, unedited: the USGS's
instantaneous values with their provisional and estimated spans (the
Guadalupe at Hunt stopped reporting stage at its peak and the USGS
estimated the discharge after it), or a series digitised from an
operator's report where no download exists (the CHJ's SAIH hydrographs of
October 2024, about 1 % of full scale). A case's README and the `peaks`
notes say which.

## 4. Reading

1. Fetch the catalog row's `river.index` under its `crc32`, and validate
   it (§2). A viewer that does not know the block plays the case without
   river data.
2. A network case: fetch `reaches.parquet` — or, for one area, its footer
   and the row groups whose `bbox` statistics meet the view — and open the
   chosen track's store ([`zarr-profile.md`](zarr-profile.md), "Network
   store"). A frame is one range of the shard; colour each reach by its
   value, or by its ratio to `baseflow`.
3. A gauge: one range of `gauges.jsonl`. On a network case its modelled
   series is the store's column `feature`; on a grid case, the case's
   field sampled at `sample` (or at `lon`/`lat`).
4. An extent: fetch its GeoJSON and draw it from `at` on, with its credit.

## 5. Writing

`python -m xuebuild.river build showcase/river-cases/<id>.json` reads the
work directory a definition names (its `case.json` lists the frames, the
gauges with their series files and the extents) and writes the case's
`river/` directory whole, the index last, then reads the index back
through the validator (`xuebuild/river/schema.py::read_index`), which
checks every file it names against what is beside it. The definition:

```json
{"id": "guadalupe-2025", "kind": "network", "work": "tmp/2025-07-04-guadalupe/work",
 "output": "web/public/data/showcase/guadalupe-2025/river", "model": "NWM v3.0",
 "tracks": [{"id": "analysis", "kind": "analysis", "product": "analysis_assim"},
            {"id": "short_range-2025070409", "kind": "forecast", "product": "short_range",
             "cycle": "2025-07-04T09:00:00Z"}],
 "gauges": ["08165500", "08166200", "08167000"],
 "extent": [], "disclaimer": "…"}
```

`work` and `output` are relative to the working directory (`--work` and
`--output` override them); a network case names its `model` and its
tracks — an analysis track takes the whole analysis window, a forecast
track the frames of one cycle — and either kind picks its gauges by id and
its extents by path from the work directory. `credits` defaults to the
work directory's. Writing GeoParquet needs pyarrow.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
