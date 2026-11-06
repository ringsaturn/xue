//! The projection arithmetic of a model published on its own map projection:
//! the port of `xuebuild/reproject.py`, which explains the rules; the two must
//! agree to the bit.
//!
//! The grid's planes stay as the model wrote them. The projection places the
//! published cells ([`ProjectedGrid`]) and turns grid-relative winds to east and
//! north ([`rotate_winds`]). Point trigonometry runs through `f64`'s methods, the
//! C library's `sin`/`cos`/`tan`/`pow`, which is what Python's `math` module calls
//! too. A plane sees only exact IEEE multiplications, additions, divisions and
//! square roots, which NumPy's vectorised loops round the same way.

use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::LazyLock;

use crate::encode::errors::{EncodeError, Result};
pub use crate::projection::LambertConformal;

/// `grid.projection.kind` of [`LambertConformal`].
const LAMBERT_CONFORMAL_KIND: &str = "lambert-conformal";

/// A published grid in projected space, in the north-to-south row order GDAL
/// reads it in: the `grid.projection` block of a bundle's metadata. `x0`/`y0` are
/// the *center* of the first (north-west) cell; cell (`row`, `column`) is centered
/// at `x0 + column * dx`, `y0 + row * dy`, so `dx` is positive and `dy` negative.
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
    /// Every second row and column from the first: the first cell stays where
    /// it is.
    pub fn decimated(&self) -> Self {
        Self {
            width: self.width.div_ceil(2),
            height: self.height.div_ceil(2),
            dx: self.dx * 2.0,
            dy: self.dy * 2.0,
            ..*self
        }
    }

    /// The `height` x `width` cells from (`row`, `column`).
    pub fn window(&self, row: usize, column: usize, height: usize, width: usize) -> Self {
        Self {
            width,
            height,
            x0: self.x0 + column as f64 * self.dx,
            y0: self.y0 + row as f64 * self.dy,
            ..*self
        }
    }

    /// The `grid.projection` block.
    pub fn metadata(&self) -> Map<String, Value> {
        let projection = &self.projection;
        let mut block = Map::new();
        block.insert("kind".into(), json!(LAMBERT_CONFORMAL_KIND));
        block.insert("radius".into(), json!(projection.radius));
        block.insert("standardParallel1".into(), json!(projection.standard_parallel_1));
        block.insert("standardParallel2".into(), json!(projection.standard_parallel_2));
        block.insert("latitudeOfOrigin".into(), json!(projection.latitude_of_origin));
        block.insert("centralMeridian".into(), json!(projection.central_meridian));
        block.insert("x0".into(), json!(self.x0));
        block.insert("y0".into(), json!(self.y0));
        block.insert("dx".into(), json!(self.dx));
        block.insert("dy".into(), json!(self.dy));
        block
    }
}

/// One grid-relative wind pair turned to east and north, in place, on the flat
/// planes `grid` describes (`docs/encoder.md`, "Native grids").
///
/// Read off the cell's coordinates rather than its longitude, so no libm call
/// runs per cell: only `origin_radius` goes through the C library, once. The
/// radius takes the cone's sign, as in `LambertConformal::inverse`.
pub fn rotate_winds(grid: &ProjectedGrid, u: &mut [f64], v: &mut [f64]) -> Result<()> {
    let cells = grid.width * grid.height;
    if u.len() != cells || v.len() != cells {
        return Err(EncodeError::conversion(format!(
            "a wind pair of {} and {} cells on a {}x{} grid",
            u.len(),
            v.len(),
            grid.width,
            grid.height
        )));
    }
    let sign = 1.0_f64.copysign(grid.projection.cone_constant());
    let origin_radius = grid.projection.origin_radius();
    let columns: Vec<f64> = (0..grid.width).map(|column| grid.x0 + column as f64 * grid.dx).collect();
    for row in 0..grid.height {
        let apex = origin_radius - (grid.y0 + row as f64 * grid.dy);
        let base = row * grid.width;
        for (column, &x) in columns.iter().enumerate() {
            let rho = sign * (apex * apex + x * x).sqrt();
            let (sine, cosine) = (x / rho, apex / rho);
            let (east, north) = (u[base + column], v[base + column]);
            u[base + column] = cosine * east + sine * north;
            v[base + column] = -sine * east + cosine * north;
        }
    }
    Ok(())
}

