//! Grid discovery, the -180-first column roll, and regional cropping — the
//! grid half of `xue/binconvert.py`.

use serde_json::{json, Map, Value};

use crate::errors::{EncodeError, Result};

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

#[derive(Debug, Clone, Copy)]
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
        }
    }

    pub fn wraps(&self) -> bool {
        (self.width as f64 * self.longitude_step - 360.0).abs() < 1e-6
    }

    /// `(height, width)` of the plane the extraction reads.
    pub fn source_shape(&self) -> (usize, usize) {
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
        }
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
    })
}
