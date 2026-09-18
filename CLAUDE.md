# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this is

Xue packs global weather forecast runs into per-variable Zarr v3 stores laid
out for playback and plays them back in a static browser page. The layout
was developed as a single-file container, `.xue`, which is no longer
published (every live run, round and case has been store-only since
2026-09-15) but is still the encoders' intermediate, the store being derived
from it, and is read by every decoder indefinitely. Three implementations of
one format live here and must stay in agreement:

- Python encoder: `xuebuild/` (fetch → GDAL extract → quantize → temporal
  residuals → zstd → container → manifest). `xue` on the Python side is the
  native binding, `xuebuild` the build pipeline.
- Rust decoder: `rust/xue` (core crate) and `rust/xue-wasm` (wasm-bindgen
  bindings, built into `web/src/wasm/`).
- TypeScript frontend: `web/src/` (manifest resolution, decode worker, WebGL2
  layers, playback).

A native port of `convert-bin` lives at `rust/xue/src/encode/`, behind the
`xue` crate's off-by-default `encoder` feature, with a PyO3 wrapper at
`rust/xue-py/` (distribution `xuepy`, imported as `xue`, carrying the decoder
too). It links GDAL, grib-rs and zstd in process and is held to the Python
encoder by byte-for-byte identical output (`docs/encoder.md`).

`xuebuild` depends on the `xuepy` wheel and converts through it by default:
`xuebuild/encoder.py` dispatches, `XUE_ENCODER` (`auto` | `native` |
`python`) overrides, and the scheduled `publish-*` workflows pin `native` so
a fallback to the slow path fails instead of passing unnoticed. The one
exception is `publish-jma.yml`: the wheel on PyPI may predate a source (a
wheel ships on the crate's tags, a cron run takes no input), so its
`Resolve the encoder` step asks `native.knows_source` and takes `native`
when the installed wheel has the model, else the reference pipeline with
GDAL and a `::warning::` on the run; a dispatch can still insist on either.
The native
encoder writes no video and no live pointer; `xuebuild/native.py` reads the
codes back out of the bundles it wrote, hands them to the ffmpeg encoder,
folds the descriptors into the manifest and then writes the pointer, whose
CRC32 covers the finished manifest. `tests/test_native.py` compares every
artifact of one build against the other byte for byte.

The Python encoder is the reference: a format change goes there first, and
the native side follows.

The `encoder` feature is off by default because it links GDAL and the
decoder does not. The two are also separated by cargo profile: `release` is
tuned for the wasm decoder, `encoder` inherits it at `opt-level = 3`.
Profiles live in `rust/Cargo.toml` because Cargo reads them from the
workspace root only.

`docs/format.md` is the normative spec. `README.md` covers usage and
publishing; `showcase/README.md` covers authoring historical cases.

## Commands

```sh
make check                       # verify GDAL / zstd / node / wasm-pack versions
make wasm                        # build the WASM decoder into web/src/wasm/ (generated, gitignored)
make mvp [MODEL=gfs|ecmwf|aifs|sflux] # check + install + wasm + build a run + vite build
make serve                       # vite preview on 127.0.0.1
npm run dev                      # vite dev server

make test                        # rust + python + web unit tests (incl. encoder parity)
make tc-build [ISSUE=YYYYMMDDHH] # the tropical cyclone product for one hour (needs eccodes' bufr_dump)
make airport-build [ROUND=YYYYMMDDHHMM]  # the airport METAR / TAF product for one ten-minute round
make sounding-build [ISSUE=YYYYMMDDHH] # the radiosonde sounding product for one hour (needs bufr_dump)
.venv/bin/python -m xuebuild build-bin --model himawari --run latest --hours 3 --round now  # one Himawari round (needs gdalwarp)
make test-rust                   # regenerates the golden fixture, then cargo test
make test-e2e                    # playwright (needs `npx playwright install chromium`)
make encoder-rust                # build the native encoder from source (needs GDAL + libclang)
make encoder-rust-test           # its unit tests plus the byte-identity golden test
make encoder-wheel               # a self-contained wheel carrying a minimal GDAL
npm run build                    # tsc --noEmit && vite build
.venv/bin/python -m xuebuild export-zarr <bundle.xue> [--delta] [--index-location start|end]
.venv/bin/python -m xuebuild stac --model gfs --run YYYYMMDDHH [--round HHMM]   # rewrite a run's STAC documents
.venv/bin/python -m xuebuild stac --product sounding|airport|tc --issue YYYYMMDDHH[MM]  # a point product's
```

Single tests:

```sh
.venv/bin/python -m unittest tests.test_bin -v
.venv/bin/python -m unittest tests.test_bin.SomeCase.test_method
cd rust && cargo test -p xue golden
npx vitest run tests/web/manifest.test.ts
npx playwright test tests/e2e/app.spec.ts -g "scrub"
wasm-pack test --headless --chrome rust/xue-wasm   # decoder in a real browser
```

Use `.venv/bin/python` (created by `uv sync`); the Makefile's `PYTHON`
variable picks it automatically, but direct invocations must not rely on the
system interpreter.

`web/src/wasm/` is generated and gitignored; the frontend will not typecheck
or build until `make wasm` has run.

## Architecture

### Delivery contract (touches all three implementations)

Two layers, both versioned:

- The only mutable object per model is a small live pointer at the data
  root (`latest.json` for GFS, `latest-<model>.json` otherwise, pointer
  schema v1). Uploading it is what takes a run live.
- Everything it names, the run's `manifest.json` (schema v5) and every
  artifact, is immutable, addressed with `?v=<crc32>` cache busting, and
  lives under `<model>.<run>/`.

Manifest paths are resolved relative to the manifest URL, so a run directory
can be served from either the site origin or the R2 bucket
(`VITE_DATA_BASE_URL`, see `web/.env.deploy`). The production hostnames
appear in the frontend only through `web/src/site.ts`.

Manifest schema changes are a two-sided deploy: the new shell accepts old
manifests, but an old cached shell rejects new ones. Deploy the Pages shell
before publishing data in a widened schema. The same rule applies to a
container version bump and to a bundle metadata version bump (below).

The set of bundles is not part of the schema. All three validators admit a
manifest's `variable` on its shape alone (`^[a-z][a-z0-9]*$`, unique), with
the dataset's own core set (`SourceSpec.core_bundle_ids`: the `tmp2m` /
`prate` pair on a forecast, `cref` on a radar mosaic) still required on a
live run. A run may publish a bundle the shell has no chart for and the
shell renders it generically. Adding a level of a known family is two
source-table lines and no frontend change; a new quantity is an encoder
registration plus, optionally, frontend chart knowledge.

Inside a bundle, `numericId` / `variableId` is a file-local handle: both
encoders number a bundle's variables 1..n in bundle order, so every scalar
bundle carries variable 1 and every vector bundle 1 (u) and 2 (v). A
variable's identity is its GRIB2 `parameter` block (`web/src/identity.ts`
derives family, level and vector-ness from it once a session is open; the id
string is only the naming convention the rail and `?type=` read before
then). Nothing may key across sessions by `numericId` alone:
`web/src/sessionkeys.ts` scopes the frame cache and probe keys by session,
and each session's worker is bound to it by closure.

### Discovery (search engines, language models, agents)

