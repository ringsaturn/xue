//! Grid discovery, the -180-first column roll, and regional cropping — the
//! port of `xuebuild/grid.py`.

use std::path::Path;

use serde_json::{json, Map, Value};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::reproject::ProjectedGrid;
use crate::projection::{gaussian_latitude, GaussianRows};

/// A rectangular window cut out of every extracted plane, indexed in the
/// -180-first layout the column roll produces.
///
/// `column_start` may run past the source's last column: a window crossing the
/// antimeridian continues from column 0, so columns are always taken modulo
/// `source_width`. Rows never wrap — the grid ends at the poles.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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
/// plane becomes its maximum. Never serialized: the bundle's grid is the
/// thinned one.
/// Mirrors `BlockReduction` in `xuebuild/grid.py`.
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
    /// carries the source dimensions the extraction actually reads. Never
    /// serialized: the cropped origin and extent already say where the data is.
    pub crop: Option<CropWindow>,
    /// Set for a source published coarser than it arrives (MRMS): the
    /// extracted planes are the source grid, and every field above describes
    /// the thinned grid their block maxima land on, before any crop. Never
    /// serialized either.
    pub downsample: Option<BlockReduction>,
    /// Set on a grid on a map projection ([`GridInfo::projected`]), which has
    /// none of the regular fields: this says where its cells are, and a window's
    /// or a tier's is that window's or tier's own.
    pub projection: Option<ProjectedGrid>,
    /// Set on a regular Gaussian grid, beside the regular fields every other
    /// number here still comes from.
    pub gaussian: Option<GaussianRows>,
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
            downsample: None,
            projection: None,
            gaussian: None,
        }
    }

    /// The grid a projected file's planes are published on. Its regular fields
    /// are NaN, so arithmetic on them cannot pass for a place.
    pub fn projected(projection: ProjectedGrid, crop: Option<CropWindow>) -> Self {
        Self {
            crop,
            projection: Some(projection),
            ..Self::new(projection.width, projection.height, f64::NAN, f64::NAN, f64::NAN, f64::NAN)
        }
    }

    pub fn wraps(&self) -> bool {
        (self.width as f64 * self.longitude_step - 360.0).abs() < 1e-6
    }

    /// `(height, width)` of the plane the extraction reads.
    pub fn source_shape(&self) -> (usize, usize) {
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
    /// south pole, and 720 columns at a doubled step still cover 360 degrees. A
    /// projected grid's first cell stays where it is at twice the spacing, and a
    /// Gaussian grid's rows are every second one of those it had.
    pub fn decimated(&self) -> Self {
        if let Some(projection) = &self.projection {
            return Self::projected(projection.decimated(), None);
        }
        Self {
            width: self.width.div_ceil(2),
            height: self.height.div_ceil(2),
            first_longitude: self.first_longitude,
            first_latitude: self.first_latitude,
            longitude_step: self.longitude_step * 2.0,
            latitude_step: self.latitude_step * 2.0,
            column_roll: 0,
            crop: None,
            downsample: None,
            projection: None,
            gaussian: self.gaussian.map(|rows| GaussianRows { row_stride: rows.row_stride * 2, ..rows }),
        }
    }

    /// The grid a source's block reduction publishes — the port of
    /// `downsample_grid` in `xuebuild/grid.py`: one cell per
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
            downsample: Some(BlockReduction {
                factor,
                source_width: self.width,
                source_height: self.height,
            }),
            projection: None,
            gaussian: None,
        })
    }

    pub fn metadata(&self) -> Map<String, Value> {
        let mut block = Map::new();
        block.insert("width".into(), json!(self.width));
        block.insert("height".into(), json!(self.height));
        block.insert("layout".into(), json!("row-major"));
        block.insert("rowOrder".into(), json!("north-to-south"));
        block.insert("columnOrder".into(), json!("west-to-east"));
        if let Some(projection) = &self.projection {
            block.insert("wrapLongitude".into(), json!(false));
            block.insert("projection".into(), Value::Object(projection.metadata()));
            return block;
        }
        block.insert("firstLongitude".into(), json!(self.first_longitude));
        block.insert("firstLatitude".into(), json!(self.first_latitude));
        block.insert("longitudeStep".into(), json!(self.longitude_step));
        block.insert("latitudeStep".into(), json!(self.latitude_step));
        block.insert("wrapLongitude".into(), json!(self.wraps()));
        if let Some(rows) = &self.gaussian {
            block.insert(
                "gaussian".into(),
                json!({"n": rows.n, "firstRow": rows.first_row, "rowStride": rows.row_stride}),
            );
        }
        block
    }
}

