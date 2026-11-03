//! The physical derivations a conversion runs on extracted planes: the
//! precipitation rate from accumulations, averages and interval totals,
//! Bolton θe, the precipitation type, the vapour flux, the wave vector and
//! the clear-air turbulence index. Each mirrors its namesake in
//! `xuebuild/binconvert.py` operation for operation.

use crate::encode::errors::{EncodeError, Result};
use crate::encode::grid::GridInfo;
use crate::encode::variables::STANDARD_GRAVITY;

/// The clear-air turbulence index on each of its surfaces, `(level, above,
/// below)`: the deformation is taken on the surface itself, the vertical
/// shear across the pair of surfaces that brackets it among the eight
/// registered — centred at 250 hPa, one-sided at the two ends. Mirrors
/// `CAT_SHEAR_LAYERS` in `xuebuild/binconvert.py`.
const CAT_SHEAR_LAYERS: [(u32, u32, u32); 3] = [(300, 250, 300), (250, 200, 300), (200, 200, 250)];

/// `(above, below)`, the surfaces the vertical shear of `cat<level>` is
/// taken across.
fn cat_shear_layer(level: u32) -> Option<(u32, u32)> {
    CAT_SHEAR_LAYERS
        .iter()
        .find(|(at, _, _)| *at == level)
        .map(|&(_, above, below)| (above, below))
}

// -- derived precipitation ---------------------------------------------------

/// Mean precipitation rate (mm/h) over the step ending at the current frame,
/// from run-total accumulations in mm. The first frame has no preceding
/// interval, so its rate is zero; packing noise can make the accumulation dip
/// slightly, so negative differences clamp to zero.
pub fn deaccumulate_precipitation(
    current_mm: &[f64],
    previous_mm: Option<&[f64]>,
    step_hours: i64,
) -> Vec<f64> {
    let Some(previous_mm) = previous_mm else {
        return vec![0.0; current_mm.len()];
    };
    current_mm
        .iter()
        .zip(previous_mm)
        .map(|(current, previous)| (current - previous).max(0.0) / step_hours as f64)
        .collect()
}

/// Mean precipitation rate (mm/h) over the step ending at the current frame,
/// from the total that fell over that step, in mm.
///
/// The third arrival shape of precipitation, and the simplest: Open-Meteo's
/// `precipitation` already describes the interval since the model's previous
/// native output time, so there is nothing to difference against and no
/// predecessor plane to share — one f64 division by the hours that interval
/// spans, which is this frame's lead less the previous frame's on the axis
/// being built (1, 3 or 6 hours on IFS HRES). The operation order around it
/// is the one every source follows (`extract_planes`): the NaN fill first,
/// then unit conversion — none, for a variable already in millimetres — and
/// only then this division. Mirrors `interval_rate` in
/// `xuebuild/binconvert.py`, division for division.
pub fn interval_rate(total_mm: &[f64], step_hours: i64) -> Result<Vec<f64>> {
    if step_hours <= 0 {
        return Err(EncodeError::conversion(format!(
            "an interval precipitation total spans {step_hours} hours"
        )));
    }
    Ok(total_mm.iter().map(|total| total / step_hours as f64).collect())
}

/// First hour of the averaging window whose interval ends at `hour`.
///
/// GFS interval averages reset every `window_hours`: f001–f006 average from
/// hour 0, f007–f012 from hour 6, and so on.
pub fn average_window_start(hour: i64, window_hours: i64) -> Result<i64> {
    if hour <= 0 {
        return Err(EncodeError::conversion(
            "averaged precipitation has no analysis frame",
        ));
    }
    Ok(window_hours * ((hour - 1) / window_hours))
}

/// Mean rate (mm/h) over the step ending at `hour`, from GFS window-cumulative
/// average rates (kg/m^2 s, sflux `PRATE ave`).
///
/// Each frame is scaled to mm accumulated since its window start and
/// differenced against the previous frame of the same window; the window's
/// first frame differences against zero. The rate divisor is the interval the
/// difference spans, so a mixed-step axis needs no external step argument.
pub fn deaverage_precipitation(
    current_average: &[f64],
    hour: i64,
    previous_average: Option<&[f64]>,
    previous_hour: Option<i64>,
    window_hours: i64,
) -> Result<Vec<f64>> {
    let window_start = average_window_start(hour, window_hours)?;
    let accumulated_mm: Vec<f64> = current_average
        .iter()
        .map(|value| value * 3600.0 * (hour - window_start) as f64)
        .collect();
    let mut previous_mm = vec![0.0; accumulated_mm.len()];
    let mut interval_start = window_start;
    if let (Some(previous_average), Some(previous_hour)) = (previous_average, previous_hour) {
        if !(window_start < previous_hour && previous_hour < hour) {
            return Err(EncodeError::conversion(
                "previous averaged frame is outside the current averaging window",
            ));
        }
        previous_mm = previous_average
            .iter()
            .map(|value| value * 3600.0 * (previous_hour - window_start) as f64)
            .collect();
        interval_start = previous_hour;
    }
    Ok(deaccumulate_precipitation(
        &accumulated_mm,
        Some(&previous_mm),
        hour - interval_start,
    ))
}

/// The smallest specific humidity the derivation sees, in kg/kg: a dry
/// stratospheric cell can carry zero, whose vapour pressure has no logarithm.
const THETA_E_MINIMUM_Q: f64 = 1e-7;

