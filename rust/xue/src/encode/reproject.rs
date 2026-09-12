//! Regridding a projected source onto the regular latitude/longitude grid
//! the format describes — the port of `xuebuild/reproject.py`, which
//! explains the rules; the two must agree to the bit.
//!
//! A model computed on a map projection (HRRR, Lambert conformal conic,
//! 3 km) is resampled onto a regular grid before anything else reads it: the
//! plane is bilinearly sampled at each target cell center, whose projected
//! position is found with the projection's own forward formulas. The target
//! grid is the source's footprint snapped outwards to the declared step, and
//! the corners of that rectangle the conic domain never covered take the
//! nearest source cell, continued outwards.
//!
//! The per-row and per-column trigonometry runs through `f64`'s methods —
//! the C library's `sin`/`cos`/`tan`/`pow`, which is what Python's `math`
//! module calls too — and only exact IEEE multiplications, subtractions and
//! divisions are done on the whole plane, in the reference encoder's order.

use regex::Regex;
use std::sync::LazyLock;

use crate::encode::errors::{EncodeError, Result};

/// A Lambert conformal conic projection on a sphere, as GRIB2 grid
/// definition template 3.30 declares it and as GDAL reports it back
/// (`Lambert Conic Conformal (2SP)` with a spherical datum). Snyder (1987)
/// §15, spherical case; with two equal standard parallels the cone constant
/// is `sin` of the parallel.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LambertConformal {
    /// Sphere radius in metres — 6371229 for every NCEP product.
    pub radius: f64,
    pub standard_parallel_1: f64,
    pub standard_parallel_2: f64,
    pub latitude_of_origin: f64,
    pub central_meridian: f64,
}

impl LambertConformal {
    pub fn cone_constant(&self) -> f64 {
        let phi_1 = self.standard_parallel_1.to_radians();
        let phi_2 = self.standard_parallel_2.to_radians();
        if phi_1 == phi_2 {
            return phi_1.sin();
        }
        (phi_1.cos() / phi_2.cos()).ln()
            / ((std::f64::consts::PI / 4.0 + phi_2 / 2.0).tan()
                / (std::f64::consts::PI / 4.0 + phi_1 / 2.0).tan())
            .ln()
    }

    /// `R·F` — the radius of the parallel at latitude `φ` is
    /// `scale / tan(π/4 + φ/2)^n`.
    pub fn scale(&self) -> f64 {
        let n = self.cone_constant();
        let phi_1 = self.standard_parallel_1.to_radians();
        self.radius * (phi_1.cos() * (std::f64::consts::PI / 4.0 + phi_1 / 2.0).tan().powf(n) / n)
    }

    /// Projected distance from the apex of the cone to the parallel at
    /// `latitude` (degrees).
    pub fn parallel_radius(&self, latitude: f64) -> f64 {
        self.scale()
            / (std::f64::consts::PI / 4.0 + latitude.to_radians() / 2.0)
                .tan()
                .powf(self.cone_constant())
    }

    pub fn origin_radius(&self) -> f64 {
        self.parallel_radius(self.latitude_of_origin)
    }

    /// The angle, in radians, the meridian at `longitude` makes with the
    /// central meridian on the cone.
    pub fn meridian_angle(&self, longitude: f64) -> f64 {
        self.cone_constant() * (longitude - self.central_meridian).to_radians()
    }

    /// Projected `(x, y)` in metres of one point.
    pub fn forward(&self, longitude: f64, latitude: f64) -> (f64, f64) {
        let rho = self.parallel_radius(latitude);
        let theta = self.meridian_angle(longitude);
        (rho * theta.sin(), self.origin_radius() - rho * theta.cos())
    }

    /// Longitude and latitude, in degrees, of one projected point.
    pub fn inverse(&self, x: f64, y: f64) -> (f64, f64) {
        let n = self.cone_constant();
        let dy = self.origin_radius() - y;
        let rho = (x * x + dy * dy).sqrt().copysign(n);
        let theta = x.atan2(dy);
        let latitude = 2.0 * (self.scale() / rho).powf(1.0 / n).atan() - std::f64::consts::PI / 2.0;
        (
            self.central_meridian + (theta / n).to_degrees(),
            latitude.to_degrees(),
        )
    }
}

/// A source grid in projected space: what GDAL's geotransform says about a
/// Lambert conformal GRIB, in the north-to-south row order GDAL reads it in.
/// `x0`/`y0` are the *center* of the first (north-west) cell; `dx`/`dy` are
/// both positive.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ProjectedGrid {
    pub projection: LambertConformal,
    pub width: usize,
    pub height: usize,
    pub x0: f64,
    pub y0: f64,
    pub dx: f64,
    pub dy: f64,
}

