# Xue Zarr Profile

This document specifies how a Xue bundle is written as a **Zarr v3 store**:
the same quantized codes as the `.xue` container, in a layout any Zarr
client reads. It is normative for what the exporter
(`xuebuild/zarrstore.py`, `xue export-zarr`, `build-bin --zarr`) writes and
for what a store must look like to be one of these. The container itself is
specified in [`format.md`](format.md); this profile adds nothing to it and
changes nothing in it.

Status: the store is **derived** from a bundle, never the other way round.
A build writes the `.xue` first, reads its codes back and writes the store
from them, so the two carry identical codes by construction. The frontend
plays a store **by default** wherever a bundle's manifest entry names one
(see "Reading"; `?backend=xue` asks for the container instead), and a run
whose entries name only their stores — no `.xue` at all — is accepted; a
run published without stores plays from its containers as before, and a
reader that does not know the manifest field ignores it. The container is
on its way to becoming a read-only legacy format: **HRRR already publishes
the store alone** (`build-bin --zarr --no-xue`: the `.xue` is still written,
the store derived from it, and the file retired before upload), the other
sources still publish the container, and every decoder keeps reading it
indefinitely, since runs, cases and rounds already published are never
rebuilt.

## Why a profile

A container v2 bundle is, to within its index format, a sharded Zarr v3
`uint8` array: one chunk is one spatial tile of one six-frame temporal
group, a group is one contiguous run of chunks in row-major tile order, each
chunk is one Zstandard frame, and the temporal residual is a wrapping
difference against the previous frame inside the chunk. Zarr spells the same
things as a `sharding_indexed` codec, a `zstd` codec and, optionally, an
array-to-array delta codec. Writing the convention down is what lets a
store written by anyone else be played the same way, and lets xarray, zarrs
or zarrita read a Xue run without a decoder of their own.

## Layout

One bundle is one Zarr **group**; one variable is one **array** in it, named
by the variable's `id`. Coordinates are three one-dimensional arrays beside
the variables.

```text
<model>.<run>/<bundle>.zarr/        tmp2m.zarr, wind10m.zarr, tmp2m.half.zarr
  zarr.json                         group; attributes.xue = the bundle's metadata JSON
  index.bin                         every shard's index, concatenated (recommended; below)
  <variable id>/                    tmp2m/; under wind10m.zarr: ugrd10m/ and vgrd10m/
    zarr.json                       array metadata (below)
    c/<t>/0/0                       one shard per time chunk t
  time/  latitude/  longitude/      coordinate arrays, one chunk each (c/0)
```

A half-resolution rendition is its own store, `<bundle>.half.zarr`, derived
from `<bundle>.half.xue` exactly as the full one is from `<bundle>.xue`.

## Array metadata

Every variable array is `uint8` of shape `[frameCount, height, width]`
(time, then the grid in the container's row-major, north-to-south,
west-to-east order) and carries exactly one codec, `sharding_indexed`.