/// GRIB2 code table 4.201's codes for the four types pgrb2's categorical
/// flags report, in the order the derivation's inputs list them and are
/// combined in: rain, then freezing rain, then ice pellets, then snow, each
/// later flag overriding an earlier one. Both encoders share this order so
/// even a point the model should never produce encodes byte-identically.
/// Mirrors `PTYPE_CODES` in `xuebuild/binconvert.py`.
const PTYPE_CODES: [f64; 4] = [1.0, 3.0, 8.0, 5.0];

/// The equivalent potential temperature on one isobaric surface, in K, from
/// the temperature already in °C and the specific humidity already in g/kg
/// there: Bolton (1980) eq. 43 with its own lifting-condensation-level
/// temperature (eq. 15) and the dew point inverted from its eq. 10. Every
/// operation runs in the exact order `derive_theta_e` in
/// `xuebuild/binconvert.py` runs it, in f64, which is what keeps the two
/// encoders byte-identical on a field neither reads from a record.
pub fn derive_theta_e(temperature: &[f64], specific_humidity: &[f64], level_hpa: u32) -> Vec<f64> {
    let p = f64::from(level_hpa);
    temperature
        .iter()
        .zip(specific_humidity)
        .map(|(temperature_c, humidity_g)| {
            let t = temperature_c + 273.15;
            let q = (humidity_g / 1000.0).max(THETA_E_MINIMUM_Q);
            let r = q / (1.0 - q);
            let e = p * r / (0.622 + r);
            let ln_e = (e / 6.112).ln();
            let dew_point = 243.5 * ln_e / (17.67 - ln_e) + 273.15;
            let t_lcl = 1.0 / (1.0 / (dew_point - 56.0) + (t / dew_point).ln() / 800.0) + 56.0;
            let theta = t * (1000.0 / p).powf(0.2854 * (1.0 - 0.28 * r));
            let r_g = r * 1000.0;
            theta * ((3.376 / t_lcl - 0.00254) * r_g * (1.0 + 0.00081 * r_g)).exp()
        })
        .collect()
}

/// The categorical precipitation type on the ground, from the four 0/1 flags
/// pgrb2 carries — the derivation's `inputs`, rain, freezing rain, ice
/// pellets and snow in that order: WMO code table 4.201's value for each
/// (1, 3, 8, 5), 0 where none is set. The flags are combined in that order,
/// each setting the code where it is non-zero. Comparisons and selects
/// only, in f64 — exactly what `derive_ptype` in `xuebuild/binconvert.py`
/// does, so the two encoders stay byte-identical on a field neither reads
/// from a record.
pub fn derive_ptype(values: &[(String, Vec<f64>)], inputs: &[String]) -> Result<Vec<f64>> {
    if inputs.len() != PTYPE_CODES.len() {
        return Err(EncodeError::conversion(format!(
            "the precipitation type combines {} flags, not {}",
            PTYPE_CODES.len(),
            inputs.len()
        )));
    }
    let plane = |name: &str| -> Result<&[f64]> {
        values
            .iter()
            .find(|(id, _)| id == name)
            .map(|(_, plane)| plane.as_slice())
            .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for ptype")))
    };
    let mut codes = vec![0.0f64; plane(&inputs[0])?.len()];
    for (variable_id, code) in inputs.iter().zip(PTYPE_CODES) {
        let plane = plane(variable_id)?;
        for (slot, value) in codes.iter_mut().zip(plane) {
            if *value > 0.0 {
                *slot = code;
            }
        }
    }
    Ok(codes)
}

/// The water vapour flux components on one isobaric surface, q·V/g in
/// g·cm⁻¹·hPa⁻¹·s⁻¹, from the specific humidity already in g/kg and the wind
/// in m/s there. One multiplication then one division per component, in this
/// order, in f64 — exactly what `derive_vapour_flux` in
/// `xuebuild/binconvert.py` does, so the two encoders stay byte-identical on
/// a field neither reads from a record.
pub fn derive_vapour_flux(
    specific_humidity: &[f64],
    u_wind: &[f64],
    v_wind: &[f64],
) -> (Vec<f64>, Vec<f64>) {
    let flux = |wind: &[f64]| -> Vec<f64> {
        specific_humidity
            .iter()
            .zip(wind)
            .map(|(q, w)| q * w / STANDARD_GRAVITY)
            .collect()
    };
    (flux(u_wind), flux(v_wind))
}

/// The wave vector: the significant wave height, already in metres, laid
/// along the direction the waves travel, as an eastward and a northward
/// component. The primary direction is degrees true the waves come *from*,
/// the meteorological convention the wind uses, so the components are the
/// wind's `(-h sin θ, -h cos θ)`. Degrees to radians by one multiplication,
/// sine and cosine, the negated height times each, in f64 — the order
/// `derive_wave_vector` in `xuebuild/binconvert.py` runs them in, so the
/// two encoders stay byte-identical on a field neither reads from a record.
pub fn derive_wave_vector(height: &[f64], direction: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let mut u = Vec::with_capacity(height.len());
    let mut v = Vec::with_capacity(height.len());
    for (h, degrees) in height.iter().zip(direction) {
        let radians = degrees * (std::f64::consts::PI / 180.0);
        let negated = -h;
        u.push(negated * radians.sin());
        v.push(negated * radians.cos());
    }
    (u, v)
}

