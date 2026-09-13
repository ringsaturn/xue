//! Grid discovery, the -180-first column roll, and regional cropping — the
//! grid half of `xuebuild/binconvert.py`.

use std::path::Path;
use std::sync::Arc;

use serde_json::{json, Map, Value};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::reproject::Resampler;

/// A rectangular window cut out of every extracted plane, indexed in the
/// -180-first layout the column roll produces.
///
/// `column_start` may run past the source's last column: a window crossing the
/// antimeridian continues from column 0, so columns are always taken modulo
/// `source_width`. Rows never wrap — the grid ends at the poles.
#[derive(Debug, Clone, Copy)]
pub struct CropWindow {
    pub source_width: usize,
    pub source_height: usize,
    pub row_start: usize,
    pub column_start: usize,
    pub width: usize,
    pub height: usize,
}

impl CropWindow {
    /// The window of one `(source_height, source_width)` plane.
    pub fn take(&self, plane: &[f64]) -> Vec<f64> {
        let mut cropped = Vec::with_capacity(self.width * self.height);
        for row in self.row_start..self.row_start + self.height {
            let base = row * self.source_width;
            for column in self.column_start..self.column_start + self.width {
                cropped.push(plane[base + column % self.source_width]);
            }
        }
        cropped
    }
}

/// The thinning a source's `Downsample` asks for, fixed to one file's grid:
/// every `factor` x `factor` block of the `(source_height, source_width)`
/// plane becomes its maximum. Carried on the grid the way a `Resampler` is,
/// and like it never serialized — the bundle's grid is the thinned one.
/// Mirrors `BlockReduction` in `xuebuild/binconvert.py`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BlockReduction {
    pub factor: usize,
    pub source_width: usize,
    pub source_height: usize,
}

impl BlockReduction {
    pub fn source_shape(&self) -> (usize, usize) {
        (self.source_height, self.source_width)
    }

    /// The block maximum of one `(source_height, source_width)` plane. A NaN
    /// anywhere in a block is the block's value, as numpy's `max` has it, so
    /// a broken record still fails the completeness check downstream instead
    /// of vanishing into a maximum (`f64::max` would drop it).
    pub fn take(&self, plane: &[f64]) -> Result<Vec<f64>> {
        if plane.len() != self.source_width * self.source_height {
            return Err(EncodeError::conversion(format!(
                "plane has {} cells, expected {}x{}",
                plane.len(),
                self.source_width,
                self.source_height
            )));
        }
        let k = self.factor;
        let (width, height) = (self.source_width / k, self.source_height / k);
        let mut reduced = Vec::with_capacity(width * height);
        for row in 0..height {
            for column in 0..width {
                let mut best = f64::NEG_INFINITY;
                for r in 0..k {
                    let base = (row * k + r) * self.source_width + column * k;
                    for value in &plane[base..base + k] {
                        if value.is_nan() || best.is_nan() {
                            best = f64::NAN;
                        } else if *value > best {
                            best = *value;
                        }
                    }
                }
                reduced.push(best);
            }
        }
        Ok(reduced)
    }
}

#[derive(Debug, Clone)]
pub struct GridInfo {
    pub width: usize,
    pub height: usize,
    pub first_longitude: f64,
    pub first_latitude: f64,
    pub longitude_step: f64,
    pub latitude_step: f64,
    /// Columns every extracted plane is rolled right by, so grids GDAL leaves
    /// starting at Greenwich (the sflux Gaussian grid) come out in the same
    /// -180-first layout as every other source. 0 for grids GDAL already
    /// rotates.
    pub column_roll: usize,
    /// Regional window applied after the roll (showcase cases). When set,
    /// every field above describes the *cropped* planes, and the window
    /// carries the source dimensions the extraction actually reads — or, on
    /// a projected source, the regular grid the resampler produces. Never
    /// serialized: the cropped origin and extent already say where the data is.
    pub crop: Option<CropWindow>,
    /// Set for a source on a map projection (HRRR): the extracted planes are
    /// the projected grid, and every field above describes the regular grid
    /// they are resampled onto (`reproject.rs`), before any crop. Shared by
    /// every extraction worker, hence the `Arc`. Never serialized either.
    pub resample: Option<Arc<Resampler>>,
    /// Set for a source published coarser than it arrives (MRMS): the
    /// extracted planes are the source grid, and every field above describes
    /// the thinned grid their block maxima land on, before any crop. Never
    /// serialized either.
    pub downsample: Option<BlockReduction>,
}

