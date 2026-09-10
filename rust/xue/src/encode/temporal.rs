//! Xue temporal grouping, tiling and residual coding — the port of
//! `xuebuild/temporal.py`.
//!
//! Residuals are one-byte modulo-256 wrapping differences. Wrapping
//! subtraction and addition are lossless for every byte pair, so there is no
//! residual range check, no signed interpretation, and no RAW fallback.
//!
//! Grouping is shared by both container versions: a group is a run of
//! consecutive frames formed inside a segment of constant step. What differs
//! is what a group is physically — v1 stores one plane per frame with a
//! middle anchor, v2 packs the group into each tile's chunk with the
//! residuals chained against the previous frame.

use crate::format::{crc32, ChunkEntry, GroupEntry, Predictor, TileGeometry, VariableEntry};

use crate::encode::errors::{EncodeError, Result};

pub const GROUP_LENGTH: usize = 6;

pub fn encode_residual(current: &[u8], base: &[u8]) -> Result<Vec<u8>> {
    if current.len() != base.len() {
        return Err(EncodeError::conversion("plane lengths differ"));
    }
    Ok(current
        .iter()
        .zip(base)
        .map(|(value, anchor)| value.wrapping_sub(*anchor))
        .collect())
}

/// Maximal runs of constant step (docs/format.md "segment"): a boundary falls
/// between two frames exactly where the step changes, so a uniform axis is one
/// segment and the GFS 240-hour axis is two.
pub fn split_segments(offsets: &[i64]) -> Result<Vec<Vec<i64>>> {
    if offsets.windows(2).any(|pair| pair[1] <= pair[0]) {
        return Err(EncodeError::conversion(
            "forecast hours must be unique and ascending",
        ));
    }
    let Some(&first) = offsets.first() else {
        return Err(EncodeError::conversion("time axis is empty"));
    };
    let mut segments = vec![vec![first]];
    let mut step: Option<i64> = None;
    for pair in offsets.windows(2) {
        let (previous, current) = (pair[0], pair[1]);
        if step.is_some_and(|step| current - previous != step) {
            segments.push(vec![current]);
        } else {
            segments.last_mut().expect("non-empty").push(current);
        }
        step = Some(current - previous);
    }
    Ok(segments)
}

/// Temporal groups, formed inside segments of constant step so no group
/// straddles a change of cadence (an ANCHOR residual is then always a
/// difference between frames one step apart).
pub fn group_forecast_hours(offsets: &[i64]) -> Result<Vec<Vec<i64>>> {
    Ok(split_segments(offsets)?
        .into_iter()
        .flat_map(|segment| {
            segment
                .chunks(GROUP_LENGTH)
                .map(<[i64]>::to_vec)
                .collect::<Vec<_>>()
        })
        .collect())
}

/// The anchor is the frame at zero-based index `floor(n / 2)` in its group.
pub fn anchor_hour(group: &[i64]) -> Result<i64> {
    group
        .get(group.len() / 2)
        .copied()
        .ok_or_else(|| EncodeError::conversion("temporal group is empty"))
}


/// One tile's block of a group's planes, frame-major and clipped.
///
/// `planes` are the group's frames in axis order, each a flat plane of the
/// grid `geometry` describes. The block is the tile's clipped rectangle, so
/// the last column and the last row come out narrower or shorter.
pub fn tile_stack(planes: &[&[u8]], geometry: &TileGeometry, tile: u32) -> Result<Vec<u8>> {
    let (row, column) = geometry.origin(tile);
    let (height, width) = geometry.shape(tile);
    let grid_width = geometry.width as usize;
    let mut block = Vec::with_capacity(planes.len() * height as usize * width as usize);
    for plane in planes {
        if plane.len() != grid_width * geometry.height as usize {
            return Err(EncodeError::conversion("plane does not match the grid"));
        }
        for line in 0..height as usize {
            let start = (row as usize + line) * grid_width + column as usize;
            block.extend_from_slice(&plane[start..start + width as usize]);
        }
    }
    Ok(block)
}

/// The stored bytes of one chunk, in place.
///
/// RAW leaves the codes alone — the only encoding precipitation tolerates.
/// PREVIOUS keeps the first frame whole and replaces every later frame with
/// its modulo-256 difference against the frame before it in the same chunk,
/// which is the preceding frame on the axis because a group is contiguous.
pub fn encode_chunk(block: &mut [u8], predictor: Predictor, stride: usize) -> Result<()> {
    match predictor {
        Predictor::Raw => Ok(()),
        Predictor::Previous => {
            // Walk backwards so each frame still sees the untouched codes of
            // the frame before it.
            let frames = block.len() / stride.max(1);
            for frame in (1..frames).rev() {
                let (previous, current) = block.split_at_mut(frame * stride);
                let previous = &previous[(frame - 1) * stride..];
                for (target, base) in current[..stride].iter_mut().zip(previous.iter()) {
                    *target = target.wrapping_sub(*base);
                }
            }
            Ok(())
        }
        other => Err(EncodeError::conversion(format!(
            "predictor {other:?} is not valid in a v2 chunk"
        ))),
    }
}

/// The v2 index tables and uncompressed chunks of one bundle.
///
/// `plane` resolves one (frame offset, variable id) pair to its codes. Every
/// variable shares the file's one axis and therefore its groups, and the
/// chunks come back in the physical order the spec fixes: group, then tile
/// row-major, then variable. The planes of a group are looked up once and
/// then cut tile by tile, so a bundle borrows each plane once rather than
/// once per tile.
pub fn build_chunks<'a>(
    offsets: &[i64],
    variables: &[VariableEntry],
    plane: &dyn Fn(i64, u8) -> Result<&'a [u8]>,
    geometry: &TileGeometry,
) -> Result<(Vec<GroupEntry>, Vec<(ChunkEntry, Vec<u8>)>)> {
    let mut groups = Vec::new();
    let mut chunks = Vec::new();
    let mut first_frame = 0u16;
    for group in group_forecast_hours(offsets)? {
        groups.push(GroupEntry { first_frame, frame_count: group.len() as u8 });
        first_frame += group.len() as u16;
        // One lookup per (frame, variable) for the whole group, reused by
        // every tile.
        let frames: Vec<Vec<&[u8]>> = variables
            .iter()
            .map(|variable| {
                group
                    .iter()
                    .map(|offset| plane(*offset, variable.variable_id))
                    .collect::<Result<Vec<_>>>()
            })
            .collect::<Result<_>>()?;
        for tile in 0..geometry.count() {
            let (height, width) = geometry.shape(tile);
            let stride = height as usize * width as usize;
            for (variable, planes) in variables.iter().zip(&frames) {
                let mut block = tile_stack(planes, geometry, tile)?;
                // The CRC covers the reconstruction, so take it before the
                // residual chain overwrites the codes in place.
                let entry = ChunkEntry { compressed_length: 0, crc32: crc32(&block) };
                encode_chunk(&mut block, variable.predictor, stride)?;
                chunks.push((entry, block));
            }
        }
    }
    Ok((groups, chunks))
}
