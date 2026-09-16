# The native encoder

`xuebuild/` is the reference encoder: Python driving GDAL, zstd and ffmpeg as
CLI subprocesses. `rust/xue/src/encode/` is the same `convert-bin` pipeline
written in Rust with those tools linked in process, behind the `xue` crate's
off-by-default `encoder` feature, and published as the `xuepy` wheel.

A build converts through it by default. The Python encoder remains the
reference (a format change lands there first), and the native encoder is
held to it by byte-for-byte identical output.

## Motivation

Profiling the Python pipeline put the remaining wall time in extraction:
`gdal_translate -of ENVI -ot Float64` writes every plane to disk as float64
and the encoder reads it back. A full GFS run is 121 files × 4 variables ×
1.04 M points × 8 bytes ≈ 4 GB written and re-read per build, plus one
subprocess per file. Reading the same bands through GDAL's C API skips all
of it.

## Components

| Stage | Python | Native |
|---|---|---|
| grid + band metadata | `gdalinfo -json` subprocess | `gdal-sys` (georust) in process, exported back to Python as `xue.gdal_info` so `xuebuild` can inspect without a system GDAL |
| plane extraction | `gdal_translate` → ENVI → `np.fromfile` | `GDALRasterIO` into a `Vec<f64>` |
| GRIB2 record index | hand-rolled section walker (`xuebuild/grib2.py`) | [grib-rs](https://github.com/noritada/grib-rs) |
| compression | `compression.zstd` / `zstd` CLI | `zstd` crate (`ZSTD_compress2`) |
| poster deflate | `zlib.compress(level=9)` | `flate2` on libz |
| read-back verify | `xuebuild/binformat.py` reference reader | the `xue` decoder crate, the same code the browser runs |
| H.264 companions | `ffmpeg` subprocess | not built here; `xuebuild/native.py` encodes them afterwards from the bundles this wrote |

`gdal-sys` is used rather than the high-level `gdal` crate because, as of
`gdal` 0.19, the safe wrapper does not compile against GDAL 3.13 (its
`GDALDataType` and `GDALRasterIOExtraArg` changed shape). The surface needed
here is a dozen stable C functions (`src/gdalio.rs`).

## Building

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

The flags mirror `python -m xuebuild convert-bin`. `--skip-video` is
accepted and ignored, since this binary builds no video either way; see *In
the build pipeline* for the path that does.

## In the build pipeline

`xuebuild` depends on the `xuepy` wheel and converts through it by default.
`xuebuild/encoder.py` is the dispatch and `XUE_ENCODER` overrides it:

| `XUE_ENCODER` | |
|---|---|
| `auto` (default) | the native encoder when the wheel imports, the Python pipeline otherwise |
| `native` | require the wheel; fail if it is missing |
| `python` | the reference pipeline, whatever is installed |

`make check` reports which one a build would take. The scheduled `publish-*`
workflows pin `native`: a run that fell back to the Python pipeline would
take hours longer and look identical in the logs. `publish-jma.yml` is the
exception: a wheel ships on the crate's tags, not with every source, so
the wheel on PyPI can predate the model, and a scheduled run cannot pass an
input. Its `Resolve the encoder` step asks `native.knows_source(model)`
(an empty conversion, refused before any input is read) and sets
`XUE_ENCODER=native` when the installed wheel has the source, else
`python` with GDAL installed and a `::warning::` on the run, so the fall
back is explicit rather than silent; a dispatch can still insist on either.

The native encoder does not write two artifacts, and `xuebuild/native.py`
supplies them:

- The H.264 companions. The codes are read back out of the bundles the
  native encoder wrote (with the decoder the same wheel carries) and handed
  to the existing `xuebuild/videoconvert.py`. Nothing is re-extracted and
  nothing is quantized twice; the planes fed to ffmpeg are the container's
  own bytes. As in the reference, a missing ffmpeg drops the artifact and
  leaves the `.xue` as the fallback.
- The live pointer. It carries the manifest's CRC32, and folding the video
  descriptors in changes the manifest, so the native encoder is asked for
  the manifest alone and the pointer is written afterwards, from the
  finished bytes.

`tests/test_native.py` holds the two together: one build through each
encoder into identically shaped directories, then every artifact compared
byte for byte (bundles, half-resolution variants, posters, H.264 streams and
their indexes and playlists, `manifest.json`, and the live pointer).

## Python bindings

`rust/xue-py/` is a thin PyO3 + `rust-numpy` wrapper, published as the
`xuepy` distribution and imported as `xue`. It carries both halves of the
crate.

The decoder is `Bundle`, the same reader the browser runs through wasm.
Planes come back as `uint8` NumPy arrays of quantized codes:

```python
import xue

bundle = xue.Bundle.open("web/public/data/gfs.2026082006/tmp2m.xue")
plane = bundle.decode(variable_id=1, frame_offset=24)   # uint8, width * height
codebook = bundle.metadata["variables"][0]["quantization"]
celsius = codebook["offset"] + plane * codebook["scale"]
```

The encoder is `convert_bin`, which runs the whole native conversion and
returns the same report dictionary the Python pipeline returns, plus
`quantize` / `encode_residual` / `decimate` / `encode_poster`, which take
and return NumPy arrays so individual stages can be compared against
`xuebuild/quantize.py` and `xuebuild/temporal.py` without running a build:

```python
report = xue.convert_bin(["data/raw/gfs.2026082006"], "out/", model="gfs")
```

### Installing

```sh
pip install xuepy
```

The wheel carries its own GDAL, so nothing else has to be installed and no
environment variable has to be set. The wheel is `abi3-py311`: one per
platform, not one per Python minor version. Linux wheels are
`manylinux_2_39`, tagged with the glibc of the ubuntu-24.04 runner that
built them, which means Ubuntu 24.04 and later, Debian 13 and later;
broader coverage would mean building inside a `manylinux_2_28` container.
macOS is arm64 only.

### Building a wheel

```sh
make encoder-wheel          # minimal GDAL, then the wheel, then repair it
```

`scripts/build-gdal-minimal.sh` builds libaec, OpenJPEG, HDF5, netCDF, PROJ
and GDAL from source into `build/gdal-minimal`, and
`scripts/build-encoder-wheel.sh` stages GDAL's and PROJ's data directories
plus every licence text into the package, runs maturin, and lets `delocate`
(macOS) or `auditwheel` (Linux) move the shared libraries in. zlib and
sqlite3 come from the platform and are not bundled.

### Why a bundled GDAL, and why a minimal one

The wheel has to carry GDAL, because the GRIB driver will not read a band
without GDAL's own data directory: with `GDAL_DATA` unset it reports
`Cannot find grib2_center.csv` and then matches zero records, so record
matching fails outright rather than degrading.

A distribution GDAL is not suitable to carry. A package-manager build pulls
a 318 MB closure of 227 shared libraries (Arrow, TileDB, OpenBLAS, libicu,
x265), of which the encoder uses none. It also includes Poppler (GPL-2/3),
x265 (GPL-2), libde265 (LGPL-3), libspatialite and mariadb-connector-c
(LGPL), whose obligations a redistributed binary would have to answer for.

Built here with the GRIB and netCDF drivers, plus the JP2OpenJPEG driver
the GRIB driver decodes JPEG 2000-packed records through (WAVEWATCH III
packs the GFS-Wave files that way, DRS template 5.40), and nothing else,
the whole closure is eight libraries:

| | |
|---:|---|
| 16.4 MB | libgdal |
| 4.2 MB | libproj |
| 3.9 MB | libhdf5 |
| 1.2 MB | libnetcdf |
| 0.4 MB | libopenjp2 |
| | libhdf5_hl, libaec, libsz |

plus 10.2 MB of `proj.db` and 3 MB of GDAL's data tables: a 12 MB wheel.
zlib, sqlite3, libSystem and libc++ come from the platform.

Everything bundled is permissive and allows binary redistribution with
attribution: GDAL and PROJ (MIT), HDF5, libaec and OpenJPEG (BSD), netCDF
(MIT-style), and zlib and sqlite3 from the platform. Several ask explicitly
for their notice to travel with a binary, so the wheel carries them in
`xue/licenses/`.

### Build problems and their fixes

Each is commented where it happens:

- Enumerating the codecs to disable does not work. GDAL probes several
  through pkg-config, which has its own search path; a machine with
  Homebrew ends up linking libjxl and Brotli into a build with no driver
  able to use them, and because those were built for a newer macOS, the
  wheel comes out tagged `macosx_26_0` instead of `macosx_11_0`.
  `GDAL_USE_EXTERNAL_LIBS=OFF` plus the three dependencies by name is the
  reliable form, and for OpenJPEG the library's path as well: GDAL's probe
  goes through pkg-config, CMake caches what it found the first time the
  tree was configured, and a developer machine linked Homebrew's
  `libopenjp2` into a wheel meant to carry its own.
- CMake will compile against one copy of a library and link another. With
  Homebrew's HDF5 2.1 headers ahead of the 1.14 built here, netCDF built
  cleanly and then failed at run time on every netCDF-4 file with
  `H5Pset_libver_bounds(): high bound is not valid`, an enum value that
  only exists in the newer library. `CMAKE_IGNORE_PREFIX_PATH` keeps the
  prefix and the platform SDK the only things in scope.
- `delocate` cannot vendor what it cannot resolve. The extension links
  libgdal by its `@rpath` install name, so the build passes
  `-C link-arg=-Wl,-rpath,<prefix>/lib`; delocate strips the rpath again
  once the libraries are copied in.

## Equivalence

`make encoder-rust-test` runs the crate's unit tests plus a golden test that
encodes `tests/fixtures/gfs.2026081406.f000.crop.grib2` and demands the
exact bytes `tests/prepare_bin_fixture.py` produced with the Python encoder.

Four places needed care to reach that:

- The geotransform is the doubles GDAL holds, on both sides. The Python
  encoder inspects through the wheel's `gdal_info` whenever the wheel is
  installed, which reports the geotransform at full precision; the
  `gdalinfo -json` it falls back to without one prints it at a precision
  that has changed between GDAL releases (16 significant digits in 3.8,
  every digit later). The native encoder used to round to 16 digits to
  imitate that text, which put it an ulp off the reference on any origin
  the rounding touched (a regional crop of the GFS-Wave grid, whose step is
  slightly over 0.25°), so it now reads the doubles as they are;
  `tests/test_native.py` holds the two together on such a crop.
- `flate2` links stock libz, not `zlib-rs`. `zlib-rs` is a port of zlib-ng,
  whose deflate output differs from the zlib CPython uses, which would
  change every poster payload.
- The manifest's embedded metadata strings are `json.dumps` at its
  defaults. A bundle stores its metadata compact and in raw UTF-8; the
  `metadataJson` a manifest carries for a poster or a video is the same
  object written with a space after every separator and every non-ASCII
  character escaped, so the degree sign in the temperature unit reaches the
  manifest as `°`. `to_spaced_json` does both.
- Compression is one-shot on both sides. The Python encoder calls
  `compression.zstd.compress`, which records the pledged source size in the
  frame header. The streaming encoder does not, and picks different window
  parameters (about 5 % larger payloads and different bytes), so
  `zstd_compress` here calls `ZSTD_compress2` one-shot as well.

The last one puts a floor under the comparison: `compression.zstd` is
Python 3.14+, and below it the reference falls back to piping planes
through the zstd CLI, which streams. Those frames decode identically and
are a few per cent larger, so a build on 3.12 is valid but not the same
bytes, and `tests/test_native.py` skips its byte comparisons there. The
published runs are built on 3.14.

## Not covered

Fetching (`xue fetch`), the showcase driver, `build-bin` and `verify-bin`
stay in Python, as does the H.264 encode itself: the native side writes the
bundles those companions are derived from, and `xuebuild/native.py` does
the rest.

## Verified against

Every source, on real runs, with every artifact compared byte for byte:

| Source | What it exercises |
|---|---|
| GFS | the plain path, the two-variable wind bundles, the derived vapour flux and the derived 850 hPa equivalent potential temperature (`exp` / `ln` / `pow` on both sides), the surface diagnostics, and the ocean fields: the wave records' bitmap fill and, on a whole frame, the WAVEWATCH III grid snapped to the exact 0.25° grid |
| ECMWF | de-accumulating `tp`, and the shorter prate axis that follows; the records that arrive under another identity than GFS's (the interval-maximum gust on the 10 m surface and its own analysis-less axis, the cloud cover as a local parameter in a 0–1 fraction, the most-unstable CAPE on surface type 17, the skin temperature as 0/0/17, the ice thickness with a bitmap, the wave period and direction under neighbouring parameter numbers), each matched both by the GRIB2 header index and by GDAL's band metadata (`tests/test_ecmwf.py`, on the Kara Sea fixture) |
| GFS-SFLUX | de-averaging `prate_ave`, the Gaussian grid, the -180 column roll, `dswrf` |
| HRRR | a projected source: the Lambert conformal grid read out of GDAL's WKT, the footprint, the resampling onto the regular 0.03° grid (`sin` / `cos` / `tan` / `pow` per row and column, exact IEEE arithmetic per cell; `tests/test_hrrr.py`), and the `MSLMA` / `REFC` aliases |
| GFS, cropped | `--bbox` with `--bundles`, and `manifest.json` |
| CMA-RADAR | the NetCDF observation path: unscaling, the fill value, a `unitSeconds: 360` axis listing its offsets around archive gaps, `--hours` |
| JMA-HRPNS | a fetched observation that arrives as a NetCDF series (`series_file`), the way the CMA file does, with a cadence: the five-minute times snapped to their slots and the window's first hour taken as the run (`unitSeconds: 300`, `firstFrameOffset: 1`, offsets listed around a gap), a byte-packed rate unscaled through `scale_factor` 0.5 with the 255 fill folded to the codebook bottom, and a run directory holding exactly one series (`tests/test_jma.py`) |
| NOAA-MRMS | a fetched observation: one GRIB per two-minute frame, each its own reference time, re-keyed onto the window's axis (`unitSeconds: 120`, the observation times snapped to the mark, the first hour as the run); the MRMS-local identities (discipline 209) under `cref` and `prate` through the registry's alternates, a rate already in mm/h, the `-999` / `-99` / `-3` sentinels folded to the codebook bottom; a regional grid described on its round step; and the 2 x 2 block-maximum thinning onto 0.02° (`tests/test_mrms.py`) |

One condition the reference never met: GDAL's netCDF driver is not
thread-safe, and reading one file from several threads fails with `netCDF
chunk fetch failed: NetCDF: HDF error`. Every extraction was its own
`gdal_translate` process in Python. `encode::gdalio::netcdf_guard`
serializes those reads; GRIB stays parallel.