The shell describes itself in three places that must agree with what the
encoder publishes. `web/index.html` and `web/showcase.html` carry the static
English metadata, the hreflang set and a JSON-LD graph; `web/src/pagemeta.ts`
rewrites title, description, canonical and og:url once the view is known (a
case, `/?case=<id>`, is its own page; every live view is `/`; a `?lang=`
rendering is canonical to itself). `web/public/robots.txt` and
`web/public/llms.txt` are static prose. Update `llms.txt` when `sources.py`
changes what a model publishes or `urlstate.ts` gains a parameter.
`sitemap.xml` and `llms-full.txt` (README + `docs/*.md` + `showcase/README.md`,
links rewritten to the repository) are generated at build time by
`web/tooling/discovery.ts`, which is why `deploy-pages.yml` also triggers on
those documents. The sitemap's cases come from the published catalog
(`showcase.json` at the build's `VITE_DATA_BASE_URL`), never from
`showcase/cases/`, so a deploy build fails if the bucket does not answer.

### Container versions

`FixedHeader.version` says what a payload is. v1 is plane-major: one payload
is one whole plane of one frame, indexed by `IDX1` + `PlaneEntry`, with a
middle RAW anchor per temporal group. v2 is tiled: one payload is a chunk,
one spatial tile of one temporal group for one variable, indexed by `IDX2`
plus three compact tables (variables with their predictor, groups
partitioning the axis, one 8-byte entry per chunk whose offset is a prefix
sum). Physical order is group → tile (row-major) → variable, so a whole
group is one contiguous range, a viewport's tile row another, and one cell's
whole series is one chunk per group. Tiling is a storage property: the
metadata JSON, codebooks and residual arithmetic are unchanged.
`docs/format.md` §"Container v2" is normative.

Both encoders write v2 and every decoder reads both versions. A streaming
session decodes and fetches only the tiles its viewport covers
(`web/src/tiles.ts` turns the view into rectangles, the worker protocol
carries them, `u_cover` in `layer.ts` clips to what a partial plane holds),
and a pinned point reads its whole series in one round trip. A narrowed
session never fetches the rest of the grid; the data card reads "Viewport
fully buffered" for that case. A decoder must always keep reading v1:
published runs and showcase cases carry those bytes and are never rebuilt.

### Bundle metadata schema versions

Inside a `.xue` file, `schemaVersion` is the lowest version a reader must
implement. v1 and v2 are the legacy whole-hour axes (`firstForecastHour`
with `stepHours` or `hours`). v3, what the encoder writes now, adds a GRIB2
`parameter` block to every variable and makes the time axis unit-neutral
(`unitSeconds` + `firstFrameOffset` + `frameStep` | `frameOffsets`).
`unitSeconds` is 3600 for every forecast source, 360 for the radar mosaic,
120 for MRMS; it must be the coarsest unit that fits, and a decoder rejects
both an unimplemented version and an overdeclared one, so each file has
exactly one valid encoding.

v3 also admits two optional blocks beside a variable's `parameter`,
`band` (the spectral band a satellite image was measured in, GRIB2 template
4.31's fields, written from `SourceSpec.bands`) and `producer` (the
algorithm behind a composite field); both are validated when present by all
three readers, need v3 and raise no floor, and are ignored by readers that
predate them, so adding them was one-sided (`docs/format.md` §"Band and
Producer"). No source writes either yet.

Within the container a plane's key is a frame offset, not a forecast hour:
`PlaneEntry.frameOffset`, the worker protocol's `frameOffset`, and
`SourceFrame.lead_seconds` upstream of the axis derivation.

Encoder (`xuebuild/binconvert.py::build_metadata`), Python reader
(`xuebuild/binformat.py::_parse_metadata`), Rust
(`rust/xue/src/decode/metadata.rs`) and
`web/src/manifest.ts::parseBundleMetadata` must agree. This version is
distinct from the manifest's schema v5 and the pointer's v1.

### Encoder pipeline (`xuebuild/`)

`sources.py` is the per-model registry (`SourceSpec`): data location, the
published time axis as `(last_hour, step)` segments (the last boundary is
`horizon_hours`, the `--hours` default), the cycle cadence (`cycle_hours`: 6
for the global models, 1 for HRRR), the fetched inputs, the published
bundles, the production grid, and fetch concurrency. Adding or changing a
model starts here; the frontend mirror is `FORECAST_MODELS` in
`web/src/manifest.ts`. Source kinds:

- Forecast (`gfs`, `ecmwf`, `aifs`, `sflux`, `hrrr`). `aifs` is ECMWF's
  data-driven AIFS Single from the same open data service, fetched by the
  IFS path under another model directory (`fetch.py::ECMWF_OPEN_DATA_MODELS`,
  `ifs` | `aifs-single`), six-hourly to 360 h from every cycle, landing
  ~5.5 h after it (`publish-aifs.yml`). Its open data encodes five fields
  unlike the IFS — `tp` as the WMO 0/1/52 accumulated in **kg/m² (mm)**
  rather than the local 0/1/193 in metres, `tcc` as the WMO 0/6/1 in
  percent from the ground surface up, and the three layer clouds (which
  IFS open data lacks) on the ECMWF layer boundaries (ground, 800 hPa,
  450 hPa) — each a `RecordAlternate` under the GFS identity, the
  millimetre total a unit rule (`gdal.precipitation_accumulation_is_mm`,
  mirrored in `inspect.rs`) read off the alternate's unit. It carries no
  isobaric `r`, gust, CAPE, ice thickness or peak wave period, so those
  bundles are absent. `tests/test_aifs.py` holds the two-frame
  `aifs.*.crop.grib2` pair through both encoders. `hrrr` has a `regrid`: the
  model is computed on a Lambert conformal grid, `_grid_info` reads the
  projection out of GDAL's WKT (`reproject.py`; the wheel's `gdal_info`
  reports `coordinateSystem.wkt`), builds a `Resampler` onto the regular
  0.03° grid over the source footprint, and `_extract_planes` resamples
  each plane (bilinear, edge cells continued into the corners the conic
  domain never covered) before the crop. `rust/xue/src/encode/reproject.rs`
  repeats the arithmetic and `tests/test_hrrr.py` holds it byte-identical,
  so the operation order is fixed. The shell clips the raster, particles,
  probe and contour labels to the footprint (`web/src/domain.ts`,
  `FORECAST_MODELS[].domain`), and `region` is where the camera goes when a
  regional model is opened on a view showing none of it.
- Archived series-file observation (`cma`, once `radar`): the CMA level-3 composite
  reflectivity mosaic over China, `observation=True`, `series_file=True`,
  a rolling window like `jma` (`latest-cma.json`, `publish-cma.yml`
  the same shape as `publish-jma.yml` with `MODEL=cma HOURS=3` and no
  frame cache) but read out of an archive rather than decoded from tiles:
  a private sync job (never name its repository or its bucket in this
  repo, its docs or any published metadata) keeps the agency's six-minute
  mosaics as one plain Zarr v3 store per UTC day in an archive named only
  by the `XUE_CMA_ARCHIVE` environment variable (no default; an `s3://`
  prefix read with the `R2_*` credentials, or a local directory), each
  store with a complete 240-slot `time` axis and a `slot_status`, and
  `xuebuild/cmaarchive.py` reads it in process with zarr-python over s3fs
  (the `cma` dependency group, imported nowhere else; `publish-cma.yml`
  runs `uv sync --group cma`): `stored_slots` reads the two small index
  arrays of each day the window touches, `read_window` the written frames
  (one orthogonal read per day store), and `write_series` hands them to
  the converter as the NetCDF series `observation.py` has always read
  (`fetch.py::_fetch_cma_run`, `latest_cma_slot` from the last day's
  stores, `_cma_run_is_complete` when the archive has a slot at or past
  the window's end). The grid is the portal's zoom-5 plate carrée
  tile grid (`CMA_ZOOM`, 0.0439°, 1792 × 1024; the power-of-two step
  passes `_snap_regional_steps` untouched), `cadence_seconds` is 360, so
  `unitSeconds` is 360. The portal publishes fifteen to twenty minutes
  late and the archive syncs every ten minutes, so the live window
  ends twenty to thirty minutes behind real time. A showcase case names a
  window of the archive by `run`, or a local file the tool wrote by
  `dataset` (the cases cut before the archive); `showcase.py` admits
  either on a series-file source. The id changed from `radar` with the
  shape (the run directory and pointer on R2 are `cma.<run>/` and
  `latest-cma.json`), so a wheel that knows `radar` is never taken by
  `native.knows_source` for one that knows `cma`; the manifest identity
  `CMA-RADAR` and the cases built under it are unchanged.
- Fetched observation (`mrms`): a run is a window named by its first hour
  (`window_hours`, also its `--hours` default), its frames listed off the
  bucket (`fetch.py::mrms_window_frames`: one gzipped GRIB per product per
  frame, each product's stamp snapped down to its `cadence_seconds` slot, a
  slot kept only when every product has it, `fetch.json` left beside the
  frames), and the converter re-keys the frames onto the window's axis
  (`binconvert.py::_snap_observation_frames`, mirrored in `convert.rs`).
  Records are matched under `cref` / `prate` through `RecordAlternate`s for
  the MRMS-local discipline 209 (`_is_mrms_record`); the `-999` / `-99` /
  `-3` sentinels fold to the codebook bottom through `fill_values`. A
  `downsample` publishes the grid coarser than it arrives (`BlockReduction`
  on the `GridInfo`, applied in `_extract_planes` after the fill rules and
  before the crop). `_grid_info` also snaps a regional grid's steps to the
  thousandth of a degree (`_snap_regional_steps`, mirrored in `grid.rs`),
  since GDAL derives the step from the first and last coordinates and MRMS
  writes its last one short. The live feed is a rolling window: `--run
  latest` resolves (`fetch.py::latest_mrms_slot`, `resolve_run`) to the run
  whose last hour holds the bucket's newest frame, `--hours 4` covers three
  whole hours plus the hour in progress, and since the same run is rebuilt
  every five minutes each build takes `--round HHMM` and lands in
  `mrms.<run>/<HHMM>/` with a `window.json` (`fetch.py::window_summary`).
  Never overwrite an object under an unchanged `?v=`: a viewer's range
  requests against it decode the wrong bytes. `publish-mrms.yml` runs one
  round of `scripts/window_rounds.sh` per job on a five-minute cron
  (`ONCE=true`, so no runner is held for an hour; a `loop` dispatch runs
  rounds to twenty past the next hour and yields to the next scheduled
  job through `scripts/successor_queued.sh`). A round is: newest frame vs
  the live `window.json` → build → `make upload-r2 … ROUND=` →
  `prune-r2-rounds` keeps the live run's newest two rounds (in clock
  order: a round is named by its build minute and a run's rounds may
  straddle midnight) and trims every other run to its newest one,
  `prune-r2 KEEP=2` the previous run. The shell polls the MRMS pointer every two
  minutes, treats a changed `manifestCrc32` as a new run, and on a rolling
  window keeps the playhead by observation time or follows the end when it
  was at the end (`checkForNewRun` / `resumeOnNewRun` in `main.ts`).
  `pickBundleVariant` scales the needed width by the bundle's longitude
  span, so a regional grid can take its half tier on a far-out view.
- Fetched series-file observation (`jma`): the JMA precipitation nowcast
  over Japan, fetched like MRMS (a rolling window, `latest-jma.json`,
  `publish-jma.yml` the same shape as `publish-mrms.yml` with `MODEL=jma
  HOURS=3`, a `loop` dispatch polling every two minutes) but arriving as
  one NetCDF series per window
  (`SourceSpec.series_file`, also true of `cma`; `convert_bin` and
  `convert.rs` branch on it). The fetch is the `jma-radar` tool
  (`xuebuild/jmacli.py`: `python -m jma_radar window --json`, or
  `XUE_JMA_RADAR`), which reads the agency's `targetTimes_N1.json` (the
  last three hours of five-minute analyses), decodes the palette tiles and
  writes the series (`fetch.py::_fetch_jma_run`, the grid constants
  `JMA_ZOOM` / `JMA_GRID_STEP` / `JMA_BBOX` / `JMA_RESAMPLING` beside it:
  0.005°, the finest grid the zoom-8 tiles support);
  `latest_jma_slot` reads the listing itself and `latest_observation_slot`
  dispatches by source. The agency publishes intensity classes, not dBZ, so
  the source ships `prate` alone at each class's representative rate (all
  nine distinct in the codebook) and its core set is `prate`. On the NetCDF
  path a `cadence_seconds` means the window is the axis: `observation.py`
  (mirrored in `observation.rs`) snaps each time to its slot and takes the
  first slot's whole hour as the run, so `unitSeconds` is 300. The tool's
  decoded-frame cache (`data/raw/jma-frames/`, one file per frame) is
  mirrored on the bucket by `make pull-r2-frames` / `push-r2-frames` /
  `prune-r2-frames` (`FRAME_CACHE=true` in the rounds script), so the
  agency serves each frame once; the tiles expire after days and there is
  no archive, so a case can only be cut from what that cache keeps.

