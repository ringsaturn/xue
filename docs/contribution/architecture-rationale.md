# Why the format looks like this

The access pattern is continuous playback of a complete global forecast
with free timeline scrubbing. A map tile pyramid stores every frame as its
own set of precoloured images. For one GFS run, the same two variables:

| Delivery | Size |
|---|---:|
| Per-frame raster tiles (PMTiles, zoom 0–4, precoloured) | 3,205.87 MB |
| Source GRIB2 | 137.73 MB |
| Xue (`tmp2m.xue` ≈ 29 MB + `prate.xue` ≈ 36 MB) | ≈ 65 MB |

[`scripts/pmtiles_size_assessment/`](../../scripts/pmtiles_size_assessment/)
rebuilds the tile baseline frame by frame and reports the totals.

A bundle stores each variable as quantized single-byte planes on the
native forecast grid (no reprojection, no baked-in colours), with bounded
temporal prediction (six-frame groups for smooth fields, independent
frames for precipitation) and one Zstandard frame per chunk. Every frame
is individually addressable, so the page paints a first-frame poster, then
range-requests only the index and the chunks its viewport and playhead
need, prefetching into a byte-budgeted cache. Adjacent frames blend on the
GPU.

## Container and store

The Zarr store ([`docs/zarr-profile.md`](../zarr-profile.md)) carries the
same codes in a layout any Zarr client reads: a container v2 bundle is, to
within its index format, a sharded Zarr `uint8` array. Since 2026-09-15
every live run, round and case is published as stores alone
(`build-bin --zarr --no-xue`). The `.xue` container
([`docs/format.md`](../format.md)) remains:

- the intermediate both encoders write and derive the store from, so the
  store's codes are the container's by construction and the two encoders
  stay byte-identical through it;
- the format every decoder keeps reading, for runs and cases published
  before the switch and for local builds without `--no-xue`;
- the format the golden fixtures and the size comparison above are stated
  in.

`xue export-zarr --delta` swaps in the `xue.delta` codec (the container's
temporal residual as a codec, `xuebuild/zarrcodec.py`), under which a
chunk's compressed bytes equal the bundle's wherever the two chunkings
coincide.

## Four implementations

- Python encoder (`xuebuild/`), the reference: a format change lands here
  first. GDAL, ffmpeg and eccodes are CLI subprocesses.
- Native encoder (`rust/xue/src/encode/`, shipped in the `xuepy` wheel):
  the same conversion with GDAL, grib-rs and zstd linked in, held to the
  reference by byte-identical output ([`docs/encoder.md`](../encoder.md)).
- Rust decoder (`rust/xue`, `rust/xue-wasm`), built into the frontend.
- TypeScript frontend (`web/src/`): manifest resolution and tier choice, a
  decode worker with windowed prefetch, WebGL2 blend playback, wind
  particles, and an opt-in WebCodecs H.264 path.
