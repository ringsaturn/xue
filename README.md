# 雪 / Xue

[![test](https://github.com/ringsaturn/xue/actions/workflows/test.yml/badge.svg)](https://github.com/ringsaturn/xue/actions/workflows/test.yml)
[![docs](https://github.com/ringsaturn/xue/actions/workflows/docs-rust.yml/badge.svg)](https://ringsaturn.github.io/xue/)

> Xue (雪, pronounced /ɕɥɛ/, roughly "shweh"), Chinese for snow.

Xue packs weather forecast runs into per-variable Zarr v3 stores laid out
for playback ([`docs/zarr-profile.md`](docs/zarr-profile.md): quantized
single-byte planes, small spatial tiles × six consecutive steps per chunk,
one shard per variable) and renders them in the browser with a static
MapLibre page. A Rust WebAssembly worker decodes chunks on demand, a WebGL2
layer does inverse Web Mercator projection and palette lookup on the GPU,
and wind renders through a GPU particle layer. The layout was developed in
a single-file container, `.xue` ([`docs/format.md`](docs/format.md)).
Since 2026-09-15 nothing online is published in it; every decoder still
reads it.

Live demo: <https://xue.ringsaturn.me>. The control in the top-left corner
switches between the sources below. No source publishes one cadence all the
way out, so the mixed-step time axes are listed outright in the bundle
metadata (schema version 3 in `docs/format.md`: an offset list against a
declared unit). All sources share the same format, decoder and rendering
pipeline.

## Sources

| Source | Grid and cadence | Newest run (UTC) |
|---|---|---|
| NOAA GFS | 0.25° global · hourly to F120, 3-hourly to F240 | <a href="https://dataset.ringsaturn.me/xue/latest.json"><img alt="the newest gfs run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest.json&query=%24.runTime&label=&color=0b7cbd&style=flat-square&cacheSeconds=600" width="170"></a> |
| GFS surface flux | ~13 km Gaussian global · as GFS | <a href="https://dataset.ringsaturn.me/xue/latest-sflux.json"><img alt="the newest sflux run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-sflux.json&query=%24.runTime&label=&color=2b6cb0&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF IFS open data | 0.25° global · 3-hourly to F144, 6-hourly to F240 | <a href="https://dataset.ringsaturn.me/xue/latest-ecmwf.json"><img alt="the newest ecmwf run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-ecmwf.json&query=%24.runTime&label=&color=1f6f8b&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF AIFS Single | 0.25° global · 6-hourly to F360 | <a href="https://dataset.ringsaturn.me/xue/latest-aifs.json"><img alt="the newest aifs run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-aifs.json&query=%24.runTime&label=&color=3c7a89&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF IFS HRES (Open-Meteo) | 0.1° global · hourly to F090, 3-hourly to F144, 6-hourly to F360; 00Z and 12Z | <a href="https://dataset.ringsaturn.me/xue/latest-ifshres.json"><img alt="the newest ifshres run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-ifshres.json&query=%24.runTime&label=&color=2a9d8f&style=flat-square&cacheSeconds=600" width="170"></a> |
| NOAA HRRR | 0.03° contiguous US · hourly to F18, a cycle every hour | <a href="https://dataset.ringsaturn.me/xue/latest-hrrr.json"><img alt="the newest hrrr run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-hrrr.json&query=%24.runTime&label=&color=7b4ea3&style=flat-square&cacheSeconds=600" width="170"></a> |
| NOAA MRMS radar | 0.02° contiguous US · every 2 min, a rolling 4 h window | <a href="https://dataset.ringsaturn.me/xue/latest-mrms.json"><img alt="the newest mrms run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-mrms.json&query=%24.runTime&label=&color=b7472a&style=flat-square&cacheSeconds=600" width="170"></a> |
| JMA precipitation nowcast | 0.005° Japan · every 5 min, a rolling 3 h window | <a href="https://dataset.ringsaturn.me/xue/latest-jma.json"><img alt="the newest jma run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-jma.json&query=%24.runTime&label=&color=c25b2c&style=flat-square&cacheSeconds=600" width="170"></a> |
| CMA radar mosaic | 0.0439° China · every 6 min, a rolling 3 h window | <a href="https://dataset.ringsaturn.me/xue/latest-cma.json"><img alt="the newest cma run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-cma.json&query=%24.runTime&label=&color=b03a5b&style=flat-square&cacheSeconds=600" width="170"></a> |
| Himawari-9 | 0.04° disk at 140.7°E · every 10 min, a rolling 6 h window | <a href="https://dataset.ringsaturn.me/xue/latest-himawari.json"><img alt="the newest himawari run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-himawari.json&query=%24.runTime&label=&color=6d597a&style=flat-square&cacheSeconds=600" width="170"></a> |
| GOES-19 (East) | 0.04° disk at 75.2°W · every 10 min, a rolling 6 h window | <a href="https://dataset.ringsaturn.me/xue/latest-goeseast.json"><img alt="the newest goeseast run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-goeseast.json&query=%24.runTime&label=&color=5c677d&style=flat-square&cacheSeconds=600" width="170"></a> |
| GOES-18 (West) | 0.04° disk at 137°W · every 10 min, a rolling 6 h window | <a href="https://dataset.ringsaturn.me/xue/latest-goeswest.json"><img alt="the newest goeswest run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-goeswest.json&query=%24.runTime&label=&color=5c677d&style=flat-square&cacheSeconds=600" width="170"></a> |
| Meteosat-12 | 0.04° disk at 0° · hourly (the openly released cycle), a rolling 24 h window | <a href="https://dataset.ringsaturn.me/xue/latest-meteosat.json"><img alt="the newest meteosat run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-meteosat.json&query=%24.runTime&label=&color=4a5d8f&style=flat-square&cacheSeconds=600" width="170"></a> |

The last column reads the newest run off the source's live pointer
(`latest.json` for GFS, `latest-<model>.json` for the rest with the
`--model` ids of the Usage section, which it links to): a forecast's
cycle, or the hour a rolling window starts at, its frames running
several hours past it. Grid sizes and frame counts are in the source
notes below.

Each model publishes as an independent dataset under `<model>.<run>/`, taken
live by its pointer at the data root. The geostationary mosaic
(`?model=geo`) is a view over the imagers and not a dataset: the shell opens
whichever of their runs answer, clips each disk to the longitudes it sees
least obliquely — a partition at the midpoints between sub-satellite
longitudes — and follows one timeline by valid time; nothing is published
for it.

Bundle sets:

- GFS: 2 m temperature, precipitation rate, 10 m wind, the surface
  diagnostics (gust, total / low / middle / high cloud cover, CAPE,
  visibility, 2 m dew point and apparent temperature), 850 / 700 / 500 hPa
  vertical velocity, 850 hPa equivalent potential temperature (derived from
  temperature and specific humidity), the ocean fields (skin temperature,
  sea ice cover and thickness, significant wave height and primary period
  from the cycle's GFS-Wave file, plus the derived wave vector: the height
  laid along the direction of travel as a u/v pair), mean sea level pressure
  with the geopotential height, temperature, relative humidity and wind on
  all eight registered isobaric surfaces (1000 / 925 / 850 / 700 / 500 /
  300 / 250 / 200 hPa — the mandatory levels of a radiosonde ascent, so the
  model column beside a sounding is complete) and the 850 hPa water vapour
  flux (`q·V/g`, derived).
- ECMWF: the subset of that set its open data carries, under the same
  identities (`msl` as `prmsl`, `mucape` as `cape`, `skt` as `tmpsfc`,
  wave fields from the `wave` stream). Open data has no precipitation-rate
  field, so `tp` is differenced between frames into an interval-mean rate;
  the series starts at F003 (64 frames). The gust is an interval maximum
  that is empty at the analysis, so its series also starts at F003. Not in
  the open data: the layer cloud covers, visibility, sea ice cover,
  apparent temperature.
- sflux: the core four (temperature, precipitation de-averaged from
  window-cumulative records, from F001, wind) plus `dswrf`, instantaneous
  surface downward shortwave radiation.
- IFS HRES (`ifshres`): the surface set the centre's ~9 km operational
  forecast carries where Open-Meteo forwards it — 2 m temperature and dew
  point, precipitation rate, 10 m wind, downward shortwave radiation, sea
  level pressure, gust, total / low / middle / high cloud cover, CAPE,
  visibility, skin temperature and sea ice thickness: fifteen bundles, the
  same ids, codebooks and charts as GFS, so switching between the two keeps
  the layer. No isobaric levels (the 9 km product has none; `ecmwf` keeps
  them at 0.25°) and no waves. The source is the one forecast that arrives
  as a NetCDF series rather than GRIB: `om2nc` reads the Open-Meteo `.om`
  files by byte range and resamples the reduced Gaussian O1280 grid onto
  the regular 0.1° one (nearest neighbour) once, on the fetch side, so
  neither encoder does Gaussian-grid arithmetic. Precipitation arrives as
  the total over the interval since the previous step and is divided by the
  step into a rate, which — like the radiation and the gust — has no
  analysis frame. Only the 00Z and 12Z cycles reach F360 and only those are
  published; a cycle is complete about six and a half hours after it.
- HRRR: the core pair, sea level pressure (`MSLMA`), 850 / 700 / 500 hPa
  heights, 925 / 850 / 500 hPa temperature, the 10 m, 925, 850 and 250 hPa
  winds, the surface diagnostics the surface file carries, and forecast
  composite reflectivity under `cref`. The model runs on a Lambert
  conformal grid, which the format does not describe, so the encoder
  resamples every plane onto a regular 0.03° grid over the domain's
  footprint (`xuebuild/reproject.py`, repeated bit for bit by the native
  encoder); corners the conic domain never covered repeat the nearest cell
  and the viewer clips to the model's footprint.
- MRMS: composite reflectivity under `cref` and the radar-derived
  precipitation rate under `prate`. A run is a window (`--run` names its
  first hour, `--hours` its length) whose frames are listed off the
  `noaa-mrms-pds` bucket, fetched one gzipped GRIB per product, and thinned
  two to one by block maximum onto the 0.02° grid. A three-hour window is
  about 90 frames and 20–40 MB of reflectivity.
- JMA: the agency's 高解像度降水ナウキャスト (high-resolution precipitation
  nowcast) analysis over Japan, published on its website as map tiles whose
  palette encodes ten precipitation intensity classes. Not a reflectivity,
  so it ships `prate` alone, at each class's representative rate (0.5, 3,
  7.5, 15, 25, 40, 65 and 100 mm/h; the codebook keeps every class
  distinct). The [jma-radar](https://github.com/ringsaturn/jma-radar) tool
  (`uv pip install git+https://github.com/ringsaturn/jma-radar`, driven by
  `xuebuild/jmacli.py`) lists the agency's `targetTimes`, decodes the
  zoom-8 tiles onto a 0.005° grid over the radar coverage envelope
  (121–149°E, 20.5–45.5°N) by the strongest class in each cell, and writes
  a window as one NetCDF series, which both encoders read the way they read
  the CMA file. The listing reaches three hours back, so a window is three
  hours; a whole window is a few megabytes. Source: Japan Meteorological Agency
  website (出典：気象庁ホームページ), regridded and reclassified.
- CMA radar: the China Meteorological Administration's level-3 composite
  reflectivity mosaic (RADAR_L3_MST_CREF, 雷达组合反射率拼图), a national
  composite every six minutes published on the agency's data portal as BIN
  tiles on a plate carrée tile grid, shipped under `cref`. A private
  sync job decodes the zoom-5 tiles (0.0439°, 1792 × 1024 cells over
  67.5–146.25°E and 11.25–56.25°N) and keeps them as one plain Zarr v3
  store per UTC day in an archive of its own, every ten minutes; a run
  here is a window read back out of those stores by `xuebuild/cmaarchive.py`
  (zarr-python over s3fs, the `cma` dependency group: `uv sync --group
  cma`) and handed to the converter as one NetCDF series, the shape the
  cases before the archive were built from. `XUE_CMA_ARCHIVE` names the
  archive (an `s3://` base read with the `R2_*` credentials, or a local
  directory the stores were copied to; either way the prefix holding
  `RADAR_L3_MST_CREF_GISJPG_Tiles_CR/z5/<year>/<date>.zarr`, and a base
  none of whose days has a store is refused rather than read as an
  archive with nothing written) and has no default; the portal
  publishes each mosaic fifteen to twenty minutes late and the archive
  syncs every ten minutes, so the live window ends twenty to thirty
  minutes behind real time. A three-hour window is about thirty frames and one to
  two megabytes of reflectivity.
- Himawari: the Japan Meteorological Agency's Himawari-9 geostationary
  imager at 140.7°E, as NOAA redistributes it on the `noaa-himawari9`
  bucket — the ISatSS product, every channel of every ten-minute full-disk
  scan already calibrated and cut into 88 NetCDF tiles on the geostationary
  projection. Six infrared channels are fetched (3.9, 6.2, 8.6, 10.4,
  11.2 and 12.3 µm: AHI bands 7, 8, 11, 13, 14, 15); the 10.4 µm one is
  published as brightness temperature under `ir104` (at 0.6 K over
  180–332 K; the cloud-top picture), four of them feed the classic
  **Dust RGB** (`dustrgb`: red the 12.3 − 10.4 µm split window, green
  11.2 − 8.6 µm with a gamma, blue the 10.4 µm window, each stretched to
  0–1 — lofted mineral dust reads pink to magenta over dark blue-green
  surfaces, day and night), computed per scan in the fetch stage by the
  [shachen](https://github.com/ringsaturn/shachen) package's
  `dust_rgb` with the SEVIRI stretches (no AHI retune has been published)
  and shipped as one three-variable bundle whose guns carry the producer's
  id and version beside a local-use parameter, and five of them feed
  **DEBRA** (`dustcf`: Miller et al. 2017's dust confidence factor in
  0–1, the same package's cloud mask, dust tests and confidence against
  a clear-sky background modelled from a GFS skin temperature and the
  CAMEL emissivity climatology, with a split-window gate; computed
  wherever the ground is water or inside a staged emissivity region,
  which today are the Gobi through Japan, the US Southwest and the
  Saharan transport corridor — the CAMEL months the operational DEBRA
  pipeline stages on the bucket, pulled by `make pull-r2-ancillary`;
  [docs/satellite.md](docs/satellite.md) §"The DEBRA confidence"). The fetch stage
  (`xuebuild/satellite/`, [docs/satellite.md](docs/satellite.md)) lists a
  slot's tiles, mosaics them with `gdalbuildvrt`, warps them with
  `gdalwarp` onto a 0.04° plate carrée grid over the useful disk (3000 ×
  3000 cells, 80.7–200.7°E — past the antimeridian — and 60°S–60°N),
  caches each channel's frame (a GeoTIFF, mirrored on the bucket like the
  JMA frames), composes the guns and the confidence from the slot's
  frames and caches them the same way, and stacks the window's frames into one NetCDF series
  per variable with `gdal_translate`, which both encoders read the way
  they read the JMA file; the geostationary arithmetic lives in GDAL alone.
  The source is named by its orbital slot, not the spacecraft: the
  spacecraft, instrument and band are the `band` block beside the
  variable's parameter block (docs/format.md), so Himawari-10 will change a
  registry row and nothing published. The tiles are generated about eight
  minutes after a scan starts and listed about fifteen after, so the live
  window ends fifteen to twenty minutes behind real time; a scan is 26 MB
  of tiles, a warped frame about 8 MB, the six-hour window (37 frames)
  some 200 MB of stores across the ladder (full, half, quarter and
  eighth: the satellite sources publish three reduced tiers where the
  others publish the half). Needs a system
  GDAL with `gdalwarp` on PATH whichever encoder converts.
- GOES-East and GOES-West: NOAA's GOES-19 at 75.2°W and GOES-18 at
  137.0°W, from the `noaa-goes19` and `noaa-goes18` buckets — the CMIPF
  full-disk product, each channel of each ten-minute scan as one
  calibrated netCDF on the geostationary projection (sweep x), landing
  about ten minutes after the scan starts. Everything above applies with
  the platform row swapped: the same six ABI channels (7, 8, 11, 13, 14,
  15), `ir104`, `dustrgb` and `dustcf` published — the composite with the
  GOES-R Quick Guide's ABI stretches, which the producer picks by
  instrument — the same
  0.04° step and six-hour window, a frame cache per role
  (`data/raw/goeseast-frames/`). The East disk is 135.2°W–15.2°W; the West
  disk crosses the antimeridian and is spelled 163°E–283°E, as Himawari's
  is. A scan is six 24 MB files rather than 88 tiles.
- Meteosat: EUMETSAT's Meteosat-12 (MTG-I1) at 0°, from the EUMETSAT Data
  Store rather than a public bucket — the FCI Level 1c full-disk product,
  each ten-minute cycle as forty netCDF chunk files that carry every
  channel as radiances. The search is anonymous; the download takes a
  registered account's consumer key and secret (`EUMETSAT_CONSUMER_KEY` /
  `EUMETSAT_CONSUMER_SECRET`, free at https://api.eumetsat.int/api-key),
  and a runner without them builds nothing. The reader converts the
  radiances to brightness temperature with the product's own Planck
  coefficients and georeferences the strips itself (GDAL needs the
  `hdf5plugin` JPEG-LS filter for the files and cannot place a chunk from
  its grouped grid mapping); FCI has no 11.2 µm window, so the Dust RGB's
  green gun reads 10.5 − 8.7 µm, the original SEVIRI recipe. **Only the
  cycle on each hour is published**, a 24-hour window of 25 frames: the
  EUMETSAT data policy releases the hourly Level 1 cycle as Core data
  under CC-BY-4.0 and the cycles between under terms that do not allow
  this use. Contains modified EUMETSAT Meteosat data 2026 (the year of
  distribution is stamped wherever the attribution is shown).

Every level of the isobaric families is registered; turning one on is a line
in `xuebuild/sources.py` and its mirror in the native encoder, not a format
change.

## Format rationale

The `.xue` container is retired from the live service as of 2026-09-15.
Every live run, rolling-window round and showcase case is published as Zarr
v3 stores alone (`build-bin --zarr --no-xue`, what the scheduled workflows
pass), listed in a [STAC catalog](docs/stac.md). The container remains in
three roles: the intermediate both encoders write and derive the store from
(so the store's codes are the container's by construction, and the two
encoders stay byte-identical through it); the format every decoder keeps
reading, for runs and cases published before the switch and for anything
built locally without `--no-xue`; and the format the golden fixtures and the
size comparison below are stated in. The layout is what the rationale is
about, and the store carries it unchanged.

The access pattern is continuous playback of a complete global forecast
with free timeline scrubbing. A map tile pyramid stores each frame as its
own set of precolored images. For one GFS run, the same two variables
measured:

| Delivery | Size |
|---|---:|
| Per-frame raster tiles (PMTiles, zoom 0–4, precolored) | 3,205.87 MB |
| Source GRIB2 | 137.73 MB |
| Xue (`tmp2m.xue` ≈ 29 MB + `prate.xue` ≈ 36 MB) | ≈ 65 MB |

The tile-pyramid baseline is reproducible:
[`scripts/pmtiles_size_assessment/`](scripts/pmtiles_size_assessment/)
rebuilds it frame by frame and reports the byte totals.

A bundle stores each variable as quantized single-byte planes on the native
forecast grid (no reprojection, no tiling, no baked-in colors) with bounded
temporal prediction (six-frame groups for smooth fields, independent frames
for precipitation) and one Zstandard frame per chunk. Every frame is
individually addressable, so the page paints a first-frame poster before the
rest of the bundle arrives, then range-requests only the index and the
chunks it needs, prefetching around the playhead into a byte-budgeted cache.
Adjacent frames blend on the GPU during playback.

Four implementations share the format:

- Python encoder (`xuebuild/`): fetch → convert → quantize → temporal
  residuals → zstd → container → store. GDAL and ffmpeg are CLI
  subprocesses; zstd runs through the standard library's `compression.zstd`
  on Python ≥ 3.14 and the zstd CLI on older interpreters. This is the
  reference: a format change lands here first.
- Native encoder (`rust/xue/src/encode/`, shipped as the `xuepy` wheel):
  the same conversion with GDAL, grib-rs and zstd linked in. A build runs
  through it by default and it is held to the reference by byte-for-byte
  identical output: bundles, posters, variants, H.264 companions, manifest
  and live pointer. See [`docs/encoder.md`](docs/encoder.md).
- Rust decoder (`rust/xue` core crate, `rust/xue-wasm` bindings), built
  into the frontend via `make wasm`.
- TypeScript frontend (`web/src/`): manifest resolution with
  resolution-tier selection, a decode worker with windowed prefetch, the
  WebGL2 blend-playback layer, the wind particle layer, and an alternate
  WebCodecs H.264 path, opt-in per session with `?use_h264=true`.

Cross-language golden tests keep the Python encoder and Rust decoder
byte-identical.

The Zarr store ([`docs/zarr-profile.md`](docs/zarr-profile.md)) is the same
codes in a layout any Zarr client reads: a container v2 bundle is, to within
its index format, a sharded Zarr `uint8` array. `build-bin --zarr` (or
`XUE_ZARR=1`) derives `<bundle>.zarr/` beside every `.xue` and each of its
reduced-resolution variants and names it in the manifest (`zarr: {path,
byteLength, crc32}`). The store carries the bundle's metadata verbatim in
its root attributes plus CF `scale_factor` / `add_offset` / `_FillValue` on
linear codebooks and `time` / `latitude` / `longitude` coordinates, so
`xarray.open_zarr` reads it as physical values with nothing installed beyond
a Zarr client. `--no-xue` (or `XUE_CONTAINER=0`) publishes the store alone.
`xue export-zarr <bundle.xue>` derives one by hand; `--delta` swaps in the
`xue.delta` codec (the container's temporal residual as a codec,
`xuebuild/zarrcodec.py`), under which a chunk's compressed bytes equal the
bundle's wherever the two chunkings coincide. A plain `build-bin` (and `make
mvp`, which passes `--zarr`) keeps both on disk for local work.

## Requirements

- Python ≥ 3.12 (NumPy and the `xuepy` wheel; `uv sync` creates `.venv`)
- GDAL ≥ 3.8 with the GRIB driver
- zstd ≥ 1.5 (bundled with Python ≥ 3.14; the zstd CLI is required only on
  older interpreters)
- Node.js ≥ 22
- Rust toolchain + `wasm-pack` (builds the browser decoder)
- eccodes (`grib_set`, ECMWF source only: open data is CCSDS/AEC-packed and
  is repacked to `grid_simple` at fetch time so any GDAL build can read it)
- [om2nc](https://github.com/ringsaturn/om2nc) (`ifshres` source only: it
  reads Open-Meteo's `.om` files and writes the NetCDF series the encoders
  ingest). A standalone binary under GPL-2.0-only, run as a subprocess the
  way GDAL, ffmpeg and eccodes are — nothing in this tree imports, links or
  vendors it, and no om dependency appears in `pyproject.toml`,
  `Cargo.toml` or `web/`. Install a pinned release from its GitHub
  Releases (the publish workflow verifies the sha256) or set `XUE_OM2NC`
  to the command.
- AWS CLI v2 and `jq` (publishing only: the R2 bucket is managed over its
  S3 API)

`make check` verifies versions and required commands. The NOAA and ECMWF
public S3 buckets need no AWS credentials.

## Usage

Build the latest run end to end and serve the frontend:

```sh
make mvp                  # NOAA GFS (default)
make mvp MODEL=ecmwf      # ECMWF IFS open data
make mvp MODEL=aifs       # ECMWF AIFS Single open data
make mvp MODEL=ifshres    # ECMWF IFS HRES 0.1° via Open-Meteo (needs om2nc)
make mvp MODEL=sflux      # GFS surface flux
make mvp MODEL=hrrr       # NOAA HRRR
make serve
```

Or step by step (`--model gfs|sflux|ecmwf|aifs|ifshres|hrrr|mrms|jma|cma|himawari|goeseast|goeswest|meteosat`,
default `gfs`; `--hours` defaults to the whole axis the model publishes: 240
for the global models, 360 for AIFS and IFS HRES, 18 for HRRR, and on the
rolling windows their length):

```sh
python -m xuebuild fetch --run latest --hours 240
python -m xuebuild convert-bin data/raw/gfs.YYYYMMDDHH \
  --output web/public/data/gfs.YYYYMMDDHH \
  --manifest web/public/data/manifest.json
python -m xuebuild verify-bin web/public/data/gfs.YYYYMMDDHH/tmp2m.xue
python -m xuebuild build-bin --run latest --hours 240
python -m xuebuild build-bin --model ecmwf --run latest --hours 240
python -m xuebuild build-bin --model aifs --run latest --hours 360
python -m xuebuild build-bin --model ifshres --run latest --hours 360
python -m xuebuild build-bin --model sflux --run latest --hours 240
python -m xuebuild build-bin --model hrrr --run latest
python -m xuebuild build-bin --model mrms --run 2026091300 --hours 3   # a past window
python -m xuebuild build-bin --model mrms --run latest --hours 4 --round now   # one round of the live window
python -m xuebuild build-bin --model jma --run latest --hours 3 --round now    # the JMA nowcast, through jma-radar
python -m xuebuild build-bin --model cma --run latest --hours 3 --round now    # the CMA mosaic, out of its archive
python -m xuebuild build-bin --model himawari --run latest --hours 6 --round now  # Himawari-9 infrared, warped from NOAA's tiles
python -m xuebuild build-bin --model goeseast --run latest --hours 6 --round now  # GOES-19 the same (goeswest for GOES-18)
python -m xuebuild build-bin --model meteosat --run latest --hours 24 --round now  # Meteosat-12, hourly (needs the EUMETSAT_* credentials)
```

`XUE_ENCODER` picks which encoder converts: `auto` (the default: the `xuepy`
wheel when it is installed), `native` (require it, fail if it is missing),
or `python` (the subprocess pipeline, the reference). `make check` reports
which one a build would take. The two produce identical bytes, so the choice
is only about speed.

`--hours` may be any hour on the model's published axis; a shorter uniform
build (`--hours 120`) has a plain-step axis rather than a listed one.

`build-bin` writes one bundle per scalar variable (a `.xue`, and with
`--zarr` the `<bundle>.zarr/` store derived from it; with `--no-xue` the
store alone), the source's reduced-resolution renditions (a `.half` on
every source; `.quarter` and `.eighth` too on the satellite sources,
`SourceSpec.variant_factors`), a first-frame poster,
and, on GFS and HRRR, a lossless H.264 companion for the surface fields
(`--skip-variants` / `--skip-video` disable them; ECMWF and sflux have the
companion switched off in `xuebuild/sources.py`, since the video path is
opt-in and the sflux encode was a third of that run's bytes). The pressure
family gets neither a poster nor a companion: the page draws it as contour
lines, which need the exact codes. Each two-variable vector bundle
(`wind10m.xue`, `wind850.xue`, `qflux850.xue`) is added when the input GRIB
carries its components (older cached GRIBs without them are skipped;
`--force-download` re-fetches). `manifest.json` carries per-bundle path,
byte length, CRC-32 and the `variants` resolution ladder. `verify-bin`
validates one file's structure and decodes every frame. Conversion runs one
`gdalinfo` and one multi-band `gdal_translate` per GRIB file, parallelized
across files: a 121-frame run converts in about 43 seconds.

Raw GRIB fragments live in `data/raw/`, published data in
`web/public/data/`, final static output in `dist/`. Overwriting an existing
manifest requires `--force` (`make mvp FORCE=--force`).

### URL state

`/?model=gfs&type=wind`, `/?model=ecmwf&type=temp`. `model` accepts `gfs` /
`ecmwf` (alias `ifs`) / `aifs` (alias `aifs-single`) / `ifshres` (aliases
`hres`, `ifs9km`) / `sflux` / `hrrr` / `mrms`; `type` accepts aliases
such as `tmp2m` / `prate` / `wind10m` / `solar` / `radar`, and each isobaric
field names itself (`pressure`, `hgt500`, `tmp850` / `t850`, `rh700`,
`wind850`, `qflux850` / `vapor850`; there is no separate `level`
parameter). Both are case-insensitive, the address bar stays in sync, and
unrecognized values fall back to defaults. In the page the rail carries a
tile for each core field (temperature, precipitation, wind, cloud on a
forecast model) and a MORE tile opening the list of every field the run
publishes; one tile stands for a whole family and the level row on the
transport capsule picks the surface. The pressure lines are a switch in the
rail's overlay section, and the level row's ALONE member leaves them by
themselves.

The view is in the fragment, `#map=<zoom>/<lat>/<lon>` (MapLibre's own
spelling), kept current on every pan and zoom. A link without one opens on
the dataset's own region (a case's box, or a regional model's footprint),
and switching to a regional model frames that footprint unless the map is
already over it.

Clicking the map pins a point and opens its forecast over the transport
capsule: the field on screen at the playhead, and under it a meteogram
(temperature and dew point, precipitation, wind with gusts and direction,
cloud layers, sea level pressure) read from the same run at that grid cell,
each row present when the run publishes its bundle. On a tiled bundle a
whole series costs one range request per temporal group. A press on the
chart scrubs the timeline. The pin reads the two station products as well,
whether or not their marks are switched on: a radiosonde ascent within
150 km is drawn under the rows as a skew-T with the run's own isobaric
levels dotted over it (and a full-height chart a button away), and an
airport within 40 km lays its last day of METARs over the rows themselves,
its TAF along the bottom as a row of forecast periods.

The two station products are marks over whatever layer is on screen, each
behind its own rail tile and off until pressed: `?stations=snd` draws the
radiosonde soundings (a circle per station, filled by its 500 hPa
temperature), `?stations=apt` the airports (a circle per station, coloured
by its flight category, thinned below zoom 6 to the stations with a current
TAF), `?stations=snd,apt` both, and nothing at all draws neither. A click on
a mark opens its card; a station observed far from the playhead is drawn
faint. Neither product takes a session, fetches per frame or holds up
playback.

Session settings, read once at load:

- `?res=half` (alias `low`) always loads the smallest reduced rendition; `?res=full`
  (alias `high`) always loads the canonical bundle. A dataset that ships no
  reduced tier (every showcase case) is full resolution either way; the
  data card names the tier in use (`Xue ½`, `Zarr ¼`).
- `?use_h264=true` opts into the WebCodecs H.264 companions.
- `?backend=xue` reads a bundle through its `.xue` container where the run
  publishes one beside the store. The store is what the viewer opens by
  default wherever the manifest names one; the data card reads `Zarr`.

## Comparing models at a point

`/compare.html?lat=<deg>&lon=<deg>` lays every live forecast model at one
point on one clock: a block per model with its run in the header, a row of
local hours and a row per field (`?fields=tmp2m,prate,wind10m`, among
temperature, dew point, precipitation rate, wind, gust, cloud cover and
sea level pressure), five days hourly or fifteen days three-hourly
(`?span=360`), with the nearest airport's METAR reports of the last day as
a block of the same shape above them. Each value is the model's own grid
cell nearest the point, read out of the bundle's Zarr store with one range
request per temporal group and never interpolated; no map and no playback
session is opened. The viewer's point probe links to the page at the
pinned point, and a regional model appears only where its domain covers
the point.

## Historical showcase

Besides the live feed, the site publishes cases: one past weather event
each, cropped to the region and hours it is about, listed at
`/showcase.html` and played back by the ordinary viewer at `/?case=<id>`,
which pins the case's dataset and run and frames the map on its region.

Most cases are archived forecast runs. A case can also be a series of
observations: the `cma` source is the CMA level-3 radar mosaic (composite
reflectivity, `cref`), a case being a window of the tool's archive named by
its first hour or, as the cases before the archive were, a local NetCDF file
the tool wrote (`dataset`). Its frames come every six minutes,
which the bundle time axis carries exactly (`unitSeconds`); a 212-hour case
at that cadence is 2096 frames.

A case is a checked-in JSON file (`showcase/cases/<id>.json`) naming the
model, the run (or dataset file), the range, bounding box, and the subset of
variables:

```sh
make showcase-check                      # validate every definition
make showcase CASE=zhengzhou-2021        # fetch, crop, encode, index
make upload-r2-showcase                  # publish cases + the catalog
```

Or on a runner: [`showcase.yml`](.github/workflows/showcase.yml) takes a
case id (or several) by dispatch, pulls the live cases' catalog rows first
(`make live-showcase-catalog`, so the rewritten `showcase.json` still lists
every case), builds and uploads. The archives sit in AWS us-east-1, which is
where a runner is fastest; a satellite case (`himawari`, a window of the
ISatSS archive named by its first hour; a few hours is a case, six the
live feed's own window) is minutes there.

Cropping happens in the encoder (`crop_grid` / `convert_bin(bbox=...)`): the
window is rounded outward to whole grid cells, may cross the antimeridian,
and the resulting bundle is an ordinary one whose `grid` block names a
window instead of the globe. A case is a few megabytes, so cases stay
published permanently while runs are pruned.

Archive depth limits which forecast events are possible: NOAA GFS and sflux
reach back to about 2021-01, ECMWF open data to about 2024-02, Himawari-9's
ISatSS tiles to 2022-12 (Himawari-8's bucket holds 2019–2022, not yet a
platform here). Radar cases need the decoded file locally
(`XUE_OBSERVATION_ROOT`). See
[`showcase/README.md`](showcase/README.md) for the authoring guide.

## Publishing

The live site is a static shell on Cloudflare Pages; data lives on a public
R2 bucket (`dataset.ringsaturn.me/xue/`), because bundles exceed the Pages
25 MB per-file limit. The bucket is managed over R2's S3 API with the AWS
CLI:

```sh
make upload-r2 MODEL=gfs RUN=2026081600   # run assets, warm the CDN, then the live pointer
make prune-r2  MODEL=gfs                  # delete the runs it superseded
make upload-r2-showcase                   # showcase cases, then the catalog
```

Pruning only considers `<model>.<run>/` directories, so showcase cases are
never removed by it.

Between the assets and the pointer, `upload-r2` runs `make warm-r2`
(`scripts/warm_edge_cache.sh`): one full GET of every artifact through
`dataset.ringsaturn.me` with the site's `Origin` header, so the edge and the
Smart Tiered Cache upper tier hold the run when its first viewer arrives. A
cold fill from R2 runs at about 1 MB/s per object. The H.264 companions are
left cold, since they are opt-in. A failed warm-up is reported and the
pointer goes live anyway, and `WARM=false` skips it: the rolling-window
rounds (MRMS, JMA) do, since a round is a handful of small objects replaced
minutes later.

Credentials are an R2 API token's key pair in `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, plus `CLOUDFLARE_ACCOUNT_ID` for the endpoint.

GitHub Actions runs the loop on a schedule, one workflow per source
([`publish-gfs.yml`](.github/workflows/publish-gfs.yml),
[`publish-sflux.yml`](.github/workflows/publish-sflux.yml),
[`publish-ecmwf.yml`](.github/workflows/publish-ecmwf.yml),
[`publish-aifs.yml`](.github/workflows/publish-aifs.yml),
[`publish-ifshres.yml`](.github/workflows/publish-ifshres.yml),
[`publish-hrrr.yml`](.github/workflows/publish-hrrr.yml), the last every
hour), all calling the reusable
[`publish.yml`](.github/workflows/publish.yml) with the `R2_ACCESS_KEY_ID`
/ `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_ACCOUNT_ID` repository secrets. The
bucket keeps only the live run per source (the MRMS window keeps the
previous run too). Each workflow also takes a manual dispatch with a
dry-run switch.

The scheduled publish builds a run in pieces. Bundles are independent of one
another, so `publish.yml` splits them into groups (`xuebuild
bundle-groups`, at most `max_jobs` of roughly equal cost) and builds each in
a job of its own: the job fetches only its bundles' GRIB records, packs them
(`build-bin --bundles …`), syncs and warms what it built (`make
upload-r2-bundles`) and hands on a partial manifest,
`manifest.part.<group>.json`, as a workflow artifact. One last job merges
the parts (`xuebuild assemble-run`), uploads and warms the manifest and
takes the run live (`make upload-r2-manifest`), then prunes. Wall time is
one group's build and stays flat as bundles are added;
`tests/test_assemble.py` holds a split build byte-identical to a whole one.
By hand:

```sh
# one group per machine
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --profile balanced --bundles tmp2m prate
make upload-r2-bundles MODEL=gfs RUN=2026081600
# then, with every manifest.part.*.json gathered into web/public/data/gfs.2026081600/
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600
make upload-r2-manifest MODEL=gfs RUN=2026081600    # manifest, warm it, then the pointer
```

When the cycle R2 already serves is still the newest one, the publish has
nothing to do unless the source has started publishing bundles that run
does not carry. Then it tops the run up: `bundle-groups --base-manifest`
lists only the bundles the live manifest lacks, those groups are built and
uploaded as above, and `assemble-run --base-manifest` merges their parts
onto the live manifest. `force` rebuilds everything. By hand:

```sh
make live-manifest MODEL=gfs > live-manifest.json
.venv/bin/python -m xuebuild bundle-groups --model gfs --base-manifest live-manifest.json   # what is missing
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --bundles htsgw perpw
make upload-r2-bundles MODEL=gfs RUN=2026081600
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600 --base-manifest live-manifest.json
make upload-r2-manifest MODEL=gfs RUN=2026081600
```

The Pages shell is deployed separately (`make deploy`) and only needs
redeploying when frontend code changes.

### The rolling windows (MRMS, JMA, CMA radar, Himawari, GOES)

An observation feed is never complete, so
[`publish-mrms.yml`](.github/workflows/publish-mrms.yml),
[`publish-jma.yml`](.github/workflows/publish-jma.yml),
[`publish-cma.yml`](.github/workflows/publish-cma.yml),
[`publish-himawari.yml`](.github/workflows/publish-himawari.yml),
[`publish-goeseast.yml`](.github/workflows/publish-goeseast.yml),
[`publish-goeswest.yml`](.github/workflows/publish-goeswest.yml) and
[`publish-meteosat.yml`](.github/workflows/publish-meteosat.yml) each run one round of
[`scripts/window_rounds.sh`](scripts/window_rounds.sh) (`MODEL=mrms`, `jma`,
`cma`, `himawari`, `goeseast`, `goeswest` or `meteosat`, `ONCE=true`) per job on a five-minute cron (ten for
the satellites, whose scans are ten minutes apart; the Meteosat job finds a new hourly cycle once in six), the finest GitHub offers: a job is a
few minutes rather than a runner held for an hour, at the price of the cron's
ten to twenty minutes of lateness on every round. A dispatch with `loop` runs
the rounds until twenty past the next hour and yields to the next scheduled job
once it is waiting (`scripts/successor_queued.sh`), for catching up by hand. A round compares the bucket's newest
frame with the live round's `window.json` (`make live-window`) and, when
there is something new, builds the whole window again (`build-bin --run
latest --hours 4 --round HHMM`; frames already on disk are reused, and the
previous run's are linked across when the window crosses an hour), uploads
the round with `make upload-r2 … ROUND=HHMM` into `mrms.<run>/<HHMM>/`
(warmed, then the pointer), and prunes: rounds before the run's newest two
(`make prune-r2-rounds`, never the one the pointer names), then runs before
the newest two (`make prune-r2 KEEP=2`, so a viewer still on the previous
window keeps its artifacts). Each round goes into its own subdirectory
because the same run is rebuilt every five minutes and an object rewritten
under an unchanged `?v=` would serve a viewer's range requests the wrong
bytes. One log line per round records the newest frame's age when the
pointer was written and each step's seconds. The JMA job runs with
`FRAME_CACHE=true` and `HOURS=3`: the decoded-frame cache the jma-radar
tool keeps (`data/raw/jma-frames/`) is pulled from the bucket before the
first build (`make pull-r2-frames`, the window's hours only), pushed back
after every upload and pruned to the last seven days once an hour, so the
agency's tiles are fetched once whatever runner asks. It also resolves its
encoder at job time rather than pinning `native` like the other publish
workflows: a `xuepy` wheel that predates the source (the wheels ship on the
crate's tags) sends the rounds through the reference pipeline with GDAL and
a warning on the run, and a wheel that knows it takes the native path
again with no change to the workflow. The CMA radar job is the same shape
without the frame cache: the archive is named by the `XUE_CMA_ARCHIVE`
secret and read in process with the `R2_*` credentials
(`CMA_ARCHIVE_ACCESS_KEY_ID` / `CMA_ARCHIVE_SECRET_ACCESS_KEY` when the
dataset bucket's token cannot read it, else that token), with the `cma`
dependency group synced. The Himawari job is the JMA shape with a
frame cache of warped GeoTIFFs (`data/raw/himawari-frames/<variable>/`,
one directory per channel and per Dust RGB gun, kept eight hours on the
bucket — the window plus slack, since NOAA archives the scans and a day of
the cache is some 4.5 GB; `FRAMES_KEEP_HOURS`), a six-hour window
(`HOURS=6`), the `satellite` dependency group
(the shachen package the Dust RGB and DEBRA are computed with), the
staged CAMEL months pulled before the first build (`ANCILLARY=true`,
`make pull-r2-ancillary`) and installs
`gdal-bin` whichever encoder converts, since the fetch stage warps through
the system GDAL; the two GOES jobs are that workflow with `MODEL` swapped.
By hand:

```sh
ONCE=true scripts/window_rounds.sh                     # one round, as the job would run it
MODEL=jma HOURS=3 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh
MODEL=cma HOURS=3 ONCE=true scripts/window_rounds.sh   # needs XUE_CMA_ARCHIVE and the R2_* credentials
MODEL=himawari HOURS=6 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh   # needs gdalwarp on PATH
MODEL=goeseast HOURS=6 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh   # or goeswest; the same
MODEL=meteosat HOURS=24 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh  # needs the EUMETSAT_* credentials and hdf5plugin
.venv/bin/python -m xuebuild build-bin --model mrms --run latest --hours 4 --round now
make upload-r2 MODEL=mrms RUN=2026091321 ROUND=1405  # the round the build named
make prune-r2-rounds MODEL=mrms && make prune-r2 MODEL=mrms KEEP=2
```

### Tropical cyclones

Storm tracks are a second product beside the runs ([`docs/tc.md`](docs/tc.md)).
Every hour `xue tc-build` fetches the centres' and the models' track files
(JTWC's warnings, NHC's ATCF decks, the NCEP GFS and GEFS tracker output,
ECMWF's `tf` BUFR through eccodes' `bufr_dump`, IBTrACS), parses each,
resolves which sightings are one storm (ATCF ids first, invests and
model-found systems by alias memory and proximity) and writes
`web/public/data/tc.<hour>/` plus the pointer `latest-tc.json`. Each source
fails on its own and is recorded in the product's `sources`; the pointer is
withheld only when nothing contributed.
[`publish-tc.yml`](.github/workflows/publish-tc.yml) runs at twenty past
every hour, independent of the raster publishes:

```sh
make live-tc-index                          # the live index, so ids carry over
make tc-build                               # this hour, into web/public/data/
make upload-r2-tc ISSUE=2026091301          # the directory, then the pointer
make prune-r2-tc                            # issues older than two days
.venv/bin/python -m xuebuild tc-build --issue 2026091206 --offline --raw-dir tests/fixtures/tc   # from the fixture
```

The viewer draws the product over any layer: the rail's storm tile opens a
sheet of the systems the product carries, `?tc=<id>` deep-links one, and
the agency forecasts (dashed ahead of the playhead, solid behind it, wind
radii at the current position), best tracks and model tracks follow the
timeline by valid time.

### Airports

Airport observations and forecasts are a third product beside the runs
([`docs/airport.md`](docs/airport.md)). Every ten minutes `xue
airport-build` fetches three cached files from the NOAA Aviation Weather
Center (the world's decoded METARs of the last ninety minutes, every
current TAF, and the station table at most once a day), converts them to
SI, merges the observations onto the previous round's 24-hour history and
writes `web/public/data/airport.<round>/` — `history.jsonl`, one line per
station, and the `index.json` that carries each station's newest
observation and the byte span of its line — plus the pointer
`latest-airport.json`. So the browser reads one airport with one range
request, and anyone wanting the whole day streams a single file instead of
listing a thousand. Either observation source may fail on its own; the
pointer is withheld only when both do.
[`publish-airport.yml`](.github/workflows/publish-airport.yml) runs every
ten minutes, independent of the raster publishes:

```sh
make live-airport-index                      # the live index and its history file
make airport-build                           # this round, into web/public/data/
make upload-r2-airport ROUND=202609161440    # the history, then the index, then the pointer
make prune-r2-airport                        # rounds older than three hours
.venv/bin/python -m xuebuild airport-build --round 202609161430 --offline --raw-dir tests/fixtures/airport   # from the fixture
```

The viewer draws the round as marks over any layer (`?stations=apt`), each
in its flight category's colour; clicking one reads its newest observation.

### Soundings

Radiosonde ascents are a fourth product beside the runs
([`docs/sounding.md`](docs/sounding.md)). Every hour `xue sounding-build`
lists the two GTS→WIS2 gateway directories in the WMO WIS2 Global Cache — a
public bucket where the Japan Meteorological Agency and the Deutscher
Wetterdienst republish every country's GTS bulletins — downloads the TEMP
bulletins that have arrived since the last issue, decodes them with
eccodes' `bufr_dump`, keeps one sounding per station and nominal time (the
longest, then the latest correction), thins each ascent to the classical
TEMP set, derives the freezing level, precipitable water, 850–500 hPa
lapse rate and tropopause, and writes
`web/public/data/sounding.<hour>/` plus the pointer `latest-sounding.json`.
An issue is two files: `soundings.jsonl`, one line per station, and the
`index.json` that spans it, so the viewer reads one station with one range
request and an analyst streams the whole file. A station with no new
ascent this hour is carried forward from the previous issue. Each gateway
fails on its own and is recorded in the product's `sources`. [`publish-sounding.yml`](.github/workflows/publish-sounding.yml)
runs at a quarter past every hour, independent of the raster publishes:

```sh
make live-sounding-index                    # the live issue, for the watermark and the copy-forward
make sounding-build                         # this hour, into web/public/data/
make upload-r2-sounding ISSUE=2026091402    # the directory, then the pointer
make prune-r2-sounding                      # issues older than two days
.venv/bin/python -m xuebuild sounding-build --issue 2026091402 --offline --raw-dir tests/fixtures/sounding   # from the fixture
.venv/bin/python tests/prepare_sounding_golden.py   # regenerate the golden after a deliberate change
```

The viewer draws the issue as marks over any layer (`?stations=snd`), each
filled by its 500 hPa temperature; clicking one reads the headline the
index carries — the 500 hPa temperature and dew point, the freezing level,
the precipitable water and the level count.

The soundings are WMO core data under the WMO Unified Data Policy — free
and unrestricted, attribution of the original source requested. The
originating national meteorological and hydrological services are the
source of every ascent; the WIS2 Global Cache is the distribution.

### The STAC catalog

Beside the pointers, the manifests and `showcase.json`, which keep their
shape, the encoder derives a static [STAC](https://stacspec.org/) catalog
([`docs/stac.md`](docs/stac.md)): `catalog.json` at the data root, one
`<source>/collection.json` per live source whose `item` / `latest-version`
links name the live Item at the stable `<source>/item.json` (the run's Item
with its hrefs relocated, so a bookmark outlives the run), one
`<source>.<run>/item.json` per run beside its manifest with an asset per
artifact (the Zarr store as `application/vnd.zarr`, sizes and CRCs under
the file extension, the grid and axis as datacube dimensions, the cycle as
`forecast:reference_datetime`), and `showcase/collection.json` with an Item
per case. Every document is a function of the manifest, the catalog row and
the source registry only, so it is the same whether a run was built whole
or in pieces, and it is written wherever the manifest is: `build-bin`,
`assemble-run`, `showcase build` / `refresh` / `catalog`. The upload
targets carry them: an Item with its manifest, a Collection and the catalog
with the pointer, the showcase's with `upload-r2-showcase`.

The root also carries a plain `index.html`
([`web/dataroot/index.html`](web/dataroot/index.html), `make
upload-r2-index`): the same entry for a person, naming `catalog.json` and
these documents, with a table of the live Collections read from the catalog
in the browser.

The three point products are in it on the same terms, derived from an
issue's `index.json` instead of a manifest: `sounding/`, `airport/` and
`tc/` each carry a `collection.json` and the live `item.json`, and
`<product>.<issue>/item.json` sits beside each index with the stations'
bounding box as its geometry and one asset per file the issue ships — the
index under its `?v=`, the one NDJSON file, or one JSON file per storm.
Each product's build writes them and `upload-r2-<product>` carries them;
`xue stac --product <p> --issue <i>` rewrites them for an issue on disk.

```python
import pystac, xarray as xr
run = next(pystac.Catalog.from_file("https://dataset.ringsaturn.me/xue/catalog.json").get_child("gfs").get_items())
ds = xr.open_zarr(run.assets["tmp2m"].get_absolute_href())
```

## Testing

```sh
make test        # Rust decoder + Python encoder + frontend unit tests
npx playwright install chromium
make test-e2e    # browser end-to-end tests
```

Python tests cover the quantization codebooks, modulo-256 temporal
residuals, container structure and rejection paths (truncation,
out-of-range offsets, overlaps, gaps, nonzero padding, cyclic dependencies,
checksum failures), the manifest contract, and the tropical cyclone product
(each parser, the identity rules, the validators, and a build held to a
committed golden). The Rust side adds cross-language golden tests decoding
byte-identically against the Python reference, plus mutation fuzzing;
`wasm-pack test --headless --chrome rust/xue-wasm` runs the decoder in a
real browser. Browser tests cover bundle download and verification,
on-demand loading and reuse, playback, scrubbing, and error recovery.
Fixture provenance and regeneration are documented in
`tests/fixtures/README.md`.

## Data and Licensing

Weather data comes from
[NOAA GFS](https://registry.opendata.aws/noaa-gfs-bdp-pds/) (public domain)
and [ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data),
the IFS and the AIFS (CC BY 4.0, © European Centre for Medium-Range Weather Forecasts; this
project distributes converted derivatives: "Contains modified ECMWF open
data"). The IFS HRES surface fields (`ifshres`) are ECMWF's ~9 km
operational forecast as [Open-Meteo](https://open-meteo.com) forwards it
under CC BY 4.0: "Adapted from ECMWF IFS by ECMWF, licensed under CC BY
4.0", via Open-Meteo. Neither ECMWF nor Open-Meteo endorses this use. The basemap is [Protomaps](https://protomaps.com)-hosted vector
tiles, © [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors.

The code is dual-licensed under MIT and Apache-2.0
([LICENSE-MIT](LICENSE-MIT) / [LICENSE-APACHE](LICENSE-APACHE)); use either
at your option.

## Acknowledgments

This project is built with [Claude Code](https://claude.com/claude-code),
supported by a Claude Max (20x) subscription provided through Anthropic's
[Claude for OSS](https://claude.com/contact-sales/claude-for-oss) program.
Thank you.
