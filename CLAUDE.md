# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Xue packs global weather forecast runs into a custom per-variable binary
container (`.xue`) and plays them back in a static browser page. Three
implementations of one format live here and must stay in agreement:

- **Python encoder** — `xuebuild/` (fetch → GDAL extract → quantize → temporal
  residuals → zstd → container → manifest). The name is deliberate: this is
  the build pipeline, and `xue` on the Python side is the native binding.
- **Rust decoder** — `rust/xue` (core crate) and `rust/xue-wasm`
  (wasm-bindgen bindings, built into `web/src/wasm/`).
- **TypeScript frontend** — `web/src/` (manifest resolution, decode worker,
  WebGL2 layers, playback).

Beside them, a native port of `convert-bin` at `rust/xue/src/encode/`, behind
the `xue` crate's off-by-default `encoder` feature, plus a PyO3 wrapper at
`rust/xue-py/` (distribution `xuepy`, imported as `xue`, carrying the decoder
too). It links GDAL, grib-rs and zstd in process instead of shelling out, and
is held to the Python encoder by byte-for-byte identical output — see
`docs/encoder.md`.

`xuebuild` depends on the `xuepy` wheel and converts through it by default:
`xuebuild/encoder.py` dispatches, `XUE_ENCODER` (`auto` | `native` | `python`)
overrides, and the scheduled `publish-*` workflows pin `native` so a silent
fall back to the slow path cannot happen unnoticed. The native encoder writes
no video and no live pointer, so `xuebuild/native.py` supplies both: it reads
the codes back out of the bundles it just wrote (with the decoder the same
wheel carries), hands them to the existing ffmpeg encoder, folds the
descriptors into the manifest and only then writes the pointer, whose CRC32
covers the finished manifest. `tests/test_native.py` is what holds the two
encoders together end to end — every artifact of one build compared byte for
byte against the other.

The Python encoder stays the reference; a format change goes there first, and
the native side follows it.

The feature is off by default because it links GDAL and the decoder does not,
and the two are separated by cargo profile as well: `release` stays tuned for
the wasm decoder, `encoder` inherits it at `opt-level = 3`. Profiles live in
`rust/Cargo.toml` because Cargo reads them from the workspace root only.

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

`web/src/wasm/` is generated and gitignored — the frontend will not typecheck
or build until `make wasm` has run.

## Architecture

### Delivery contract (touches all three implementations)

Two layers, both versioned:

- The only **mutable** object per model is a tiny live pointer at the data
  root (`latest.json` for GFS, `latest-<model>.json` otherwise, pointer schema
  v1). Uploading it is what takes a run live.
- Everything it names — the run's `manifest.json` (schema v5) and every
  artifact — is **immutable**, addressed with `?v=<crc32>` cache busting, and
  lives under `<model>.<run>/`.

Manifest paths are resolved relative to the manifest URL, so a run directory
can be served from either the site origin or the R2 bucket
(`VITE_DATA_BASE_URL`, see `web/.env.deploy`). The production hostnames
appear in the frontend only through `web/src/site.ts`.

### Discovery (search engines, language models, agents)

The shell describes itself in three places that must stay in agreement with
what the encoder publishes. `web/index.html` and `web/showcase.html` carry
the static English metadata, the hreflang set and a JSON-LD graph (site,
application, source code, the live dataset); `web/src/pagemeta.ts` rewrites
title, description, canonical and og:url once the view is known — a case
(`/?case=<id>`) is its own page, every live view is `/`, and a `?lang=`
rendering is canonical to itself. `web/public/robots.txt` (allows every
crawler, AI ones by name) and `web/public/llms.txt` (the llmstxt.org index:
sources, published bundle set, URL grammar, the pointer → manifest → bundle
contract, decoder packages) are static prose — **update `llms.txt` when
`sources.py` changes what a model publishes or `urlstate.ts` gains a
parameter**. `sitemap.xml` (the two pages plus one URL per case) and
`llms-full.txt` (README + `docs/*.md` + `showcase/README.md`, links
rewritten to the repository) are generated at build time by
`web/tooling/discovery.ts`, which is why `deploy-pages.yml` also triggers on
those documents. The sitemap's cases come from the *published* catalog
(`showcase.json` at the build's `VITE_DATA_BASE_URL`), never from
`showcase/cases/` — a definition may sit unbuilt for a long time — so a
deploy build fails if the bucket does not answer, and a newly uploaded case
reaches the sitemap at the next shell deploy.

Manifest schema changes are a two-sided deploy: the new shell accepts old
manifests, but an old cached shell rejects new ones — **deploy the Pages shell
before publishing data in a widened schema**. The *set* of bundles is not a
schema: a manifest's `variable` is admitted on its shape alone
(`^[a-z][a-z0-9]*$`, unique; `tmp2m` and `prate` still required on a live
run) in all three validators, so a run may publish a bundle the shell has
never heard of and the shell renders it generically instead of refusing the
manifest. Adding a level of a known family is two source-table lines and no
frontend change; a new quantity is an encoder registration plus, optionally,
frontend chart knowledge.

Inside a bundle, `numericId` / `variableId` is a **file-local handle**: both
encoders number a bundle's variables 1..n in bundle order, so every scalar
bundle carries variable 1 and every vector bundle 1 (u) and 2 (v). A
variable's identity is its GRIB2 `parameter` block (`web/src/identity.ts`
derives family, level and vector-ness from it once a session is open; the id
string is only the naming convention the rail and `?type=` read before
then). Nothing may key across sessions by `numericId` alone —
`web/src/sessionkeys.ts` scopes the frame cache and probe keys by session,
and each session's worker is bound to it by closure.

