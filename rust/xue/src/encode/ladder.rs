//! The resolution ladder: the tier names, grids, tiles and decimated codes
//! of the reduced-resolution rungs a source publishes beside its full tier.

use std::collections::BTreeMap;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::grid::GridInfo;
use crate::encode::registry::{Decimation, Registry};

/// The file suffix and STAC tier name of one rung of the resolution ladder:
/// `half` for factor 2, `quarter` for 4, `eighth` for 8. Any other factor is
/// a registry bug (`SourceSpec::variant_factors`), reported with the factor.
///
/// Mirrors `variant_tier` in `xuebuild/ladder.py`.
fn tier_name(factor: usize) -> Result<&'static str> {
    match factor {
        2 => Ok("half"),
        4 => Ok("quarter"),
        8 => Ok("eighth"),
        _ => Err(EncodeError::conversion(format!(
            "no tier name for a resolution factor of {factor}"
        ))),
    }
}

/// The grid a ladder rung is published on: the full grid decimated
/// log2(factor) times (3000 → 1500 → 750 → 375; 721 rows → 361 → 181 → 91),
/// which is the arithmetic `GridInfo::decimated` repeats rather than one
/// division by the factor, so a rung's origin and step are exactly the
/// posters' and the tier above it. Factor 1 is the full grid.
///
/// Mirrors `variant_grid` in `xuebuild/ladder.py`.
fn variant_grid(grid: &GridInfo, factor: usize) -> GridInfo {
    let mut rung = grid.clone();
    for _ in 0..factor.trailing_zeros() {
        rung = rung.decimated();
    }
    rung
}

/// The tile size one bundle is cut with.
///
/// A reduced variant divides the source tile by its factor, rounding up, so
/// tile number n covers the same ground in every tier and a viewport keeps
/// its tile rectangle across a tier switch (`ceil(ceil(n / 2) / 2) ==
/// ceil(n / 4)`, so the eighth tier's tile is the half tier's halved twice).
/// The size is then clamped to the grid, because the format requires
/// `1 <= tile <= grid` so that a single-tile file states its grid size
/// exactly — and a regional crop is routinely smaller than the source's tile
/// (a six-degree showcase window is 24 x 24 cells against the 0.25-degree
/// grid's 48 x 52 tile). Clamping makes such a file one tile, which is the
/// right answer: there is nothing left to subdivide. Factor 1 is the full
/// tier.
///
/// Mirrors `bundle_tile(tile, grid, factor=…)` in `xuebuild/ladder.py`.
pub(super) fn bundle_tile(tile: (usize, usize), grid: &GridInfo, factor: usize) -> (usize, usize) {
    let tile = (tile.0.div_ceil(factor), tile.1.div_ceil(factor));
    (tile.0.min(grid.width), tile.1.min(grid.height))
}

/// Reduced copy of one quantized plane: every `factor`-th row and column of
/// the full plane from row and column 0 (rows/columns 0, 2, 4, … for the
/// half tier), matching the poster decimation so every tier shares the same
/// sample sites — sampling the full plane by 4 or 8 is the same as halving
/// it twice or three times. `grid` is the full plane's grid.
///
/// Mirrors `decimate_codes(codes, grid, factor)` in `xuebuild/ladder.py`.
pub fn decimate_codes(codes: &[u8], grid: &GridInfo, factor: usize) -> Vec<u8> {
    let mut reduced =
        Vec::with_capacity(grid.width.div_ceil(factor) * grid.height.div_ceil(factor));
    for row in (0..grid.height).step_by(factor) {
        for column in (0..grid.width).step_by(factor) {
            reduced.push(codes[row * grid.width + column]);
        }
    }
    reduced
}

