# Sources

Engineering notes per source: what each one fetches, how its fetch stage
differs, and what the rest of the system relies on. What a source publishes
(grid, axis, cadence, variables) is in [`docs/sources.md`](../sources.md).
`xuebuild/sources.py` (`SourceSpec`) is the registry; the native encoder
reads it as `rust/xue/registry.json`, and the shell's mirror is
`FORECAST_MODELS` in `web/src/manifest.ts`. The conversion itself is in
[pipeline.md](pipeline.md), the delivery contract in
[delivery.md](delivery.md), and the satellite fetch stage in
[../satellite.md](../satellite.md).

## Overview

| id | kind | fetch stage | core |
| --- | --- | --- | --- |
| `gfs` | forecast | `.idx` byte ranges, NOAA (Google mirror) | `tmp2m`, `prate` |
| `sflux` | forecast | as GFS, surface flux files | `tmp2m`, `prate` |
| `ecmwf` | forecast | ECMWF open data, repacked from CCSDS | `tmp2m`, `prate` |
| `ifs` | forecast | ECMWF open data at 0.1°, `-sfc`/`-pl` objects reassembled | `tmp2m`, `prate` |
| `aifs` | forecast | ECMWF open data (`aifs-single`) | `tmp2m`, `prate` |
| `hrrr` | forecast (regional, on its own Lambert grid) | NOAA, two mirrors | `tmp2m`, `prate` |
| `rrfs` | forecast (regional, on its own Lambert grid), with smoke and dust | NOAA, every hour proves a run | `tmp2m`, `prate` |
| `gefsaero` | forecast (aerosol) | GEFS chem files, Google mirror | `aod` |
| `cmadust` | series-file forecast (dust, regional) | WMO WIS2 caches, NetCDF classic by byte range | `dustsfc` |
| `silam` | series-file forecast (air quality) | FMI open data on AWS, whole NetCDF-4 day files | `pm25` |
| `cfs` | forecast (seasonal) | one object per variable per run | `tmp2m`, `prate` |
| `iconeu` | forecast (regional) | DWD open data, one `.bz2` field per parameter and step | `tmp2m`, `prate` |
| `arpege` | forecast | Météo-France packages, whole objects cut by identity | `tmp2m`, `prate` |
| `cmageps` | forecast (ensemble centre) | WMO WIS2 caches, message heads walked by Range, products rewritten to plain templates | `tmp2m`, `prate` |
| `ifshres` | series-file forecast | om2nc (Open-Meteo `.om`) | `tmp2m`, `prate` |
| `woof` | series-file forecast, no feed (cases only) | `xue wrf-series` over a local WRF run | `tmp2m` |
| `efas`, `efaswb` | series-file forecast, no feed (cases only) | `xue efas-series` over an EWDS GRIB | `dis06` |
| `era5` | series-file reanalysis, no feed (cases only) | `xue era5-series` over a CDS NetCDF | `tmp2m` |
| `cams` | series-file aerosol analysis, no feed (cases only) | `xue cams-series` over an ADS NetCDF | `aod` |
| `mrms` | fetched observation | gzipped GRIB listed off the bucket | `cref` |
| `mrms3d` | fetched observation (volume) | 33 gzipped GRIBs a frame, listed off the bucket | `refl3d` |
| `jma` | series-file observation | `jma-radar` tool, frame cache | `prate` |
| `cma` | series-file observation (archived) | `cmaarchive.py` over a Zarr archive | `cref` |
| `aurora` | frame-cache observation | SWPC OVATION JSON, frame cache | `aurora` |
| `himawari`, `goeseast`, `goeswest` | satellite | `xuebuild/satellite/`, frame cache | `ir104` |
| `himawarijp` | satellite (regional box) | `xuebuild/satellite/`, the tiles over its box, frame cache of its own | `ir104` |
| `meteosat` | satellite | `xuebuild/satellite/` (EUMETSAT Data Store) | `ir104` |

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
asking the source's feed, `observation_window_start`) to the window whose last
hour holds the newest frame. The same run is rebuilt as frames arrive, so
each build takes `--round HHMM` and lands in `<model>.<run>/<HHMM>/` with a
`window.json` (`fetch.py::window_summary`). A new round is a new
directory, because nothing is overwritten under an unchanged `?v=`
([publishing.md](publishing.md)). A round is: newest frame vs
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

**Feeds.** `SourceSpec.feed` names the family a source is fetched
through, a key of `xuebuild/feeds.py::FEEDS`. Its fetcher answers the four
questions `fetch.py` asks of a source — the object of a forecast hour
(`object_url`), whether a run has landed (`is_complete`), an observation's
newest frame (`latest_slot`) and the run's download (`fetch_run`) — so
nothing in the fetch tests a source id. A family is an upstream service and
the shape of its objects: `noaa-gfs`, `noaa-sflux` and
`noaa-gefs-aerosols` share the `.idx` byte-range path under their own
object names; `noaa-hrrr`, `noaa-rrfs`, `ecmwf-open-data` (`ecmwf`, `ifs`, `aifs`),
`dwd-icon-eu`, `meteofrance-arpege` and `cmageps` are the other one-GRIB-per-hour
feeds (`FrameFeed`, whose frames `fetch_frame` writes); `noaa-cfs`,
`open-meteo`, `cmadust` and `fmi-silam` fetch series; `noaa-mrms` (`mrms`, `mrms3d`),
`jma-nowcast`, `cma-archive`, `ovation` and `satellite` (every disk) are
observation windows; `local` is a source nothing fetches (`woof`, `efas`,
`efaswb`, `era5`, `cams`). What a family needs to tell its sources apart
comes from their own fields (`platform`, `open_meteo`, `companion_files`)
or a table in the family's code (`ECMWF_OPEN_DATA_MODELS`). The families'
code stays in `fetch.py` beside the request, range and index helpers;
`feeds.py` only says which of it serves which question.

