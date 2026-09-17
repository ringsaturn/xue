# Geostationary satellite imagery

How a geostationary imager's channels become Xue bundles: the `himawari`
source today, its Dust RGB composite, and the seams a second satellite, a
second channel and another composite product go through. The bundle
format is unchanged (`format.md`); what this document fixes is the fetch
stage, `xuebuild/satellite/`, and the metadata a satellite variable
carries.

## Shape

A satellite source is a `series_file` observation source, the shape of
the JMA nowcast and the CMA mosaic: the fetch stage writes one NetCDF
series per window on a regular latitude/longitude grid, and the converter
downstream — cropping, quantization, temporal grouping, the container,
the Zarr store, the manifest — is the observation path unchanged in both
encoders. What the fetch stage does is:

1. list the scan's files on the agency's public bucket (a *slot*: one
   scan of one channel);
2. fetch them when the slot is complete (every tile has landed);
3. open them as one GDAL dataset on the geostationary projection;
4. warp that dataset onto the published grid, once, into a cached frame;
5. run each producer on the slot's warped channels, once, into cached
   frames of its own;
6. stack the window's frames into one series per variable.

The geostationary projection is implemented once, in GDAL, on the fetch
side. Neither encoder learns the sweep angle or the ellipsoid; the native
encoder reads the series the Python stage wrote, and
`tests/test_native.py` holds the two byte-identical from there.

## The platform registry

`xuebuild/satellite/platforms.py` is one row per spacecraft at an orbital
slot:

| Field | Himawari-9 |
|---|---|
| `role` (the source id) | `himawari` |
| `spacecraft` | Himawari-9 |
| `satellite_number` (WMO C-5) | 174 |
| `instrument` / `instrument_type` (WMO C-8) | AHI / 297 |
| `sub_longitude` | 140.7 |
| `sweep_axis` | `y` |
| `channels` | the sixteen AHI bands |
| `reader` | `isatss` |
| `bucket` / `prefix` | `noaa-himawari9` / `AHI-L2-FLDK-ISatSS` |
| `tile_count` | 88 |
| `cadence_seconds` | 600 |

A source is named by the **orbital role**, never by the spacecraft: the
pointer (`latest-himawari.json`), the run directories (`himawari.<run>/`)
and the manifest `model` (`HIMAWARI`) outlive a handover. When Himawari-10
takes the slot, the platform row changes (`spacecraft`,
`satellite_number`, `bucket`) and nothing published does; the spacecraft
is in each file, as the `band` block below. The GOES-East and GOES-West
platforms are registered (GOES-19 at 75.2°W, GOES-18 at 137.0°W, ABI) and
unpublished: publishing one is a `SourceSpec`, a reader for the single-file
`CMIPF` product, a workflow and a shell entry.

A **channel** is a bundle. Its id is the nominal wavelength,
instrument-neutral: `ir104` is AHI band 13 and ABI channel 13 alike, an
infrared window at 10.4 µm; the exact central wave number is in the
`band` block. The registry carries every channel the instrument has;
`sources.py` says which a source publishes (`bundle_scalar_ids`) and
carries their `band` blocks (`bands`, from `Platform.bands`).

| id | wavelength | AHI | ABI | quantity | codebook |
|---|---|---|---|---|---|
| `ir104` | 10.4 µm | 13 | 13 | brightness temperature, K (the shell reads it in °C) | 180–331.8 at 0.6 |
| `ir086`, `ir112`, `ir123` | 8.6, 11.2, 12.3 µm | 11, 14, 15 | 11, 14, 15 | brightness temperature; fetched for the Dust RGB, registered variables, not published by any source | 180–331.8 at 0.6 |
| `ir039`, `ir096`, `ir133` | 3.9, 9.6, 13.3 µm | 7, 12, 16 | 7, 12, 16 | brightness temperature | registered on the platform; not yet a variable |
| `wv062`, `wv069`, `wv073` | 6.2, 6.9, 7.3 µm | 8, 9, 10 | 8, 9, 10 | brightness temperature | same |
| `vis064`, `nir086`, `nir161`, … | 0.64, 0.86, 1.61 µm | 3, 4, 5 | 2, 3, 5 | reflectance | same |

