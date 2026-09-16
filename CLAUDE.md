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
make mvp [MODEL=gfs|ecmwf|sflux] # check + install + wasm + build a run + vite build
make serve                       # vite preview on 127.0.0.1
npm run dev                      # vite dev server

make test                        # rust + python + web unit tests (incl. encoder parity)
make tc-build [ISSUE=YYYYMMDDHH] # the tropical cyclone product for one hour (needs eccodes' bufr_dump)
make test-rust                   # regenerates the golden fixture, then cargo test
make test-e2e                    # playwright (needs `npx playwright install chromium`)
make encoder-rust                # build the native encoder from source (needs GDAL + libclang)
make encoder-rust-test           # its unit tests plus the byte-identity golden test
make encoder-wheel               # a self-contained wheel carrying a minimal GDAL
npm run build                    # tsc --noEmit && vite build
.venv/bin/python -m xuebuild export-zarr <bundle.xue> [--delta] [--index-location start|end]
.venv/bin/python -m xuebuild stac --model gfs --run YYYYMMDDHH [--round HHMM]   # rewrite a run's STAC documents
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

- Forecast (`gfs`, `ecmwf`, `sflux`, `hrrr`). `hrrr` has a `regrid`: the
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
- Local observation (`radar`): `observation=True`, `series_file=True`, no
  cycle, no lead time, one NetCDF file per event read by `observation.py`,
  no pointer, no cron job, no fetch. The axis is whatever times the
  observations carry.
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
  requests against it decode the wrong bytes. `publish-mrms.yml` is one job
  an hour looping `scripts/window_rounds.sh` (a round every five minutes:
  newest frame vs the live `window.json` → build → `make upload-r2 …
  ROUND=` → `prune-r2-rounds` keeps the run's newest two rounds, `prune-r2
  KEEP=2` the previous run). The shell polls the MRMS pointer every two
  minutes, treats a changed `manifestCrc32` as a new run, and on a rolling
  window keeps the playhead by observation time or follows the end when it
  was at the end (`checkForNewRun` / `resumeOnNewRun` in `main.ts`).
  `pickBundleVariant` scales the needed width by the bundle's longitude
  span, so a regional grid can take its half tier on a far-out view.
- Fetched series-file observation (`jma`): the JMA precipitation nowcast
  over Japan, fetched like MRMS (a rolling window, `latest-jma.json`,
  `publish-jma.yml` running one round of the same `scripts/window_rounds.sh`
  per job on a five-minute cron, `MODEL=jma HOURS=3 ONCE=true`, so no
  runner is held for an hour; a `loop` dispatch runs rounds every two
  minutes to twenty past the next hour and yields to the next scheduled
  job through `scripts/successor_queued.sh`) but arriving as one NetCDF
  series per window
  (`SourceSpec.series_file`, also true of `radar`; `convert_bin` and
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
two-frame `ecmwf.*.crop.grib2` pair that `tests/test_ecmwf.py` builds
through both encoders) and regenerating the registry fixtures.

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
  grouping, bundle writing, half-res variants, posters, H.264 companions
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
  (`run` + `hours`), a local-file observation case (`radar`: `dataset`
  instead of `run`, `XUE_OBSERVATION_ROOT`) or a fetched-observation case
  (`mrms`: `run` is the window's first hour, `hours` its length); a window
  the archive cannot fill to its declared end is refused.
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

### External tools

GDAL, zstd, ffmpeg and eccodes are invoked as CLI subprocesses (`gdal.py`,
`zstdcli.py`, `ffmpegcli.py`, `eccodescli.py`); NumPy is the only runtime
dependency. Two exceptions:

- zstd runs in-process via the stdlib `compression.zstd` on Python ≥ 3.14
  and falls back to the CLI below that. The two are interchangeable on
  decode and not byte-identical on encode.
- `gdalinfo` has a second source. `gdal.dataset_info` is the one entry
  point and reads through the `xuepy` wheel's linked GDAL (`xue.gdal_info`,
  one reason for the `xuepy>=0.16` floor) when the build converts natively,
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
(viewport and connection) unless `?res=half` or `?res=full` pins it.

`layer.ts` renders one quantized R8 plane with inverse Web Mercator and a
palette lookup in the fragment shader, blending two frames via `u_mix`
(never animate raster opacity). `setVectorField` switches the data texture
to RG8 (u codes in red, v in green, the packing `particles.ts` builds, which
`main.ts` interleaves once per frame and memoizes) and looks the palette up
by `magnitude / maxMagnitude`; every vector bundle takes this path with its
own ceiling from `levels.ts::vectorMaxMagnitude`. `web/src/levels.ts` is the
family registry: one rail tile per family, the level row on the capsule
picks the member, and cloud cover lists its members outright in
`FamilyInfo.members`. Past ten visible rail tiles `main.ts` marks the rail
dense and the stylesheet drops the tiles to 36px. The rail is a scroll
column in a fixed box (under the zoom tile down to the capsule on desktop;
under the round controls on phones, where `main.ts` publishes the capsule's
measured height as `--capsule-height`), and its tiles are grouped by
quantity in `index.html`, so a new tile goes into its group. Over a field
the lines group is on the row whenever the run publishes a pressure surface:
a pressed member is the overlay, pressing it again takes the lines off.

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
one ink, on by default, off from the capsule (`?particles=off`, remembered
in `localStorage`, off by default under `prefers-reduced-motion`). Wind
narrows to the viewport while the overlay is off; with it on the session
takes the whole plane, since the particles respawn across the grid.
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
  "FORECAST HOUR" / `F058` / 模式周期 / 有效时间 for "TIME ELAPSED" /
  `T+058:24` / 观测起点 / 观测时间, on the viewer and the showcase cards.
- Valid times read in one display zone (`web/src/timezone.ts`): the
  browser's own, or the pinned point's while a probe is open (`tzf-wasm`, a
  4 MB index loaded on the first pin, `Etc/GMT±N` over open water).
  `displayZone` is a live binding (`onDisplayZoneChange`). The run cycle
  stays UTC wherever it is stamped, and so do the showcase cards. Offset
  labels (`UTC+9`) and zone ids are instrument text, English in every
  locale.
- URL state (`?model=`, `?type=`, `?lines=`, `?case=`, `?res=`,
  `?use_h264=`, `?particles=`, `?backend=`, `?tc=` with `?tcagency=` /
  `?tcmodel=` / `?tcmembers=`) is parsed in `urlstate.ts`; `?lang=` belongs
  to `i18n.ts` and `?theme=` to `theme.ts`. Unrecognized values fall back
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
