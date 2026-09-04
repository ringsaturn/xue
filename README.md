# 雪 / Xue

[![test](https://github.com/ringsaturn/xue/actions/workflows/test.yml/badge.svg)](https://github.com/ringsaturn/xue/actions/workflows/test.yml)
[![GFS/0p25 run](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest.json&query=%24.run&label=GFS/0p25&color=0b7cbd&cacheSeconds=600)](https://dataset.ringsaturn.me/xue/latest.json)
[![GFS/SFLUX run](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-sflux.json&query=%24.run&label=GFS/SFLUX&color=2b6cb0&cacheSeconds=600)](https://dataset.ringsaturn.me/xue/latest-sflux.json)
[![ECMWF/IFS 0p25 run](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-ecmwf.json&query=%24.run&label=ECMWF/IFS%200p25&color=1f6f8b&cacheSeconds=600)](https://dataset.ringsaturn.me/xue/latest-ecmwf.json)

> Xue (雪, pronounced /ɕɥɛ/, roughly "shweh"), Chinese for snow.

Xue packs global weather forecasts — 2 m temperature, precipitation rate,
10 m wind, and solar radiation, out to 240 hours — into a custom
per-variable spatiotemporal binary format (`.xue`), and renders them in the
browser with a static MapLibre page: a Rust WebAssembly worker decodes
frames on demand, a custom WebGL2 layer does inverse Web Mercator
projection and palette lookup entirely on the GPU, and the 10 m wind
renders through a GPU particle layer.

Live demo: <https://xue.ringsaturn.me>. The control in the top-left
corner switches between three sources: NOAA GFS 0.25° (hourly to F120,
3-hourly to F240 — 161 frames), GFS surface flux on its native ~13 km
Gaussian grid (same cadence as GFS, with a solar-radiation layer), and
ECMWF IFS open data 0.25° (3-hourly to 144 h, 6-hourly to F240 — 65
frames). No source publishes one cadence all the way out, so these
mixed-step time axes are listed outright in the bundle metadata (schema
version 2 in [`docs/format.md`](docs/format.md)). The three sources share
the same format, the same decoder, and the same rendering pipeline.

## Format rationale

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

A `.xue` file stores each variable as quantized single-byte planes on the
native forecast grid — no reprojection, no tiling, no baked-in colors —
with bounded temporal prediction (six-frame anchor groups for smooth
fields, independent frames for precipitation) and one Zstandard frame per
plane. Every frame is individually addressable, so the page paints a
first-frame poster before the rest of the bundle arrives, then
range-requests only the container header and the temporal groups it
actually needs, prefetching around the playhead into a byte-budgeted
cache. Adjacent frames blend on the GPU during playback.

The format is specified in [`docs/format.md`](docs/format.md). Three
implementations share it:

- **Python encoder** (`xue/`): fetch → convert → quantize → temporal
  residuals → zstd → container. GDAL and ffmpeg are invoked as CLI
  subprocesses — no binary Python dependencies. zstd runs through the
  standard library's `compression.zstd` on Python ≥ 3.14 (per-plane
  subprocess overhead dominated the build otherwise) and falls back to
  the zstd CLI on older interpreters.
- **Rust decoder** (`rust/xue` core crate, `rust/xue-wasm` bindings),
  built into the frontend via `make wasm`.
- **TypeScript frontend** (`web/src/`): manifest resolution with
  resolution-tier selection, a decode worker with windowed prefetch, the
  WebGL2 blend-playback layer, the wind particle layer, and an alternate
  WebCodecs H.264 path where the browser supports it.

Cross-language golden tests keep the Python encoder and Rust decoder
byte-identical.

## Requirements

- Python ≥ 3.12 (NumPy; `uv sync` creates `.venv`)
- GDAL ≥ 3.8 with the GRIB driver
- zstd ≥ 1.5 (bundled with Python ≥ 3.14; the zstd CLI is required only
  on older interpreters)
- Node.js ≥ 22
- Rust toolchain + `wasm-pack` (builds the browser decoder)
- eccodes (`grib_set`, ECMWF source only: open data is CCSDS/AEC-packed
  and is repacked to `grid_simple` at fetch time so any GDAL build can
  read it)
- AWS CLI v2 and `jq` (publishing only: the R2 bucket is managed over its
  S3 API)

`make check` verifies versions and required commands. The NOAA and ECMWF
public S3 buckets need no AWS credentials.

## Usage

Build the latest run end to end and serve the frontend:

```sh
make mvp                  # NOAA GFS (default)
make mvp MODEL=ecmwf      # ECMWF IFS open data
make mvp MODEL=sflux      # GFS surface flux (native ~13 km, adds solar radiation)
make serve
```

Or step by step (`--model gfs|ecmwf|sflux`, default `gfs`):

```sh
python -m xue fetch --run latest --hours 240
python -m xue convert-bin data/raw/gfs.YYYYMMDDHH \
  --output web/public/data/gfs.YYYYMMDDHH \
  --manifest web/public/data/manifest.json
python -m xue verify-bin web/public/data/gfs.YYYYMMDDHH/tmp2m.xue
python -m xue build-bin --run latest --hours 240
python -m xue build-bin --model ecmwf --run latest --hours 240
python -m xue build-bin --model sflux --run latest --hours 240
```

Each model publishes as an independent dataset: GFS runs land in
`gfs.<run>/` and go live via `latest.json` at the data root, ECMWF in
`ecmwf.<run>/` via `latest-ecmwf.json`, and GFS surface flux in
`sflux.<run>/` via `latest-sflux.json`. ECMWF open data carries no
precipitation-rate field, so the converter differences the run-total
accumulation `tp` between consecutive frames into an interval-mean rate
(mm/h); the analysis frame has no preceding interval, so the ECMWF
precipitation series starts at F003 (64 frames) while other variables keep
F000 (65 frames); the page's timeline follows the active variable. The
sflux source uses the native ~13 km Gaussian grid (3072 × 1536), derives
precipitation from window-cumulative mean-rate records (also without F000,
160 frames from F001), and additionally publishes the `dswrf` layer
(instantaneous surface downward shortwave radiation, W/m²). `--hours` may
be any hour on the model's published axis, so shorter uniform builds
(e.g. `--hours 120`) still work and stay metadata schema version 1.

`build-bin` writes one `.xue` per scalar variable (plus a half-resolution
`.half.xue` rendition, a first-frame poster, and a per-variable lossless
H.264 companion; disable with `--skip-variants` / `--skip-video`), adds the
two-variable `wind10m.xue` when the input GRIB carries the 10 m wind
components (older cached GRIBs without wind records are skipped
automatically — re-fetch with `--force-download` to pick wind up), and
generates `manifest.json` (per-bundle path, byte length, CRC-32, and the
`variants` resolution ladder). `verify-bin` fully validates one file's
structure and decodes every frame. Conversion runs one `gdalinfo` and one
multi-band `gdal_translate` per GRIB file, parallelized across files: a
full 121-frame run converts in about 43 seconds.

Raw GRIB fragments live in `data/raw/`, published data in
`web/public/data/`, final static output in `dist/`. Overwriting an
existing manifest requires `--force` (`make mvp FORCE=--force`).

The page supports shareable URLs per model and layer:
`/?model=gfs&type=wind`, `/?model=ecmwf&type=temp`, and so on. `model`
accepts `gfs` / `ecmwf` (alias `ifs`) / `sflux`; `type` also accepts
aliases like `tmp2m` / `prate` / `wind10m` / `solar` / `radar`; both are
case-insensitive. The address bar stays in sync when switching, and
unrecognized values fall back to defaults.

## Historical showcase

Besides the live feed, the site publishes **cases**: one past weather event
each, cropped to the region and hours it is about. They are listed at
`/showcase.html` and played back by the ordinary viewer at `/?case=<id>`,
which pins the case's dataset and run and frames the map on its region.

Most cases are archived forecast runs. A case can also be a series of
observations: the `radar` source is the CMA level-3 radar mosaic (composite
reflectivity, `cref`), which has no live feed and no scheduled job — it
reaches the site only as a case someone builds from a local NetCDF file. Its
frames come every six minutes rather than every hour, which the bundle time
axis carries exactly (`unitSeconds`, see [`docs/format.md`](docs/format.md));
a 212-hour case at that cadence is 2096 frames.

A case is defined by a small checked-in JSON file
(`showcase/cases/<id>.json`) naming the model, the run (or dataset file),
the range, bounding box, and the subset of variables the event is about:

```sh
make showcase-check                      # validate every definition
make showcase CASE=zhengzhou-2021        # fetch, crop, encode, index
make upload-r2-showcase                  # publish cases + the catalog
```

Cropping happens in the encoder (`--bbox` equivalents `crop_grid` /
`convert_bin(bbox=...)`): the window is rounded outward to whole grid cells
and may cross the antimeridian, and the resulting file is an ordinary `.xue`
whose `grid` block names a window instead of the globe. That keeps a case to
a few megabytes, so cases stay published permanently while runs are pruned.

Archive depth limits which forecast events are possible: NOAA GFS and sflux
reach back to about 2021-01, ECMWF open data to about 2024-02. Radar cases
depend on having the decoded file locally
(`XUE_OBSERVATION_ROOT`). See [`showcase/README.md`](showcase/README.md) for
the authoring guide.

## Publishing

The live site is a static shell on Cloudflare Pages; forecast data lives on
a public R2 bucket (`dataset.ringsaturn.me/xue/`), because bundles exceed
the Pages 25 MB per-file limit. The bucket is managed over R2's S3 API with
the AWS CLI:

```sh
make upload-r2 MODEL=gfs RUN=2026081600   # run assets, then the live pointer
make prune-r2  MODEL=gfs                  # delete the runs it superseded
make upload-r2-showcase                   # showcase cases, then the catalog
```

Pruning only ever considers `<model>.<run>/` directories, so showcase cases
are never swept up by it.

Credentials are an R2 API token's key pair in `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, plus `CLOUDFLARE_ACCOUNT_ID` for the endpoint.

GitHub Actions runs the whole loop on a schedule — one workflow per source
([`publish-gfs.yml`](.github/workflows/publish-gfs.yml),
[`publish-sflux.yml`](.github/workflows/publish-sflux.yml),
[`publish-ecmwf.yml`](.github/workflows/publish-ecmwf.yml)), all calling the
reusable [`publish.yml`](.github/workflows/publish.yml) — using the
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_ACCOUNT_ID`
repository secrets. The bucket keeps only the live run per source. Each
workflow also takes a manual dispatch with a dry-run switch.

The Pages shell is deployed separately (`make deploy`) and only needs
redeploying when frontend code changes.

## Testing

```sh
make test        # Rust decoder + Python encoder + frontend unit tests
npx playwright install chromium
make test-e2e    # browser end-to-end tests
```

Python tests cover the quantization codebooks, modulo-256 temporal
residuals, container structure and rejection paths (truncation,
out-of-range offsets, overlaps, gaps, nonzero padding, cyclic
dependencies, checksum failures), and the manifest contract. The Rust side
adds cross-language golden tests decoding byte-identically against the
Python reference, plus mutation fuzzing;
`wasm-pack test --headless --chrome rust/xue-wasm` runs the decoder in a
real browser. Browser tests cover bundle download and verification,
on-demand loading and reuse, playback, scrubbing, and error recovery.
Fixture provenance and regeneration are documented in
`tests/fixtures/README.md`.

## Data and Licensing

Weather data comes from
[NOAA GFS](https://registry.opendata.aws/noaa-gfs-bdp-pds/) (public
domain) and
[ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
(CC BY 4.0, © European Centre for Medium-Range Weather Forecasts; this
project distributes converted derivatives — "Contains modified ECMWF open
data"). The basemap is [Protomaps](https://protomaps.com)-hosted vector
tiles, © [OpenStreetMap](https://www.openstreetmap.org/copyright)
contributors.

The code is dual-licensed under MIT and Apache-2.0
([LICENSE-MIT](LICENSE-MIT) / [LICENSE-APACHE](LICENSE-APACHE)); use
either at your option.

## Acknowledgments

This project is built with [Claude Code](https://claude.com/claude-code),
supported by a Claude Max (20x) subscription provided through Anthropic's
[Claude for OSS](https://claude.com/contact-sales/claude-for-oss)
program. Thank you.
