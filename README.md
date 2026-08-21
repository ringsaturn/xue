# 雪 / Xue

[![test](https://github.com/ringsaturn/xue/actions/workflows/test.yml/badge.svg)](https://github.com/ringsaturn/xue/actions/workflows/test.yml)

> Xue (雪, pronounced /ɕɥɛ/, roughly "shweh"), Chinese for snow.

Xue packs global weather forecasts — 2 m temperature, precipitation rate,
10 m wind, and solar radiation, out to 120 hours — into a custom
per-variable spatiotemporal binary format (`.xue`), and renders them in the
browser with a static MapLibre page: a Rust WebAssembly worker decodes
frames on demand, a custom WebGL2 layer does inverse Web Mercator
projection and palette lookup entirely on the GPU, and the 10 m wind
renders through a GPU particle layer.

Live demo: <https://xue.ringsaturn.me>. The control in the top-left
corner switches between three sources: NOAA GFS 0.25° (hourly, 121
frames), ECMWF IFS open data 0.25° (3-hourly, 41 frames), and GFS surface
flux on its native ~13 km Gaussian grid (hourly, with a solar-radiation
layer). The three sources share the same format, the same decoder, and the
same rendering pipeline.

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
python -m xue fetch --run latest --hours 120
python -m xue convert-bin data/raw/gfs.YYYYMMDDHH \
  --output web/public/data/gfs.YYYYMMDDHH \
  --manifest web/public/data/manifest.json
python -m xue verify-bin web/public/data/gfs.YYYYMMDDHH/tmp2m.xue
python -m xue build-bin --run latest --hours 120
python -m xue build-bin --model ecmwf --run latest --hours 120
python -m xue build-bin --model sflux --run latest --hours 120
```

Each model publishes as an independent dataset: GFS runs land in
`gfs.<run>/` and go live via `latest.json` at the data root, ECMWF in
`ecmwf.<run>/` via `latest-ecmwf.json`, and GFS surface flux in
`sflux.<run>/` via `latest-sflux.json`. ECMWF open data carries no
precipitation-rate field, so the converter differences the run-total
accumulation `tp` between consecutive frames into an interval-mean rate
(mm/h); the analysis frame has no preceding interval, so the ECMWF
precipitation series starts at F003 (40 frames) while other variables keep
F000 (41 frames); the page's timeline follows the active variable. The
sflux source uses the native ~13 km Gaussian grid (3072 × 1536), derives
precipitation from window-cumulative mean-rate records (also without F000,
120 frames from F001), and additionally publishes the `dswrf` layer
(instantaneous surface downward shortwave radiation, W/m²).

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
aliases like `tmp2m` / `prate` / `wind10m` / `solar`; both are
case-insensitive. The address bar stays in sync when switching, and
unrecognized values fall back to defaults.

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
