//! Xue v1 temporal grouping and residual coding — the port of
//! `xue/temporal.py`.
//!
//! Residuals are one-byte modulo-256 wrapping differences. Wrapping
//! subtraction and addition are lossless for every byte pair, so there is no
//! residual range check, no signed interpretation, and no RAW fallback.

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
