# Xue Container Format

This document is the normative specification of the Xue container (`.xue`),
a single indexed file holding one bundle: the metadata and quantized codes
of [`bundle.md`](bundle.md), framed so a browser can decode a run frame by
frame from HTTP range reads.

The container is a legacy layout. Every live run, round and showcase case
is published as a Zarr v3 store ([`zarr-profile.md`](zarr-profile.md))
alone, and a build writes the store straight from its codes; an encoder
writes a container only when asked (`--xue`, `container=True`). Every
decoder keeps reading it, since runs and cases published as containers are
never rebuilt: for a reader nothing here is deprecated.

Two container versions exist, and they differ in what a payload is:

- v1 is plane-major: one payload is one whole plane of one frame. Sections
  marked *(v1)* describe it. Nothing writes it any more; every decoder must
  keep reading it.
- v2 is tiled: one payload is a chunk, one spatial tile of one temporal
  group for one variable, so a reader can fetch the region it is showing
  and read one cell's whole series at one chunk per group. Both encoders
  write it. See *[Container v2](#container-v2)*.

Both share the fixed header and the payload rules below; the metadata,
the codebooks and the residual arithmetic are [`bundle.md`](bundle.md)'s.
A decoder rejects a container version it does not implement, so a
v2-capable shell must be deployed before the first v2 run is published.

The Python encoder (`xuebuild/binformat.py`) and the Rust decoder
(`rust/xue`) are the two reference implementations, held byte-identical by
cross-language golden tests.

## Design Summary

The format targets a product that animates a complete global forecast
(steps of one to six hours out to 240 hours, changing step partway through
the range) with continuous playback and timeline scrubbing. For one
121-frame GFS run, per-frame raster PMTiles for two variables measured
3,205.87 MB against 137.73 MB of source GRIB. Xue stores each variable as
quantized single-byte planes on the native grid (projection and coloring
happen on the GPU) with bounded temporal prediction and per-payload
Zstandard compression; the same two variables fit in 65–71 MB for that run
depending on profile. Size grows roughly linearly with frame count;
coarser-step frames have larger inter-frame differences and compress
slightly worse. Every frame is individually addressable, and the index makes
HTTP-range streaming possible: a client fetches the structural prefix (a few
KB) and then range-requests one temporal group or one chunk at a time.

Design decisions:

- Quantized uint8 planes. Each decoded frame is a contiguous single-channel
  plane that uploads directly to a WebGL2 `R8` texture. Codebooks are
  visualization-oriented (0.25 °C temperature error budget, a logarithmic
  precipitation codebook that preserves light-rain resolution).
- An axis that names its own unit and may change step. GFS is hourly to
  f120 and three-hourly beyond, ECMWF three-hourly to 144 hours and
  six-hourly beyond, and the radar mosaic publishes every six minutes. The
  axis declares the seconds one step unit is worth and either a uniform
  step or its offsets outright, so it is always exact.
- Bounded temporal groups. Smooth fields are coded in six-frame groups with
  one-byte residuals; groups are formed inside a segment of constant step,
  so no group straddles a change of cadence. In v1 a group is a middle RAW
  anchor plus residuals against it, capping random access at two plane
  decodes; in v2 a group is packed whole into each tile's chunk and the
  residuals chain against the previous frame, which is smaller and, since
  the chunk decompresses as a unit, no more expensive to seek into.
  Precipitation regions move with weather systems and temporal differencing
  increases their compressed size, so precipitation is stacked RAW in both
  versions.
- One independent Zstandard frame per payload (a plane in v1, a chunk in
  v2) for direct indexing, sequential reads, and localized failure
  isolation.
- Space is addressable in v2. A payload covers one tile of the grid, so a
  zoomed-in viewport fetches only the tiles it shows, and one cell's whole
  series costs one chunk per group. Tiles are cut in grid space, not Web
  Mercator; reprojection stays in the fragment shader.
- Strict parsing. All offsets and lengths are validated with checked
  arithmetic before any allocation; sections must be strictly adjacent with
  zero padding; every reconstructed plane carries a CRC-32.

## Conventions

- All binary integers are little-endian.
- All offsets are absolute (relative to the beginning of the file).
- Structural sections are aligned to 8 bytes; `align8(x)` rounds `x` up to
  the next multiple of 8. Padding bytes must be zero.
- Metadata is UTF-8 JSON; the index uses fixed-width binary entries.
- Quantization follows [`bundle.md`](bundle.md), "Conventions".

## Container Layout

Both versions have the same five sections in the same order; the index and
what the payloads are differ. This is v1; see
*[Container v2](#container-v2)* for the tiled index.

```text
+------------------------------+ 0
| FixedHeader, 80 bytes        |  version = 1
+------------------------------+ metadataOffset
| UTF-8 metadata JSON          |
+------------------------------+ indexOffset
| IndexHeader, 16 bytes        |
| PlaneEntry[entryCount]       |
+------------------------------+ dictionaryOffset, optional
| Zstandard dictionary         |
+------------------------------+ dataOffset
| Zstandard payloads           |
| zero padding for alignment   |
+------------------------------+ fileSize
```

### FixedHeader

The magic value is the 8-byte sequence `XUE\0\0\0\0\0` (ASCII `XUE`
followed by five NUL bytes).

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 8 | bytes | magic | `XUE\0\0\0\0\0` |
| 8 | 2 | u16 | version | 1 for v1 |
| 10 | 2 | u16 | headerSize | Fixed at 80 |
| 12 | 4 | u32 | flags | Fixed at 0 for v1 |
| 16 | 8 | u64 | fileSize | Must equal the actual file length |
| 24 | 8 | u64 | metadataOffset | Fixed at 80 for v1 |
| 32 | 8 | u64 | metadataLength | JSON byte count, excluding padding |
| 40 | 8 | u64 | indexOffset | 8-byte aligned |
| 48 | 8 | u64 | indexLength | Includes IndexHeader and every entry |
| 56 | 8 | u64 | dataOffset | 8-byte aligned |
| 64 | 8 | u64 | dictionaryOffset | 8-byte aligned, 0 when no dictionary |
| 72 | 8 | u64 | dictionaryLength | Dictionary byte count, 0 when no dictionary |

Before allocating memory, the parser must validate checked addition and
multiplication, monotonic offsets, and containment of every range within
`fileSize`.

Sections are strictly adjacent, separated only by zero padding for 8-byte
alignment. The parser must verify:

- `metadataOffset = 80`.
- `indexOffset = align8(metadataOffset + metadataLength)`.
- When `dictionaryLength` is 0, `dictionaryOffset` must be 0 and
  `dataOffset = align8(indexOffset + indexLength)`.
- When `dictionaryLength` is nonzero,
  `dictionaryOffset = align8(indexOffset + indexLength)` and
  `dataOffset = align8(dictionaryOffset + dictionaryLength)`.
- Payloads are stored contiguously in physical order starting at
  `dataOffset`, with no per-payload alignment and no unindexed gaps.
- `fileSize = align8(end of the last payload)`, and every padding byte is
  zero.

Unindexed gaps, overlaps, and nonzero padding bytes make the file invalid.

### Metadata JSON

The UTF-8 metadata JSON of [`bundle.md`](bundle.md), "Metadata JSON",
verbatim.

### IndexHeader (v1)

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 4 | bytes | magic | ASCII `IDX1` |
| 4 | 2 | u16 | entrySize | Fixed at 40 for v1 |
| 6 | 2 | u16 | version | Fixed at 1 for v1 |
| 8 | 4 | u32 | entryCount | Must equal (variables × frames per variable) declared in metadata |
| 12 | 4 | u32 | reserved | Must be 0 |

### PlaneEntry (v1)

Index entries are sorted by `(variableId, frameOffset)`. Each entry is 40
bytes:

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 1 | u8 | variableId | A handle into this file's metadata; see below |
| 1 | 1 | u8 | predictor | See enum below |
| 2 | 1 | u8 | compression | 0 NONE, 1 ZSTD, 2 ZSTD_DICT |
| 3 | 1 | u8 | flags | See flags below |
| 4 | 2 | u16 | frameOffset | This plane's offset on the metadata time axis |
| 6 | 2 | u16 | dependencyOffset | 65535 when no dependency exists |
| 8 | 2 | u16 | groupId | Temporal group ID; equals frameOffset for RAW-only series |
| 10 | 2 | u16 | reserved0 | Must be 0 |
| 12 | 4 | u32 | compressedLength | Payload byte count |
| 16 | 8 | u64 | dataOffset | Absolute payload offset |
| 24 | 4 | u32 | decodedLength | Must equal `grid.width × grid.height` |
| 28 | 4 | u32 | crc32 | CRC-32 of the reconstructed quantized plane |
| 32 | 1 | u8 | minimumCode | Actual minimum code in the final plane |
| 33 | 1 | u8 | maximumCode | Actual maximum code in the final plane |
| 34 | 6 | bytes | reserved1 | Every byte must be 0 |

Predictor enum:

| Value | Name | Meaning |
|---:|---|---|
| 0 | RAW | Decompressed payload is the complete quantized plane |
| 1 | ANCHOR | Payload is a residual relative to dependencyOffset |
| 2 | PREVIOUS | Payload is a residual relative to the preceding frame on this variable's time axis |
| 3 | ZERO | No payload, output a zero-filled plane, reserved for local block formats |

Flags:

- Bit 0: Zstandard frame includes a checksum.
- All other bits must be 0.

Residuals always use modulo-256 wrapping semantics, so no flag describes
residual interpretation. Overflow presence is derivable from `maximumCode`,
so no flag duplicates it.

The `crc32` field uses CRC-32/IEEE (polynomial `0x04C11DB7`, reflected
implementation `0xEDB88320`, initial value and final XOR `0xFFFFFFFF`,
identical to zlib's `crc32`), computed over the reconstructed quantized
plane. The Zstandard checksum validates the compressed payload; the CRC32
validates predictor reconstruction.

#### `variableId` is file-local

`variableId` is a handle, not a name: it ties an index entry to one variable
descriptor in this file's metadata. It must be 1–255 and unique within the
file, and every id an index entry names must appear in the file's
`variables`; there is no global table to check it against, and a decoder
must not carry one. What a variable is lives in the metadata: its
`parameter` block (see [`bundle.md`](bundle.md), "Variable Identity") and its
`id` string.

The encoders in this repository assign ids positionally: 1…n by the
variable's order in the bundle's variable list, which is also the order the
v2 chunk layout uses. A single-variable bundle is always id 1, and a vector
bundle is 1 for the U component and 2 for the V component.

Historical assignment: encoders before this rule drew ids from a global
registry, and files already published carry those numbers: 1 `tmp2m`,
2 `prate`, 3 `ugrd10m`, 4 `vgrd10m`, 5 `dswrf`, 6 `cref`, 7 `prmsl`,
8–15 `hgt1000` … `hgt200`, 16–23 `tmp1000` … `tmp200`,
24–31 `rh1000` … `rh200`, 32–39 `spfh1000` … `spfh200`,
40–47 `ugrd1000` … `ugrd200`, 48–55 `vgrd1000` … `vgrd200`,
56–63 `uqflx1000` … `uqflx200`, 64–71 `vqflx1000` … `vqflx200` (each
isobaric family in the level order 1000, 925, 850, 700, 500, 300, 250, 200).
No decoder required those numbers, so every such file stays valid.

### Payload Rules (v1)

- Every non-ZERO entry maps to one independent Zstandard frame.
- ZSTD_DICT frames decompress with the embedded dictionary, which the
  decoder loads once at open time. A ZSTD_DICT entry is invalid when
  `dictionaryLength` is 0. No production build embeds a dictionary: trained
  dictionaries showed no gain on full ~1 MB planes. The section and mode
  are reserved for smaller payloads.
- The decompressed payload length must equal `decodedLength`.
- RAW decompresses directly to the quantized plane.
- ANCHOR decompresses to a one-byte residual, then adds the dependency
  plane.
- PREVIOUS decompresses to a one-byte residual, then adds the plane of the
  preceding frame on the time axis: `frameOffsets[i - 1]`, or
  `frameOffset - frameStep` on a uniform axis (not `frameOffset - 1`, which
  does not exist on an axis whose step is not one unit). A PREVIOUS entry
  on the first frame of the axis is invalid, and every PREVIOUS entry must
  carry exactly that preceding offset in `dependencyOffset`: like ANCHOR,
  the dependency is explicit in the index, never the sentinel, never
  derived, so the same file has only one encoding. No reference bundle uses
  PREVIOUS in v1; the reference encoder emits only RAW and ANCHOR there.
- The decoder must reject a frame when length, checksum, or dependency
  validation fails.
- The decoder must not allocate an output larger than `width × height` or a
  configured safety limit based on untrusted file values.

## Quantization Codebooks

The codebooks of [`bundle.md`](bundle.md), "Quantization Codebooks".

## Temporal Prediction

The predictors are shared by both container versions. Which of them a file may use, and what a residual is
computed against, is defined by the index: v1 allows all four per plane
(*[PlaneEntry (v1)](#planeentry-v1)*), v2 allows RAW and PREVIOUS per
variable and chains inside a chunk (*[Container v2](#container-v2)*).

The residual arithmetic is [`bundle.md`](bundle.md), "Residuals".

Per-variable rules in v1:

- Linear-codebook fields (`tmp2m`, `ugrd10m`, `vgrd10m`, `dswrf`, the
  surface diagnostics, the ocean fields, the isobaric fills and the pressure
  family `prmsl` / `hgt<level>`) use temporal prediction. Each segment of
  the time axis splits independently into groups of 6 frames, so a group
  never spans a change of step. Within each group of `n` frames, the frame
  at zero-based index `floor(n / 2)` is the anchor: it uses predictor RAW,
  and every other frame in the group uses ANCHOR residuals against it. A
  trailing single-frame group is its own anchor and uses RAW. Random access
  to any frame costs at most two plane decodes (anchor + target). `groupId`
  counts groups sequentially across the whole variable and does not restart
  at a segment boundary.
- Precipitation (`prate`) uses independent RAW planes for every frame, with
  `groupId` mirroring `frameOffset`. Precipitation regions move with
  weather systems; fixed-grid differencing creates both entering and
  leaving edges and measurably increases compressed size.

Grouping per segment rather than across the whole timeline keeps every
ANCHOR residual a difference between frames one step apart. A group
straddling the GFS 120-hour transition would difference frames three hours
apart against an anchor chosen among frames one hour apart: still lossless,
since residuals wrap modulo 256, but a larger residual and a
worse-compressing one. Splitting costs at most one extra RAW anchor per
segment boundary per variable.

Segment-aligned grouping is an encoder rule, not a decode-time invariant. A
decoder validates the dependency structure recorded in the index (see Error
Handling) and never needs to derive segments; a group that straddled a
boundary would still reconstruct exactly and is not rejected.

Every plane is complete; see [`bundle.md`](bundle.md), "Complete planes".

## Physical Payload Order (v1)

Payloads are ordered by variable and temporal group, with each group's
anchor payload first inside its group:

```text
temperature group 0 anchor (f003)
temperature group 0 residual f000
temperature group 0 residual f001
temperature group 0 residual f002
temperature group 0 residual f004
temperature group 0 residual f005
temperature group 1 anchor
...
precipitation f000 RAW
precipitation f001 RAW
...
```

The anchor's actual frame offset remains in the index; sequential playback
does not depend on physical payload order. Keeping a temporal group's
payloads contiguous means one HTTP range request fetches one decodable
group.

In the two-variable wind bundle the payloads interleave per temporal group
(a `ugrd10m` group followed by the same `vgrd10m` group), so streaming one
wind frame touches two adjacent byte spans.

Interleaving all forecast times per grid point would turn single-plane reads
into wide file gathers, so Xue v1 keeps complete planes contiguous; temporal
continuity is represented by residuals instead.

## Streaming

This section describes v1; *[Streaming (v2)](#streaming-v2)* covers the
tiled container, which streams the same way with narrower spans.

The format needs no side files to stream. The structural prefix
`[0, dataOffset)` (header, metadata, index, optional dictionary; a few KB in
production) validates like a whole file minus the byte-content checks past
the prefix. An explicit offsets array costs roughly five bytes per frame,
under 1 KB for a 240-hour axis, so the prefix stays small enough to fetch in
one range request. A streaming reader then range-fetches payload spans on
demand; the per-group contiguity above means the bytes for one frame form
one or two contiguous spans. Integrity comes from the per-plane CRC-32 and
the Zstandard frame checksums, so a whole-file checksum is only meaningful
for full downloads.

## Error Handling

These rules apply to both versions except where they name a v1 structure;
*[Error handling, additions for v2](#error-handling-additions-for-v2)* adds
the tiled index's own.

A decoder must reject:

- Unknown container versions, and metadata `schemaVersion` values the
  decoder does not implement.
- Nonzero reserved fields in a known version.
- Unknown predictor, compression, or flags values, and a `variableId` that
  is not one of the file's own metadata `variables` ("unknown" here means
  absent from this file, not absent from any registry; there is none).
- ZSTD_DICT entries when `dictionaryLength` is 0.
- Entries overlapping the header, metadata, index, or dictionary.
- Overlapping payload ranges (ZERO entries may have zero length).
- Unindexed gaps between sections or payloads, and nonzero padding bytes.
- Duplicate `(variableId, frameOffset)` pairs.
- A `time` block declaring both `frameStep` and `frameOffsets`, or neither;
  a `frameOffsets` array that is not strictly increasing, whose length
  differs from `frameCount`, whose first element differs from
  `firstFrameOffset`, whose steps are all equal, or that contains an offset
  above 65534; a uniform axis whose last offset
  `firstFrameOffset + (frameCount − 1) × frameStep` exceeds 65534; a
  `unitSeconds` that is not a whole divisor of 3600 or is finer than the
  offsets need; a field the time block does not define; a version 1 or 2
  block carrying a version 3 field, or the reverse. The same rules apply to
  a version 1 or 2 axis under its own field names.
- A `parameter` block in a file below schemaVersion 3, or a variable
  without one in a schemaVersion 3 file; a parameter code outside 0–255; a
  fixed surface with exactly one of its scale factor and scaled value
  `null`; a key the block does not define.
- A `band`, `producer` or `aerosol` block in a file below schemaVersion 3;
  a block that is not an object (`null` included); a `band` block missing
  one of its five fields, carrying a value outside its range, or carrying a
  key it does not define; a `producer` block missing `id` or `version`,
  whose `id` does not match `^[a-z][a-z0-9]*$`, whose `version` is not a
  non-empty string, or carrying a key it does not define; an `aerosol`
  block missing one of its eleven fields or carrying a key it does not
  define, a type outside its range, a limit with exactly one of its scale
  factor and scaled value `null`, a `null` limit under an interval type
  other than 255, or a non-`null` limit under type 255.
- An `ensemble` block in a file below schemaVersion 3; one that is not an
  object (`null` included); one whose keys are not exactly one of the three
  shapes (`derivedForecast` alone, `percentileValue` alone, or
  `probabilityType` with its four limit fields); a value outside its range;
  a limit with exactly one of its scale factor and scaled value `null`; a
  limit that is `null` where its probability type uses it, or not `null`
  where the type does not.
- A `grid.projection` that is not an object (`null` included), whose `kind`
  is not one the decoder implements, missing one of its ten fields, carrying a
  key it does not define, with a field of the wrong type or not finite, a
  `radius` that is not positive, a standard parallel or `latitudeOfOrigin` at
  or past a pole, standard parallels that sum to zero, a `centralMeridian`
  outside ±180, a `dx` that is not positive or a `dy` that is not negative. A
  grid carrying `projection` and any of the four regular fields or `gaussian`,
  even `null`, or whose `wrapLongitude` is anything but `false`.
- A `grid.gaussian` that is not an object (`null` included), in a file of any
  version but 3, missing one of its three fields or carrying a key it does not
  define, with an `n` outside 1–32768, a negative `firstRow`, a `rowStride`
  below 1, or a last row `firstRow + (height − 1) × rowStride` at or past `2n`.
- A declared `schemaVersion` other than the lowest able to express the
  metadata: a schemaVersion 2 file that declares `stepHours`, a schemaVersion
  1 or 2 file whose variables carry `parameter` or whose time block names a
  unit, a schemaVersion 4 file without `grid.projection`, or a file below
  schemaVersion 4 with one.
- An incomplete frame sequence for any declared variable. The expected
  sequence is `frameOffsets` when present, and
  `firstFrameOffset + i * frameStep` otherwise; a decoder must never
  reconstruct it arithmetically when `frameOffsets` is present.
- ANCHOR or PREVIOUS dependencies that leave the variable or temporal
  group, cyclic dependencies, or chains deeper than the group length; a
  PREVIOUS entry whose `dependencyOffset` is not exactly the preceding frame
  on the axis (the 65535 sentinel included).
- Any integer computation that would overflow (use checked arithmetic).
- A plane whose CRC-32 or Zstandard checksum fails.

## Container v2

Container v2 changes what a payload is: a chunk, one spatial tile of one
temporal group for one variable, instead of one whole plane of one frame. A
file holds the same set of quantized codes as its v1 counterpart, cut into
tiles and packed group by group, so a reader can fetch the region it is
looking at, and read one cell's series by fetching one chunk per group. An
encoder writes one version or the other for a whole file; there is no
mixing.

The fixed header, the metadata JSON (schema versions 1–3, variable identity,
the time axis, regional grids), the quantization codebooks and the
modulo-256 residual arithmetic are shared with v1 and are not repeated here.

```text
+------------------------------+ 0
| FixedHeader, 80 bytes        |  version = 2
+------------------------------+ metadataOffset = 80
| UTF-8 metadata JSON          |  unchanged, schemaVersion 3
+------------------------------+ indexOffset
| IndexHeader, 32 bytes        |  magic IDX2
| VariableEntry[variableCount] |  4 bytes each
| GroupEntry[groupCount]       |  4 bytes each
| ChunkEntry[chunkCount]       |  8 bytes each
+------------------------------+ dictionaryOffset, optional
| Zstandard dictionary         |
+------------------------------+ dataOffset
| chunk payloads, contiguous   |
| zero padding for alignment   |
+------------------------------+ fileSize
```

Tiling is a storage property, not a dataset property: the grid, the time
axis, the variable identities and the codebooks are unchanged, so the
metadata JSON is byte-for-byte what a v1 file of the same data would carry
and the tiling appears only in the binary index.

### FixedHeader (v2)

Identical to v1 in layout and rules, with `version = 2`. `flags` stays 0.
Section adjacency, alignment and zero padding are validated exactly as in
v1; `indexLength` covers the IndexHeader and all three tables.

### Tiles

The grid declared in the metadata is cut into tiles of
`tileWidth × tileHeight` cells, starting at the grid's first cell (the
north-west corner in the published layout) and laid out row-major:

```text
tileColumns = ceil(width  / tileWidth)
tileRows    = ceil(height / tileHeight)
tileCount   = tileColumns × tileRows
tile t covers columns [ (t mod tileColumns) × tileWidth,  +tileWidth  )
          and rows    [ (t div tileColumns) × tileHeight, +tileHeight )
```

Tiles in the last column and the last row are clipped to the grid; the width
of a clipped tile is `width − (tileColumns − 1) × tileWidth`, and likewise
for its height. A file must declare `1 ≤ tileWidth ≤ width` and
`1 ≤ tileHeight ≤ height`, so a file with one tile declares its grid size
exactly. `tileColumns`, `tileRows` and `tileCount` are derived from the
metadata grid and never stored, so the geometry has one encoding.

Tiles are cells, never degrees: the geographic footprint of a tile follows
from the `grid` block, and the horizontal wrap of a global grid is a
property of the grid, not of any tile; the last tile column does not wrap
into the first.

### Temporal groups

A group is a run of consecutive frames on the time axis. The groups of a
file partition the axis in order: the first group starts at frame index 0,
each group starts where the previous one ends, and the last group ends at
`frameCount`. Every variable in the file uses the same groups (a file has
one axis). A group's frames are stored inside every chunk of that group in
axis order.

Grouping is chosen by the encoder (see *[Encoder rules](#encoder-rules-v2)*);
a decoder validates the partition and nothing else about it.

### IndexHeader (v2)

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 4 | bytes | magic | ASCII `IDX2` |
| 4 | 2 | u16 | version | 2 |
| 6 | 2 | u16 | headerSize | 32 |
| 8 | 2 | u16 | tileWidth | `1 ≤ tileWidth ≤ grid.width` |
| 10 | 2 | u16 | tileHeight | `1 ≤ tileHeight ≤ grid.height` |
| 12 | 2 | u16 | groupCount | ≥ 1 |
| 14 | 1 | u8 | variableCount | ≥ 1, equals the number of metadata variables |
| 15 | 1 | u8 | compression | 1 ZSTD or 2 ZSTD_DICT, for every chunk |
| 16 | 4 | u32 | chunkCount | Must equal `groupCount × tileCount × variableCount` |
| 20 | 4 | u32 | reserved | 0 |
| 24 | 8 | u64 | reserved | 0 |

### VariableEntry

One per variable, sorted by ascending `variableId`; the set must equal the
metadata's `variables`.

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 1 | u8 | variableId | A handle into this file's metadata (unchanged from v1) |
| 1 | 1 | u8 | predictor | 0 RAW or 2 PREVIOUS |
| 2 | 2 | u16 | reserved | 0 |

The predictor applies to every chunk of the variable. ANCHOR (1) and ZERO
(3) are not valid in v2: a chunk is decompressed whole, so an anchor adds
no random access and would only give one chunk a second valid encoding.

### GroupEntry

One per group, in axis order.

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 2 | u16 | firstFrame | Index into the axis (not a frame offset); 0 for the first group, the previous group's `firstFrame + frameCount` otherwise |
| 2 | 1 | u8 | frameCount | 1–255; the last group must end exactly at the axis's `frameCount` |
| 3 | 1 | u8 | reserved | 0 |

### ChunkEntry and chunk order

One per chunk, in physical order:

```text
for group g in 0 .. groupCount
  for tile t in 0 .. tileCount           (row-major)
    for variable v in 0 .. variableCount (ascending variableId)
      chunk position p = (g × tileCount + t) × variableCount + v
```

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 4 | u32 | compressedLength | ≥ 1 |
| 4 | 4 | u32 | crc32 | CRC-32/IEEE of the reconstructed chunk |

A chunk's offset is not stored. Payloads are contiguous in position order
starting at `dataOffset`: the offset of chunk 0 is `dataOffset` and the
offset of chunk `p + 1` is the offset of chunk `p` plus its
`compressedLength`. `fileSize = align8(end of the last chunk)`, and every
padding byte is zero. All of this is checked arithmetic against `fileSize`
before any allocation. Since the order is fixed by this specification and
the chunks are strictly adjacent, a stored offset would be redundant and
would more than double the index.

This order serves the three access patterns: a whole group is one
contiguous span (a global view fetches what v1 fetched); the tiles a
viewport needs form one contiguous span per tile row per group; and one
cell's series is one chunk per group, with the two components of a wind
bundle adjacent.

### Chunk contents

Every chunk is one independent Zstandard frame that must carry a content
checksum, decompressing to exactly

```text
decodedLength = group.frameCount × tile.height × tile.width
```

bytes (the tile's clipped height and width), laid out frame-major: the
group's frames in axis order, each as the tile's rows top to bottom, each
row west to east, the same orientation as the plane. With ZSTD_DICT the
embedded dictionary applies, and a ZSTD_DICT file with `dictionaryLength = 0`
is invalid.

The reconstructed chunk is derived from the decompressed bytes by the
variable's predictor:

- RAW: the bytes are the codes.
- PREVIOUS: the first frame's bytes are its codes; every later frame's
  bytes are the modulo-256 residual against the reconstructed previous
  frame of the same chunk, which is the preceding frame on the axis because
  groups are contiguous. `residual = (current − previous) mod 256`,
  `current = (residual + previous) mod 256`.

`crc32` covers the reconstructed chunk in the same frame-major layout. A
decoder must reject a chunk whose Zstandard checksum, decompressed length or
CRC-32 fails, and must not allocate more than `decodedLength` (or a
configured safety limit) from file values.

### Decoding a frame and a series

A frame of a variable is assembled from the chunks of its group for the
tiles wanted: each chunk contributes the frame's `tile.height × tile.width`
block at the tile's grid position. A reader may assemble a partial plane
from a subset of tiles; which tiles it holds is then part of the frame's
state, and a renderer must not draw cells no tile has covered.

One cell's series is the cell's byte from the frame block of the tile
containing it, in every group's chunk of that tile. The cost of a series is
one chunk per group of one tile, independent of the number of frames.

### Streaming (v2)

The structural prefix `[0, dataOffset)` validates as in v1. A streaming
reader derives every chunk's byte span from the index and range-fetches
spans on demand; the spans a viewport needs are contiguous per tile row and
may be coalesced across small gaps at the reader's discretion. Integrity is
per chunk (Zstandard checksum plus CRC-32).

The index is larger than v1's: the chunk count is the group count times the
tile count times the variable count, at 8 bytes each (94 KB for a 420-tile,
28-group GFS bundle). That is one extra range request before the first
frame.

### Error handling, additions for v2

A decoder must reject, in addition to the v1 rules that still apply (unknown
container versions, section geometry, padding, dictionary presence,
overflow):

- An IndexHeader whose magic, version or headerSize differ, or whose
  reserved words are nonzero.
- `tileWidth` or `tileHeight` of zero or larger than the grid.
- A `variableCount` or variable set that differs from the metadata's own
  (every `variableId` here must be a `variableId` there, and the two sets
  must be equal); unsorted or duplicate `variableId`s; a predictor other
  than RAW or PREVIOUS; nonzero reserved fields.
- Groups that do not partition the axis: a first group not starting at 0, a
  gap or overlap between consecutive groups, a `frameCount` of 0, or a last
  group not ending at the axis's `frameCount`.
- A `chunkCount` other than `groupCount × tileCount × variableCount`, or an
  `indexLength` other than the header plus the three tables.
- A `compressedLength` of 0, or chunk spans that exceed `fileSize` or leave
  `fileSize ≠ align8(end of the last chunk)`.
- A Zstandard frame without a content checksum, or one whose decompressed
  length differs from `decodedLength`.
- A reconstructed chunk whose CRC-32 fails.

### Encoder rules (v2)

Normative for byte identity between the two encoders:

- Every variable's grouping is `GROUP_LENGTH = 6` frames, formed inside
  segments of constant step exactly as v1's temporal grouping, applied once
  per file to the shared axis. A trailing short group is its own group.
- Predictor: RAW for `prate`, `cref` and the categorical `ptype`; PREVIOUS
  for every linear-codebook field.
- Tile size is a per-source registry value; a reduced-resolution variant
  decimated by `f` (2, 4 or 8: every `f`-th row and column of the canonical
  plane, from row and column 0; the 3.9 µm window's sites carry the largest
  code within `f / 2` cells, [`satellite.md`](satellite.md)) uses `(ceil(tileWidth / f), ceil(tileHeight
  / f))`, so a tile with the same number covers the same ground in every
  tier. A regional (cropped) file tiles its own grid from its own origin
  with the source's tile size.
- The tile is then clamped to the grid: `min(tileWidth, width)` and
  `min(tileHeight, height)`. The format requires `1 <= tile <= grid` so that
  a single-tile file states its grid size exactly, and a regional crop is
  routinely smaller than its source's tile (a six-degree showcase window is
  24 x 24 cells against the 0.25-degree grid's 48 x 52). Clamping makes such
  a file one tile.
- Compression is ZSTD at the production level with the content checksum; no
  dictionary.

## File Naming

A container is named after its bundle ([`bundle.md`](bundle.md), "Bundle
naming") with `.xue`: `tmp2m.xue`, `wind10m.xue`, `tmp2m.half.xue`. A run's
`manifest.json` names each bundle's container (path, byte length and
whole-file CRC-32) where one was written, beside or instead of its store;
the manifest and the `latest.json` pointer are delivery concerns defined by
the reference implementations (`xuebuild/manifest.py`,
`web/src/manifest.ts`), not by this container spec.

### Retired artifacts readers still accept

The H.264 companion is retired: no encoder writes it and the viewer no
longer decodes it. A run or case published with it carries, beside a
scalar bundle, `<variable>.h264` (a raw Annex B stream), its per-frame
index `<variable>.h264.index.json` and a debug playlist
`<variable>.h264.m3u8`, and its manifest entry carries a `video` block
(`streamPath`, `indexPath`, `byteLength`, `crc32`, `codec`, `width`,
`height`, `gop`, `frameCount`, `metadataJson`). Every manifest validator
admits an entry with that block and leaves it unread, whatever its shape;
the STAC Item of such a run still lists the two files as assets
([stac.md](stac.md)).

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
