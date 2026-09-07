# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Xue packs global weather forecast runs into a custom per-variable binary
container (`.xue`) and plays them back in a static browser page. Three
implementations of one format live here and must stay in agreement:

- **Python encoder** — `xue/` (fetch → GDAL extract → quantize → temporal
  residuals → zstd → container → manifest).
- **Rust decoder** — `rust/xue` (core crate) and `rust/xue-wasm`
  (wasm-bindgen bindings, built into `web/src/wasm/`).
- **TypeScript frontend** — `web/src/` (manifest resolution, decode worker,
  WebGL2 layers, playback).

Beside them, and *not* part of the delivery contract: **`rust/xue-encode`**, an
experimental native port of `convert-bin` (its own cargo workspace, plus a
PyO3 wrapper under `rust/xue-encode/python`). It links GDAL, grib-rs and zstd
in process instead of shelling out, and is held to the Python encoder by
byte-for-byte identical output — see `rust/xue-encode/README.md`. The Python
encoder stays the reference; a format change goes there first. It publishes as
the `xue-encode` crate plus a wheel carrying its own minimal GDAL, both on
`encoder-v*` tags (the decoder crate's `v*` tags are separate, and the two
version independently).

`docs/format.md` is the normative spec. `README.md` covers usage and
publishing; `showcase/README.md` covers authoring historical cases.

## Commands

```sh
make check                       # verify GDAL / zstd / node / wasm-pack versions
make wasm                        # build the WASM decoder into web/src/wasm/ (generated, gitignored)
make mvp [MODEL=gfs|ecmwf|sflux] # check + install + wasm + build a run + vite build
make serve                       # vite preview on 127.0.0.1
npm run dev                      # vite dev server

make test                        # rust + python + web unit tests
make test-rust                   # regenerates the golden fixture, then cargo test
make test-e2e                    # playwright (needs `npx playwright install chromium`)
make encoder-rust                # build the experimental native encoder (rust/xue-encode)
make encoder-rust-test           # its unit tests plus the byte-identity golden test
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

Encoder (`xue/binconvert.py::build_metadata`), Python reader
(`xue/binformat.py::_parse_metadata`), Rust (`rust/xue/src/lib.rs`) and
`web/src/manifest.ts::parseBundleMetadata` must agree. Do not conflate this
with the manifest's schema v5 or the pointer's v1. Like a manifest widening,
a metadata version bump is a two-sided deploy: **ship the Pages shell before
publishing data at the new version.**

### Encoder pipeline (`xue/`)

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
- `observation.py` — the NetCDF ingest: one `gdalinfo` pass turns a file's
  bands into the same `SourceFrame` list the GRIB inspectors return, plus the
  `PlaneSource` saying to unscale the values and what its fill value means.
- `showcase.py` — case definitions → cropped bundles → `showcase.json`. An
  observation case names a local `dataset` file instead of a `run` to fetch
  (`XUE_OBSERVATION_ROOT`).

External tools are invoked as CLI subprocesses (`gdal.py`, `zstdcli.py`,
`ffmpegcli.py`, `eccodescli.py`) rather than added as binary Python
dependencies; NumPy is the only runtime dependency. The exception is zstd,
which runs in-process via the stdlib `compression.zstd` on Python ≥ 3.14
(subprocess overhead dominated bundle writing) and falls back to the CLI
below that — the two are interchangeable on decode but not byte-identical on
encode.

Errors that are the user's to fix subclass `XueError` (`xue/errors.py`); the
CLI turns them into `error: …` and exit code 2. Anything else is a bug.

### Decoder and frontend

`rust/xue/src/lib.rs` exposes `Bundle` (whole file in memory) and
`StreamingBundle` (structural prefix only, payload bytes fed in as range
responses arrive) over shared validation and decode code. All arithmetic on
file values is checked, and nothing is allocated from a file value before
validation.

`web/src/worker.ts` owns the WASM decoder and speaks one message protocol
(`booted` → `init`/`init-stream` → `ready`, then `decode` → `frame`) in both
full and streaming modes. `web/src/webcodecs.ts` implements the same protocol
over a native `VideoDecoder` for the H.264 companion artifacts, so `main.ts`
holds either one in the same field without branching. Prefetch in both is
windowed: the main thread sends `prefetch-window` with the hours just ahead of
the playhead plus a concurrency cap.

`main.ts` (large, deliberately central) picks the delivery path per session:
WebCodecs if supported and a video artifact exists, otherwise streaming if a
range probe succeeds, otherwise a whole-bundle download; and picks a
resolution tier via `pickBundleVariant` from the viewport and connection.
`layer.ts` renders one quantized R8 plane with inverse Web Mercator and a
palette lookup in the fragment shader, blending two frames via `u_mix` (never
animate raster opacity). `particles.ts` renders 10 m wind. `playback.ts` holds
the frame-rate ladder and the per-frame dwell that keeps a mixed-step axis
moving at one apparent speed.

## Conventions

- Locale is `zh`/`en` via `web/src/i18n.ts`, fixed per page load. Only
  human-facing copy is translated; thrown `Error` messages, worker messages
  and diagnostics stay English in both locales.
- Timeline copy follows the *kind* of dataset, not the locale:
  `isObservationModel` (the frontend mirror of `SourceSpec.observation`)
  swaps "FORECAST HOUR"/`F058`/模式周期/有效时间 for
  "TIME ELAPSED"/`T+058:24`/观测起点/观测时间, on the viewer and on the
  showcase cards. Observations have no run cycle and no lead time.
- URL state (`?model=`, `?type=`, `?case=`, `?lang=`) is parsed in
  `urlstate.ts`; unrecognized values fall back to defaults rather than error.
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