/// The clear-air turbulence derivation's constants, each one literal shared
/// with `xuebuild/binconvert.py`: the mean Earth radius; the latitude past
/// which the zonal spacing shrinks towards nothing and no airway is drawn;
/// the floor that keeps a calm cell's logarithm finite; Sharman & Pearson's
/// (2017) climatological ln EDR mean and standard deviation.
pub const EARTH_RADIUS_M: f64 = 6371000.0;
pub const CAT_POLAR_LATITUDE: f64 = 85.0;
pub const CAT_TI1_FLOOR: f64 = 1e-12;
pub const CAT_EDR_LOG_MEAN: f64 = -2.572;
pub const CAT_EDR_LOG_STD: f64 = 0.5067;
/// The EDR below which a cell is written as no turbulence: the shell draws
/// nothing there, and the range would otherwise carry most of a bundle's
/// entropy. Mirrors `CAT_EDR_NIL` in `xuebuild/binconvert.py`.
pub const CAT_EDR_NIL: f64 = 0.10;

/// `np.maximum(value, floor)` for a finite `floor`: a NaN propagates, which
/// `f64::max` would swallow.
fn numpy_maximum(value: f64, floor: f64) -> f64 {
    if value.is_nan() || value >= floor {
        value
    } else {
        floor
    }
}

/// The `(rows, columns)` planes one clear-air turbulence frame reads, keyed
/// by input id, with the geometry of the whole grid they cover.
pub struct CatPlanes<'a> {
    pub planes: Vec<(&'a str, &'a [f64])>,
    pub rows: usize,
    pub columns: usize,
    /// The rows' centre latitudes in degrees, north first.
    pub latitudes: &'a [f64],
    pub longitude_step: f64,
    pub latitude_step: f64,
    pub wraps: bool,
}

impl CatPlanes<'_> {
    fn plane(&self, variable_id: &str) -> Result<&[f64]> {
        self.planes
            .iter()
            .find(|(id, _)| *id == variable_id)
            .map(|(_, plane)| *plane)
            .ok_or_else(|| EncodeError::conversion(format!("missing {variable_id} plane for the turbulence index")))
    }
}

/// Ellrod & Knapp's (1992) TI1 on one isobaric surface, in s^-2, and the
/// mask of the cells it is defined on: every row but the first and the last
/// whose latitude is within [`CAT_POLAR_LATITUDE`], every column when the
/// grid wraps and every column but the end ones when it does not; elsewhere
/// the index is 0. The planes must be the whole grid the source arrives on,
/// or the differences land on the wrong neighbours.
///
/// Every operation runs in the order `ellrod_ti1` in
/// `xuebuild/binconvert.py` documents, in f64: per row `c = cos(latitude *
/// rad)` and `dx = R * c * (longitude_step * rad)`, `dy = R *
/// (-latitude_step * rad)`, the centred differences over `2 * dx` and
/// `2 * dy` (row r-1 the northern neighbour, columns modulo the width),
/// `DEF = sqrt(DST * DST + DSH * DSH)`, `VWS = sqrt(du * du + dv * dv) /
/// max(dz, 1)`, `TI1 = VWS * DEF`.
pub fn ellrod_ti1(input: &CatPlanes, level: u32) -> Result<(Vec<f64>, Vec<bool>)> {
    let (above, below) = cat_shear_layer(level)
        .ok_or_else(|| EncodeError::conversion(format!("no clear-air turbulence index on {level} hPa")))?;
    let (rows, columns) = (input.rows, input.columns);
    if input.latitude_step >= 0.0 {
        return Err(EncodeError::conversion(
            "the clear-air turbulence index needs a north-to-south grid".to_string(),
        ));
    }
    if rows < 3 || input.latitudes.len() != rows {
        return Err(EncodeError::conversion(format!(
            "cannot difference a {rows}-row plane against {} latitudes",
            input.latitudes.len()
        )));
    }
    let u = input.plane(&format!("ugrd{level}"))?;
    let v = input.plane(&format!("vgrd{level}"))?;
    let u_above = input.plane(&format!("ugrd{above}"))?;
    let v_above = input.plane(&format!("vgrd{above}"))?;
    let z_above = input.plane(&format!("hgt{above}"))?;
    let u_below = input.plane(&format!("ugrd{below}"))?;
    let v_below = input.plane(&format!("vgrd{below}"))?;
    let z_below = input.plane(&format!("hgt{below}"))?;
    let cells = rows * columns;
    for plane in [u, v, u_above, v_above, z_above, u_below, v_below, z_below] {
        if plane.len() != cells {
            return Err(EncodeError::conversion(format!(
                "a {}-point plane is not the {rows} x {columns} grid",
                plane.len()
            )));
        }
    }
    let rad = std::f64::consts::PI / 180.0;
    let two_dx: Vec<f64> = input
        .latitudes
        .iter()
        .map(|latitude| {
            let cosine = (latitude * rad).cos();
            let dx = EARTH_RADIUS_M * cosine * (input.longitude_step * rad);
            2.0 * dx
        })
        .collect();
    let dy = EARTH_RADIUS_M * (-input.latitude_step * rad);
    let two_dy = 2.0 * dy;
    let mut ti1 = vec![0.0f64; cells];
    let mut valid = vec![false; cells];
    for row in 1..rows - 1 {
        if input.latitudes[row].abs() > CAT_POLAR_LATITUDE {
            continue;
        }
        let north = (row - 1) * columns;
        let here = row * columns;
        let south = (row + 1) * columns;
        let (first, last) = if input.wraps { (0, columns) } else { (1, columns.saturating_sub(1)) };
        for column in first..last {
            let east = here + (column + 1) % columns;
            let west = here + (column + columns - 1) % columns;
            let dudx = (u[east] - u[west]) / two_dx[row];
            let dvdx = (v[east] - v[west]) / two_dx[row];
            let dudy = (u[north + column] - u[south + column]) / two_dy;
            let dvdy = (v[north + column] - v[south + column]) / two_dy;
            let dst = dudx - dvdy;
            let dsh = dvdx + dudy;
            let deformation = (dst * dst + dsh * dsh).sqrt();
            let cell = here + column;
            let du = u_above[cell] - u_below[cell];
            let dv = v_above[cell] - v_below[cell];
            let dz = numpy_maximum(z_above[cell] - z_below[cell], 1.0);
            let shear = (du * du + dv * dv).sqrt() / dz;
            ti1[cell] = shear * deformation;
            valid[cell] = true;
        }
    }
    Ok((ti1, valid))
}