/// How far a file's first latitude may lie from the Gaussian row it is taken
/// for, as a fraction of a row. GDAL's even spacing is within 0.014 of one of the
/// true rows on sflux; on CFSv2, whose step is 180°/190, it starts true and a
/// window cut from the northern hemisphere is at most this far off.
const GAUSSIAN_ROW_TOLERANCE: f64 = 0.25;

/// Which rows of the Gaussian grid of `n` a file's are: the inverse of the
/// iteration's first guess, rounded, so a whole file's are all of them and a file
/// cut out of one starts wherever its first latitude says, its rows still
/// consecutive. A file that is not on the grid is refused. Mirrors
/// `gaussian_rows` in `xuebuild/grid.py`.
pub fn gaussian_rows(grid: &GridInfo, n: u32, path: &Path) -> Result<GaussianRows> {
    let order = 2.0 * f64::from(n);
    let first_row = ((90.0 - grid.first_latitude) * (order + 0.5) / 180.0 - 0.25).floor();
    let step = grid.latitude_step.abs();
    if first_row < 0.0
        || first_row + grid.height as f64 > order
        || (step * 2.0 * f64::from(n) - 180.0).abs() > 1.8
        || (gaussian_latitude(n, first_row as u32) - grid.first_latitude).abs() > step * GAUSSIAN_ROW_TOLERANCE
    {
        return Err(EncodeError::conversion(format!(
            "{} is not on the Gaussian grid of {n} latitudes per hemisphere",
            path.display()
        )));
    }
    Ok(GaussianRows { n, first_row: first_row as u32, row_stride: 1 })
}

/// How far short of (or past) a full circle a grid's columns may fall, as a
/// fraction of one cell, and still be the global grid they clearly are.
const GLOBAL_SPAN_TOLERANCE_CELLS: f64 = 1e-3;

/// Give a global grid the exact step and origin its column count implies —
/// the port of `snap_global_longitudes` in `xuebuild/grid.py`, which
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

/// The steps a regional grid is snapped to, as (unit, divisor) in the
/// order they are tried: whole thousandths of a degree, then whole
/// arc-minutes (EFAS's 1/60°, which no decimal spells: the double GDAL
/// derives for it needs seventeen digits, more than some `gdalinfo`
/// releases print). Mirrors `_REGIONAL_STEP_UNITS` in `xuebuild/grid.py`.
fn regional_step_units() -> [(f64, f64); 2] {
    [(1e-3, 1000.0), (1.0 / 60.0, 60.0)]
}