### Container versions

Distinct from the metadata schema below: `FixedHeader.version` says what a
*payload* is. **v1** is plane-major — one payload is one whole plane of one
frame, indexed by `IDX1` + `PlaneEntry`, with a middle RAW anchor per
temporal group. **v2** is tiled — one payload is a **chunk**: one spatial
tile of one temporal group for one variable, indexed by `IDX2` plus three
compact tables (variables with their predictor, groups partitioning the
axis, and one 8-byte entry per chunk whose offset is a prefix sum rather
than a stored field). Physical order is group → tile (row-major) → variable,
which keeps a whole group one contiguous range (so a global view fetches
exactly what v1 fetched), a viewport's tile row another, and one cell's
whole series at one chunk per group. Tiling is a storage property: the
metadata JSON, codebooks and residual arithmetic are unchanged, and the
geometry appears only in the binary index. `docs/format.md` §"Container v2"
is normative.

Status: both encoders write v2 and every decoder reads both versions. The
frontend uses what tiling buys: a streaming session decodes and fetches only
the tiles its viewport covers (`web/src/tiles.ts` turns the view into
rectangles, the worker protocol carries them, `u_cover` in `layer.ts` clips
to what a partial plane actually holds), and a pinned point reads its whole
series in one round trip instead of waiting for playback to walk the axis.
A narrowed session never fetches the rest of the grid, so "resident" means
what the view needs, not the whole file — the worker says which, and the
data card reads "Viewport fully buffered" for the narrow case. A decoder
must always keep reading v1 — published runs and showcase cases carry those
bytes and are never rebuilt. Like a manifest widening, the switch is a
two-sided deploy: **ship the Pages shell before publishing v2 data.**

### Bundle metadata schema versions

Inside a `.xue` file, `schemaVersion` is the lowest version a reader must
implement. v1 and v2 are the legacy whole-hour axes (`firstForecastHour`
with `stepHours` or `hours`) that published runs still carry. **v3** — what
the encoder writes now — changes two things: every variable declares its
GRIB2 `parameter` block (discipline / category / number plus the fixed
surface), and the time axis becomes unit-neutral (`unitSeconds` +
`firstFrameOffset` + `frameStep` | `frameOffsets`), so a sub-hourly series
has an exact axis. `unitSeconds` is 3600 for every forecast source, which
leaves their offsets equal to their forecast hours; the radar mosaic
declares 360. It must be the coarsest unit that fits, and a decoder rejects
both an unimplemented version and an overdeclared one, so each file has
exactly one valid encoding.

Within the container a plane's key is a **frame offset**, not a forecast
hour: `PlaneEntry.frameOffset`, the worker protocol's `frameOffset`, and
`SourceFrame.lead_seconds` upstream of the axis derivation.

Encoder (`xuebuild/binconvert.py::build_metadata`), Python reader
(`xuebuild/binformat.py::_parse_metadata`), Rust
(`rust/xue/src/decode/metadata.rs`) and
`web/src/manifest.ts::parseBundleMetadata` must agree. Do not conflate this
with the manifest's schema v5 or the pointer's v1. Like a manifest widening,
a metadata version bump is a two-sided deploy: **ship the Pages shell before
publishing data at the new version.**

### Encoder pipeline (`xuebuild/`)