/// Reduced-resolution copy of one quantized plane that keeps peaks: at each
/// site `decimate_codes` keeps, the largest code within `factor / 2` rows
/// and columns either side of it, clipped at the plane's edges, so every
/// cell reaches a site. `nodata_code` counts below every code and is the
/// site's only when its whole window is nodata.
///
/// Mirrors `decimate_peak_codes(codes, grid, factor, nodata_code)` in
/// `xuebuild/ladder.py`.
fn decimate_peak_codes(codes: &[u8], grid: &GridInfo, factor: usize, nodata_code: u8) -> Vec<u8> {
    let half = factor / 2;
    let mut reduced =
        Vec::with_capacity(grid.width.div_ceil(factor) * grid.height.div_ceil(factor));
    for row in (0..grid.height).step_by(factor) {
        let rows = row.saturating_sub(half)..=(row + half).min(grid.height - 1);
        for column in (0..grid.width).step_by(factor) {
            let columns = column.saturating_sub(half)..=(column + half).min(grid.width - 1);
            let mut peak: Option<u8> = None;
            for r in rows.clone() {
                for &code in &codes[r * grid.width + columns.start()..=r * grid.width + columns.end()] {
                    if code != nodata_code {
                        peak = Some(peak.map_or(code, |p| p.max(code)));
                    }
                }
            }
            reduced.push(peak.unwrap_or(nodata_code));
        }
    }
    reduced
}

/// One plane's reduced tier: the peak-keeping decimation for a variable
/// whose registry entry asks for it (the 3.9 µm window, published to show
/// fires, whose one-cell hot spots a plain decimation drops three times in
/// four at the half tier), the site's own code otherwise. Mirrors
/// `decimate_variant_codes` in `xuebuild/ladder.py`.
fn decimate_variant_codes(
    variable_id: &str,
    codes: &[u8],
    grid: &GridInfo,
    factor: usize,
    profile: &str,
    registry: &Registry,
) -> Result<Vec<u8>> {
    if registry.variable(variable_id)?.decimation != Decimation::Peak {
        return Ok(decimate_codes(codes, grid, factor));
    }
    let book = registry.codebook(profile, variable_id)?;
    let Some(linear) = book.as_linear() else {
        return Err(EncodeError::conversion(format!(
            "{variable_id} keeps its peaks only on a linear codebook"
        )));
    };
    Ok(decimate_peak_codes(codes, grid, factor, linear.nodata_code as u8))
}

/// HLS `STREAM-INF` style bandwidth hint: average bits per second needed to
/// keep up with the 12 fps playback rate while downloading the whole tier.
pub(super) fn playback_bandwidth(byte_length: usize, frame_count: usize) -> u64 {
    let fps = 12.0;
    let value = byte_length as f64 * 8.0 * fps / frame_count.max(1) as f64;
    (value.round() as u64).max(1)
}

/// One rung of the ladder: its file suffix, factor, the grid of every family
/// in play decimated to it, and the codes of every plane sampled from the
/// full ones.
pub(super) struct Rung<'a> {
    pub suffix: String,
    pub factor: usize,
    pub grids: Vec<(Option<&'a str>, GridInfo)>,
    pub codes_by_offset: BTreeMap<i64, Vec<(String, Vec<u8>)>>,
}

/// The rungs of `factors`, in the order given. `plane_grid` answers the full
/// grid a plane was quantized on.
pub(super) fn build_ladder<'a, 'g>(
    factors: &[usize],
    grids: &[(Option<&'a str>, GridInfo)],
    codes_by_offset: &BTreeMap<i64, Vec<(String, Vec<u8>)>>,
    plane_grid: impl Fn(&str) -> &'g GridInfo,
    profile: &str,
    registry: &Registry,
) -> Result<Vec<Rung<'a>>> {
    factors
        .iter()
        .map(|&factor| {
            Ok(Rung {
                suffix: format!(".{}", tier_name(factor)?),
                factor,
                grids: grids
                    .iter()
                    .map(|(family, family_grid)| (*family, variant_grid(family_grid, factor)))
                    .collect(),
                codes_by_offset: codes_by_offset
                    .iter()
                    .map(|(offset, planes)| {
                        Ok((
                            *offset,
                            planes
                                .iter()
                                .map(|(name, codes)| {
                                    Ok((
                                        name.clone(),
                                        decimate_variant_codes(
                                            name,
                                            codes,
                                            plane_grid(name),
                                            factor,
                                            profile,
                                            registry,
                                        )?,
                                    ))
                                })
                                .collect::<Result<_>>()?,
                        ))
                    })
                    .collect::<Result<_>>()?,
            })
        })
        .collect::<Result<_>>()
}

/// One tier of one bundle, as a write task: the full tier (factor 1, no
/// suffix) or a rung of the ladder.
pub(super) struct Tier<'a> {
    pub bundle_id: &'a str,
    pub suffix: &'a str,
    pub factor: usize,
    pub grid: &'a GridInfo,
    pub codes: &'a BTreeMap<i64, Vec<(String, Vec<u8>)>>,
}