/// Give a regional grid the round step it was clearly published on — the
/// port of `snap_regional_steps` in `xuebuild/grid.py`, which
/// explains the rule; the two must agree to the bit.
///
/// The same GDAL habit that puts a global grid a hair off the globe puts a
/// regional one a hair off its step: MRMS writes the last longitude of its
/// 0.01° grid two millionths short, which GDAL turns into a 0.0099999997°
/// step. A grid whose cells, at the nearest whole thousandth of a degree
/// or, failing that, the nearest whole arc-minute (EFAS's 1/60°), span its
/// extent to within a thousandth of a cell is on that step, on both axes
/// independently, and its first *center* is placed on the half-step grid
/// (a grid whose edges are on whole steps has its centers half a step in)
/// when it lies within the same tolerance. A step that is near neither
/// passes through unchanged, as does a global grid.
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
/// given. The snapped values are whole units divided out at the end —
/// `30 / 1000` is the double nearest 0.03 where `30 * 0.001` is not.
fn snap_axis(step: f64, first: f64, count: usize) -> (f64, f64) {
    let count = count as f64;
    for (unit, divisor) in regional_step_units() {
        let units = (step / unit + 0.5).floor();
        let rounded = units / divisor;
        if units == 0.0 || (count * step - count * rounded).abs() > step.abs() * GLOBAL_SPAN_TOLERANCE_CELLS {
            continue;
        }
        let half_steps = (first / (rounded / 2.0) + 0.5).floor();
        let mut snapped_first = (half_steps * units) / (2.0 * divisor);
        if (snapped_first - first).abs() > rounded.abs() * GLOBAL_SPAN_TOLERANCE_CELLS {
            snapped_first = first;
        }
        return (rounded, snapped_first);
    }
    (step, first)
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
///
/// On a projected grid the box is a curved patch: see `crop_projected`. A
/// Gaussian grid is cropped by its regular fields, and its rows follow.
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
    if let Some(projection) = grid.projection {
        return crop_projected(&grid, &projection, west, south, east, north);
    }

    // Degrees east of the grid's origin. A wrapping grid has every longitude
    // on some column. A regional grid is taken in the copy of the world it
    // lies in: a satellite disk over the Pacific runs from 80.7 to 200.7, and
    // a box spelled -170 is at 190 on it, while a box a few degrees west of
    // the origin is west of it, not most of the way around the world.
    let mut offset = (west - grid.first_longitude).rem_euclid(360.0);
    if !grid.wraps() && offset > (grid.width - 1) as f64 * grid.longitude_step + 1e-9 {
        offset -= 360.0;
    }
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
        downsample: grid.downsample,
        projection: None,
        gaussian: grid.gaussian.map(|rows| GaussianRows {
            first_row: rows.first_row + row_start as u32 * rows.row_stride,
            ..rows
        }),
    })
}

/// Points walked along each edge of a box, ends included: an edge is a curve on
/// the projected plane, and these bound it to well under a cell of any window a
/// case asks for.
const BBOX_EDGE_POINTS: usize = 64;
/// Past this the cone's radius runs away, and a box reaching a pole is the whole
/// grid in any case.
const BBOX_LATITUDE_LIMIT: f64 = 89.9;