- `sources.py` — the per-model registry (`SourceSpec`): where the data comes
  from, the published time axis as `(last_hour, step)` segments (whose last
  boundary is `horizon_hours`, the `--hours` default), the cycle cadence
  (`cycle_hours`: 6 for the global models, 1 for HRRR — `parse_run` and
  `resolve_run` read it), which input variables are fetched, which bundles
  are published, the production grid, and fetch concurrency. **Adding or
  changing a model starts here**, and the frontend mirror is
  `FORECAST_MODELS` in `web/src/manifest.ts`. A source with
  `observation=True` (`radar`) is not a forecast at all: no live pointer, no
  cron job, no fetch — one local NetCDF file per event, read by
  `observation.py`, with whatever time axis the file carries. A source with
  a `regrid` (`hrrr`) is computed on a map projection: `_grid_info` reads
  the Lambert conformal parameters out of GDAL's WKT (`reproject.py`, and
  the wheel's `gdal_info` reports `coordinateSystem.wkt` for it), builds a
  `Resampler` onto the regular grid of that step over the source's
  footprint, and `_extract_planes` resamples each plane (bilinear, edge
  cells continued into the corners the conic domain never covered) before
  the crop; `production_grid` and `tile` describe the regular grid. The
  arithmetic is repeated by `rust/xue/src/encode/reproject.rs` and held
  byte-identical (`tests/test_hrrr.py`), so its op order is not free. The
  shell knows the model's footprint (`web/src/domain.ts`,
  `FORECAST_MODELS[].domain`): the raster shader, the particles, the probe
  and the contour labels all clip to it, and `region` is where the camera
  goes when a regional model is opened on a view showing none of it.
- `variables.py` — the variable registry, in GRIB2's own terms: the parameter
  triple, the fixed surface, the metadata label and unit, plus the GRIB
  matching hints (element, `.idx` phrase and its alternates at another
  centre — HRRR's `MSLMA` under `prmsl`, `REFC` under `cref` — ECMWF
  param). One entry per
  variable feeds both record matching and the schema v3 metadata block; it
  assigns no container id (see the delivery contract above). Some entries
  are *input-only*: ECMWF `tp` de-accumulates into `prate`, sflux
  `prate_ave` de-averages into `prate`; neither reaches a bundle. The
  isobaric families (`hgt`, `tmp`, `rh`, `spfh`, `ugrd`/`vgrd`,
  `uqflx`/`vqflx`) are generated from one table of eight levels; `isobaric_variable(id)` answers `(family, level)`, and the
  vapour flux pair is derived in the converter (`q·V/g`), never fetched.
  Registration is not publication: `SourceSpec.bundle_scalar_ids` and
  `bundle_vector_ids` say what a source ships, and a vector bundle
  (`wind10m`, `wind<level>`, `qflux<level>`) ships only when every input in
  `binconvert.vector_input_ids` is fetched. `tests/fixtures/isobaric-registry.json`
  holds the three implementations to one set of ids and codebooks, the way
  `pressure-registry.json` does for the pressure family and
  `surface-registry.json` for the surface diagnostics (`gust`, the four
  cloud covers, `cape`, `vis`, `dpt2m`, `aptmp2m`). The isobaric families
  also include `vvel` (fetched) and `thetae` — the first **derived scalar**:
  `binconvert.DERIVED_SCALARS` names its inputs (`tmp<level>`,
  `spfh<level>`), `derive_theta_e` is Bolton (1980) in a fixed operation
  order the native encoder repeats, and like a vapour flux bundle it ships
  only when its inputs are fetched. GFS publishes all of these; ECMWF and
  sflux do not yet. The **ocean set** (`ocean-registry.json`: `tmpsfc`
  skin temperature / SST, `icec`, `icetk`, `htsgw`, `perpw`, `dirpw`) is
  GFS-only too and brings two mechanisms: a source may read a second **file
  family** of the same cycle (`SourceSpec.companion_files`, the `wave`
  family = `gfswave.*.global.0p25.fFFF.grib2`, appended by the fetcher
  after the pgrb2 records so a frame is still one GRIB — and a run is
  complete only when its wave frames are up too, usually within minutes
  of pgrb2 f240, occasionally 20 min after),
  and a record may not cover its grid (`VariableSpec.fill_values`: the
  wave bitmap's GDAL nodata 9999 becomes the codebook bottom before unit
  conversion, in both encoders — the format has no bitmap). WAVEWATCH III
  packs as JPEG 2000, which the wheel's GDAL reads through the OpenJPEG
  `scripts/build-gdal-minimal.sh` links for that one purpose
  (`tests/fixtures/gfswave.*.jp2.crop.grib2` holds it to the reference
  GDAL); a family some GDAL cannot read can still be marked
  `CompanionFile.repack`, which repacks it to `grid_simple` with `grib_set`
  at fetch time the way ECMWF's CCSDS files are and makes `bundle-groups`
  flag its jobs `eccodes` so `publish.yml` installs it — no family needs
  that now. The **wave vector** `wave` (`uwave` / `vwave`, Xue-local
  10/0/250–251) is the second derived vector after the vapour flux:
  `binconvert.DERIVED_VECTORS` names its inputs (`htsgw`, `dirpw`),
  `derive_wave_vector` lays the height along the direction of travel in
  the wind's convention (`-h sin θ, -h cos θ`) so the frontend's vector
  path — magnitude fill, particles, the probe's `atan2(-u, -v)` — draws
  the sea as it draws the wind. `htsgw` stays published beside it; `dirpw`
  is fetched as an input only (released after the derivation, like
  `spfh850`), since a scalar direction is no chart and the vector carries
  it.
  Widening a source's input list means recutting
  `tests/fixtures/gfs.*.crop.grib2` (same run, same `-srcwin`) and
  regenerating the registry fixtures.
