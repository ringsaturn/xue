# xue

Parser and frame decoder for the Xue (雪) v1 bundle format — a per-variable
spatiotemporal container that packs global weather forecasts (quantized
single-byte planes, temporal residual prediction, zstd) for streaming
playback in the browser.

This crate is the decoding half of the [Xue
project](https://github.com/ringsaturn/xue); the encoder is the Python
pipeline in the same repository, and the normative format specification is
[`docs/format.md`](https://github.com/ringsaturn/xue/blob/main/docs/format.md).
Every integer computation on untrusted input uses checked arithmetic, and no
allocation is sized from a file value before it is validated against the
metadata grid and the file length.

Two readers share the same structural validation and decode logic:

- `Bundle` opens a complete `.xue` file held in memory.
- `StreamingBundle` opens only the structural prefix (header + metadata +
  index) and accepts payload bytes incrementally as HTTP range responses
  arrive, reporting which byte span a requested frame still needs.

## Example

```rust
use xue::{Bundle, FrameRequest};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let bytes = std::fs::read("tmp2m.xue")?;
    let mut bundle = Bundle::open(&bytes)?;

    // Physical-value scale/offset, grid shape, and forecast axis live in
    // the embedded metadata JSON; variable ids are also listed directly.
    let variable_id = bundle.variable_ids()[0];
    println!("{}", bundle.metadata_json());

    // One quantized plane, plane_length() bytes, row-major on the grid.
    let plane = bundle.decode_frame(FrameRequest { variable_id, forecast_hour: 0 })?;
    assert_eq!(plane.len(), bundle.plane_length());
    Ok(())
}
```

The sibling `xue-wasm` crate wraps this decoder in `wasm-bindgen` bindings
for the project's web frontend; it is build-generated output and is not
published to crates.io.

## License

MIT OR Apache-2.0.
