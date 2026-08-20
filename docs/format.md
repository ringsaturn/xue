# Xue v1 Binary Format Specification

This document is the normative specification of the Xue v1 container
(`.xue`), a per-variable spatiotemporal binary format for packing a full
forecast run of one gridded weather field into a single indexed file that a
browser can decode frame by frame.

The Python encoder (`xue/binformat.py`, `xue/quantize.py`,
`xue/temporal.py`) and the Rust decoder (`rust/xue`) are the two reference
implementations, held byte-identical by cross-language golden tests.

## Design Summary

The format targets a product that animates a complete global forecast
(hourly or 3-hourly steps out to 120 hours) with continuous playback and
fast timeline scrubbing. Per-frame tile pyramids are larger under that
access pattern: for one GFS run, per-frame raster PMTiles for two
variables measured 3,205.87 MB, against 137.73 MB of source GRIB. Xue
instead stores each variable as quantized single-byte planes on the native
grid — projection and coloring happen later, on the GPU — with bounded
temporal prediction and per-plane Zstandard compression. The same two
variables fit in roughly 65–71 MB per run depending on profile, every frame is
individually addressable, and the index makes HTTP-range streaming
possible: a client can fetch only the structural prefix (a few KB) and then
range-request one temporal group at a time.

Key decisions:

- **Quantized uint8 planes.** Each decoded frame is a contiguous
  single-channel plane that uploads directly to a WebGL2 `R8` texture.
  Codebooks are visualization-oriented (0.25 °C temperature error budget, a
  logarithmic precipitation codebook that preserves light-rain resolution).
- **Bounded temporal groups.** Smooth fields (temperature, wind, solar
  radiation) use six-frame groups with a middle RAW anchor and one-byte
  residuals, capping random access at two plane decodes. Precipitation
  fields move with weather systems, and temporal differencing increases
  their compressed size; every precipitation plane is independent RAW.
- **One independent Zstandard frame per plane** for direct indexing,
  sequential reads, and localized failure isolation.
- **Strict parsing.** All offsets and lengths are validated with checked
  arithmetic before any allocation; sections must be strictly adjacent with
  zero padding; every reconstructed plane carries a CRC-32.

## Conventions

- All binary integers are little-endian.
- All offsets are absolute (relative to the beginning of the file).
- Structural sections are aligned to 8 bytes; `align8(x)` rounds `x` up to
  the next multiple of 8. Padding bytes must be zero.