- `fetch.py` → `idx.py` / `grib2.py` — byte-range fetches of exact GRIB
  records, one `.idx` + range set per file family; ECMWF open data is
  CCSDS-packed and is repacked to `grid_simple` with `grib_set` at fetch
  time.
- `binconvert.py` — the whole conversion: grid discovery (a global grid is
  snapped to `360 / width` — WAVEWATCH III writes the last longitude a hair
  off, and a wave-only bundle group must land on the same grid as its
  pgrb2 siblings; `grid.rs` repeats the rule), cropping (`crop_grid`,
  showcase cases), unit conversion, de-accumulation / de-averaging,
  quantization, temporal grouping, bundle writing, half-res variants,
  posters, H.264 companions, manifest entries.
- `quantize.py` / `temporal.py` / `binformat.py` — the format itself:
  codebooks, modulo-256 residual prediction, container read/write.
- `manifest.py` — manifest and live-pointer construction *and validation*;
  both are validated on write.
- `observation.py` — the NetCDF ingest: one `dataset_info` pass turns a file's
  bands into the same `SourceFrame` list the GRIB inspectors return, plus the
  `PlaneSource` saying to unscale the values and what its fill value means.
- `showcase.py` — case definitions → cropped bundles → `showcase.json`. An
  observation case names a local `dataset` file instead of a `run` to fetch
  (`XUE_OBSERVATION_ROOT`).
- `assemble.py` — a run built in pieces. The scheduled `publish.yml` fans a
  run out over one job per **bundle group** (`bundle-groups` packs the
  source's bundles into at most `max_jobs` jobs of roughly equal cost; the
  matrix also says which jobs need ffmpeg): each job runs `build-bin
  --bundles …`, which fetches only those bundles' inputs (into
  `data/raw/partial/<group>/`, never mistaken for a full fetch), converts
  with `bundle_ids` restricted and writes `manifest.part.<group>.json`
  beside the bundles instead of `manifest.json`; the finalize job merges
  the parts with `assemble-run` (every published bundle exactly once, in
  publication order, core pair required) and only then writes the pointer.
  `make upload-r2-bundles` / `upload-r2-manifest` are the two halves of
  `upload-r2`; a part never reaches the bucket. The same independence gives
  the **top-up**: when the resolved cycle is already live but lacks bundles
  the source now publishes, `publish.yml` builds only those
  (`bundle-groups --base-manifest`, against `make live-manifest`) and
  `assemble-run --base-manifest` merges the parts onto the live manifest —
  a new variable reaches the live run without a full rebuild; `force`
  still rebuilds everything. `tests/test_assemble.py` holds a split build
  and a top-up byte-identical to a whole one — a bundle's bytes must never
  depend on what else was in the build, so nothing cross-variable may creep
  into a bundle or its manifest entry.

### Tropical cyclone product (`xuebuild/tc/`)

