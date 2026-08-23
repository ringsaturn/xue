# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Pipeline that packs global forecasts (2 m temperature `tmp2m`, precipitation rate `prate`, 10 m wind `ugrd10m`/`vgrd10m`, solar radiation `dswrf` on sflux only, f000–f120) into a custom per-variable spatiotemporal binary format (Xue, `.xue`), plus a static MapLibre page that decodes it with a Rust WASM worker and renders via a custom WebGL2 layer (inverse Web Mercator + palette lookup done in the shader). Three sources share the whole pipeline (`--model gfs|ecmwf|sflux`, registry in `sources.py`): NOAA GFS 0.25° (hourly, 121 frames), ECMWF IFS open data (3-hourly, 41 frames; CCSDS-packed GRIB repacked via eccodes `grib_set` at fetch time because GDAL builds often lack libaec, and prate derived by de-accumulating `tp`), and GFS sflux on the native ~13 km Gaussian grid (hourly; planes rolled to the −180-first layout at extract time; prate de-averaged from window-cumulative `PRATE ave` records, so its axis starts at f001; adds the `dswrf` bundle). Each model is an independent dataset: `gfs.<run>/` + `latest.json`, `ecmwf.<run>/` + `latest-ecmwf.json`, `sflux.<run>/` + `latest-sflux.json`. The wind pair ships as one two-variable `wind10m.xue` bundle (optional in inputs and manifest, like `dswrf`) and renders through a GPU particle layer. Three languages, one format:

- **Python encoder** (`xue/`): fetch → convert → quantize → temporal residuals → zstd → container. GDAL and ffmpeg are invoked as CLI subprocesses (`gdal.py`, `ffmpegcli.py`) — no binary Python dependencies. zstd (`zstdcli.py`) uses the stdlib `compression.zstd` in-process on Python ≥ 3.14 (per-plane subprocess overhead was the build bottleneck) with a zstd-CLI fallback for older interpreters; bundles are written and read-back-verified concurrently through one shared machine-sized pool. Band discovery reads GRIB2 section 0/1/4 headers directly (`grib2.py`, ~1 ms/file vs ~1 s/file for `gdalinfo -json`); one real gdalinfo pass per run doubles as the wind-availability probe and a cross-check of the header index, and any parse failure or disagreement falls the run back to full gdalinfo inspection.
- **Rust decoder** (`rust/xue` core crate; `rust/xue-wasm` bindings). `make wasm` builds it into `web/src/wasm/` (gitignored generated output consumed by the frontend).
- **TypeScript frontend** (`web/src/`): `main.ts` orchestrates; `manifest.ts` resolves the mutable `latest.json` live pointer → per-run `manifest.json` and picks a resolution tier (`pickBundleVariant`); `worker.ts` owns WASM decode, a byte-budgeted plane cache, and windowed prefetch; `layer.ts` is the MapLibre custom layer with two-texture blend playback; `particles.ts` is the wind GPU particle layer (u/v interleaved into one RG8 texture, ping-pong state/trail textures, speed palette); `poster.ts` decodes fast-first-frame posters via `DecompressionStream`; `webcodecs.ts` is the alternate H.264 path (any variable; used only when the stream is not larger than the `.xue`); `i18n.ts` is the zh/en UI dictionary (locale = `?lang=` param > persisted footer-toggle choice > `navigator.language`, fixed per page load; the Protomaps basemap label language follows it; thrown Error diagnostics stay English in both locales).

The format spec is `docs/format.md` (normative — update it if the format changes). The README is `README.md` (English).

## Commands

`make check` verifies external tools: Python ≥3.12 (use `uv sync` → `.venv`), GDAL ≥3.8 with GRIB driver, zstd CLI, Node ≥22, Rust + `wasm-pack`.

Build and run:

```sh
make mvp                 # fetch latest run, build .xue bundles + manifest, build frontend
make mvp RUN=2026081600 HOURS=120 PROFILE=balanced FORCE=--force
make mvp MODEL=ecmwf     # same, from ECMWF IFS open data (needs eccodes grib_set)
make serve               # vite preview of dist/
npm run dev              # dev server (reads web/public/data/ directly)
make wasm                # rebuild WASM decoder after Rust changes
```

CLI equivalents: `python -m xue {fetch,convert-bin,verify-bin,build-bin}` (see README for arguments). Re-writing an existing manifest requires `--force`.

Tests:

