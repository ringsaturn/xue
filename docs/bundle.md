# Xue Bundle Metadata and Codes

A Xue bundle is one or more variables of one forecast run or observation
window, quantized to single-byte codes on the source's native grid. This
document is normative for what every bundle carries whatever writes it down:
the metadata JSON and its schema versions, the quantization codebooks, and
the modulo-256 residual arithmetic. The published layout is a Zarr v3 store
([`zarr-profile.md`](zarr-profile.md)); the `.xue` container
([`format.md`](format.md)) is the legacy layout every decoder keeps
reading. Both carry the codes and the metadata specified here unchanged.

The Python encoder (`xuebuild/metadata.py`, `xuebuild/quantize.py`,
`xuebuild/temporal.py`) is the reference; the native encoder and the Rust
decoder (`rust/xue`) are held to it by cross-language golden and parity
tests.

## Conventions

- Metadata is UTF-8 JSON.
- All quantization rounding is round-half-up: `round(x) = floor(x + 0.5)`.
  Every rounded quantity in this format is non-negative, so this equals
  round-half-away-from-zero. Implementations must not use round-half-even
  (the behavior of Python's built-in `round` and the IEEE 754 default); a
  mismatch changes codes for values landing exactly on a half step.

## Metadata JSON

The shape of a schemaVersion 3 or 4 document is
[`schemas/bundle-metadata.schema.json`](schemas/bundle-metadata.schema.json),
which is normative: a document that does not match it is not version 3 or 4
metadata. The sections below give the meaning of each field and the rules
across fields that a schema cannot say, which every reader checks after
the shape: the lowest version that fits, the one encoding of a time axis,
unique `numericId`s, and the halves of a GRIB2 value that are null
together. Versions 1 and 2 have no schema; their rules are this section's
prose alone. `tests/test_schemas.py` holds the frozen container corpus
(`tests/fixtures/v1/`), the synthetic run's containers, posters and stores,
and the committed stores to it; the shell checks a version 3 document with
the validator generated from it (`web/src/schema/generated/`), and the Rust
decoder's tests tie the fields it reads to the names the schema gives.

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
ship together in one two-variable `wind10m` bundle. Every variable's
quantization block must be one of the codebook types below.

`numericId` is a handle, unique within the bundle and meaningless outside
it: the variable's position in a store's group document, its `variableId`
in a container's index ([`format.md`](format.md), "`variableId` is
file-local"). The encoders here number the variables 1…n in the order they
are listed.

### Metadata Schema Version

`schemaVersion` is the lowest metadata schema a reader must implement to
parse the file. It versions the metadata JSON only; the layout that carries
it is versioned on its own (a store's `xue_profile`, a container's
FixedHeader `version`).

| `schemaVersion` | Introduces | Recognized by |
|---:|---|---|
| 1 | — | a uniform whole-hour `time` axis declaring `stepHours` |
| 2 | Explicitly listed forecast hours | a whole-hour `time` axis listing `hours` |
| 3 | Per-variable GRIB2 identity, and a time axis in units it names | a `parameter` block on every variable (optionally with `band` / `producer` / `aerosol` / `ensemble` beside it), and a `time` block declaring `unitSeconds` |
| 4 | A grid on a map projection | a `grid.projection` block in place of the four regular grid fields; variables and time block as in version 3 |

An encoder must emit the lowest version that can express the file: the
highest version any single feature of the metadata requires, and no higher.
For the versions defined so far that is 4 when the grid carries
`projection`, otherwise 3 when the variables carry `parameter`, otherwise 2
when the axis lists `hours`, otherwise 1. A schemaVersion 3 file with a
uniform axis is still version 3, since the parameter block sets its floor, and
a `parameter` block is invalid below version 3. A file on a regular grid
never declares version 4, and a reader that does not implement version 4
refuses a projected file rather than misplacing it.

Decoders enforce both sides of that rule: they must reject a schema version
they do not implement, and must reject a declared version higher than the
lowest the metadata needs (see Error Handling). Rejecting unknown versions
makes an older decoder reject a file it would otherwise misread; rejecting
overdeclared versions gives any given metadata exactly one valid encoding.
Every already published bundle stays valid and byte-identical under both
rules.

Schema version 3 is what the reference encoder writes; versions 1 and 2
remain readable and are published by nothing. Because older decoders reject
a version they do not implement, a decoder implementing a new version must
be deployed before the first file at that version is published.

### Variable Identity

From schemaVersion 3, every variable declares what it is in GRIB2's own
terms, so a reader can recognize a field without matching on `id` strings.
The block is required on every variable of a version 3 file and invalid
below it.

The `parameter` block is a variable's identity. Neither `variableId` (a
file-local handle) nor the `id` string carries it; the parameter triple and
the fixed surface do, and two files describe the same field exactly when
their blocks agree.

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
in that surface's own unit. A surface that carries no value ("entire
atmosphere", type 10, or a source that encodes the value as missing) writes
both halves as `null`, which is how GRIB2 encodes it; one half alone is
invalid. Both keys are always present.

`typeOfStatisticalProcessing` is absent for an instantaneous field and
present when the values are a statistic over the step. It distinguishes a
`prate` bundle whose source carried an instantaneous rate (GFS pgrb2) from
one derived by de-accumulating or de-averaging (ECMWF, GFS sflux), which are
otherwise the same parameter.

No other keys are allowed; a version 3 decoder must reject an unknown one,
so any later addition is a new schema version. GRIB2's local-use ranges
(parameter numbers and surface types 192–254) need no special treatment: a
local number is an ordinary number, and several variables below use one.

The identity written is the registered one, whatever record the encoder
read. Centres write the same quantity differently (ECMWF encodes its mean
sea level pressure as plain pressure on the mean sea level surface, its gust
as an interval maximum on the 10 m surface, its skin temperature as a
parameter of its own with no surface value), and the encoder's registry
accepts such a record as an alias (another parameter number on the same
surface) or an alternate (a whole other identity: surface, value,
statistical process, unit) of the variable, so that GFS and ECMWF publish
one chart under one `parameter` block. The table notes each such
acceptance; none of them is ever written.

The identities this pipeline produces are below. This is not an admission
list: a decoder validates the shape of a `parameter` block, never its
contents against this table, and a file naming a parameter not listed here
is a well-formed file. The table says which fields a chart-aware reader (one
with a palette, a contour interval or a legend for the quantity) can
recognize; anything else it can still decode.