```json
{
  "zarr_format": 3, "node_type": "array",
  "shape": [161, 721, 1440], "data_type": "uint8",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [6, 728, 1440]}},
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

- **Time chunk.** The outer chunk holds six frames on a *regular* grid:
  chunk `t` covers frames `[6t, 6t + 6)`. The container cuts its groups
  inside segments of constant step and restarts them where the step
  changes (GFS: a one-frame group at f120), so a store chunk may straddle
  two of the container's groups. The two layouts coincide up to the first
  change of step.
- **Inner chunk** = the bundle's tile, `[6, tileHeight, tileWidth]`. The
  inner chunks of a shard are the bundle's tiles in the bundle's row-major
  order; their count is `tileRows × tileColumns`.
- **Outer chunk (shard)** = one time chunk of the whole grid, rounded up to
  whole inner chunks along each axis because the sharding codec requires
  it: `[6, tileRows × tileHeight, tileColumns × tileWidth]` (GFS
  `[6, 728, 1440]`). The chunk grid may run past the array's shape; the
  cells beyond it, and the frames beyond the axis in the last time chunk,
  are stored as `fill_value`.
- **`fill_value`** is the variable's `nodataCode`. Padding, and any read
  past the grid, is explicitly nodata rather than a plausible low code.
- **Inner codec chain.** The default is the standard `[bytes, zstd]` with
  level 15 and a content checksum, which any Zarr client decodes with
  nothing registered. `--delta` puts `{"name": "xue.delta",
  "configuration": {"axis": 0}}` in front of `bytes` on every variable the
  bundle predicts from the previous frame, and never on a RAW one
  (precipitation, reflectivity).
- **Shard index.** One `(offset, nbytes)` pair of little-endian `uint64`
  per inner chunk, in inner-chunk row-major order, followed by the CRC-32C
  of those pairs as four little-endian bytes. Offsets are from the start of
  the shard. `index_location` is `end` by default — a client fetches the
  index as an HTTP suffix range without knowing the object's length, and
  zarrita reads only that form — and may be `start`. Both are written; a
  reader must accept both. A pair of `2^64 − 1` marks a chunk that was
  never written; the exporter writes every chunk.
- **`chunk_key_encoding`** is `default` with the `/` separator, so shard `t`
  is the object `c/<t>/0/0`.

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

## Attributes

The group's `zarr.json`:

```json
"attributes": {
  "xue": { "schemaVersion": 3, "model": "…", "product": "…", "runTime": "…",
           "profile": "…", "time": { "…" }, "grid": { "…" }, "variables": [ "…" ] },
  "xue_profile": 1,
  "xue_index": { "path": "index.bin", "shardIndexBytes": 6724, "timeChunks": 27,
                 "arrays": ["tmp2m"] }
}
```

`xue` is the bundle's metadata JSON **verbatim** — the same document the
container embeds ([`format.md`](format.md), "Metadata JSON") — so the parsers
that read a bundle's metadata read a store's. `xue_profile` is this
document's version. `xue_index` describes the whole-store index (below) and
is present exactly when the store carries one.

Each variable array's `zarr.json`:

```json
"attributes": {
  "xue": {
    "variable": { "numericId": 1, "id": "tmp2m", "label": "…", "unit": "°C",
                  "parameter": { "…" },
                  "quantization": { "type": "linear", "offset": -60.0, "scale": 0.5,
                                    "minimumCode": 0, "maximumCode": 220, "nodataCode": 255 } },
    "predictor": "previous"
  },
  "scale_factor": 0.5, "add_offset": -60.0, "_FillValue": 255
}
```

- `xue.variable` is that variable's entry from the metadata's `variables`
  list, copied, for a reader that opens one array alone.
- `xue.predictor` is `previous` or `raw` and is informational: decoding
  follows the codec chain.
- `_FillValue` is the `nodataCode` for every variable. `scale_factor` and
  `add_offset` are written for a **linear** codebook only, so a CF-aware
  client dequantizes it (value = `code × scale + offset`); the `log1p`
  codebook has no CF spelling and is described by
  `xue.variable.quantization` alone.
- Dimensions are declared in the array metadata's `dimension_names`, the
  Zarr v3 field.

## Whole-store index

Recommended, not required. A `sharding_indexed` shard keeps its own index
at one end of itself, so the first inner chunk of any shard costs two
dependent requests — the index, then the chunk — and a point series, which
touches every shard once, pays `2 × timeChunks` where the container pays
one chunk per group over an index its structural prefix already holds.
The store's answer is one object, `index.bin` at the store's root, that is
**the verbatim concatenation of every shard's index**: for each variable
array in the order the group's `xue.variables` lists them, for each time
chunk `t` in order, the `16 × innerChunkCount + 4` bytes the shard stores
(the `(offset, nbytes)` pairs and their CRC-32C, exactly as they sit at the
shard's `index_location`, whichever end that is). Offsets remain relative
to their shard. The group's `xue_index` attribute says how to cut it:

```json
"xue_index": { "path": "index.bin", "shardIndexBytes": 6724, "timeChunks": 27,
               "arrays": ["ugrd10m", "vgrd10m"] }