impl ProjectedGrid {
    /// `(west, south, east, north)` in degrees: the extremes every boundary
    /// cell center reaches — the whole boundary, in the reference encoder's
    /// order (the top and bottom rows column by column, then the two side
    /// columns), since `min`/`max` over the same doubles land the same.
    pub fn footprint(&self) -> (f64, f64, f64, f64) {
        let (mut west, mut south) = (f64::INFINITY, f64::INFINITY);
        let (mut east, mut north) = (f64::NEG_INFINITY, f64::NEG_INFINITY);
        let last_column = self.width - 1;
        let last_row = self.height - 1;
        let mut cells: Vec<(usize, usize)> = Vec::with_capacity(2 * (self.width + self.height));
        for column in 0..self.width {
            cells.push((column, 0));
            cells.push((column, last_row));
        }
        for column in [0, last_column] {
            for row in 1..last_row {
                cells.push((column, row));
            }
        }
        for (column, row) in cells {
            let (longitude, latitude) = self.projection.inverse(
                self.x0 + column as f64 * self.dx,
                self.y0 - row as f64 * self.dy,
            );
            west = west.min(longitude);
            east = east.max(longitude);
            south = south.min(latitude);
            north = north.max(latitude);
        }
        (west, south, east, north)
    }
}

/// What a source declares: that its planes are projected and must be
/// resampled onto a regular grid of `step` degrees.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Regrid {
    pub step: f64,
}

/// Bilinear sampling of every source plane at the target grid's cell
/// centers, computed once per build and shared by every plane.
#[derive(Debug, Clone)]
pub struct Resampler {
    pub source: ProjectedGrid,
    pub width: usize,
    pub height: usize,
    pub first_longitude: f64,
    pub first_latitude: f64,
    pub step: f64,
    /// Per target cell, row-major: the source column west of it and the
    /// source row north of it, and the fractions of the way east and south
    /// to the next.
    column: Vec<usize>,
    row: Vec<usize>,
    fx: Vec<f64>,
    fy: Vec<f64>,
}

impl Resampler {
    pub fn source_shape(&self) -> (usize, usize) {
        (self.source.height, self.source.width)
    }

    /// One source plane, `(source.height, source.width)` row-major,
    /// resampled to `(height, width)`. The four corner weights are applied
    /// in the reference encoder's order — the two rows blended east-west
    /// first, then north-south.
    pub fn take(&self, plane: &[f64]) -> Result<Vec<f64>> {
        if plane.len() != self.source.width * self.source.height {
            return Err(EncodeError::conversion(format!(
                "resampler expected a {}x{} plane, got {} values",
                self.source.width,
                self.source.height,
                plane.len()
            )));
        }
        let stride = self.source.width;
        let mut out = Vec::with_capacity(self.width * self.height);
        for index in 0..self.width * self.height {
            let column = self.column[index];
            let row = self.row[index];
            let fx = self.fx[index];
            let fy = self.fy[index];
            let base = row * stride + column;
            let top = plane[base] * (1.0 - fx) + plane[base + 1] * fx;
            let bottom = plane[base + stride] * (1.0 - fx) + plane[base + stride + 1] * fx;
            out.push(top * (1.0 - fy) + bottom * fy);
        }
        Ok(out)
    }
}

/// Round to ten decimal places the way Python's `round(value, 10)` does.
fn round10(value: f64) -> f64 {
    format!("{value:.10}").parse().unwrap_or(value)
}

