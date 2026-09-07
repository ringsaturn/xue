# xue-encode — experimental native encoder

The production encoder is Python (`xue/`), driving GDAL, zstd and ffmpeg as
CLI subprocesses. This crate is the same `convert-bin` pipeline written in
Rust with those tools linked in process.

It is an experiment, not a replacement: the Python encoder remains the
reference, and this one is held to it by **byte-for-byte identical output**.

## Why

Profiling the Python pipeline (see the notes in `CLAUDE.md` and the format
spec) put the remaining wall in extraction: `gdal_translate -of ENVI -ot
Float64` writes every plane to disk as float64 and the encoder reads it back.
A full GFS run is 121 files × 4 variables × 1.04 M points × 8 bytes ≈ 4 GB
written and re-read per build, plus one subprocess per file. Reading the same
bands through GDAL's C API skips all of it.

## What it uses

| Stage | Python | Here |
|---|---|---|
| grid + band metadata | `gdalinfo -json` subprocess | `gdal-sys` (georust) in process |
| plane extraction | `gdal_translate` → ENVI → `np.fromfile` | `GDALRasterIO` into a `Vec<f64>` |
| GRIB2 record index | hand-rolled section walker (`xue/grib2.py`) | [grib-rs](https://github.com/noritada/grib-rs) |
| compression | `compression.zstd` / `zstd` CLI | `zstd` crate (`ZSTD_compress2`) |
| poster deflate | `zlib.compress(level=9)` | `flate2` on libz |
| read-back verify | `xue/binformat.py` reference reader | the `xue` decoder crate — the same code the browser runs |
| H.264 companions | `ffmpeg` subprocess | *not built* — scaffolding for comparing compression strategies, not part of what this experiment measures |

`gdal-sys` is used rather than the high-level `gdal` crate because, as of
`gdal` 0.19, the safe wrapper does not compile against GDAL 3.13 — its
`GDALDataType` and `GDALRasterIOExtraArg` changed shape. The surface needed
here is a dozen stable C functions (`src/gdalio.rs`).

## Installing the crate

```toml
[dependencies]
xue-encode = "0.1"
```

Like any GDAL binding it needs a system GDAL at build time, and `gdal-sys`
generates its bindings with `bindgen`, so libclang too. `cargo build` picks
GDAL up through `pkg-config`.

The crate and the decoder crate version independently and release on separate
tags — `encoder-v*` here, `v*` for `xue`.

## Building from this repository

GDAL must be discoverable through `pkg-config`, and `gdal-sys` generates its
bindings with `bindgen`, so libclang must be present too:

```sh
export PKG_CONFIG_PATH="$(gdal-config --prefix)/lib/pkgconfig"
cargo build --release -p xue-encode
```

Or, from the repository root, `make encoder-rust`.

## Using

```sh
xue-encode convert-bin data/raw/gfs.2026082006 --model gfs --output out/
xue-encode convert-bin cases/ --model gfs --output out/ \
    --bbox 105,28,122,42 --bundles prate,wind10m --manifest out/manifest.json
xue-encode convert-bin --help
```

The flags mirror `python -m xue convert-bin`. `--skip-video` is accepted and
ignored, since no video is built either way.

## Python bindings

`python/` is a thin PyO3 + `rust-numpy` wrapper: `convert_bin` runs the whole
native conversion and returns the same report dictionary the Python encoder
returns, and `quantize` / `encode_residual` / `decimate` / `encode_poster`
take and return NumPy arrays so individual stages can be A/B-tested against
`xue/quantize.py` and `xue/temporal.py` without running a whole build.

```python
import xue_encode_py
report = xue_encode_py.convert_bin(["data/raw/gfs.2026082006"], "out/", model="gfs")
```

### Installing

The wheels are **not on PyPI**. Each carries its own GDAL, the encoder is
pinned to the library versions its byte-identity comparison was run against,
and it is a research artifact rather than something to depend on. They ride on
GitHub release assets instead, behind a PEP 503 index:

```sh
pip install xue-encode-py \
  --index-url https://ringsaturn.github.io/xue/simple/ \
  --extra-index-url https://pypi.org/simple
```

```toml
# pyproject.toml, for uv
[[tool.uv.index]]
name = "xue-encoder"
url = "https://ringsaturn.github.io/xue/simple"
explicit = true

[tool.uv.sources]
xue-encode-py = { index = "xue-encoder" }
```

### Building a wheel

```sh
make encoder-wheel          # minimal GDAL, then the wheel, then repair it
```

`scripts/build-gdal-minimal.sh` builds libaec, HDF5, netCDF, PROJ and GDAL from
source into `build/gdal-minimal`, and `scripts/build-wheel.sh` stages GDAL's
and PROJ's data directories plus every licence text into the package, runs
maturin, and lets `delocate` (macOS) or `auditwheel` (Linux) move the shared
libraries in. zlib and sqlite3 come from the platform and are not bundled.

The wheel is `abi3-py311`: one per platform, not one per Python minor version.

The Linux wheel carries the glibc floor of whatever built it — `manylinux_2_39`
from an ubuntu-24.04 runner. That is enough for this project's own CI; a widely
installable wheel would have to be built inside a `manylinux_2_28` container.

### Why a bundled GDAL, and why a minimal one

The wheel has to carry GDAL, because the GRIB driver will not read a band
without GDAL's own data directory — with `GDAL_DATA` unset it reports
`Cannot find grib2_center.csv` and then matches zero records, so record
matching fails outright rather than degrading.

A distribution GDAL is the wrong thing to carry. A package-manager build pulls
a 318 MB closure of 227 shared libraries — Arrow, TileDB, OpenBLAS, libicu,
x265 — of which the encoder uses none. It also drags in Poppler (GPL-2/3),
x265 (GPL-2), libde265 (LGPL-3), libspatialite and mariadb-connector-c
(LGPL), whose obligations a redistributed binary would have to answer for.

Built here with the GRIB and netCDF drivers and nothing else, the whole
closure is seven libraries:

| | |
|---:|---|
| 16.4 MB | libgdal |
| 4.2 MB | libproj |
| 3.9 MB | libhdf5 |
| 1.2 MB | libnetcdf |
| | libhdf5_hl, libaec, libsz |

plus 10.2 MB of `proj.db` and 3 MB of GDAL's data tables — **a 12 MB wheel**.
zlib, sqlite3, libSystem and libc++ come from the platform.

Everything bundled is permissive and allows binary redistribution with
attribution: GDAL and PROJ (MIT), HDF5 and libaec (BSD), netCDF (MIT-style),
and zlib and sqlite3 from the platform. Several ask explicitly for their
notice to travel with a binary, so the wheel carries them in
`xue_encode_py/licenses/`.

### Three things that make the build brittle

Each is commented where it happens, and each was found the hard way:

* **Enumerating the codecs to disable does not work.** GDAL probes several
  through pkg-config, which has its own search path; a machine with Homebrew
  ends up linking libjxl and Brotli into a build with no driver able to use
  them, and — because those were built for a newer macOS — the wheel comes out
  tagged `macosx_26_0` instead of `macosx_11_0`. `GDAL_USE_EXTERNAL_LIBS=OFF`
  plus the two dependencies by name is the reliable form.
* **CMake will compile against one copy of a library and link another.** With
  Homebrew's HDF5 2.1 headers ahead of the 1.14 built here, netCDF built
  cleanly and then failed at run time on every netCDF-4 file with
  `H5Pset_libver_bounds(): high bound is not valid` — an enum value that only
  exists in the newer library. `CMAKE_IGNORE_PREFIX_PATH` keeps the prefix and
  the platform SDK the only things in scope.
* **`delocate` cannot vendor what it cannot resolve.** The extension links
  libgdal by its `@rpath` install name, so the build passes
  `-C link-arg=-Wl,-rpath,<prefix>/lib`; delocate strips the rpath again once
  the libraries are copied in.

## Equivalence

`make encoder-rust-test` runs the crate's unit tests plus a golden test that
encodes `tests/fixtures/gfs.2026081406.f000.crop.grib2` and demands the exact
bytes `tests/prepare_bin_fixture.py` produced with the Python encoder.

Two places needed deliberate bug-compatibility to reach that:

* **The geotransform is rounded to 16 significant digits.** The Python encoder
  reads it out of `gdalinfo -json`, which prints `%.16g`, so its published
  `firstLongitude` / `longitudeStep` are up to an ulp off the doubles GDAL
  holds. Reading them in process is strictly more precise but would publish
  different metadata for the same run, so `as_gdalinfo_json` reproduces the
  rounding.
* **`flate2` links stock libz, not `zlib-rs`.** `zlib-rs` is a port of zlib-ng,
  whose deflate output differs slightly from the zlib CPython uses, which would
  change every poster payload.

Compression is the third place they could drift: the Python encoder calls
`compression.zstd.compress`, a one-shot compress that records the pledged
source size in the frame header. The streaming encoder does not, and picks
different window parameters — about 5 % larger payloads and different bytes —
so `zstd_compress` here calls `ZSTD_compress2` one-shot as well.

## Not covered

Fetching (`xue fetch`), the showcase driver, `build-bin` and `verify-bin` stay
in Python, as do the H.264 companions — those exist to compare compression
strategies against the container, which is not what this experiment measures.

## Verified against

Every source, on real runs, with every artifact compared byte for byte:

| Source | What it exercises |
|---|---|
| GFS | the plain path, plus the two-variable wind bundle |
| ECMWF | de-accumulating `tp`, and the shorter prate axis that follows |
| GFS-SFLUX | de-averaging `prate_ave`, the Gaussian grid, the -180 column roll, `dswrf` |
| GFS, cropped | `--bbox` with `--bundles`, and `manifest.json` |
| CMA-RADAR | the NetCDF observation path: unscaling, the fill value, a `unitSeconds: 360` axis listing its offsets around archive gaps, `--hours` |

One thing the reference never had to handle showed up here: GDAL's netCDF
driver is not thread-safe, and reading one file from several threads fails with
`netCDF chunk fetch failed: NetCDF: HDF error`. Every extraction was its own
`gdal_translate` process in Python, so it never met this. `gdalio::netcdf_guard`
serializes those reads; GRIB stays parallel.