Storm tracks are a second product beside the runs, not inside the
container: `docs/tc.md` is normative (schema v1). `xue tc-build` fetches
each source into `data/raw/tc.<issue>/<source>/` with a `fetch.json`
(`tc/fetch.py` — JTWC's RSS → `.tcw`, NHC's `CurrentStorms.json` → gzip
a-deck + b-deck, the NCEP tracker's `avno` and GEFS member files, ECMWF
`tf` BUFR through `eccodescli.bufr_dump_json`, IBTrACS), parses each with
a parser that imports no other (`atcf.py`, `tcw.py`, `bufrtracks.py`,
`ibtracs.py` → the shapes in `track.py`, SI units at the parser),
resolves identities (`identity.py`: ATCF id first; invests and
model-found systems get `x-<basin>-<hour>-<n>` ids that the **previous
hour's index** carries forward by alias and by proximity, so
`publish-tc.yml` runs `make live-tc-index` before building) and writes
`web/public/data/tc.<issue>/` plus `latest-tc.json` (`schema.py`
validates on write; `build.py` is the only place the sources meet). A
source fails on its own into `sources[]`; the pointer is withheld only
when no agency and no model contributed. `tests/fixtures/tc/` is a
fetched hour and `tests/fixtures/tc/expected/` the golden built from it
(`tests/prepare_tc_golden.py` regenerates; needs `bufr_dump`);
`tc-registry.json` pins `registry.py` and `web/src/tc/agencies.ts` to
one table (`tests/web/tc.test.ts`). The shell draws the product as
**marks** over any composition (`web/src/tc/`): `schema.ts` validates
what `schema.py` writes, `tracks.ts` fetches pointer → index → storm and
interpolates a track by valid time, `layers.ts` is MapLibre GeoJSON
layers (agency forecasts dashed ahead of the playhead and solid behind
it, the wind radii at the current position, best tracks, model tracks,
ensemble members off by default), `panel.ts` the storm sheet behind the
rail's `#tc-tile`. The marks take no session and never gate the playhead;
`main.ts::syncTcTime` hands them the frame's valid time, `loadTc` polls
the pointer with the runs, and a case hides them. URL state is
`?tc=<id>|off`, `?tcagency=`, `?tcmodel=`, `?tcmembers=`.

External tools are invoked as CLI subprocesses (`gdal.py`, `zstdcli.py`,
`ffmpegcli.py`, `eccodescli.py`) rather than added as binary Python
dependencies; NumPy is the only runtime dependency. Two exceptions:

- **zstd** runs in-process via the stdlib `compression.zstd` on Python ≥ 3.14
  (subprocess overhead dominated bundle writing) and falls back to the CLI
  below that — the two are interchangeable on decode but not byte-identical
  on encode.
- **`gdalinfo`** has a second source. `gdal.dataset_info` is the one entry
  point for it, and reads through the `xuepy` wheel's linked GDAL
  (`xue.gdal_info`, one reason for the `xuepy>=0.13.1` floor) when the build converts
  natively, the subprocess otherwise — the choice follows `XUE_ENCODER`, so
  a run never mixes two GDAL installs. That is what lets the scheduled
  `publish-*` workflows install **no GDAL at all**: extraction was already
  in the wheel, and inspection was the last caller left.
  `tests/test_gdalinfo.py` diffs the two sources field by field. Extraction
  (`gdal_translate`) has no such fallback, so the reference pipeline
  (`XUE_ENCODER=python`) still needs a system GDAL.

Errors that are the user's to fix subclass `XueError` (`xuebuild/errors.py`); the
CLI turns them into `error: …` and exit code 2. Anything else is a bug.

### Decoder and frontend

`rust/xue/src/decode/` exposes `Bundle` (whole file in memory) and
`StreamingBundle` (structural prefix only, payload bytes fed in as range
responses arrive) over shared validation and decode code, in four layers:
`metadata.rs` (grid, time axis, variable set), `structure.rs` (header
geometry, index, dependency chains), `core.rs` (payload residency and
residual replay) and `bundle.rs` (the two public readers). All arithmetic on
file values is checked, and nothing is allocated from a file value before
validation.

Under both directions sits `rust/xue/src/format.rs`: the container's byte
layout and nothing else — the constants, the `Predictor`/`Compression` enums,
and a `pack`/`unpack` pair for `FixedHeader`, `IndexHeader` and `PlaneEntry`.
The decoder unpacks through it and the native encoder packs through it, so a
field cannot drift between them; it validates only what a field's own
encoding demands and never allocates from a file value. Everything semantic
stays above it.

`web/src/worker.ts` owns the WASM decoder and speaks one message protocol
(`booted` → `init`/`init-stream` → `ready`, then `decode` → `frame`) in both
full and streaming modes. `web/src/webcodecs.ts` implements the same protocol
over a native `VideoDecoder` for the H.264 companion artifacts, so `main.ts`
holds either one in the same field without branching. Prefetch in both is
windowed: the main thread sends `prefetch-window` with the hours just ahead of
the playhead plus a concurrency cap.