/// The clear-air turbulence on one isobaric surface as an eddy dissipation
/// rate in m^(2/3)/s: [`ellrod_ti1`] projected from its own lognormal fit
/// `(mean, std)` of ln TI1 onto the climatological ln EDR distribution.
/// `b = CAT_EDR_LOG_STD / std`, `a = CAT_EDR_LOG_MEAN - b * mean`, then per
/// cell `exp(a + b * ln(max(TI1, CAT_TI1_FLOOR)))`, then 0 where that is
/// strictly below [`CAT_EDR_NIL`] (0.10 itself keeps its code) and 0 where
/// TI1 is undefined — exactly `derive_cat` in `xuebuild/binconvert.py`.
pub fn derive_cat(input: &CatPlanes, level: u32, calibration: (f64, f64)) -> Result<Vec<f64>> {
    let (mean, std) = calibration;
    let (ti1, valid) = ellrod_ti1(input, level)?;
    let b = CAT_EDR_LOG_STD / std;
    let a = CAT_EDR_LOG_MEAN - b * mean;
    Ok(ti1
        .iter()
        .zip(&valid)
        .map(|(value, defined)| {
            if *defined {
                let ln_ti1 = numpy_maximum(*value, CAT_TI1_FLOOR).ln();
                nil_below_threshold((a + b * ln_ti1).exp())
            } else {
                0.0
            }
        })
        .collect())
}

/// 0 for an EDR strictly below [`CAT_EDR_NIL`], the EDR itself otherwise —
/// 0.10 included, and a NaN, as `np.where(edr < CAT_EDR_NIL, 0.0, edr)`
/// keeps it.
fn nil_below_threshold(edr: f64) -> f64 {
    if edr < CAT_EDR_NIL {
        0.0
    } else {
        edr
    }
}

/// `(rows, columns)` of a variable's plane before its crop window is cut.
/// Mirrors `uncropped_shape` in `xuebuild/binconvert.py`.
pub fn uncropped_shape(grid: &GridInfo) -> (usize, usize) {
    match grid.crop {
        None => (grid.height, grid.width),
        Some(crop) => (crop.source_height, crop.source_width),
    }
}

/// The centre latitude of every row of the uncropped plane, in degrees:
/// `first_latitude + (row - crop_row_start) * latitude_step`. Mirrors
/// `uncropped_latitudes` in `xuebuild/binconvert.py`.
pub fn uncropped_latitudes(grid: &GridInfo) -> Vec<f64> {
    let (rows, _) = uncropped_shape(grid);
    let row_start = grid.crop.map_or(0, |crop| crop.row_start) as f64;
    (0..rows)
        .map(|row| grid.first_latitude + (row as f64 - row_start) * grid.latitude_step)
        .collect()
}

/// Whether the uncropped plane's columns run all the way round. Mirrors
/// `uncropped_wraps` in `xuebuild/binconvert.py`.
pub fn uncropped_wraps(grid: &GridInfo) -> bool {
    let (_, columns) = uncropped_shape(grid);
    (columns as f64 * grid.longitude_step - 360.0).abs() < 1e-6
}

/// One `cat<level>` frame on the published (cropped) grid, derived on the
/// uncropped planes and only then cut to the window. `plane` answers each
/// input's uncropped plane. Mirrors `_derive_cat_plane` in
/// `xuebuild/binconvert.py`.
pub(super) fn derive_cat_plane<'a>(
    plane: impl Fn(&str) -> Result<&'a [f64]>,
    bundle_id: &str,
    level: u32,
    inputs: &[String],
    grid: &GridInfo,
    calibration: &[(u32, f64, f64)],
) -> Result<Vec<f64>> {
    let &(_, mean, std) = calibration.iter().find(|(at, _, _)| *at == level).ok_or_else(|| {
        EncodeError::conversion(format!(
            "the source publishes {bundle_id} without a ln TI1 calibration for {level} hPa"
        ))
    })?;
    let (rows, columns) = uncropped_shape(grid);
    let latitudes = uncropped_latitudes(grid);
    let planes = inputs
        .iter()
        .map(|id| Ok((id.as_str(), plane(id)?)))
        .collect::<Result<Vec<_>>>()?;
    let edr = derive_cat(
        &CatPlanes {
            planes,
            rows,
            columns,
            latitudes: &latitudes,
            longitude_step: grid.longitude_step,
            latitude_step: grid.latitude_step,
            wraps: uncropped_wraps(grid),
        },
        level,
        (mean, std),
    )?;
    Ok(match grid.crop {
        Some(crop) => crop.take(&edr),
        None => edr,
    })
}