impl GridInfo {
    pub fn new(
        width: usize,
        height: usize,
        first_longitude: f64,
        first_latitude: f64,
        longitude_step: f64,
        latitude_step: f64,
    ) -> Self {
        Self {
            width,
            height,
            first_longitude,
            first_latitude,
            longitude_step,
            latitude_step,
            column_roll: 0,
            crop: None,
            resample: None,
            downsample: None,
        }
    }

    pub fn wraps(&self) -> bool {
        (self.width as f64 * self.longitude_step - 360.0).abs() < 1e-6
    }

    /// `(height, width)` of the plane the extraction reads.
    pub fn source_shape(&self) -> (usize, usize) {
        if let Some(resample) = &self.resample {
            return resample.source_shape();
        }
        if let Some(downsample) = &self.downsample {
            return downsample.source_shape();
        }
        match self.crop {
            None => (self.height, self.width),
            Some(crop) => (crop.source_height, crop.source_width),
        }
    }

    /// The grid produced by keeping every second row and column. For the
    /// 721-row production grid the last kept row still lands exactly on the
    /// south pole, and 720 columns at a doubled step still cover 360 degrees.
    pub fn decimated(&self) -> Self {
        Self {
            width: self.width.div_ceil(2),
            height: self.height.div_ceil(2),
            first_longitude: self.first_longitude,
            first_latitude: self.first_latitude,
            longitude_step: self.longitude_step * 2.0,
            latitude_step: self.latitude_step * 2.0,
            column_roll: 0,
            crop: None,
            resample: None,
            downsample: None,
        }
    }

    /// The grid a source's block reduction publishes — the port of
    /// `_downsample_grid` in `xuebuild/binconvert.py`: one cell per
    /// `factor` x `factor` block, centered on the block (half a source step
    /// in per extra cell, rounded to ten decimals the way a crop's origin
    /// is), at `factor` times the step. The source must divide into whole
    /// blocks, and a global or rolled grid is not thinned.
    pub fn downsampled(&self, factor: usize, path: &Path) -> Result<Self> {
        if factor < 1 {
            return Err(EncodeError::conversion(format!(
                "downsample factor must be positive, not {factor}"
            )));
        }
        if !self.width.is_multiple_of(factor) || !self.height.is_multiple_of(factor) {
            return Err(EncodeError::conversion(format!(
                "{} is {}x{}, which does not divide into {factor}x{factor} blocks",
                path.display(),
                self.width,
                self.height
            )));
        }
        if self.wraps() || self.column_roll != 0 {
            return Err(EncodeError::conversion(format!(
                "{} is a global grid, which this source does not thin",
                path.display()
            )));
        }
        let k = factor as f64;
        Ok(Self {
            width: self.width / factor,
            height: self.height / factor,
            first_longitude: round10(self.first_longitude + self.longitude_step * (k - 1.0) / 2.0),
            first_latitude: round10(self.first_latitude + self.latitude_step * (k - 1.0) / 2.0),
            longitude_step: self.longitude_step * k,
            latitude_step: self.latitude_step * k,
            column_roll: 0,
            crop: None,
            resample: None,
            downsample: Some(BlockReduction {
                factor,
                source_width: self.width,
                source_height: self.height,
            }),
        })
    }