/// The regular grid a projected source lands on, and how to sample it —
/// the port of `build_resampler` in `xuebuild/reproject.py`.
pub fn build_resampler(source: ProjectedGrid, regrid: Regrid) -> Result<Resampler> {
    if source.width < 2 || source.height < 2 {
        return Err(EncodeError::conversion(
            "a projected grid needs at least two rows and two columns to resample",
        ));
    }
    let step = regrid.step;
    let (west, south, east, north) = source.footprint();
    let first_column = (west / step).floor() as i64;
    let last_column = (east / step).ceil() as i64;
    let first_row = (north / step).ceil() as i64;
    let last_row = (south / step).floor() as i64;
    let width = (last_column - first_column + 1) as usize;
    let height = (first_row - last_row + 1) as usize;
    let first_longitude = round10(first_column as f64 * step);
    let first_latitude = round10(first_row as f64 * step);
    if width as f64 * step > 360.0 {
        return Err(EncodeError::conversion(
            "a projected source wider than the globe cannot be regridded",
        ));
    }

    let projection = source.projection;
    let origin_radius = projection.origin_radius();
    let radius: Vec<f64> = (0..height)
        .map(|j| projection.parallel_radius(first_latitude - j as f64 * step))
        .collect();
    let angles: Vec<f64> = (0..width)
        .map(|i| projection.meridian_angle(first_longitude + i as f64 * step))
        .collect();
    let sin_theta: Vec<f64> = angles.iter().map(|theta| theta.sin()).collect();
    let cos_theta: Vec<f64> = angles.iter().map(|theta| theta.cos()).collect();

    let cells = width * height;
    let mut column = Vec::with_capacity(cells);
    let mut row = Vec::with_capacity(cells);
    let mut fx = Vec::with_capacity(cells);
    let mut fy = Vec::with_capacity(cells);
    let max_column = (source.width - 1) as f64;
    let max_row = (source.height - 1) as f64;
    let floor_column = (source.width - 2) as f64;
    let floor_row = (source.height - 2) as f64;
    for j in 0..height {
        for i in 0..width {
            let x = radius[j] * sin_theta[i];
            let y = origin_radius - radius[j] * cos_theta[i];
            // Clamp into the source grid: a cell beyond its edge samples
            // the edge. `clamp` is numpy's `clip` for finite bounds.
            let c = ((x - source.x0) / source.dx).clamp(0.0, max_column);
            let r = ((source.y0 - y) / source.dy).clamp(0.0, max_row);
            let c0 = c.floor().min(floor_column);
            let r0 = r.floor().min(floor_row);
            column.push(c0 as usize);
            row.push(r0 as usize);
            fx.push(c - c0);
            fy.push(r - r0);
        }
    }
    Ok(Resampler {
        source,
        width,
        height,
        first_longitude,
        first_latitude,
        step,
        column,
        row,
        fx,
        fy,
    })
}

// `PARAMETER["Latitude of false origin",38.5,` — GDAL's WKT2 spelling of
// each Lambert conformal parameter, as `gdalinfo -json` prints it and as
// `OSRExportToWktEx` returns it.
static WKT_PARAMETER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"PARAMETER\["([^"]+)",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)"#).expect("valid regex")
});
static WKT_SPHERE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"ELLIPSOID\["[^"]*",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?),\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)"#)
        .expect("valid regex")
});
const LAMBERT_METHOD: &str = "Lambert Conic Conformal (2SP)";