**Packing.** `SourceSpec.packing` is `published` (GDAL reads the records as
they come), `ccsds`: CCSDS/AEC (DRS template 5.42), which GDAL builds
without libaec cannot decode, or `jpeg2000` (DRS 5.40), which GDAL builds
without OpenJPEG cannot; `fetch_frame` repacks every frame of the last
two to `grid_simple` with eccodes' `grib_set` before writing it. `ecmwf`,
`ifs`, `aifs`, `iconeu` and `arpege` are `ccsds`, `cmageps` is `jpeg2000`
(and its fetch rewrites each record's template with `grib_set` too);
`assemble.group_needs_eccodes` flags every build job of a source that is
not `published` `eccodes`, which is all `publish.yml` reads to install the
tools. Both fields are the fetch's
alone: the registry document leaves them out.

**Frame caches.** A feed without an archive deep enough for a whole window
(JMA tiles expire after days, the aurora feed keeps one grid, the satellite
scans are slow to list) caches each decoded frame under
`data/raw/<model>-frames/<variable>/`, mirrored on the bucket by `make
pull-r2-frames` / `push-r2-frames` / `prune-r2-frames` (`FRAME_CACHE=true`
in the rounds script). The Makefile globs key on
`<variable>/<name>_<YYYYMMDDHHMMSS>.<ext>`, and the pull spans as far ahead
of the clock as behind it (aurora frames are named for future valid times).
A frame is one object: whatever a frame needs said about it (a satellite
frame's packing and provenance) goes inside the file, never in a sidecar
beside it, since every object costs a write on push and a delete on
prune.
A showcase case can only be cut from what the cache keeps.

**The series-file path.** `SourceSpec.series_file` sources arrive as CF
NetCDF series rather than per-frame GRIB; `convert_bin` and `convert.rs`
branch on it, and `observation.py` (mirrored in `observation.rs`) ingests
them. With a `cadence_seconds`, each time is snapped to its slot and the
first slot's whole hour is the run. `observation.series_files` accepts one
file per window (radar) or one per variable (`<stem>.<variable>.nc`,
satellites, `ifshres`). The fetch stage owns every projection on this path,
so neither encoder does reduced-Gaussian or geostationary arithmetic.

**Grids.** A global grid is snapped to `360 / width` and a regional grid's
steps to the thousandth of a degree or, failing that, the arc-minute
(`grid.snap_regional_steps`, mirrored in `grid.rs`), since GDAL derives the step
from the first and last coordinates and some producers write the last one
short; the arc-minute also keeps the two encoders on one double for EFAS's
1/60°, which `gdalinfo -json` prints short of its seventeen digits. A `downsample`
(`BlockReduction` on the `GridInfo`) publishes coarser than the source,
applied in `_extract_planes` after the fill rules and before the crop. A file on
a Lambert conformal projection is published as it is: `GridInfo.projection`
replaces the regular fields, the bundle is schemaVersion 4, and
`grid_relative_winds` turns its wind pairs next to `derive_vector` (HRRR, below).
A regular Gaussian grid keeps the regular fields GDAL reports, and `gaussian_n`
adds what its rows really are (`GridInfo.gaussian`, the `grid.gaussian` block and
the store's `latitude` coordinate). `docs/encoder.md`, "Native grids", has the
arithmetic. `fill_values` fold
sentinels to the codebook bottom before unit conversion; the format has no
bitmap.

**Native encoder dispatch.** `native.knows_source(model)` asks the installed
wheel to convert nothing for the source's whole published bundle set; a
wheel that predates the source or one of its bundles refuses, and the
workflow takes the reference pipeline with a `::warning::`. The observation
workflows (`jma`, `cma`, `aurora`, the four satellites) resolve this way,
because a cron run cannot wait for a wheel release; the forecast
workflows pin the native encoder, so a forecast source cannot publish on
schedule before a wheel carries it. A source whose shape changes
takes a new id so an old wheel is not mistaken for a new one (`radar` →
`cma`).

## Forecasts

**gfs.** The reference source and the full bundle set (79 bundles, isobaric
families on all eight levels, the clear-air turbulence `cat300`/`cat250`/`cat200`
with its own `cat_calibration`, the GFS-Wave companion `gfswave.*` beside
`atmos/`, read for the combined sea, the wind sea and the first swell
partition). The swell partitions sit on NCEP's local surface 241, an ordered
sequence whose value is the rank; GDAL spells it `1-RESERVED(241)`, so the
matcher reads the rank off the assembled template. `tmp2m`, `prate` and `wind10m` ship a `<bundle>.series.zarr`
companion (`series_bundle_ids`; see [../zarr-profile.md](../zarr-profile.md)).
`XUE_GFS_BASE_URL` overrides the mirror.

**sflux.** GFS's native-resolution surface flux files on the ~13 km Gaussian
grid (3072 × 1536, `gaussian_n` 768); `prate_ave` is a window mean de-averaged into `prate`.
`tmp2m`, `prate`, `dswrf` and `wind10m` ship series companions.

**ecmwf.** ECMWF open data; CCSDS packing is repacked to `grid_simple` with
`grib_set` at fetch time. Of the three mirrors (`XUE_ECMWF_BASE_URLS`) the S3 and
`data.ecmwf.int` ones answer bursts with 503 and are paced; Google's is not. The `wave` stream is a
companion beside `oper`. Publishes what the open data carries, in GFS order,
so a model switch keeps the layer; the gust is analysis-optional and spelled
`10fg` to 90 h, `10fg3` beyond (`ecmwf_alternate_params`). The `cat` bundles carry a fit of
their own (`cat_calibration`): on the same runs IFS's ln TI1 mean sits about
0.25 below GFS's at every level.

**ifs.** The same cycle's records as `ecmwf` at the model's full 0.1°
regular grid (the `0p1` objects beside the `0p25` ones, published since
the 2026-10-20 06z cycle), landing about two hours after the 0.25° set.
The registry entry is `ecmwf`'s — inputs, companions, bundles, axis rules
and, pending a refit on 0.1° runs, the `cat_calibration` — at `ifshres`'s
grid, tile and two-rung ladder. What differs is the fetch: at 0.1° the
`oper` stream is split into a `-sfc` object (every surface and soil
record) and a `-pl` one (the pressure levels), and the `wave` stream is
one object suffixed `-sfc` (`fetch.py::EcmwfOpenData`, `split`). The
fetcher picks each record's object by its level type
(`ecmwf_object_part`), loads each `.index` once per frame and assembles
one GRIB per step in source order, so the converter sees the same frame
as for `ecmwf`; a run is complete only when all three objects are at
both ends. The 06/18 cycles reach 144 h here (90 h at 0.25°,
`short_cycle_hours`), so `latest` skips them only past that.

**aifs.** ECMWF's AIFS Single, fetched by the IFS path under another model
directory (`fetch.py::ECMWF_OPEN_DATA_MODELS`, `aifs-single` beside `ifs`),
landing ~5.5 h after each cycle. Its open data encodes five fields unlike
IFS, each a `RecordAlternate` under the GFS identity: `tp` as WMO 0/1/52 in
**kg/m² (mm)** rather than local 0/1/193 in metres (the
`precipitation-accumulation` rule's millimetre spellings, mirrored in
`inspect.rs`), `tcc` as
WMO 0/6/1 in percent, and the three layer clouds on the ECMWF layer
boundaries (ground, 800 hPa, 450 hPa). No isobaric `r`, gust, CAPE, ice
thickness or peak wave period. `tests/test_aifs.py`.

**hrrr.** Computed on a Lambert conformal grid and published on it, 1799 × 1059
cells cut into 64 × 64 tiles. `grid.grid_info` reads the projection from GDAL's
WKT (`reproject.py`; the wheel's `gdal_info` reports `coordinateSystem.wkt`). The
records' winds are along the grid's axes (`grid_relative_winds`), so
`_quantize_file` turns every vector pair to east and north. `tests/test_hrrr.py`
holds the two encoders identical, so the operation order is fixed. Mirrors lag each other
per frame, so a frame comes from the first mirror that has it and a cycle is
complete only when one mirror has every hour (`XUE_HRRR_BASE_URLS`). Runs and
cases published before the native grid are resampled onto a regular 0.03° grid,
and the shell clips those to the footprint (`FORECAST_MODELS[].domain`,
`web/src/domain.ts`), by the bundle's metadata, never the id.
`wind80m` is the 80 m U/V pair `wrfsfc` writes for wind power (0/2/2–3 on
surface 103 at 80 m). The 00/06/12/18Z cycles run to F48: `long_cycles`
lets a build that names one of them ask for `--hours 48`, while the
published axis, the `--hours` default and the live runs stay at F18.

**rrfs.** NOAA's Rapid Refresh Forecast System, operational since the 2026-11-03
12Z cycle (SCN 26-48), from its NODD bucket (`noaa-rrfs-ops-pds`,
`XUE_RRFS_BASE_URL`, also holding the parallel feed back to 2026-08-12). The
deterministic run's CONUS output is on HRRR's grid to the last digit of the
origin, so projection, tile and `grid_relative_winds` are HRRR's and no
projection code is new. The 3 km North America output (polar stereographic)
carries only eighteen records and the 13 km one is on a rotated grid neither
encoder reads. A frame comes from two objects, `2dfld` and the `prslev`
companion, about 47 MB of ranges together, and the bucket fills them out of
order, so a run is complete only when every hour of both is there with its
`.idx`. RRFS-SD is no separate product: smoke and dust are records of `2dfld`.
Near-surface concentrations are template 4.48 `MASSDEN` (0/20/0) at 8 m in
kg/m³, hence the `mass-density-to-micrograms` rule, smoke as particulate organic
matter under 2.5 µm (62010) and dust as two size cuts (62001). Total PM2.5 and
PM10 are hour means in template 4.46, whose records GDAL names `unknown` and no
matcher reads, so they are not published; column masses (`COLMD`) would need a
second rule. `AOTK` is template 4.0 on surface 200 with no species or wavelength,
so it is `aotk` rather than `aod`. Three spellings differ from HRRR's: `PRATE`
has a maximum beside it (excluded), `REFC` is WMO 0/16/5 on surface 200 (an
alternate), and the sea level pressure is `MSLET`, accepted under `prmsl` as
`MSLMA` is. `tests/test_rrfs.py`.

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
the identities by the registry snapshots (`rust/xue/registry.json`,
`web/src/registry.ts`).

**cmadust.** CMA-CW v1.0/DUST, the China Meteorological Administration's
sand and dust storm forecast for the WMO SDS-WAS Asian Center, exchanged
through WMO WIS2 as `core` data (WMO Unified Data Policy: free and
unrestricted). One NetCDF per lead time,
`Z_NAFP_C_BABJ_<YYYYMMDDHH>0000_P_CEMC-CMA-CW-DUST_15KM_ASIA-<HHH>00.nc`
(the last five digits are hours × 100), 105 per run, about 112 MB each,
landing about 07:15–07:30Z (00Z) and 19:15–19:20Z (12Z); the caches keep a
few days. Two WIS2 global caches carry them and **neither is complete on
its own**: the Met Office/NOAA cache (`XUE_CMADUST_NOAA_CACHE_URL`, the
name under a fixed prefix) and JMA's (`XUE_CMADUST_JMA_CACHE_URL`, the name
under a directory of the same name and an upload stamp, so a key is known
only from a listing). `xuebuild/cmadust.py` lists the run's prefix on
both, ignores keys that are not a whole `.nc` (partial uploads carry a
numeric suffix), and reads each lead time from the first cache that lists
it, NOAA first; a run is complete when the two listings together name
every lead time (`fetch.py::_run_is_complete`), and a lead time neither
names fails the fetch rather than leaving a gap. A file is NetCDF3
classic, uncompressed: one Range read of 16 KB holds the header (about
2 KB, parsed with `struct`; a longer one is read again at the length the
parse asks for) and the `lon` / `lat` coordinates behind it, which are
checked against the grid; then one Range read per wanted surface field
(`sconc_dust` → `dustsfc`, `aod550_dust` → `aoddust`, big-endian float32,
1 MB each). The four pressure-level fields, nearly all of the file, are
never read. A file whose header describes another length than the listed
size is a cut-off upload and falls back to the other cache. Each variable's
run becomes one NetCDF classic series, `cmadust.<run>.<id>.nc`, written by
the same module (no netCDF4): `time` in hours since the cycle,
`forecast_reference_time` beside it, latitudes ascending as the file has
them (GDAL turns them north-up), a NaN fill. The grid is the model's own,
835 × 301 cells of 0.15° from 25°E to 150.1°E and 15°N to 60°N, published
as is: a tile of 56 × 43 cells, no ladder. `dustsfc` is a
quantity of its own, every particle size the model carries (not
`pm10dust`, GEFS-Aerosols' dust under 10 µm): WMO 0/20/0 on the ground with
a dust `aerosol` block of no size or wavelength interval, a `log1p`
codebook to 50,000 µg/m³ (a storm core passes 30,000). `aoddust` keeps
the GEFS-Aerosols identity and codebook, maximum 5, although CMA-CW's dust
optical depth exceeds 5 over strong dust events: 5–15 is common in a storm
core, with occasional isolated single-cell spikes up to about 1500. Those
cells take the overflow code, which the map draws at the top colour and a
point read answers as null. Negative surface concentrations from the model
are coded 0. `tests/test_cmadust.py`.

**silam.** SILAM v6.1, the Finnish Meteorological Institute's global
air-quality forecast, from FMI's AWS open data bucket
(`XUE_SILAM_BASE_URL`, `https://fmi-opendata-silam-surface-netcdf.s3.amazonaws.com`,
CC BY 4.0). One NetCDF-4 file per field and forecast day,
`global/<YYYYMMDD>/silam_glob_v6_1_<YYYYMMDD>_<FIELD>_d<N>.nc`, `N` 0–4,
24 hourly frames each from +1 h of the day, about 35 MB of deflated HDF5;
the whole run lands at once, about 05:07Z, and the bucket keeps a few
days. The bucket carries total PM2.5 and PM10 (and gases), no dust of its
own. `xuebuild/silam.py` lists the folder (a run is complete when it holds
every published field's day files through the horizon), fetches each day
file whole and reads it with netCDF4, the `silam` dependency group:
`publish.yml` syncs it in the build job for this model only. Each file is
held to the product: the `lat` / `lon` of the 0.2° grid (float32, so the
tolerance is 1e-3°), a time axis in seconds since a UTC instant (FMI counts
from two days before the folder's date; only valid times are compared),
the field in `ug/m3` and float32. Both the `_FillValue` and the
`missing_value` become NaN. The file's longitudes run -179.8° to 180°; the
fetch rolls the 180° column to the west edge, so the series run from -180°
like the GFS grid. Each variable's run becomes one series,
`silam.<run>.<id>.nc`, written by `cmadust.write_series` with this
product's names and unit: 570 MB a variable, so `publish-silam.yml` builds
the two bundles in two jobs. Every value is the hour's mean ending at its
time, a 3-hourly frame included, so no statistical process is declared,
and there is no analysis: the axis starts at F001. The grid is 1800 × 897
cells, tile 60 × 69 (30 × 13 tiles, no clipped edge) and the half tier.
The quantities, identities and codebooks are GEFS-Aerosols' `pm25` and
`pm10`. `tests/test_silam.py`.

**cfs.** NCEP CFSv2 member 01, nine-month runs on the T126 Gaussian grid
(384 × 190, `gaussian_n` 95, on the surface family only), 1092 frames. The bucket (`noaa-cfs-pds`, `XUE_CFS_BASE_URL`) is
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

**iconeu.** DWD's ICON-EU, the European nest of ICON, on its regular 0.0625°
grid (1377 × 657, centres 23.5°W to 62.5°E and 29.5°N to 70.5°N), so
nothing is resampled and the power-of-two step passes the regional snap
unchanged; the shell has a `region` for it and no `domain`. DWD's open data
server (`XUE_DWD_BASE_URL`) writes one bzip2-compressed, CCSDS-packed GRIB2
field per parameter and step under `icon-eu/grib/<HH>/<param>/`, with no
index, and keeps only about a day of cycles. The fetch names each field by
the registry's `dwd_param` (`VariableSpec.dwd_param`, the document's
`dwdParam`), decompresses and joins a frame's fields in source order and
repacks the frame to grid_simple with eccodes, as for the ECMWF open data;
a run is complete when every input is there at both ends. The 00/06/12/18Z
cycles run to F120 and land about 3.5 h after the cycle (the 03/09/15/21Z
ones stop at F30 and are not published). A basic set: `TOT_PREC` is the
run total as the WMO 0/1/52 in kg/m² (AIFS's `tp` alternate), `VMAX_10M`
the maximum gust over the step on the 10 m surface (ECMWF's gust
alternate; an empty interval at the analysis, so optional there) and
`CLCT` the WMO 0/6/1 in percent on the ground surface (AIFS's `tcdc`
alternate) and the terrain `HSURF` the geometric height 0/3/6 in metres
(the `orog` alternate beside ECMWF's geopotential), one time-invariant
object per cycle (`icon-eu_europe_regular-lat-lon_time-invariant_<run>_HSURF`)
fetched at the analysis alone. `tests/test_iconeu.py`.

**arpege.** Météo-France's ARPEGE on its 0.25° GLOB025 grid, the GFS grid,
from the data.gouv.fr dataset "Paquets Arpège - Résolution 0,25°"
(Licence Ouverte 2.0). A cycle's surface package `SP1` is four multi-field
GRIB2 objects on an OVH S3 bucket (`XUE_METEOFRANCE_BASE_URL`), one per
range of steps (`000H024H`, `025H048H`, `049H072H`, `073H102H`, about
740 MB together), CCSDS packed, parameter-major, with no index. The fetch
downloads the packages the frames still to cut reach into `SP1/` under the
run's raw directory (which the converter's frame listing does not
descend), indexes each package's records (`grib2.index_messages` zipped
with `grib2.message_spans`) and cuts each frame by the records' identities
(the header matcher's own test) and valid time, then repacks it like any
CCSDS frame; a run is complete when every package its axis reaches is on
the bucket, since an object appears whole. Every build job downloads the
whole packages, so `publish-arpege.yml` asks for two jobs. Every cycle runs
to F102, hourly to 48 and three-hourly after; the 00/12Z packages land about
3–4 h after the cycle, the 06/18Z ones about 5 h. The packages carry no
precipitation total at the analysis, and an accumulated source differences
its first step against the analysis plane, so the fetch writes that record:
the first step's `tp` restamped to an empty interval at the run time with
every value zero (`eccodescli.zero_accumulation_at_analysis`, `grib_set -d
0 -s endStep=0`) — the shape ICON-EU's `TOT_PREC` has at step 0. Same
encodings and basic set as `iconeu`; the gust is the maximum over the hour
ending at each step, three-hourly steps included. The terrain (0/3/6 in
metres, as DWD's) is in no `SP1` package: the first `SP2` package
(`000H024H`, about 430 MB) writes it once, at the analysis, so the fetch
walks that object's messages over Range, a 512-byte head each
(`grib2.message_head`), and reads the one matching record whole; a run is
complete only once that package is on the bucket too. `tests/test_arpege.py`.

**cmageps.** CMA GRAPES GEPS v1.3, the China Meteorological
Administration's global ensemble (30 perturbed members and the control), as
WMO WIS2 exchanges it (`urn:wmo:md:cn-cma:data.core.weather.prediction.forecast.medium-range.probabilistic.global`,
`core` data). CMA exchanges no members: one GRIB2 file per lead time,
`Z_NAFP_C_BABJ_<YYYYMMDDHH>0000_P_CMA-WIPPSGEPS-GLB-<HHH>.grib2`, about
80 MB of JPEG2000-packed **ensemble products**, three-hourly to +78 h and
six-hourly to +360 h; only the six-hourly files are read (61 frames). A
file's 87 messages are the mean and spread (template 4.2,
`derivedForecast` 0 and 4) of the 500 hPa height and the sea-level
pressure; the 0/25/50/75/100th percentiles (4.6) of the 2 m and 850 hPa
temperature, the 10 m, 850 hPa and 250 hPa wind speeds, the instantaneous
10 m gust (`i10fg`, 0/2/22 at 10 m), the total cloud cover, the CAPE and
two wind shears; the percentiles (4.10) of the precipitation over the six
hours ending at the step and over a longer window, and of the six-hour
snowfall; and exceedance probabilities (4.5, 4.9). Only the centre is read:
the spread, the other percentiles and the probabilities need an ensemble
statistic the format does not carry yet. `xuebuild/cmageps.py`
publishes the ensemble's centre under the deterministic identities: the
mean where it is the only statistic (height, pressure), the median
elsewhere, and for the precipitation the median of the window that ends at
the step and starts at the axis's previous step. The files have no index,
so a frame is a walk of the message heads over Range (512 bytes each:
section 0 gives the message's length, sections 1 and 4 its identity),
stopping once every input is found — about eighty reads — and one Range
read of each record. Each record is then rewritten with `grib_set`
(`eccodescli.set_keys`) to the plain template its identity is matched on
(4.2 and 4.6 → 4.0, 4.10 → 4.8, eccodes carrying the interval and the
statistical process across), the ground surface's missing value is written
as 0, and the gust is moved from 10 m to the ground surface, the identity
GFS's instantaneous gust has (`SURFACE_ALIASES`); the frame is then
repacked to `grid_simple` (`packing="jpeg2000"`). GDAL names the
rewritten six-hour total `APCP06`, which is the `surface` rule the
registry's `apcp` carries; it is the interval-precipitation path, as on
Open-Meteo's series, but on GRIB frames: the rate is the total over six.
The analysis file has no precipitation, and its gust and cloud cover are
fields of zeros, so all three are `optional_at_analysis` and never read
at +0. The grid is the 0.25° grid without the pole rows (1440 × 720, first
centre 89.875°N, 0°E), which the global snap takes as it is; tile 48 × 48
(30 × 15, no clipped edge), the half tier. The caches are data
(`cmageps.CACHES`, `xuebuild/wis2.py`): the Met Office/NOAA one and JMA's
are listed, and a run is complete when their listings together name
every lead time (neither does on its own, and the NOAA one carries
partial uploads under a numeric suffix, which are skipped); CMA's own
cache (`XUE_CMAGEPS_CMA_CACHE_URL`) cannot be listed but serves a file by
Range under the UTC day it arrived, so the cycle's day and the next are
tried, last, as a fallback (it keeps files for days, KMA's and DWD's,
which work the same way, about one). The caches take a run in batches: a
00Z run's leads to about +72 h at 05:20Z and the rest at 18:04Z, a 12Z
run's between 18:01Z and 19:01Z (20:01Z some days, a few leads now and
then held to 03:02Z the next day). `tests/test_cmageps.py`.

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
(a `volume` in `bundles.py`, after MRMS's `refl3d`), in g/kg on the linear `clw`
codebook; the NaN under the terrain is filled as 0 g/kg, so the volume
carries no nodata code and the viewer hides the ground by occlusion.
No ladder: a plane is under 5000 cells. `tests/test_woof.py`.

**efas, efaswb.** The Copernicus Emergency Management Service's European
Flood Awareness System (EFAS v5, LISFLOOD, run by ECMWF): `efas` is the
forecast driven by the ECMWF high-resolution run (`EFAS-v5` / `hres`,
00Z and 12Z cycles), `efaswb` the water balance, the same model forced by
observed meteorology that the forecasts start from (`EFAS-v5-WB` /
`water_balance`). Both are on the EFAS grid, 4530 × 2970 cells of 1/60°
from 25.25°W to 50.25°E and 22.75°N to 72.25°N (cell centres half a step
in). The Early Warning Data Store releases them thirty days after the
cycle, so there is **no live feed**: as for `woof`, `latest_filename` is
None, nothing fetches them, `require_complete` refuses them, and a run is
requested by hand (`ewds.climate.copernicus.eu`, one queued request per
dataset) and reached only through a showcase case naming the `dataset`
directory. The `xue efas-series` tool (`xuebuild/efas/`, the `efas`
dependency group: the eccodes bindings with `eccodeslib`, and netCDF4;
`uv sync --group efas --inexact`) owns every EFAS particular. It reads one
GRIB with eccodes — every message `dis06`, WMO 1/0/7 "discharge from
rivers or streams" on the ground or water surface, product template 4.72
with statistical process 0 over 6 h, which GDAL decodes without any of
its metadata — and holds each message to that identity and the grid.
A forecast's run is the messages' reference time and each frame the end of
its six-hour interval. The water balance has no cycle: `--start` names
the run, and each message is valid at its own validity date or, where the
store wrote no real date, six hours after the previous one from the start.
Frames are kept on the source's axis, f006 … `--hours` (120 by default).
A cell off the rivers is the GRIB's bitmap; eccodes would hand it back as
the message's `missingValue`, 9999, which a large river's discharge
reaches, so the reader sets `missingValue` to the series fill −9999
(`EFAS_SERIES_FILL`) first. Beside the discharge it derives two
fields, stamped `producer_id = xue`, `producer_version = 1`
(`PRODUCER_VERSION`): the **return-period class** `disrp` of each frame
against EFAS v5.1's flood thresholds
(`flood_threshold_efas_v5.1_rl_{1.5,2.0,5.0,20.0}.nc` from the CEMS
auxiliary data page, `--thresholds` is their directory) — 1 below the
1.5-year level, then 2, 3, 4, 5 at or above the 1.5-, 2-, 5- and 20-year
levels — evaluated, as EFAS evaluates it, only where the upstream area
exceeds 50 km² and the 1.5-year level 1 m³/s, the fill elsewhere; and the
**upstream area** `upstream` from the v5 static map (`ec_upArea`, m² with
no unit attribute, written in km²), the same plane on every frame so a
viewer reads it at any frame and the delta codec makes the repeats nearly
free. It writes `<model>.<run>.<variable>.nc`, one CF series per variable
in `write_series`'s shape, so both encoders take the series-file path
unchanged. The whole grid by default (a 120 h HRES run is 159 MB of
`dis06`, 36 MB of `disrp` and 368 MB of `upstream`); `--bbox` keeps only
the cells whose centres lie inside, which the converter accepts because a
series-file build takes its grid from the file and holds it to
`production_grid` only when a complete run is required. The identities are
`dis06` 1/0/7 on surface 1 with `typeOfStatisticalProcessing: 0`
(`statistical_processes`), log codebook (0.1 m³/s trace, 10 knee,
100 000 top); `disrp` 1/192/1 under producer `xue`, a class codebook 0–5
whose code 0 is "not evaluated";
`upstream` 1/192/2 under producer `xue`, log codebook (1 km² trace, 100
knee, 10⁷ top) — pinned by the registry snapshots. The series carry
no NaN: every series declares `_FillValue` −9999 and every river
variable names it as its one `fill_values` entry with `value_range[0]` 0,
so both encoders fold it to the codebook bottom twice over, through the
file's declared fill (`observation._plane_source`) and the registry's
(`binconvert._fill_missing`), as the soil layers' 9999 is folded to "no
soil": no river is zero discharge and zero area, and an unevaluated cell
class 0, apart from every class. No ladder: a case
is a box of a few thousand cells. The STAC prose carries the CEMS-FLOODS
licence, the credit "Contains modified Copernicus Emergency Management
Service information 2024" and the licence's caveat that EFAS data are for
information only and do not constitute a flood warning.
`tests/test_efas.py`.

**era5.** ECMWF's ERA5 reanalysis for the Copernicus Climate Change
Service (`ERA5` / `reanalysis-single-levels`): what the weather was, hourly
on the 0.25° regular grid, for a case that should show the observed
situation rather than one forecast run. A reanalysis has no cycle and the
Climate Data Store releases a day about five days late (ERA5T, replaced by
the final ERA5 two to three months later), so there is **no live feed**:
as for `efaswb`, a window is named by its first hour (`--start`, any hour,
`cycle_hours` 1), and the axis is hourly from F0, every hour an analysis,
to at most 1,440 hours (sixty days), so one case can hold a whole
episode that lasts weeks. The `xue era5-series` tool (`xuebuild/era5/`, the
`era5` dependency group: cdsapi and netCDF4, `uv sync --group era5
--inexact`) asks the store for `reanalysis-era5-single-levels` in NetCDF
over the `--bbox` padded by a cell (whole days; the key is `CDSAPI_KEY` or
the `key:` line of `~/.cdsapirc`, which one Copernicus account shares
across the CDS and EWDS) and saves the answer beside the series, or reads
one given with `--input`. The store sends a zip of one file per step type
when a request mixes them: `data_stream-oper_stepType-instant.nc` (`t2m`,
`d2m`, `u10`, `v10`) and `…-max.nc` (`fg10`), all on `valid_time` in
seconds since 1970 with latitudes north first. The tool holds every field
to the 0.25° grid, keeps the hours of the window and the cells whose
centres lie in the box, turns the two temperatures from kelvin into degrees
Celsius and writes `era5.<start>.<variable>.nc` in `write_series`'s shape,
so both encoders take the series-file path unchanged and learn nothing
ERA5. `fg10` is the 3 s gust maximum over the hour ending at the frame
("since previous post-processing", hourly in ERA5): published as `gust`
with `typeOfStatisticalProcessing: 2`, the way ECMWF's open-data gust is.
No ladder: a case is a box of a few hundred cells. The STAC
prose carries CC-BY-4.0, the credit "Contains modified Copernicus Climate
Change Service information" and the dataset's DOI as `cite-as`.
`tests/test_era5.py`.

**cams.** The Copernicus Atmosphere Monitoring Service's global
atmospheric composition system at ECMWF (`CAMS` /
`global-composition-analysis`): the IFS with aerosol and chemistry,
assimilating satellite aerosol optical depth and emitting fires from GFAS,
on the 0.4° regular grid, for a case that should show where smoke or dust
was rather than where one forecast run sent it. It publishes
GEFS-Aerosols' own quantities (`aod`, `aodorg`, `pm25`), so the shell and
both encoders know them already; CAMS's organic matter optical depth is
the component `aodorg` names. The Atmosphere Data Store holds the system's
analyses and forecasts from 2015 (`cams-global-atmospheric-composition-forecasts`)
and no reanalysis reaches the present (EAC4 trails by a year or more), so
the tool asks for every 00Z and 12Z cycle at +0, +3, +6 and +9 h: the
frames run back to back every three hours, each the newest word the
system had at its valid time, and where two cycles reach the same hour the
shorter lead is kept. **No live feed**, as for `era5`: a window is named by
its first hour (`--start`, any 3 h mark, `cycle_hours` 3) and runs to at
most 1,440 hours. `xue cams-series` (`xuebuild/cams/`, the `era5`
dependency group; the same account key as the CDS, but the ADS's own terms
must be accepted once on its site) asks for the window as one date range
over the `--bbox` padded by a cell, saves the store's `netcdf_zip` beside
the series, or reads one given with `--input`. The store's `data_sfc.nc`
lays every field out (forecast_period, forecast_reference_time, latitude,
longitude), latitudes north first, coordinates with float noise the tool
rounds off; PM2.5 comes in kg m**-3 and is written in µg/m³, the optical
depths as `1`, and the series reuse ERA5's `Window` and `write_series`.
The codebooks are GEFS-Aerosols' (optical depth to 5, PM2.5 to
1000 µg/m³), so the core of a dense plume is drawn at the top of the scale.
No ladder. The STAC prose carries CC-BY-4.0, the credit
"Contains modified Copernicus Atmosphere Monitoring Service information"
and the dataset's DOI as `cite-as`. `tests/test_cams.py`.

## Observations

**mrms.** Frames listed off the bucket (`fetch.py::mrms_window_frames`: a
slot is kept only when every product has it) and re-keyed onto the window's
axis (`discover.py::snap_observation_frames`,
mirrored in `convert.rs`). Records match under `cref` / `prate` through
`RecordAlternate`s for the MRMS-local discipline 209 (the `template-mrms`
rule in their `gdal_rules`), and under `rqi` through its own MRMS-local
identity, as the volume's levels do;
the `-999` / `-99` / `-3` / `-1` sentinels are `fill_values`. The quality
index is stamped on the mark like the rate, so it shares the composite's
slots; its codebook starts a tenth below 0 so the filled no-coverage cells
are a code of their own. `downsample` factor
2; MRMS writes its last coordinate short, hence the step snap. The
workflow's `--hours 4` covers three whole hours plus the hour in progress. `pickBundleVariant` scales the needed width by the bundle's
longitude span, so a regional grid can take its half tier zoomed out.

**mrms3d.** The same mosaic's reflectivity on its 33 constant-altitude
levels (`REFLECTIVITY_LEVELS_M`, 500 m to 19 km MSL), each a product of its
own on the bucket and an id of its own (`refl<metres>`), published as one
`refl3d` bundle (a `volume` in `bundles.py`, after the composites in
manifest order; no poster). The levels are scanned every two
minutes and published every ten: `SourceSpec.object_cadence_seconds` (120)
is the bucket's interval, `cadence_seconds` (600) the axis's, and
`fetch.py::mrms_object_slot` snaps an object down to the former and keeps it
only on a multiple of the latter. A frame is therefore the scan stamped in
`[mark, mark + 120 s)`, every level from that one scan, and a mark any level
lacks is a gap; the window listing, the live slot and the completeness check
all go through it (both sources are the `noaa-mrms` feed, and exactly the
sources `SourceSpec.mrms` marks).
The levels share one GRIB element, so the `template-mrms` rule compares the
level too (template 4.0's assembled surface, or the `<metres>-GPML` short
name);
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

`himawari`, `himawarijp`, `goeseast`, `goeswest` and `meteosat` are
series-file observations whose fetch stage is `xuebuild/satellite/`;
registry, readers, projector, frame cache, producers, the Dust RGB and the
dust confidence are specified in [../satellite.md](../satellite.md). What
the rest of the repo must keep in mind:

- The source id is the **role** (orbital slot); the spacecraft is the
  `band` block on each variable (`SourceSpec.bands` from `Platform.bands`).
- The reference pipeline and every satellite workflow need a system GDAL
  with `gdalwarp` whichever encoder converts: the wheel's GDAL has no
  GeoTIFF driver. Workflows install `gdal-bin` and the `satellite` group.
- **A regional box is a source of its own.** `himawarijp` is the
  `himawari` platform with `SourceSpec.region` set (24–46°N, 122–148°E)
  and a finer `grid_step`: the frames are warped onto the box, and the
  ISatSS reader fetches only the tiles whose footprint reaches it
  (`ISatSSReader.tiles_covering`). The frame cache is keyed by the source
  id (`<source>-frames`), so the box and the disk never share a frame;
  a full-disk source's id is its platform's role, so the disks' cache
  directories kept their names. `region` is mirrored in `sources.rs`.
- **Meteosat is hourly.** FCI scans every ten minutes, but EUMETSAT releases
  only the cycle on each hour as Core data under CC-BY-4.0; the cycles
  between are under a licence that does not allow this use. So
  `cadence_seconds` is 3600 against the platform's 600, the window is 24 h,
  and the attribution "Contains modified EUMETSAT Meteosat data" is
  required. It needs the `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`
  secrets (the workflow warns and builds nothing without them) and
  `hdf5plugin` for FCI's JPEG-LS filter.
- Composites (`dustrgb`, `dustcf`, `zhouye`, `truecolor`)
  are computed in the fetch stage by producers, not by the converter.
  `bundle_input_ids` asks `Producer.inputs_for(platform)`, and
  `convert.rs::composite_input_ids` must answer the same per source (the
  Dust RGB drops `ir112` on FCI, the true colour `vis051` on the ABI). The
  registry knows a producer's id (`VariableSpec.producer_id`), never its
  version, which comes from the series file's stamp. Code 0 is no data in
  every gun.
- `truecolor` is Xue's own recipe (`TrueColorProducer`, producer id
  `xue`): its version is `producers.TRUE_COLOR_VERSION`, bumped by hand
  when a recipe constant changes, because a cached slot is recomposed
  when the version differs and must not be on every package release. Its
  inputs are the solar bands for the day side, which cost a round some
  500–600 MB of downloads over the windows' 140 (the 0.5 km red band alone
  is 200–430 MB a slot), and `ir039` and `ir104` for the night side, which
  every satellite source fetches already. Reflectance frames are warped as
  block means (`fetch.resampling_for`), brightness temperatures
  bilinearly. The night constants are one set for every imager, so the
  `geo` mosaic's seams do not show after dark; only the day side's green
  differs by imager.
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
- Frames are kept `FRAMES_KEEP_HOURS` (8; 26 on Meteosat): a day of
  twenty variables on a NOAA disk is ~10.5 GB.
- They ship two lower rungs (`variant_factors` `(2, 4)`): a 3000 × 3000
  plane does not fit the shell's cell budget zoomed out. No eighth: it
  cost a quarter of a disk's bucket objects; the coarsest zoom plays the
  quarter.
- The registry snapshots (`rust/xue/registry.json`, `web/src/registry.ts`)
  pin variables and bundles across the three implementations.

## Adding a source

Most recent additions to copy from: `aurora` (an observation) and `cfs` (a
forecast).

1. **Registry.** A `SourceSpec` in `xuebuild/sources.py` (axis or window,
   cadence, inputs, published and core bundles, grid, tile, ladder,
   fetch concurrency). New variables in `xuebuild/variables.py` — the
   identity plus, where the defaults do not fit, the `conversion`,
   `gdal_rules`, `predictor`, `decimation` and `companions` fields — and
   their codebooks in `xuebuild/quantize.py` for every profile; a new
   bundle that is not a plain scalar in `xuebuild/bundles.py`. Regenerate
   `rust/xue/registry.json`, and a new family's fixture under
   `tests/fixtures/`.
2. **Fetch.** The source's `feed` (and `packing="ccsds"` if its records
   arrive CCSDS packed; see "Feeds" and "Packing" above). On a known
   service, name its family. A new service is a fetcher in
   `xuebuild/feeds.py` registered in `FEEDS` — frame URLs or the fetch
   tool, the completeness check, and for an observation the latest-slot
   resolver — over functions in `xuebuild/fetch.py`. An external tool
   stays a subprocess (`*cli.py`) and an optional dependency goes in its
   own `pyproject.toml` group. Add the model to the CLI help in
   `xuebuild/cli.py`.
3. **Native encoder.** It has no table of its own: it reads the
   regenerated `rust/xue/registry.json`. Rust changes only for a rule the
   closed sets lack (`units.rs`, `inspect.rs`, `convert.rs`,
   `observation.rs`), plus a line in `docs/registry.md` and
   `docs/encoder.md`; see the `add-source` skill.
4. **Tests.** A small crop fixture (`tests/fixtures/<id>.*`, described in
   `tests/fixtures/README.md`) and `tests/test_<id>.py` running it through
   both encoders.
5. **STAC.** Licence and provider prose in the `sources` entry of
   `xuebuild/stac_prose.json` (`tests/test_stac.py` holds it to the registry).
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