/// The smallest whole-cell window covering a box's edges. Each of the four edges
/// is walked in [`BBOX_EDGE_POINTS`] points and projected; the window takes the
/// cell at or before the lowest column and row they reach and the one at or after
/// the highest, the rule a regular grid's crop has, clamped to the grid. A box
/// wholly off the grid is refused.
fn crop_projected(
    grid: &GridInfo,
    projected: &ProjectedGrid,
    west: f64,
    south: f64,
    east: f64,
    north: f64,
) -> Result<GridInfo> {
    let south = south.max(-BBOX_LATITUDE_LIMIT);
    let north = north.min(BBOX_LATITUDE_LIMIT);
    let (mut lowest_column, mut highest_column) = (f64::INFINITY, f64::NEG_INFINITY);
    let (mut lowest_row, mut highest_row) = (f64::INFINITY, f64::NEG_INFINITY);
    let last = (BBOX_EDGE_POINTS - 1) as f64;
    for index in 0..BBOX_EDGE_POINTS {
        let longitude = west + (east - west) * index as f64 / last;
        let latitude = south + (north - south) * index as f64 / last;
        for (point_longitude, point_latitude) in
            [(longitude, south), (longitude, north), (west, latitude), (east, latitude)]
        {
            let (x, y) = projected.projection.forward_wrapped(point_longitude, point_latitude);
            let column = (x - projected.x0) / projected.dx;
            let row = (y - projected.y0) / projected.dy;
            lowest_column = lowest_column.min(column);
            highest_column = highest_column.max(column);
            lowest_row = lowest_row.min(row);
            highest_row = highest_row.max(row);
        }
    }
    let column_start = ((lowest_column + 1e-9).floor() as i64).max(0);
    let column_end = ((highest_column - 1e-9).ceil() as i64).min(grid.width as i64 - 1);
    let row_start = ((lowest_row + 1e-9).floor() as i64).max(0);
    let row_end = ((highest_row - 1e-9).ceil() as i64).min(grid.height as i64 - 1);
    let width = column_end - column_start + 1;
    let height = row_end - row_start + 1;
    if width <= 0 || height <= 0 {
        return Err(EncodeError::conversion(format!(
            "bbox does not overlap the source grid: ({west}, {south}, {east}, {north})"
        )));
    }
    let (row_start, column_start) = (row_start as usize, column_start as usize);
    let (height, width) = (height as usize, width as usize);
    Ok(GridInfo::projected(
        projected.window(row_start, column_start, height, width),
        Some(CropWindow {
            source_width: grid.width,
            source_height: grid.height,
            row_start,
            column_start,
            width,
            height,
        }),
    ))
}

#[cfg(test)]
mod tests {
    use super::{
        crop_grid, gaussian_rows, normalize_longitudes, snap_global_longitudes, snap_regional_steps,
        BlockReduction, GridInfo,
    };
    use crate::encode::reproject::{LambertConformal, ProjectedGrid};
    use crate::projection::GaussianRows;
    use std::path::Path;

    fn hrrr() -> ProjectedGrid {
        ProjectedGrid {
            projection: LambertConformal {
                radius: 6371229.0,
                standard_parallel_1: 38.5,
                standard_parallel_2: 38.5,
                latitude_of_origin: 38.5,
                central_meridian: -97.5,
            },
            width: 1799,
            height: 1059,
            x0: -2697520.143,
            y0: 1587306.153,
            dx: 3000.0,
            dy: -3000.0,
        }
    }

    #[test]
    fn a_box_on_a_projected_grid_is_the_window_around_its_edges() {
        let grid = hrrr();
        let cropped = crop_grid(GridInfo::projected(grid, None), (-100.0, 35.0, -95.0, 40.0)).expect("window");
        let crop = cropped.crop.expect("a window");
        assert_eq!((crop.source_width, crop.source_height), (1799, 1059));
        assert_eq!((crop.width, crop.height), (cropped.width, cropped.height));
        assert_eq!(cropped.source_shape(), (1059, 1799));
        let window = cropped.projection.expect("projected");
        assert_eq!(window.x0, grid.x0 + crop.column_start as f64 * grid.dx);
        assert_eq!(window.y0, grid.y0 + crop.row_start as f64 * grid.dy);
        // Every corner of the box lies inside the window, spelled either way.
        for (longitude, latitude) in [(-100.0, 35.0), (-95.0, 35.0), (-100.0, 40.0), (260.0, 40.0)] {
            let (x, y) = grid.projection.forward_wrapped(longitude, latitude);
            let (column, row) = ((x - window.x0) / window.dx, (y - window.y0) / window.dy);
            assert!(column >= 0.0 && column <= (window.width - 1) as f64, "{longitude} {column}");
            assert!(row >= 0.0 && row <= (window.height - 1) as f64, "{latitude} {row}");
        }
        assert!(crop_grid(GridInfo::projected(grid, None), (-95.0, 35.0, -100.0, 40.0)).is_err());
        assert!(crop_grid(GridInfo::projected(grid, None), (100.0, 35.0, 110.0, 40.0)).is_err());
        assert!(crop_grid(cropped, (-100.0, 35.0, -95.0, 40.0)).is_err(), "cropped once");
    }

