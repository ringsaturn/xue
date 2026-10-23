# Xue Zarr Profile

This document specifies how Xue bundles are written as Zarr v3 stores: the
same quantized codes as the `.xue` container, in a layout any Zarr client
reads. It is normative for what the store writers (`xuebuild/zarrstore.py`,
the native encoder's `encode/zarrstore.rs`, `build-bin`, `xue export-zarr`)
write and for what a store must look like to be one of these. The container itself is specified in
[`format.md`](format.md); this profile adds nothing to it and changes
nothing in it. A second branch, the [polar store](#polar-store), carries
radar sweeps in beam-and-gate coordinates for products that have no grid
and no container, and a third, the [network store](#network-store), one
value per river reach per frame.

A store is a function of its bundles' metadata, their quantized codes, their
tiles, each variable's predictor and each bundle's dimension names (see
"Coordinates"), and of nothing else. A build writes it
straight from the codes it quantized; where it writes a `.xue` as well, the
container comes from the same codes, so the two carry identical codes by
construction, and `xue export-zarr` derives the same store from a finished
`.xue`. The frontend plays a store by default wherever a bundle's manifest
entry names one (see "Reading"; `?backend=xue` asks for the container
instead), and a run whose entries name only their stores is accepted; a run
published without stores plays from its containers, and a reader that does
not know the manifest field ignores it. The store is the published
artifact: every live run, rolling-window round and showcase case publishes
the store alone, with no `.xue` written at all, and a [STAC
catalog](stac.md) lists them. The container is a legacy format that every
decoder keeps reading, since runs, cases and rounds already published are
never rebuilt.

## Why a profile

A container v2 bundle is, to within its index format, a sharded Zarr v3
`uint8` array: one chunk is one spatial tile of one six-frame temporal
group, a group is one contiguous run of chunks in row-major tile order, each
chunk is one Zstandard frame, and the temporal residual is a wrapping
difference against the previous frame inside the chunk. Zarr spells the same
things as a `sharding_indexed` codec, a `zstd` codec and, optionally, an
array-to-array delta codec. Writing the convention down lets a store written
by anyone else be played the same way, and lets xarray, zarrs or zarrita
read a Xue run without a decoder of their own.

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

`bundles` is a reserved store stem: no bundle id may be `bundles`. A store
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
(time, then the grid in the container's row-major, north-to-south,
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
  nothing registered. `--delta` puts `{"name": "xue.delta",
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
the running sum, modulo 256. On `uint8` this is the container's PREVIOUS
predictor ([`format.md`](format.md), "Temporal Prediction") moved into the
codec chain: an inner chunk under `[xue.delta, bytes, zstd]` compresses to
the same bytes as the bundle's chunk over the same frames and the same
unclipped tile. It is not numcodecs' `Delta`, which differences the
flattened buffer. `xuebuild/zarrcodec.py` implements it for zarr-python
(`register()`); it is only needed to read a store written with `--delta`.

## Group document

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
  verbatim, the same document the container embeds ([`format.md`](format.md),
  "Metadata JSON"), so the parsers that read a bundle's metadata read a
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
  coordinates `time`, `latitude`, `longitude`, those some bundle uses; then
  every other coordinate array in the order its first bundle names it
  (`time_<bundle id>`, `latitude_<family>`, `longitude_<family>` within
  one bundle).
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
`longitude`). A store written before consolidated metadata lacks it and
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
- `_FillValue` is the `nodataCode` for every variable. `scale_factor` and
  `add_offset` are written for a linear codebook only, so a CF-aware client
  dequantizes it (value = `code × scale + offset`); the `log1p` codebook has
  no CF spelling and is described by `xue.variable.quantization` alone.
- Dimensions are declared in the array metadata's `dimension_names`, the
  Zarr v3 field: `["time", "latitude", "longitude"]`, or the bundle's own
  names (below). Every array of one bundle carries the same three.

## Coordinates

The coordinate arrays are one-dimensional, one chunk each under `[bytes]`
(little-endian), named by the dimension they label: the frame's offset from
the bundle's `runTime` in seconds (`int32`, `units: "seconds since
<runTime>"`, `calendar: "proleptic_gregorian"`, `standard_name: "time"`,
fill `0`), the cell centre latitudes and longitudes (`float64`,
`degrees_north` / `degrees_east`, fill `NaN`) from the bundle's grid. They
are for xarray and its kind; a player reads the metadata.

- The **primary coordinates** `time`, `latitude` and `longitude` are the
  run's axis and the source's primary grid at the store's tier. They are
  written once per store.
- A bundle whose time axis is cut by a rule of its own takes the dimension
  `time_<bundle id>`, and the store carries a coordinate array of that name
  with that bundle's axis (same encoding as `time`). The rules are the two
  by which the encoder shortens a bundle's axis: a published scalar whose
  series has no analysis frame (`binconvert.analysis_optional_ids`: a
  de-accumulated or de-averaged rate, an interval maximum such as `gust`),
  and a static field (the terrain, one frame at the analysis).
- A bundle on a grid family of its own takes `latitude_<family>` and
  `longitude_<family>`, `<family>` being exactly the string
  `binconvert.bundle_grid_family` returns for it: the id of the companion
  file family its inputs are read from (`pgb`, the CFSv2 pressure levels on
  their 1° grid, so `latitude_pgb`). Every bundle of one family shares the
  pair, as the primary grid's bundles share `latitude` / `longitude`. A
  family id matches `^[a-z][a-z0-9]*$`.
- The decision is the rule, not a comparison of axes, so it is a function of
  the source registry and the bundle id alone
  (`binconvert.store_dimensions`): a rate gets `time_prate` even on a build
  whose axis happens to start past the analysis. A fanned-out job building
  a subset of the bundles therefore writes the same objects a whole build
  does, and every job writes the primary coordinates it needs, the same
  bytes each time. A multi-variable bundle's arrays share one set of names.
- Two bundles naming one coordinate carry the same document and bytes for
  it; a writer refuses to merge them otherwise.

A bundle's dimension names are `time` or `time_<its bundle id>`, then
`latitude` and `longitude`, or `latitude_<family>` and `longitude_<family>`
with one family id matching `^[a-z][a-z0-9]*$`; any other name is
invalid.

## Manifest

A manifest entry's `zarr` descriptor is unchanged in shape: `{path,
byteLength, crc32}`, or the segmented form (see "Segmented stores").

- `path` names the shared store of the entry's tier: `bundles.zarr` on the
  bundle, `bundles.half.zarr` on its half variant, and so on. Every entry of
  a tier carries the same path. The validators (`xuebuild/manifest.py`,
  `rust/xue/src/encode/manifest.rs`, `web/src/manifest.ts`) exempt exactly
  these paths from the duplicate-path rule: a map store's path, or a
  segment's, whose **final path component** is exactly `bundles.zarr` or
  `bundles.<tier>.zarr`, `<tier>` a non-empty run of lowercase ASCII
  letters other than `series` (any such tier, not only `half`, `quarter`
  and `eighth`). Every other path — a container, a series store (so
  `bundles.series.zarr` too), a poster, a video, a one-bundle store — stays
  unique in the manifest.
- `byteLength` is the bundle's share: the objects of its variable arrays
  (each `zarr.json` and shard) and of the coordinate arrays it uses (each
  `zarr.json` and chunk). The group document is not counted. It is what a
  viewer downloads for that bundle alone, which is what the delivery sizes
  and a tier's `bandwidth` are computed from.
- `crc32` is the CRC-32 of the store's group document, identical on every
  entry naming the store.

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
arrays already on the bucket are not touched.

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
   blocks. It is parsed with the same rules as the container's embedded
   metadata (`format.md`, "Metadata JSON"), so a player that reads bundles
   already reads this. A reader must refuse a group with an `xue_profile`
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
  frames by the container's rules (the coarsest `unitSeconds` that fits,
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
  metadata as the bytes a container embeds (`binformat.metadata_json_bytes`); the JSON
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
  this branch's version, `1`. A reader tells the three branches apart by
  which block is present and refuses a group carrying more than one of
  `xue`, `xue_polar` and `xue_network`, or a `version` it does not
  implement.
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
  window. `time` follows the container's rules exactly
  ([`format.md`](format.md), "Time Axis"): offsets from `runTime` on the
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
- Codes. The `log1p` codebook of [`format.md`](format.md) with `trace`
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

## Equivalence with the container

The codes are identical by construction: a build writes the store and the
container from the same codes, `xue export-zarr` re-encodes every chunk
from the codes a bundle decodes to (into a one-bundle store), and `tests/test_zarr.py` holds a
NumPy-only read-back of every frame to `decode_plane` and a store written
from the codes to the one exported from their bundle, object for object. Compressed bytes are
identical where the layouts coincide (a store chunk covering exactly one of
the bundle's groups on a tile the grid does not clip) under the delta chain
for a predicted variable and under either chain for a RAW one; the export
report counts those chunks (`comparableChunks`, `identicalChunks`) rather
than assuming it. On a 49-frame GFS run every comparable chunk was
identical.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