    pub fn metadata(&self) -> Map<String, Value> {
        let mut block = Map::new();
        block.insert("width".into(), json!(self.width));
        block.insert("height".into(), json!(self.height));
        block.insert("layout".into(), json!("row-major"));
        block.insert("rowOrder".into(), json!("north-to-south"));
        block.insert("columnOrder".into(), json!("west-to-east"));
        block.insert("firstLongitude".into(), json!(self.first_longitude));
        block.insert("firstLatitude".into(), json!(self.first_latitude));
        block.insert("longitudeStep".into(), json!(self.longitude_step));
        block.insert("latitudeStep".into(), json!(self.latitude_step));
        block.insert("wrapLongitude".into(), json!(self.wraps()));
        block
    }
}

/// How far short of (or past) a full circle a grid's columns may fall, as a
/// fraction of one cell, and still be the global grid they clearly are.
const GLOBAL_SPAN_TOLERANCE_CELLS: f64 = 1e-3;

/// Give a global grid the exact step and origin its column count implies —
/// the port of `_snap_global_longitudes` in `xuebuild/binconvert.py`, which
/// explains the rule; the two must agree to the bit.
///
/// GDAL derives a GRIB grid's longitude step from the first and last
/// longitudes, and WAVEWATCH III writes the 0.25° grid's last column as
/// 359.750016°, which makes a 0.2500000111° step whose 1440 columns do not
/// close the circle. A grid whose columns span 360° to within a thousandth
/// of a cell is the global grid: its step becomes `360 / width` and its
/// first center is placed on that step's grid (rounding half up) when it
/// lies within the same tolerance of it. Exact and regional grids pass
/// through unchanged.
pub fn snap_global_longitudes(grid: GridInfo) -> GridInfo {
    let span = grid.width as f64 * grid.longitude_step;
    if (span - 360.0).abs() > grid.longitude_step * GLOBAL_SPAN_TOLERANCE_CELLS {
        return grid;
    }
    let step = 360.0 / grid.width as f64;
    let mut first = (grid.first_longitude / step + 0.5).floor() * step;
    if (first - grid.first_longitude).abs() > step * GLOBAL_SPAN_TOLERANCE_CELLS {
        first = grid.first_longitude;
    }
    GridInfo {
        longitude_step: step,
        first_longitude: first,
        ..grid
    }
}

/// The steps a regional grid is snapped to: whole thousandths of a degree.
const REGIONAL_STEP_UNIT: f64 = 1e-3;

/// Give a regional grid the round step it was clearly published on — the
/// port of `_snap_regional_steps` in `xuebuild/binconvert.py`, which
/// explains the rule; the two must agree to the bit.
///
/// The same GDAL habit that puts a global grid a hair off the globe puts a
/// regional one a hair off its step: MRMS writes the last longitude of its
/// 0.01° grid two millionths short, which GDAL turns into a 0.0099999997°
/// step. A grid whose cells, at the nearest whole thousandth of a degree,
/// span its extent to within a thousandth of a cell is on that step, on
/// both axes independently, and its first *center* is placed on the
/// half-step grid (a grid whose edges are on whole steps has its centers
/// half a step in) when it lies within the same tolerance. A step that is
/// not near a thousandth passes through unchanged, as does a global grid.
pub fn snap_regional_steps(grid: GridInfo) -> GridInfo {
    if grid.wraps() {
        return grid;
    }
    let (longitude_step, first_longitude) =
        snap_axis(grid.longitude_step, grid.first_longitude, grid.width);
    let (latitude_step, first_latitude) =
        snap_axis(grid.latitude_step, grid.first_latitude, grid.height);
    GridInfo {
        longitude_step,
        first_longitude,
        latitude_step,
        first_latitude,
        ..grid
    }
}

