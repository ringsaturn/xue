# Xue Zarr Profile

This document specifies how a Xue bundle is written as a Zarr v3 store: the
same quantized codes as the `.xue` container, in a layout any Zarr client
reads. It is normative for what the exporter (`xuebuild/zarrstore.py`, `xue
export-zarr`, `build-bin --zarr`) writes and for what a store must look like
to be one of these. The container itself is specified in
[`format.md`](format.md); this profile adds nothing to it and changes
nothing in it.

The store is derived from a bundle, never the other way round. A build
writes the `.xue` first, reads its codes back and writes the store from
them, so the two carry identical codes by construction. The frontend plays
a store by default wherever a bundle's manifest entry names one (see
"Reading"; `?backend=xue` asks for the container instead), and a run whose
entries name only their stores is accepted; a run published without stores
plays from its containers, and a reader that does not know the manifest
field ignores it. Since 2026-09-15 the store is the published artifact:
every live run, rolling-window round and showcase case publishes the store
alone (`build-bin --zarr --no-xue`: the `.xue` is written, the store derived
from it, and the file retired before upload), a [STAC catalog](stac.md)
lists them, and the container is a build intermediate and a legacy format
that every decoder keeps reading, since runs, cases and rounds already
published are never rebuilt.

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

One bundle is one Zarr group; one variable is one array in it, named by the
variable's `id`. Coordinates are three one-dimensional arrays beside the
variables.

```text
<model>.<run>/<bundle>.zarr/        tmp2m.zarr, wind10m.zarr, tmp2m.half.zarr
  zarr.json                         group; attributes.xue = the bundle's metadata JSON
  <variable id>/                    tmp2m/; under wind10m.zarr: ugrd10m/ and vgrd10m/
    zarr.json                       array metadata (below)
    c/0/0/0                         the array's one shard: the whole axis of the whole grid
  time/  latitude/  longitude/      coordinate arrays, one chunk each (c/0)
```

A store is a handful of objects (three per scalar variable, two more per
extra variable, six for the coordinates) whatever the length of its axis. A
bucket bills every object written, and a store cut one shard per time chunk
cost a GFS run some five thousand objects where the container cost sixty. A
shard the size of the array is one object read by range, as the container
was, and its index is the whole index, read once.

A reduced-resolution rendition is its own store, `<bundle>.<tier>.zarr`,
derived from `<bundle>.<tier>.xue` exactly as the full one is from
`<bundle>.xue`. The ladder is per source (`SourceSpec.variant_factors`):
every source has the half (`tmp2m.half.zarr`, the grid decimated by two),
and the satellite disks add the quarter and the eighth (`ir104.quarter.zarr`,
`ir104.eighth.zarr`; 3000 → 1500 → 750 → 375 cells a side, the tile 64 →
32 → 16 → 8), each decimated from the full plane.

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

## Attributes

The group's `zarr.json`:

```json
"attributes": {
  "xue": { "schemaVersion": 3, "model": "…", "product": "…", "runTime": "…",
           "profile": "…", "time": { "…" }, "grid": { "…" }, "variables": [ "…" ] },
  "xue_profile": 1
}
```

`xue` is the bundle's metadata JSON verbatim, the same document the
container embeds ([`format.md`](format.md), "Metadata JSON"), so the
parsers that read a bundle's metadata read a store's. `xue_profile` is this
document's version. A store written before this revision also carries a
`xue_index` attribute naming a whole-store index object, `index.bin`; a
reader ignores both, since the shard's own index is the whole index.

The group document also carries consolidated metadata, in the Zarr v3
inline form zarr-python reads:

```json
"consolidated_metadata": {
  "kind": "inline", "must_understand": false,
  "metadata": { "tmp2m": { "…the array's zarr.json…" }, "time": { "…" },
                "latitude": { "…" }, "longitude": { "…" } }
}
```

Every array document of the store is repeated verbatim, so a client on an
origin that cannot be listed (a plain HTTP bucket, where every store is
served from) still discovers the arrays: `xr.open_zarr(url)` works with no
`consolidated=` argument and no directory listing. The group document is
written last, after the arrays, and the store's `crc32` (its `?v=`) is the
CRC-32 of this document, so it covers every array's metadata too. A reader
that opens arrays by name (the frontend, `zarr`'s `open_group(url)["tmp2m"]`)
never needs it; a store written before this revision lacks it and opens
with `consolidated=False` from a listable store only.

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
  `add_offset` are written for a linear codebook only, so a CF-aware client
  dequantizes it (value = `code × scale + offset`); the `log1p` codebook has
  no CF spelling and is described by `xue.variable.quantization` alone.
- Dimensions are declared in the array metadata's `dimension_names`, the
  Zarr v3 field.

## Reading

What a player needs from a store, in the order it needs it. The frontend's
own reader (`web/src/zarr/`) is one implementation; the rules are the
profile's.

1. The group. Fetch `zarr.json` at the store's root. `attributes.xue` is
   the bundle metadata verbatim: the grid, the time axis, the variable set
   with their codebooks and GRIB2 parameter blocks. It is parsed with the
   same rules as the container's embedded metadata (`format.md`, "Metadata
   JSON"), so a player that reads bundles already reads this. A reader must
   refuse a group without the block or with an `xue_profile` it does not
   implement.
2. Each array. Fetch `<id>/zarr.json` for every variable the metadata lists
   (the array's name is the variable's `id`; `numericId` is the handle a
   player's own protocol uses). Validate before trusting: `uint8`, three
   dimensions, a regular chunk grid, exactly one `sharding_indexed` codec,
   an inner chain of `[bytes, zstd]` or `[xue.delta{axis: 0}, bytes,
   zstd]`, index codecs `[bytes, crc32c]` little-endian, `index_location`
   `start` or `end`, `fill_value` a byte, and an outer chunk equal to
   `[shardFrames, tileRows × tileHeight, tileColumns × tileWidth]` with
   `shardFrames` a positive multiple of the time chunk (the whole axis
   rounded up on a store this revision writes, six on one written before).
   Every array of one store is cut the same way. The tiling this yields
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

## Equivalence with the container

The codes are identical by construction: the exporter re-encodes every
chunk from the codes the bundle decodes to, and `tests/test_zarr.py` holds a
NumPy-only read-back of every frame to `decode_plane`. Compressed bytes are
identical where the layouts coincide (a store chunk covering exactly one of
the bundle's groups on a tile the grid does not clip) under the delta chain
for a predicted variable and under either chain for a RAW one; the export
report counts those chunks (`comparableChunks`, `identicalChunks`) rather
than assuming it. On a 49-frame GFS run every comparable chunk was
identical.
