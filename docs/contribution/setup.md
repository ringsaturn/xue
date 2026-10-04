# Setup and local builds

## Requirements

- Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/). `uv sync` creates
  `.venv` with NumPy and the `xuepy` wheel. Always run Python as
  `.venv/bin/python`; the Makefile picks it automatically.
- GDAL ≥ 3.8 with the GRIB driver. Needed by `XUE_ENCODER=python`, by the
  satellite fetch stage (`gdalwarp`, `gdal_translate`, `gdalbuildvrt`)
  whichever encoder converts, and by the tests. The native encoder inspects
  inputs through the wheel's own GDAL.
- zstd ≥ 1.5: in the standard library on Python ≥ 3.14, the `zstd` CLI
  below that.
- Node.js ≥ 22.
- A Rust toolchain and `wasm-pack` (the browser decoder, `make wasm`).
- ffmpeg: the H.264 companions (GFS and HRRR).
- eccodes: `grib_set` repacks ECMWF open data (CCSDS-packed) to
  `grid_simple` at fetch time; `bufr_dump` decodes the tropical cyclone and
  sounding BUFR.
- [om2nc](https://github.com/ringsaturn/om2nc), `ifshres` only: reads
  Open-Meteo's `.om` files and writes the NetCDF series the encoders ingest.
  It is GPL-2.0-only and stays a subprocess: nothing in this tree imports,
  links or vendors it, and no om dependency may enter `pyproject.toml`,
  `Cargo.toml` or `web/`. Install a pinned release (the publish workflow
  checks its sha256) or set `XUE_OM2NC` to the command.
- [jma-radar](https://github.com/ringsaturn/jma-radar), `jma` only:
  `uv pip install git+https://github.com/ringsaturn/jma-radar`, or
  `XUE_JMA_RADAR`.
- Optional dependency groups: `uv sync --group zarr` (tests, reading a
  delta store), `--group satellite` (Dust RGB / DEBRA producers and the FCI
  plugin), `--group cma` (the CMA archive reader), `--group aurora`.
- Credentials: `XUE_CMA_ARCHIVE` plus the `R2_*` pair for `cma`;
  `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` for `meteosat`.
- AWS CLI v2 and `jq` for publishing only.

`make check` verifies versions and commands, and reports which encoder a
build would take. The native encoder from source (`make encoder-rust`)
also needs libclang.

`web/src/wasm/` is generated and gitignored: run `make wasm` before the
frontend typechecks or builds.

## Building a run

```sh
make mvp                  # NOAA GFS (default)
make mvp MODEL=ecmwf      # aifs, ifshres, sflux, hrrr, gefsaero, cfs likewise
make mvp MODEL=cfs HOURS=240   # CFSv2's full run is 1092 frames; slow
make serve
```

Step by step (`--model` takes any id in [`docs/sources.md`](../sources.md);
`--hours` defaults to the whole published axis):

```sh
.venv/bin/python -m xuebuild fetch --run latest --hours 240
.venv/bin/python -m xuebuild convert-bin data/raw/gfs.YYYYMMDDHH \
  --output web/public/data/gfs.YYYYMMDDHH \
  --manifest web/public/data/manifest.json
.venv/bin/python -m xuebuild verify-bin web/public/data/gfs.YYYYMMDDHH/tmp2m.xue
.venv/bin/python -m xuebuild build-bin --model ecmwf --run latest --zarr
.venv/bin/python -m xuebuild build-bin --model mrms --run 2026091300 --hours 3            # a past window
.venv/bin/python -m xuebuild build-bin --model mrms --run latest --hours 4 --round now    # one live round
.venv/bin/python -m xuebuild build-bin --model himawari --run latest --hours 6 --round now
.venv/bin/python -m xuebuild export-zarr <bundle.xue> [--delta] [--index-location start|end]
```

- `XUE_ENCODER`: `auto` (default; the wheel when installed), `native`
  (fail without it) or `python` (the reference pipeline). Both produce
  identical bytes.
- `--hours` may be any hour on the model's axis.
- `build-bin` writes per bundle a `.xue`, with `--zarr` (`XUE_ZARR=1`) the
  `<bundle>.zarr/` store derived from it, and with `--no-xue`
  (`XUE_CONTAINER=0`) the store alone; the reduced tiers (`.half`, plus
  `.quarter` / `.eighth` on satellites), a first-frame poster and, where
  the source enables it, H.264 companions. `--skip-variants` /
  `--skip-video` turn those off. The pressure family gets no poster or
  companion: contours need exact codes.
- A vector bundle (`wind10m`, `wind850`, `qflux850`, …) is added only when
  its components were fetched; `--force-download` re-fetches.
- `verify-bin` validates one container and decodes every frame.
- Overwriting an existing manifest needs `--force`
  (`make mvp FORCE=--force`).

Raw inputs live in `data/raw/`, published data in `web/public/data/`, the
static build in `dist/`. All of it is gitignored.

## The frontend locally

`npm run dev` serves the shell with Vite. The dev server has no Range
support, so streaming stores are exercised through `npm run build` and
`make serve`. The basemap key is origin-locked to `localhost` (not
`127.0.0.1`): browse `http://localhost:4173` to see real tiles.