    #[test]
    fn a_projected_grid_says_nothing_regular() {
        let block = GridInfo::projected(hrrr(), None).decimated().metadata();
        assert_eq!(
            block.keys().map(String::as_str).collect::<Vec<_>>(),
            ["width", "height", "layout", "rowOrder", "columnOrder", "wrapLongitude", "projection"]
        );
        assert_eq!(block["width"], 900);
        assert_eq!(block["projection"]["dy"], -6000.0);
    }

    /// The whole CFSv2 grid is all the rows; a window is wherever its first
    /// latitude says, and a file that is not on the grid is refused.
    #[test]
    fn a_gaussian_grid_finds_its_rows() {
        let path = Path::new("x");
        let step = -180.0 / 190.0;
        let whole = GridInfo::new(384, 190, -180.0, 89.7507 + step / 2.0, 0.9375, step);
        assert_eq!(gaussian_rows(&whole, 95, path).unwrap(), GaussianRows { n: 95, first_row: 0, row_stride: 1 });
        let first = crate::projection::gaussian_latitude(95, 30);
        let window = GridInfo::new(48, 40, 90.0, first, 0.9375, step);
        assert_eq!(gaussian_rows(&window, 95, path).unwrap().first_row, 30);
        // Half a row off, past the last row, or at another grid's step.
        assert!(gaussian_rows(&GridInfo::new(48, 40, 90.0, first + step / 2.0, 0.9375, step), 95, path).is_err());
        assert!(gaussian_rows(&GridInfo::new(48, 170, 90.0, first, 0.9375, step), 95, path).is_err());
        assert!(gaussian_rows(&GridInfo::new(48, 40, 90.0, first, 0.9375, -0.5), 95, path).is_err());
    }

    #[test]
    fn a_crop_and_a_tier_keep_a_gaussian_grid_s_rows() {
        let step = -180.0 / 190.0;
        let mut grid = GridInfo::new(384, 190, -180.0, 89.277, 0.9375, step);
        grid.gaussian = Some(GaussianRows { n: 95, first_row: 0, row_stride: 1 });
        let cropped = crop_grid(grid, (0.0, 0.0, 30.0, 30.0)).expect("window");
        let row_start = cropped.crop.expect("a window").row_start as u32;
        assert_eq!(cropped.gaussian, Some(GaussianRows { n: 95, first_row: row_start, row_stride: 1 }));
        let tier = cropped.decimated();
        assert_eq!(tier.gaussian, Some(GaussianRows { n: 95, first_row: row_start, row_stride: 2 }));
        assert_eq!(tier.metadata()["gaussian"]["rowStride"], 2);
    }

    const WAVE_STEP: f64 = 0.2500000111188325;
    const WAVE_ORIGIN: f64 = -180.12500000555943;

    #[test]
    fn a_regional_grid_past_the_antimeridian_is_cropped_in_its_own_copy_of_the_world() {
        // A Himawari disk on plate carrée: 80.7 to 200.7 at 0.04°, not wrapping.
        let disk = GridInfo::new(3000, 3000, 80.7, 60.0, 0.04, -0.04);
        assert!(!disk.wraps());
        // A box spelled west of -180 lands on the disk's eastern columns...
        let east = crop_grid(disk.clone(), (-170.0, 0.0, -160.0, 10.0)).unwrap();
        assert_eq!(east.crop.as_ref().unwrap().column_start, 2732);
        assert_eq!(east.width, 252);
        assert_eq!(east.first_longitude, -170.02);
        // ...the same box spelled past 180 is the same window...
        let same = crop_grid(disk.clone(), (190.0, 0.0, 200.0, 10.0)).unwrap();
        assert_eq!(same.crop, east.crop);
        // ...a box just west of the origin clamps to it rather than reading
        // as almost a whole world east, and one east of the disk misses it.
        let west = crop_grid(disk.clone(), (70.0, 0.0, 90.0, 10.0)).unwrap();
        assert_eq!(west.crop.as_ref().unwrap().column_start, 0);
        assert_eq!(west.first_longitude, 80.7);
        assert!(crop_grid(disk, (-150.0, 0.0, -100.0, 10.0)).is_err());
    }

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