- Satellite (`himawari`, `goeseast`, `goeswest`, `meteosat`): Himawari-9's
  infrared windows as NOAA redistributes them (the ISatSS tiles on
  `noaa-himawari9`), GOES-19's / GOES-18's from NOAA's own buckets (the
  CMIPF full-disk files on `noaa-goes19` / `noaa-goes18`) and
  Meteosat-12's from the EUMETSAT Data Store (the FCI Level 1c chunks of
  collection `EO:EUM:DAT:0662`), each a
  `series_file` observation like `jma` / `cma` whose fetch stage is
  `xuebuild/satellite/` (`docs/satellite.md`): `platforms.py` is the
  registry (one row per spacecraft at an orbital slot, its channels,
  bucket and reader; the source id is the **role**, and the spacecraft is
  the `band` block on the variable, from `SourceSpec.bands` via
  `Platform.bands`; `Platform.region` spells a disk that crosses the
  antimeridian with the east edge past 180 — Himawari 80.7–200.7,
  GOES-West 163–283 — and GOES-East on negative longitudes, Meteosat on
  −60–60; `reference_channel` is the one a listing walks; `bucket` /
  `prefix` are a bucket and key prefix or a store's host and collection
  id), `readers.py` lists and opens a slot
  (`ISatSSReader`: 88 tiles mosaicked with `gdalbuildvrt`; `CMIPFReader`:
  one file per channel under `YYYY/DDD/HH/`, the scan start floored to
  the cadence as the slot, `recent_slots` walking hour directories newest
  first so `latest_slot` costs two to four requests; `FCIReader`: forty
  chunk files carrying every channel, so a slot's files are shared across
  channels (`Reader.files_per_channel` false) and the outermost two chunks
  at either end never fetched (`needed_chunks`), each chunk's radiance
  band read with `gdal_translate` under `HDF5_PLUGIN_PATH` from
  `hdf5plugin` (the JPEG-LS filter a stock GDAL lacks), converted to
  brightness temperature with the group's Planck coefficients read by
  `gdalmdiminfo`, and written as an Int16 strip whose VRT carries the
  geostationary SRS and extent the reader builds itself since GDAL does
  not connect the grouped grid mapping; `satellite/eumetsat.py` is the
  store: anonymous OpenSearch, a bearer token from `POST /token` with the
  `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` environment
  variables, renewed on 401 and `retryAfter` honoured on 429, and a named
  error without them), `projector.py` warps
  it onto the platform's region at `SourceSpec.grid_step`
  (`GdalWarpProjector`; 120° × 120° at 0.04°, 3000 × 3000; a scene wholly
  outside the grid is refused, since a blank warp is also a lost
  projection), `assemble.py` caches the
  frame (`data/raw/himawari-frames/<variable>/*.tif`, mirrored like the JMA
  frames but kept eight hours, `FRAMES_KEEP_HOURS`, since the agencies
  archive the scans and a day of seven variables is ~4.5 GB) and writes the window series with `gdal_translate -of netCDF`
  through a VRT carrying `NETCDF_DIM_*` metadata, `fetch.py` runs a window
  (`fetch.py::_fetch_satellite_run` in the top-level module dispatches on
  `SourceSpec.platform`). The reference pipeline and the workflow need a
  system GDAL with `gdalwarp` whichever encoder converts (the wheel's GDAL
  has no GeoTIFF driver); `publish-himawari.yml` is `publish-jma.yml` on a
  ten-minute cron with `gdal-bin` installed unconditionally, a six-hour
  window (`window_hours=6`, `HOURS=6`) and the `satellite` dependency
  group, `publish-goeseast.yml` / `publish-goeswest.yml` are it with
  `MODEL` swapped, and `publish-meteosat.yml` adds the two EUMETSAT
  secrets (warning and building nothing without them), `HOURS=24`, and a
  step that proves the runner's GDAL reads the fixture chunk through the
  plugin. **Meteosat is hourly**: the imager scans every ten minutes, but
  the source's `cadence_seconds` is 3600 against the platform's 600
  (`window_slots` / `latest_slot` keep the slots on the source's cadence,
  `source_cadence` / `on_cadence` in `satellite/fetch.py`) and its window
  24 h, because the EUMETSAT data policy releases the Level 1 cycle on
  each hour as Core data under CC-BY-4.0 and the cycles between under a
  licence that does not allow this use; the attribution "Contains
  modified EUMETSAT Meteosat data" is required. Each source fetches the
  windows its imager has (`ir086`, `ir104`,
  `ir112`, `ir123`: AHI bands / ABI channels 11, 13, 14, 15; FCI's IR 8.7,
  IR 10.5 and IR 12.3 under `ir086`, `ir104`, `ir123` — an FCI channel
  takes the id of the nearest window, and FCI has no 11.2 µm one — all
  `variables.py` rows with the one
  parameter GRIB2 0/4/4 on surface 8, 180–331.8 K at 0.6) and publishes
  `ir104` as a scalar plus the **Dust RGB composite** `dustrgb`
  (`SourceSpec.bundle_composite_ids`, `binconvert.COMPOSITE_BUNDLES`: the
  three guns `dustr` / `dustg` / `dustb` as variables 1–3 of one bundle,
  each a local-use parameter 3/192/1–3 on surface 8 with a `producer`
  block `{id: "shachen", version}` beside it and one codebook for every
  profile, linear −0.004 + 0.004 × code to 1.0 so **code 0 is "no data" in
  every gun** and black stays a value). The composite is computed in the
  fetch stage, not the converter: `producers.py::DustRGBProducer` runs
  `shachen.dustrgb.dust_rgb` (the `satellite` group; SEVIRI stretches for
  AHI and FCI, the Quick Guide's for ABI; `inputs_for(platform)` is the
  four windows or, without 11.2 µm, three with the 10.4 µm window as the
  green gun's minuend — `bundle_input_ids` asks it for the source's
  platform, and `convert.rs::composite_input_ids` must answer the same by
  source) once per slot on the warped
  channels and caches each gun as a frame of its own (`assemble.read_frame`
  / `write_frame`, Int16 at 1e-4, the sidecar carrying the producer id and
  version), so a round composes one slot. The window is then one series
  file per variable, `<stem>.<variable>.nc` (GDAL stacks a VRT into one
  extra-dimensioned variable only when its bands are exactly that axis),
  the produced ones stamped with `producer_id` / `producer_version`
  variable attributes; `observation.series_files` resolves a run directory
  either way (per-variable files, or the one file the radar sources
  write), `inspect_observation` reads several variables with their own
  `PlaneSource` and returns the stamps, the converter reads only the
  variables the requested bundles carry (`bundle_variable_ids`) and
  `build_metadata(producers=…)` writes the block from the stamp — the
  registry knows the producer's id (`VariableSpec.producer_id`), never its
  version. `native.knows_source` asks the wheel for the source's whole
  published bundle set, so a wheel that predates the composite sends the
  workflow down the reference pipeline.
  `SATELLITE_VARIABLE_IDS` (channels + guns) and
  `tests/fixtures/satellite-registry.json` (`variables` + `bundles`,
  regenerated by `tests/prepare_satellite_registry.py`); the shell
  classifies a channel by the band's wave number
  (`identity.ts::satelliteChannel`), `ChartFamily` `ir104`, the `satellite`
  sheet group, the `cloudtop` meteogram row, and a local-use parameter by
  `producer.id` + number; the file stays in kelvin and `web/src/units.ts`
  shows it in Celsius (legend ticks and readouts only).
  `tests/test_satellite.py` runs the sixteen fixture tiles in
  `tests/fixtures/himawari/` (four bands, two scans) through the whole
  stage, the producer and both encoders; `tests/test_goes.py` the eight
  cut CMIPF windows in `tests/fixtures/goes/`
  (`tests/prepare_goes_fixture.py`) under `goeseast` the same way;
  `tests/test_meteosat.py` one real FCI chunk from EUMETSAT's public test
  data in `tests/fixtures/meteosat/` (standing for every chunk, the
  reader's selection narrowed to it) and a trimmed real search response
  under `meteosat`, hourly, against a stand-in store and a fake token
  opener (the GDAL cases skip without `hdf5plugin`).

`variables.py` is the variable registry in GRIB2 terms: the parameter
triple, the fixed surface, label and unit, plus the matching hints (element,
`.idx` phrase and its alternates at another centre, ECMWF param, and
`ecmwf_alternate_params` where the open data spells one field differently
along the axis: the gust is `10fg` to 90 h and `10fg3` beyond). Another
centre's record is accepted under a variable's identity two ways, matched by
the GRIB2 header index and by GDAL's band metadata in both encoders and
never written: `grib2_aliases` (another triple on the same surface) and
`grib2_alternates` (a whole `RecordAlternate`: surface, value, statistical
process, GDAL unit). One entry per variable feeds both record matching and
the schema v3 metadata block; it assigns no container id. Some entries are
input-only (ECMWF `tp` de-accumulates into `prate`, sflux `prate_ave`
de-averages into `prate`; `dirpw` and `spfh<level>` feed derivations). The
isobaric families (`hgt`, `tmp`, `rh`, `spfh`, `ugrd`/`vgrd`,
`uqflx`/`vqflx`, `vvel`, `thetae`) are generated from one table of eight
levels; `isobaric_variable(id)` answers `(family, level)`.

Registration is not publication: `SourceSpec.bundle_scalar_ids` and
`bundle_vector_ids` say what a source ships. A vector bundle (`wind10m`,
`wind<level>`, `qflux<level>`, `wave`) ships only when every input in
`binconvert.vector_input_ids` is fetched; a derived scalar (`thetae<level>`,
`binconvert.DERIVED_SCALARS`, Bolton 1980 in a fixed operation order) and a
derived vector (`binconvert.DERIVED_VECTORS`: `qflux` = `q·V/g`, `wave` =
the significant height laid along the direction of travel in the wind's
convention, `(-h sin θ, -h cos θ)`) ship only when their inputs are fetched.
GFS publishes the full set; ECMWF publishes what its open data carries, in
GFS order, so a model switch keeps the layer; sflux stays at its four. A
variable empty at the analysis (`optional_at_analysis`, the ECMWF gust) is
listed by `binconvert.analysis_optional_ids` (mirrored in `convert.rs`) and
its series starts at the first step, as the de-accumulated `prate` does.

Two mechanisms came with the ocean set: a source may read a second file
family of the same cycle (`SourceSpec.companion_files`: GFS's `gfswave.*`
beside `atmos/`, ECMWF's `wave` stream beside `oper`; the fetcher appends the
records after the primary ones so a frame is still one GRIB, and a run is
complete only when its wave frames are up, usually within minutes of pgrb2
f240, occasionally 20 min after), and a record may not cover its grid
(`VariableSpec.fill_values`: GDAL's nodata 9999 becomes the codebook bottom
before unit conversion, in both encoders; the format has no bitmap).
WAVEWATCH III packs as JPEG 2000, which the wheel's GDAL reads through the
OpenJPEG that `scripts/build-gdal-minimal.sh` links;
`tests/fixtures/gfswave.*.jp2.crop.grib2` holds it to the reference GDAL. A
family some GDAL cannot read can be marked `CompanionFile.repack`, which
repacks it to `grid_simple` with `grib_set` at fetch time and makes
`bundle-groups` flag its jobs `eccodes`; no family needs that now.

The registry fixtures (`tests/fixtures/isobaric-registry.json`,
`pressure-registry.json`, `surface-registry.json`, `ocean-registry.json`)
hold the three implementations to one set of ids and codebooks. Widening a
source's input list means recutting its crop fixture
(`tests/fixtures/gfs.*.crop.grib2`, same run, same `-srcwin`, or the
two-frame `ecmwf.*.crop.grib2` / `aifs.*.crop.grib2` pairs that
`tests/test_ecmwf.py` / `tests/test_aifs.py` build through both encoders)
and regenerating the registry fixtures.

Other modules:

- `fetch.py` → `idx.py` / `grib2.py`: byte-range fetches of exact GRIB
  records, one `.idx` + range set per file family. ECMWF open data is
  CCSDS-packed and is repacked to `grid_simple` with `grib_set` at fetch
  time.
- `binconvert.py`: the whole conversion. Grid discovery (a global grid is
  snapped to `360 / width`, since WAVEWATCH III writes the last longitude
  off and a wave-only bundle group must land on the same grid as its pgrb2
  siblings; `grid.rs` repeats the rule), cropping (`crop_grid`), unit
  conversion, de-accumulation / de-averaging, quantization, temporal
  grouping, bundle writing, the reduced-resolution ladder
  (`SourceSpec.variant_factors`, `(2,)` by default — the `.half` tier —
  and `(2, 4, 8)` on the satellite sources, `.quarter` and `.eighth`
  beside it: each rung every f-th row and column of the quantized codes
  with the tile divided by f, `binconvert.variant_tier` / `_variant_grid`
  / `_bundle_tile(factor=)`, mirrored in `convert.rs`; the manifest's
  `variants` list is in ascending factor order and nothing keys on the
  suffix), posters, H.264 companions
  (surface fields of a source whose `SourceSpec.video` is on: GFS and HRRR),
  manifest entries.
- `quantize.py` / `temporal.py` / `binformat.py`: codebooks, modulo-256
  residual prediction, container read/write.
- `manifest.py`: manifest and live-pointer construction and validation;
  both are validated on write.
- `observation.py`: the NetCDF ingest, producing the same `SourceFrame`
  list the GRIB inspectors return plus a `PlaneSource` for unscaling and
  the fill value.
- `showcase.py`: case definitions → cropped bundles → `showcase.json`. A
  case's `title` / `summary` must carry all eleven UI locales
  (`showcase.LOCALES`, held to `web/src/i18n.ts` by
  `tests/fixtures/locales.json`); `showcase refresh` rewrites a built case's
  sidecar from its definition without a rebuild. A case is a forecast case
  (`run` + `hours`), a local-file observation case (a series-file source
  with `dataset` instead of `run`, `XUE_OBSERVATION_ROOT`; how the `cma`
  cases were cut before the archive) or a fetched-observation case
  (`mrms`, `cma`: `run` is the window's first hour, `hours` its length);
  a window the archive cannot fill to its declared end is refused.
- `assemble.py`: a run built in pieces. `publish.yml` fans a run out over
  one job per bundle group (`bundle-groups` packs the source's bundles into
  at most `max_jobs` jobs of roughly equal cost and says which need
  ffmpeg). Each job runs `build-bin --bundles …`, fetching only those
  bundles' inputs into `data/raw/partial/<group>/` and writing
  `manifest.part.<group>.json`; the finalize job merges the parts with
  `assemble-run` (every published bundle exactly once, core pair required)
  and then writes the pointer. `make upload-r2-bundles` /
  `upload-r2-manifest` are the two halves of `upload-r2`; a part never
  reaches the bucket. The top-up uses the same pieces: when the resolved
  cycle is live but lacks bundles the source now publishes, `publish.yml`
  builds only those (`bundle-groups --base-manifest` against `make
  live-manifest`) and `assemble-run --base-manifest` merges them onto the
  live manifest; `force` rebuilds everything. `tests/test_assemble.py`
  holds a split build and a top-up byte-identical to a whole one, so
  nothing cross-variable may enter a bundle or its manifest entry.

### Zarr store (`xuebuild/zarrstore.py`)

A bundle is published as a Zarr v3 store, `<bundle>.zarr/`
(`docs/zarr-profile.md` is normative): one group per bundle whose
`attributes.xue` is the bundle's metadata JSON verbatim, one `uint8` array
per variable, one `sharding_indexed` shard per array (the whole axis of the
whole grid, read by range as the container was) whose inner chunks are
regular six-frame time chunks of the bundle's tiles, `[bytes, zstd{15,
checksum}]` inside, `fill_value` = `nodataCode`, CF `scale_factor` /
`add_offset` / `_FillValue` on linear codebooks, and `time` / `latitude` /
`longitude` coordinate arrays. One shard per array keeps a store at a
handful of objects (a bucket bills per object; cut per time chunk a GFS run
was ~5 000 objects), and the shard index, read once as a suffix range, is
the whole index. The reader takes the shard length from the array's chunk
shape and still reads the earlier per-time-chunk stores.

The store is derived from the finished `.xue` by `zarrstore.export_bundle`
(`read_bundle` → chunk by chunk → pad edge tiles → zstd → hand-written shard
index + CRC-32C; NumPy only), so both encoder paths produce identical
stores: `binconvert` exports inside each bundle job, `native.py::_zarr_reports`
after the wheel has written, and `test_native.py` compares the objects byte
for byte. `build-bin --zarr` / `XUE_ZARR=1` turns it on; `--no-xue` /
`XUE_CONTAINER=0` (publish.yml's `container: false`, the env pair on
`publish-mrms.yml`, `showcase build --no-xue`) retires the container after
the store and the video companions have been derived from it
(`binconvert.retire_container`, both encoder paths), and the manifest entry
names the store alone (`tests/test_native.py::NativeStoreOnlyParityTests`).
`xue export-zarr <bundle>` derives one by hand, with `--delta` (the
`xue.delta` codec, `xuebuild/zarrcodec.py`) and `--index-location
start|end`. The manifest's optional `zarr` descriptor `{path, byteLength,
crc32}` on a bundle entry and each variant (`crc32` is that of the group
`zarr.json`, the store's `?v=`) is validated when present by all three
validators and carried through `assemble-run` unchanged. Store time chunks
coincide with the bundle's groups only up to the first change of step, so
every chunk is re-encoded from codes and the export report measures
`comparableChunks` / `identicalChunks`. The `zarr` dependency group (`uv
sync --group zarr`) is for the tests and for reading a delta store.

The frontend plays a store through a third `DecodeChannel`, `web/src/zarr/`,
the default: `main.ts::loadVariable` takes it whenever the bundle or its
picked tier carries a `zarr` descriptor, unless `?backend=xue`
(`urlstate.ts::parseBackendFromSearch`). The order is store over ranges →
`.xue` over ranges → `.xue` downloaded whole → store by whole objects (a
`ZarrStore` with `ranges: false`, taken only when the entry ships no
container; a probe session is streamed or nothing). All three validators
admit an entry that names only its store: the container's `path` /
`byteLength` / `crc32` are present whole or absent whole, and an entry with
neither delivery is refused (`containerOf` / `deliveryBytes` in
`manifest.ts`). `tests/e2e/app.spec.ts` runs on the fixture manifest with
its stores stripped (`tests/e2e/artifacts.ts::withoutStores`) and
`zarr.spec.ts` drives the default and the store-only shape. A tab whose
validator refuses a live manifest, or whose Zarr reader cannot open a live
store, reloads itself once per manifest (`ManifestRejectedError` /
`StoreRejectedError`, `main.ts::reloadForNewerShell`, the crc32 kept in
`sessionStorage`). `zarr/worker.ts` answers the same protocol as
`worker.ts` (`init-stream` gains `kind: "zarr"`) over `zarr/session.ts`:
`shard.ts` validates the group and array documents, parses the
CRC-32C-checked shard index once per shard and maps frame and tile to a
byte span (`shardOf`; a time chunk is a fixed six frames and may straddle
the container's groups), and `store.ts` appends the store's `?v=` and
coalesces the ranges of one shard issued in one microtask (gap ≤ 64 KB) into
one request. Decoding is `decodeChunk` from `rust/xue-wasm`
(`decode::core::decode_chunk`). `tests/web/zarr.test.ts` holds every frame
and series byte-identical to `WasmBundle`; `npm run measure:backends` prints
requests and bytes per backend on a local run.

### STAC catalog (`xuebuild/stac.py`)

A static STAC 1.1.0 catalog is derived beside the JSON the shell reads; the
frontend never reads it (`docs/stac.md` is the contract). Root
`catalog.json`, `<source>/collection.json` (the pointer's STAC face, whose
`item` / `latest-version` links name the live Item `<source>/item.json`
beside it, the run's Item with hrefs relocated by `relocate_item` so the
path outlives the run; both uploaded with the pointer by
`upload-r2-pointer`), `<source>.<run>/item.json` beside each manifest
(`<run>/<HHMM>/item.json` for an MRMS round; uploaded no-cache with the
manifest), and `showcase/collection.json` + `showcase/<case>/item.json`
(written by `write_catalog`, uploaded by `upload-r2-showcase`). Every
document is a pure function of the manifest, the catalog row and
`sources.py` (no timestamps, no host names, relative links only), so the
split-build and top-up identity tests cover them. The writer runs in the
CLI, not the converter: `build-bin` for a whole run, `assemble-run`, and
`xue stac`; a `--bundles` piece writes none. Licenses and provider prose
live in `_source_prose`, held to the registry by `tests/test_stac.py`.

The three point products (`sounding`, `airport`, `tc`, `POINT_PRODUCTS`)
are in it on the same terms, derived from an issue's `index.json` alone by
`write_point_product_documents`: `<product>/collection.json`,
`<product>/item.json` (the issue's Item relocated) and
`<product>.<issue>/item.json` beside the index, with one asset per file the
issue ships (`index`, the NDJSON `soundings` / `history`, or one per
storm), the stations' bounding box as the geometry, and no `cube:` or
`forecast:` fields. Here the product's own build writes them (after the
index and the pointer; the Collection and the live Item only when the
pointer was written), `xue stac --product <p> --issue <i>` rewrites them,
and `upload-r2-<product>` pushes the issue's Item with the index and then
`upload-r2-stac-collection STAC_DIR=<product>`. Their prose is
`_point_product_prose` (every licence `other` with a link; see
`docs/stac.md` §"Point products"), and the goldens of the three products
carry the Items they write.

### Tropical cyclone product (`xuebuild/tc/`)

Storm tracks are a second product beside the runs (`docs/tc.md`, schema v1).
`xue tc-build` fetches each source into `data/raw/tc.<issue>/<source>/` with
a `fetch.json` (`tc/fetch.py`), parses each with a parser that imports no
other (`atcf.py`, `tcw.py`, `bufrtracks.py`, `ibtracs.py` → the shapes in
`track.py`, SI units at the parser), resolves identities (`identity.py`:
ATCF id first; invests and model-found systems get `x-<basin>-<hour>-<n>`
ids that the previous hour's index carries forward by alias and proximity,
so `publish-tc.yml` runs `make live-tc-index` before building) and writes
`web/public/data/tc.<issue>/` plus `latest-tc.json` (`schema.py` validates
on write; `build.py` is the only place the sources meet). A source fails on
its own into `sources[]`; the pointer is withheld only when nothing
contributed. `tests/fixtures/tc/` is a fetched hour and
`tests/fixtures/tc/expected/` the golden (`tests/prepare_tc_golden.py`
regenerates; needs `bufr_dump`); `tc-registry.json` pins `registry.py` and
`web/src/tc/agencies.ts` to one table. The shell draws the product as marks
over any composition (`web/src/tc/`: `schema.ts`, `tracks.ts`, `layers.ts`,
`panel.ts`); the marks take no session and never gate the playhead.
`main.ts::syncTcTime` hands them the frame's valid time, `loadTc` polls the
pointer with the runs, and a case hides them. URL state is `?tc=<id>|off`,
`?tcagency=`, `?tcmodel=`, `?tcmembers=`.

### Airport product (`xuebuild/airport/`)

Airport observations and forecasts are a third product beside the runs
(`docs/airport.md`, schema v1), the same point-product contract as tc.
`xue airport-build` fetches the NOAA Aviation Weather Center's three cache
files into `data/raw/airport.<round>/` with a `fetch.json` (`fetch.py`; the
station table is fetched at most once a day with `If-Modified-Since` and
lives at `data/raw/airport-stations.json`, the required custom User-Agent
and the hundred-a-minute budget are honoured here), parses each with a
parser that imports no other (`metar.py` the decoded CSV, `taf.py` the
decoded XML, `stations.py` the table; SI at the parser through `units.py`,
a value outside the contract's ranges written null rather than raised on)
and writes `web/public/data/airport.<round>/` plus `latest-airport.json`
(`schema.py` validates on write; `build.py` is the only place the sources
meet). A round is a UTC minute floored to ten.

A round is two files. `history.jsonl` is one line per station, sorted by
ICAO — the station with its last 24 h of METARs and its current TAF — and
`index.json` carries every station's position and newest observation as a
sixteen-value row whose last two are the byte `offset` and `length` of that
station's line. So a browser reads one airport with one range request and
an analyst streams one object; the validator holds the spans in row order
and inside the file the index measured. Each round merges this round's
METARs onto the previous round's history by (station, time) with the new
report winning, drops what is older than 24 h, and a station whose window
empties leaves. `make live-airport-index` pulls the live pointer, index and
history file (three objects), `upload-r2-airport` pushes history → index →
pointer, `prune-r2-airport` keeps `AIRPORT_KEEP` rounds. The pointer is
withheld only when both the METARs and the TAFs failed.
`tests/fixtures/airport/` holds two consecutive fetched rounds and
`expected/` the golden (`tests/prepare_airport_golden.py` regenerates; no
network, no eccodes). The shell draws the round as station marks
(`web/src/stations/`, below).

### Radiosonde sounding product (`xuebuild/sounding/`)

Observed vertical profiles are a fourth product beside the runs
(`docs/sounding.md`, schema v1), the tc product's shape throughout.
`xue sounding-build` lists the two GTS→WIS2 gateway directories of the
public `wis2globalcache` S3 bucket (`fetch.py`: unsigned `list-type=2`
under `data/<gateway>/data/core/I/U/S/`, `wis2:<centre>` reserved for the
native topic directories), downloads only the objects newer than the
previous index's per-gateway `watermark` into
`data/raw/sounding.<issue>/<gateway>/` with a `fetch.json`, decodes each
bulletin with `eccodescli.bufr_dump_json` (`bufr.py`: one `Sounding` per
subset, levels closed by the first repeat of a level key, fixed point with
`-32768` missing, nominal time from the bulletin's `<ddhhmm>`, each ascent
thinned to the classical TEMP set by `THINNING_RATIO` with the pre-thinning
count kept as `reported`), derives
four quantities in a fixed operation order (`derive.py`) and writes
`web/public/data/sounding.<issue>/` plus `latest-sounding.json`
(`schema.py` validates on write; `build.py` is the only place the sources
meet). An issue is two objects, as the airport product is: `soundings.jsonl`
one line per station sorted by id, and `index.json` whose rows carry each
station's `offset`/`length` into it, so a reader takes one station by range
and an analyst streams the file. Identity is the five-digit WMO number, the published id the native
WIGOS id where there is one else `0-20000-0-<wmo>`; one sounding survives
per station-hour, the longest and then the latest arrival (a correction).
A station with no new ascent is carried forward from the previous issue's
`soundings.jsonl`, which is why `make live-sounding-index` pulls that file
with the index and checks its CRC32 before trusting it. A gateway fails on its own into
`sources[]`; the pointer is withheld only when nothing contributed and
nothing carried forward. `tests/fixtures/sounding/` is a fetched hour with
one gateway down and `expected/` the golden
(`tests/prepare_sounding_golden.py` regenerates; needs `bufr_dump`).
`publish-sounding.yml` runs at a quarter past every hour with no GDAL and
no wheel. The shell draws the issue as station marks (`web/src/stations/`,
below).

### Station marks (`web/src/stations/`)

The sounding and airport products are drawn the same way, from one module
shaped like `web/src/tc/`: `schema.ts` validates both indexes, both
pointers and one line of either `.jsonl` (structurally, refusing a
`schemaVersion` above 1, a row that is not the airport index's sixteen
values, a span past the file it indexes and a value outside the contract's
ranges); `fetch.ts` reads the pointer no-cache and the index through
`fetchImmutable` with its `?v=`, resolves to null on a 404 so a root
without the product costs nothing, and reads one station with one
`Range: bytes=<offset>-<offset+length-1>` against the `.jsonl` (a 206 must
answer the span asked for, a 200 is sliced at the same offsets);
`layers.ts` is two MapLibre GeoJSON circle sources — soundings filled by
`headline.t500` through a fixed cool→warm ramp, hollow where the ascent
never reached 500 hPa, and airports in the four flight-category colours
with one hex set per ground tone, thinned below zoom 6 to the stations
carrying a TAF; `card.ts` builds the popup a clicked mark opens. The marks
take no session, fetch nothing per frame and never gate the playhead: the
playhead's valid time only sets a paint expression that draws a station
observed more than three hours (airports) or fifteen hours (soundings)
from it faint, so a playhead move is two paint properties rather than five
thousand rebuilt features. `main.ts::loadStations` polls both pointers
with the runs, each product independently, `syncStationTime` follows the
readout, a case hides both, and each product is its own rail tile
(`#sounding-tile`, `#airport-tile`), off until pressed. URL state is
`?stations=snd|apt|snd,apt|off`, in the link alone — nothing is stored.