`main.ts` (large, deliberately central) composes the view from **slots**
rather than holding one layer: a *fill* slot (temperature, precipitation,
reflectivity, radiation, wind speed) and a *lines* slot (the pressure
family), each a `ForecastLayer` instance of its own, each fed by its own
bundle session with its own worker, grid, tiles and resolution tier.
`ViewComposition {fill, lines}` says what is on screen; the **primary**
session (the fill's, or the lines' when nothing is filled) drives the
timeline, legend, data card and ground tone, and the lines slot as an
overlay follows it by *lead seconds* on its own axis — a frame not decoded
yet keeps the last one up, a lead time the overlay's axis lacks hides it.
The primary gates the playhead; overlays never do. Prefetch fans out to every
session on screen (the primary at the connection's concurrency, an overlay at
one), an overlay takes the half-resolution tier unless `?res=full`, and every
`?type=` of old is a composition with one slot filled, so the single-layer
views run exactly the code they always did. It also picks the delivery path
per session:
WebCodecs only when `?use_h264=true` opts in and a video artifact exists and
the browser supports it, otherwise streaming if a range probe succeeds,
otherwise a whole-bundle download; and picks a resolution tier via
`pickBundleVariant` from the viewport and connection, unless `?res=half` or
`?res=full` pins one end of the ladder. The video path is off by
default — the Xue decoder is the everyday path, and `use_h264` (parsed in
`urlstate.ts` like the rest of the URL state) is what turns the companions
back on.
`layer.ts` renders one quantized R8 plane with inverse Web Mercator and a
palette lookup in the fragment shader, blending two frames via `u_mix` (never
animate raster opacity). The same shader also draws **10 m wind** as a filled
speed field: `layer.ts::setVectorField` switches the data texture to RG8 (u
codes in red, v in green — the packing `particles.ts` already builds, which
`main.ts` interleaves once per frame and memoizes), reconstructs each channel
on its own and looks the palette up by `magnitude / maxMagnitude` — the same
path draws every **vector bundle** (`wind<level>`, `qflux<level>`), each with
its own ceiling from `levels.ts::vectorMaxMagnitude`. `web/src/levels.ts` is
the **family** registry: one rail tile per family (temperature from 2 m up,
wind from 10 m up, relative humidity, specific humidity, vapour flux,
vertical velocity, θe, pressure — and cloud cover, whose members are the
total and the three layers rather than isobaric surfaces, listed outright
in `FamilyInfo.members`), the level row on the capsule picks the member,
and the fill's group sits beside the lines' group. Past ten visible rail
tiles `main.ts` marks the rail dense and the stylesheet drops the tiles to
36px with a smaller icon, the letter caption kept. The rail is a scroll column in a fixed
box — under the zoom tile down to the capsule's bottom edge on desktop,
under the round controls down to the capsule on phones, where `main.ts`
publishes the capsule's measured height as `--capsule-height` — faded at a
clipped end like the level row, and its tiles are grouped by quantity in
`index.html` (temperature, water, wind and pressure, the remaining surface
diagnostics, the upper-air dynamics), so a new tile goes into its group
rather than onto the end. Over a field the lines group is on the
row whenever the run publishes a pressure surface: a pressed member is the
overlay (and pressing it again takes the lines off), none pressed means no
lines, so the overlay is always one press away in either direction. The same
shader draws the **pressure family** (mean
sea level pressure and the eight isobaric geopotential heights) as contour
lines instead of a filled field: `web/src/pressure.ts` holds the per-level
contour intervals and emphasised lines, `layer.ts::setContours` turns the
pass on, and the lines are found per pixel from the dequantized value and its
screen gradient — no marching squares, no CPU. What makes that legal is an
*encoder* rule: each pressure codebook's offset puts every standard contour
exactly half a code off, so a line never coincides with a flat plateau. The
8-bit staircase still shows in a weak gradient (1 hPa codes, 4 hPa lines),
so before contouring, `layer.ts::prerender` smooths each uploaded plane in
grid space — a separable Gaussian, coverage-renormalised, into a 16-bit RG8
texture the same shader then samples; `CONTOUR_SMOOTHING_CELLS` in `main.ts`
sets its width, and filled fields never go through it. The labels — a value on
each line, an H/L (高/低) with its value on each closed center — are the one
CPU step: `web/src/isolines.ts` reduces the displayed plane to the cells in
view, smooths it the same way, runs marching squares and a windowed-extremum
search, and `labels.worker.ts` runs that off the main thread; the result is a
small GeoJSON that two MapLibre symbol layers place (along the line, at the
point) with the basemap's own collision handling, a halo in the ground tone
opening the gap in the line. Playback throttles the trace to once a second
so labels do not crawl along moving lines; a stop, a step or a pan refreshes
at once.
`tests/fixtures/pressure-registry.json` is the committed golden that holds
the codebooks, intervals and emphasised lines identical across the Python
encoder, the Rust encoder and the frontend. `particles.ts` advects GPU
particles through that same wind field, as an overlay over the colored field
rather than the layer itself: it is on by default, drawn in one ink (a second
speed ramp over the first reads as mud), and the viewer switches it off from
the transport capsule — `?particles=off`, remembered in `localStorage`, and
off by default under `prefers-reduced-motion`, where the simulation is frozen
anyway. Wind narrows to the viewport like any other streaming scalar while
the overlay is off; the particles respawn across the whole grid, so with them
on the session takes the whole plane. `playback.ts` holds
the frame-rate ladder and the per-frame dwell that keeps a mixed-step axis
moving at one apparent speed.