    /// The EFAS grid is on the arc-minute, which no thousandth spells: GDAL
    /// derives 0.016666666666666607 and -0.016666666666666226 from the
    /// coordinates, and both axes land on the double nearest 1/60 with the
    /// centers on the half-minute grid — the same doubles the Python side
    /// writes, however many digits its `gdalinfo` printed.
    #[test]
    fn the_efas_grid_snaps_to_its_arc_minute() {
        let (lon_step, lat_step) = (0.016666666666666607, -0.016666666666666226);
        let full = snap_regional_steps(GridInfo::new(4530, 2970, -25.25 + lon_step / 2.0, 72.25 + lat_step / 2.0, lon_step, lat_step));
        assert_eq!((full.longitude_step, full.latitude_step), (1.0 / 60.0, -1.0 / 60.0));
        assert_eq!((full.first_longitude, full.first_latitude), (-3029.0 / 120.0, 8669.0 / 120.0));
        let crop = snap_regional_steps(GridInfo::new(21, 15, -0.75 + lon_step / 2.0, 39.55 + lat_step / 2.0, lon_step, lat_step));
        assert_eq!((crop.first_longitude, crop.first_latitude), (-89.0 / 120.0, 4745.0 / 120.0));
        // A step a whole thousandth off stays off; so does one near no minute.
        for grid in [
            GridInfo::new(100, 100, -100.0, 40.0, 0.011, -0.011),
            GridInfo::new(100, 100, -100.0, 40.0, 0.0175, -0.0175),
        ] {
            let same = snap_regional_steps(grid.clone());
            assert_eq!((same.longitude_step, same.first_longitude), (grid.longitude_step, grid.first_longitude));
        }
    }

    /// The CFSv2 T126 Gaussian grid, as GDAL reports the surface flux
    /// records: 384 x 190 from a corner at (-0.46875, 89.7507), so the
    /// first cell center is exactly on Greenwich — the sflux grid's shape.
    /// It closes the circle at 360 / 384, and the columns at or past 180
    /// move to the front, which is what puts it in the -180-first layout
    /// every other source arrives in.
    #[test]
    fn the_cfs_gaussian_grid_snaps_to_the_circle_and_rolls_to_minus_180() {
        let longitude_step = 0.9375;
        let latitude_step = -0.9473684210526316;
        let raw = GridInfo::new(
            384,
            190,
            -0.46875 + longitude_step / 2.0,
            89.7507 + latitude_step / 2.0,
            longitude_step,
            latitude_step,
        );
        assert_eq!(raw.first_longitude, 0.0, "the first center is on Greenwich");
        assert!(raw.wraps());
        let snapped = snap_global_longitudes(raw);
        assert_eq!(snapped.longitude_step, 360.0 / 384.0);
        assert_eq!(snapped.first_longitude, 0.0);
        // A wrapping grid is never re-stepped as a regional one.
        let snapped = snap_regional_steps(snapped);
        assert_eq!(snapped.longitude_step, 0.9375);
        let rolled = normalize_longitudes(snapped);
        assert_eq!(rolled.column_roll, 192);
        assert_eq!(rolled.first_longitude, -180.0);
        assert_eq!((rolled.width, rolled.height), (384, 190));
        // 192 columns of 0.9375° is exactly half the world, so the roll is
        // the grid's own half: column 0 of the rolled plane is 180° E.
        assert_eq!(rolled.column_roll as f64 * rolled.longitude_step, 180.0);
    }