### External tools

GDAL, zstd, ffmpeg and eccodes are invoked as CLI subprocesses (`gdal.py`,
`zstdcli.py`, `ffmpegcli.py`, `eccodescli.py`); NumPy is the only runtime
dependency. Two exceptions:

- zstd runs in-process via the stdlib `compression.zstd` on Python ≥ 3.14
  and falls back to the CLI below that. The two are interchangeable on
  decode and not byte-identical on encode.
- `gdalinfo` has a second source. `gdal.dataset_info` is the one entry
  point and reads through the `xuepy` wheel's linked GDAL (`xue.gdal_info`,
  one reason for the `xuepy>=0.19` floor) when the build converts natively,
  the subprocess otherwise; the choice follows `XUE_ENCODER`, so a run never
  mixes two GDAL installs. This is what lets the scheduled `publish-*`
  workflows install no GDAL. `tests/test_gdalinfo.py` diffs the two sources
  field by field. Extraction (`gdal_translate`) has no such fallback, so
  `XUE_ENCODER=python` still needs a system GDAL.

Errors that are the user's to fix subclass `XueError` (`xuebuild/errors.py`);
the CLI turns them into `error: …` and exit code 2. Anything else is a bug.

### Decoder

`rust/xue/src/decode/` exposes `Bundle` (whole file in memory) and
`StreamingBundle` (structural prefix only, payload bytes fed in as range
responses arrive) over shared code in four layers: `metadata.rs` (grid, time
axis, variable set), `structure.rs` (header geometry, index, dependency
chains), `core.rs` (payload residency and residual replay) and `bundle.rs`
(the two public readers). All arithmetic on file values is checked, and
nothing is allocated from a file value before validation.