/// A number the way Python's `repr` prints a float: the shortest round trip.
/// `{:?}` spells it the same from 1e-4 up to 1e16, which holds any sphere's radius
/// and any angle a grid's projection is declared with.
fn shortest(value: f64) -> String {
    format!("{value:?}")
}

/// The projection as WKT2, the `crs_wkt` of a store's `crs` array
/// (`docs/zarr-profile.md`, "Coordinates"). Spelled here rather than taken from
/// GDAL, whose text differs between releases: the array document is part of a
/// store's bytes.
fn lambert_wkt(projection: &LambertConformal) -> String {
    let degree = r#"ANGLEUNIT["degree",0.0174532925199433]"#;
    let metre = r#"LENGTHUNIT["metre",1]"#;
    format!(
        concat!(
            r#"PROJCRS["Lambert Conic Conformal on a sphere","#,
            r#"BASEGEOGCRS["Sphere","#,
            r#"DATUM["Sphere",ELLIPSOID["Sphere",{radius},0,{metre}]],"#,
            r#"PRIMEM["Greenwich",0,{degree}]],"#,
            r#"CONVERSION["Lambert Conic Conformal (2SP)","#,
            r#"METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],"#,
            r#"PARAMETER["Latitude of false origin",{origin},{degree}],"#,
            r#"PARAMETER["Longitude of false origin",{meridian},{degree}],"#,
            r#"PARAMETER["Latitude of 1st standard parallel",{first},{degree}],"#,
            r#"PARAMETER["Latitude of 2nd standard parallel",{second},{degree}],"#,
            r#"PARAMETER["Easting at false origin",0,{metre}],"#,
            r#"PARAMETER["Northing at false origin",0,{metre}]],"#,
            r#"CS[Cartesian,2],"#,
            r#"AXIS["easting",east,ORDER[1],{metre}],"#,
            r#"AXIS["northing",north,ORDER[2],{metre}]]"#,
        ),
        radius = shortest(projection.radius),
        origin = shortest(projection.latitude_of_origin),
        meridian = shortest(projection.central_meridian),
        first = shortest(projection.standard_parallel_1),
        second = shortest(projection.standard_parallel_2),
        degree = degree,
        metre = metre,
    )
}