A click on the map pins a **point probe**: `probe.ts` turns the click into a
grid cell, and on a v2 bundle the worker's `series` message reads that
cell's whole axis in one round trip (one chunk per temporal group of one
tile); a v1 bundle or the video path fills it in opportunistically from
the planes decoded for the screen. The probe's panel docks over the
transport capsule at the capsule's width — a ring on the map marks the
point — and under the headline (the field on screen at the playhead) sit
the **meteogram rows** (`meteogram.ts`): temperature with the dew point,
precipitation, wind with the gust and direction arrows, the cloud layers
(the total standing in when no layer is published), sea level pressure.
Rows come from the manifest, not the model: a run publishes what it
publishes and the rest are simply absent. Each row's bundle is opened as a
*probe session* — `loadVariable(id, sequence, "probe")`, the primary's tier
on the streaming path alone, never the video path and never a whole
download (a host that cannot serve ranges leaves the row empty) — so the
rows cost the structural prefix plus one tile's chunks apiece. Every row
reads the primary axis by lead seconds (`alignSeries`), so a bundle whose
axis lacks a frame leaves a gap rather than a shifted column; the row
labels and readouts are DOM text on the same pitch as the canvas the
traces are drawn on; a press on either chart scrubs the timeline. Only the
numeric rows exist — no per-column weather icons. The panel and the
capsule are laid out on the same two columns — `--probe-column` for the
labels, the rest for the axis — so the sparkline's plot, the traces and
the track run on one line and their three playheads coincide; the capsule
reads down its label column the way a meteogram row does (caption, frame,
valid time) and keeps the level chips and the transport buttons on the
axis column, which is what holds it to two rows.

The shell is a map with controls floating over it, not a map beside a panel:
the display-serif title in the top-left corner names the layer *and* opens the
run picker (`#model-sheet` — a panel under it on desktop, a bottom sheet on
phones), three round buttons sit top-right (language, cases, appearance — the
language one opening a picker of the eleven endonyms through the same sheet
mechanism, `web/src/sheet.ts`, that `#model-sheet` and the sources sheet
use), the color scale runs down the left edge, one 48px tile per layer down the right,
and one capsule at the bottom (960px wide at most) holds the whole transport
in two rows: the pressure family's level chips with the speed and play
buttons, then the forecast hour and valid time beside a track whose tick
marks, playhead, day labels and end labels *are* the slider's appearance
(the `<input type=range>` itself is transparent and only carries the hit area,
the keyboard and the accessible name). `src/theme.ts` resolves light/dark
before the first render the way `i18n.ts` resolves the locale, and both
switch **in place**, never by reload: `toggleTheme` / `setLocale` persist the
choice, restamp the document and notify listeners (`onThemeChange`,
`onLocaleChange`), and `main.ts::applyAppearance` / `applyLocale` repaint
what the stylesheet cannot. The basemap is the delicate part — a Protomaps
flavor and label language are baked into the style, and `map.setStyle`
would drop the custom WebGL layers the forecast is drawn in — so
`syncBasemapStyle` builds the style again and applies the property-level
diff (paint, layout, filter, sprite) to the layers already on the map.
`isDark`, `locale`, `htmlLang` and `basemapLang` are live bindings: read
them at use, never capture them in a module-level constant. `index.html`
repeats the theme detection inline so the shell never paints on the wrong
ground first.

Light does not mean a light map. Each layer keeps the ground its palette needs
— a warm sheet under temperature, a dark slate under precipitation, wind,
radar and solar (their palettes run translucent at the low end and vanish on
paper), chart stock under the pressure family — so the chrome's tokens and the
map's are two different things. The chrome (capsule, rail, sheet, cards) always
uses the theme's own; anything floating *directly* on the map (the title, the
color scale's numbers, the credits) uses `--map-ink` / `--map-ink-muted`, which
follow `body[data-ground]` — stamped by `applyBasemapTheme` from the basemap
tone's luminance. So do the basemap's own place labels and boundaries: a
Protomaps flavor is baked into the style at construction and cannot be swapped
without `setStyle`, so `applyBasemapInk` repaints their text and line colors
instead. There is no fade at the top or bottom of the map: the title carries
its own text shadow and the capsule its own surface.