`rust/xue/src/format.rs` holds the container's byte layout and nothing else:
constants, the `Predictor` / `Compression` enums, and `pack` / `unpack` for
`FixedHeader`, `IndexHeader` and `PlaneEntry`. The decoder unpacks through
it and the native encoder packs through it, so a field cannot drift between
them.

### Frontend

`web/src/worker.ts` owns the WASM decoder and speaks one message protocol
(`booted` → `init`/`init-stream` → `ready`, then `decode` → `frame`) in both
full and streaming modes. `web/src/webcodecs.ts` implements the same
protocol over a native `VideoDecoder` for the H.264 companions, so `main.ts`
holds either one in the same field. Prefetch is windowed: the main thread
sends `prefetch-window` with the hours ahead of the playhead plus a
concurrency cap.

`main.ts` composes the view from slots: a fill slot (temperature,
precipitation, reflectivity, radiation, wind speed) and a lines slot (the
pressure family), each a `ForecastLayer` fed by its own bundle session with
its own worker, grid, tiles and resolution tier. `ViewComposition {fill,
lines}` says what is on screen; the primary session (the fill's, or the
lines' when nothing is filled) drives the timeline, legend, data card and
ground tone, and the lines overlay follows it by lead seconds on its own
axis (a frame not decoded yet keeps the last one up; a lead time the
overlay's axis lacks hides it). The primary gates the playhead; overlays
never do. Prefetch fans out to every session on screen (the primary at the
connection's concurrency, an overlay at one), an overlay takes the
half-resolution tier unless `?res=full`, and every `?type=` is a composition
with one slot filled. Delivery path per session: WebCodecs only when
`?use_h264=true` opts in and a video artifact exists and the browser
supports it, otherwise streaming if a range probe succeeds, otherwise a
whole-bundle download; the resolution tier comes from `pickBundleVariant`
(viewport and connection, then a per-frame cell budget:
`main.ts::planeCellBudget`, 2.5 M cells, halved on a ≤ 4 GB device and
quartered on a constrained connection, against the rung's cells times the
share of the grid in view at session open, `manifest.ts::visibleGridShare`
— a full 3000² satellite plane never fits zoomed out, so the ladder's
lower rungs are what the disk plays at, while a view on a storm still
takes the full tier since a streaming session decodes only its viewport's
tiles) unless `?res=half` (the smallest rung shipped) or `?res=full` pins
it. The plane cache budget grows with the primary session's frame size to
hold the prefetch window (capped at 256 MB, 128 MB on a ≤ 4 GB device) and
the window shrinks to what the cache holds; the data card names the rung
(`Xue ½`, `Zarr ¼`).

`layer.ts` renders one quantized R8 plane with inverse Web Mercator and a
palette lookup in the fragment shader, blending two frames via `u_mix`
(never animate raster opacity). `setVectorField` switches the data texture
to RG8 (u codes in red, v in green, the packing `particles.ts` builds, which
`main.ts` interleaves once per frame and memoizes) and looks the palette up
by `magnitude / maxMagnitude`; every vector bundle takes this path with its
own ceiling from `levels.ts::vectorMaxMagnitude`. `web/src/levels.ts` is the
family registry: one rail tile per family, and a family's members are
two kinds. `FamilyInfo.levels` are the same quantity on other surfaces
(the isobaric set, or the cloud layers listed outright): the level row on
the capsule picks among them. `FamilyInfo.variants` are related but
different quantities behind one tile (sea ice cover and thickness, wave
height and period), never on the level row: the field sheet lists them as
chips under the family's row, and pressing the pressed tile again cycles
through them. A family tile's gloss follows the member on screen
(`syncFamilyGloss`: TEMP 2M, TEMP 850, ICE THICK).

The layer rail is three sections, one kind of press each (`index.html`,
`.rail-section`): the fields (a radio: the dataset's core tiles,
`FORECAST_MODELS[].railCore`, the tile of the field on screen when it is
not a core one, and MORE, which opens `#field-sheet`, every field the run
publishes grouped by quantity, written by `main.ts::renderFieldSheet` from
the rail's own tiles and a stopgap group table), the overlays (switches
drawn over the field and following the playhead: the pressure lines, the
particles, the experiment's derived layers) and the marks (the storm sheet
trigger, the soundings and the airports). A switch tile carries
`data-toggle` and a ring in its corner, a sheet trigger `data-dialog` and a
chevron. `syncFieldTiles` hides every field tile the run does not ship or
the screen does not show, and writes a generic tile for a field this build
has no tile for while it is on screen; the rail is marked dense
(`syncRailDensity`, tiles at 36px) when its visible tiles would not fit its
box at full size. The rail is a scroll column in a fixed box (under the
zoom tile down to the capsule on desktop; under the round controls on
phones, where `main.ts` publishes the capsule's measured height as
`--capsule-height`). A new field is a row of `web/src/variables.ts`
and, if it gets an icon, a tile in the field section; without one it is a
row of the sheet's last group. What is on screen is one object, `view: ViewState`
(`web/src/viewstate.ts`: the field and the lines, the particles, the
experiment's derived layers, the storm and station marks); the rail's
pressed states (`syncRail`), the level row and the address bar
(`syncUrl` over `searchForView`, the inverse of `parseView`) are
projections of it, and nothing else holds a copy. The lines group is on the level row whenever the run
publishes a pressure surface: a pressed member is the overlay, pressing it
again takes the lines off, as does the rail's pressure switch, and the
group's ALONE member drops the field to leave the chart by itself (the
field last on screen, `lastField`, comes back on the next press).

`web/src/variables.ts` is the variable table: one `VariableSpec` per
registered bundle id (`KnownBundleId`, a complete record), carrying what
the shell shows for it — the instrument code, headline and buffer title,
the localized label and legend (closures, so they follow the locale), the
legend's gradient source, the ground (`GroundId`, mapped to tones per theme
by `main.ts::GROUND_TONES`), the `?type=` name and aliases, the meteogram
and showcase codes, the rail family (`IsobaricFamily`, which tile stands
for it) and the field sheet's group. `chart` (a `ChartFamily`, what the
field is) and `family` (which tile) are different questions and both are
kept. The surface rows are written out in the sheet's order; the isobaric
rows are generated from the family registry and the pressure rows from
the level registry. Every id-keyed table derives from it: `identity.ts`
resolves id ↔ identity through `variableSpec` / `specForIdentity`,
`urlstate.ts` builds its alias table from it, `meteogram.ts` and
`showcase.ts` read their codes, `main.ts` its copy, legends, grounds and
sheet rows. The table is built on first use, never at load: it sits on an
import cycle (identity → variables → levels → pressure → identity), so
nothing in it may run while those registries are initialising.
`tests/web/variables.test.ts` holds the markup's tiles, the identity maps,
the family registry and the alias set to it.

The pressure family is drawn as contour lines: `web/src/pressure.ts` holds
the per-level intervals and emphasised lines, `layer.ts::setContours` turns
the pass on, and lines are found per pixel from the dequantized value and
its screen gradient. This depends on an encoder rule: each pressure
codebook's offset puts every standard contour exactly half a code off, so a
line never coincides with a flat plateau. Before contouring,
`layer.ts::prerender` smooths each uploaded plane in grid space (a separable
Gaussian, coverage-renormalised, into a 16-bit RG8 texture;
`CONTOUR_SMOOTHING_CELLS` in `main.ts` sets its width); filled fields never
go through it. Labels (a value on each line, an H/L with its value on each
closed center) are the one CPU step: `web/src/isolines.ts` reduces the
displayed plane to the cells in view, smooths it the same way, runs marching
squares and a windowed-extremum search in `labels.worker.ts`, and two
MapLibre symbol layers place the result. Playback throttles the trace to
once a second; a stop, a step or a pan refreshes at once.
`tests/fixtures/pressure-registry.json` holds the codebooks, intervals and
emphasised lines identical across the three implementations.

`particles.ts` advects GPU particles through the wind field as an overlay in
one ink, on by default, off from the rail's overlay section
(`?particles=off`, remembered in `localStorage`, off by default under
`prefers-reduced-motion`). Wind narrows to the viewport while the overlay
is off; with it on the session takes the whole plane, since the particles
respawn across the grid.
`playback.ts` holds the frame-rate ladder and the per-frame dwell that keeps
a mixed-step axis at one apparent speed.

A click on the map pins a point probe: `probe.ts` turns the click into a
grid cell, and on a v2 bundle the worker's `series` message reads that
cell's whole axis in one round trip; a v1 bundle or the video path fills it
in from the planes decoded for the screen. The panel docks over the
transport capsule at the capsule's width; under the headline sit the
meteogram rows (`meteogram.ts`: temperature with dew point, precipitation,
wind with gust and direction, cloud layers, sea level pressure). Rows come
from the manifest: a run publishes what it publishes and the rest are
absent. Each row's bundle is a probe session (`loadVariable(id, sequence,
"probe")`, the primary's tier on the streaming path alone, never video and
never a whole download). Every row reads the primary axis by lead seconds
(`alignSeries`), so a missing frame leaves a gap rather than a shifted
column. The panel and the capsule share two columns (`--probe-column` for
the labels, the rest for the axis), so the sparkline, the traces and the
track run on one line and their playheads coincide.

A pin also reads the two point products, whether or not their rail tiles are
pressed: `stations/nearest.ts` resolves the nearest ascent (150 km) and the
nearest airport (40 km) out of the loaded indexes. The ascent becomes a
skew-T under the rows (`sounding/section.ts` over `sounding/skewt.ts`, its
`SkewTInk` read from the panel's `--skewt-*` custom properties at every
draw, so a theme switch repaints in place), behind a disclosure that is open
above phone width and closed below it; opening it is what fetches the
station's line (one range request) and opens a probe session per isobaric
`tmp`/`rh`/`wind` bundle the run publishes (`sounding/model.ts`, joined to
the rows in `probeBundleIds`). The model column is read at the frame nearest
the *ascent's* nominal time, within three hours, and is `profileFromModel`
of whatever levels answered; further than that the legend says there is
none. A time button per nominal time swaps the ascent, and `#skewt-sheet`
(`sheet.ts`) is the same chart at full height. The airport's day of reports
is laid over the rows themselves: `stations/observations.ts` turns each
METAR into marks at its own lead seconds (`axisPosition` places them between
frames; anything outside the run is dropped), the sky's codes into oktas,
and the TAF into a `taf` row of bands — hatched for `TEMPO`/`PROB` — while
the headline gains the ICAO and its category chip and each row's readout
gains the nearest report within ninety minutes. None of it moves the
playhead; the playhead only decides which readouts are shown.

