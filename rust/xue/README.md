# xue

Decoder for Xue (雪) weather bundles: global forecasts and observation
windows as quantized single-byte planes with temporal residual prediction
and zstd, streamed by HTTP range reads for playback in the browser.

The Xue service publishes its runs as Zarr v3 stores
([`docs/zarr-profile.md`](https://github.com/ringsaturn/xue/blob/main/docs/zarr-profile.md));
what a bundle carries, its metadata and codebooks, is
[`docs/bundle.md`](https://github.com/ringsaturn/xue/blob/main/docs/bundle.md).
The `zarr` module parses a store's group and array documents and its shard
index, and `decode_chunk` decodes one inner chunk. The crate also reads the
single-file `.xue` container
([`docs/format.md`](https://github.com/ringsaturn/xue/blob/main/docs/format.md)),
both versions, forever: runs and cases published before the stores carry
those bytes and are never rebuilt. In v1 a payload is one whole plane of one
frame; in v2 it is a chunk — one spatial tile of one temporal group for one
variable — which is what makes a partial read cheap.

This crate is the decoding half of the [Xue
project](https://github.com/ringsaturn/xue); the production encoder is the
Python pipeline in the same repository. Every integer computation on
untrusted input uses checked arithmetic, and no allocation is sized from a
file value before it is validated against the metadata grid and the file
length.

For the container, two readers share the same structural validation and
decode logic:

- `Bundle` opens a complete `.xue` file held in memory.
- `StreamingBundle` opens only the structural prefix (header + metadata +
  index) and accepts payload bytes incrementally as HTTP range responses
  arrive, reporting which byte span a requested frame still needs.

Both answer the whole-plane call `decode_frame`, and, on a tiled file, two
calls that read a fraction of it: `decode_frame_tiles` fills only the tiles a
viewport covers, and `decode_series` reads one grid cell across the whole
time axis at one chunk per temporal group, independent of how many frames
that axis has. `StreamingBundle` reports the byte spans either still needs
(`missing_spans`, `missing_series_spans`), coalesced so a row of neighbouring
tiles costs one range request rather than one per tile.

## Example

```rust
use xue::{Bundle, FrameRequest};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let bytes = std::fs::read("tmp2m.xue")?;
    let mut bundle = Bundle::open(&bytes)?;

    // Physical-value scale/offset, grid shape, and time axis live in the
    // embedded metadata JSON; variable ids and the frame-offset axis are
    // also listed directly.
    let variable_id = bundle.variable_ids()[0];
    println!("{}", bundle.metadata_json());

    // One quantized plane, plane_length() bytes, row-major on the grid.
    let plane = bundle.decode_frame(FrameRequest { variable_id, frame_offset: 0 })?;
    assert_eq!(plane.len(), bundle.plane_length());
    Ok(())
}
```

The sibling `xue-wasm` crate wraps this decoder in `wasm-bindgen` bindings
for the project's web frontend; it is build-generated output and is not
published to crates.io.

## License

MIT OR Apache-2.0.

## The `encoder` feature

Off by default, and not part of what this crate is for. It enables an
experimental native encoder that links GDAL and writes the stores (and,
when asked, `.xue` containers) byte-identically to the Python reference pipeline — see
[`docs/encoder.md`](https://github.com/ringsaturn/xue/blob/main/docs/encoder.md).
Enabling it requires a system GDAL and libclang at build time. A decode-only
build resolves none of its dependencies and needs neither: `cargo add xue`
pulls in `crc32fast`, `ruzstd`, `serde_json` and `bzip2` (its pure-Rust
backend, for the NEXRAD Level 3 reader) and nothing else, with no C
toolchain involved. That is also why the crate builds for
`wasm32-unknown-unknown`.

The rendered docs cover it: `[package.metadata.docs.rs]` asks docs.rs for
`--all-features`, and gdal-sys' `DOCS_RS` branch supplies prebuilt bindings
so the feature documents without a system GDAL. <https://ringsaturn.github.io/xue/>
is the same thing built from `main`, ahead of the next release.