```

Array `a` (its position in `arrays`), time chunk `t` is the block at
`(a × timeChunks + t) × shardIndexBytes`. Every array of a store shares one
tiling and one axis, so one block size and one time-chunk count describe
all of them, and the object is exactly `arrays.length × timeChunks ×
shardIndexBytes` long. The object counts in the manifest descriptor's
`byteLength` like any other and is versioned by the store's `?v=`. A
standard Zarr client never looks for it and is unaffected by it; the shards
are unchanged, so a store without the object reads exactly as before.

What it buys: a reader fetches the object whole at open — a few tens of
kilobytes on a production grid: 6,724 bytes per shard on the 0.25° GFS
tiling (420 tiles), 60,516 bytes for a 49-frame scalar and twice that for
the wind pair, 181,548 bytes on the full 161-frame axis — verifies each
block's CRC-32C with the
same parser a shard's own index goes through, and holds every offset up
front, the way the container holds its whole index in the prefix. A
series is then opening plus one chunk per time chunk; a global view or a
viewport is one request fewer per shard. The object is a seed, not a
dependency: a reader that cannot fetch it, finds it the wrong length, or
finds a block whose checksum fails, reads that shard's own index as it
would have without it.

## Coordinates

`time` is `int32`, `units: "seconds since <runTime>"`, `calendar:
"proleptic_gregorian"`, values `frameOffset × unitSeconds` for every frame
of the axis. `latitude` and `longitude` are `float64` cell centres expanded
from the metadata's `grid` (`first + i × step`, in the grid's row and column
order). Each is one chunk, `bytes` little-endian, uncompressed. They are for
xarray and its kind; the viewer reads the group's `xue.time` and `xue.grid`.

## Manifest descriptor

A bundle entry in `manifest.json` (schema 5, unchanged), and each entry in
its `variants`, may carry:

```json
"zarr": { "path": "tmp2m.zarr", "byteLength": 12347075, "crc32": "760cef95" }
```

`path` is the store's root, relative to the manifest, under the same rules
as the bundle path but ending in `.zarr` and colliding with no other path in
the manifest; `byteLength` is the sum of every object in the store; `crc32`
is the CRC-32 of the group's `zarr.json`, the one value a client appends as
`?v=` to every object it fetches from the store. Every object under the run
directory is immutable, as the bundles are. All three validators
(`xuebuild/manifest.py`, `rust/xue/src/encode/manifest.rs`,
`web/src/manifest.ts`) check the field only when present, and hold an entry
to naming **at least one** delivery: the container's `path`, `byteLength`
and `crc32` are one unit, present whole or absent whole, so an entry may
carry the container, the store, or both, never neither.

## Reading

What a player needs from a store, in the order it needs it. The frontend's
own reader (`web/src/zarr/`) is one implementation; the rules are the
profile's.

1. **The group.** Fetch `zarr.json` at the store's root. `attributes.xue`
   is the bundle metadata verbatim: the grid, the time axis, the variable
   set with their codebooks and GRIB2 parameter blocks. It is parsed with
   the same rules as the container's embedded metadata (`format.md`,
   "Metadata JSON"), so a player that reads bundles already reads this. A
   reader must refuse a group without the block or with an `xue_profile` it
   does not implement, and — when `xue_index` is present — one whose
   `shardIndexBytes` is not `16 × tileCount + 4` for the arrays' tiling,
   whose `timeChunks` is not the arrays', or whose `arrays` omits a
   variable: a store whose index describes other arrays is malformed.
2. **Each array.** Fetch `<id>/zarr.json` for every variable the metadata
   lists (the array's name is the variable's `id`; `numericId` is the
   handle a player's own protocol uses). Validate before trusting: `uint8`,
   three dimensions, a regular chunk grid, exactly one `sharding_indexed`
   codec, an inner chain of `[bytes, zstd]` or `[xue.delta{axis: 0},
   bytes, zstd]`, index codecs `[bytes, crc32c]` little-endian,
   `index_location` `start` or `end`, `fill_value` a byte, and an outer
   chunk equal to `[timeChunk, tileRows × tileHeight, tileColumns ×
   tileWidth]`. Every array of one store is cut the same way. The tiling
   this yields — `tileWidth`, `tileHeight`, `tileColumns`, `tileRows` — is
   the same shape the container reports, and a viewport maps to a
   rectangle of inner chunks the same way.
3. **Locating a frame.** A frame's index on the axis is its position in
   `frameOffsets` (or its distance from `firstFrameOffset` in `frameStep`s),
   and its time chunk is `floor(index / timeChunk)`, its frame within the
   chunk `index mod timeChunk`. The time chunk is fixed at six on a regular
   grid and **may straddle two of the container's groups** (GFS: from the
   change of step at 120 h on); a reader never consults the container's
   grouping. Inner chunk `t` of shard `c/<time chunk>/0/0` is tile `t` in
   row-major order, `tileColumns` per row.
4. **The shard index.** With `xue_index` present, fetch `index.bin` whole
   once at open and take each shard's index as the block at `(a ×
   timeChunks + t) × shardIndexBytes`; without it, or for a block that
   fails to verify, or an object that cannot be fetched or is not exactly
   `arrays.length × timeChunks × shardIndexBytes` long, read the shard's
   own: `16 × tileCount + 4` bytes at the end of the shard
   (`index_location: "end"`, fetched as the HTTP suffix range
   `bytes=-N` without knowing the object's length) or at its start
   (`bytes=0-(N−1)`). Either way, verify the trailing CRC-32C over the
   pairs before using any offset; a pair of `2^64 − 1` is a chunk the
   shard never held, which reads as `fill_value` throughout. Cache the
   index per shard: a frame's siblings in the same time chunk, and a
   series, reuse it.
5. **Inner chunks.** Fetch `[offset, offset + nbytes)` of the shard.
   Decompress the Zstandard frame to exactly `timeChunk × tileHeight ×
   tileWidth` bytes — refuse a frame without a content checksum, a checksum
   that does not match, or an output of any other length — and, under the
   delta chain, replay the modulo-256 running sum along the frame axis.
   This is the container's chunk path, and `decodeChunk` in the WASM
   decoder does exactly it for bytes that did not come out of a `.xue`
   index.
6. **Trimming the padding.** An inner chunk is stored whole. Its rows past
   `height`, its columns past `width` (the last tile row and column) and,
   in the last time chunk, its frames past `frameCount` are padding: copy
   only `min(tileHeight, height − row)` rows of `min(tileWidth, width −
   column)` cells for the frames that lie on the axis.
7. **Coalescing ranges.** A shard's inner chunks are contiguous in tile
   order, so a tile row is one contiguous span of the object. A reader
   that issues one range per inner chunk pays one round trip per tile — a
   6 × 4 tile viewport costs 24 requests where the container costs 4. The
   profile's recommendation is to gather the ranges of one shard that a
   read needs, sort them by offset, merge neighbours whose gap is small
   (the frontend uses 64 KB), and fetch each run as one range; the
   over-read of a merged gap is cheaper than the round trip it saves. With
   that, a global view is one range per shard, a viewport one range per
   tile row per shard, and a point series one chunk per time chunk — plus,
   without the whole-store index, one index read per shard the reader has
   not seen, the request the container's prefix-sum table never needs.

With the whole-store index a series costs opening plus `timeChunks`
requests, the container's own shape (its prefix plus one chunk per group);
without it `2 × timeChunks` (index and chunk per shard). Both are
independent of the frame count.

## Equivalence with the container

The codes are identical by construction: the exporter re-encodes every
chunk from the codes the bundle decodes to, and `tests/test_zarr.py` holds a
NumPy-only read-back of every frame to `decode_plane`. Compressed bytes are
identical where the layouts coincide — a store chunk covering exactly one of
the bundle's groups on a tile the grid does not clip — under the delta chain
for a predicted variable and under either chain for a RAW one; the export
report counts those chunks (`comparableChunks`, `identicalChunks`) rather
than assuming it. On a 49-frame GFS run every comparable chunk was identical.
