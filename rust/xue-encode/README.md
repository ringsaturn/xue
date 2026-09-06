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
| H.264 companions | `ffmpeg` subprocess | *not built* (optional artifacts; the frontend falls back to `.xue`) |

`gdal-sys` is used rather than the high-level `gdal` crate because, as of
`gdal` 0.19, the safe wrapper does not compile against GDAL 3.13 — its
`GDALDataType` and `GDALRasterIOExtraArg` changed shape. The surface needed
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

The flags mirror `python -m xue convert-bin`. `--skip-video` is accepted and
ignored, since no video is built either way.

## Python bindings

`python/` is a thin PyO3 + `rust-numpy` wrapper: `convert_bin` runs the whole
native conversion and returns the same report dictionary the Python encoder
returns, and `quantize` / `encode_residual` / `decimate` / `encode_poster`
take and return NumPy arrays so individual stages can be A/B-tested against
`xue/quantize.py` and `xue/temporal.py` without running a whole build.

```sh
make encoder-rust-wheel     # builds a wheel with maturin (needs uv)
```

maturin is only needed to produce a `.whl`. `cargo build --release -p
xue-encode-py` produces the same module as a `cdylib`; copying
`target/release/libxue_encode_py.dylib` to `xue_encode_py.so` on the Python
path is enough to import it.

```python
import xue_encode_py
report = xue_encode_py.convert_bin(["data/raw/gfs.2026082006"], "out/", model="gfs")
```

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

Fetching (`xue fetch`), the showcase driver, `build-bin`, `verify-bin` and the
H.264 companion artifacts stay in Python. The observation (NetCDF) path is
implemented but has not been diffed against the Python encoder — there is no
radar fixture in the repository.