Shell layout: a display-serif title top-left names the layer and opens the
run picker (`#model-sheet`); three round buttons top-right (language, cases,
appearance) share `web/src/sheet.ts` with the model and sources sheets; the
color scale runs down the left edge, one 48px tile per layer down the right,
and one capsule at the bottom (960px wide at most) holds the transport in
two rows. The `<input type=range>` is transparent and carries only the hit
area, the keyboard and the accessible name; the tick marks, playhead and
labels are drawn beside it. `src/theme.ts` resolves light/dark before the
first render as `i18n.ts` resolves the locale, and both switch in place,
never by reload: `toggleTheme` / `setLocale` persist, restamp the document
and notify listeners (`onThemeChange`, `onLocaleChange`), and
`main.ts::applyAppearance` / `applyLocale` repaint what the stylesheet
cannot. `map.setStyle` would drop the custom WebGL layers, so
`syncBasemapStyle` builds the style again and applies the property-level
diff to the layers already on the map. `isDark`, `locale`, `htmlLang` and
`basemapLang` are live bindings: read them at use, never capture them in a
module-level constant. `index.html` repeats the theme detection inline so
the shell never paints on the wrong ground first.

Each layer keeps the ground its palette needs (a warm sheet under
temperature, dark slate under precipitation, wind, radar and solar, chart
stock under the pressure family), so the chrome's tokens and the map's are
separate. The chrome uses the theme's own; anything floating directly on the
map (title, color-scale numbers, credits) uses `--map-ink` /
`--map-ink-muted`, which follow `body[data-ground]`, stamped by
`applyBasemapTheme` from the basemap tone's luminance. `applyBasemapInk`
repaints the basemap's label and line colors the same way. The coastline is
the shell's own layer (`buildBasemapStyle` inserts a `coastline` line layer
over the `earth` polygons under the boundaries; the forecast layers insert
themselves before it). The round controls, the zoom tile, the layer rail and
the credit mark share one 44px column at the same right offset (20px, 16px
on phones); a control added to that edge keeps to it. The credit is a mark
with the sources sheet behind it; the line beside it shows only above
1400px.