#[cfg(test)]
mod ladder_tests {
    use super::{bundle_tile, decimate_codes, tier_name, variant_grid};
    use crate::encode::grid::GridInfo;

    #[test]
    fn a_rung_halves_the_tile_once_per_doubling() {
        // The satellite tile against the disk, and the 0.25° tile against
        // the GFS grid: `ceil(n / f)` is `ceil` applied once per halving.
        let disk = GridInfo::new(3000, 3000, 80.7, 60.0, 0.04, -0.04);
        assert_eq!(bundle_tile((64, 64), &disk, 1), (64, 64));
        assert_eq!(bundle_tile((64, 64), &variant_grid(&disk, 2), 2), (32, 32));
        assert_eq!(bundle_tile((64, 64), &variant_grid(&disk, 4), 4), (16, 16));
        assert_eq!(bundle_tile((64, 64), &variant_grid(&disk, 8), 8), (8, 8));
        let world = GridInfo::new(1440, 721, -180.0, 90.0, 0.25, -0.25);
        assert_eq!(bundle_tile((48, 52), &world, 1), (48, 52));
        assert_eq!(bundle_tile((48, 52), &variant_grid(&world, 2), 2), (24, 26));
        assert_eq!(bundle_tile((48, 52), &variant_grid(&world, 4), 4), (12, 13));
        assert_eq!(bundle_tile((48, 52), &variant_grid(&world, 8), 8), (6, 7));
        // A grid smaller than the tile clamps to itself in every tier.
        let window = GridInfo::new(24, 24, 0.0, 6.0, 0.25, -0.25);
        assert_eq!(bundle_tile((48, 52), &window, 1), (24, 24));
        assert_eq!(bundle_tile((48, 52), &variant_grid(&window, 8), 8), (3, 3));
    }

    #[test]
    fn the_rung_grids_are_the_full_grid_decimated_once_per_doubling() {
        let world = GridInfo::new(1440, 721, -180.0, 90.0, 0.25, -0.25);
        for (factor, width, height) in [(1, 1440, 721), (2, 720, 361), (4, 360, 181), (8, 180, 91)] {
            let rung = variant_grid(&world, factor);
            assert_eq!((rung.width, rung.height), (width, height), "factor {factor}");
            assert_eq!(rung.longitude_step, 0.25 * factor as f64, "factor {factor}");
            assert_eq!(rung.first_latitude, 90.0);
        }
        let disk = GridInfo::new(3000, 3000, 80.7, 60.0, 0.04, -0.04);
        assert_eq!(variant_grid(&disk, 8).width, 375);
        assert_eq!(tier_name(2).unwrap(), "half");
        assert_eq!(tier_name(4).unwrap(), "quarter");
        assert_eq!(tier_name(8).unwrap(), "eighth");
        assert!(tier_name(16).unwrap_err().to_string().contains("16"));
        assert!(tier_name(3).is_err());
    }

    #[test]
    fn sampling_by_four_or_eight_is_repeated_halving() {
        // An odd-sized plane, so every rung keeps its ceiling row and column.
        let grid = GridInfo::new(13, 11, 0.0, 10.0, 1.0, -1.0);
        let codes: Vec<u8> = (0..(13 * 11) as u32).map(|index| (index * 7 % 251) as u8).collect();
        let half = decimate_codes(&codes, &grid, 2);
        assert_eq!(half.len(), 7 * 6);
        let half_grid = variant_grid(&grid, 2);
        let quarter = decimate_codes(&half, &half_grid, 2);
        assert_eq!(decimate_codes(&codes, &grid, 4), quarter);
        assert_eq!(quarter.len(), 4 * 3);
        let eighth = decimate_codes(&quarter, &variant_grid(&half_grid, 2), 2);
        assert_eq!(decimate_codes(&codes, &grid, 8), eighth);
        assert_eq!(eighth.len(), 2 * 2);
        // Row and column 0 of the full plane head every rung.
        assert_eq!(eighth[0], codes[0]);
        assert_eq!(eighth[1], codes[8]);
        assert_eq!(eighth[2], codes[8 * 13]);
    }
}
