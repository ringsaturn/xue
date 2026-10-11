# Sources

Engineering notes per source: what each one fetches, how its fetch stage
differs, and what the rest of the system relies on. `xuebuild/sources.py`
(`SourceSpec`) is the registry; the native encoder mirrors it in
`rust/xue/src/encode/sources.rs`, and the shell's mirror is
`FORECAST_MODELS` in `web/src/manifest.ts`. The conversion itself is in
[pipeline.md](pipeline.md), the delivery contract in
[delivery.md](delivery.md), and the satellite fetch stage in
[../satellite.md](../satellite.md).

## Overview

| id | kind | fetch stage | axis / cadence | core |
| --- | --- | --- | --- | --- |
| `gfs` | forecast | `.idx` byte ranges, NOAA (Google mirror) | 1 h to F120, 3 h to F240; 6-hourly cycles | `tmp2m`, `prate` |
| `sflux` | forecast | as GFS, surface flux files | as GFS | `tmp2m`, `prate` |
| `ecmwf` | forecast | ECMWF open data, repacked from CCSDS | 3 h to F144, 6 h to F240 | `tmp2m`, `prate` |
| `aifs` | forecast | ECMWF open data (`aifs-single`) | 6 h to F360 | `tmp2m`, `prate` |
| `hrrr` | forecast (regional, regridded) | NOAA, two mirrors | 1 h to F18; hourly cycles | `tmp2m`, `prate` |
| `gefsaero` | forecast (aerosol) | GEFS chem files, Google mirror | 3 h to F120 | `aod` |
| `cfs` | forecast (seasonal) | one object per variable per run | 6 h from F6 to F6552; 00Z/12Z | `tmp2m`, `prate` |
| `ifshres` | series-file forecast | om2nc (Open-Meteo `.om`) | 1 h to F90, 3 h to F144, 6 h to F360; 00Z/12Z | `tmp2m`, `prate` |
| `woof` | series-file forecast, no feed (cases only) | `xue wrf-series` over a local WRF run | 1 h from F1 to F72 | `tmp2m` |
| `mrms` | fetched observation | gzipped GRIB listed off the bucket | 120 s slots, 3 h window (4 in the workflow) | `cref` |
| `mrms3d` | fetched observation (volume) | 33 gzipped GRIBs a frame, listed off the bucket | 600 s marks of 120 s scans, 3 h window | `refl3d` |
| `jma` | series-file observation | `jma-radar` tool, frame cache | 300 s, 3 h window | `prate` |
| `cma` | series-file observation (archived) | `cmaarchive.py` over a Zarr archive | 360 s, 3 h window | `cref` |
| `aurora` | frame-cache observation | SWPC OVATION JSON, frame cache | 300 s, 12 h window | `aurora` |
| `himawari`, `goeseast`, `goeswest` | satellite | `xuebuild/satellite/`, frame cache | 600 s, 6 h window | `ir104` |
| `meteosat` | satellite | `xuebuild/satellite/` (EUMETSAT Data Store) | 3600 s, 24 h window | `ir104` |

Each source publishes from `.github/workflows/publish-<id>.yml`. Forecast
workflows call the reusable `publish.yml` (one build job per bundle
group, then `assemble-run`; see [publishing.md](publishing.md)), whose model
`case` is an allow-list a new forecast source must be added to. Observation
workflows run rounds of `scripts/window_rounds.sh`. `geo` in
`FORECAST_MODELS` is not a source: it is a shell view composing the four
disks by longitude (`mosaic.ts`).

## Shared mechanisms

**Axis and cycles.** A forecast's axis is `steps`, `(last_hour, step)`
segments whose last boundary is `horizon_hours` (the `--hours` default);
`first_hour` moves the start off the analysis (CFS). `cycle_hours` is the
cycle cadence; a source carries one horizon, so a model whose short cycles
stop early publishes only its long cycles (`ifshres`, `cfs`: 12). The CLI's
bound on `--hours` is `MAX_FORECAST_HOURS`, derived from the registry.

