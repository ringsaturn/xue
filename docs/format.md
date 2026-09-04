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
(steps of one to six hours out to 240 hours, changing step partway through
the range) with continuous playback and fast timeline scrubbing. Per-frame
tile pyramids are larger under that access pattern: for one 121-frame GFS
run, per-frame raster PMTiles for two variables measured 3,205.87 MB,
against 137.73 MB of source GRIB. Xue instead stores each variable as
quantized single-byte planes on the native grid — projection and coloring
happen later, on the GPU — with bounded temporal prediction and per-plane
Zstandard compression. The same two variables fit in roughly 65–71 MB for
that run depending on profile, and grow roughly linearly with frame count
(coarser-step frames span larger inter-frame differences, so their
residuals compress slightly worse). Every
frame is individually addressable, and the index makes HTTP-range streaming
possible: a client can fetch only the structural prefix (a few KB) and then
range-request one temporal group at a time.

Key decisions:

- **Quantized uint8 planes.** Each decoded frame is a contiguous
  single-channel plane that uploads directly to a WebGL2 `R8` texture.
  Codebooks are visualization-oriented (0.25 °C temperature error budget, a
  logarithmic precipitation codebook that preserves light-rain resolution).
- **An axis that names its own unit and may change step.** No source
  publishes one cadence all the way to 240 hours: GFS is hourly to f120 and
  three-hourly beyond, ECMWF three-hourly to 144 hours and six-hourly
  beyond. And not every dataset is hourly at all — the radar mosaic
  publishes every six minutes. The axis therefore declares the seconds one
  step unit is worth and either a uniform step or its offsets outright, so
  it is always exact rather than approximated.
