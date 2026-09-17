# Geostationary satellite imagery

How a geostationary imager's channels become Xue bundles: the `himawari`,
`goeseast`, `goeswest` and `meteosat` sources, their Dust RGB composite,
and the seams a further satellite, a second channel and another composite
product go through. The bundle format is unchanged (`format.md`); what
this document fixes is the fetch stage, `xuebuild/satellite/`, and the
metadata a satellite variable carries.

## Shape

A satellite source is a `series_file` observation source, the shape of
the JMA nowcast and the CMA mosaic: the fetch stage writes one NetCDF
series per window on a regular latitude/longitude grid, and the converter
downstream — cropping, quantization, temporal grouping, the container,
the Zarr store, the manifest — is the observation path unchanged in both
encoders. What the fetch stage does is:

1. list the scan's files on the agency's public bucket or data store (a
   *slot*: one scan of one channel);
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

| Field | Himawari-9 | GOES-19 | GOES-18 | Meteosat-12 | Meteosat-9 |
|---|---|---|---|---|---|
| `role` (the source id) | `himawari` | `goeseast` | `goeswest` | `meteosat` | `meteosatiodc` |
| `spacecraft` | Himawari-9 | GOES-19 | GOES-18 | Meteosat-12 (MTG-I1) | Meteosat-9 (MSG2) |
| `satellite_number` (WMO C-5) | 174 | 273 | 272 | 71 | 56 |
| `instrument` / `instrument_type` (WMO C-8) | AHI / 297 | ABI / 617 | ABI / 617 | FCI / 210 | SEVIRI / 207 |
| `sub_longitude` | 140.7 | −75.2 | −137.0 | 0.0 | 45.5 |
| `sweep_axis` | `y` | `x` | `x` | `y` | `y` |
| `channels` | the sixteen AHI bands | the sixteen ABI channels | the sixteen ABI channels | the sixteen FCI channels | the twelve SEVIRI channels |
| `reader` | `isatss` | `cmipf` | `cmipf` | `fci` | `seviri` (none) |
| `bucket` / `prefix` | `noaa-himawari9` / `AHI-L2-FLDK-ISatSS` | `noaa-goes19` / `ABI-L2-CMIPF` | `noaa-goes18` / `ABI-L2-CMIPF` | `api.eumetsat.int` / `EO:EUM:DAT:0662` | `api.eumetsat.int` / `EO:EUM:DAT:MSG:HRSEVIRI-IODC` |
| `tile_count` | 88 | 1 | 1 | 40 | 1 |
| `cadence_seconds` (the scans') | 600 | 600 | 600 | 600 | 900 |
| `region` (west … east) | 80.7 … 200.7 | −135.2 … −15.2 | 163 … 283 | −60 … 60 | −14.5 … 105.5 |

A source is named by the **orbital role**, never by the spacecraft: the
pointer (`latest-himawari.json`, `latest-goeseast.json`,
`latest-meteosat.json`), the run directories (`himawari.<run>/`) and the
manifest `model` (`HIMAWARI`, `GOES-EAST`, `GOES-WEST`, `METEOSAT`)
outlive a handover. When Himawari-10 takes the slot, or GOES-19 hands
East to its successor, the platform row changes (`spacecraft`,
`satellite_number`, `bucket`) and nothing published does; the spacecraft
is in each file, as the `band` block below. The `reference_channel`
(`ir104`, which every imager here has) is the one a listing walks to find
a bucket's slots. `bucket` and `prefix` name where the files are in the
terms of the reader: a public bucket and a key prefix, or a data store's
host and a collection id. Meteosat-9 at 45.5°E (the Indian Ocean service)
is a row and nothing more: its SEVIRI Level 1.5 native file has no reader
yet (GDAL's `MSGN` driver gives radiances and the brightness temperatures
would be a Planck fit per channel, as for FCI below), and no source names
it.

`Platform.region` is 60° either side of the sub-satellite longitude and
±60° of latitude. A disk that crosses the antimeridian is spelled with the
west edge inside −180 … 180 and the east edge past 180 — Himawari's 80.7 …
200.7, GOES-West's 163 … 283 — the one shape the encoders' `crop_grid` and
the shell's viewport arithmetic take; GOES-East stays on negative
longitudes like the regional radar grids; Meteosat's −60 … 60 sits on
the prime meridian. The four sources publish the same variables (`ir104`
and `dustrgb`; the ABI composite takes the Quick Guide's stretches, FCI
has no 11.2 µm window and its green gun reads the 10.5 µm one), and every
seam below is shared: what differs between them is the platform row and
the reader — and, for Meteosat, the cadence the source publishes at,
which the data policy sets (§"Rounds").

A **channel** is a bundle. Its id is the nominal wavelength,
instrument-neutral: `ir104` is AHI band 13 and ABI channel 13 alike, an
infrared window at 10.4 µm; the exact central wave number is in the
`band` block. The registry carries every channel the instrument has;
`sources.py` says which a source publishes (`bundle_scalar_ids`) and
carries their `band` blocks (`bands`, from `Platform.bands`).

| id | wavelength | AHI | ABI | FCI | quantity | codebook |
|---|---|---|---|---|---|---|
| `ir104` | 10.4 µm (FCI 10.5) | 13 | 13 | 14 (IR 10.5) | brightness temperature, K (the shell reads it in °C) | 180–331.8 at 0.6 |
| `ir086`, `ir112`, `ir123` | 8.6, 11.2, 12.3 µm (FCI 8.7, —, 12.3) | 11, 14, 15 | 11, 14, 15 | 12, —, 15 | brightness temperature; fetched for the Dust RGB, registered variables, not published by any source | 180–331.8 at 0.6 |
| `ir039`, `ir096`, `ir133` | 3.9, 9.6, 13.3 µm | 7, 12, 16 | 7, 12, 16 | 9, 13, 16 | brightness temperature | registered on the platform; not yet a variable |
| `wv062`, `wv069`, `wv073` | 6.2, 6.9, 7.3 µm | 8, 9, 10 | 8, 9, 10 | 10, —, 11 | brightness temperature | same |
| `vis064`, `nir086`, `nir161`, … | 0.64, 0.86, 1.61 µm | 3, 4, 5 | 2, 3, 5 | 3, 4, 7 | reflectance | same |

An FCI channel takes the id of the window it is nearest to (IR 10.5 is
`ir104`, IR 8.7 `ir086`; the exact wave number is in the band block, and
the shell classifies by it), so the Dust RGB's inputs keep their ids
across the three imagers.

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

| gun | recipe | stretch (AHI and FCI: the SEVIRI set) | stretch (ABI: the Quick Guide's) |
|---|---|---|---|
| `dustr` | BT 12.3 − BT 10.4 µm | −4 … +2 K, γ 1 | −6.7 … +2.6 K |
| `dustg` | BT 11.2 − BT 8.6 µm (FCI: BT 10.5 − BT 8.7, the imager has no 11.2 µm window; the original SEVIRI `IR10.8 − IR8.7`) | 0 … +15 K, γ 2.5 | −0.5 … +20 K, γ 2.5 |
| `dustb` | BT 10.4 µm | 261 … 289 K, γ 1 | 261.2 … 288.7 K |

Which channels a platform feeds the recipe is `Producer.inputs_for`
(`DustRGBProducer`: the four windows, or three where 11.2 µm is missing
and 10.4 µm stands in for it); `bundle_input_ids` asks it for the
source's platform, so a `--bundles dustrgb` build and the workflow fetch
what that imager has.

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

`CMIPFReader` is NOAA's `ABI-L2-CMIPF` product, the GOES-R full disk:
each channel of each scan as **one** netCDF-4 file under
`ABI-L2-CMIPF/YYYY/DDD/HH/` (year, day of year, hour), named
`OR_ABI-L2-CMIPF-M<mode>C<channel>_G<nn>_s<start>_e<end>_c<created>.nc`
with `YYYYDDDHHMMSSs` stamps. `CMI` is unsigned 16-bit, 5424 × 5424 at
2 km for the infrared channels, with the band's own scale and offset to
kelvin (C13: 0.06145332 and 89.62) and 65535 where the disk is not; the
`goes_imager_projection` is the CF `geostationary` mapping with sweep `x`
and x/y in radians, which GDAL turns into a metre geotransform on its own
(its warning about the radian axis unit concerns the SRS's axis, not the
georeference). A scan in mode 6 starts twenty seconds past every ten-minute
mark and its **slot is that mark** (the start floored to the cadence); the
listing accepts any mode digit. A slot is complete once its file is listed
(`tile_count` 1), the file lands some ten minutes after the scan starts,
and a reissue is the later `c` stamp. The slot opens as a one-source VRT
over the `CMI` variable, so the no-data floor is applied the way it is to
a mosaic. Listing is per hour directory: `list_hours` reads a day's hour
prefixes in one request, `list_hour` one hour's files for a channel, and
`recent_slots` walks the hours newest first and stops once it has the few
slots `latest_slot` asks about — two or three requests a round rather
than a whole day's twenty-four. The `DQF` quality flags are not read
(99.9996 % of a sampled disk is flag 0).

`FCIReader` is EUMETSAT's MTG FCI Level 1c full-disk product (FDHSI, the
Data Store collection `EO:EUM:DAT:0662`): one product per ten-minute
repeat cycle, cut into forty netCDF-4 chunk files, each a strip of some
140 rows of the 5568 × 5568 2 km grid carrying **every channel** as
12-bit radiance codes with a scale and offset
(`/data/<channel>/measured/effective_radiance`, mW m⁻² sr⁻¹ (cm⁻¹)⁻¹, fill
65535). Three things stop GDAL opening a chunk as the other readers'
files are opened, and the reader does them itself:

- the chunks are compressed with an HDF5 filter (JPEG-LS, filter 32018)
  a stock GDAL cannot decode: the plugin comes with the `hdf5plugin`
  distribution (the `satellite` dependency group) and is handed to each
  GDAL subprocess the reader runs as `HDF5_PLUGIN_PATH`
  (`hdf5_plugin_environment`; an environment that already names one is
  left alone);
- the values are radiances: the reader reads the channel's codes with
  `gdal_translate`, unpacks them, and converts them with the channel's
  own Planck coefficients, scalar variables of its `measured` group read
  through `gdalmdiminfo` — `BT = (c2 ν / ln(1 + c1 ν³ / L) − b) / a`, the
  conversion the product documents — then writes an Int16 strip in
  steps of 1/128 K above 100 K (`FCI_BT_SCALE` / `FCI_BT_OFFSET`; a raw
  file and a VRT that carries the packing and the unit). The step is a
  binary fraction on purpose: a decimal one such as 0.01 K puts some
  values an ulp either side of a codebook half-step depending on whether
  a platform fuses `code × scale + offset`, and the two encoders then
  disagree by one code on one cell;
- the grid mapping (`/data/mtg_geos_projection`: sweep `y`, height
  35786400 m, WGS84, longitude 0) sits in a parent group GDAL does not
  connect to the variable, so the strip's VRT carries the geostationary
  SRS built from those fields and an extent from GDAL's own reading of
  the chunk's scan-angle coordinates (radians, a column or row number
  packed with a scale and offset) times the height; the strip is written
  west to east and north to south whichever way GDAL read the file. The
  reader checks the packing of the rows and the grid's width against the
  constants it plans chunks with (`FCI_IR_GRID`, `FCI_IR_ANGLE_STEP`,
  `FCI_IR_ANGLE_OFFSET`), so a product change is an error.