**Rolling windows and rounds.** An observation run is a window named by its
first hour (`window_hours`, the `--hours` default), and `cadence_seconds`
makes the window's slots the axis, so `unitSeconds` is the cadence.
`--run latest` resolves (`fetch.py::resolve_run`, `latest_observation_slot`
dispatching per source, `observation_window_start`) to the window whose last
hour holds the newest frame. The same run is rebuilt as frames arrive, so
each build takes `--round HHMM` and lands in `<model>.<run>/<HHMM>/` with a
`window.json` (`fetch.py::window_summary`). **Never overwrite an object
under an unchanged `?v=`**: a viewer's range requests against it would decode
the wrong bytes; a new round is a new directory. A round is: newest frame vs
the live `window.json` → build → `make upload-r2 … ROUND=` →
`prune-r2-rounds`, which keeps the live run's newest two rounds in clock
order (a round is named by its build minute and a run's rounds may straddle
midnight) and lists only the run directory the pointer names. The previous
run is left whole until superseded; `prune-r2 KEEP=2` then deletes it, and
runs only on a run change. Each scheduled job runs one round (`ONCE=true`,
so no runner is held for an hour); a `loop` dispatch runs rounds to twenty
past the next hour and yields through `scripts/successor_queued.sh`. The
shell treats a changed `manifestCrc32` on the pointer as a new run
(`checkForNewRun` / `resumeOnNewRun` in `main.ts`).

**Frame caches.** A feed without an archive deep enough for a whole window
(JMA tiles expire after days, the aurora feed keeps one grid, the satellite
scans are slow to list) caches each decoded frame under
`data/raw/<model>-frames/<variable>/`, mirrored on the bucket by `make
pull-r2-frames` / `push-r2-frames` / `prune-r2-frames` (`FRAME_CACHE=true`
in the rounds script). The Makefile globs key on
`<variable>/<name>_<YYYYMMDDHHMMSS>.<ext>`, and the pull spans as far ahead
of the clock as behind it (aurora frames are named for future valid times).
A showcase case can only be cut from what the cache keeps.

**The series-file path.** `SourceSpec.series_file` sources arrive as CF
NetCDF series rather than per-frame GRIB; `convert_bin` and `convert.rs`
branch on it, and `observation.py` (mirrored in `observation.rs`) ingests
them. With a `cadence_seconds`, each time is snapped to its slot and the
first slot's whole hour is the run. `observation.series_files` accepts one
file per window (radar) or one per variable (`<stem>.<variable>.nc`,
satellites, `ifshres`). The fetch stage owns every projection on this path,
so neither encoder does Gaussian or geostationary arithmetic.

**Grids.** A global grid is snapped to `360 / width` and a regional grid's
steps to the thousandth of a degree (`_snap_regional_steps`, mirrored in
`grid.rs`), since GDAL derives the step from the first and last coordinates
and some producers write the last one short. A `downsample`
(`BlockReduction` on the `GridInfo`) publishes coarser than the source,
applied in `_extract_planes` after the fill rules and before the crop. A
`regrid` resamples a projected grid (HRRR, below). `fill_values` fold
sentinels to the codebook bottom before unit conversion; the format has no
bitmap.

**Native encoder dispatch.** `native.knows_source(model)` asks the installed
wheel to convert nothing for the source's whole published bundle set; a
wheel that predates the source or one of its bundles refuses, and the
workflow takes the reference pipeline with a `::warning::`. The observation
workflows (`jma`, `cma`, `aurora`, the four satellites) resolve this way,
because a cron run cannot wait for a wheel release; `publish.yml` and
`publish-mrms.yml` pin `XUE_ENCODER=native`, so a forecast source cannot
publish on schedule before a wheel carries it. A source whose shape changes
takes a new id so an old wheel is not mistaken for a new one (`radar` →
`cma`).

## Forecasts

