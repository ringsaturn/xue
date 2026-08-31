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

Inside a `.xue` file, `schemaVersion` describes the *time axis only*: v1 is a
uniform axis declaring `stepHours`; v2 lists forecast `hours` outright,
required whenever the axis changes cadence (GFS/sflux go 3-hourly after f120,
ECMWF 6-hourly after f144). Encoder (`xue/binconvert.py::_time_metadata`),
Python reader (`xue/binformat.py::_parse_time_axis`), Rust
(`rust/xue/src/lib.rs`) and `web/src/manifest.ts::timeAxisHours` must agree.
Do not conflate this with the manifest's schema v5 or the pointer's v1.

### Encoder pipeline (`xue/`)

- `sources.py` — the per-model registry (`SourceSpec`): where GRIB comes from,
  the published time axis as `(last_hour, step)` segments, which input
  variables are fetched, which bundles are published, the production grid, and
  fetch concurrency. **Adding or changing a model starts here**, and the
  frontend mirror is `FORECAST_MODELS` in `web/src/manifest.ts`.
- `variables.py` — per-variable GRIB identification (element, `.idx` phrase,
  GRIB2 discipline/category/number/level, ECMWF param) plus the value range
  the codebook is built from. Some entries are *input-only*: ECMWF `tp`
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
- `showcase.py` — case definitions → cropped bundles → `showcase.json`.

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