```sh
make test                # Rust + Python + vitest (runs prepare_bin_fixture.py for you)
make test-e2e            # Playwright (once: npx playwright install chromium)
python -m unittest tests.test_bin -v                       # one Python module
python -m unittest tests.test_bin.ClassName.test_name -v   # one test
npx vitest run tests/web/manifest.test.ts                  # one web unit test
npx playwright test -g "pattern"                           # one e2e test
cd rust && cargo test                                      # Rust only — run tests/prepare_bin_fixture.py first
wasm-pack test --headless --chrome rust/xue-wasm        # decoder in a real browser
```

Cross-language golden tests: `tests/prepare_bin_fixture.py` encodes the cropped GRIB fixture with the Python encoder and writes golden planes; Rust tests must decode byte-identically. Playwright global setup builds synthetic tiny bundles (`tests/prepare_web_fixture.py`). Everything generated lands in ignored directories; fixture provenance is in `tests/fixtures/README.md`. There are no linters configured; `npm run build` runs `tsc --noEmit`. CI (`.github/workflows/test.yml`) runs the same suites on push to `main` and on pull requests, split into four jobs — Python (3.12 and 3.14, the latter covering the stdlib zstd path), Rust golden tests, web unit tests plus the typechecked frontend build, and Playwright e2e; prose-only changes (`**.md`, `docs/`, licences) skip the workflow entirely.

Benchmarks: `make bench` (compression modes), `make bench-video` (lossless H.264 path), `make bench-lossy` (lossy tier ladder with scientific-metric acceptance).

## Deploy model

Cloudflare Pages project `project-xue` (custom domain https://xue.ringsaturn.me) serves only the static shell (`dist-deploy/`, built with `vite build --mode deploy` which sets `VITE_DATA_BASE_URL` from `web/.env.deploy`). All forecast data lives on the public R2 bucket (`dataset.ringsaturn.me/xue/`) because bundles exceed the Pages 25 MB file limit.

- R2 is managed over its S3 API with the AWS CLI, entirely from the Makefile — no publishing code of ours. `make upload-r2 MODEL=... RUN=<cycle>` is `aws s3 sync` of the run directory (immutable cache metadata, `?v=<crc32>`-addressed) followed by `aws s3 cp` of the mutable per-model pointer (`latest.json` for GFS, `latest-<model>.json` otherwise), which is the go-live switch. `make live-run MODEL=...` prints the run the pointer names. Endpoint is `https://$(CLOUDFLARE_ACCOUNT_ID).r2.cloudflarestorage.com`; credentials are an R2 API token key pair in `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`. The Makefile exports `AWS_DEFAULT_REGION=auto` and the `when_required` checksum settings that S3-compatible backends need.
- `make prune-r2 MODEL=...` lists the bucket, keeps the newest `KEEP` run directories of that model (default 1 — the live run only; the bucket carries no history) and `aws s3 rm --recursive`s the rest. The run the live pointer names is never deleted, and pruning off a real listing also sweeps leftovers from an interrupted upload. `DRY_RUN=--dryrun` previews.
- Scheduled publishing is one workflow per source — `publish-gfs.yml` (`20 5,11,17,23` UTC), `publish-sflux.yml` (`50 5,11,17,23`, frees the runner's preinstalled toolchains first — native-grid runs are ~4.5× the size), `publish-ecmwf.yml` (`30 8,20`; only ECMWF 00z/12z reach F120 in the open data oper series) — all keeping one run each — each with its own concurrency group and manual dispatch (run, profile, keep, force, dry-run). They all call the reusable `publish.yml` (`workflow_call`), which owns the whole loop: resolve the newest complete cycle → skip if R2 already serves it → build → upload → prune → verify the pointer. Credentials are the `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_ACCOUNT_ID` repository secrets, passed down with `secrets: inherit`.
- The Pages shell only needs deploying when frontend code changes. `deploy-pages.yml` does it automatically on pushes to `main` that touch frontend-relevant paths (also manual dispatch), building the WASM decoder + `make deploy-build` and deploying with `make deploy-pages`; it authenticates with the `CLOUDFLARE_API_TOKEN` repository secret (a Cloudflare API token with Pages Edit — wrangler reads the env var, and the R2 S3 key pair can't deploy Pages). Locally, `make deploy` (= `deploy-build` + `deploy-pages`) still works via the wrangler OAuth login.
- The wrangler OAuth token cannot manage DNS or zone Cache Rules — those are Dashboard-manual (noted in the Makefile).