**gfs.** The reference source and the full bundle set (65 bundles, isobaric
families on all eight levels, the clear-air turbulence `cat300`/`cat250`/`cat200`
with its own `cat_calibration`, the GFS-Wave companion `gfswave.*` beside
`atmos/`). `tmp2m`, `prate` and `wind10m` ship a `<bundle>.series.zarr`
companion (`series_bundle_ids`; see [../zarr-profile.md](../zarr-profile.md)),
and with HRRR it is the only source with H.264 video (`SourceSpec.video`).
`XUE_GFS_BASE_URL` overrides the mirror.

**sflux.** GFS's native-resolution surface flux files on the ~13 km Gaussian
grid (3072 × 1536); `prate_ave` is a window mean de-averaged into `prate`.
`tmp2m`, `prate`, `dswrf` and `wind10m` ship series companions.

**ecmwf.** ECMWF open data; CCSDS packing is repacked to `grid_simple` with
`grib_set` at fetch time. Of the three mirrors (`XUE_ECMWF_BASE_URLS`) the S3 and
`data.ecmwf.int` ones answer bursts with 503 and are paced; Google's is not. The `wave` stream is a
companion beside `oper`. Publishes what the open data carries, in GFS order,
so a model switch keeps the layer; the gust is analysis-optional and spelled
`10fg` to 90 h, `10fg3` beyond (`ecmwf_alternate_params`). The `cat` bundles carry a fit of
their own (`cat_calibration`): on the same runs IFS's ln TI1 mean sits about
0.25 below GFS's at every level.

**aifs.** ECMWF's AIFS Single, fetched by the IFS path under another model
directory (`fetch.py::ECMWF_OPEN_DATA_MODELS`, `ifs` | `aifs-single`),
landing ~5.5 h after each cycle. Its open data encodes five fields unlike
IFS, each a `RecordAlternate` under the GFS identity: `tp` as WMO 0/1/52 in
**kg/m² (mm)** rather than local 0/1/193 in metres (the unit rule
`gdal.precipitation_accumulation_is_mm`, mirrored in `inspect.rs`), `tcc` as
WMO 0/6/1 in percent, and the three layer clouds on the ECMWF layer
boundaries (ground, 800 hPa, 450 hPa). No isobaric `r`, gust, CAPE, ice
thickness or peak wave period. `tests/test_aifs.py`.

**hrrr.** Computed on a Lambert conformal grid. `_grid_info` reads the
projection from GDAL's WKT (`reproject.py`; the wheel's `gdal_info` reports
`coordinateSystem.wkt`) and builds a `Resampler` onto a regular 0.03° grid
over the footprint; `_extract_planes` resamples each plane (bilinear, edge
cells continued into the corners the conic domain never covers) before the
crop. `reproject.rs` repeats the arithmetic and `tests/test_hrrr.py` holds
it byte-identical, so the operation order is fixed. Mirrors lag each other
per frame, so a frame comes from the first mirror that has it and a cycle is
complete only when one mirror has every hour (`XUE_HRRR_BASE_URLS`). The
shell clips to the footprint (`FORECAST_MODELS[].domain`, `web/src/domain.ts`).
`wind80m` is the 80 m U/V pair `wrfsfc` writes for wind power (0/2/2–3 on
surface 103 at 80 m). The 00/06/12/18Z cycles run to F48: `long_cycles`
lets a build that names one of them ask for `--hours 48`, while the
published axis, the `--hours` default and the live runs stay at F18.

**gefsaero.** GEFS-Aerosols, the `chem/pgrb2ap25/` `a2d_0p25` files
(`XUE_GEFS_BASE_URL`). Nine scalars: 550 nm AOD total
and by species (`aod`, `aoddust`, `aodsalt`, `aodsulf`, `aodorg`, `aodbc`;
WMO 0/20/102, entire atmosphere) and surface PM in µg/m³ (`pm25`, `pm10`,
`pm10dust`; NCEP-local 0/13/193 and 0/13/192). Every record is template
4.48: `grib2.py` / `gribindex.rs` parse it (the 24-octet shift),
`gdal.py` / `inspect.rs` match on `GRIB_PDS_PDTN` +
`GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES`, and identity is `parameter` + the
`aerosol` block, since the six species share one triple. The `.idx` spells
the species after the hour, so `VariableSpec.index_qualifier` is a second
phrase, matched case-insensitively (Google capitalises `Dust Dry`).
Codebooks are parameterised `log1p` (`PrecipitationCodebook`), pinned with
the identities by `tests/fixtures/aerosol-registry.json`.