- **Bounded temporal groups.** Smooth fields (temperature, wind, solar
  radiation) use six-frame groups with a middle RAW anchor and one-byte
  residuals, capping random access at two plane decodes. Groups are formed
  inside a segment of constant step, so no group straddles a change of
  cadence. Precipitation fields move with weather systems, and temporal
  differencing increases their compressed size; every precipitation plane
  is independent RAW.
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
  "schemaVersion": 3,
  "model": "GFS",
  "product": "pgrb2.0p25",
  "runTime": "2026-08-15T06:00:00Z",
  "time": {
    "unitSeconds": 3600,
    "firstFrameOffset": 0,
    "frameStep": 1,
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
      "parameter": {
        "discipline": 0,
        "parameterCategory": 0,
        "parameterNumber": 0,
        "typeOfFirstFixedSurface": 103,
        "scaleFactorOfFirstFixedSurface": 0,
        "scaledValueOfFirstFixedSurface": 2
      },
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

#### Metadata Schema Version

`schemaVersion` is the lowest metadata schema a reader must implement to
parse the file. It versions the metadata JSON only; the container layout is
versioned by the FixedHeader `version` field, which stays 1 for every
schema version below.

| `schemaVersion` | Introduces | Recognized by |
|---:|---|---|
| 1 | — | a uniform whole-hour `time` axis declaring `stepHours` |
| 2 | Explicitly listed forecast hours | a whole-hour `time` axis listing `hours` |
| 3 | Per-variable GRIB2 identity, and a time axis in units it names | a `parameter` block on every variable, and a `time` block declaring `unitSeconds` |

An encoder must emit the lowest version that can express the file: the
highest version any single feature of the metadata requires, and no higher.
For the versions defined so far that is 3 when the variables carry
`parameter`, otherwise 2 when the axis lists `hours`, otherwise 1. A
schemaVersion 3 file with a uniform axis is still version 3 — the parameter
block, not the axis, sets its floor — and a `parameter` block is invalid
below version 3.

Decoders enforce both sides of that rule: they must reject a schema version
they do not implement, and must also reject a declared version higher than
the lowest the metadata needs (see Error Handling). Rejecting unknown
versions is what makes an older decoder reject a file it would otherwise
misread — silently ignoring an unknown `parameter` block, or misreading a
mixed-cadence axis — instead of guessing; rejecting overdeclared versions
gives any given metadata exactly one valid encoding. Every already published
bundle stays valid and byte-identical under both rules.

*Schema version 3 is what the reference encoder writes; versions 1 and 2
remain readable and are still published by nothing. Because older decoders
reject a version they do not implement, the schemaVersion-3-capable frontend
must be deployed before the first version 3 run is published.*

#### Variable Identity

From schemaVersion 3, every variable declares what it *is* in GRIB2's own
terms, so a reader can recognize a field without matching on `id` strings.
The block is required on every variable of a version 3 file and invalid
below it.

| Field | GRIB2 origin | Rule |
|---|---|---|
| `discipline` | Section 0, octet 7 | 0–255 |
| `parameterCategory` | Section 4, code table 4.1 | 0–255 |
| `parameterNumber` | Section 4, code table 4.2 | 0–255 |
| `typeOfFirstFixedSurface` | Section 4, code table 4.5 | 0–255 |
| `scaleFactorOfFirstFixedSurface` | Section 4 | −127…127, or `null` |
| `scaledValueOfFirstFixedSurface` | Section 4 | 0…4294967294, or `null` |
| `typeOfStatisticalProcessing` | Section 4, code table 4.10 | 0–255, optional |

The surface value is `scaledValueOfFirstFixedSurface × 10^−scaleFactorOfFirstFixedSurface`
in that surface's own unit. A surface that carries no value — "entire
atmosphere" (type 10), or a source that encodes the value as missing —
writes **both** halves as `null`, which is how GRIB2 encodes it; one half
alone describes nothing and is invalid. Both keys are always present.

`typeOfStatisticalProcessing` is absent for an instantaneous field and
present when the values are a statistic over the step. It is what
distinguishes a `prate` bundle whose source carried an instantaneous rate
(GFS pgrb2) from one derived by de-accumulating or de-averaging (ECMWF,
GFS sflux), which are otherwise the same parameter.

No other keys are allowed; a version 3 decoder must reject an unknown one,
so any later addition is a new schema version. GRIB2's local-use ranges
(parameter numbers and surface types 192–254) need no special treatment
here — a local number is an ordinary number, and two of the variables below
already use one.

Registered variable identities:

| `id` | Parameter | Surface | Notes |
|---|---|---|---|
| `tmp2m` | 0 / 0 / 0 | 103, 2 m | |
| `prate` | 0 / 1 / 7 | 1, 0 | `typeOfStatisticalProcessing: 0` when derived (ECMWF, sflux) |
| `ugrd10m` | 0 / 2 / 2 | 103, 10 m | |
| `vgrd10m` | 0 / 2 / 3 | 103, 10 m | |
| `dswrf` | 0 / 4 / 192 | 1, 0 | NCEP local parameter |
| `cref` | 0 / 16 / 5 | 10, no value | Composite reflectivity, entire atmosphere |

#### Time Axis

The axis is a list of integer **frame offsets** from `runTime`, each worth
`unitSeconds` seconds. The frame at offset `o` is valid at
`runTime + o × unitSeconds`, and `o` is exactly what the index's
`frameOffset` field carries.

From schemaVersion 3 the block is:

- **`unitSeconds`** — seconds per offset unit. It must be a whole divisor of
  3600, and it must be the **coarsest** unit that expresses every offset
  exactly, so an axis has one encoding rather than one per divisor of its
  step. Formally, `gcd(3600 / unitSeconds, offset₀, offset₁, …)` must be 1.
  Every forecast source is hourly and declares 3600, which leaves its
  offsets equal to its forecast hours; the radar mosaic publishes every six
  minutes and declares 360.
- **`firstFrameOffset`** and **`frameCount`** — always present.
- Exactly one of **`frameStep`** (the axis is uniform, and frame `i` is at
  `firstFrameOffset + i × frameStep`) and **`frameOffsets`** (the axis does
  not hold one step throughout and is listed outright). A forecast run lists
  its offsets because the source changes cadence partway; an observation
  series lists them because the archive has gaps where a publication was
  missed.

No other field may appear in the block.

A miniature mixed axis, hourly then three-hourly:

```json
"time": {
  "unitSeconds": 3600,
  "firstFrameOffset": 0,
  "frameCount": 6,
  "frameOffsets": [0, 1, 2, 3, 6, 9]
}
```

The production GFS 240-hour axis is the same shape at full length: the
161-element list `0, 1, …, 119, 120, 123, 126, …, 237, 240`.

A `frameOffsets` array must have exactly `frameCount` elements, must be
strictly increasing, must begin with `firstFrameOffset`, and every element
must be in `[0, 65534]` — the `frameOffset` field is a u16 and 65535 is the
`dependencyOffset` sentinel. The same upper bound applies to a uniform
axis's last offset, `firstFrameOffset + (frameCount − 1) × frameStep`. A
`frameOffsets` array whose steps are all equal is invalid — a uniform axis
has exactly one encoding, `frameStep` — which also rules out arrays of fewer
than three elements, since any shorter axis is trivially uniform. Declaring
both `frameStep` and `frameOffsets`, or neither, makes the file invalid.

**Schema versions 1 and 2** describe the same thing in whole hours only:
`firstForecastHour` plus one of `stepHours` (uniform, version 1) and `hours`
(listed, version 2), with `unitSeconds` implicitly 3600. Those files remain
valid and are read unchanged. The two shapes never mix: a version 1 or 2
block carrying any version 3 field, or a version 3 block carrying any of the
hour-named fields, is invalid.

A **segment** is a maximal run of frames with a constant step. For frames
`h[0] < h[1] < … < h[n-1]` with steps `d[i] = h[i+1] - h[i]`, a segment
boundary falls between `h[i]` and `h[i+1]` exactly where `d[i] != d[i-1]`
— equivalently, the last frame of the old cadence closes the earlier
segment and the first frame of the new cadence opens the next. A uniform
axis is one segment. The GFS 240-hour axis is two: 121 hourly frames f000–f120,
then 40 three-hourly frames f123–f240.

Segments are derived, never stored. They matter twice: they bound temporal
grouping (see Temporal Prediction), and they are where the physical meaning
of a derived field changes — past a step increase, a precipitation rate
obtained by de-accumulating or de-averaging its source records is a mean
over a longer window, so the field is smoother and its peaks lower on the
far side of the boundary. That is a property of the source data, not of
this container, but a renderer that labels units should not claim the two
segments are the same measurement.

The encoder rotates longitude columns so the first column is `-180`,
preserving north-to-south row order. Grids that natively start at
Greenwich (the GFS surface-flux Gaussian grid) are rolled by the encoder
into the same `-180`-first layout, so every published grid shares it. A
renderer applies inverse Web Mercator, converts longitude and latitude to
grid coordinates, and samples this layout directly.

#### Regional Grids

A grid need not cover the world. The `grid` block already says exactly what
a file covers, and a regional file is an ordinary file whose origin and
extent name a window rather than the globe — the container, the index, and
every codebook are unchanged. The reference pipeline publishes such files
for its historical showcase cases, cut out of a global run.

Two rules follow for readers, and both are properties a global grid
satisfies trivially:

- `wrapLongitude` is true only when `width x longitudeStep` is 360 degrees.
  A reader must take the horizontal wrap from this field, not from the
  model: sampling filters may wrap across the antimeridian only when it is
  true, and must clamp otherwise.
- The grid coordinate of a longitude is
  `(longitude - firstLongitude) mod 360 / longitudeStep`. Taking the offset
  modulo 360 is what keeps a window that crosses the antimeridian
  contiguous — such a window declares a `firstLongitude` near +180 and runs
  past it. Coordinates outside `[0, width)` or `[0, height)` are outside the
  file; a renderer must draw nothing there rather than clamp, which would
  smear the border across the map.

`model` and `product` identify the source dataset. Registered pairs:

| `model` | `product` | Grid | Steps published | Notes |
|---|---|---|---|---|
| `GFS` | `pgrb2.0p25` | 1440 × 721, 0.25° | 1 h to f120, 3 h to f240 | All series include the analysis frame (f000). `prate` is an instantaneous rate at every step |
| `ECMWF` | `ifs-0p25` | 1440 × 721, 0.25° | 3 h to 144 h, 6 h to 240 h | `prate` is de-accumulated from the run-total `tp`, so its series has no analysis frame and starts at `firstFrameOffset: 3` |
| `GFS-SFLUX` | `sfluxgrb` | 3072 × 1536 Gaussian, ~13 km | 1 h to f120, 3 h to f240 | `prate` is de-averaged from window-cumulative records and starts at `firstFrameOffset: 1`; the only source shipping `dswrf` |
| `CMA-RADAR` | `l3-mst-cref` | tile grid, 360/(256·2^z) degrees | 6 min, as published | Observations, not a forecast: `runTime` is the first observation and offsets count from it. The only source shipping `cref`, and the only one whose `unitSeconds` is not 3600; the axis lists its offsets wherever a publication was missed |

How far a run is published is a pipeline choice, not a format constraint;
the steps above are what each source makes available. A uniform series
declares a `frameStep`, while a series that runs to 240 hours crosses a step
change on every forecast source above and therefore lists its `hours`; an
observation series lists them wherever the archive has a gap.

Not every source is a forecast. `CMA-RADAR` is a series of observed
analyses, and the container describes it with no change: `runTime` is the
first observation in the series and each frame's `frameOffset` counts
six-minute units from it. A reader that labels the axis should take that
from the dataset identity rather than assume every file is a forecast.

Time axes may therefore differ between bundles of one run, in both step and
extent. The container layout is identical for every model — only the
metadata identity, the grid, and the time axis differ. Readers must derive
the frame list from `time` and the grid from `grid`, never from the model
name, and must not assume two bundles of one run share an axis.

### IndexHeader

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 4 | bytes | magic | ASCII `IDX1` |
| 4 | 2 | u16 | entrySize | Fixed at 40 for v1 |
| 6 | 2 | u16 | version | Fixed at 1 for v1 |
| 8 | 4 | u32 | entryCount | Must equal (variables × frames per variable) declared in metadata |
| 12 | 4 | u32 | reserved | Must be 0 |

### PlaneEntry

Index entries are sorted by `(variableId, frameOffset)`. Each entry is 40
bytes:

| Offset | Length | Type | Field | Rule |
|---:|---:|---|---|---|
| 0 | 1 | u8 | variableId | Registered ids below |
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

Registered `variableId` values:

| Value | Variable | Field |
|---:|---|---|
| 1 | `tmp2m` | 2 m temperature |
| 2 | `prate` | Surface precipitation rate |
| 3 | `ugrd10m` | 10 m wind, U component |
| 4 | `vgrd10m` | 10 m wind, V component |
| 5 | `dswrf` | Surface downward shortwave radiation flux, instantaneous |
| 6 | `cref` | Radar composite reflectivity over the entire atmosphere |

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
- PREVIOUS decompresses to a one-byte residual, then adds the plane of the
  preceding frame on the time axis: `frameOffsets[i - 1]`, or
  `frameOffset - frameStep` on a uniform axis. It is not `frameOffset - 1` —
  on any axis whose step is not one unit, that plane does not exist. A
  PREVIOUS entry on the first frame of the axis is invalid, and every
  PREVIOUS entry must carry exactly that preceding offset in
  `dependencyOffset` — like ANCHOR, the dependency is explicit in the index,
  never the sentinel, never left to be derived, so the same file has only
  one encoding. (No reference bundle uses PREVIOUS; the reference encoder
  emits only RAW and ANCHOR.)
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
| `cref` | 0 dBZ | 0.5 | 160 | 255 | 0.25 dB |

The `compact` profile doubles each `scale` (temperature 1.0 → maximumCode
110, wind 1.0 → 127, dswrf 10 → 127, cref 1.0 → 80).

`cref` code 0 is both "no echo" and "outside the radar network's coverage".
A ground mosaic is a regional product on a rectangular grid, and this
container carries no bitmap, so the bottom of the codebook is what a
renderer paints as nothing. It is deliberately an ordinary in-range code
rather than a reserved one: a renderer that interpolates codes before its
palette lookup would otherwise colour the gap between a reserved code and
its neighbour with a class the data never reached.

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
  smooth enough for temporal prediction. Each segment of the time axis
  splits independently into groups of 6 frames, so a group never spans a
  change of step. Within each group of `n` frames, the frame at zero-based
  index `floor(n / 2)` is the anchor: it uses predictor RAW, and every
  other frame in the group uses ANCHOR residuals against it. A trailing
  single-frame group is its own anchor and uses RAW. Random access to any
  frame therefore costs at most two plane decodes (anchor + target).
  `groupId` counts groups sequentially across the whole variable and does
  not restart at a segment boundary.
- **Precipitation (`prate`)** uses independent RAW planes for every
  frame, with `groupId` mirroring `frameOffset`. Precipitation
  regions move with weather systems; fixed-grid differencing creates both
  entering and leaving edges and measurably increases compressed size.

Grouping per segment rather than across the whole timeline keeps every
ANCHOR residual a difference between frames one step apart. A group
straddling the GFS 120-hour transition would difference frames three hours
apart against an anchor chosen among frames one hour apart — still
lossless, since residuals wrap modulo 256, but a larger residual and a
worse-compressing one. Splitting costs at most one extra RAW anchor per
segment boundary per variable.

Segment-aligned grouping is an encoder rule, not a decode-time invariant.
A decoder validates the dependency structure recorded in the index (see
Error Handling) and never needs to derive segments to decode or to
validate; a group that did straddle a boundary would still reconstruct
exactly and is not rejected.

Xue v1 requires complete input planes: every point of every plane carries a
code. The forecast grids satisfy that outright, and a source whose product
does not cover its whole grid (the radar mosaic) resolves it before
quantization by mapping absent points to the bottom of the variable's
codebook — a value, not a gap. A future missing-data implementation should
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

The anchor's actual frame offset remains in the index; sequential playback
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
checks past the prefix. An explicit `hours` array costs roughly five bytes
per frame, under 1 KB for a 240-hour axis, so the prefix stays small enough
to fetch in one range request. A streaming reader then range-fetches
payload spans on demand; the per-group contiguity above means the bytes for
one frame form
one or two contiguous spans. Integrity comes from the per-plane CRC-32 and
the Zstandard frame checksums, so a whole-file checksum is only meaningful
for full downloads.

## Error Handling

A decoder must reject:

- Unknown container versions, and metadata `schemaVersion` values the
  decoder does not implement.
- Nonzero reserved fields in a known version.
- Unknown predictor, compression, variableId, or flags values.
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
  without one in a schemaVersion 3 file; a parameter code outside 0–255;
  a fixed surface with exactly one of its scale factor and scaled value
  `null`; a key the block does not define.
- A declared `schemaVersion` other than the lowest able to express the
  metadata — a schemaVersion 2 file that declares `stepHours`, or a
  schemaVersion 1 or 2 file whose variables carry `parameter` or whose time
  block names a unit.
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

## File Naming

These conventions sit outside the container but are what the reference
pipeline produces: one file per variable per run (`tmp2m.xue`,
`prate.xue`, `dswrf.xue`, `cref.xue`), the two-variable `wind10m.xue`, and
half-resolution renditions named `<variable>.half.xue` — structurally
identical bundles whose metadata declares the decimated grid. A run
directory also carries a `manifest.json` describing every bundle (path,
byte length, whole-file CRC-32, resolution variants, optional poster and
H.264 companion artifacts); the manifest and a tiny mutable `latest.json`
pointer are delivery concerns defined by the reference implementations
(`xue/manifest.py`, `web/src/manifest.ts`), not by this container spec.