Adding a channel is: a `VariableSpec` in `variables.py` and its mirror in
`encode/variables.rs` (parameter block, codebook, label), the id in the
source's `input_variable_ids` and `bundle_scalar_ids`, a regenerated
`satellite-registry.json` (`tests/prepare_satellite_registry.py`), and a
row of the shell's variable table with a palette. Nothing in the fetch
stage, the converter or the format changes: the fetch warps every channel
a source lists, and the ingest reads one series file per variable.

### The Dust RGB

`dustrgb` is the classic Dust RGB (Lensky and Rosenfeld 2008, as
EUMeTrain's recipe compilation and the GOES-R Quick Guide fix it): three
stretches of the infrared windows, no ancillary data, no cloud mask —
lofted mineral dust reads pink to magenta over dark blue-green surfaces,
day and night; thick ice cloud dark red, thin cirrus near black, mid-level
cloud ochre.

| gun | recipe | stretch (AHI: the SEVIRI set) | stretch (ABI: the Quick Guide's) |
|---|---|---|---|
| `dustr` | BT 12.3 − BT 10.4 µm | −4 … +2 K, γ 1 | −6.7 … +2.6 K |
| `dustg` | BT 11.2 − BT 8.6 µm | 0 … +15 K, γ 2.5 | −0.5 … +20 K, γ 2.5 |
| `dustb` | BT 10.4 µm | 261 … 289 K, γ 1 | 261.2 … 288.7 K |

Each gun is a number in 0–1 and the three are one bundle, variables 1, 2
and 3 in that order (`binconvert.COMPOSITE_BUNDLES`,
`SourceSpec.bundle_composite_ids`). A composite has no GRIB2 parameter:
each gun takes a local-use one — discipline 3 (space products), category
192, parameter number 1, 2 or 3, on surface 8 — that means nothing
without the `producer` block beside it, `{"id": "shachen", "version":
…}`. The id is registered on the variable (`VariableSpec.producer_id`,
mirrored in `encode/variables.rs`); the version is whatever ran, stamped
on the series by the fetch stage and copied into the metadata by
whichever encoder converts, so a shachen upgrade changes the files it
produces and no registry.

The codebook is one for every profile: linear, offset −0.004, step
0.004, codes 0–251, so code 1 is 0.0, code 251 is 1.0 and **code 0 —
one step below zero — is "no data"**: a cell the disk does not cover, or
one an input channel lacked, is code 0 in all three guns at once, and
black (0, 0, 0) stays a value at code 1. The shell paints nothing where
any gun is code 0 and otherwise shows the three guns straight as the
pixel's colour; there is no palette. The composite has no poster and no
video companion; it has the half-resolution variant and the Zarr store
(three arrays) like a vector bundle.

## Metadata

A satellite variable's identity is its `parameter` block, brightness
temperature at the nominal top of the atmosphere (GRIB2 0 / 4 / 4 on
surface 8), which every infrared band of every imager shares, plus the
`band` block beside it (`format.md` §"Band and Producer"), template
4.31's five fields:

```json
"band": {
  "satelliteSeries": 0,
  "satelliteNumber": 174,
  "instrumentType": 297,
  "scaleFactorOfCentralWaveNumber": 0,
  "scaledValueOfCentralWaveNumber": 96086
}
```

The shell classifies a channel by the band's central wave number (10–11 µm
is the infrared window, `ir104`), never by the spacecraft, so GOES-19's
10.35 µm channel and Himawari-9's 10.41 µm one draw with one palette. A
brightness temperature with no band, or in a band the shell has no chart
for, renders generically.

Cells the disk never covers arrive as the product's fill (`-32767`) and
become the bottom of the codebook, 180 K — below any cloud top a 10 µm
channel reports — which the shell paints as nothing, the way the radar
mosaics' no-coverage cells are handled. The format carries no bitmap.

## The fetch stage

### Reader

A reader (`readers.py`, `Reader`) lists a platform's slots and a slot's
objects, downloads them, and opens them as **one thing GDAL can open**: a
dataset on the geostationary CRS whose values are the physical quantity or
carry a scale and offset to it. Its responsibility ends there.

`ISatSSReader` is NOAA's ISatSS product: each channel of each scan as 88
Sectorized CMI NetCDF tiles (550 × 550 at 2 km) under
`AHI-L2-FLDK-ISatSS/YYYY/MM/DD/HHMM/`, named
`OR_HFD-<res>-B<bits>-M1C<channel>-T<tile>_GH9_s<start>_c<created>.nc`. The
bit-depth field varies by channel (B11 for most, B12 for channels 10–15,
B14 for channel 7), so the listing prefix spells the channel's own and
never guesses. The tiles are already calibrated (`Sectorized_CMI`, Int16
with `scale_factor` 0.064208984375 and `add_offset` 69, kelvin) on the CF
`geostationary` projection with x/y in microradians; GDAL warns about the
unit and computes the geotransform correctly. A day's slots are the
directory listing (`delimiter=/`), a slot's tiles one listing under the
channel's prefix, a reissued tile the later `c` stamp. The slot opens as a
`gdalbuildvrt` mosaic of the tiles by georeference.

A second reader is a class here: `CMIPFReader` for the GOES `ABI-L2-CMIPF`
product (one 5424² file per channel per slot, `CMI`, sweep x), and an
`HSDReader` for the raw Himawari Standard Data segments should the ISatSS
product ever stop.

### Projector

A projector (`projector.py`, `Projector`) writes the reader's dataset
resampled onto a `TargetGrid` — edges and step, cell centres half a step
inside, longitudes allowed past 180 — as an Int16 GeoTIFF with the
source's nodata, scale and offset on the band. `GdalWarpProjector` is
`gdalwarp -t_srs EPSG:4326 -te … -tr … -r bilinear`, deterministic for one
GDAL version, multithreaded. A NumPy projector with a cached lookup table
(for parallax correction, or exact alignment with a producer's grid) is
the same interface.

The published grid is the platform's region at the source's `grid_step`:
60° either side of the sub-satellite longitude and ±60° of latitude, past
which the view is too oblique to read. For Himawari that is 80.7°E to
200.7°E at 0.04°, 3000 × 3000 cells, first cell centre 80.72°E, 59.98°N —
a grid that crosses the antimeridian, which the encoders' `crop_grid` and
the shell's viewport arithmetic take in the grid's own copy of the world
(a box spelled `-170` is at 190 on it).

### Frames and the cache

A frame is one slot of one variable on the target grid, at
`data/raw/<role>-frames/<variable>/<variable>_<YYYYMMDDHHMMSS>.tif`: a
channel's, warped by the projector, or a produced one, written by
`assemble.write_frame` (Int16 at 1e-4 with the same fill, through an ENVI
header and `gdal_translate`, no raster library). Beside every frame is a
packing sidecar — the scale, offset and unit the frame's codes mean, read
off the source tile before the warp for a channel, and for a produced
frame also the producer's id and version. A slot is warped once and
composed once: the rounds script pulls the window's hours of frames from
the bucket before the first build and pushes new ones after every upload
(`make pull-r2-frames` / `push-r2-frames` / `prune-r2-frames`, `MODEL=himawari`;
the targets take every variable directory), so a round downloads one
slot's tiles (26 MB per channel) and runs the producer on one slot
whatever the window's length. A channel's frame is a pure function of its
tiles and the GDAL version, a produced one of its input frames and the
producer's version (`produce_frames` recomposes a cached slot whose
sidecar names another version); a GDAL upgrade may resample the next
frame differently, and the cached older frames stay as they are, each
under its own `?v=` once published.

### The series

`assemble.write_series` stacks one variable's frames into its series
file of the window, `<stem>.<variable>.nc` (`himawari.2026091703.ir104.nc`,
`….dustr.nc`, …): a VRT over the frames carrying the netCDF driver's
extra-dimension metadata (`NETCDF_DIM_EXTRA={time}`,
`NETCDF_DIM_time_DEF`, `NETCDF_DIM_time_VALUES`, one `NETCDF_DIM_time`
per band, `NETCDF_VARNAME=<variable>`), translated with
`gdal_translate -of netCDF` into `<variable>(time, lat, lon)`, packed
Int16 with the frames' own scale and offset and fill, `time` in seconds
since the run, `units` the registry's (`K`, or `1` for a gun), and on a
produced variable the `producer_id` and `producer_version` attributes
(the band metadata keys of the VRT become the variable's attributes).
One file per variable because the netCDF driver stacks a VRT's bands into
one extra-dimensioned variable only when the bands are exactly that axis;
given two variables' bands it writes one variable per band instead. Every
series of a window carries the same axis: a slot is in the window only
when every channel landed whole, and every producer's outputs exist
exactly when its inputs do. The reference pipeline needs no NetCDF
library; GDAL writes the file, and the same GDAL reads it back through
`xuebuild/observation.py` (`series_files` resolves a run directory to a
file per variable, or to the one file the radar sources write;
`inspect_observation` reads each variable with its own packing and fill,
holds the axes equal, returns the producer stamps), which snaps each time
to its 600 s slot and takes the first slot's hour as the run. The
converter reads only the variables the requested bundles carry: the three
unpublished channels are never quantized.

### Rounds

`fetch.fetch_window` walks every slot from the run's hour through `hours`
past it: a cached frame is read back, a complete slot on the bucket is
fetched and warped, an incomplete or absent slot is left out (the axis
allows the gap, and the next round takes it if it lands). The live
window's end is the newest slot whose tiles have all landed
(`latest_slot`: today's directory listing, then yesterday's; the newest
three slots asked for their tiles, the rest trusted). `resolve_run("latest")`
is the six whole hours (`window_hours`) ending with that slot's hour, as
for MRMS and JMA with their own lengths. The tiles of a scan are generated about eight minutes after it
starts and listed some fifteen after; a round is a listing, one scan's
tiles, one warp, one stack and a conversion, three to four minutes, so the
live window ends fifteen to twenty minutes behind real time.
`.github/workflows/publish-himawari.yml` runs one round per job on a
ten-minute cron and installs `gdal-bin` whichever encoder converts,
since the wheel's GDAL is a library with no GeoTIFF driver.

## Producers

A composite product (a dust index, a split-window difference, an RGB) is
a `Producer` (`producers.py`): its input is one slot's channels, already
warped — float planes in the channel's unit with NaN where no data — plus
the ancillary objects it names, its output one or more new planes on the
same grid, each written as a frame of its own and cached beside the
channels' (`fetch.produce_frames`), so a round composes the slot it
warped and reads the rest back. It never touches quantization, the
container, the store or the manifest: its outputs are stacked into series
like channels and the converter reads them as the components of one
composite bundle. Two kinds share the interface: an in-process NumPy
function with a fixed operation order, which can be held to a golden, and
an external one that runs elsewhere and writes to the shared bucket, read
back the way the CMA archive is. A produced variable's identity is a
local-use GRIB2 parameter with the `producer` block beside it, the id
registered on the variable, the version taken from the frames the
producer wrote.

`DustRGBProducer` is the first: `id` `shachen`, `bundle_id` `dustrgb`,
inputs `ir086`, `ir104`, `ir112`, `ir123`, outputs `dustr`, `dustg`,
`dustb`, no ancillaries, `version` the installed shachen distribution's.
It builds an xarray scene of the four planes and calls
`shachen.dustrgb.dust_rgb` with the stretch set the platform's instrument
takes (`DUST_RGB` for AHI, `DUST_RGB_ABI` for ABI — the rule shachen
applies by satpy reader, made here by `Platform.instrument`), then marks
every cell any input lacked as no data in all three guns. `PRODUCERS` is
keyed by the bundle produced; `SourceSpec.bundle_composite_ids` lists what
a source publishes, and `_fetch_satellite_run` runs every listed producer
whose channels the window fetches. shachen is the `satellite` dependency
group (`uv sync --group satellite`), imported inside the producer and
nowhere else.

## Fixtures and tests

`tests/fixtures/himawari/` is sixteen real tiles (T020 and T021 of two
consecutive scans in bands 11, 13, 14 and 15) and `tests/test_satellite.py`
runs them through the whole stage — listing, completeness, download,
mosaic, warp, cache, the producer (held cell for cell to
`shachen.dustrgb.dust_rgb` called directly), series, ingest, conversion on
the production grid with the composite bundle, a crop past the
antimeridian, a `--bundles` build that reads one series alone — against a
stand-in for the bucket, then through both encoders.
`tests/fixtures/satellite-registry.json` (`variables`: each channel's and
gun's label, unit, parameter block, band block or producer id and
codebooks; `bundles`: each composite's components in order) holds them
identical across the Python encoder, the Rust encoder and the shell;
`tests/prepare_satellite_registry.py` regenerates it.