**cfs.** NCEP CFSv2 member 01, nine-month runs on the T126 Gaussian grid
(384 × 190), 1092 frames. The bucket (`noaa-cfs-pds`, `XUE_CFS_BASE_URL`) is
the transpose of every other source: one object per variable holding the
whole run (`time_grib_01/<name>.01.<run>.daily.grb2` with an `.idx`),
written as the model runs. The fetch reads each variable as a few large
ranges and cuts them into the frame-per-file layout the converter reads; a
run is complete (`_cfs_run_is_complete`) only when every sidecar names the
F6552 record with a successor and the object is that long. The series carry
no analysis and the flux files' `:anl:` records are instantaneous, so
`first_hour = 6` (both encoders) and f000 is never fetched. The flux
quantities are declared means (`statistical_processes`) although encoded as
instantaneous. The pressure
family (`CFS_PGB_IDS`) is a companion file on its own 1° grid (360 × 181).
The shell's manifest ceiling on `forecastHours` is 8760 for this axis.
`tests/test_cfs.py`.

**ifshres.** ECMWF IFS HRES (~9 km) as Open-Meteo forwards the real-time
archive. The fetch stage is om2nc (`xuebuild/om2nccli.py`, `XUE_OM2NC`): it
reads the `.om` files under `SourceSpec.open_meteo`'s directory
(`ecmwf_ifs`) by byte range and resamples the reduced Gaussian O1280 grid
onto the regular 0.1° grid (3600 × 1801, nearest neighbour), one NetCDF per
variable. **om2nc is GPL-2.0-only**: it stays a subprocess on the PATH, and
no om dependency may reach `pyproject.toml`, `Cargo.toml` or `web/`. On the
series-file path as a forecast, the run time is the epoch in the `time`
unit and an `optional_at_analysis` variable may lack lead 0.
`VariableSpec.open_meteo` is the Open-Meteo spelling of a variable.
Precipitation is the total since the previous step, divided by the step
into `prate` (`interval_precipitation`; its input `apcp` is read, never
published). A NaN nodata is a fill (`PlaneSource.fill_nan`), which is how
sea ice thickness over land arrives. `publish-ifshres.yml` installs the
pinned om2nc release.

