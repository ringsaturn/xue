# Xue Binary Format Specification

This document is the normative specification of the Xue container (`.xue`),
a per-variable spatiotemporal binary format for packing a full forecast run
of one gridded weather field into a single indexed file that a browser can
decode frame by frame.

Two container versions exist, and they differ in exactly one thing: what a
payload *is*.

- **v1** is plane-major — one payload is one whole plane of one frame.
  Sections marked *(v1)* below describe it. Nothing writes it any more, and
  every decoder must keep reading it: published runs and showcase cases carry
  those bytes and are never rebuilt.
- **v2** is tiled — one payload is a **chunk**: one spatial tile of one
  temporal group for one variable, so a reader can fetch just the region it
  is showing and read one cell's whole series cheaply. It is what both
  encoders write, and it is specified in *[Container v2](#container-v2)*.

Everything else is shared and is specified once: the fixed header, the
metadata JSON and its schema versions, the quantization codebooks, and the
modulo-256 residual arithmetic. A decoder rejects a container version it
does not implement, so a v2-capable shell must be deployed before the first
v2 run is published.

The Python encoder (`xuebuild/binformat.py`, `xuebuild/quantize.py`,
`xuebuild/temporal.py`) and the Rust decoder (`rust/xue`) are the two reference
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
  radiation) are coded in six-frame groups with one-byte residuals; groups
  are formed inside a segment of constant step, so no group straddles a
  change of cadence. In v1 a group is a middle RAW anchor plus residuals
  against it, capping random access at two plane decodes; in v2 a group is
  packed whole into each tile's chunk and the residuals chain against the
  previous frame, which is both smaller and, since the chunk decompresses
  as a unit, no more expensive to seek into. Precipitation fields move with
  weather systems, and temporal differencing increases their compressed
  size; precipitation is stacked RAW in both versions.
- **One independent Zstandard frame per payload** — a plane in v1, a chunk
  in v2 — for direct indexing, sequential reads, and localized failure
  isolation.
- **Space is addressable too (v2).** A payload covers one tile of the grid,
  so a zoomed-in viewport fetches only the tiles it shows, and one cell's
  whole series costs one chunk per group rather than a decode of every
  plane. The tiles are cut in grid space, not Web Mercator: reprojection
  stays in the fragment shader.
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

Both versions have the same five sections in the same order; the index and
what the payloads are differ. This is v1 — see
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