    /// A 48 x 40 cell window cut out of that grid with `gdal_translate
    /// -srcwin` — the CFSv2 test fixture. GDAL re-derives the step from the
    /// window's first and last coordinates and lands a hair off 0.9375, and
    /// the regional snap must leave it there: a 0.9375° step is nowhere
    /// near a whole thousandth of a degree, and rounding it to 0.938 would
    /// move the last column a fortieth of a cell.
    #[test]
    fn a_crop_of_the_cfs_grid_keeps_its_gaussian_steps() {
        let longitude_step = 0.9374986808510636;
        let latitude_step = -0.9473684358974358;
        let raw = GridInfo::new(
            48,
            40,
            89.53112565957447 + longitude_step / 2.0,
            59.434895217948714 + latitude_step / 2.0,
            longitude_step,
            latitude_step,
        );
        assert!(!raw.wraps());
        let snapped = snap_regional_steps(snap_global_longitudes(raw.clone()));
        assert_eq!(snapped.longitude_step, longitude_step);
        assert_eq!(snapped.latitude_step, latitude_step);
        assert_eq!(snapped.first_longitude, raw.first_longitude);
        assert_eq!(snapped.first_latitude, raw.first_latitude);
        // And a window of a global grid is not rolled either.
        assert_eq!(normalize_longitudes(snapped).column_roll, 0);
    }

    /// The other CFSv2 grid: the 1° pressure-level series of the `pgb`
    /// family, which GDAL reports as 360 x 181 from a corner at
    /// (-180.5, 90.5). Its step is already exactly 1° and its first cell
    /// center already at -180, so the global snap changes nothing and there
    /// is no roll — the family arrives in the -180-first layout the surface
    /// grid has to be rolled into.
    #[test]
    fn the_cfs_one_degree_grid_needs_neither_snapping_nor_a_roll() {
        let raw = GridInfo::new(360, 181, -180.5 + 0.5, 90.5 - 0.5, 1.0, -1.0);
        assert_eq!((raw.first_longitude, raw.first_latitude), (-180.0, 90.0));
        assert!(raw.wraps());
        let snapped = snap_global_longitudes(raw);
        assert_eq!(snapped.longitude_step, 360.0 / 360.0);
        assert_eq!(snapped.longitude_step, 1.0);
        assert_eq!(snapped.first_longitude, -180.0);
        // A wrapping grid is never re-stepped as a regional one.
        let snapped = snap_regional_steps(snapped);
        assert_eq!((snapped.longitude_step, snapped.latitude_step), (1.0, -1.0));
        let rolled = normalize_longitudes(snapped);
        assert_eq!(rolled.column_roll, 0);
        assert_eq!((rolled.first_longitude, rolled.first_latitude), (-180.0, 90.0));
        assert_eq!((rolled.width, rolled.height), (360, 181));
        // 181 rows of 1° is pole to pole, the row count the 90 x 91 tile
        // cuts into two rows of tiles, the last one cell short.
        assert_eq!(181_usize.div_ceil(91), 2);
    }

    /// The 45 x 38 cell window of that grid the CFSv2 pressure-level fixture
    /// was cut to with `gdal_translate -srcwin 270 30 45 38`: GDAL reports
    /// the geotransform (89.5, 1, 0, 60.5, 0, -1), so the window keeps whole
    /// degrees, the regional snap leaves both axes where they are, and
    /// nothing rolls.
    #[test]
    fn a_crop_of_the_cfs_one_degree_grid_keeps_its_whole_degrees() {
        let raw = GridInfo::new(45, 38, 89.5 + 0.5, 60.5 - 0.5, 1.0, -1.0);
        assert!(!raw.wraps());
        let snapped = snap_regional_steps(snap_global_longitudes(raw));
        assert_eq!((snapped.longitude_step, snapped.latitude_step), (1.0, -1.0));
        assert_eq!((snapped.first_longitude, snapped.first_latitude), (90.0, 60.0));
        assert_eq!(normalize_longitudes(snapped).column_roll, 0);
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