**woof.** A WRF-ARW run commissioned on Recast Systems' WOOF service
(gpuwm), nested 12 → 3 → 1 → 0.5 km from the GFS and read on its 500 m
innermost domain over Mount Fuji. There is no feed: `latest_filename` is
None, so the source is the first forecast that is not `fetched` (a forecast
is fetched only through a live pointer, an observation only through a
`window_hours`), it is absent from `fetch` / `build-bin`, `require_complete`
refuses it, and it is reached only through showcase cases naming a
`dataset` directory under `XUE_OBSERVATION_ROOT`
(`showcase/README.md`). The `xue wrf-series` tool (`xuebuild/wrf/`, the
`wrf` dependency group: netCDF4) owns every WRF particular — destaggering,
rotating the grid-relative wind to earth-relative, the dewpoint, the
layered cloud maxima by MSL height (0–2 / 2–6 / >6 km), differencing the
run-total precipitation into the hour's `apcp`, the 80 m wind (`U` and `V`
destaggered onto the mass points, read linearly between the two model
levels that bracket 80 m above the ground in each column, then rotated),
the sea level pressure (`PSFC` reduced hypsometrically over `HGT` with the
2 m virtual temperature warmed down the standard lapse rate to half the
height — the plain reduction, not WRF's `slp`, because a nest a few tens
of kilometres across carries it as a background field; the series is in
pascals, which the converter turns into hectopascals as it does a GRIB
record), interpolating `QCLOUD` linearly in height onto the 24 altitudes
of the `clw<m>` family (each column's mass-level heights from `PH + PHB`;
the lowest level's value between the ground and it, zero above the top,
NaN under the model terrain), and the bilinear regrid from the Lambert
conformal nest onto the regular 0.005° grid inscribed in it (79 × 61; a
coarser nest takes `--step`) — and writes `woof.<run>.<variable>.nc`, one
CF series per variable, so both encoders take the `ifshres` series-file
path unchanged. Published are eleven surface scalars and `prmsl`, the
`wind10m` and `wind80m` pairs and the `cloud3d` volume.
Frames start at f001 (`first_hour = 1`: f000 is the GFS analysis on the
nest, dropped by the tool) and `apcp` is on every frame, so nothing is
analysis-optional; `prate` is the interval rate (`interval_precipitation`).
The 24 cloud water levels ship as one volume bundle, `cloud3d`
(`VOLUME_BUNDLES`, after MRMS's `refl3d`), in g/kg on the linear `clw`
codebook; the NaN under the terrain is filled as 0 g/kg, so the volume
carries no nodata code and the viewer hides the ground by occlusion.
No ladder and no video: a plane is under 5000 cells. `tests/test_woof.py`.

## Observations

**mrms.** Frames listed off the bucket (`fetch.py::mrms_window_frames`: a
slot is kept only when every product has it) and re-keyed onto the window's
axis (`binconvert.py::_snap_observation_frames`,
mirrored in `convert.rs`). Records match under `cref` / `prate` through
`RecordAlternate`s for the MRMS-local discipline 209 (`_is_mrms_record`);
the `-999` / `-99` / `-3` sentinels are `fill_values`. `downsample` factor
2; MRMS writes its last coordinate short, hence the step snap. The
workflow's `--hours 4` covers three whole hours plus the hour in progress. `pickBundleVariant` scales the needed width by the bundle's
longitude span, so a regional grid can take its half tier zoomed out.

**mrms3d.** The same mosaic's reflectivity on its 33 constant-altitude
levels (`REFLECTIVITY_LEVELS_M`, 500 m to 19 km MSL), each a product of its
own on the bucket and an id of its own (`refl<metres>`), published as one
`refl3d` bundle (`binconvert.py::VOLUME_BUNDLES`, after the composites in
manifest order; no poster, no video). The levels are scanned every two
minutes and published every ten: `SourceSpec.object_cadence_seconds` (120)
is the bucket's interval, `cadence_seconds` (600) the axis's, and
`fetch.py::mrms_object_slot` snaps an object down to the former and keeps it
only on a multiple of the latter. A frame is therefore the scan stamped in
`[mark, mark + 120 s)`, every level from that one scan, and a mark any level
lacks is a gap; the window listing, the live slot and the completeness check
all go through it (`SourceSpec.mrms` is the dispatch key for both sources).
The levels share one GRIB element, so `_is_mrms_record` compares the level
too (template 4.0's assembled surface, or the `<metres>-GPML` short name);
the identity written is the MRMS-local 209/9/0 on surface 102, since no WMO
parameter names a reflectivity on an altitude surface. Codebooks and the
RAW predictor are `cref`'s. Thinned five to one by block maximum onto 0.05°
(1400 x 700, tile 50). A full-resolution frame is about 26 MB of GRIB and
33 planes of 7000 x 3500: the native encoder reads and thins one band at a
time, while the Python reference holds them all (fine on crop fixtures,
gigabytes on a real frame), which is why its workflow pins native.

**jma.** The JMA precipitation nowcast over Japan. The fetch is the
`jma-radar` tool (`xuebuild/jmacli.py`: `python -m jma_radar window --json`,
or `XUE_JMA_RADAR`), which reads `targetTimes_N1.json` (the last three hours
of five-minute analyses), decodes the palette tiles and writes one series
per window at 0.005°, the finest grid the zoom-8 tiles support (constants
beside `fetch.py::_fetch_jma_run`).
The agency publishes intensity classes, not dBZ, so the source ships `prate`
alone at each class's representative rate (all nine distinct codes).

**cma.** The CMA level-3 composite reflectivity mosaic over China, read out
of an archive rather than decoded live. A private sync job keeps the
six-minute mosaics as one Zarr v3 store per UTC day (240-slot `time` axis
plus `slot_status`). **Never name that job's repository or bucket** in this
repo, its docs or published metadata; the archive is named only by
`XUE_CMA_ARCHIVE` (no default; an `s3://` prefix read with the `R2_*`
credentials, or a local directory; it must be the prefix above the product
directory). `xuebuild/cmaarchive.py` reads it with zarr-python over s3fs
(the `cma` dependency group, imported nowhere else) and writes the NetCDF
series the observation ingest reads. Grid: the portal's zoom-5 plate carrée tiles
(`CMA_ZOOM`, 0.0439°, 1792 × 1024; the power-of-two step passes the snap
untouched). No frame cache. The portal is 15–20 min late and the archive
syncs every 10 min, so the live window ends 20–30 min behind real time. A
showcase case names a window by `run`, or a local file by `dataset`. The id
was `radar`; the manifest identity `CMA-RADAR` and its cases are unchanged.

**aurora.** NOAA SWPC OVATION, the percent chance aurora is visible
overhead, on a 1° global grid. The feed carries only the newest grid, so
each round adds it to the frame cache (`xuebuild/aurora.py`, one NetCDF per
five-minute slot) and `_fetch_aurora_run` assembles the cached frames. A cold cache cannot reach a whole window
back, so `observation_window_start` anchors the window at the hour of the
earliest cached frame (the pointer requires the run time to equal the run
id); the window lengthens as the cache fills. Valid times lead the clock by
about ninety minutes. `force` never clears the cache (past grids cannot be
re-fetched). Needs the `aurora` dependency group.

## Satellites

`himawari`, `goeseast`, `goeswest` and `meteosat` are series-file
observations whose fetch stage is `xuebuild/satellite/`; registry, readers,
projector, frame cache, producers, the Dust RGB and the dust confidence are
specified in [../satellite.md](../satellite.md). What the rest of the repo
must keep in mind:

- The source id is the **role** (orbital slot); the spacecraft is the
  `band` block on each variable (`SourceSpec.bands` from `Platform.bands`).
- The reference pipeline and every satellite workflow need a system GDAL
  with `gdalwarp` whichever encoder converts: the wheel's GDAL has no
  GeoTIFF driver. Workflows install `gdal-bin` and the `satellite` group.
- **Meteosat is hourly.** FCI scans every ten minutes, but EUMETSAT releases
  only the cycle on each hour as Core data under CC-BY-4.0; the cycles
  between are under a licence that does not allow this use. So
  `cadence_seconds` is 3600 against the platform's 600, the window is 24 h,
  and the attribution "Contains modified EUMETSAT Meteosat data" is
  required. It needs the `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`
  secrets (the workflow warns and builds nothing without them) and
  `hdf5plugin` for FCI's JPEG-LS filter.
- Composites (`dustrgb`, `dustcf`) are computed in the fetch stage by
  producers, not by the converter. `bundle_input_ids` asks
  `Producer.inputs_for(platform)`, and `convert.rs::composite_input_ids`
  must answer the same per source. The registry knows a producer's id
  (`VariableSpec.producer_id`), never its version, which comes from the
  series file's stamp. Code 0 is no data in every gun.
- `dustcf` ships on the three NOAA-redistributed disks only (Meteosat's
  hourly cycle is not published); its ancillaries need `ANCILLARY=true` in
  the rounds script. The CAMEL months are staged by `xuebuild/satellite/staging.py`
  (`make stage-ancillary` then `make push-r2-ancillary`; `stage-ancillary.yml`
  on the 25th and the 1st, the `staging` group, Earthdata credentials in
  `~/.netrc`, no GDAL). Its region table (one row, `global`: the whole
  granule) decides where the confidence is defined over land, which is every
  disk's land; a bad subset is fixed by restaging, which overwrites.
- `dustcf` (DEBRA) and `zhouye` (ZHOUYE, shachen ≥ 0.4.0) are two bundles
  from one producer: `Producer.bundle_ids` lists both, `PRODUCERS` keys both
  to the same object, and the fetch runs it once. A source listing only one
  still gets both frames cached; the converter reads what is listed.
- Frames are kept `FRAMES_KEEP_HOURS` (8; 26 on Meteosat): a day of seven
  variables is ~4.5 GB.
- They ship three lower rungs (`variant_factors` `(2, 4, 8)`): a
  3000 × 3000 plane does not fit the shell's cell budget zoomed out.
- `tests/fixtures/satellite-registry.json` pins variables and bundles across
  the three implementations (`tests/prepare_satellite_registry.py`).

## Adding a source

Most recent additions to copy from: `aurora` (an observation) and `cfs` (a
forecast).

1. **Registry.** A `SourceSpec` in `xuebuild/sources.py` (axis or window,
   cadence, inputs, published and core bundles, grid, tile, ladder, video,
   fetch concurrency). New variables in `xuebuild/variables.py` and their
   codebooks in `xuebuild/quantize.py` for every profile; a new family also
   regenerates its registry fixture under `tests/fixtures/`.
2. **Fetch.** The dispatch in `xuebuild/fetch.py`: frame URLs or the fetch
   tool, the completeness check, and for an observation the latest-slot
   resolver wired into `latest_observation_slot` / `resolve_run`. An
   external tool stays a subprocess (`*cli.py`) and an optional dependency
   goes in its own `pyproject.toml` group. Add the model to the CLI help in
   `xuebuild/cli.py`.
3. **Native mirror.** The same source, variables and codebooks in
   `rust/xue/src/encode/sources.rs`, `variables.rs`, `quantize.rs` (and
   `grid.rs` / `inspect.rs` / `observation.rs` for a new rule), plus a line in
   `docs/encoder.md`. Python changes first; the native side follows.
4. **Tests.** A small crop fixture (`tests/fixtures/<id>.*`, described in
   `tests/fixtures/README.md`) and `tests/test_<id>.py` running it through
   both encoders byte for byte.
5. **STAC.** Licence and provider prose in `xuebuild/stac.py::_source_prose`
   and `tests/fixtures/stac-prose.json` (`tests/test_stac.py` holds them to
   the registry).
6. **Shell.** A row in `FORECAST_MODELS` (and the `ForecastModelId` union) in
   `web/src/manifest.ts`, with `domain` / `region` for a regional grid; any
   new quantity in `web/src/variables.ts`, `identity.ts`, `palettes.ts`;
   copy in all eleven locales (`en.ts` first); the static metadata in
   `web/index.html`; `tests/web/manifest.test.ts`. Widen any manifest limit
   the source exceeds (as `forecastHours` was for CFS) in all three
   validators and `docs/format.md`.
7. **Workflow.** `publish-<id>.yml`: a forecast calls `publish.yml` and is
   added to its model allow-list; an observation copies the nearest
   rounds workflow (`publish-jma.yml` shape) with `FRAME_CACHE` set if it
   caches frames and its `Resolve the encoder` step on
   `native.knows_source`. `publish.yml` pins the native encoder, so a new
   forecast source publishes on schedule only once the wheel carries it.
8. **Discovery and docs.** `web/public/llms.txt`, the README sources table
   (with its run badge), and this page.
9. **Deploy order.** Deploy the Pages shell before publishing any data:
   an older `FORECAST_MODELS` does not know the model id. Then release the
   `xuepy` wheel carrying the source so the native path (and the pinned
   forecast workflows) can take it.
