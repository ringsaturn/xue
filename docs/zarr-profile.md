# Xue Zarr Profile

This document specifies how Xue bundles are written as Zarr v3 stores, the
layout every live run, rolling-window round and showcase case is published
in and a [STAC catalog](stac.md) lists. It is normative for what the store
writers (`xuebuild/zarrstore.py`, the native encoder's
`encode/zarrstore.rs`, `build-bin`) write and for what a store must look
like to be one of these. What a bundle carries, its metadata JSON,
quantization codebooks and residual arithmetic, is
[`bundle.md`](bundle.md); this profile says how those land in a store. A
second branch, the [polar store](#polar-store), carries radar sweeps in
beam-and-gate coordinates for products that have no grid, a third, the
[network store](#network-store), one value per river reach per frame, and a
fourth, the [column store](#column-store), the codes of a run's map stores
cut so that one cell's whole profile over the whole axis is a few chunks.

A store is a function of its bundles' metadata, their quantized codes, their
tiles, each variable's predictor and each bundle's dimension names (see
"Coordinates"), and of nothing else. A build writes it straight from the
codes it quantized. The frontend plays a store wherever a bundle's manifest
entry names one (see "Reading"). Runs published before the switch to stores
carry `.xue` containers ([`format.md`](format.md)), a legacy layout every
decoder keeps reading: such a run plays from its containers, and
`?backend=xue` asks for the container where an entry names both.

## Why a profile

A player streams a bundle by range reads: one spatial tile of six frames
at a time, a viewport as a few contiguous spans, one cell's series as one
chunk per six frames, each chunk one Zstandard frame whose frames may be
wrapping differences against the frame before. Zarr spells that as a
`uint8` array under a `sharding_indexed` codec, a `zstd` codec and,
optionally, an array-to-array delta codec. Writing the convention down lets
a store written by anyone else be played the same way, and lets xarray,
zarrs or zarrita read a Xue run without a decoder of their own. (The `.xue`
container's v2 index ([`format.md`](format.md)) cuts the same chunks; the
store replaces it.)

## Layout

Every bundle of one resolution tier of a run, a rolling window's round or a
segment is one Zarr group, the **shared store**, `xue_profile` 2; one
variable is one array in it, named by the variable's `id` (unique within a
source). The coordinates sit beside the variables, written once for the
store.

```text
<model>.<run>[/<HHMM>]/bundles.zarr/   every bundle of the full tier
  zarr.json                            group; attributes.xue_bundles = each bundle's metadata JSON
  <variable id>/                       tmp2m/, prate/, ugrd10m/ and vgrd10m/ (wind10m), …
    zarr.json                          array metadata (below)
    c/0/0/0                            the array's one shard: the whole axis of the whole grid
  time/  latitude/  longitude/         the primary coordinates, one chunk each (c/0)
  time_<bundle>/  latitude_<family>/ … a bundle's own axis, a grid family's grid ("Coordinates")
<model>.<run>[/<HHMM>]/bundles.half.zarr/   the half tier, the same shape
```

`bundles` and `columns` (the [column store](#column-store)'s) are reserved
store stems: no bundle id may be either. A store
is a handful of objects per array (two per variable, two per coordinate)
plus one group document, whatever the length of its axis and however many
bundles it holds. A bucket bills every object written: a store cut one shard
per time chunk cost a GFS run some five thousand objects where the container
cost sixty, and one store per bundle still spent most of a run's objects on
group documents and coordinate chunks repeated in every store (a GFS run
1,571 objects, 190 of them shards; a satellite round 253, 25 of them
shards). A shard the size of the array is one object read by range, as the
container was, and its index is the whole index, read once.

A reduced-resolution rendition is its own shared store,
`bundles.<tier>.zarr`, written from the tier's decimated codes exactly as
the full one is from the full codes. The ladder is per source
(`SourceSpec.variant_factors`): every source has the half
(`bundles.half.zarr`, the grid decimated by two), and the satellite disks
add the quarter (`bundles.quarter.zarr`; 3000 → 1500 → 750 cells a side,
the tile 64 → 32 → 16), each decimated from the full plane. The eighth
(`bundles.eighth.zarr`, decimation by eight) is still a valid tier that no
source builds: a reader takes the tiers a bundle has from its manifest
entry, so a store published with one still plays. Every rung is every
f-th row and
column, except that the 3.9 µm window (`ir039`) keeps the largest code
within f/2 cells of each site, so a one-cell hot spot is not dropped
(`docs/satellite.md`, "The 3.9 µm window").

**Profile 1, the one-bundle store**, is the previous revision and stays
readable forever: one bundle per group, `<bundle>.zarr` and
`<bundle>.<tier>.zarr` (`tmp2m.zarr`, `tmp2m.half.zarr`), its metadata in
the group's `attributes.xue`, the same arrays without `xue.bundle` and the
three primary coordinates of that bundle alone. Runs, rounds and showcase
cases published before shared stores are profile 1 and are never rebuilt;
a series store is still profile 1 (see "Series store"). Everything below
applies to both profiles except where it names one.

## Array metadata

Every variable array is `uint8` of shape `[frameCount, height, width]`
(time, then the grid in the metadata grid's row-major, north-to-south,
west-to-east order) and carries exactly one codec, `sharding_indexed`.

```json
{
  "zarr_format": 3, "node_type": "array",
  "shape": [161, 721, 1440], "data_type": "uint8",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [162, 728, 1440]}},
  "chunk_key_encoding": {"name": "default"},
  "fill_value": 255,
  "codecs": [{"name": "sharding_indexed", "configuration": {
      "chunk_shape": [6, 52, 48],
      "codecs": [{"name": "bytes"},
                 {"name": "zstd", "configuration": {"level": 15, "checksum": true}}],
      "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}},
                       {"name": "crc32c"}],
      "index_location": "end"}}],
  "dimension_names": ["time", "latitude", "longitude"],
  "attributes": { "…": "see Attributes" }
}
```

- Time chunk. The inner chunk holds six frames on a regular grid: time
  chunk `t` covers frames `[6t, 6t + 6)`. The container cuts its groups
  inside segments of constant step and restarts them where the step changes
  (GFS: a one-frame group at f120), so a store chunk may straddle two of
  the container's groups. The two layouts coincide up to the first change
  of step.
- Inner chunk: one time chunk of the bundle's tile, `[6, tileHeight,
  tileWidth]`. Within a shard the inner chunks are ordered row-major over
  the shard's chunk grid: time chunk by time chunk, and within each the
  bundle's tiles in the bundle's row-major order, `tileRows × tileColumns`
  of them, so one time chunk is one contiguous run of the object, as one
  group is in the container.
- Outer chunk (shard): the whole array, the axis rounded up to whole time
  chunks and the grid rounded up to whole tiles, because the sharding codec
  requires whole inner chunks along each axis: `[6 × timeChunks, tileRows
  × tileHeight, tileColumns × tileWidth]` (GFS at 161 frames: `[162, 728,
  1440]`). The chunk grid may run past the array's shape; the cells beyond
  it, and the frames beyond the axis in the last time chunk, are stored as
  `fill_value`. A reader takes the shard's frame count from the array's own
  chunk shape and must accept any multiple of the time chunk: stores
  written before this revision are cut one shard per time chunk
  (`chunk_shape[0] = 6`, objects `c/<t>/0/0`), and those stay readable.
- `fill_value` is the variable's `nodataCode`. Padding, and any read past
  the grid, is explicitly nodata rather than a plausible low code.
- Inner codec chain. The default is the standard `[bytes, zstd]` with
  level 15 and a content checksum, which any Zarr client decodes with
  nothing registered. The delta chain (the writers' `delta` option, which
  the series store always takes) puts `{"name": "xue.delta",
  "configuration": {"axis": 0}}` in front of `bytes` on every variable the
  bundle predicts from the previous frame, and never on a RAW one
  (precipitation, reflectivity).
- Shard index. One `(offset, nbytes)` pair of little-endian `uint64` per
  inner chunk of the shard's chunk grid, in row-major order (time chunk,
  then tile), followed by the CRC-32C of those pairs as four little-endian
  bytes: `16 × timeChunks × tileCount + 4` bytes (GFS, 27 × 420 chunks:
  181,444). Offsets are from the start of the shard. `index_location` is
  `end` by default (a client fetches the index as an HTTP suffix range
  without knowing the object's length, and zarrita reads only that form)
  and may be `start`. A reader must accept both. A pair of `2^64 − 1` marks
  a chunk that was never written; the exporter writes every chunk, the
  padding time chunk past the axis included (a few bytes of `fill_value`
  each).
- `chunk_key_encoding` is `default` with the `/` separator, so the shard is
  the object `c/0/0/0`.

## The `xue.delta` codec

Array-to-array. Along `axis` the first slice is stored whole and every later
slice as its modulo-256 difference against the slice before it; decoding is
the running sum, modulo 256: the residual arithmetic of
[`bundle.md`](bundle.md), "Residuals", the `.xue` container's PREVIOUS
predictor moved into the codec chain: an inner chunk under `[xue.delta, bytes, zstd]` compresses to
the same bytes as the bundle's chunk over the same frames and the same
unclipped tile. It is not numcodecs' `Delta`, which differences the
flattened buffer. `xuebuild/zarrcodec.py` implements it for zarr-python
(`register()`); it is only needed to read a store written with the delta
chain.

## Group document

The shape of a grid store's group document, either profile, is
[`schemas/store-group.schema.json`](schemas/store-group.schema.json), and
of every array document (each array's `zarr.json`, repeated in the
consolidated block) [`schemas/store-array.schema.json`](schemas/store-array.schema.json);
the polar, network and column branches' groups are
[`schemas/polar-group.schema.json`](schemas/polar-group.schema.json),
[`schemas/network-group.schema.json`](schemas/network-group.schema.json) and
[`schemas/column-group.schema.json`](schemas/column-group.schema.json).
They are normative for shape, and embed the bundle metadata schema
([`bundle.md`](bundle.md), "Metadata JSON") wherever a document carries a
bundle's metadata or a variable of it. This section and the ones below say
what the fields mean and what a reader checks across them: the profile it
implements, exactly one branch's block, the bundle it opens by id, key
order and the CRC. `tests/test_schemas.py` holds the committed stores
(`tests/fixtures/indicators/store/`, `tests/fixtures/river/network/`,
`tests/fixtures/column/`) and stores it builds without GDAL (the shell's synthetic run, a window cut
into segments, a polar round and window) to them. The shell checks a group
with the generated validator over what it reads, the envelope and the
bundle asked for, so a shared store opens every bundle a reader can parse
whatever another bundle carries.

A shared store's `zarr.json`:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "xue_profile": 2,
    "xue_bundles": {
      "tmp2m": { "schemaVersion": 3, "model": "…", "product": "…", "runTime": "…",
                 "profile": "…", "time": { "…" }, "grid": { "…" }, "variables": [ "…" ] },
      "prate": { "…" },
      "wind10m": { "…" }
    }
  },
  "consolidated_metadata": {
    "kind": "inline", "must_understand": false,
    "metadata": { "tmp2m": { "…the array's zarr.json…" }, "prate": { "…" },
                  "ugrd10m": { "…" }, "vgrd10m": { "…" },
                  "time": { "…" }, "latitude": { "…" }, "longitude": { "…" },
                  "time_prate": { "…" } }
  }
}
```

- `xue_bundles` maps each bundle id to that bundle's metadata JSON
  verbatim ([`bundle.md`](bundle.md), "Metadata JSON"), so the parsers that read a bundle's metadata read a
  store's. Its keys are in manifest bundle order (the source's publication
  order). A group whose `xue_bundles` is missing, empty or not an object is
  invalid. There is no `attributes.xue` on a shared group: a profile-1
  reader refuses the group (it refuses an `xue_profile` it does not
  implement), a profile-2 reader selects its bundle by id.
- `xue_profile` is this document's version: `2` for a shared store, `1` for
  a one-bundle store. A reader refuses any other value.
- `consolidated_metadata` is the Zarr v3 inline form zarr-python reads.
  Every array document of the store is repeated verbatim, so a client on an
  origin that cannot be listed (a plain HTTP bucket, where every store is
  served from) still discovers the arrays: `xr.open_zarr(url)` works with
  no `consolidated=` argument and no directory listing. Its keys are in
  this order: every variable array, bundle by bundle in `xue_bundles` order
  and within a bundle in its `variables` order; then the primary
  coordinates `time`, `latitude`, `longitude`, `y`, `x`, `crs`, those some
  bundle uses; then every other coordinate array in the order its first bundle
  names it (`time_<bundle id>`, `latitude_<family>`, `longitude_<family>`,
  `y_<family>`, `x_<family>` within one bundle).
- Key order is part of the bytes. The document is serialized as every
  document of the profile is: JSON with two-space indentation, one member
  or element per line, `": "` after a key, non-ASCII characters as raw
  UTF-8, and a trailing newline — Python's `json.dumps(document, indent=2,
  ensure_ascii=False) + "\n"` (`zarrstore._dump_json`), mirrored by the
  native encoder's writer for the array and group documents alike.
- It is written last, after every array, and its CRC-32 is the store's
  `crc32` — the `?v=` every object of the store is fetched under — and the
  `crc32` of every manifest entry that names the store. It covers every
  array's metadata too.

A one-bundle store's `zarr.json` (profile 1) carries `"attributes": {"xue":
{…the bundle's metadata…}, "xue_profile": 1}` and the same consolidated
metadata over its own arrays (the variable arrays, then `time`, `latitude`,
`longitude`, or `time`, `y`, `x`, `crs` on a projected grid). A store written before consolidated metadata lacks it and
opens with `consolidated=False` from a listable store only; one written
before that also carries an `xue_index` attribute naming a whole-store index
object, `index.bin`, which a reader ignores, since the shard's own index is
the whole index.

A reader that opens arrays by name (the frontend, `zarr`'s
`open_group(url)["tmp2m"]`) never needs the consolidated block.

## Array attributes

Each variable array's `zarr.json`:

```json
"attributes": {
  "xue": {
    "bundle": "tmp2m",
    "variable": { "numericId": 1, "id": "tmp2m", "label": "…", "unit": "°C",
                  "parameter": { "…" },
                  "quantization": { "type": "linear", "offset": -60.0, "scale": 0.5,
                                    "minimumCode": 0, "maximumCode": 220, "nodataCode": 255 } },
    "predictor": "previous"
  },
  "scale_factor": 0.5, "add_offset": -60.0, "_FillValue": 255
}
```

- `xue.bundle` is the id of the bundle the array belongs to, so an array
  opened alone knows which entry of `xue_bundles` describes it. It leads the
  block in a shared store and is absent from a one-bundle store.
- `xue.variable` is that variable's entry from the metadata's `variables`
  list, copied, for a reader that opens one array alone.
- `xue.predictor` is `previous` or `raw` and is informational: decoding
  follows the codec chain.
- Every store writer writes `xue.variable` and `xue.predictor`, and a
  reader accepts an array without them: the schema names them without
  requiring them.
- `_FillValue` is the `nodataCode` for every variable. `scale_factor` and
  `add_offset` are written for a linear codebook only, so a CF-aware client
  dequantizes it (value = `code × scale + offset`); the `log1p` codebook has
  no CF spelling and is described by `xue.variable.quantization` alone.
- Dimensions are declared in the array metadata's `dimension_names`, the
  Zarr v3 field: `["time", "latitude", "longitude"]`, `["time", "y", "x"]` on
  a projected grid, or the bundle's own names (below). Every array of one
  bundle carries the same three.
- On a projected grid every variable array also has `grid_mapping: "crs"`, its
  last attribute, naming the scalar array of *[Coordinates](#coordinates)*, and on no other
  grid does one.

## Coordinates

The coordinate arrays are one-dimensional, one chunk each under `[bytes]`
(little-endian), named by the dimension they label: the frame's offset from
the bundle's `runTime` in seconds (`int32`, `units: "seconds since
<runTime>"`, `calendar: "proleptic_gregorian"`, `standard_name: "time"`,
fill `0`), the cell centre latitudes and longitudes (`float64`,
`degrees_north` / `degrees_east`, fill `NaN`) from the bundle's grid. They
are for xarray and its kind; a player reads the metadata.

On a grid with a `gaussian` block the `latitude` coordinate holds the true
Gaussian latitudes of its rows, not the regular fields' even ones. Each is
computed alone, by this iteration in this order of operations, so that every
writer lands on the same double (a library root finder differs by up to 1.8e-12°
near the poles). With `N = 2n` and `i` the row on the whole Gaussian grid from
the north:

```text
x = cos(π × (i + 0.75) / (N + 0.5))
repeat at most 100 times:
    p0, p1 = 1, x
    for k = 2 … N:
        p0, p1 = p1, ((2k − 1) × x × p1 − (k − 1) × p0) / k
    derivative = N × (x × p1 − p0) / (x × x − 1)
    step = p1 / derivative
    x = x − step
    stop when |step| < 1e-15
latitude = degrees(asin(x))
```

For `n = 95` the first rows are 89.27671287810583, 88.33975425118209,
87.3972623773714; for `n = 768`, 89.91032453466268, 89.79415738840316,
89.67730421235662. `tests/test_native_grids.py` and `rust/xue/src/projection.rs`
hold these goldens.

On a grid with a `projection` block there is no latitude or longitude axis.
The coordinates are `y` and `x`, in place of `latitude` and `longitude`:
`float64`, fill `NaN`, `units: "m"`, `standard_name:
"projection_y_coordinate"` and `"projection_x_coordinate"`, holding `y0 + row ×
dy` and `x0 + column × dx`. Two-dimensional latitude and longitude arrays are
not written; a client computes them from the `crs` array.

That array, `crs`, is zero-dimensional: `shape: []`, `chunk_grid` with
`chunk_shape: []`, `data_type: "int32"`, `fill_value: 0`, `codecs:
[{"name": "bytes", "configuration": {"endian": "little"}}]`, `dimension_names:
[]`. It has a `zarr.json` and no chunk, and it is written once per store. It
exists for its attributes, the CF grid mapping, which xarray and rioxarray read:

| Attribute | Value |
|---|---|
| `grid_mapping_name` | `lambert_conformal_conic` |
| `latitude_of_projection_origin` | `latitudeOfOrigin` |
| `longitude_of_central_meridian` | `centralMeridian` |
| `standard_parallel` | `[standardParallel1, standardParallel2]` |
| `earth_radius` | `radius` |
| `crs_wkt` | the same projection as WKT2, in the one spelling `reproject.LambertConformal.wkt` writes, each number as its shortest round trip, so that writers agree on the bytes |

The attributes come in this order. Whatever the grid, a dimension's coordinate
array follows these rules.

- The **primary coordinates** `time`, `latitude` and `longitude`, or `y` and
  `x` on a source published on a projection, are the run's axis and the source's
  primary grid at the store's tier. They are written once per store.
- A bundle whose time axis is cut by a rule of its own takes the dimension
  `time_<bundle id>`, and the store carries a coordinate array of that name
  with that bundle's axis (same encoding as `time`). The rules are the three
  by which the encoder cuts a bundle's axis: a published scalar whose
  series has no analysis frame (`discover.analysis_optional_ids`: a
  de-accumulated, de-averaged or window-differenced rate, an interval
  maximum such as `gust`,
  a field the source writes only from a later hour, such as CMA GRAPES
  GEPS's 24-hour precipitation probabilities from +24 h, whose axis starts
  there), a published scalar read from a field the source writes only every
  so many hours (the source's `variableStepHours`: ECMWF's 24-hour
  precipitation probabilities, one window ending every 12 hours from +24 h
  on a three- and six-hourly run, whose axis is the run's thinned to those
  hours), and a static field (the terrain, one frame at the analysis). A
  reader takes a bundle's frames at the times its own axis lists and has no
  frame of it between them.
- A bundle on a grid family of its own takes `latitude_<family>` and
  `longitude_<family>`, `<family>` being exactly the string
  `discover.bundle_grid_family` returns for it: the id of the companion
  file family its inputs are read from (`pgb`, the CFSv2 pressure levels on
  their 1° grid, so `latitude_pgb`). Every bundle of one family shares the
  pair, as the primary grid's bundles share `latitude` / `longitude`. A
  family id matches `^[a-z][a-z0-9]*$`.
- The decision is the rule, not a comparison of axes, so it is a function of
  the source registry and the bundle id alone
  (`discover.store_dimensions`): a rate gets `time_prate` even on a build
  whose axis happens to start past the analysis. A fanned-out job building
  a subset of the bundles therefore writes the same objects a whole build
  does, and every job writes the primary coordinates it needs, the same
  bytes each time. A multi-variable bundle's arrays share one set of names.
- Two bundles naming one coordinate carry the same document and bytes for
  it; a writer refuses to merge them otherwise.

A bundle's dimension names are `time` or `time_<its bundle id>`, then
`latitude` and `longitude`, or `latitude_<family>` and `longitude_<family>`
with one family id matching `^[a-z][a-z0-9]*$`. On a grid with a `projection`
block, and only there, the last two are `y` and `x`, or `y_<family>` and
`x_<family>`. Any other name is invalid, and so is a bundle whose names
disagree with its grid block. The array `crs` belongs to no dimension: a
bundle uses it by its variables' `grid_mapping`, and every projected bundle
of a store shares it, since a family differs in grid and not in projection.

## Manifest

The manifest's shape, schema v5, is
[`schemas/manifest.schema.json`](schemas/manifest.schema.json), which is
normative. The shell checks it with the generated validator before the
rules a schema cannot say; the three hand validators
(`xuebuild/manifest.py`, `encode/manifest.rs`, `web/src/manifest.ts`)
hold the rules below, the registered dataset and the core set, and
`tests/test_schemas.py` holds the writers' manifests to the schema. A
manifest entry's `zarr` descriptor is
unchanged in shape: `{path, byteLength, crc32}`, or the segmented form
(see "Segmented stores").

- `path` names the shared store of the entry's tier: `bundles.zarr` on the
  bundle, `bundles.half.zarr` on its half variant, and so on. Every entry of
  a tier carries the same path. The validators (`xuebuild/manifest.py`,
  `rust/xue/src/encode/manifest.rs`, `web/src/manifest.ts`) exempt exactly
  these paths from the duplicate-path rule: a map store's path, or a
  segment's, whose **final path component** is exactly `bundles.zarr` or
  `bundles.<tier>.zarr`, `<tier>` a non-empty run of lowercase ASCII
  letters other than `series` (any such tier, not only `half`, `quarter`
  and `eighth`). Every other path — a container, a series store (so
  `bundles.series.zarr` too), a poster, a one-bundle store — stays
  unique in the manifest.
- `byteLength` is the bundle's share: the objects of its variable arrays
  (each `zarr.json` and shard) and of the coordinate arrays it uses (each
  `zarr.json` and chunk). The group document is not counted. It is what a
  viewer downloads for that bundle alone, which is what the delivery sizes
  and a tier's `bandwidth` are computed from.
- `crc32` is the CRC-32 of the store's group document, identical on every
  entry naming the store.

A run that ships a [column store](#column-store) names it in the manifest's
optional top-level `columns`, beside `bundles`: `{path, byteLength, crc32}`,
the store's root relative to the manifest, the sum of every object in it and
the CRC-32 of its group document. `path` is a relative path with no `..`
component whose final component is exactly `columns.zarr`, and it is unique
in the manifest like any other. The manifest schema stays 5: a reader that
does not know the field reads the run without it. A showcase case and a
round of a rolling window have none.

**Fan-out and assembly.** A publish built one bundle group per job
(`build-bin --bundles`) writes, in each job, its bundles' arrays and
coordinates into the run's `bundles[.<tier>].zarr/` and a **partial group
document** `bundles[.<tier>].zarr/zarr.part.<first bundle id>.json` — the
group document above over that job's bundles alone, its first bundle (in
manifest order) naming it — and no `zarr.json`. The job's partial manifest
carries the part's CRC-32 as `crc32`. Parts are local: the uploads exclude
them. `assemble-run` merges the parts of each store into its group
document, in manifest order, writes it, and stamps its CRC-32 on every
entry naming the store; a bundle in two parts, a part's bundle the manifest
does not name with that store, or a bundle no part carries is an error. A
whole build writes its part and its group document the same way, and
removes the part; `tests/test_assemble.py` holds split = whole.

**Top-ups.** A top-up builds only the bundles the live run lacks, and merges
their parts onto the live group document, fetched from the bucket: a
part's bundle replaces the live one, the live bundles keep their metadata
and array documents. The new group document is written over the live one's
path under a new CRC-32, which every entry naming the store — the live ones
too — then carries, so nothing is served under an unchanged `?v=`. The
arrays already on the bucket are not touched. The live manifest's `columns`
is kept only when no bundle the top-up built is a member of the source's
column store and the source still lists one; otherwise it is dropped and
the column store is built anew from the run as topped up.

## Reading

What a player needs from a store, in the order it needs it. The frontend's
own reader (`web/src/zarr/`) is one implementation; the rules are the
profile's.

1. The group. A reader opens a bundle by **(store, bundle id)**: the
   store path from the bundle's manifest entry, and the entry's `variable`.
   Fetch `zarr.json` at the store's root under the entry's `?v=`. On
   `xue_profile` 2, `attributes.xue_bundles[<bundle id>]` is the bundle
   metadata verbatim; on `xue_profile` 1, `attributes.xue` is: the grid,
   the time axis, the variable set with their codebooks and GRIB2 parameter
   blocks. It is parsed by the rules of [`bundle.md`](bundle.md),
   "Metadata JSON", wherever it comes from. A reader must refuse a group with an `xue_profile`
   it does not implement, a profile-1 group without `xue`, a profile-2
   group whose `xue_bundles` is missing, empty or not an object, and a
   profile-2 group without the bundle asked for. Grid and axis checks are
   against that bundle's metadata, never the store's other bundles. A cache
   keyed by store path (the group, the coordinates, a shard index) serves
   every bundle of the tier.
2. Each array. Fetch `<id>/zarr.json` for every variable the bundle's
   metadata lists (the array's name is the variable's `id`; `numericId` is
   the handle a player's own protocol uses), exactly as for a one-bundle
   store. Validate before trusting: `uint8`, three
   dimensions, a regular chunk grid, exactly one `sharding_indexed` codec,
   an inner chain of `[bytes, zstd]` or `[xue.delta{axis: 0}, bytes,
   zstd]`, index codecs `[bytes, crc32c]` little-endian, `index_location`
   `start` or `end`, `fill_value` a byte, and an outer chunk equal to
   `[shardFrames, tileRows × tileHeight, tileColumns × tileWidth]` with
   `shardFrames` a positive multiple of the time chunk (the whole axis
   rounded up on a store this revision writes, six on one written before).
   Every array of one bundle is cut the same way. The tiling this yields
   (`tileWidth`, `tileHeight`, `tileColumns`, `tileRows`) is the same shape
   the container reports, and a viewport maps to a rectangle of inner
   chunks the same way.
3. Locating a frame. A frame's index on the axis is its position in
   `frameOffsets` (or its distance from `firstFrameOffset` in
   `frameStep`s), its time chunk is `floor(index / timeChunk)`, and its
   frame within the chunk `index mod timeChunk`. The time chunk is fixed at
   six on a regular grid and may straddle two of the container's groups
   (GFS: from the change of step at 120 h on); a reader never consults the
   container's grouping. The time chunk lives in shard `floor(timeChunk ×
   timeChunk / shardFrames)` (shard 0 on a store this revision writes) as
   the object `c/<shard>/0/0`, and its tiles are the `tileCount`
   consecutive index entries from position `((timeChunk × timeChunk) mod
   shardFrames) / timeChunk × tileCount`, tile `t` in row-major order,
   `tileColumns` per row.
4. The shard index. Read the shard's index once, on the first chunk wanted
   from it: `16 × chunksPerShard + 4` bytes at the end of the shard
   (`index_location: "end"`, fetched as the HTTP suffix range `bytes=-N`
   without knowing the object's length) or at its start (`bytes=0-(N−1)`).
   Verify the trailing CRC-32C over the pairs before using any offset; a
   pair of `2^64 − 1` is a chunk the shard never held, which reads as
   `fill_value` throughout. Hold the index for the session: on a store this
   revision writes it is the array's whole index, so every later frame,
   viewport and series locates its chunks without another dependent
   request. It is the container's structural prefix in Zarr's terms.
5. Inner chunks. Fetch `[offset, offset + nbytes)` of the shard. Decompress
   the Zstandard frame to exactly `timeChunk × tileHeight × tileWidth`
   bytes (refuse a frame without a content checksum, a checksum that does
   not match, or an output of any other length) and, under the delta chain,
   replay the modulo-256 running sum along the frame axis. This is the
   container's chunk path; `decodeChunk` in the WASM decoder does exactly
   it for bytes that did not come out of a `.xue` index.
6. Trimming the padding. An inner chunk is stored whole. Its rows past
   `height`, its columns past `width` (the last tile row and column) and,
   in the last time chunk, its frames past `frameCount` are padding: copy
   only `min(tileHeight, height − row)` rows of `min(tileWidth, width −
   column)` cells for the frames that lie on the axis.
7. Coalescing ranges. A shard's inner chunks are contiguous in tile order,
   so a tile row is one contiguous span of the object. A reader that
   issues one range per inner chunk pays one round trip per tile: a 6 × 4
   tile viewport costs 24 requests where the container costs 4. The
   profile's recommendation is to gather the ranges of one shard that a
   read needs, sort them by offset, merge neighbours whose gap is small
   (the frontend uses 64 KB), and fetch each run as one range; the
   over-read of a merged gap costs less than the round trip it saves. With
   that, a global view is one range per time chunk, a viewport one range
   per tile row per time chunk, and a point series one chunk per time
   chunk, plus one index read per shard the reader has not seen, which on
   a store this revision writes is one per array, at open.

A series costs opening plus `timeChunks` requests, the container's own
shape (its prefix plus one chunk per group), independent of the frame
count.

## Series store

A **series store** is the same bundle's quantized codes cut for the other
access pattern: one cell's whole series. It is a one-bundle store
(profile 1) except for the geometry of its arrays, so every rule above
applies unchanged; it is never shared, one bundle per store.

- The store's root is `<bundle>.series.zarr`, and its group document carries
  the bundle's metadata verbatim in `attributes.xue`, `xue_profile` 1. Both
  stores name the same run of the same
  bundle; a manifest entry carries the map store as `zarr` and the series
  store as `series`, each a `{path, byteLength, crc32}` descriptor. The
  series descriptor is full-resolution only and absent wherever the pipeline
  does not derive one.
- Every variable array is `uint8` of shape `[frameCount, height, width]` and
  carries exactly one `sharding_indexed` codec, with `chunk_shape =
  [frameCount, blockHeight, blockWidth]` — the whole axis of one spatial
  block — and the outer chunk (shard) the whole array rounded up to whole
  blocks: `[frameCount, ceil(height/blockHeight) * blockHeight,
  ceil(width/blockWidth) * blockWidth]`. The array's own `chunk_shape` is the
  authority for the block, as it is the authority for the time chunk above.
- The inner codec chain is the variable's, exactly as the map store:
  `[xue.delta{axis: 0}, bytes, zstd]` on a PREVIOUS variable and
  `[bytes, zstd]` on a RAW one. The delta runs over the whole axis, so an
  inner chunk is the variable's whole temporal residual cube for one block.
  Unlike the map store, which leaves `xue.delta` off so a generic Zarr client
  needs nothing registered, the series store defaults to it: it is the
  layout's own predictor, and its only readers already decode it.
- `fill_value` is the variable's `nodataCode`; the rows and columns past the
  grid are written as it. There is no frame padding: the axis *is* the time
  chunk.
- The three coordinate arrays are the primary ones, over the bundle's own
  axis.

The block is a per-source registry value; 8 x 8 is what the reference
pipeline writes. Which bundles ship a series store at all is the source
registry's `series_bundle_ids` — opt-in per source, so a rollout can watch
one dataset's storage and read counts before the next: the GFS
0.25-degree run for `tmp2m`, `prate` and `wind10m`, HRRR for `tmp2m`,
`wind10m`, `gust`, `tcdc` and `wind80m`, sflux for `tmp2m`, `dswrf` and
`wind10m`. Measured
on `gfs.2026090918` (161 frames, 0.25° global), an
8 x 8 series store is 37.2 MB against the bundle's 37.0 MB for `tmp2m` (101.4
MB against 100.8 for the two-variable `wind10m`), its shard index is 262 KB
per array (16 bytes per inner chunk), and one cell's whole series is a 1.9 KB
median chunk.

Reading one cell's series: one inner chunk of the block containing the cell,
which holds every frame, plus the shard index once per store. A bilinear read
of four neighbouring cells is one chunk where they share a block and two to
four where they do not. A series store is read for series only: a frame read
through it would fetch one tiny chunk per block, which is not what it is cut
for.

## Segmented stores

A rolling window (radar, satellite, the aurora nowcast) is rebuilt every few
minutes, but between two rounds only its newest frames change. Its map
stores are therefore published cut along time: each resolution tier's
shared store is cut into **shared segments**, every one an ordinary shared
store under this profile, and each bundle's segments' axes concatenate into
the bundle's.

- A segment holds the frames of one clock interval: an hour for a feed
  whose frames are less than an hour apart, six hours (from 00, 06, 12 and
  18 UTC) for an hourly or slower one. The window's newest segment is
  partial, as is the oldest when the window does not start on a segment
  boundary; every other one is whole and comes out the same, byte for
  byte, in every round that holds it.
- The interval is the store's: decided from every bundle's frames
  together, so the bundles of one tier are cut at the same boundaries. A
  segment holds every bundle of the tier that has a frame in its interval
  (a bundle with none there is absent from it), in `xue_bundles` order.
- In a segment each bundle is the bundle over its own frames there:
  `xue_bundles[<id>]` is the bundle's metadata with `runTime` the bundle's
  first frame in the segment and the `time` block recomputed over its
  frames by the rules of `bundle.md`, "Time Axis" (the coarsest `unitSeconds` that fits,
  `frameStep` when uniform, `frameOffsets` where a slot is missing).
  Everything else in the metadata, its dimension names, its codec choices
  and the cutting of every array are the bundle's.
- Segments live beside the run directories, at
  `<model>.segments/<YYYYMMDDHHMM>/<digest>/bundles[.<tier>].zarr`: the
  segment's start in UTC, then eight hex digits of a SHA-256 over what the
  whole shared segment is a function of. The digest's input is, bundle by
  bundle in the segment's `xue_bundles` order: the bundle id's UTF-8 bytes
  and a newline (`0x0a`); its three dimension names as the JSON array
  `["time", "latitude", "longitude"]` (comma-space separated); its segment
  metadata as its canonical JSON bytes (`binformat.metadata_json_bytes`); the JSON
  array `[<delta>, "<index location>"]` (`[false, "end"]`); then for each
  variable id in sorted order, its UTF-8 bytes followed by its codes, frame
  by frame. The same contents always land under the same path, and
  different contents never do, so a round names a segment an earlier round
  published instead of writing it again, and a client's cache of it stays
  valid from round to round. Like every other published object, a segment
  is never rewritten under the same path.
- In the manifest, a segmented `zarr` descriptor carries `segments`, a
  non-empty list of `{path, byteLength, crc32}` store descriptors in
  ascending time order, and `byteLength`, their sum. It carries no `path`
  or `crc32` of its own. A segment's path is relative to the manifest and
  may climb out of the round's directory with at most two leading `..`
  components (a round sits at `<model>.<run>/<HHMM>/`), and nowhere else:

  ```json
  "zarr": {
    "byteLength": 64212334,
    "segments": [
      {"path": "../../himawari.segments/202610112200/1a2b3c4d/bundles.half.zarr",
       "byteLength": 11030021, "crc32": "8e0c5a1f"},
      …
    ]
  }
  ```

  A `series` descriptor is never segmented. Every bundle of a shared
  segment names the same path and `crc32` (the segment's group document's)
  with its own `byteLength`, its share as above; the shared-path exemption
  applies to a segment's path by the same final-component test.
- A reader opens every segment by (segment, bundle id), refuses a list
  whose segments differ, for that bundle, in anything but `runTime` and
  `time` or whose frames are not strictly ascending across segments, and
  plays the concatenated axis: frame `i` of the bundle is the frame at its
  absolute time in the segment that holds it.

Cutting is a publishing step over finished stores (`build-bin --round
--segments`, `xuebuild/segments.py`): it reads the codes back and writes
each segment with the reference writer, whichever encoder built the round,
and it skips every segment the live round already names for every bundle it
holds. A round of the previous layout (one store per bundle) is cut into
one-bundle segments, `<bundle>[.<tier>].zarr`, whose digest is the same
input as above for its one bundle without the id and dimension prefix. The
window's posters and its `window.json` stay in the round's directory.

## Polar store

A **polar store** carries radar sweeps in the radar's own coordinates — one
row per beam, one column per range gate — instead of a latitude-longitude
grid. It is the profile's second branch and the one a single-site radar
product writes ([`nexrad.md`](nexrad.md)); it has no `.xue` counterpart and
is never derived from a bundle. Everything in "Array metadata" and
"Reading" applies except where this section says otherwise: the shard, the
index, the codecs and the reading of an inner chunk are the same; the
dimensions, the chunking and the attributes are its own.

- A store is one product over one or more **rounds**, the intervals a
  product delivers its sweeps in. A polar store is never shared. A **round store** holds one round,
  `<round directory>/<product>.zarr` (`nexrad.202610050805/n0b.zarr`); a
  **window store** holds every round of a closed window, one store per
  product for the whole of it (a showcase case's, `nexrad.md` §1). The
  group's attributes carry a `xue_polar` block in place of `xue` and
  `xue_profile`; its `version` is this branch's version, `1`. A reader
  tells the branches apart by which one is present and refuses a group
  carrying both or neither, or a `version` it does not implement.
- One data array, named by the product (`n0b`), `uint8` of shape `[site,
  scan, azimuth, range]` with `dimension_names` exactly those four. `site`
  indexes the radars that scanned in the store's rounds, `scan` the sweeps
  of one site in time order, `azimuth` the beam and `range` the gate.
- Chunking. The inner chunk is one site's sweeps of one round: `[1, depth,
  azimuth, range]`, where `depth` is the largest number of sweeps any site
  has in any one of the store's rounds. Inner chunk `[s, r, 0, 0]` is site
  `s` in the store's round `r`, so `scan` runs over the whole store, round
  after round, `rounds × depth` long, and scan slot `r × depth + k` is the
  site's `k`-th sweep of round `r`. The outer chunk (shard) is the whole
  array, so `c/0/0/0/0` is the only data object and its index holds one
  pair per site and round, in that order; a site with no sweep in a round
  has the never-written pair there. A site's round with fewer sweeps is
  padded to `depth` with `fill_value`; whether a sweep exists is said by
  `scan_time`, never by the codes. A round store is the one-round case:
  its index holds one pair per site.
- Inner codec chain `[bytes, zstd]` (level 15, content checksum). There is
  no `xue.delta`: consecutive sweeps are minutes apart and a difference
  against the previous one is larger than the sweep itself. Index codecs,
  `index_location` and the never-written pair are as above.
- `fill_value` is `0`.
- Codes. A sweep's codes are its source's own, unchanged: a linear codebook
  over a contiguous run of codes plus **reserved codes** that carry a state
  rather than a value. The data array's attributes declare both, in their
  own `xue_polar` block:

  ```json
  "attributes": {
    "xue_polar": {
      "variable": { "id": "n0g", "label": "Base velocity", "unit": "m/s",
                    "quantization": { "type": "linear", "offset": -64.5, "scale": 0.5,
                                      "minimumCode": 2, "maximumCode": 255 },
                    "reservedCodes": { "0": "below_threshold", "1": "range_folded" },
                    "signConvention": "positive_away" },
      "geometry": { "azimuthStart": 0.0, "azimuthStep": 0.5,
                    "rangeStart": 0.0, "rangeStep": 0.25 }
    },
    "scale_factor": 0.5, "add_offset": -64.5,
    "flag_values": [0, 1], "flag_meanings": "below_threshold range_folded"
  }
  ```

  value = `code × scale + offset` for `minimumCode ≤ code ≤ maximumCode`; a
  reserved code has no value and a reader must not dequantize, filter or
  interpolate across it as one. `signConvention` is present on a radial
  velocity and is `positive_away` (motion away from the radar is positive).
  `scale_factor` / `add_offset` let a CF client dequantize; `flag_values`
  tells it which codes are not values. There is no `_FillValue`: code 0 is
  data (no echo), not missing.
- Geometry. Beam `j` covers azimuths `[azimuthStart + j × azimuthStep,
  azimuthStart + (j + 1) × azimuthStep)` in degrees clockwise from true
  north; gate `i` covers slant ranges `[rangeStart + i × rangeStep,
  rangeStart + (i + 1) × rangeStep)` in kilometres from the antenna. A
  source whose beams do not start on this grid is binned by beam centre.
- Coordinates, beside the data array, each one chunk under `[bytes]`
  (little-endian), as a grid store's coordinates are:

  | array | dtype | shape | meaning |
  |---|---|---|---|
  | `azimuth` | `float32` | `[azimuth]` | beam centre, degrees true |
  | `range` | `float32` | `[range]` | gate centre, km slant range |
  | `site_latitude`, `site_longitude` | `float64` | `[site]` | antenna, WGS84 degrees |
  | `site_height` | `float32` | `[site]` | antenna, metres above mean sea level |
  | `scan_time` | `int64` | `[site, scan]` | sweep start, Unix seconds UTC; `-1` for a padding sweep |
  | `elevation` | `float32` | `[site, scan]` | sweep elevation angle, degrees; `NaN` for padding |

  The group's block is `{"version": 1, "product": "n0b", "round":
  "2026-10-05T08:05:00Z", "sites": ["TLX", "TBW", …]}`: the product, the
  round's UTC minute, and the sites in index order by the source's own
  identifiers. A window store carries `"rounds": ["2023-03-24T23:05:00Z",
  …]` in place of `round`: its rounds' UTC minutes in chunk order, oldest
  first. A block carrying both or neither is refused.
- Mapping a gate to the ground is the reader's: the profile fixes the
  coordinates, not the projection. The reference reader uses the
  standard-refraction (4/3 effective earth radius) beam model from the
  site's WGS84 position.
- Reading one site's round: the group, the array's `zarr.json`, then the
  shard index (suffix range) and the site's inner chunk of that round; a
  product that publishes the chunk spans (`nexrad.md` §4) lets a reader
  skip the index and read the chunk directly, one range per site per
  round in either kind of store.

## Network store

A **network store** carries a quantity that lives on the reaches of a
river network — one value per reach per frame — instead of on a grid. It
is the profile's third branch and the one a river case writes
([`river.md`](river.md)); it has no `.xue` counterpart and is never
derived from a bundle. The reaches' geometry is not in the store: it is a
GeoParquet file beside it, cut so that one index serves both. Everything
in "Array metadata" and "Reading" applies except where this section says
otherwise: the shard, the index, the codecs and the reading of an inner
chunk are the same; the dimensions, the chunking and the attributes are
its own.

- A store holds one **track**: one product over one run of frames, an
  analysis window or one forecast cycle. A network store is never shared. The group's attributes carry a
  `xue_network` block in place of `xue` and `xue_profile`; its `version` is
  this branch's version, `1`. A reader tells the branches apart by which
  block is present and refuses a group carrying more than one of `xue` (or
  `xue_bundles`), `xue_polar`, `xue_network` and `xue_columns`, or a
  `version` it does not implement.
- The block:

  ```json
  "xue_network": {
    "version": 1, "model": "NWM v3.0", "product": "short_range",
    "runTime": "2025-07-04T09:00:00Z",
    "time": { "unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 18, "frameStep": 1 },
    "geometry": { "path": "reaches.parquet", "byteLength": 1114663, "crc32": "de943f65",
                  "featureCount": 10940, "rowGroupRows": 4096 },
    "variables": [ { "numericId": 1, "id": "streamflow", "label": "Streamflow", "unit": "m3/s",
                     "quantization": { "type": "log1p", "…": "…" } } ]
  }
  ```

  `runTime` is the cycle of a forecast and the first frame of an analysis
  window. `time` follows [`bundle.md`](bundle.md), "Time Axis", exactly: offsets from `runTime` on the
  coarsest unit that fits, `frameStep` when uniform, `frameOffsets`
  otherwise. `geometry` names the GeoParquet by a path relative to the
  directory holding the store, its length and the CRC-32 of its bytes, its
  row count (`featureCount`) and its row group length (`rowGroupRows`).
  `variables` lists the one variable, `streamflow`, with its codebook.
- One data array, `streamflow`, `uint8` of shape `[frameCount,
  featureCount]` with `dimension_names` `["time", "feature"]`, carrying
  exactly one `sharding_indexed` codec. Feature `f` is row `f` of the
  GeoParquet.
- Chunking. The inner chunk is `[6, rowGroupRows]`: six frames of one row
  group's reaches, time chunk `t` covering frames `[6t, 6t + 6)` as in a
  grid store. The outer chunk (shard) is the whole array rounded up to
  whole inner chunks, `[6 × ceil(frameCount / 6), rowGroupRows ×
  ceil(featureCount / rowGroupRows)]`, so `c/0/0` is the only data object;
  its index holds one pair per inner chunk in row-major order, time chunk
  by time chunk and within each the row groups in order, followed by the
  CRC-32C of the pairs. Frames past the axis and features past the last
  row are stored as `fill_value`. One frame is one contiguous run of the
  shard (every row group of one time chunk), and one reach's series is one
  chunk per time chunk.
- Inner codec chain `[xue.delta{axis: 0}, bytes, zstd]` (level 15,
  content checksum): consecutive hours of one reach differ by a code or
  two. A reader must accept `[bytes, zstd]` as well. Index codecs `[bytes
  little-endian, crc32c]`, `index_location` `end`, and the never-written
  pair are as above.
- `fill_value` is the codebook's `nodataCode`, 255.
- Codes. The `log1p` codebook of [`bundle.md`](bundle.md) with `trace`
  0.01, `scale` 1.0 and `maximum` 100000 (m³/s): code 0 is a flow below
  0.01 m³/s, codes 1–253 the logarithmic grid, about 4.7 % apart, 254 a
  flow above the maximum, and 255 no value — a reach the model does not
  route, such as one inside a lake, or a time it has none. The data
  array's attributes repeat the variable as `xue_network.variable` and
  carry `_FillValue` 255; there is no CF packing, as for any `log1p`
  variable.
- Side arrays, beside the data array, each one chunk under `[bytes]`
  (little-endian), as a grid store's coordinates are:

  | array | dtype | shape | meaning |
  |---|---|---|---|
  | `feature_id` | `int64` | `[feature]` | the reach's identifier in its source (NWM `feature_id`) |
  | `time` | `int64` | `[time]` | the frame's valid time, Unix seconds UTC |
  | `baseflow` | `float32` | `[feature]` | the reach's usual flow before the event, m³/s; `NaN` where unknown |

- The group carries consolidated metadata, written last, and the store's
  `crc32` is the CRC-32 of its group document, as for a grid store.

**The GeoParquet.** The reaches are a GeoParquet 1.1 file: column
`geometry`, WKB `LineString` in WGS84 longitude and latitude (no `crs`
key, GeoParquet's `OGC:CRS84` default); a `covering.bbox` column `bbox`, a
struct of `xmin`, `ymin`, `xmax`, `ymax` in `float32` rounded outward so
the box always contains the line; and `feature_id` (`int64`), `order`
(`int8`, the stream order) and `gage` (`string`, nullable: the gauge on the
reach, if any), in the order `feature_id`, `order`, `gage`, `geometry`,
`bbox`. Rows are sorted along a Hilbert curve by the length-weighted
centroid of each line (a 2^16 grid over the network's extent), and cut
into row groups of exactly `rowGroupRows` rows (4096), the last one
shorter, zstd-compressed. Row group `g` holds rows `[g × rowGroupRows,
(g + 1) × rowGroupRows)`, the reaches of the store's feature chunk `g`, so
a reader that wants one area reads the row groups whose `bbox` statistics
meet it and then exactly the matching inner chunks, and a reader that has
a row from a click knows the chunk without a lookup.

Measured on the Guadalupe 2025 case (10,940 reaches of NWM v3.0, Hilbert
order): the GeoParquet is 1,114,663 bytes with a 6,579-byte footer and row
groups of 415,954, 394,267 and 297,851 bytes. The 73-frame hourly
analysis shard is 145,650 bytes under the delta chain and 163,086 under
`[bytes, zstd]`; the 18-frame forecast shard 33,631 and 38,768. The side
arrays weigh 132 KB per store, most of the store at these sizes: whole
stores are 286,352 and 173,890 bytes.

Reading one frame: the group, the array's `zarr.json`, the shard index
(suffix range), then one range covering the frame's time chunk. Reading
one reach's series: the same index, then one chunk per time chunk at the
reach's row group.

## Column store

A **column store** carries a forecast run's isobaric families and the
surface fields a profile stands on, cut so that one cell's column, every
level over the whole axis, is one chunk per quantity. It is the profile's
fourth branch. It is cut from the codes of the run's published map stores
and holds exactly those codes: nothing in it is quantized again, so a value
read from it and the value the map shows for that cell are the same. A run
without one is complete; a reader then takes a column from the map stores,
one bundle at a time. Everything in "Array metadata" and "Reading" applies
except where this section says otherwise.

- One store per run, `<model>.<run>/columns.zarr`, full resolution only. It
  is never shared, never segmented and has no reduced tiers. The manifest
  names it in `columns` (see "Manifest").
- The group's attributes carry an `xue_columns` block in place of `xue` and
  `xue_profile`; its `version` is this branch's version, `1`. A reader tells
  the branches apart by which block is present and refuses a group carrying
  more than one, or a `version` it does not implement.

**Arrays.** An array is `uint8` and is one quantity. A **levelled** array
holds it on several isobaric surfaces, shape `[frameCount, levelCount,
height, width]`, `dimension_names` `["time", "level", "latitude",
"longitude"]`. A **surface** array holds one field that has no level, shape
`[frameCount, height, width]`, `dimension_names` `["time", "latitude",
"longitude"]`. An array on a time axis other than the store's has the
dimension `time_<array>` in place of `time`, and a levelled array whose
levels are not the first levelled array's has `level_<array>` in place of
`level`, `<array>` being its id. Version 1 is for regular and Gaussian
grids; a run on a projected grid has no column store.

- Each array carries exactly one codec, `sharding_indexed`.
- Inner chunk: `[frameCount, levelCount, blockHeight, blockWidth]` (surface
  `[frameCount, blockHeight, blockWidth]`): the whole axis and every level
  of one block of cells. The writer's block is 8 x 8; a reader takes it from
  the array document.
- Outer chunk (shard): `[frameCount, levelCount, shardHeight, shardWidth]`
  (surface `[frameCount, shardHeight, shardWidth]`). `shardHeight` and
  `shardWidth` are whole multiples of the block and `shardWidth` is at least
  the grid's width, so the shards are bands of rows. The writer's
  `shardHeight` is 96, twelve blocks, and its `shardWidth` the width rounded
  up to whole blocks. The band keeps an object small enough (a few hundred
  MB at most) for a CDN that will not cache one over 512 MB, and its index
  at 34,564 bytes on a 0.25-degree global grid where one index over the
  whole array would be 262 KB.
- Shard `s` holds rows `[s × shardHeight, (s + 1) × shardHeight)` and is the
  object `c/0/0/<s>/0` (surface `c/0/<s>/0`), `s` from `0` to
  `ceil(height / shardHeight) − 1`. The last band's chunk may run past the
  grid, as the outer chunk of a grid store may.
- Inner chunks of a shard are in row-major order over its blocks: block
  `(r, c)` of a shard is index entry `r × (shardWidth / blockWidth) + c`.
  A block partly past the grid is stored whole, its cells past the grid as
  `fill_value`. A block wholly past the grid is never written: its index
  pair is `2^64 − 1` twice. Any other block is written, whatever it holds.
  A reader reads a never-written block as `fill_value`.
- Shard index: `16 × chunksPerShard + 4` bytes with the layout and the
  CRC-32C of a grid store's, `chunksPerShard` being `(shardHeight /
  blockHeight) × (shardWidth / blockWidth)`. Offsets are from the start of
  the shard. `index_location` is `"start"`, so the index is the plain range
  `bytes=0-(N−1)`, which a browser sends without the CORS preflight a suffix
  range costs. A reader accepts `"end"` too.
- Inner codec chain: `[xue.delta{axis: 0}, bytes, zstd]` (level 6, content
  checksum) on an array whose variables are all predicted from the previous
  frame, `[bytes, zstd]` on one whose variables are all RAW, and on a
  one-frame array. The level is 6 where the other stores' is 15 for the cut
  time: a run's whole column store is compressed on one machine after the run is
  live. Measured on an M3 Max, level
  6 is 7 times faster than 15 for 8 % more bytes on an ECMWF temperature, and 4
  times faster for 1.4 % more on a GFS relative humidity. A reader does not
  depend on the level. The predictor is the variable's, the one the series store
  uses; an array mixing the two is an error in the writer. The delta runs
  along time only: a further difference along the levels made the arrays 20 %
  larger. A reader follows the codec chain.
- `fill_value` is the variables' `nodataCode`, which is the same for every
  variable of one array (an error in the writer otherwise).

**Group document.** The block:

```json
"xue_columns": {
  "version": 1, "model": "GFS", "product": "pgrb2.0p25",
  "runTime": "2026-11-24T00:00:00Z", "profile": "balanced",
  "time": { "unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 161, "frameOffsets": [ "…" ] },
  "grid": { "…the bundle metadata's grid block…" },
  "arrays": [
    { "id": "tmp", "levels": [1000, 975, 950],
      "members": [ { "bundle": "tmp1000", "variable": { "…" } },
                   { "bundle": "tmp975", "variable": { "…" } },
                   { "bundle": "tmp950", "variable": { "…" } } ],
      "shards": ["8a1f0c3e", "…"] },
    { "id": "orog", "time": { "unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 1, "frameStep": 1 },
      "members": [ { "bundle": "orog", "variable": { "…" } } ],
      "shards": ["0d3a2e6b", "…"] }
  ]
}
```

- `model`, `product`, `runTime`, `profile` and `grid` are those of the
  bundles' metadata, which every member shares; `time` is the `time` block
  of the first array's bundles ([`bundle.md`](bundle.md), "Time Axis"). An
  array whose bundles' block differs carries its own `time`. All members of
  one array share one axis.
- `arrays` lists the arrays; the writer lists levelled arrays first. An `id` matches
  `^[a-z][a-z0-9]*$`, is unique and is none of `time`, `level`, `latitude`
  and `longitude`. A surface array's id is its variable's.
- `levels` is present on a levelled array and only there: positive integers
  in hPa, strictly descending. `members` is index for index with it. A
  surface array has exactly one member.
- A member names the **bundle** its codes come from, which is the id a reader
  asks by, and carries that bundle's `variable` object verbatim ("Metadata
  JSON"), codebook included. The codebooks of one array's levels may differ
  in offset and scale. A variable id occurs once in a store. `numericId`
  inside a member is the bundle's own and means nothing here. Two arrays may
  name one bundle: a wind bundle's two components are the arrays `ugrd` and
  `vgrd`.
- `shards` holds the CRC-32 of each of the array's shard objects, in order.
  It makes the group document, and so the store's `crc32`, depend on every
  byte of the store: two stores of one run that differ in any object differ
  in their `?v=`. The writer writes it; a reader needs it for nothing.
- `consolidated_metadata` is present, inline, and holds every array document
  verbatim: the data arrays in the order of `arrays`, then `time`, `level`
  (where a levelled array exists), `latitude`, `longitude`, then each
  `time_<array>` and `level_<array>` in the order of `arrays`. A reader takes
  an array's document from it and fetches no `<array>/zarr.json`; it holds
  each data array's document to the block (type, shape, dimension names, one
  `sharding_indexed` codec, chunk shapes, chain, index codecs, `fill_value`)
  before using it. The document is serialized as every document of the
  profile is and is written last; its CRC-32 is the store's `crc32`.

**Array attributes.** `xue_columns`, `{"array": "<id>"}`; `long_name` and
`units` where every member has the same label and the same unit;
`scale_factor` and `add_offset` only where every member has the same linear
codebook (a levelled array whose levels differ has no CF spelling, and a
client dequantizes it by the members); `_FillValue`. In that order.

**Coordinates.** `time`, `latitude` and `longitude` are a grid store's
("Coordinates"), the true Gaussian rows included; `time_<array>` is the same
encoding over that array's axis. `level` (and `level_<array>`) is `int32`
hPa in the array's order, one chunk under `[bytes]` (little-endian), fill
`0`, attributes `standard_name: "air_pressure"`, `units: "hPa"`, `positive:
"down"`.

**Reading one cell's column.** Read the group document once per run. For
each array, `s = floor(row / shardHeight)`; fetch that shard's index once
and verify its CRC-32C; the cell's block is entry `floor((row − s ×
shardHeight) / blockHeight) × (shardWidth / blockWidth) + floor(column /
blockWidth)`. Fetch `[offset, offset + nbytes)`, decompress it to exactly
`frameCount × levelCount × blockHeight × blockWidth` bytes, replay the
running sum along time under the delta chain, and take the cell at (`row
mod blockHeight`, `column mod blockWidth`): `frameCount × levelCount` codes.
A value is the member's codebook applied to its level's codes. Neighbouring
cells of a block need no further request.

The size against the map store depends on the axis. On one GFS run's fifteen
levels, hourly over most of the axis, the five levelled arrays measured 2.99
GB at level 15 against the map store's 2.97 GB, one cell's blocks about 0.18
MB in five reads; blocks of 4 x 4 and 16 x 16 cells were 8 % and 3 % larger.
On ECMWF's three-hourly axis the `tmp` array measured 32 % over the map
store's arrays. The delta is smaller than RAW on the GFS run and was 6 % larger
on that array; an array still takes the one chain its variables' predictor
says. Other fields and sources were not measured.

**Writing.** Two writers, the reference (`xuebuild/columns.py`,
`write_column_store`) and the wheel's (`xue.write_columns`), take one plan
document, read the rest from the map stores it names and write the same
bytes for one libzstd version, whatever number of processes or threads cut
the shards. `build-columns` builds the plan from the source's
`column_arrays`, the registry and the run's manifest, runs either writer and
attaches the store to the run. The plan:

```json
{ "columnsPlan": 1, "arrays": [
  { "id": "tmp", "predictor": "previous", "levels": [1000, 975],
    "members": [ { "bundle": "tmp1000", "variable": "tmp1000", "store": "bundles.zarr" },
                 { "bundle": "tmp975", "variable": "tmp975", "store": "bundles.zarr" } ] },
  { "id": "orog", "predictor": "previous",
    "members": [ { "bundle": "orog", "variable": "orog", "store": "bundles.zarr" } ] } ] }
```

`store` is a map store of either profile, relative to the run's directory;
of it a writer opens `zarr.json`, whose consolidated metadata holds the
array documents, and `<variable>/c/0/0/0`. `levels` is present on a levelled
array only. A writer refuses a plan whose `columnsPlan` is not the integer `1`, an unknown
key or predictor, no array, an array id or a variable twice, an id the block
would refuse, `levels` that are not strictly descending positive integers
of at most 2147483647, one per member, or a surface array whose one member is not its id; members
whose `model`, `product`, `runTime`, `profile` or `grid` differ; members of
one array whose `time` or `nodataCode` differs; a projected grid; a map array
whose shape is not its bundle's `[frameCount, height, width]`, or that is
more than one shard; a `nodataCode` that is not a byte; and an output
directory that exists. A cut that fails removes its output. Values are equal
here when they are the same JSON: `1` and `1.0` differ, key order does not
matter. An array that mixes predicted and RAW variables has no plan, nor has
a store whose members' published isobaric surfaces are not its `levels`, and
`build-columns` attaches no store with an object over 512 MB.

Besides the codes, the bytes are fixed by these choices.

- The block's keys, in order: `version`, `model`, `product`, `runTime`,
  `profile`, `time`, `grid`, `arrays`; an array's: `id`, `time` (if its own),
  `levels` (if levelled), `members`, `shards`; a member's: `bundle`,
  `variable`. `time`, `grid` and `variable` are the bundle metadata's objects
  with their keys in their order and their numbers as the map store's group
  document has them: an integer stays one, and a float is its shortest round
  trip with a fractional part. The two writers' JSON libraries spell a float
  below 1e-4 or from 1e16 in magnitude (zero aside) differently, so no such
  number may occur in a codebook; the registry admits none. `profile` is copied with the other three. The
  arrays are in the plan's order and the store's `time` is the first one's.
- The group document's keys: `zarr_format`, `node_type`, `attributes` (holding
  `xue_columns` alone), `consolidated_metadata` (`kind`, `must_understand`,
  `metadata`), as in a grid store.
- A data array's document is a grid store's in every key and its order
  (`zarr_format`, `node_type`, `shape`, `data_type`, `chunk_grid`,
  `chunk_key_encoding`, `fill_value`, `codecs`, `attributes`,
  `dimension_names`; in the sharding configuration `chunk_shape`, `codecs`,
  `index_codecs`, `index_location`), with the attributes of "Array
  attributes" in that order. `scale_factor` and `add_offset` are the
  `scale` and `offset` of the one `quantization` object, of type `linear`,
  that every member carries identically. A coordinate's document is a grid
  store's coordinate document.
- A shard is its index, then the payloads back to back in index order, the
  first at offset `16 × chunksPerShard + 4`, with nothing between or after
  them. A payload is one Zstandard frame over one whole block in C order
  (frame, level, row, column), compressed alone, level 6, content checksum
  and content size on. Under the delta chain the block is differenced along
  frames with its padding cells included, which are `fill_value` in every
  frame. Every block the grid reaches is written, one that is all
  `fill_value` too; the blocks of a band past the grid's last row are the
  index entries after the written ones and are `2^64 − 1` twice.
- A cell of a map array's chunk that was never written is `fill_value`.

## Equivalence with the container

A build asked for `.xue` containers as well ([`format.md`](format.md))
writes them and the stores from the same codes, so the two carry identical
codes by construction. `tests/test_zarr.py` holds a NumPy-only
read-back of every frame to the codes a store was written from, and the
container parity test (`tests/test_native.py`) builds both deliveries and
reads the container back through the wheel's decoder. Compressed bytes are
identical where the layouts coincide (a store chunk covering exactly one of
the bundle's groups on a tile the grid does not clip) under the delta chain
for a predicted variable and under either chain for a RAW one.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
