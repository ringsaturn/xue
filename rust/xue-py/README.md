# xuepy

Xue (雪) weather bundles for Python: the decoder, and an experimental native
encoder — the [Xue](https://github.com/ringsaturn/xue) Rust crate through PyO3.

A `.xue` file packs one variable of a global forecast run as quantized
single-byte planes with temporal residual prediction and per-plane Zstandard,
so a browser can animate a 240-hour run frame by frame. `docs/format.md` in the
repository is the normative specification.

## Installing

```sh
pip install xuepy
```

Linux wheels are `manylinux_2_39` (Ubuntu 24.04+, Debian 13+) and macOS is
arm64 only. There is no sdist: building from source needs GDAL, libclang and
cmake, and an unsupported platform is better served by a clear "no matching
distribution" than by a confusing build failure.

## Decoding

`Bundle` is the same reader the browser runs through wasm, so a bundle that
decodes here decodes there. A plane comes back as the `uint8` NumPy array of
quantized codes the shader would have uploaded to an R8 texture; the
metadata's `quantization` block turns it back into physical values.

```python
import xue

bundle = xue.Bundle.open("tmp2m.xue")
plane = bundle.decode(variable_id=1, frame_offset=24)   # uint8, width * height

codebook = bundle.metadata["variables"][0]["quantization"]
celsius = codebook["offset"] + plane * codebook["scale"]
```

`bundle.frame_offsets`, `bundle.unit_seconds` and `bundle.metadata` describe
the time axis and the grid. Reconstruction is checked against the CRC-32 the
encoder wrote, so a corrupt file raises rather than returning wrong numbers.

## Encoding

Experimental, and held to the Python reference pipeline by byte-for-byte
identical output on every source it supports — GFS, ECMWF, GFS surface flux,
and the CMA radar mosaic.

```python
report = xue.convert_bin(["data/raw/gfs.2026082006"], "out/", model="gfs")
```

The stages are also exposed on their own — `quantize`, `encode_residual`,
`decimate`, `encode_poster` — taking and returning NumPy arrays.

## What is in the wheel

Its own minimal GDAL: the GRIB and netCDF drivers and nothing else, with
GDAL's and PROJ's data directories beside it, because the GRIB driver cannot
read a band without them. Seven shared libraries rather than the 227 a
distribution GDAL pulls in, all permissively licensed, with their notices in
`xue/licenses/`. Nothing needs to be installed alongside it and no environment
variable needs setting.

See [`docs/encoder.md`](https://github.com/ringsaturn/xue/blob/main/docs/encoder.md)
for how it is built and what it is measured against.