/// One axis of [`snap_regional_steps`]: `(step, first)` snapped, or as
/// given. The snapped values are whole thousandths divided out at the end —
/// `30 / 1000` is the double nearest 0.03 where `30 * 0.001` is not.
fn snap_axis(step: f64, first: f64, count: usize) -> (f64, f64) {
    let thousandths = (step / REGIONAL_STEP_UNIT + 0.5).floor();
    let rounded = thousandths / 1000.0;
    let count = count as f64;
    if thousandths == 0.0 || (count * step - count * rounded).abs() > step.abs() * GLOBAL_SPAN_TOLERANCE_CELLS {
        return (step, first);
    }
    let half_steps = (first / (rounded / 2.0) + 0.5).floor();
    let mut snapped_first = (half_steps * thousandths) / 2000.0;
    if (snapped_first - first).abs() > rounded.abs() * GLOBAL_SPAN_TOLERANCE_CELLS {
        snapped_first = first;
    }
    (rounded, snapped_first)
}

/// Roll grids that start at Greenwich to the -180-first layout.
///
/// GDAL's GRIB driver rotates global regular lat/lon grids to start at -180
/// but leaves the sflux Gaussian grid starting at longitude 0; downstream
/// (shaders, posters, particles) assumes one layout, so the columns whose
/// centers lie at or past 180 degrees move to the front of every extracted
/// plane.
pub fn normalize_longitudes(grid: GridInfo) -> GridInfo {
    // A wrapping grid GDAL already rotated starts within half a wrap of -180;
    // the sflux grid's first cell center computes to exactly 0.0, so the test
    // must be "starts near -180", not "non-positive".
    if !grid.wraps() || grid.first_longitude < -90.0 {
        return grid;
    }
    let pivot = ((180.0 - grid.first_longitude) / grid.longitude_step - 1e-9).ceil();
    if pivot < 0.0 || pivot > grid.width as f64 {
        return grid;
    }
    let pivot = pivot as usize;
    let roll = grid.width.saturating_sub(pivot);
    if roll == 0 || roll >= grid.width {
        return grid;
    }
    GridInfo {
        first_longitude: grid.first_longitude + pivot as f64 * grid.longitude_step - 360.0,
        column_roll: roll,
        ..grid
    }
}

/// Round to ten decimal places the way Python's `round(value, 10)` does:
/// correct decimal rounding, ties to even.
fn round10(value: f64) -> f64 {
    format!("{value:.10}").parse().unwrap_or(value)
}