#[cfg(test)]
mod tests {
    use super::interval_rate;

    /// `prate` from an interval total: one division, whatever the step.
    /// Mirrors `IntervalRateTests` in `tests/test_ifshres.py`.
    #[test]
    fn the_interval_rate_is_the_total_over_the_hours_it_covers() {
        let total = [0.0, 1.5, 12.0];
        assert_eq!(interval_rate(&total, 1).expect("an hour"), vec![0.0, 1.5, 12.0]);
        assert_eq!(interval_rate(&total, 3).expect("three hours"), vec![0.0, 0.5, 4.0]);
        assert_eq!(interval_rate(&total, 6).expect("six hours"), vec![0.0, 0.25, 2.0]);
    }

    #[test]
    fn an_interval_of_no_hours_is_a_conversion_error() {
        for step in [0, -3] {
            let error = interval_rate(&[0.0; 4], step).expect_err("no interval");
            assert!(error.to_string().contains("spans"), "{error}");
        }
    }
}

/// The clear-air turbulence bundles, the analytic cases of
/// `tests/test_cat.py`: centred differences on the sphere, the wrapping
/// longitude, the rows the index is not defined on, the projection onto EDR
/// and a crop cut after the derivation.
#[cfg(test)]
mod cat_tests {
    use super::{
        derive_cat, derive_cat_plane, ellrod_ti1, nil_below_threshold, numpy_maximum, uncropped_latitudes,
        CatPlanes, CAT_EDR_LOG_MEAN, CAT_TI1_FLOOR, CAT_EDR_LOG_STD, CAT_EDR_NIL, CAT_SHEAR_LAYERS,
        EARTH_RADIUS_M,
    };
    use crate::encode::errors::{EncodeError, Result};
    use crate::encode::grid::{CropWindow, GridInfo};
    use crate::encode::registry::{DerivationKind, Registry};
    use serde_json::json;

    /// The inputs the document lists for one turbulence bundle.
    fn cat_inputs(bundle_id: &str) -> Vec<String> {
        Registry::embedded().scalar_derivation(bundle_id).unwrap().inputs.clone()
    }

    const RAD: f64 = std::f64::consts::PI / 180.0;

    /// The inputs of `cat<level>`: `(u, v)` on the surface and below, the
    /// wind above 10 m/s stronger eastward and the height above 1000 m
    /// higher — a vertical shear of exactly 0.01 s^-1 everywhere.
    fn planes(level: u32, cells: usize, u: &[f64], v: &[f64]) -> Vec<(String, Vec<f64>)> {
        let &(_, above, below) = CAT_SHEAR_LAYERS.iter().find(|(at, _, _)| *at == level).unwrap();
        let z = vec![9000.0; cells];
        let mut out = vec![
            (format!("ugrd{below}"), u.to_vec()),
            (format!("vgrd{below}"), v.to_vec()),
            (format!("hgt{below}"), z.clone()),
            (format!("ugrd{above}"), u.iter().map(|x| x + 10.0).collect()),
            (format!("vgrd{above}"), v.to_vec()),
            (format!("hgt{above}"), z.iter().map(|x| x + 1000.0).collect()),
        ];
        for (id, plane) in [(format!("ugrd{level}"), u), (format!("vgrd{level}"), v)] {
            if !out.iter().any(|(name, _)| *name == id) {
                out.push((id, plane.to_vec()));
            }
        }
        out
    }

    fn set(planes: &mut [(String, Vec<f64>)], id: &str, plane: Vec<f64>) {
        planes.iter_mut().find(|(name, _)| name == id).unwrap().1 = plane;
    }