The Protomaps key is origin-locked to the production domains and to
`localhost`, not `127.0.0.1`, which is what `playwright.config.ts` serves
from, so the e2e suite stubs tiles out. To see the real basemap locally,
browse `http://localhost:4173`.

## Conventions

- Locale is one of eleven (`zh`, `zh-Hant`, `en`, `ja`, `ko`, `de`, `fr`,
  `es`, `pt`, `tr`, `ru`) via `web/src/i18n.ts`; appearance is `light` /
  `dark` via `web/src/theme.ts`. Long-lived status copy in `main.ts` goes
  through `say()` so a switch can restate it. The dictionary is one module
  per language under `web/src/locales/`, each typed `Record<MessageKey,
  string>` against `en.ts`, the source of truth and the only one carrying
  design notes. Only human-facing copy is translated; thrown `Error`
  messages, worker messages, instrument-panel codes (`F058`, `12 FPS`,
  `PLAY`) and diagnostics stay English.
- With no `?type=` and no tile pressed this session
  (`main.ts::variableChosen`), opening or switching to a model shows
  `FORECAST_MODELS[].defaultVariable` (the reflectivity on MRMS) else
  `DEFAULT_VARIABLE` (precipitation); a chosen layer is kept across a model
  switch.
- Timeline copy follows the kind of dataset: `isObservationModel` swaps
  "FORECAST HOUR" / `F058` / 模式周期 / 有效时间 for "OBSERVED" / the
  frame's clock time in the display zone (`09:50`) / 最新观测 (the window's
  newest frame, UTC) / 观测时间, on the viewer, and the showcase cards name
  a case's start and span. An observation has no run to count from, so
  nothing reads as an offset from the window's start.