/// Restrict a grid to the smallest whole-cell window covering `bbox`
/// (`west, south, east, north`, degrees).
///
/// The window keeps the cell whose center sits at or before each lower edge
/// and the one at or after each upper edge, so the cropped planes always cover
/// the requested box outright. On a wrapping source grid `west > east` crosses
/// the antimeridian; the returned origin is renormalized into [-180, 180) and
/// the window, not the metadata, carries the wrap.
pub fn crop_grid(grid: GridInfo, bbox: (f64, f64, f64, f64)) -> Result<GridInfo> {
    if grid.crop.is_some() {
        return Err(EncodeError::conversion("a grid can only be cropped once"));
    }
    let (west, south, east, north) = bbox;
    let printed = format!("({west}, {south}, {east}, {north})");
    if !(-90.0..90.0).contains(&south) || south >= north || north > 90.0 {
        return Err(EncodeError::conversion(format!(
            "bbox latitudes must satisfy -90 <= south < north <= 90: {printed}"
        )));
    }
    if !(-360.0..=360.0).contains(&west) || !(-360.0..=360.0).contains(&east) {
        return Err(EncodeError::conversion(format!(
            "bbox longitudes must be within [-360, 360]: {printed}"
        )));
    }
    if east < west && !grid.wraps() {
        return Err(EncodeError::conversion(
            "only a wrapping grid can be cropped across the antimeridian",
        ));
    }
    let mut span = (east - west).rem_euclid(360.0);
    if span == 0.0 {
        if east < west || !grid.wraps() {
            return Err(EncodeError::conversion(format!(
                "bbox must have a positive longitude span: {printed}"
            )));
        }
        span = 360.0;
    }

    let offset = if grid.wraps() {
        (west - grid.first_longitude).rem_euclid(360.0)
    } else {
        west - grid.first_longitude
    };
    let mut column_start = (offset / grid.longitude_step + 1e-9).floor() as i64;
    let mut column_end = ((offset + span) / grid.longitude_step - 1e-9).ceil() as i64;
    let width: i64;
    if grid.wraps() {
        width = (column_end - column_start + 1).min(grid.width as i64);
        if width >= grid.width as i64 {
            column_start = 0;
        }
        column_start = column_start.rem_euclid(grid.width as i64);
    } else {
        column_start = column_start.max(0);
        column_end = column_end.min(grid.width as i64 - 1);
        width = column_end - column_start + 1;
    }
    let width = if grid.wraps() && width >= grid.width as i64 {
        grid.width as i64
    } else {
        width
    };
    if width <= 0 {
        return Err(EncodeError::conversion(format!(
            "bbox does not overlap the source grid: {printed}"
        )));
    }

    let row_start = (((north - grid.first_latitude) / grid.latitude_step + 1e-9).floor() as i64).max(0);
    let row_end = (((south - grid.first_latitude) / grid.latitude_step - 1e-9).ceil() as i64)
        .min(grid.height as i64 - 1);
    let height = row_end - row_start + 1;
    if height <= 0 {
        return Err(EncodeError::conversion(format!(
            "bbox does not overlap the source grid: {printed}"
        )));
    }

    let mut first_longitude = grid.first_longitude + column_start as f64 * grid.longitude_step;
    if width < grid.width as i64 {
        first_longitude = (first_longitude + 180.0).rem_euclid(360.0) - 180.0;
    }
    let (width, height) = (width as usize, height as usize);
    let (row_start, column_start) = (row_start as usize, column_start as usize);
    Ok(GridInfo {
        width,
        height,
        first_longitude: round10(first_longitude),
        first_latitude: round10(grid.first_latitude + row_start as f64 * grid.latitude_step),
        longitude_step: grid.longitude_step,
        latitude_step: grid.latitude_step,
        column_roll: grid.column_roll,
        crop: Some(CropWindow {
            source_width: grid.width,
            source_height: grid.height,
            row_start,
            column_start,
            width,
            height,
        }),
        resample: grid.resample.clone(),
        downsample: grid.downsample,
    })
}

#[cfg(test)]
mod tests {
    use super::{snap_global_longitudes, snap_regional_steps, BlockReduction, GridInfo};
    use std::path::Path;

    const WAVE_STEP: f64 = 0.2500000111188325;
    const WAVE_ORIGIN: f64 = -180.12500000555943;

    #[test]
    fn the_wave_grid_snaps_to_the_pgrb2_grid() {
        let raw = GridInfo::new(1440, 721, WAVE_ORIGIN + WAVE_STEP / 2.0, 90.0, WAVE_STEP, -0.25);
        assert!(!raw.wraps(), "as GDAL reports it, the grid does not close");
        let snapped = snap_global_longitudes(raw);
        assert_eq!((snapped.longitude_step, snapped.first_longitude), (0.25, -180.0));
        assert!(snapped.wraps());
    }

    #[test]
    fn exact_and_regional_grids_pass_through_unchanged() {
        for grid in [
            GridInfo::new(1440, 721, -180.0, 90.0, 0.25, -0.25),
            // The sflux Gaussian grid before its roll: first center at 0.
            GridInfo::new(3072, 1536, 0.0, 89.91, 0.1171875, -0.117),
            GridInfo::new(80, 80, 118.0, 38.0, WAVE_STEP, -0.25),
            GridInfo::new(80, 80, 118.0, 38.0, 0.25, -0.25),
        ] {
            let snapped = snap_global_longitudes(grid.clone());
            assert_eq!(snapped.longitude_step, grid.longitude_step);
            assert_eq!(snapped.first_longitude, grid.first_longitude);
        }
    }