`numericId` is the variable's `variableId` — the handle the binary index
refers to it by, unique within the file and meaningless outside it (see
*[`variableId` is file-local](#variableid-is-file-local)*). The encoders
here number the variables 1…n in the order they are listed.

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

**The `parameter` block is a variable's identity.** Neither `variableId` —
a file-local handle (see *[`variableId` is file-local](#variableid-is-file-local)*)
— nor the `id` string carries it; the parameter triple and the fixed surface
do, and two files describe the same field exactly when their blocks agree.

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

The identities this pipeline produces are below. This is **not** an
admission list: a decoder validates the shape of a `parameter` block, never
its contents against this table, and a file naming a parameter that is not
here is a well-formed file. The table says which fields a *chart-aware*
reader — one that has a palette, a contour interval or a legend for the
quantity — can recognize; anything else it can still decode, and simply has
no chart for.

| `id` | Parameter | Surface | Notes |
|---|---|---|---|
| `tmp2m` | 0 / 0 / 0 | 103, 2 m | |
| `prate` | 0 / 1 / 7 | 1, 0 | `typeOfStatisticalProcessing: 0` when derived (ECMWF, sflux) |
| `ugrd10m` | 0 / 2 / 2 | 103, 10 m | |
| `vgrd10m` | 0 / 2 / 3 | 103, 10 m | |
| `dswrf` | 0 / 4 / 192 | 1, 0 | NCEP local parameter |
| `cref` | 0 / 16 / 5 | 10, no value | Composite reflectivity, entire atmosphere |
| `gust` | 0 / 2 / 22 | 1, 0 | Instantaneous surface wind gust |
| `tcdc` | 0 / 6 / 1 | 10, no value | Total cloud cover, entire atmosphere; the instantaneous record, not the interval average |
| `cape` | 0 / 7 / 6 | 1, 0 | Surface-based CAPE (not the mixed-layer variants on surface type 108) |
| `lcdc` / `mcdc` / `hcdc` | 0 / 6 / 3, 0 / 6 / 4, 0 / 6 / 5 | 214 / 224 / 234, no value | Low / middle / high cloud cover, each its own parameter on its own layer surface; the instantaneous records |
| `vis` | 0 / 19 / 0 | 1, 0 | Surface visibility, quantized in km |
| `dpt2m` | 0 / 0 / 6 | 103, 2 m | 2 m dew point |
| `aptmp2m` | 0 / 0 / 21 | 103, 2 m | NCEP's 2 m apparent temperature |
| `tmpsfc` | 0 / 0 / 0 | 1, 0 | Surface (skin) temperature — the SST over water |
| `icec` | 10 / 2 / 0 | 1, 0 | Sea ice cover, a 0–1 proportion quantized in percent |
| `icetk` | 10 / 2 / 1 | 1, 0 | Sea ice thickness |
| `htsgw` | 10 / 0 / 3 | 1, no value | Significant height of combined wind waves and swell (GFS-Wave); WAVEWATCH III writes the surface value as 1, so none is declared and either is accepted |
| `perpw` | 10 / 0 / 11 | 1, no value | Primary wave mean period (GFS-Wave) |
| `dirpw` | 10 / 0 / 10 | 1, no value | Primary wave direction, degrees true the waves come from (GFS-Wave); a record's 360 is reduced to 0 |
| `uwave` / `vwave` | 10 / 0 / 250, 10 / 0 / 251 | 1, no value | Wave vector components in metres: the significant wave height laid along the direction the waves travel, derived by the encoder from `htsgw` and `dirpw` as the wind's `(-h sin θ, -h cos θ)` — Xue-local parameter numbers |
| `prmsl` | 0 / 3 / 1 | 101, no value | Mean sea level pressure, the quantity ECMWF calls `msl` and encodes as 0 / 3 / 0 on this surface — accepted on input, never written (not NCEP's MSLET, 0 / 3 / 192) |
| `hgt<level>` | 0 / 3 / 5 | 100, `<level>` hPa in Pa | Geopotential height, one variable per isobaric surface |
| `tmp<level>` | 0 / 0 / 0 | 100, `<level>` hPa in Pa | Temperature on the isobaric surface |
| `rh<level>` | 0 / 1 / 1 | 100, `<level>` hPa in Pa | Relative humidity on the isobaric surface |
| `spfh<level>` | 0 / 1 / 0 | 100, `<level>` hPa in Pa | Specific humidity on the isobaric surface, quantized in g/kg |
| `ugrd<level>` / `vgrd<level>` | 0 / 2 / 2, 0 / 2 / 3 | 100, `<level>` hPa in Pa | Wind components on the isobaric surface |
| `uqflx<level>` / `vqflx<level>` | 0 / 1 / 250, 0 / 1 / 251 | 100, `<level>` hPa in Pa | Water vapour flux components, `q·V/g` in g·cm⁻¹·hPa⁻¹·s⁻¹ — Xue-local parameter numbers |
| `vvel<level>` | 0 / 2 / 8 | 100, `<level>` hPa in Pa | Vertical velocity ω in Pa/s on the isobaric surface |
| `thetae<level>` | 0 / 0 / 3 | 100, `<level>` hPa in Pa | Equivalent potential temperature in K, derived by the encoder (Bolton 1980) from the temperature and specific humidity on the surface — GRIB2's EPOT number |

The eight registered isobaric surfaces are 1000, 925, 850, 700, 500, 300, 250
and 200 hPa, and every isobaric family is registered on all eight: `hgt1000`
… `hgt200`, `tmp1000` … `tmp200`, and so on. Within a family the variables
differ only in the surface value, which is written in the surface's own unit
— pascals — so `hgt500` carries `scaleFactorOfFirstFixedSurface: 0`,
`scaledValueOfFirstFixedSurface: 50000`.

The water vapour flux is not a GRIB2 field at all: the encoder derives it on
each surface as the specific humidity (g/kg) times the wind component (m/s)
over standard gravity (9.80665 m/s²), the quantity a synoptic chart contours
in g·cm⁻¹·hPa⁻¹·s⁻¹. GRIB2 has no standard parameter for a per-level
horizontal vapour flux, so the two components take local-use numbers 250 and
251 in the moisture category — an ordinary number in the format's terms, and
one no centre this pipeline reads from uses.

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

The `crc32` field uses CRC-32/IEEE — polynomial `0x04C11DB7`, reflected
implementation `0xEDB88320`, initial value and final XOR `0xFFFFFFFF`,
identical to zlib's `crc32` — computed over the reconstructed quantized
plane. The Zstandard checksum validates the compressed payload, while CRC32
validates predictor reconstruction.

#### `variableId` is file-local

`variableId` is a **handle**, not a name: the whole of its meaning is that it
ties an index entry to one variable descriptor in *this file's* metadata. It
must be 1–255 and unique within the file, and every id an index entry names
must appear in the file's `variables`; there is no global table to check it
against, and a decoder must not carry one. What a variable *is* lives in the
metadata: its `parameter` block (see
*[Variable Identity](#variable-identity)*) and its `id` string.

The encoders in this repository assign ids **positionally**: 1…n by the
variable's order in the bundle's variable list, which is also the order the
v2 chunk layout uses. A single-variable bundle is therefore always id 1, and
a vector bundle is 1 for the U component and 2 for the V component.

*Historical assignment.* Encoders before this rule drew ids from a global
registry, and files already published carry those numbers: 1 `tmp2m`,
2 `prate`, 3 `ugrd10m`, 4 `vgrd10m`, 5 `dswrf`, 6 `cref`, 7 `prmsl`,
8–15 `hgt1000` … `hgt200`, 16–23 `tmp1000` … `tmp200`,
24–31 `rh1000` … `rh200`, 32–39 `spfh1000` … `spfh200`,
40–47 `ugrd1000` … `ugrd200`, 48–55 `vgrd1000` … `vgrd200`,
56–63 `uqflx1000` … `uqflx200`, 64–71 `vqflx1000` … `vqflx200` (each
isobaric family in the level order 1000, 925, 850, 700, 500, 300, 250, 200).
No decoder ever required those numbers — they were only ever matched against
the file's own metadata — so every such file stays valid, and nothing needs
rebuilding.

### Payload Rules (v1)

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
| `gust` | 0 m/s | 0.5 | 254 | 255 | 0.25 m/s |
| `tcdc` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `cape` | 0 J/kg | 25 | 254 | 255 | 12.5 J/kg |
| `lcdc` / `mcdc` / `hcdc` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `vis` | 0 km | 0.1 | 254 | 255 | 0.05 km |
| `dpt2m` | −70 °C | 0.5 | 220 | 255 | 0.25 °C |
| `aptmp2m` | −90 °C | 1 | 150 | 255 | 0.5 °C |
| `tmpsfc` | −60 °C | 0.5 | 254 | 255 | 0.25 °C |
| `icec` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `icetk` | 0 m | 0.02 | 254 | 255 | 0.01 m |
| `htsgw` | 0 m | 0.1 | 254 | 255 | 0.05 m |
| `perpw` | 0 s | 0.1 | 254 | 255 | 0.05 s |
| `dirpw` | 0° | 1.5 | 239 | 255 | 0.75° |
| `uwave` / `vwave` | −25.4 m | 0.2 | 254 | 255 | 0.1 m |
| `prmsl` | 870.5 hPa | 1 | 254 | 255 | 0.5 hPa |
| `hgt1000` | −905 m | 10 | 254 | 255 | 5 m |
| `hgt925` | −249 m | 6 | 254 | 255 | 3 m |
| `hgt850` | 423 m | 6 | 254 | 255 | 3 m |
| `hgt700` | 1911 m | 6 | 254 | 255 | 3 m |
| `hgt500` | 4252 m | 8 | 254 | 255 | 4 m |
| `hgt300` | 7505 m | 10 | 254 | 255 | 5 m |
| `hgt250` | 8598 m | 12 | 254 | 255 | 6 m |
| `hgt200` | 10086 m | 12 | 254 | 255 | 6 m |
| `tmp1000` | −60 °C | 0.5 | 240 | 255 | 0.25 °C |
| `tmp925` | −65 °C | 0.5 | 230 | 255 | 0.25 °C |
| `tmp850` | −70 °C | 0.5 | 230 | 255 | 0.25 °C |
| `tmp700` | −75 °C | 0.5 | 220 | 255 | 0.25 °C |
| `tmp500` | −85 °C | 0.5 | 200 | 255 | 0.25 °C |
| `tmp300` | −95 °C | 0.5 | 190 | 255 | 0.25 °C |
| `tmp250` | −100 °C | 0.5 | 190 | 255 | 0.25 °C |
| `tmp200` | −100 °C | 0.5 | 180 | 255 | 0.25 °C |
| `rh<level>` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `spfh1000` / `spfh925` | 0 g/kg | 0.2 | 254 | 255 | 0.1 g/kg |
| `spfh850` / `spfh700` | 0 g/kg | 0.1 | 254 | 255 | 0.05 g/kg |
| `spfh500` | 0 g/kg | 0.02 | 254 | 255 | 0.01 g/kg |
| `spfh300` | 0 g/kg | 0.01 | 254 | 255 | 0.005 g/kg |
| `spfh250` / `spfh200` | 0 g/kg | 0.005 | 254 | 255 | 0.0025 g/kg |
| `ugrd<level>` / `vgrd<level>` | −127 m/s | 1 | 254 | 255 | 0.5 m/s |
| `uqflx<level>` / `vqflx<level>` | −63.5 g·cm⁻¹·hPa⁻¹·s⁻¹ | 0.5 | 254 | 255 | 0.25 |
| `vvel<level>` | −6.35 Pa/s | 0.05 | 254 | 255 | 0.025 Pa/s |
| `thetae1000` | 235 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae925` | 232 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae850` | 230 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae700` | 235 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae500` | 250 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae300` | 285 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae250` | 295 K | 0.5 | 254 | 255 | 0.25 K |
| `thetae200` | 305 K | 0.5 | 254 | 255 | 0.25 K |

The `compact` profile doubles each `scale` (temperature 1.0 → maximumCode
110, wind 1.0 → 127, dswrf 10 → 127, cref 1.0 → 80, gust 1.0 → 127, every
cloud cover 1.0 → 100, cape 50 → 127, vis 0.2 → 127, dpt2m 1.0 → 110,
aptmp2m 2 → 75, tmpsfc 1.0 → 127, icec 1.0 → 100, icetk 0.04 → 127, htsgw
and perpw 0.2 → 127, and every pressure-family and isobaric codebook → half
its maximumCode over the same range). The one exception to "the same range"
is `dirpw`, whose compact codebook stops at 357° (3 → 119): 360 / 3 codes
would put 360°, which is 0°, back on the grid — and the wave vector's, which
stops at ±25.2 m (0.4 → 126) so that 0 stays on the grid: land is (0, 0) in
the pair, and the middle code of both. `balanced` takes the compact `icec`
beside the compact humidity and cloud cover.

The wave fields are the first whose records do not cover the grid: GFS-Wave
carries a bitmap, and land comes out of GDAL as its nodata value (9999). The
encoder maps those points to code 0 — 0 m, 0 s, 0° — before quantization,
the way the radar mosaic's fill is handled (see "Xue v1 requires complete
input planes" below); the `nodataCode` is still never written.

The isobaric temperature takes its range per level: the low end holds the
Antarctic winter at every surface, the high end the below-ground
extrapolation the lowest surfaces take under high terrain, and no single
127-degree window covers both 850 hPa in summer and 200 hPa in winter.
Specific humidity spans two orders of magnitude between the surface and the
upper troposphere, so its step follows the level. Relative humidity is the
noisiest field published, so the `balanced` profile takes its `compact`
codebook (1 %, maximumCode 100), and the cloud covers — the same kind of
field on the same scale — follow it; those are the departures from quality
in that profile besides precipitation. The equivalent potential temperature
takes a 127 K window per level at the temperature's step, placed so the
warm-moist tropical end fits (850 hPa runs to 357 K); only the 850 hPa
window is verified against an analysis, the others follow the potential
temperature's rise with height. None of the isobaric fills is contoured, so
none carries the half-code rule below.

The pressure family's offsets are chosen so that every standard contour value
lands exactly **half a code** off: `(contour − offset) / scale` has fractional
part 0.5, for the level's own interval (4 hPa for `prmsl`, 30 m up to 700 hPa,
40 m at 500, 120 m above) and for the emphasised lines a chart is read by
(every 20 hPa on `prmsl`; 5840 and 5880 gpm on `hgt500`, the pair the
subtropical high is defined by). This is an **encoder** rule, in the same
sense as v1's segment grouping: a decoder must not assume it, and nothing in
the container records it. It exists because a contour drawn where the code is
flat covers a whole plateau instead of a line. The coverage is likewise fixed
per variable for all time rather than fitted per run — byte-identical
re-encoding, a stable legend, and comparable point series across runs all
depend on it.

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

The predictors and the modulo-256 arithmetic below are shared by both
container versions. Which of them a file may use, and what a residual is
computed against, is the index's business: v1 allows all four per plane
(*[PlaneEntry (v1)](#planeentry-v1)*), v2 allows RAW and PREVIOUS per
variable and chains inside a chunk (*[Container v2](#container-v2)*).

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

- **Linear-codebook fields (`tmp2m`, `ugrd10m`, `vgrd10m`, `dswrf`, the
  surface diagnostics, the ocean fields, the isobaric fills and the pressure
  family `prmsl` / `hgt<level>`)** are
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
— a `ugrd10m` group followed by the same `vgrd10m` group — so streaming one
wind frame touches two adjacent byte spans.

Interleaving all forecast times per grid point would turn single-plane
reads into wide file gathers, so Xue v1 keeps complete planes contiguous;
temporal continuity is represented by residuals instead.

## Streaming

This section describes v1; *[Streaming (v2)](#streaming-v2)* covers the
tiled container, which streams the same way with narrower spans.

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

These rules apply to both versions except where they name a v1 structure;
*[Error handling, additions for v2](#error-handling-additions-for-v2)* adds
the tiled index's own.

A decoder must reject:

- Unknown container versions, and metadata `schemaVersion` values the
  decoder does not implement.
- Nonzero reserved fields in a known version.
- Unknown predictor, compression, or flags values, and a `variableId` that
  is not one of the file's own metadata `variables` ("unknown" here means
  absent from *this* file, not absent from any registry — there is none).
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

## Container v2

Container v2 changes what a payload *is*: a **chunk** — one spatial **tile**
of one **temporal group** for one variable — instead of one whole plane of
one frame. A file holds the same set of quantized codes as its v1
counterpart, cut into tiles and packed group by group, so a reader can fetch
the region it is looking at, and read one cell's series by fetching one
chunk per group instead of decoding every plane. An encoder writes one
version or the other for a whole file; there is no mixing.

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

Tiling is a *storage* property, not a dataset property: the grid, the time
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
property of the grid, not of any tile — the last tile column does not wrap
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
(3) are not valid in v2: a chunk is decompressed whole, so an anchor buys no
random access and would only give one chunk a second valid encoding.

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

A chunk's *offset* is not stored. Payloads are contiguous in position order
starting at `dataOffset`: the offset of chunk 0 is `dataOffset` and the
offset of chunk `p + 1` is the offset of chunk `p` plus its
`compressedLength`. `fileSize = align8(end of the last chunk)`, and every
padding byte is zero. All of this is checked arithmetic against `fileSize`
before any allocation. Since the order is fixed by this specification and
the chunks are strictly adjacent, an offset would be redundant — and would
more than double the index.

This order is what makes the three access patterns cheap: a whole group is
one contiguous span (a global view fetches exactly what v1 fetched); the
tiles a viewport needs form one contiguous span per tile row per group; and
one cell's series is one chunk per group, with the two components of a wind
bundle adjacent.

### Chunk contents

Every chunk is one independent Zstandard frame that must carry a content
checksum, decompressing to exactly

```text
decodedLength = group.frameCount × tile.height × tile.width
```

bytes (the tile's *clipped* height and width), laid out frame-major: the
group's frames in axis order, each as the tile's rows top to bottom, each
row west to east — the same orientation as the plane. With ZSTD_DICT the
embedded dictionary applies, and a ZSTD_DICT file with `dictionaryLength = 0`
is invalid.

The reconstructed chunk is derived from the decompressed bytes by the
variable's predictor:

- **RAW** — the bytes are the codes.
- **PREVIOUS** — the first frame's bytes are its codes; every later frame's
  bytes are the modulo-256 residual against the reconstructed previous frame
  *of the same chunk*, which is the preceding frame on the axis because
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
therefore one chunk per group of one tile, independent of the number of
frames.

### Streaming (v2)

The structural prefix `[0, dataOffset)` validates as in v1. A streaming
reader derives every chunk's byte span from the index and range-fetches
spans on demand; the spans a viewport needs are contiguous per tile row and
may be coalesced across small gaps at the reader's discretion. Integrity is
per chunk (Zstandard checksum plus CRC-32).

The index is larger than v1's: the chunk count is the group count times the
tile count times the variable count, at 8 bytes each — 94 KB for a
420-tile, 28-group GFS bundle. That is one extra range request before the
first frame, and it is what buys every later request its narrowness.

### Error handling, additions for v2

A decoder must reject, in addition to the v1 rules that still apply (unknown
container versions, section geometry, padding, dictionary presence,
overflow):

- An IndexHeader whose magic, version or headerSize differ, or whose
  reserved words are nonzero.
- `tileWidth` or `tileHeight` of zero or larger than the grid.
- A `variableCount` or variable set that differs from the metadata's own —
  every `variableId` here must be a `variableId` there, and the two sets must
  be equal; unsorted or duplicate `variableId`s; a predictor other than RAW
  or PREVIOUS; nonzero reserved fields.
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
- Predictor: RAW for `prate` and `cref`; PREVIOUS for every linear-codebook
  field.
- Tile size is a per-source registry value; a half-resolution variant uses
  `(ceil(tileWidth / 2), ceil(tileHeight / 2))`, so a tile with the same
  number covers the same ground in both tiers. A regional (cropped) file
  tiles its own grid from its own origin with the source's tile size.
- **The tile is then clamped to the grid**: `min(tileWidth, width)` and
  `min(tileHeight, height)`. The format requires `1 <= tile <= grid` so that
  a single-tile file states its grid size exactly, and a regional crop is
  routinely smaller than its source's tile — a six-degree showcase window is
  24 x 24 cells against the 0.25-degree grid's 48 x 52. Clamping makes such a
  file one tile, which is the right answer: there is nothing left to
  subdivide.
- Compression is ZSTD at the production level with the content checksum; no
  dictionary.

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
(`xuebuild/manifest.py`, `web/src/manifest.ts`), not by this container spec.