- Valid times read in one display zone (`web/src/timezone.ts`): the
  browser's own, or the pinned point's while a probe is open (`tzf-wasm`, a
  4 MB index loaded on the first pin, `Etc/GMT±N` over open water).
  `displayZone` is a live binding (`onDisplayZoneChange`). The run cycle
  stays UTC wherever it is stamped, and so do the showcase cards. Offset
  labels (`UTC+9`) and zone ids are instrument text, English in every
  locale.
- URL state (`?model=`, `?type=`, `?lines=`, `?case=`, `?res=`,
  `?use_h264=`, `?particles=`, `?backend=`, `?tc=` with `?tcagency=` /
  `?tcmodel=` / `?tcmembers=`, `?stations=`) is parsed in `urlstate.ts`;
  `?lang=` belongs to `i18n.ts` and `?theme=` to `theme.ts`. Unrecognized values fall back
  to defaults. The camera is in the fragment, `#map=<zoom>/<lat>/<lon>`
  (MapLibre's own `hash: "map"`), so a pan never touches the query string.
  `urlstate.ts::parseCameraFromHash` only says whether a link fixed the
  view: `initialize({ frame })` frames the dataset's region on a model
  switch and on a first open without a camera, never on a retry or a new
  run.
- The Python encoder and Rust decoder are held byte-identical by golden
  tests (`rust/xue/tests/golden.rs`) against fixtures built by
  `tests/prepare_bin_fixture.py`. A format change means changing the spec,
  both implementations, and the fixtures together.
- Playwright fixtures are synthetic and built by
  `tests/prepare_web_fixture.py` from Playwright's global setup: no network,
  no GDAL.
- Generated and fetched data (`data/raw/`, `data/work/`, `dist/`,
  `dist-deploy/`, `web/public/data/<model>.<run>/`, `web/src/wasm/`,
  `tests/fixtures/generated/`) is gitignored; never commit it.
- `plans/` is a local symlink to private design notes, excluded via
  `.git/info/exclude`. It may be absent.
- Commit subjects are lowercase and imperative, optionally prefixed with a
  scope (`ci:`, `web:`, `docs:`, `fix:`).