The round controls top-right, the zoom tile under them, the layer rail and
the credit mark at the foot share one 44px column at the same right offset
(20px, 16px on phones), so the four read as one vertical axis; a control
added to that edge keeps to it. The credit is a mark, not a row: the capsule
sits on the bottom edge, the sources sheet is behind the mark at every
width, and the line beside it (basemap always; a dataset's own notice via
`body[data-model]` only while that dataset is up) shows only where it fits
next to the capsule — above 1400px.

The Protomaps key is origin-locked to the production domains **and to
`localhost`** — not to `127.0.0.1`, which is what `playwright.config.ts` serves
from, so the e2e suite stubs tiles out. To see the app over the real basemap
locally, browse `http://localhost:4173` rather than the loopback address.

## Conventions

- Locale is one of eleven — `zh`, `zh-Hant`, `en`, `ja`, `ko`, `de`, `fr`,
  `es`, `pt`, `tr`, `ru` — via `web/src/i18n.ts`, and appearance is `light`/`dark` via
  `web/src/theme.ts`; both are resolved before the first render, and the
  language picker and the appearance toggle each persist the choice and
  switch in place (see the shell section above). Long-lived status copy in
  `main.ts` goes through `say()` so a switch can restate it. The
  dictionary is one module per language under `web/src/locales/`, each typed
  `Record<MessageKey, string>` against `en.ts` — the source of truth, and the
  only one carrying the design notes on what a string has to fit — so `tsc`
  rejects a missing or stray key. Only human-facing copy is translated;
  thrown `Error` messages, worker messages, instrument-panel codes (`F058`,
  `12 FPS`, `PLAY`) and diagnostics stay English in every locale.
- Timeline copy follows the *kind* of dataset, not the locale:
  `isObservationModel` (the frontend mirror of `SourceSpec.observation`)
  swaps "FORECAST HOUR"/`F058`/模式周期/有效时间 for
  "TIME ELAPSED"/`T+058:24`/观测起点/观测时间, on the viewer and on the
  showcase cards. Observations have no run cycle and no lead time.
- Valid times read in one **display zone** (`web/src/timezone.ts`): the
  browser's own, or the pinned point's while a probe is open — `tzf-wasm`
  answers the point → zone lookup, a 4 MB index loaded on the first pin and
  never before, and `Etc/GMT±N` over open water. `displayZone` is a live
  binding like `theme` and `locale` (`onDisplayZoneChange` repaints the
  capsule readout, tooltip and day marks). The run cycle stays UTC wherever
  it is stamped — 00Z is the cycle's name — and so do the showcase cards.
  Offset labels (`UTC+9`) and zone ids are instrument text, English in
  every locale.
- URL state (`?model=`, `?type=`, `?lines=`, `?case=`, `?res=`,
  `?use_h264=`, `?particles=`, `?tc=` with `?tcagency=` / `?tcmodel=` /
  `?tcmembers=`) is parsed
  in `urlstate.ts`; `?lang=` belongs to `i18n.ts` and `?theme=` to `theme.ts`,
  since each is read before anything else renders. Unrecognized values fall
  back to defaults rather than error. The camera is in the fragment, not the
  query string — `#map=<zoom>/<lat>/<lon>`, MapLibre's own `hash: "map"`,
  which reads it at construction and rewrites it on every `moveend` — so a
  pan never touches the canonical URL. `urlstate.ts::parseCameraFromHash`
  only says whether a link fixed the view: `initialize({ frame })` frames
  the dataset's region (a case's box; `FORECAST_MODELS[].region` through
  `frameModelRegion`, unless `regionShareOfView` says the map is already
  over it) on a model switch and on a first open without a camera, and
  never on a retry or a new run.
- The Python encoder and Rust decoder are held byte-identical by golden tests
  (`rust/xue/tests/golden.rs`) against fixtures built by
  `tests/prepare_bin_fixture.py`. A format change means changing the spec, both
  implementations, and the fixtures together.
- Playwright fixtures are synthetic and built by
  `tests/prepare_web_fixture.py` from Playwright's global setup — no network,
  no GDAL.
- Generated and fetched data (`data/raw/`, `data/work/`, `dist/`,
  `dist-deploy/`, `web/public/data/<model>.<run>/`, `web/src/wasm/`,
  `tests/fixtures/generated/`) is gitignored; never commit it.
- `plans/` is a local symlink to private design notes, excluded via
  `.git/info/exclude`. It may be absent.
- Commit subjects are lowercase and imperative, optionally prefixed with a
  scope (`ci:`, `web:`, `docs:`, `fix:`).