- Metadata is UTF-8 JSON; the index uses fixed-width binary entries.
- All quantization rounding is round-half-up: `round(x) = floor(x + 0.5)`.
  Every rounded quantity in this format is non-negative, so this equals
  round-half-away-from-zero. Implementations must not use round-half-even
  (the behavior of Python's built-in `round` and the IEEE 754 default); a
  mismatch changes codes for values landing exactly on a half step.

## Container Layout

```text
+------------------------------+ 0
| FixedHeader, 80 bytes        |
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

Example (a single-variable GFS temperature bundle):

```json
{
  "schemaVersion": 1,
  "model": "GFS",
  "product": "pgrb2.0p25",
  "runTime": "2026-08-15T06:00:00Z",
  "time": {
    "firstForecastHour": 0,
    "stepHours": 1,
    "frameCount": 121
  },
  "grid": {
    "width": 1440,
    "height": 721,
    "layout": "row-major",
    "rowOrder": "north-to-south",
    "columnOrder": "west-to-east",
    "firstLongitude": -180.0,
    "firstLatitude": 90.0,
    "longitudeStep": 0.25,
    "latitudeStep": -0.25,
    "wrapLongitude": true
  },
  "variables": [
    {
      "numericId": 1,
      "id": "tmp2m",
      "label": "2 meter temperature",
      "unit": "°C",
      "quantization": {
        "type": "linear",
        "offset": -60.0,
        "scale": 0.5,
        "minimumCode": 0,
        "maximumCode": 220,
        "nodataCode": 255
      }
    }
  ]
}
```

A bundle may declare more than one variable; the two 10 m wind components
ship together in one two-variable `wind10m.xue` bundle. Every variable's
quantization block must be one of the codebook types below.

The encoder rotates longitude columns so the first column is `-180`,
preserving north-to-south row order. Grids that natively start at
Greenwich (the GFS surface-flux Gaussian grid) are rolled by the encoder
into the same `-180`-first layout, so every published grid shares it. A
renderer applies inverse Web Mercator, converts longitude and latitude to
grid coordinates, and samples this layout directly.

`model` and `product` identify the source dataset. Registered pairs:

| `model` | `product` | Grid | Step | Notes |
|---|---|---|---|---|
| `GFS` | `pgrb2.0p25` | 1440 × 721, 0.25° | 1 h | All series include the analysis frame (f000) |
| `ECMWF` | `ifs-0p25` | 1440 × 721, 0.25° | 3 h | `prate` is de-accumulated from the run-total `tp`, so its series has no analysis frame and starts at `firstForecastHour: 3` |
| `GFS-SFLUX` | `sfluxgrb` | 3072 × 1536 Gaussian, ~13 km | 1 h | `prate` is de-averaged from window-cumulative records and starts at `firstForecastHour: 1`; the only source shipping `dswrf` |

Time axes may therefore differ between bundles of one run. The container
layout is identical for every model — only the metadata identity, the grid,
and the time axis differ. Readers must derive the frame list from `time`
and the grid from `grid`, never from the model name.

### IndexHeader

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 4 | bytes | magic | ASCII `IDX1` |
| 4 | 2 | u16 | entrySize | Fixed at 40 for v1 |
| 6 | 2 | u16 | version | Fixed at 1 for v1 |
| 8 | 4 | u32 | entryCount | Must equal (variables × frames per variable) declared in metadata |
| 12 | 4 | u32 | reserved | Must be 0 |

### PlaneEntry

Index entries are sorted by `(variableId, forecastHour)`. Each entry is 40
bytes:

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 1 | u8 | variableId | Registered ids below |
| 1 | 1 | u8 | predictor | See enum below |
| 2 | 1 | u8 | compression | 0 NONE, 1 ZSTD, 2 ZSTD_DICT |
| 3 | 1 | u8 | flags | See flags below |
| 4 | 2 | u16 | forecastHour | Forecast hour of this plane |
| 6 | 2 | u16 | dependencyHour | 65535 when no dependency exists |
| 8 | 2 | u16 | groupId | Temporal group ID; equals forecastHour for RAW-only series |
| 10 | 2 | u16 | reserved0 | Must be 0 |
| 12 | 4 | u32 | compressedLength | Payload byte count |
| 16 | 8 | u64 | dataOffset | Absolute payload offset |
| 24 | 4 | u32 | decodedLength | Must equal `grid.width × grid.height` |
| 28 | 4 | u32 | crc32 | CRC-32 of the reconstructed quantized plane |
| 32 | 1 | u8 | minimumCode | Actual minimum code in the final plane |
| 33 | 1 | u8 | maximumCode | Actual maximum code in the final plane |
| 34 | 6 | bytes | reserved1 | Every byte must be 0 |

Registered `variableId` values:

| Value | Variable | Field |
|---:|---|---|
| 1 | `tmp2m` | 2 m temperature |
| 2 | `prate` | Surface precipitation rate |
| 3 | `ugrd10m` | 10 m wind, U component |
| 4 | `vgrd10m` | 10 m wind, V component |
| 5 | `dswrf` | Surface downward shortwave radiation flux, instantaneous |

Predictor enum:

| Value | Name | Meaning |
|---:|---|---|
| 0 | RAW | Decompressed payload is the complete quantized plane |
| 1 | ANCHOR | Payload is a residual relative to dependencyHour |
| 2 | PREVIOUS | Payload is a residual relative to the previous forecast time |
| 3 | ZERO | No payload, output a zero-filled plane, reserved for local block formats |

Flags:

- Bit 0: Zstandard frame includes a checksum.
- All other bits must be 0.

Residuals always use modulo-256 wrapping semantics, so no flag describes
residual interpretation. Overflow presence is derivable from `maximumCode`,
so no flag duplicates it.

The `crc32` field uses CRC-32/IEEE — polynomial `0x04C11DB7`, reflected
implementation `0xEDB88320`, initial value and final XOR `0xFFFFFFFF`,
identical to zlib's `crc32` — computed over the reconstructed quantized
plane. The Zstandard checksum validates the compressed payload, while CRC32
validates predictor reconstruction.

### Payload Rules

- Every non-ZERO entry maps to one independent Zstandard frame.
- ZSTD_DICT frames decompress with the embedded dictionary, which the
  decoder loads once at open time. A ZSTD_DICT entry is invalid when
  `dictionaryLength` is 0. (No production build currently embeds a
  dictionary: trained dictionaries showed no gain on full ~1 MB planes.
  The section and mode are reserved for smaller payloads, e.g. after
  tiling.)
- The decompressed payload length must equal `decodedLength`.
- RAW decompresses directly to the quantized plane.
- ANCHOR decompresses to a one-byte residual, then adds the dependency
  plane.
- PREVIOUS decompresses to a one-byte residual, then adds the previous
  plane.
- The decoder must reject a frame when length, checksum, or dependency
  validation fails.
- The decoder must not allocate an output larger than `width × height` or
  a configured safety limit based on untrusted file values.

## Quantization Codebooks

### Linear (`"type": "linear"`)

```text
q = round((clamp(x, offset, offset + scale * maximumCode) - offset) / scale)
x = offset + q * scale
```

Codes `0` through `maximumCode` are valid; `nodataCode` marks missing data;
codes between `maximumCode + 1` and `nodataCode - 1` are reserved. The
maximum in-range quantization error is `scale / 2`.

Registered linear codebooks (the `quality` profile; `balanced` uses the
same values unless noted):

| Variable | offset | scale | maximumCode | nodataCode | Error budget |
|---|---:|---:|---:|---:|---|
| `tmp2m` | −60 °C | 0.5 | 220 | 255 | 0.25 °C |
| `ugrd10m` / `vgrd10m` | −63.5 m/s | 0.5 | 254 | 255 | 0.25 m/s |
| `dswrf` | 0 W/m² | 5 | 254 | 255 | 2.5 W/m² |

The `compact` profile doubles each `scale` (temperature 1.0 → maximumCode
110, wind 1.0 → 127, dswrf 10 → 127).

Values outside the range clamp to the range ends before quantization.

### Logarithmic (`"type": "log1p"`, precipitation)

Precipitation is sparse and long-tailed, so it uses a logarithmic codebook
with parameters `trace`, `scale`, `maximum`:

- Code `zeroCode` (0): dry, below `trace`.
- Codes `minimumCode` (1) through `maximumCode`: positive logarithmic
  values.
- Code `overflowCode` (`maximumCode + 1`): above `maximum`.
- Code `nodataCode`: missing data.

For `trace <= p <= maximum`, with `span = maximumCode - 1`:

```text
lo = ln(1 + trace / scale)
hi = ln(1 + maximum / scale)
u = (ln(1 + p / scale) - lo) / (hi - lo)
q = 1 + round(span * u)
```

Decode with:

```text
u = (q - 1) / span
p = scale * (exp(lo + u * (hi - lo)) - 1)
```

Code `maximumCode` decodes to exactly `maximum`. The overflow code decodes
one codebook step above the maximum — the same formula evaluated at
`q = maximumCode + 1` — extending the logarithmic grid by one step so
decoded values remain strictly increasing across all non-nodata codes, and
overflow pixels stay distinguishable from the top in-range code in both a
palette and scalar inspection.

Registered precipitation codebooks (`trace = 0.01 mm/h`,
`scale = 0.05 mm/h`, `maximum = 128 mm/h` in both):

| Profile | maximumCode | overflowCode | nodataCode |
|---|---:|---:|---:|
| `quality` (256-level) | 253 | 254 | 255 |
| `balanced` / `compact` (128-level) | 125 | 126 | 127 |

The 128-level codebook still uses full R8 bytes; the smaller symbol
alphabet improves entropy coding, and the GPU needs no bit unpacking.

## Temporal Prediction

Residuals are defined unconditionally as one-byte modulo-256 wrapping
differences:

```text
residual = (current - base) mod 256
current  = (residual + base) mod 256
```

Wrapping subtraction and addition are lossless for every byte pair, so
there is no residual range check, no signed interpretation, and no RAW
fallback for out-of-range differences.

Per-variable rules in v1:

- **Linear-codebook fields (`tmp2m`, `ugrd10m`, `vgrd10m`, `dswrf`)** are
  smooth enough for temporal prediction. The timeline splits into groups of
  6 frames. Within each group of `n` frames, the frame at zero-based index
  `floor(n / 2)` is the anchor: it uses predictor RAW, and every other
  frame in the group uses ANCHOR residuals against it. A trailing
  single-frame group is its own anchor and uses RAW. Random access to any
  frame therefore costs at most two plane decodes (anchor + target).
- **Precipitation (`prate`)** uses independent RAW planes for every
  forecast time, with `groupId` mirroring `forecastHour`. Precipitation
  regions move with weather systems; fixed-grid differencing creates both
  entering and leaving edges and measurably increases compressed size.

The current global grids have no bitmap and all points are valid. Xue v1
requires complete input planes; a future missing-data implementation should
add a separate bitmap so residual bytes never conflict with the nodata
code.

## Physical Payload Order

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

The anchor's actual forecast hour remains in the index; sequential playback
does not depend on physical payload order. Keeping a temporal group's
payloads contiguous means one HTTP range request fetches one decodable
group.

In the two-variable wind bundle the payloads interleave per temporal group
— a `ugrd10m` group followed by the same `vgrd10m` group — so streaming one
wind frame touches two adjacent byte spans.

Interleaving all forecast times per grid point would turn single-plane
reads into wide file gathers, so Xue v1 keeps complete planes contiguous;
temporal continuity is represented by residuals instead.

## Streaming

The format needs no side files to stream. The structural prefix
`[0, dataOffset)` — header, metadata, index, optional dictionary, a few KB
in production — validates exactly like a whole file minus the byte-content
checks past the prefix. A streaming reader then range-fetches payload spans
on demand; the per-group contiguity above means the bytes for one frame form
one or two contiguous spans. Integrity comes from the per-plane CRC-32 and
the Zstandard frame checksums, so a whole-file checksum is only meaningful
for full downloads.

## Error Handling

A decoder must reject:

- Unknown major versions.
- Nonzero reserved fields in a known version.
- Unknown predictor, compression, variableId, or flags values.
- ZSTD_DICT entries when `dictionaryLength` is 0.
- Entries overlapping the header, metadata, index, or dictionary.
- Overlapping payload ranges (ZERO entries may have zero length).
- Unindexed gaps between sections or payloads, and nonzero padding bytes.
- Duplicate `(variableId, forecastHour)` pairs.
- An incomplete forecast-hour sequence for any declared variable (the
  expected sequence derives from the metadata `time` block).
- ANCHOR or PREVIOUS dependencies that leave the variable or temporal
  group, cyclic dependencies, or chains deeper than the group length.
- Any integer computation that would overflow (use checked arithmetic).
- A plane whose CRC-32 or Zstandard checksum fails.

## File Naming

These conventions sit outside the container but are what the reference
pipeline produces: one file per variable per run (`tmp2m.xue`,
`prate.xue`, `dswrf.xue`), the two-variable `wind10m.xue`, and
half-resolution renditions named `<variable>.half.xue` — structurally
identical bundles whose metadata declares the decimated grid. A run
directory also carries a `manifest.json` describing every bundle (path,
byte length, whole-file CRC-32, resolution variants, optional poster and
H.264 companion artifacts); the manifest and a tiny mutable `latest.json`
pointer are delivery concerns defined by the reference implementations
(`xue/manifest.py`, `web/src/manifest.ts`), not by this container spec.