/// The CF grid mapping a store's `crs` array carries, in the order the array
/// document writes it.
pub fn cf_attributes(projection: &LambertConformal) -> Value {
    json!({
        "grid_mapping_name": "lambert_conformal_conic",
        "latitude_of_projection_origin": projection.latitude_of_origin,
        "longitude_of_central_meridian": projection.central_meridian,
        "standard_parallel": [projection.standard_parallel_1, projection.standard_parallel_2],
        "earth_radius": projection.radius,
        "crs_wkt": lambert_wkt(projection),
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
            dy: -3000.0,
        }
    }

    #[test]
    fn the_first_cell_projects_where_gdal_puts_it() {
        let grid = hrrr();
        let (x, y) = grid.projection.forward(-122.719528, 21.138123);
        assert!((x - grid.x0).abs() < 1e-6, "{x}");
        assert!((y - (grid.y0 + 1058.0 * grid.dy)).abs() < 1e-6, "{y}");
        let (longitude, latitude) = grid.projection.inverse(x, y);
        assert!((longitude + 122.719528).abs() < 1e-9);
        assert!((latitude - 21.138123).abs() < 1e-9);
    }

    /// The reference's `LambertConformal.wkt` for HRRR's projection, whole.
    #[test]
    fn the_wkt_is_the_reference_s_spelling() {
        assert_eq!(
            cf_attributes(&hrrr().projection)["crs_wkt"],
            concat!(
                r#"PROJCRS["Lambert Conic Conformal on a sphere",BASEGEOGCRS["Sphere",DATUM["Sphere","#,
                r#"ELLIPSOID["Sphere",6371229.0,0,LENGTHUNIT["metre",1]]],PRIMEM["Greenwich",0,"#,
                r#"ANGLEUNIT["degree",0.0174532925199433]]],CONVERSION["Lambert Conic Conformal (2SP)","#,
                r#"METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],"#,
                r#"PARAMETER["Latitude of false origin",38.5,ANGLEUNIT["degree",0.0174532925199433]],"#,
                r#"PARAMETER["Longitude of false origin",-97.5,ANGLEUNIT["degree",0.0174532925199433]],"#,
                r#"PARAMETER["Latitude of 1st standard parallel",38.5,ANGLEUNIT["degree",0.0174532925199433]],"#,
                r#"PARAMETER["Latitude of 2nd standard parallel",38.5,ANGLEUNIT["degree",0.0174532925199433]],"#,
                r#"PARAMETER["Easting at false origin",0,LENGTHUNIT["metre",1]],"#,
                r#"PARAMETER["Northing at false origin",0,LENGTHUNIT["metre",1]]],CS[Cartesian,2],"#,
                r#"AXIS["easting",east,ORDER[1],LENGTHUNIT["metre",1]],"#,
                r#"AXIS["northing",north,ORDER[2],LENGTHUNIT["metre",1]]]"#,
            )
        );
    }

    #[test]
    fn a_tier_keeps_the_first_cell_and_a_window_moves_it() {
        let grid = hrrr();
        let tier = grid.decimated();
        assert_eq!((tier.width, tier.height), (900, 530));
        assert_eq!((tier.x0, tier.y0, tier.dx, tier.dy), (grid.x0, grid.y0, 6000.0, -6000.0));
        let window = grid.window(2, 3, 10, 20);
        assert_eq!((window.width, window.height), (20, 10));
        assert_eq!((window.x0, window.y0), (grid.x0 + 9000.0, grid.y0 - 6000.0));
    }

    #[test]
    fn the_block_is_written_in_the_reference_s_order() {
        let block = hrrr().metadata();
        assert_eq!(
            block.keys().map(String::as_str).collect::<Vec<_>>(),
            [
                "kind", "radius", "standardParallel1", "standardParallel2", "latitudeOfOrigin", "centralMeridian",
                "x0", "y0", "dx", "dy",
            ]
        );
    }

    /// A grid-relative `u` of one is the angle `α = n (λ − λ0)` in earth terms.
    #[test]
    fn a_wind_turns_by_the_cone_s_angle() {
        let projection = hrrr().projection;
        let (x, y) = projection.forward(-90.0, 40.0);
        let grid = ProjectedGrid { width: 1, height: 1, x0: x, y0: y, ..hrrr() };
        let (mut u, mut v) = (vec![1.0], vec![0.0]);
        rotate_winds(&grid, &mut u, &mut v).expect("rotated");
        let alpha = projection.meridian_angle(-90.0);
        assert!((u[0] - alpha.cos()).abs() < 1e-9, "{}", u[0]);
        assert!((v[0] + alpha.sin()).abs() < 1e-9, "{}", v[0]);
        assert!(rotate_winds(&grid, &mut [0.0, 0.0], &mut [0.0, 0.0]).is_err());
    }

    /// A southern cone's negative radius cancels its negative `n`, so the same
    /// `α = n (λ − λ0)` comes out.
    #[test]
    fn a_southern_cone_turns_by_its_own_angle() {
        let projection = LambertConformal {
            standard_parallel_1: -30.0,
            standard_parallel_2: -60.0,
            latitude_of_origin: -45.0,
            central_meridian: 140.0,
            ..hrrr().projection
        };
        let (x, y) = projection.forward(150.0, -40.0);
        let grid = ProjectedGrid { projection, width: 1, height: 1, x0: x, y0: y, ..hrrr() };
        let (mut u, mut v) = (vec![1.0], vec![0.0]);
        rotate_winds(&grid, &mut u, &mut v).expect("rotated");
        let alpha = projection.meridian_angle(150.0);
        assert!((u[0] - alpha.cos()).abs() < 1e-9, "{}", u[0]);
        assert!((v[0] + alpha.sin()).abs() < 1e-9, "{}", v[0]);
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
