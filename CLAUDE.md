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
(`VITE_DATA_BASE_URL`, see `web/.env.deploy`).

Manifest schema changes are a two-sided deploy: the new shell accepts old
manifests, but an old cached shell rejects new ones — **deploy the Pages shell
before publishing data in a widened schema**.

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
  from, the published time axis as `(last_hour, step)` segments, which input
  variables are fetched, which bundles are published, the production grid, and
  fetch concurrency. **Adding or changing a model starts here**, and the
  frontend mirror is `FORECAST_MODELS` in `web/src/manifest.ts`. A source with
  `observation=True` (`radar`) is not a forecast at all: no live pointer, no
  cron job, no fetch — one local NetCDF file per event, read by
  `observation.py`, with whatever time axis the file carries.
- `variables.py` — the variable registry, in GRIB2's own terms: the parameter
  triple, the fixed surface, the container's `numericId`, the metadata label
  and unit, plus the GRIB matching hints (element, `.idx` phrase, ECMWF
  param). One entry per variable feeds both record matching and the schema v3
  metadata block. Some entries are *input-only* (no `numeric_id`): ECMWF `tp`
  de-accumulates into `prate`, sflux `prate_ave` de-averages into `prate`;
  neither reaches a bundle.
- `fetch.py` → `idx.py` / `grib2.py` — byte-range fetches of exact GRIB
  records; ECMWF open data is CCSDS-packed and is repacked to `grid_simple`
  with `grib_set` at fetch time.
- `binconvert.py` — the whole conversion: grid discovery, cropping
  (`crop_grid`, showcase cases), unit conversion, de-accumulation /
  de-averaging, quantization, temporal grouping, bundle writing, half-res
  variants, posters, H.264 companions, manifest entries.
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

External tools are invoked as CLI subprocesses (`gdal.py`, `zstdcli.py`,
`ffmpegcli.py`, `eccodescli.py`) rather than added as binary Python
dependencies; NumPy is the only runtime dependency. Two exceptions:

- **zstd** runs in-process via the stdlib `compression.zstd` on Python ≥ 3.14
  (subprocess overhead dominated bundle writing) and falls back to the CLI
  below that — the two are interchangeable on decode but not byte-identical
  on encode.
- **`gdalinfo`** has a second source. `gdal.dataset_info` is the one entry
  point for it, and reads through the `xuepy` wheel's linked GDAL
  (`xue.gdal_info`, one reason for the `xuepy>=0.6` floor) when the build converts
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

`main.ts` (large, deliberately central) picks the delivery path per session:
WebCodecs only when `?use_h264=true` opts in and a video artifact exists and
the browser supports it, otherwise streaming if a range probe succeeds,
otherwise a whole-bundle download; and picks a resolution tier via
`pickBundleVariant` from the viewport and connection. The video path is off by
default — the Xue decoder is the everyday path, and `use_h264` (parsed in
`urlstate.ts` like the rest of the URL state) is what turns the companions
back on.
`layer.ts` renders one quantized R8 plane with inverse Web Mercator and a
palette lookup in the fragment shader, blending two frames via `u_mix` (never
animate raster opacity). The same shader draws the **pressure family** (mean
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
encoder, the Rust encoder and the frontend. `particles.ts` renders 10 m wind. `playback.ts` holds
the frame-rate ladder and the per-frame dwell that keeps a mixed-step axis
moving at one apparent speed.

The shell is a map with controls floating over it, not a map beside a panel:
the display-serif title in the top-left corner names the layer *and* opens the
run picker (`#model-sheet` — a panel under it on desktop, a bottom sheet on
phones), three round buttons sit top-right (locale, cases, appearance), the
color scale runs down the left edge, one 48px tile per layer down the right,
and one capsule at the bottom holds the whole transport: the pressure family's
level row, the forecast hour and valid time, the speed and play buttons, and a
track whose tick marks, playhead and day labels *are* the slider's appearance
(the `<input type=range>` itself is transparent and only carries the hit area,
the keyboard and the accessible name). `src/theme.ts` resolves light/dark once
per page load the way `i18n.ts` resolves the locale, and the toggle reloads for
the same reason: the basemap flavor is baked into the style, and swapping it
would mean `map.setStyle`, which drops the custom WebGL layers the forecast is
drawn in. `index.html` repeats that detection inline so the shell never paints
on the wrong ground first.

Light does not mean a light map. Each layer keeps the ground its palette needs
— a warm sheet under temperature, a dark slate under precipitation, wind,
radar and solar (their palettes run translucent at the low end and vanish on
paper), chart stock under the pressure family — so the chrome's tokens and the
map's are two different things. The chrome (capsule, rail, sheet, cards) always
uses the theme's own; anything floating *directly* on the map (the title, the
color scale's numbers, the credits) uses `--map-ink` / `--map-ink-muted`, which
follow `body[data-ground]` — stamped by `applyBasemapTheme` from the basemap
tone's luminance. The top/bottom map fade follows the same attribute, and so
do the basemap's own place labels and boundaries: a Protomaps flavor is baked
into the style at construction and cannot be swapped without `setStyle`, so
`applyBasemapInk` repaints their text and line colors instead.

The Protomaps key is origin-locked to the production domains **and to
`localhost`** — not to `127.0.0.1`, which is what `playwright.config.ts` serves
from, so the e2e suite stubs tiles out. To see the app over the real basemap
locally, browse `http://localhost:4173` rather than the loopback address.

## Conventions

- Locale is `zh`/`en` via `web/src/i18n.ts` and appearance is `light`/`dark`
  via `web/src/theme.ts`; both are fixed per page load and both toggles
  persist the choice and reload onto it. Only human-facing copy is
  translated; thrown `Error` messages, worker messages and diagnostics stay
  English in both locales.
- Timeline copy follows the *kind* of dataset, not the locale:
  `isObservationModel` (the frontend mirror of `SourceSpec.observation`)
  swaps "FORECAST HOUR"/`F058`/模式周期/有效时间 for
  "TIME ELAPSED"/`T+058:24`/观测起点/观测时间, on the viewer and on the
  showcase cards. Observations have no run cycle and no lead time.
- URL state (`?model=`, `?type=`, `?case=`, `?use_h264=`) is parsed in
  `urlstate.ts`; `?lang=` belongs to `i18n.ts` and `?theme=` to `theme.ts`,
  since each is read before anything else renders. Unrecognized values fall
  back to defaults rather than error.
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
