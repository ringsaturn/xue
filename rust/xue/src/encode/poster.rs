//! First-frame posters: one tiny artifact per variable so a variable switch
//! can paint immediately while the real stream loads.

use std::io::Write;

use flate2::write::ZlibEncoder;
use flate2::Compression;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::grid::GridInfo;

/// Encode one quantized plane as a small first-frame poster.
///
/// The plane is decimated 2x in both axes (row/column 0, 2, 4, … — for the
/// 721-row grid the last row still lands exactly on the south pole), rows are
/// delta-filtered against the previous row (PNG "Up", uint8 wraparound), and
/// the result is zlib-deflated so the browser can inflate it with the native
/// `DecompressionStream("deflate")` — no WASM on the poster path.
pub fn encode_poster(codes: &[u8], grid: &GridInfo) -> Result<(Vec<u8>, GridInfo)> {
    let poster_grid = grid.decimated();
    let mut plane = Vec::with_capacity(poster_grid.width * poster_grid.height);
    for row in (0..grid.height).step_by(2) {
        for column in (0..grid.width).step_by(2) {
            plane.push(codes[row * grid.width + column]);
        }
    }
    let mut filtered = plane.clone();
    for row in (1..poster_grid.height).rev() {
        let (previous, current) = (row - 1, row);
        for column in 0..poster_grid.width {
            filtered[current * poster_grid.width + column] = plane
                [current * poster_grid.width + column]
                .wrapping_sub(plane[previous * poster_grid.width + column]);
        }
    }
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(9));
    encoder
        .write_all(&filtered)
        .and_then(|()| encoder.finish())
        .map(|payload| (payload, poster_grid))
        .map_err(|error| EncodeError::conversion(format!("poster deflate failed: {error}")))
}

/// Reference decoder for [`encode_poster`], used by tests.
pub fn decode_poster(payload: &[u8], width: usize, height: usize) -> Result<Vec<u8>> {
    use flate2::read::ZlibDecoder;
    use std::io::Read;

    let mut plane = Vec::new();
    ZlibDecoder::new(payload)
        .read_to_end(&mut plane)
        .map_err(|error| EncodeError::conversion(format!("poster inflate failed: {error}")))?;
    if plane.len() != width * height {
        return Err(EncodeError::conversion("poster payload has the wrong size"));
    }
    for row in 1..height {
        for column in 0..width {
            plane[row * width + column] =
                plane[row * width + column].wrapping_add(plane[(row - 1) * width + column]);
        }
    }
    Ok(plane)
}