/// The projection a GDAL WKT string describes, or `None` for a geographic
/// (regular latitude/longitude) coordinate system. Anything projected that is
/// not a spherical Lambert conformal conic is an error.
pub fn lambert_conformal_from_wkt(wkt: &str) -> Result<Option<LambertConformal>> {
    let trimmed = wkt.trim_start();
    if trimmed.is_empty() || trimmed.starts_with("GEOGCRS") || trimmed.starts_with("GEOGCS") {
        return Ok(None);
    }
    if !wkt.contains(LAMBERT_METHOD) {
        return Err(EncodeError::conversion(format!(
            "unsupported map projection (only {LAMBERT_METHOD} on a sphere is): {}",
            &wkt[..wkt.len().min(80)]
        )));
    }
    let sphere = WKT_SPHERE
        .captures(wkt)
        .ok_or_else(|| EncodeError::conversion("Lambert conformal grids are supported on a sphere only"))?;
    let radius: f64 = sphere[1].parse().map_err(|_| EncodeError::conversion("invalid sphere radius"))?;
    let flattening: f64 = sphere[2].parse().map_err(|_| EncodeError::conversion("invalid sphere flattening"))?;
    if flattening != 0.0 {
        return Err(EncodeError::conversion(
            "Lambert conformal grids are supported on a sphere only",
        ));
    }
    let parameter = |name: &str| -> Result<f64> {
        WKT_PARAMETER
            .captures_iter(wkt)
            .find(|capture| &capture[1] == name)
            .and_then(|capture| capture[2].parse().ok())
            .ok_or_else(|| EncodeError::conversion(format!("Lambert conformal WKT lacks '{name}'")))
    };
    let projection = LambertConformal {
        radius,
        standard_parallel_1: parameter("Latitude of 1st standard parallel")?,
        standard_parallel_2: parameter("Latitude of 2nd standard parallel")?,
        latitude_of_origin: parameter("Latitude of false origin")?,
        central_meridian: parameter("Longitude of false origin")?,
    };
    if parameter("Easting at false origin").unwrap_or(0.0) != 0.0
        || parameter("Northing at false origin").unwrap_or(0.0) != 0.0
    {
        return Err(EncodeError::conversion(
            "Lambert conformal grids with a false origin are unsupported",
        ));
    }
    Ok(Some(projection))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The HRRR CONUS grid as GDAL reports it: 1799 x 1059 cells of 3 km,
    /// the first cell center at 21.138123 N, 122.719528 W.
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
            x0: -2699020.14252193 + 1500.0,
            y0: 1588193.8474433357 - 1500.0,
            dx: 3000.0,
            dy: 3000.0,
        }
    }

    #[test]
    fn the_first_cell_projects_where_gdal_puts_it() {
        let grid = hrrr();
        let (x, y) = grid.projection.forward(-122.719528, 21.138123);
        assert!((x - grid.x0).abs() < 1e-6, "{x}");
        assert!((y - (grid.y0 - 1058.0 * grid.dy)).abs() < 1e-6, "{y}");
        let (longitude, latitude) = grid.projection.inverse(x, y);
        assert!((longitude + 122.719528).abs() < 1e-9);
        assert!((latitude - 21.138123).abs() < 1e-9);
    }

    #[test]
    fn the_production_grid_is_the_footprint_at_three_hundredths() {
        let resampler = build_resampler(hrrr(), Regrid { step: 0.03 }).expect("resampler");
        assert_eq!((resampler.width, resampler.height), (2441, 1051));
        assert_eq!((resampler.first_longitude, resampler.first_latitude), (-134.1, 52.62));
    }

    #[test]
    fn a_constant_plane_stays_constant_and_a_linear_one_is_exact() {
        let grid = ProjectedGrid { width: 40, height: 30, ..hrrr() };
        let resampler = build_resampler(grid, Regrid { step: 0.05 }).expect("resampler");
        // To rounding: the weights are applied one by one, so a constant
        // comes back within an ulp or two rather than bit-exact.
        let constant = vec![7.5; 40 * 30];
        assert!(resampler.take(&constant).expect("plane").iter().all(|&v| (v - 7.5).abs() < 1e-12));
        // Bilinear interpolation reproduces a plane that is linear in the
        // source indices, inside the source and along its clamped extension.
        let linear: Vec<f64> = (0..30)
            .flat_map(|row| (0..40).map(move |column| 2.0 * column as f64 + 0.5 * row as f64))
            .collect();
        let out = resampler.take(&linear).expect("plane");
        for (index, &value) in out.iter().enumerate() {
            let expected = 2.0 * (resampler.column[index] as f64 + resampler.fx[index])
                + 0.5 * (resampler.row[index] as f64 + resampler.fy[index]);
            assert!((value - expected).abs() < 1e-9, "{index}: {value} vs {expected}");
        }
    }

    #[test]
    fn gdal_wkt_is_read_and_a_geographic_one_is_not_a_projection() {
        let wkt = r#"PROJCRS["unnamed",
    BASEGEOGCRS["Coordinate System imported from GRIB file",
        DATUM["unnamed",
            ELLIPSOID["Sphere",6371229,0,
                LENGTHUNIT["metre",1,
                    ID["EPSG",9001]]]],
        PRIMEM["Greenwich",0,
            ANGLEUNIT["degree",0.0174532925199433,
                ID["EPSG",9122]]]],
    CONVERSION["Lambert Conic Conformal (2SP)",
        METHOD["Lambert Conic Conformal (2SP)",
            ID["EPSG",9802]],
        PARAMETER["Latitude of false origin",38.5,
            ANGLEUNIT["degree",0.0174532925199433],
            ID["EPSG",8821]],
        PARAMETER["Longitude of false origin",-97.5,
            ANGLEUNIT["degree",0.0174532925199433],
            ID["EPSG",8822]],
        PARAMETER["Latitude of 1st standard parallel",38.5,
            ANGLEUNIT["degree",0.0174532925199433],
            ID["EPSG",8823]],
        PARAMETER["Latitude of 2nd standard parallel",38.5,
            ANGLEUNIT["degree",0.0174532925199433],
            ID["EPSG",8824]],
        PARAMETER["Easting at false origin",0,
            LENGTHUNIT["metre",1],
            ID["EPSG",8826]],
        PARAMETER["Northing at false origin",0,
            LENGTHUNIT["metre",1],
            ID["EPSG",8827]]],
    CS[Cartesian,2]]"#;
        assert_eq!(lambert_conformal_from_wkt(wkt).expect("parsed"), Some(hrrr().projection));
        assert_eq!(lambert_conformal_from_wkt("").expect("parsed"), None);
        assert_eq!(
            lambert_conformal_from_wkt(r#"GEOGCRS["Coordinate System imported from GRIB file",DATUM["unnamed"]]"#)
                .expect("parsed"),
            None
        );
        assert!(lambert_conformal_from_wkt(r#"PROJCRS["x",CONVERSION["Polar Stereographic"]]"#).is_err());
    }
}