The strips of a channel are mosaicked with `gdalbuildvrt`; the projector
warps that. A chunk carries every channel, so a slot's files are shared
by its channels (`Reader.files_per_channel` is false: `fetch_frame` puts
them under the slot's directory rather than a channel's, and the window
removes the directory once the slot's channels are done), and only the
chunks whose rows reach into ±60° of latitude are downloaded
(`needed_chunks`: 60° on the sub-satellite meridian is seen 0.1402 rad up
from the disk's centre, against the disk's 0.1556, so the outermost two
chunks at either end — 2 through 39 remain — are never fetched, some
900 MB a cycle). A slot is complete when the product lists all forty
chunks; a cycle's slot is its sensing start floored to the ten-minute
cadence (15:20:07 is 15:20).

The store is `eumetsat.py`: `search` is the anonymous OpenSearch query
(`…/search-products/1.0.0/os?format=json&pi=<collection>&dtstart=…&dtend=…`,
each product's `date` interval, `updated` stamp and `sip-entries` — one
link per file, `entry?name=<file>`), `list_slots` one search over a day,
`recent_slots` one search back over six hours, `list_slot` one over the
cycle. `download_bytes` fetches one file with a bearer token minted by
`POST https://api.eumetsat.int/token` from a registered account's
consumer key and secret — `EUMETSAT_CONSUMER_KEY` /
`EUMETSAT_CONSUMER_SECRET` in the environment, the workflow's secrets of
the same names — renewing it when the store refuses it, waiting out a
rate limit as the store's `retryAfter` asks (capped at two minutes), and
failing by name when the two variables are unset, so a build without an
account stops at the first download. The listing gives no file sizes; a
file present on disk at all is taken as complete, since a download lands
under a `.part` name until it is.

A further reader would be `HSDReader`, for the raw Himawari Standard
Data segments should the ISatSS product ever stop, or `SEVIRIReader` for
Meteosat-9's Level 1.5 native file.

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
(a box spelled `-170` is at 190 on it); GOES-West's is 163°E to 283°E the
same way (first cell centre 163.02°E), GOES-East's 135.2°W to 15.2°W
(first cell centre 135.18°W). A scene that lies wholly outside the grid is
refused rather than written blank, since a blank warp is also what a lost
projection produces.

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
past it at the source's cadence: a cached frame is read back, a complete
slot on the bucket is fetched and warped, an incomplete or absent slot is
left out (the axis allows the gap, and the next round takes it if it
lands). The live window's end is the newest slot whose tiles have all
landed (`latest_slot`: the reader's `recent_slots` — a day's slot
directories at once for ISatSS, the newest hour directory for CMIPF, one
search for FCI, yesterday's as well around midnight — then the newest
three slots asked for their tiles, the rest trusted). `resolve_run("latest")`
is the six whole hours (`window_hours`) ending with that slot's hour, as
for MRMS and JMA with their own lengths.

**Meteosat is hourly.** The imager scans every ten minutes and the store
lists every cycle, but the `meteosat` source's `cadence_seconds` is 3600
against the platform's 600, its window 24 hours (25 frames), and
`window_slots` and `latest_slot` keep the slots on the source's cadence:
the cycle on each hour alone. The reason is the EUMETSAT data policy
(2025): the Level 1 repeat cycle referenced to each clock hour is Core
data, released under CC-BY-4.0 and redistributable at any latency; the
cycles between are licensed data whose operational use within an hour of
sensing is not free and whose numbers may not be redistributed after it.
A store with hourly frames is what that policy allows this site to
publish. The attribution it requires ("Contains modified EUMETSAT
Meteosat data") is in the shell's credit and the catalog prose. The tiles of a Himawari scan are generated about eight
minutes after it starts and listed some fifteen after, a GOES file lands
some ten minutes after its scan starts; a round is a listing, one scan's
files, one warp per channel, one composition, one stack per variable and
a conversion, three to four minutes, so the live window ends fifteen to
twenty minutes behind real time; an FCI cycle is in the store some five
minutes after its sensing ends, and its round downloads 900 MB and
converts 38 strips per channel, five to ten minutes once an hour.
`.github/workflows/publish-himawari.yml`, `publish-goeseast.yml`,
`publish-goeswest.yml` and `publish-meteosat.yml` each run one round per
job on a ten-minute cron and install `gdal-bin` whichever encoder
converts, since the wheel's GDAL is a library with no GeoTIFF driver; the
Meteosat one also proves the runner's GDAL loads the JPEG-LS plugin on the
fixture chunk before a round spends a download, and warns and builds
nothing when the two `EUMETSAT_*` secrets are absent.

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
inputs `ir086`, `ir104`, `ir112`, `ir123` (`inputs_for(platform)`: the
three without `ir112` on an imager that lacks it), outputs `dustr`,
`dustg`, `dustb`, no ancillaries, `version` the installed shachen
distribution's. It builds an xarray scene of the planes (the 10.4 µm one
standing in for 11.2 µm where there is none) and calls
`shachen.dustrgb.dust_rgb` with the stretch set the platform's instrument
takes (`DUST_RGB` for AHI and FCI, `DUST_RGB_ABI` for ABI — the rule
shachen applies by satpy reader, made here by `Platform.instrument`), then
marks every cell any input lacked as no data in all three guns. `PRODUCERS` is
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

`tests/fixtures/goes/` is eight 220 × 220 windows cut from real GOES-19
CMIPF files (`tests/prepare_goes_fixture.py`: the four Dust RGB channels
of two consecutive scans, cut with `gdal_translate -of netCDF -srcwin`,
which keeps the `geostationary` mapping, the band's packing and fill — the
Venezuelan coast and Trinidad, 66–62°W, 7–11°N), and `tests/test_goes.py`
runs them the same way under the `goeseast` source: the hour-directory
listing and its request count, the slot rule, the reissue rule, the
single-file download, the warp, the producer with the ABI stretches
(and that they differ from the SEVIRI set on the scene), the series, the
ingest, the conversion on the East disk with the ABI band block, and the
native encoder byte for byte. GOES-West is the same reader on its own
bucket and grid; its tests are the registry's.

`tests/fixtures/meteosat/` is one real FCI chunk (chunk 20 of a full-disk
cycle from EUMETSAT's public MTG test data, the spectrally representative
FDHSI cycle of May 2022: simulated 2017 radiances in the operational
format, rows 2645–2784 of the grid, the strip from the equator to 2.5°S)
and a trimmed real search response, and `tests/test_meteosat.py` runs
them under the `meteosat` source with the chunk standing for every chunk
of every slot (the reader's `needed_chunks` narrowed to it): the store's
own response parsed, the token minted, refused and renewed and a rate
limit waited out against a fake opener, the chunk names, the cycle's
slot, the needed chunks and the elevation angle behind them, the Planck
conversion against the formula, the strip's footprint and its numbers
read back through GDAL's own unscaling, the producer with three inputs
held to `dust_rgb` with the 10.5 µm plane as the green minuend, the
hourly slots against a store that lists ten-minute cycles, the series,
the ingest, the conversion with the FCI band block and the hourly axis,
and the native encoder byte for byte when the wheel knows the source.
The GDAL-backed cases skip without `hdf5plugin`.