    #[test]
    fn an_origin_off_the_step_grid_keeps_its_step_only() {
        let grid = GridInfo::new(1440, 721, -179.9, 90.0, WAVE_STEP, -0.25);
        let snapped = snap_global_longitudes(grid);
        assert_eq!(snapped.longitude_step, 0.25);
        assert_eq!(snapped.first_longitude, -179.9);
    }

    #[test]
    fn a_grid_more_than_a_thousandth_of_a_cell_short_is_not_global() {
        let grid = GridInfo::new(1440, 721, -180.0, 90.0, 0.25 * (1.0 - 2e-3), -0.25);
        let snapped = snap_global_longitudes(grid.clone());
        assert_eq!(snapped.longitude_step, grid.longitude_step);
    }

    /// The MRMS grid as GDAL reports it, and a crop of it as the GRIB writer
    /// re-rounds it, both land on 0.01° with their centers half a step in.
    #[test]
    fn the_mrms_grid_snaps_to_its_hundredth() {
        let step = 0.0099999997142449;
        let full = snap_regional_steps(GridInfo::new(
            7000, 3500, -129.99999999985712 + step / 2.0, 54.9999999998571 - 0.0099999997142041 / 2.0, step, -0.0099999997142041,
        ));
        assert_eq!((full.longitude_step, full.latitude_step), (0.01, -0.01));
        assert_eq!((full.first_longitude, full.first_latitude), (-129.995, 54.995));
        let crop_step = 0.009999993710692;
        let crop = snap_regional_steps(GridInfo::new(160, 160, -77.60000099685537 + crop_step / 2.0, 51.795, crop_step, -0.01));
        assert_eq!((crop.longitude_step, crop.first_longitude), (0.01, -77.595));
        // A step that is not near a thousandth — the radar mosaic's
        // power-of-two tiles, the Gaussian grid — passes through.
        for grid in [
            GridInfo::new(512, 512, 100.0, 40.0, 360.0 / 65536.0, -360.0 / 65536.0),
            GridInfo::new(3072, 1536, -180.0, 89.91, 0.1171875, -0.117),
        ] {
            let same = snap_regional_steps(grid.clone());
            assert_eq!(same.longitude_step, grid.longitude_step);
            assert_eq!(same.first_longitude, grid.first_longitude);
        }
        // 0.03 comes out as the double nearest 0.03, not 30 * 0.001.
        let thirty = snap_regional_steps(GridInfo::new(100, 100, -100.0, 40.0, 0.03 + 1e-10, -0.03));
        assert_eq!(thirty.longitude_step, 0.03);
    }

    #[test]
    fn the_block_maximum_keeps_the_strongest_cell_and_propagates_nan() {
        let grid = snap_regional_steps(GridInfo::new(4, 2, -129.995, 54.995, 0.01, -0.01))
            .downsampled(2, Path::new("x"))
            .expect("divides");
        assert_eq!((grid.width, grid.height), (2, 1));
        assert_eq!((grid.first_longitude, grid.first_latitude), (-129.99, 54.99));
        assert_eq!((grid.longitude_step, grid.latitude_step), (0.02, -0.02));
        assert_eq!(grid.source_shape(), (2, 4));
        let reduction = grid.downsample.expect("reduction");
        assert_eq!(reduction, BlockReduction { factor: 2, source_width: 4, source_height: 2 });
        let plane = [0.0, 5.5, -1.0, f64::NAN, 3.0, 1.0, 2.0, 0.0];
        let reduced = reduction.take(&plane).expect("reduces");
        assert_eq!(reduced[0], 5.5);
        assert!(reduced[1].is_nan());
        assert!(GridInfo::new(5, 2, -129.995, 54.995, 0.01, -0.01).downsampled(2, Path::new("x")).is_err());
    }
}