    fn input<'a>(
        planes: &'a [(String, Vec<f64>)],
        rows: usize,
        columns: usize,
        latitudes: &'a [f64],
        longitude_step: f64,
        latitude_step: f64,
        wraps: bool,
    ) -> CatPlanes<'a> {
        CatPlanes {
            planes: planes.iter().map(|(id, plane)| (id.as_str(), plane.as_slice())).collect(),
            rows,
            columns,
            latitudes,
            longitude_step,
            latitude_step,
            wraps,
        }
    }

    const ROWS: usize = 9;
    const COLUMNS: usize = 12;

    fn latitudes() -> Vec<f64> {
        (0..ROWS).map(|row| 4.0 - 1.0 * row as f64).collect()
    }

    fn close(actual: f64, expected: f64, rtol: f64) -> bool {
        (actual - expected).abs() <= rtol * expected.abs()
    }

    /// A deterministic stand-in for the Python case's normal draws.
    fn noise(seed: u64, cells: usize) -> Vec<f64> {
        let mut state = seed;
        (0..cells)
            .map(|_| {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                ((state >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
            })
            .collect()
    }

    #[test]
    fn pure_stretching_is_the_zonal_gradient_over_dx() {
        let latitudes = latitudes();
        let u: Vec<f64> = (0..ROWS * COLUMNS).map(|cell| 2.0 * (cell % COLUMNS) as f64).collect();
        let v = vec![0.0; ROWS * COLUMNS];
        let planes = planes(300, ROWS * COLUMNS, &u, &v);
        let (ti1, valid) =
            ellrod_ti1(&input(&planes, ROWS, COLUMNS, &latitudes, 1.0, -1.0, false), 300).unwrap();
        for row in 1..ROWS - 1 {
            let dx = EARTH_RADIUS_M * (latitudes[row] * RAD).cos() * (1.0 * RAD);
            for column in 1..COLUMNS - 1 {
                assert!(close(ti1[row * COLUMNS + column], 0.01 * (2.0 / dx), 1e-12), "{row} {column}");
            }
            // Not wrapping: the end columns have no centred difference.
            assert!(!valid[row * COLUMNS] && !valid[row * COLUMNS + COLUMNS - 1]);
        }
        assert_eq!(ti1[4 * COLUMNS], 0.0);
        assert!(!valid[..COLUMNS].iter().any(|v| *v) && !valid[(ROWS - 1) * COLUMNS..].iter().any(|v| *v));
    }

    #[test]
    fn pure_shear_is_the_meridional_gradient_over_dy() {
        let latitudes = latitudes();
        // The eastward wind grows northward.
        let u: Vec<f64> = (0..ROWS * COLUMNS).map(|cell| -3.0 * (cell / COLUMNS) as f64).collect();
        let v = vec![0.0; ROWS * COLUMNS];
        let planes = planes(250, ROWS * COLUMNS, &u, &v);
        let (ti1, _) = ellrod_ti1(&input(&planes, ROWS, COLUMNS, &latitudes, 1.0, -1.0, false), 250).unwrap();
        let dy = EARTH_RADIUS_M * (1.0 * RAD);
        for row in 1..ROWS - 1 {
            for column in 1..COLUMNS - 1 {
                assert!(close(ti1[row * COLUMNS + column], 0.01 * 3.0 / dy, 1e-12), "{row} {column}");
            }
        }
    }

    #[test]
    fn the_vertical_shear_divides_by_the_layer_depth() {
        let latitudes = latitudes();
        let u: Vec<f64> = (0..ROWS * COLUMNS).map(|cell| (cell % COLUMNS) as f64).collect();
        let zero = vec![0.0; ROWS * COLUMNS];
        let mut planes = planes(200, ROWS * COLUMNS, &u, &zero);
        set(&mut planes, "vgrd200", vec![30.0; ROWS * COLUMNS]); // |dV| = hypot(10, 30)
        set(&mut planes, "hgt200", vec![11000.0; ROWS * COLUMNS]);
        let (ti1, _) = ellrod_ti1(&input(&planes, ROWS, COLUMNS, &latitudes, 1.0, -1.0, false), 200).unwrap();
        let dx = EARTH_RADIUS_M * 0.0f64.cos() * RAD;
        let expected = (10.0f64 * 10.0 + 30.0 * 30.0).sqrt() / 2000.0 * (1.0 / dx);
        assert!((ti1[4 * COLUMNS + 5] - expected).abs() <= 1e-20);
    }

    #[test]
    fn the_first_and_last_columns_are_neighbours_on_a_wrapping_grid() {
        let latitudes = latitudes();
        let u = noise(32, ROWS * COLUMNS);
        let v = noise(33, ROWS * COLUMNS);
        let planes = planes(300, ROWS * COLUMNS, &u, &v);
        let (ti1, valid) =
            ellrod_ti1(&input(&planes, ROWS, COLUMNS, &latitudes, 30.0, -1.0, true), 300).unwrap();
        assert!(valid[COLUMNS..(ROWS - 1) * COLUMNS].iter().all(|v| *v));
        let r = 3;
        let at = |plane: &[f64], row: usize, column: usize| plane[row * COLUMNS + column];
        let two_dx = 2.0 * (EARTH_RADIUS_M * (latitudes[r] * RAD).cos() * (30.0 * RAD));
        let two_dy = 2.0 * (EARTH_RADIUS_M * (1.0 * RAD));
        let dudx = (at(&u, r, 1) - at(&u, r, COLUMNS - 1)) / two_dx;
        let dvdx = (at(&v, r, 1) - at(&v, r, COLUMNS - 1)) / two_dx;
        let dudy = (at(&u, r - 1, 0) - at(&u, r + 1, 0)) / two_dy;
        let dvdy = (at(&v, r - 1, 0) - at(&v, r + 1, 0)) / two_dy;
        let (dst, dsh) = (dudx - dvdy, dvdx + dudy);
        // Bit-exact: the documented operation order is the contract. The
        // shear is sqrt(10 * 10) / 1000, exactly 0.01.
        assert_eq!(at(&ti1, r, 0), 0.01 * (dst * dst + dsh * dsh).sqrt());
    }

    #[test]
    fn the_polar_rows_are_not_computed() {
        let latitudes: Vec<f64> = (0..8).map(|row| 88.0 - 1.0 * row as f64).collect();
        let u: Vec<f64> = (0..8 * 12).map(|cell| (cell % 12) as f64).collect();
        let zero = vec![0.0; 8 * 12];
        let planes = planes(300, 8 * 12, &u, &zero);
        let (ti1, valid) = ellrod_ti1(&input(&planes, 8, 12, &latitudes, 30.0, -1.0, true), 300).unwrap();
        let column: Vec<bool> = (0..8).map(|row| valid[row * 12 + 5]).collect();
        assert_eq!(column, [false, false, false, true, true, true, true, false]);
        assert!(ti1[..3 * 12].iter().all(|value| *value == 0.0));
    }

    #[test]
    fn a_south_to_north_grid_is_refused() {
        let latitudes: Vec<f64> = latitudes().into_iter().rev().collect();
        let zero = vec![0.0; ROWS * COLUMNS];
        let planes = planes(300, ROWS * COLUMNS, &zero, &zero);
        let refused = ellrod_ti1(&input(&planes, ROWS, COLUMNS, &latitudes, 1.0, 1.0, true), 300);
        assert!(matches!(refused, Err(EncodeError(_))));
    }

    const PROJECTION_ROWS: usize = 5;
    const PROJECTION_COLUMNS: usize = 8;
    const PROJECTION_LATITUDES: [f64; 5] = [2.0, 1.0, 0.0, -1.0, -2.0];
    const PROJECTION_CELL: usize = 2 * PROJECTION_COLUMNS + 3;

    /// A field whose TI1 is one value on every defined cell.
    fn projection_field() -> Vec<(String, Vec<f64>)> {
        let cells = PROJECTION_ROWS * PROJECTION_COLUMNS;
        let u: Vec<f64> = (0..cells).map(|cell| (cell % PROJECTION_COLUMNS) as f64).collect();
        planes(300, cells, &u, &vec![0.0; cells])
    }

    fn projection_input(planes: &[(String, Vec<f64>)]) -> CatPlanes<'_> {
        input(planes, PROJECTION_ROWS, PROJECTION_COLUMNS, &PROJECTION_LATITUDES, 1.0, -1.0, false)
    }

    #[test]
    fn edr_is_the_lognormal_projection_of_ti1() {
        let field = projection_field();
        let grid = projection_input(&field);
        let (ti1, valid) = ellrod_ti1(&grid, 300).unwrap();
        let ti1_cell = ti1[PROJECTION_CELL];
        let std = 1.2;
        // Two standard deviations above the fit's mean: moderate, kept.
        let mean = ti1_cell.ln() - 2.0 * std;
        let edr = derive_cat(&grid, 300, (mean, std)).unwrap();
        let b = CAT_EDR_LOG_STD / std;
        let a = CAT_EDR_LOG_MEAN - b * mean;
        assert_eq!(edr[PROJECTION_CELL], (a + b * ti1_cell.ln()).exp());
        assert!((edr[PROJECTION_CELL] - (CAT_EDR_LOG_MEAN + 2.0 * CAT_EDR_LOG_STD).exp()).abs() < 1e-7);
        assert!(edr.iter().zip(&valid).all(|(value, defined)| *defined || *value == 0.0));
        // Calm air floors rather than taking the logarithm of zero, and
        // floors to nothing.
        let cells = PROJECTION_ROWS * PROJECTION_COLUMNS;
        let calm_planes = self::planes(300, cells, &vec![0.0; cells], &vec![0.0; cells]);
        let calm = derive_cat(&projection_input(&calm_planes), 300, (mean, std)).unwrap();
        assert!(calm.iter().all(|value| *value == 0.0));
    }

    #[test]
    fn nil_turbulence_is_written_as_zero() {
        let field = projection_field();
        let grid = projection_input(&field);
        let (ti1, _) = ellrod_ti1(&grid, 300).unwrap();
        // At the fit's mean the EDR is the climatological median, 0.076: nil.
        assert!(CAT_EDR_LOG_MEAN.exp() < CAT_EDR_NIL);
        let at_mean = derive_cat(&grid, 300, (ti1[PROJECTION_CELL].ln(), 1.2)).unwrap();
        assert!(at_mean.iter().all(|value| *value == 0.0));
        // The threshold itself, as `tests/test_cat.py` holds it: with
        // std = CAT_EDR_LOG_STD and mean = CAT_EDR_LOG_MEAN the projection is
        // exp(ln TI1). exp never returns 0.10 exactly, so the TI1 values tried
        // are those whose EDR is the nearest double at or above 0.10 (kept,
        // code 20) and the nearest one below it (nil).
        let projected = |t: f64| t.ln().exp();
        let mut lowest_kept = CAT_EDR_NIL;
        while projected(lowest_kept) >= CAT_EDR_NIL {
            lowest_kept = lowest_kept.next_down();
        }
        let highest_nil = lowest_kept;
        while projected(lowest_kept) < CAT_EDR_NIL {
            lowest_kept = lowest_kept.next_up();
        }
        assert!(projected(lowest_kept) - CAT_EDR_NIL < 1e-16);
        assert!(CAT_EDR_NIL - projected(highest_nil) < 1e-16);
        // derive_cat's per-cell step with that calibration (b = 1, a = 0).
        let project = |t: f64| {
            let b = CAT_EDR_LOG_STD / CAT_EDR_LOG_STD;
            let a = CAT_EDR_LOG_MEAN - b * CAT_EDR_LOG_MEAN;
            nil_below_threshold((a + b * numpy_maximum(t, CAT_TI1_FLOOR).ln()).exp())
        };
        assert_eq!(project(highest_nil), 0.0);
        assert_eq!(project(lowest_kept), projected(lowest_kept));
        assert!((project(0.3) - 0.3).abs() < 1e-12);
        let mut codes = [0u8; 1];
        let book = Registry::embedded().codebook("quality", "cat300").unwrap();
        book.quantize(&[project(lowest_kept)], &mut codes).unwrap();
        assert_eq!(codes[0], 20);
        // 0.10 itself, had it come out of exp, keeps its value and code 20.
        assert_eq!(nil_below_threshold(CAT_EDR_NIL), CAT_EDR_NIL);
        assert_eq!(nil_below_threshold(CAT_EDR_NIL.next_down()), 0.0);
        book.quantize(&[nil_below_threshold(CAT_EDR_NIL)], &mut codes).unwrap();
        assert_eq!(codes[0], 20);
        assert!(nil_below_threshold(f64::NAN).is_nan());
    }

    /// numpy's semantics: `NaN < 0.10` is false, so a NaN TI1 stays NaN
    /// rather than being clamped to nothing.
    #[test]
    fn a_nan_is_kept_as_numpy_keeps_it() {
        let mut field = projection_field();
        let mut z = vec![10000.0; PROJECTION_ROWS * PROJECTION_COLUMNS];
        z[PROJECTION_CELL] = f64::NAN;
        set(&mut field, "hgt250", z);
        let edr = derive_cat(&projection_input(&field), 300, (-16.0, 1.2)).unwrap();
        assert!(edr[PROJECTION_CELL].is_nan());
        assert!(!edr[PROJECTION_CELL + 1].is_nan());
    }

    #[test]
    fn a_crop_is_cut_after_the_derivation() {
        let (rows, columns) = (13, 24);
        let grid = GridInfo::new(columns, rows, -180.0, 60.0, 15.0, -10.0);
        let inputs = cat_inputs("cat250");
        let mut planes: Vec<(String, Vec<f64>)> = inputs
            .iter()
            .cloned()
            .enumerate()
            .map(|(index, id)| (id, noise(7 + index as u64, rows * columns).iter().map(|x| x * 20.0).collect()))
            .collect();
        for (id, offset) in [("hgt200", 12000.0), ("hgt300", 9000.0)] {
            let plane = planes.iter().find(|(name, _)| name == id).unwrap().1.iter().map(|x| x + offset).collect();
            set(&mut planes, id, plane);
        }
        let calibration = [(250, -17.0, 1.1)];
        let lookup = |name: &str| -> Result<&[f64]> {
            Ok(planes.iter().find(|(id, _)| id == name).map(|(_, plane)| plane.as_slice()).unwrap())
        };
        let whole = derive_cat_plane(lookup, "cat250", 250, &inputs, &grid, &calibration).unwrap();
        // A window across the antimeridian, touching the grid's first row.
        let crop = CropWindow {
            source_width: columns,
            source_height: rows,
            row_start: 0,
            column_start: 20,
            width: 8,
            height: 5,
        };
        let mut cropped_grid = GridInfo::new(8, 5, 120.0, 60.0, 15.0, -10.0);
        cropped_grid.crop = Some(crop);
        assert_eq!(uncropped_latitudes(&cropped_grid), uncropped_latitudes(&grid));
        let cut = derive_cat_plane(lookup, "cat250", 250, &inputs, &cropped_grid, &calibration).unwrap();
        assert_eq!(cut, crop.take(&whole));
        assert!(derive_cat_plane(lookup, "cat250", 250, &inputs, &grid, &[(300, -17.0, 1.1)]).is_err());
    }

    /// The document's inputs are the shear layer's: the wind and height
    /// above, then below, then the surface's own wind where that is not
    /// one end of the layer — the order `ellrod_ti1` reads them in.
    #[test]
    fn the_bundles_and_their_inputs() {
        let registry = Registry::embedded();
        assert_eq!(
            cat_inputs("cat250"),
            ["ugrd200", "vgrd200", "hgt200", "ugrd300", "vgrd300", "hgt300", "ugrd250", "vgrd250"]
        );
        assert_eq!(
            cat_inputs("cat300"),
            ["ugrd250", "vgrd250", "hgt250", "ugrd300", "vgrd300", "hgt300"]
        );
        for &(level, above, below) in &CAT_SHEAR_LAYERS {
            let derivation = registry.scalar_derivation(&format!("cat{level}")).unwrap();
            assert_eq!((derivation.kind, derivation.level_hpa), (DerivationKind::ClearAirTurbulence, Some(level)));
            for surface in [above, below, level] {
                for family in ["ugrd", "vgrd"] {
                    assert!(derivation.inputs.contains(&format!("{family}{surface}")), "cat{level} {family}{surface}");
                }
            }
        }
        assert!(registry.scalar_derivation("cape").is_none());
        assert!(registry.scalar_derivation("cat850").is_none());
        let spec = registry.variable("cat300").unwrap();
        assert_eq!(spec.parameter_metadata()["parameterCategory"], json!(19));
        assert_eq!(spec.parameter_metadata()["parameterNumber"], json!(29));
        assert_eq!(registry.codebook("quality", "cat300").unwrap().metadata()["maximumCode"], json!(127));
        for id in ["cat300", "cat250", "cat200"] {
            assert!(registry.is_raw(id).unwrap(), "{id}");
        }
    }

    #[test]
    fn gfs_and_ecmwf_publish_and_calibrate_all_three() {
        let registry = Registry::embedded();
        for model in ["gfs", "ecmwf", "ifs"] {
            let source = registry.source(model).unwrap();
            let published: Vec<&str> = registry
                .published_bundle_ids(source)
                .into_iter()
                .filter(|id| id.starts_with("cat"))
                .collect();
            assert_eq!(published, ["cat300", "cat250", "cat200"], "{model}");
            let levels: Vec<u32> = source.cat_calibration.iter().map(|(level, _, _)| *level).collect();
            assert_eq!(levels, [300, 250, 200], "{model}");
            for bundle_id in ["cat300", "cat250", "cat200"] {
                for input_id in registry.bundle_input_ids(source, bundle_id) {
                    assert!(source.has_input(&input_id), "{model} {bundle_id} {input_id}");
                }
            }
        }
        let aifs = registry.source("aifs").unwrap();
        assert!(!registry.published_bundle_ids(aifs).iter().any(|id| id.starts_with("cat")));
    }
}