| `id` | Parameter | Surface | Notes |
|---|---|---|---|
| `tmp2m` | 0 / 0 / 0 | 103, 2 m | |
| `prate` | 0 / 1 / 7 | 1, 0 | `typeOfStatisticalProcessing: 0` when derived (ECMWF, sflux) |
| `ugrd10m` | 0 / 2 / 2 | 103, 10 m | |
| `vgrd10m` | 0 / 2 / 3 | 103, 10 m | |
| `ugrd100m` / `vgrd100m` | 0 / 2 / 2, 0 / 2 / 3 | 103, 100 m | 100 m wind components, the turbine hub height; the `wind100m` bundle |
| `dswrf` | 0 / 4 / 192 | 1, 0 | NCEP local parameter |
| `cref` | 0 / 16 / 5 | 10, no value | Composite reflectivity, entire atmosphere; observed (the radar mosaic) or forecast (HRRR's `REFC`, NCEP-local 0 / 16 / 196, accepted on input and never written) |
| `rqi` | 209 / 8 / 0 | 102, 0 m | Radar quality index of the MRMS mosaic, 0–1, unitless. MRMS-local (discipline 209): no WMO parameter carries it, so the local identity is the one written |
| `gust` | 0 / 2 / 22 | 1, 0 | Instantaneous surface wind gust (GFS). ECMWF's `10fg` / `10fg3`, the same parameter on the 10 m surface (103, 10 m) as the maximum over the interval ending at the frame, product template 4.8, is accepted on input under this identity and never written; on ECMWF the series starts at the first step |
| `tcdc` | 0 / 6 / 1 | 10, no value | Total cloud cover, entire atmosphere; the instantaneous record, not the interval average. ECMWF's `tcc`, the ECMWF-local 0 / 6 / 192 on the ground surface as a 0–1 fraction, is accepted on input, scaled to percent, and never written |
| `cape` | 0 / 7 / 6 | 1, 0 | Surface-based CAPE (not the mixed-layer variants on surface type 108). ECMWF's `mucape`, the same parameter departing from surface type 17, the most unstable parcel's level, is accepted on input and never written |
| `cin` | 0 / 7 / 7 | 1, 0 | Surface-based convective inhibition in J/kg, never positive (not the mixed-layer variants on surface type 108) |
| `lcdc` / `mcdc` / `hcdc` | 0 / 6 / 3, 0 / 6 / 4, 0 / 6 / 5 | 214 / 224 / 234, no value | Low / middle / high cloud cover, each its own parameter on its own layer surface; the instantaneous records |
| `vis` | 0 / 19 / 0 | 1, 0 | Surface visibility, quantized in km |
| `dpt2m` | 0 / 0 / 6 | 103, 2 m | 2 m dew point |
| `aptmp2m` | 0 / 0 / 21 | 103, 2 m | NCEP's 2 m apparent temperature |
| `pwat` | 0 / 1 / 3 | 200, no value | Precipitable water, the column mass in kg/m² (a millimetre of water); NCEP writes it on its local "entire atmosphere (considered as a single layer)" surface, type 200, which carries no value like the WMO type 10 |
| `hpbl` | 0 / 3 / 196 | 1, 0 | Planetary boundary layer height in metres, NCEP's local number (the WMO table's 0 / 3 / 18 is not what pgrb2 carries) |
| `orog` | 0 / 3 / 5 | 1, 0 | Orography in metres, a static one-frame bundle. ECMWF open data's surface geopotential, 0 / 3 / 4 on the same surface, is accepted on input and divided by g; DWD's and Météo-France's geometric height, 0 / 3 / 6 in metres, is accepted as it is |
| `ptype` | 0 / 1 / 19 | 1, 0 | Precipitation type: one categorical field the encoder derives from the four flags below, in WMO code table 4.201's own values (1 rain, 3 freezing rain, 5 snow, 8 ice pellets, 0 none). Never fetched; the flags are inputs only and reach no bundle |
| `crain` / `cfrzr` / `cicep` / `csnow` | 0 / 1 / 192–195 | 1, 0 | The four 0/1 categorical precipitation-type flags pgrb2 carries (rain, freezing rain, ice pellets, snow; NCEP-local numbers), input-only, combined into `ptype` |
| `tmpsfc` | 0 / 0 / 0 | 1, 0 | Surface (skin) temperature, the SST over water. ECMWF's `skt`, its own parameter 0 / 0 / 17 with no surface value, is accepted on input and never written |
| `icec` | 10 / 2 / 0 | 1, 0 | Sea ice cover, a 0–1 proportion quantized in percent (GFS only) |
| `icetk` | 10 / 2 / 1 | 1, 0 | Sea ice thickness; ECMWF's `sithick` carries no surface value and a bitmap over land (GDAL's 9999, the bottom of the codebook), and is accepted on input |
| `htsgw` | 10 / 0 / 3 | 1, no value | Significant height of combined wind waves and swell (GFS-Wave, ECMWF `swh`); WAVEWATCH III writes the surface value as 1 and ECMWF none, so none is declared and any is accepted |
| `perpw` | 10 / 0 / 11 | 1, no value | Primary wave mean period (GFS-Wave); ECMWF's peak period `pp1d`, 10 / 0 / 34, is the nearest neighbour and accepted on input under this identity |
| `dirpw` | 10 / 0 / 10 | 1, no value | Primary wave direction, degrees true the waves come from (GFS-Wave); ECMWF's mean wave direction `mwd`, 10 / 0 / 14, is accepted the same way; a record's 360 is reduced to 0 |
| `wvhgt` / `wvper` / `wvdir` | 10 / 0 / 5, 10 / 0 / 6, 10 / 0 / 4 | 1, no value | Significant height, mean period and direction (degrees true the waves come from) of the wind waves (GFS-Wave); the direction is input-only, reduced like `dirpw`, and reaches a bundle only inside `uwindwave` / `vwindwave` |
| `swhgt` / `swper` / `swdir` | 10 / 0 / 8, 10 / 0 / 9, 10 / 0 / 7 | 241, 1 | Significant height, mean period and direction of the first (strongest) swell partition (GFS-Wave). Type 241 is NCEP's local "ordered sequence of data" surface, its value the partition's rank; only rank 1 is matched. The direction is input-only and reaches a bundle only inside `uswell` / `vswell` |
| `tsoil<n>` | 2 / 0 / 2 | 106, the layer's upper bound in m | Soil temperature in layer `n` (1–4) counted from the surface, in °C. The registered surface is GFS's (Noah layers 0–0.1, 0.1–0.4, 0.4–1 and 1–2 m, so 0, 0.1, 0.4 and 1); ECMWF's `sot`, the WMO 2 / 3 / 18 on its soil level surface (151) with layer `n` at value `n − 1` (IFS layers 0–7, 7–28, 28–100 and 100–289 cm), is accepted on input and never written. The id is the layer's index, not its depth: which depths a layer spans is the source's ([sources.md](sources.md)). Water carries no soil: the record's bitmap there becomes the bottom of the codebook, code 0, one step below every real value (GFS, which writes the ice's temperature over sea ice; the IFS writes the model's value over the sea) |
| `soilw<n>` | 2 / 0 / 192 | 106, the layer's upper bound in m | Volumetric soil moisture in layer `n`, total (liquid and frozen) water in m³/m³, NCEP's local SOILW (not the liquid part, SOILL). ECMWF's `vsw`, the WMO 2 / 0 / 25 on its soil level surface as for `tsoil<n>`, is accepted on input and never written. Water is code 0 as for `tsoil<n>` (GFS, which also masks the sea ice and writes 1 over the ice sheets, and AIFS; the IFS writes 0 over the sea) |
| `uwave` / `vwave` | 10 / 0 / 250, 10 / 0 / 251 | 1, no value | Wave vector components in metres: the significant wave height laid along the direction the waves travel, derived by the encoder from `htsgw` and `dirpw` as the wind's `(-h sin θ, -h cos θ)`; Xue-local parameter numbers |
| `uwindwave` / `vwindwave` | 10 / 0 / 252, 10 / 0 / 253 | 1, no value | Wind-wave vector components, derived from `wvhgt` and `wvdir` the way `uwave` / `vwave` are; Xue-local parameter numbers |
| `uswell` / `vswell` | 10 / 0 / 254, 10 / 0 / 255 | 241, 1 | Swell vector components of the first partition, derived from `swhgt` and `swdir` the same way, on their inputs' surface; Xue-local parameter numbers |
| `prmsl` | 0 / 3 / 1 | 101, no value | Mean sea level pressure, the quantity ECMWF calls `msl` and encodes as 0 / 3 / 0 on this surface, and HRRR writes as its MAPS reduction `MSLMA`, 0 / 3 / 198; both accepted on input, never written (not NCEP's MSLET, 0 / 3 / 192) |
| `hgt<level>` | 0 / 3 / 5 | 100, `<level>` hPa in Pa | Geopotential height, one variable per isobaric surface |
| `tmp<level>` | 0 / 0 / 0 | 100, `<level>` hPa in Pa | Temperature on the isobaric surface |
| `rh<level>` | 0 / 1 / 1 | 100, `<level>` hPa in Pa | Relative humidity on the isobaric surface |
| `spfh<level>` | 0 / 1 / 0 | 100, `<level>` hPa in Pa | Specific humidity on the isobaric surface, quantized in g/kg |
| `ugrd<level>` / `vgrd<level>` | 0 / 2 / 2, 0 / 2 / 3 | 100, `<level>` hPa in Pa | Wind components on the isobaric surface |
| `uqflx<level>` / `vqflx<level>` | 0 / 1 / 250, 0 / 1 / 251 | 100, `<level>` hPa in Pa | Water vapour flux components, `q·V/g` in g·cm⁻¹·hPa⁻¹·s⁻¹; Xue-local parameter numbers |
| `vvel<level>` | 0 / 2 / 8 | 100, `<level>` hPa in Pa | Vertical velocity ω in Pa/s on the isobaric surface |
| `thetae<level>` | 0 / 0 / 3 | 100, `<level>` hPa in Pa | Equivalent potential temperature in K, derived by the encoder (Bolton 1980) from the temperature and specific humidity on the surface; GRIB2's EPOT number |
| `cat<level>` | 0 / 19 / 29 | 100, `<level>` hPa in Pa | Clear-air turbulence as an eddy dissipation rate in m^(2/3)/s: the Ellrod TI1 index (vertical wind shear times total deformation) derived by the encoder from the wind and height on the surface and the ones bracketing it, projected per source onto the climatological EDR distribution (below); GRIB2's CAT EDR number. Registered on 300, 250 and 200 hPa only |
| `aod` | 0 / 20 / 102 | 10, no value | Aerosol optical thickness at 550 nm, total aerosol (GEFS-Aerosols), dimensionless. An aerosol product (template 4.48): the `aerosol` block beside the parameter (below) is part of the identity — type 62000, particles smaller than 20 µm, 545–565 nm — and tells it from the other six wavelengths and five species the same parameter carries in one file |
| `aoddust` / `aodsalt` / `aodsulf` / `aodorg` / `aodbc` | 0 / 20 / 102 | 10, no value | The same optical thickness for dust, sea salt, sulphate, particulate organic matter and black carbon: the same parameter and intervals, `aerosolType` 62001 / 62008 / 62006 / 62010 / 62009 |
| `pm25` | 0 / 13 / 193 | 1, 0 | PM2.5 surface concentration in µg/m³, NCEP's local PMTF number; `aerosol` type 62000, particles smaller than 2.5 µm, no wavelength interval |
| `pm10` / `pm10dust` | 0 / 13 / 192 | 1, 0 | PM10 surface concentration in µg/m³ (NCEP's local PMTC), total aerosol and dust alone: one parameter, `aerosolType` 62000 / 62001, particles smaller than 10 µm, no wavelength interval |
| `dustsfc` | 0 / 20 / 0 | 1, 0 | Surface dust concentration in µg/m³ (CMA-CW/DUST), the mass density of dust of every particle size the model carries: `aerosolType` 62001, no size interval and no wavelength interval (both type 255, limits null), which tells it from `pm10dust` |

The eight registered isobaric surfaces are 1000, 925, 850, 700, 500, 300,
250 and 200 hPa, and every isobaric family but `cat` is registered on all
eight. `cat` is registered on 300, 250 and 200 hPa: its vertical shear is
taken across 250–300 hPa for `cat300`, 200–300 for `cat250` and 200–250 for
`cat200`, the deformation on the surface itself.

The `cat<level>` derivation, in float64, in this order (both encoders):
centred differences on the uncropped global plane, before any crop window
is cut, with `R = 6371000 m`, `dx = R·cos φ·Δλ` per row and `dy = R·Δφ`,
the columns wrapping on a global grid; `DST = ∂u/∂x − ∂v/∂y`,
`DSH = ∂v/∂x + ∂u/∂y`, `DEF = √(DST² + DSH²)`; `VWS = |V_above − V_below| /
max(z_above − z_below, 1 m)`; `TI1 = VWS·DEF`; then
`EDR = exp(a + b·ln max(TI1, 10⁻¹²))` with `b = 0.5067 / σ_L`,
`a = −2.572 − b·μ_L`, the climatological ln EDR mean and standard deviation
of Sharman & Pearson (2017) and the source's own ln TI1 fit
(`SourceSpec.cat_calibration`); then every `EDR < 0.10` (ICAO's nil
turbulence) becomes 0, 0.10 itself keeping its code. The first and last
rows and every row past 85° latitude are written as 0, the codebook floor.

Within a family the variables differ only in the surface value, written in
the surface's own unit, pascals: `hgt500` carries
`scaleFactorOfFirstFixedSurface: 0`, `scaledValueOfFirstFixedSurface: 50000`.

The water vapour flux is not a GRIB2 field: the encoder derives it on each
surface as the specific humidity (g/kg) times the wind component (m/s) over
standard gravity (9.80665 m/s²), in g·cm⁻¹·hPa⁻¹·s⁻¹. GRIB2 has no standard
parameter for a per-level horizontal vapour flux, so the two components take
local-use numbers 250 and 251 in the moisture category, which no centre this
pipeline reads from uses.

The precipitation type is derived the same way: the encoder reads pgrb2's
four 0/1 categorical flags — `crain`, `cfrzr`, `cicep` and `csnow`, its
local numbers 0 / 1 / 192–195 on the ground surface — and writes one
categorical field under WMO code table 4.201's own values (0 none, 1 rain,
3 freezing rain, 5 snow, 8 ice pellets). The four are mutually exclusive in
the model's own output, so a chart of `ptype` never has to resolve a real
disagreement; where two were set anyway, the order rain, freezing rain,
ice pellets, snow decides, each later flag overriding, and both encoders
share that order so the plane stays byte-identical. The four flags are
input-only and reach no bundle.

### Band, Producer and Aerosol

A `parameter` block names a quantity on a surface, which is a satellite
image's whole identity only together with the channel it was taken in:
brightness temperature at the top of the atmosphere (0 / 4 / 4 on surface
8) is one parameter for every infrared channel of every imager, and a
composite computed from several channels has no GRIB2 parameter at all. Nor
is it an aerosol product's whole identity: aerosol optical thickness
(0 / 20 / 102 on the entire atmosphere) is one parameter for every species
at every wavelength, and GRIB2 puts what tells them apart in the product
definition template rather than the parameter. Three optional blocks may
sit beside `parameter` on a variable, at the same level as it and never
inside it, to carry what the parameter does not:

- `band`: the spectral band and the instrument that measured it, in the
  fields of GRIB2 product definition templates 4.31 and 4.32 for one
  contributing band (the templates' band count is 1 and is not written);
- `producer`: the algorithm that derived the field from other variables;
- `aerosol`: the aerosol type and the size and wavelength intervals of an
  aerosol product, in the fields of GRIB2 product definition template 4.48
  (of which 4.44 and 4.46 are subsets).

```json
{
  "numericId": 1,
  "id": "ir104",
  "label": "Brightness temperature, 10.4 µm",
  "unit": "K",
  "parameter": {
    "discipline": 0, "parameterCategory": 4, "parameterNumber": 4,
    "typeOfFirstFixedSurface": 8,
    "scaleFactorOfFirstFixedSurface": null, "scaledValueOfFirstFixedSurface": null
  },
  "band": {
    "satelliteSeries": 0,
    "satelliteNumber": 174,
    "instrumentType": 297,
    "scaleFactorOfCentralWaveNumber": 0,
    "scaledValueOfCentralWaveNumber": 96086
  },
  "quantization": { "type": "linear", "offset": 180.0, "scale": 0.6,
                    "minimumCode": 0, "maximumCode": 253, "nodataCode": 255 }
}
```

| `band` field | GRIB2 origin | Rule |
|---|---|---|
| `satelliteSeries` | Template 4.31, Common Code Table C-5 | 0–65535 |
| `satelliteNumber` | Template 4.31, Common Code Table C-5 | 0–65535 |
| `instrumentType` | Template 4.31, Common Code Table C-8 | 0–65535 |
| `scaleFactorOfCentralWaveNumber` | Template 4.31 | −127…127 |
| `scaledValueOfCentralWaveNumber` | Template 4.31 | 0…4294967294 |

The central wave number is
`scaledValueOfCentralWaveNumber × 10^−scaleFactorOfCentralWaveNumber` in
m⁻¹. All five keys are required when the block is present and no other key
is allowed. `parameter` stays the identity a reader keys on; `band` says
which channel of which instrument measured it, and a chart-aware reader
classifies a channel by its central wave number (an infrared window, a
water vapour band), not by the spacecraft: the satellite succeeding at an
orbital slot changes `satelliteNumber` and nothing else.

| `producer` field | Rule |
|---|---|
| `id` | `^[a-z][a-z0-9]*$`, the algorithm's name |
| `version` | a non-empty string, the algorithm's version as it names it |

Both keys are required when the block is present and no other key is
allowed. A produced field's `parameter` uses GRIB2's local-use ranges
(category 192 and above in the discipline that fits); a reader that charts
such fields tells them apart by `producer.id` and the parameter number
together, since local numbers mean nothing across producers.

```json
{
  "numericId": 1,
  "id": "aoddust",
  "label": "Dust aerosol optical depth at 550 nm",
  "unit": "1",
  "parameter": {
    "discipline": 0, "parameterCategory": 20, "parameterNumber": 102,
    "typeOfFirstFixedSurface": 10,
    "scaleFactorOfFirstFixedSurface": null, "scaledValueOfFirstFixedSurface": null
  },
  "aerosol": {
    "aerosolType": 62001,
    "typeOfSizeInterval": 0,
    "scaleFactorOfFirstSize": 6, "scaledValueOfFirstSize": 20,
    "scaleFactorOfSecondSize": 0, "scaledValueOfSecondSize": 0,
    "typeOfWavelengthInterval": 7,
    "scaleFactorOfFirstWavelength": 9, "scaledValueOfFirstWavelength": 545,
    "scaleFactorOfSecondWavelength": 9, "scaledValueOfSecondWavelength": 565
  },
  "quantization": { "type": "log1p", "trace": 0.005, "scale": 0.05, "maximum": 5.0,
                    "minimumCode": 1, "maximumCode": 253, "zeroCode": 0,
                    "overflowCode": 254, "nodataCode": 255 }
}
```

| `aerosol` field | GRIB2 origin | Rule |
|---|---|---|
| `aerosolType` | Template 4.48 octets 12–13, code table 4.233 | 0–65535 |
| `typeOfSizeInterval` | Octet 14, code table 4.91 | 0–255 |
| `scaleFactorOfFirstSize` / `scaledValueOfFirstSize` | Octets 15 / 16–19 | −127…127 / 0…4294967294, or both `null` |
| `scaleFactorOfSecondSize` / `scaledValueOfSecondSize` | Octets 20 / 21–24 | as above |
| `typeOfWavelengthInterval` | Octet 25, code table 4.91 | 0–255 |
| `scaleFactorOfFirstWavelength` / `scaledValueOfFirstWavelength` | Octets 26 / 27–30 | as above |
| `scaleFactorOfSecondWavelength` / `scaledValueOfSecondWavelength` | Octets 31 / 32–35 | as above |

All eleven keys are required when the block is present and no other key is
allowed. A limit is `scaledValue × 10^−scaleFactor` metres, and each of the
four is a pair whose two halves are both integers or both `null`, the way
the parameter's fixed surface value is. An interval whose type is 255
(missing) carries no limits: its two pairs are `null`, whatever a record
wrote in the octets (NCEP writes zeros there for a particulate matter
record, and they never reach the file); an interval of any other type
carries all four limits as integers. `parameter` stays the identity a
reader keys on together with this block: two aerosol variables are the same
field exactly when both blocks agree, and a reader that charts such fields
recognizes a species by `aerosolType` and a band by the wavelength interval.
A record is accepted under a registered aerosol variable only when its
template carries this whole identity — the encoder compares every field —
and an aerosol variable takes no aliases or alternates.

Each block is optional and any may appear without the others; a file that
carries none is unchanged from before they were defined. All three are
valid only from schemaVersion 3, like `parameter`, and none raises the
version a file must declare: a version 3 reader that predates them reads
the keys it knows on a variable object and ignores the rest, which is what
lets them widen version 3 rather than open a version 4. A block that is
present must be well formed; a reader that implements this section rejects
a malformed one (see Error Handling). A block is present or absent, never
`null`, so a variable has exactly one encoding.

### Ensemble

A `parameter` block names a quantity; a field computed from an ensemble of
forecasts of that quantity is not the quantity itself but a statistic of it
— the members' mean, their spread, a percentile of their distribution, or
the share of members past a threshold — and GRIB2 says which in the product
definition template (4.2 and 4.12 for a derived forecast, 4.6 and 4.10 for a
percentile, 4.5 and 4.9 for a probability), not in the parameter. The
optional `ensemble` block, beside `parameter` like the three above, carries
it. A variable without the block is a single forecast (or an observation).

The block has exactly one of three shapes; which keys it carries says
which:

```jsonc
"ensemble": { "derivedForecast": 4 }        // templates 4.2 / 4.12
"ensemble": { "percentileValue": 75 }       // templates 4.6 / 4.10
"ensemble": {                               // templates 4.5 / 4.9
  "probabilityType": 3,
  "scaleFactorOfLowerLimit": 2, "scaledValueOfLowerLimit": 1000,
  "scaleFactorOfUpperLimit": null, "scaledValueOfUpperLimit": null
}
```

| `ensemble` field | GRIB2 origin | Rule |
|---|---|---|
| `derivedForecast` | Templates 4.2 / 4.12, code table 4.7 | 0–255 (0 is the unweighted mean of all members, 4 their spread) |
| `percentileValue` | Templates 4.6 / 4.10 | 0–100 |
| `probabilityType` | Templates 4.5 / 4.9, code table 4.9 | 0–5 |
| `scaleFactorOfLowerLimit` / `scaledValueOfLowerLimit` | Templates 4.5 / 4.9 | −127…127 / 0…4294967294, or both `null` |
| `scaleFactorOfUpperLimit` / `scaledValueOfUpperLimit` | Templates 4.5 / 4.9 | as above |

A derived forecast carries `derivedForecast` alone and a percentile
`percentileValue` alone. A probability carries all five of its keys, and
which limits are integers is fixed by its type, whatever a record wrote in
the octets the type does not use (CMA writes the threshold into both):
types 0 (below the lower limit), 3 (above the lower limit) and 5 (equal to
the lower limit) carry the lower limit and a `null` upper one; types 1
(above the upper limit) and 4 (below the upper limit) the upper limit and a
`null` lower one; type 2 (between the limits) both. A limit is
`scaledValue × 10^−scaleFactor` in the GRIB2 unit of the quantity the
`parameter` names (kg m⁻² for a precipitation total, m s⁻¹ for a speed),
never the variable's `unit`; each limit is a pair whose two halves are both
integers or both `null`, as for the fixed surface value.

A probability's `unit` is `%` and its values the percentage of members past
the limit; its `parameter` is the quantity the limit is on, a total over an
interval carrying `typeOfStatisticalProcessing` 1. A percentile, a mean and
a spread keep the quantity's unit. `parameter` stays the identity a reader
keys on together with this block: two variables are the same field exactly
when both agree, so a reader that charts the quantity can draw an ensemble
member's percentile or mean beside a single forecast of it, and tells a
spread or a probability from the quantity by the block.

`ensemble` is optional, valid only from schemaVersion 3, and does not raise
the version a file must declare, on the same terms as `band`, `producer` and
`aerosol` above. A version 3 reader that predates it would draw a spread or
a probability as the quantity itself, so a shell that charts it is deployed
before the first file carrying one is published (see
[`contribution/delivery.md`](contribution/delivery.md)).

The ensemble fields this pipeline publishes (CMA GRAPES GEPS, `cmageps`)
are below. Its central fields keep the ids of the single-forecast fields
they stand in for (`tmp2m`, `prate`, `prmsl`, …) and carry the block that
says which statistic they are: `{"percentileValue": 50}`, or
`{"derivedForecast": 0}` for the 500 hPa height and the sea-level pressure,
where the mean is the only statistic CMA exchanges.

| `id` | Parameter, surface | `ensemble` | Notes |
|---|---|---|---|
| `<id>q25` / `<id>q75` for `tmp2m`, `prate`, `gust`, `tcdc`, `cape`, `tmp850` | as `<id>` | `percentileValue` 25 / 75 | The quartiles of the members' distribution, in the quantity's unit and codebook; `prateq25` / `prateq75` are the six-hour totals' quartiles divided by six, as `prate` is the median's |
| `prmslsd` / `hgt500sd` | as `prmsl` / `hgt500` | `derivedForecast` 4 | The members' spread, in hPa / m |
| `apcp24gt1`, `apcp24gt5`, `apcp24gt10`, `apcp24gt25`, `apcp24gt50`, `apcp24gt100` | 0 / 1 / 8, 1, 0, `typeOfStatisticalProcessing` 1 | `probabilityType` 3, the lower limit 1 / 5 / 10 / 25 / 50 / 100 kg m⁻² | The percentage of members whose precipitation over the 24 hours ending at the frame exceeds the limit; from the 24th hour |

### Time Axis

The axis is a list of integer frame offsets from `runTime`, each worth
`unitSeconds` seconds. The frame at offset `o` is valid at
`runTime + o × unitSeconds`; a frame's position on the axis is its index
in a store's time dimension, and `o` is what a container index's
`frameOffset` field carries.

From schemaVersion 3 the block is:

- `unitSeconds`: seconds per offset unit. It must be a whole divisor of
  3600, and it must be the coarsest unit that expresses every offset
  exactly, so an axis has one encoding. Formally,
  `gcd(3600 / unitSeconds, offset₀, offset₁, …)` must be 1. Every forecast
  source is hourly and declares 3600, which leaves its offsets equal to its
  forecast hours; the radar mosaic declares 360. A long axis is no different
  in kind: the CFSv2 seasonal run reaches hour 6552 in 1092 six-hourly
  frames, still in hourly units with `frameStep: 6`, well inside the
  `[0, 65534]` range an offset has.
- `firstFrameOffset` and `frameCount`: always present.
- Exactly one of `frameStep` (the axis is uniform, and frame `i` is at
  `firstFrameOffset + i × frameStep`) and `frameOffsets` (the axis does not
  hold one step throughout and is listed outright). A forecast run lists
  its offsets because the source changes cadence partway; an observation
  series lists them because the archive has gaps where a publication was
  missed.

No other field may appear in the block.

A mixed axis, hourly then three-hourly:

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
must be in `[0, 65534]`: the `frameOffset` field is a u16 and 65535 is the
`dependencyOffset` sentinel. The same upper bound applies to a uniform
axis's last offset, `firstFrameOffset + (frameCount − 1) × frameStep`. A
`frameOffsets` array whose steps are all equal is invalid, since a uniform
axis has exactly one encoding, `frameStep`; this also rules out arrays of
fewer than three elements. Declaring both `frameStep` and `frameOffsets`,
or neither, makes the file invalid.

Schema versions 1 and 2 describe the same thing in whole hours only:
`firstForecastHour` plus one of `stepHours` (uniform, version 1) and `hours`
(listed, version 2), with `unitSeconds` implicitly 3600. Those files remain
valid and are read unchanged. The two shapes never mix: a version 1 or 2
block carrying any version 3 field, or a version 3 block carrying any of the
hour-named fields, is invalid.

A segment is a maximal run of frames with a constant step. For frames
`h[0] < h[1] < … < h[n-1]` with steps `d[i] = h[i+1] - h[i]`, a segment
boundary falls between `h[i]` and `h[i+1]` exactly where `d[i] != d[i-1]`:
the last frame of the old cadence closes the earlier segment and the first
frame of the new cadence opens the next. A uniform axis is one segment. The
GFS 240-hour axis is two: 121 hourly frames f000–f120, then 40 three-hourly
frames f123–f240.

Segments are derived, never stored. They bound temporal grouping (see
Temporal Prediction), and they are where the physical meaning of a derived
field changes: past a step increase, a precipitation rate obtained by
de-accumulating or de-averaging its source records is a mean over a longer
window, so the field is smoother and its peaks lower on the far side of the
boundary. That is a property of the source data; a renderer that labels
units should not present the two segments as the same measurement.

A field that does not vary in time — a model's own terrain height — is an
ordinary bundle whose axis holds a single frame: `frameCount` 1 with
`frameStep` 1 on the declared unit. Nothing in a bundle marks it as
static; a producer that knows one (Xue's registry does, the variable
descriptor's `static` flag is the fetcher's and the converter's, not the
file's) simply fetches and encodes the plane once rather than at every lead
time. A reader draws it like any one-frame field, and a viewer that offers
playback has nothing to scrub.

The encoder rotates longitude columns so the first column is `-180`,
preserving north-to-south row order. Grids that natively start at Greenwich
(the GFS surface-flux Gaussian grid) are rolled by the encoder into the same
`-180`-first layout. A renderer applies inverse Web Mercator, converts
longitude and latitude to grid coordinates, and samples this layout
directly.
The grid's `layout`, `rowOrder` and `columnOrder` (`row-major`,
`north-to-south`, `west-to-east`, each the only value allowed) spell this
order out; every encoder writes them, and a reader accepts a document
without them, since the schema names them without requiring them.

### Regional Grids

A grid need not cover the world. The `grid` block says what a file covers,
and a regional file is an ordinary file whose origin and extent name a
window rather than the globe; the layout and every codebook are
unchanged. The reference pipeline publishes such files for its
historical showcase cases, cut out of a global run.

Two rules follow for readers, both of which a global grid satisfies
trivially:

- `wrapLongitude` is true only when `width x longitudeStep` is 360 degrees.
  A reader must take the horizontal wrap from this field, not from the
  model: sampling filters may wrap across the antimeridian only when it is
  true, and must clamp otherwise.
- The grid coordinate of a longitude is
  `(longitude - firstLongitude) mod 360 / longitudeStep`. Taking the offset
  modulo 360 keeps a window that crosses the antimeridian contiguous: such a
  window declares a `firstLongitude` near +180 and runs past it.
  Coordinates outside `[0, width)` or `[0, height)` are outside the file; a
  renderer must draw nothing there rather than clamp.

The four regular fields are a regular grid's. A grid carries them or a
`projection` block (*[Projected Sources](#projected-sources)*), never both and
never neither.

#### Gaussian rows

A regular Gaussian grid is regular in longitude only: its latitudes are the
roots of a Legendre polynomial. The regular fields describe it as evenly spaced
as GDAL reports it (within 0.014 of a row of the roots on `GFS-SFLUX`, whose step
spans its first row to its last; on `CFSv2`, at GDAL's 180°/190 from the first
root, drifting to half a row by the southern edge), the optional `gaussian` block
beside them says what its rows really are, and it raises no version floor:

```json
"grid": {
  "width": 3072,
  "height": 1536,
  "firstLongitude": -180.0,
  "firstLatitude": 89.9103,
  "longitudeStep": 0.1171875,
  "latitudeStep": -0.1171469,
  "wrapLongitude": true,
  "gaussian": { "n": 768, "firstRow": 0, "rowStride": 1 }
}
```

- `n` is the number of latitudes per hemisphere (1 to 32768); row `r` of the
  file is row `firstRow + r × rowStride` of the Gaussian grid's `2n`, counted
  from the north. A crop raises `firstRow`, a reduced-resolution tier
  `rowStride`. The last row must lie on the Gaussian grid. The block has
  exactly these three integer fields and needs schemaVersion 3.
- A renderer keeps sampling by the regular fields, and a reader that ignores
  the block draws what it always drew. The true latitudes are for what names a place: a point read's
  nearest row and the cell centre it reports, and a store's `latitude`
  coordinate. Past the edge of a crop a point belongs to the edge row for half
  a row's spacing; the first and last rows of the whole Gaussian grid own the
  polar caps.
- The latitudes are computed, never stored: `docs/zarr-profile.md`,
  "Coordinates", gives the iteration.

### Projected Sources

A model computed on a map projection is published on its own grid. The `grid`
block declares the projection in place of the regular fields, and the file is
schemaVersion 4:

```json
"grid": {
  "width": 1799,
  "height": 1059,
  "layout": "row-major",
  "rowOrder": "north-to-south",
  "columnOrder": "west-to-east",
  "wrapLongitude": false,
  "projection": {
    "kind": "lambert-conformal",
    "radius": 6371229.0,
    "standardParallel1": 38.5,
    "standardParallel2": 38.5,
    "latitudeOfOrigin": 38.5,
    "centralMeridian": -97.5,
    "x0": -2697520.143,
    "y0": 1586693.847,
    "dx": 3000.0,
    "dy": -3000.0
  }
}
```

- `kind` is a closed set, today `lambert-conformal` alone. A reader refuses a
  kind it does not implement; a new kind is a new value, not a new field.
- `x0` and `y0` are the centre of the first cell, the north-west one, in
  projected metres. Cell (`row`, `column`) has its centre at
  `x0 + column × dx`, `y0 + row × dy`. `dx` is positive, `dy` negative, and
  `wrapLongitude` false. Since GDAL's geotransform names the first cell's
  outer corner and different GDAL builds differ in its last digits, an encoder
  rounds the corner to three decimal places and adds half a step:
  `x0 = round(gt0, 3) + dx / 2`, `y0 = round(gt3, 3) + dy / 2`.
- A crop adds `column × dx` and `row × dy` to `x0` and `y0`. A
  reduced-resolution tier doubles `dx` and `dy` and keeps `x0` and `y0`, with
  `width = (width + 1) // 2` and likewise `height`.
- The formulas are the spherical Lambert conformal conic's (Snyder 1987, §15):
  with two equal standard parallels the cone constant is `n = sin φ1`, otherwise
  `n = ln(cos φ1 / cos φ2) / ln(tan(π/4 + φ2/2) / tan(π/4 + φ1/2))`. A longitude
  is taken to within 180° of `centralMeridian` before it is projected. Both
  standard parallels and `latitudeOfOrigin` lie strictly between the poles, and
  the two parallels must not sum to zero, which leaves no cone.
- A grid coordinate is `(x − x0) / dx`, `(y − y0) / dy`, with the rule for
  outside coordinates of *[Regional Grids](#regional-grids)*. Cells are not
  regular in longitude or latitude: a reader that maps between them and
  geography goes through the projection.
- Vector components are east and north, on this grid as on every other. A model
  writes its winds along the grid's axes (GRIB2 resolution and component flags
  `0x08`), and an encoder turns each pair before quantizing it:
  `u_e = cos α × u + sin α × v`, `v_e = −sin α × u + cos α × v`, with `α` the
  cone constant times the longitude's distance from `centralMeridian`. The
  speed is the model's. *docs/encoder.md*, "Native grids", gives the arithmetic.

Older files do something else, and a reader keeps reading them. Up to
schemaVersion 3 a projected model was resampled by the encoder onto a regular
latitude/longitude grid before anything else read it, and the file is an
ordinary regional file of that grid. Its target is fixed: the regular grid of
the source's declared step whose origin is a whole multiple of the step and
which covers the extremes of longitude and latitude the source's boundary cell
centers reach; each target cell center is projected with the source's own
forward formulas and the source plane is bilinearly interpolated there. A conic
domain is a trapezoid on that rectangle. The corners it never covered are
neither gaps nor a reserved code (the format has neither) but the nearest source
cell continued outwards (the sampling coordinate is clamped to the source grid),
so the plane is complete and no filter or contour meets an edge. Those cells are
not a forecast: a reader that knows the source's projection clips to its
footprint, and one that does not draws the extension. A reader tells the two apart by the
`grid` block alone, never by the source: one `model` publishes both.

`model` and `product` identify the source dataset. Every encoder writes
`product`, and a reader accepts a document without it: the schema names
the field without requiring it. Registered pairs:

| `model` | `product` | Grid | Steps published | Notes |
|---|---|---|---|---|
| `GFS` | `pgrb2.0p25` | 1440 × 721, 0.25° | 1 h to f120, 3 h to f240 | All series include the analysis frame (f000). `prate` is an instantaneous rate at every step |
| `ECMWF` | `ifs-0p25` | 1440 × 721, 0.25° | 3 h to 144 h, 6 h to 240 h | `prate` is de-accumulated from the run-total `tp`, so its series has no analysis frame and starts at `firstFrameOffset: 3`; so does `gust`, whose record (the maximum over the interval ending at the frame) is empty at the analysis. Its wave fields come from the cycle's `wave` stream |
| `GFS-SFLUX` | `sfluxgrb` | 3072 × 1536 Gaussian, ~13 km | 1 h to f120, 3 h to f240 | `prate` is de-averaged from window-cumulative records and starts at `firstFrameOffset: 1`; the only source shipping `dswrf` |
| `HRRR` | `wrfsfc` | 1799 × 1059, 3 km Lambert conformal, regional (see "Projected Sources" above) | 1 h to f18 | A new cycle every hour. Runs published before schemaVersion 4 are on a regular 2441 × 1051 grid at 0.03° (134.10 W – 60.90 W, 52.62 N – 21.12 N), resampled by the encoder. Its `prmsl` is the MAPS reduction and its `cref` the model's forecast reflectivity |
| `RRFS` | `2dfld.3km.conus` | 1799 × 1059, 3 km Lambert conformal, regional: HRRR's grid | 1 h to f84 | The 00/06/12/18Z cycles. Isobaric records come from the `prslev` file of each hour. Carries the near-surface smoke and dust of RRFS-SD |
| `CFSv2` | `time-grib-01` | 384 × 190 Gaussian, 0.9375° (T126) | 6 h from f6 to f6552 | The seasonal source: ensemble member 01 of the 00Z and 12Z cycles, thirty-nine weeks of a nine-month run. Its series begin at the first step, so every bundle starts at `firstFrameOffset: 6` — the cycle's analysis is in another file family and is not published. `prate`, `dswrf` and `tcdc` are means over the six hours ending at the frame (`typeOfStatisticalProcessing: 0`) although the records carry the instantaneous template |
| `CMA-RADAR` | `l3-mst-cref` | tile grid, 360/(256·2^z) degrees | 6 min, as published | Observations, not a forecast: `runTime` is the first observation and offsets count from it. The only one whose `unitSeconds` is not 3600; the axis lists its offsets wherever a publication was missed |

How far a run is published is a pipeline choice; the steps above are what
each source makes available. A uniform series declares a `frameStep`; a
series that runs to 240 hours crosses a step change on every forecast source
above and lists its offsets; an observation series lists them wherever the
archive has a gap.

`CMA-RADAR` is a series of observed analyses, and the metadata describes it
with no change: `runTime` is the first observation in the series and each
frame's `frameOffset` counts six-minute units from it. A reader that labels
the axis should take that from the dataset identity rather than assume every
file is a forecast.

Time axes may differ between bundles of one run, in both step and extent.
The layout is identical for every model; only the metadata identity, the
grid, and the time axis differ. Readers must derive the frame
list from `time` and the grid from `grid`, never from the model name, and
must not assume two bundles of one run share an axis.


## Quantization Codebooks

### Linear (`"type": "linear"`)

```text
q = round((clamp(x, offset, offset + scale * maximumCode) - offset) / scale)
x = offset + q * scale
```

Codes `0` through `maximumCode` are valid; `nodataCode` marks missing data;
codes between `maximumCode + 1` and `nodataCode - 1` are reserved. The
maximum in-range quantization error is `scale / 2`.

Registered linear codebooks (the `quality` profile; `balanced` uses the same
values unless noted):

| Variable | offset | scale | maximumCode | nodataCode | Error budget |
|---|---:|---:|---:|---:|---|
| `tmp2m` | −60 °C | 0.5 | 220 | 255 | 0.25 °C |
| `ugrd10m` / `vgrd10m` / `ugrd100m` / `vgrd100m` | −63.5 m/s | 0.5 | 254 | 255 | 0.25 m/s |
| `dswrf` | 0 W/m² | 5 | 254 | 255 | 2.5 W/m² |
| `cref` | 0 dBZ | 0.5 | 160 | 255 | 0.25 dB |
| `rqi` | −0.1 | 0.1 | 11 | 255 | none: MRMS publishes tenths; code 0 is outside radar coverage, code 1 an index of 0 (every profile) |
| `gust` | 0 m/s | 0.5 | 254 | 255 | 0.25 m/s |
| `tcdc` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `cape` | 0 J/kg | 25 | 254 | 255 | 12.5 J/kg |
| `cin` | −1016 J/kg | 4 | 254 | 255 | 2 J/kg |
| `lcdc` / `mcdc` / `hcdc` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `pwat` | 0 kg/m² | 0.5 | 254 | 255 | 0.25 kg/m² |
| `hpbl` | 0 m | 20 | 254 | 255 | 10 m |
| `orog` | −430 m | 37 | 254 | 255 | 18.5 m |
| `ptype` | 0 | 1 | 8 | 255 | — |
| `vis` | 0 km | 0.1 | 254 | 255 | 0.05 km |
| `dpt2m` | −70 °C | 0.5 | 220 | 255 | 0.25 °C |
| `aptmp2m` | −90 °C | 1 | 150 | 255 | 0.5 °C |
| `tmpsfc` | −60 °C | 0.5 | 254 | 255 | 0.25 °C |
| `icec` | 0 % | 0.5 | 200 | 255 | 0.25 % |
| `icetk` | 0 m | 0.02 | 254 | 255 | 0.01 m |
| `htsgw` | 0 m | 0.1 | 254 | 255 | 0.05 m |
| `perpw` | 0 s | 0.1 | 254 | 255 | 0.05 s |
| `wvhgt` / `swhgt` | 0 m | 0.1 | 254 | 255 | 0.05 m |
| `wvper` / `swper` | 0 s | 0.1 | 254 | 255 | 0.05 s |
| `dirpw` | 0° | 1.5 | 239 | 255 | 0.75° |
| `wvdir` / `swdir` | 0° | 1.5 | 239 | 255 | 0.75° |
| `uwave` / `vwave` | −25.4 m | 0.2 | 254 | 255 | 0.1 m |
| `uwindwave` / `vwindwave`, `uswell` / `vswell` | −25.4 m | 0.2 | 254 | 255 | 0.1 m |
| `tsoil<n>` | −61 °C | 0.5 | 254 | 255 | 0.25 °C; code 0 is no soil |
| `soilw<n>` | −0.01 m³/m³ | 0.005 | 202 | 255 | 0.0025 m³/m³; code 0 is no soil |
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
| `cat<level>` | 0 m^(2/3)/s | 0.005 | 127 | 255 | 0.0025 m^(2/3)/s |

The `compact` profile doubles each `scale` (temperature 1.0 → maximumCode
110, wind 1.0 → 127, dswrf 10 → 127, cref 1.0 → 80, gust 1.0 → 127, every
cloud cover 1.0 → 100, cape 50 → 127, cin 8 → 127, vis 0.2 → 127, dpt2m 1.0
→ 110, aptmp2m 2 → 75, pwat 1.0 → 127, hpbl 40 → 127, orog 74 → 127, tmpsfc 1.0 → 127, icec
1.0 → 100, icetk 0.04 → 127, htsgw, perpw and the wind-wave and swell heights and periods 0.2 → 127, tsoil 1.0 → 127,
soilw 0.01 → 101, and every
pressure-family and isobaric codebook → half its maximumCode over the same
range; `cat<level>` 0.01 over 0–0.64 → 64). Two codebooks do not keep the same range: `dirpw`'s compact codebook (and `wvdir`'s and `swdir`'s)
stops at 357° (3 → 119), because 360 / 3
codes would put 360°, which is 0°, back on the grid; and the wave vectors'
stop at ±25.2 m (0.4 → 126) so that 0 stays on the grid, since land is
(0, 0) in the pair and the middle code of both. The categorical `ptype`
takes one book in every profile: its integer class codes have no resolution
to trade, and the codes between the four types GFS reports are never
written. `balanced` takes the compact
`icec` beside the compact humidity and cloud cover, and the quality book for
every other one-sided field (the inhibition, the precipitable water, the
boundary layer height), as it does for the aerosol fields.

The wave fields are the first whose records do not cover the grid: GFS-Wave
carries a bitmap, and land comes out of GDAL as its nodata value (9999).
The encoder maps those points to code 0 (0 m, 0 s, 0°) before quantization,
as the radar mosaic's fill is handled (see "Complete planes" below); the `nodataCode` is still never written.
The soil layers' records do not cover it either (GFS masks the open water,
and the sea ice for the moisture; AIFS the sea for the moisture). Their
codebooks start one step below the least real value — −61 °C, −0.01 m³/m³ —
so the masked points, filled with that bottom, are code 0 and no real value
is: a reader tells "no soil" from a value without a bitmap, and 0 °C and
0 m³/m³ are codes of their own in both profiles.

The isobaric temperature takes its range per level: the low end holds the
Antarctic winter at every surface, the high end the below-ground
extrapolation the lowest surfaces take under high terrain, and no single
127-degree window covers both 850 hPa in summer and 200 hPa in winter.
Specific humidity spans two orders of magnitude between the surface and the
upper troposphere, so its step follows the level. Relative humidity is the
noisiest field published, so the `balanced` profile
takes its `compact` codebook (1 %, maximumCode 100), and the cloud covers
follow it; those are the departures from `quality` in that profile besides
precipitation. The equivalent potential temperature takes a 127 K window per
level at the temperature's step, placed so the tropical end fits (850 hPa
runs to 357 K); only the 850 hPa window is verified against an analysis,
the others follow the potential temperature's rise with height. None of the
isobaric fills is contoured, so none carries the half-code rule below.

The pressure family's offsets are chosen so that every standard contour
value lands exactly half a code off: `(contour − offset) / scale` has
fractional part 0.5, for the level's own interval (4 hPa for `prmsl`, 30 m
up to 700 hPa, 40 m at 500, 120 m above) and for the emphasised lines
(every 20 hPa on `prmsl`; 5840 and 5880 gpm on `hgt500`). This is an
encoder rule, in the same sense as a container's segment grouping: a
decoder must not assume it, and nothing in a bundle records it. It exists because a
contour drawn where the code is flat covers a whole plateau instead of a
line. The coverage is fixed per variable for all time rather than fitted per
run; byte-identical re-encoding, a stable legend, and comparable point
series across runs depend on it.

`cref` code 0 is both "no echo" and "outside the radar network's coverage".
A reader that must tell them apart reads the mosaic's `rqi` beside it,
whose code 0 is outside coverage and whose code 1 is covered with an index
of 0.
A ground mosaic is a regional product on a rectangular grid, and a bundle
carries no bitmap, so the bottom of the codebook is what a
renderer paints as nothing. It is an ordinary in-range code rather than a
reserved one: a renderer that interpolates codes before its palette lookup
would otherwise colour the gap between a reserved code and its neighbour
with a class the data never reached.

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
one codebook step above the maximum (the same formula evaluated at
`q = maximumCode + 1`), extending the logarithmic grid by one step so
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


## Residuals

A predicted variable stores each frame after the first of its run of
frames as a residual against an earlier one (a store's `xue.delta` codec,
a container's predictors).

Residuals are defined unconditionally as one-byte modulo-256 wrapping
differences:

```text
residual = (current - base) mod 256
current  = (residual + base) mod 256
```

Wrapping subtraction and addition are lossless for every byte pair, so there
is no residual range check, no signed interpretation, and no RAW fallback
for out-of-range differences.

## Complete planes

A bundle requires complete input planes: every point of every plane carries a
code. The forecast grids satisfy that outright, and a source whose product
does not cover its whole grid (the radar mosaic) resolves it before
quantization by mapping absent points to the bottom of the variable's
codebook, a value rather than a gap. A future missing-data implementation
should add a separate bitmap so residual bytes never conflict with the
nodata code.

## Bundle naming

One bundle per variable per run (`tmp2m`, `prate`, `dswrf`, `cref`), a
vector's components together (`wind10m`), and reduced-resolution renditions
`<bundle>.half`, `<bundle>.quarter` and `<bundle>.eighth` (decimated by 2,
4 and 8; which rungs a source ships is a registry value, every source at
least the half, none the eighth today, though published ones stay valid),
structurally identical bundles whose metadata declares the decimated grid.
A shared store holds every bundle of one tier (`bundles.zarr`,
`bundles.half.zarr`; [`zarr-profile.md`](zarr-profile.md), "Layout"); a
one-bundle store is `<name>.zarr`, a container `<name>.xue`.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).
