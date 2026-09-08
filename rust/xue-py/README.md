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

## Custom converters (unreleased)

`write_quantized_bundle` accepts one variable's quantized planes as
`(frame_offset, path)` pairs. Each path contains exactly `grid.width *
grid.height` bytes in the metadata's row order. Ingestion, units and
codebooks belong to the caller; Xue handles temporal prediction, Zstandard
checksums, index entries and atomic publication with memory bounded by the
plane size. Input files are never removed. The metadata JSON is validated
by the decoder and stored verbatim, so existing schema 1 consumers need no
format migration.

```python
import json
import xue

size = xue.write_quantized_bundle(
    "tmp2m.xue", json.dumps(metadata),
    [(hour, f"planes/{hour}.u8") for hour in metadata["time"]["forecastHours"]],
    grouped=True, zstd_level=15,
)
xue.Bundle.open("tmp2m.xue").verify()
```

The frames must cover the metadata axis exactly, with no duplicate offsets.
Set `grouped=False` for independent RAW frames. A conversion error raises
`RuntimeError`, removes the temporary output and preserves an existing
destination. `Bundle.verify()` checks every decoded plane's checksum and
CRC and clears its cache between frames.

## What is in the wheel

Its own minimal GDAL: the GRIB and netCDF drivers and nothing else, with
GDAL's and PROJ's data directories beside it, because the GRIB driver cannot
read a band without them. Seven shared libraries rather than the 227 a
distribution GDAL pulls in, all permissively licensed, with their notices in
`xue/licenses/`. Nothing needs to be installed alongside it and no environment
variable needs setting.

See [`docs/encoder.md`](https://github.com/ringsaturn/xue/blob/main/docs/encoder.md)
for how it is built and what it is measured against.
