//! Build Xue v1 bundles from gridded input — the port of `xuebuild/binconvert.py`.
//!
//! Each variable is packaged into its own single-variable `.xue` file so the
//! frontend can download exactly the fields it needs. A forecast source
//! arrives as one GRIB2 file per forecast hour; an observation source
//! (`observation.rs`) as one NetCDF file whose bands are the time axis.
//! Everything past frame discovery — crop, quantize, temporal grouping,
//! container write, manifest — is the same for both.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};

use serde_json::{json, Map, Value};
use time::OffsetDateTime;

use crate::encode::binformat::{self, ChunkPayload, ChunkTables, HOUR_SECONDS};
use crate::format::{ChunkEntry, Predictor, TileGeometry, VariableEntry, NO_DEPENDENCY};
use crate::encode::errors::{EncodeError, Result};
use crate::encode::variables::{
    isobaric_variable, variable_spec, ISOBARIC_LEVELS_HPA, STANDARD_GRAVITY, WAVE_VECTOR_COMPONENT_IDS,
};
use crate::encode::gdalio::{needs_serial_access, netcdf_guard, Dataset};
use crate::encode::grid::{
    crop_grid, normalize_longitudes, snap_global_longitudes, snap_regional_steps, GridInfo,
};
use crate::encode::gribindex::inspect_grib_fast;
use crate::encode::inspect::{
    inspect_grib_multi, normalize_unit, precipitation_rate_is_mm_per_hour, raster_expression,
    SUPPORTED_EXTENSIONS,
};
use crate::encode::manifest::{build_bin_manifest, build_latest_pointer, serialize_json, write_json};
use crate::encode::metadata::{axis_unit_seconds, build_metadata, lead_hours, to_spaced_json};
use crate::encode::model::{PlaneSource, SourceFrame};
use crate::encode::observation::inspect_observation;
use crate::encode::parallel::for_each_ordered;
use crate::encode::poster::encode_poster;
use crate::encode::quantize::{codebook, Codebook};
use crate::encode::reproject::{build_resampler, lambert_conformal_from_wkt, ProjectedGrid};
use crate::encode::sources::{source_spec, SourceSpec};
use crate::encode::temporal::build_chunks;

/// Scalar variables ship one single-variable bundle each; the two wind
/// components ship together in one two-variable bundle for the GPU particle
/// layer — as does the wind on each isobaric surface, the water vapour
/// flux the converter derives there, and the wave vector it derives from
/// the wave height and direction. Mirrors `VECTOR_BUNDLES` in
/// `xuebuild/binconvert.py`.
pub const WIND_COMPONENT_IDS: [&str; 2] = ["ugrd10m", "vgrd10m"];
pub const WIND_BUNDLE_ID: &str = "wind10m";
pub const WAVE_BUNDLE_ID: &str = "wave";
/// The inputs the wave vector is derived from: the significant wave height
/// and the primary wave direction. Mirrors `DERIVED_VECTORS["wave"]` in
/// `xuebuild/binconvert.py`.
const WAVE_INPUT_IDS: [&str; 2] = ["htsgw", "dirpw"];

/// The two component variables of a vector bundle, or `None` for a scalar.
pub fn vector_components(bundle_id: &str) -> Option<(String, String)> {
    if bundle_id == WIND_BUNDLE_ID {
        return Some((WIND_COMPONENT_IDS[0].into(), WIND_COMPONENT_IDS[1].into()));
    }
    if bundle_id == WAVE_BUNDLE_ID {
        return Some((WAVE_VECTOR_COMPONENT_IDS[0].into(), WAVE_VECTOR_COMPONENT_IDS[1].into()));
    }
    for (prefix, u, v) in [("wind", "ugrd", "vgrd"), ("qflux", "uqflx", "vqflx")] {
        if let Some(level) = bundle_id.strip_prefix(prefix) {
            if let Ok(level) = level.parse::<u32>() {
                if ISOBARIC_LEVELS_HPA.contains(&level) {
                    return Some((format!("{u}{level}"), format!("{v}{level}")));
                }
            }
        }
    }
    None
}

/// The isobaric surface of a `thetae<level>` bundle — the equivalent
/// potential temperature the converter derives from the temperature and the
/// specific humidity there — or `None`. Mirrors `DERIVED_SCALARS` /
/// `theta_e_level` in `xuebuild/binconvert.py`.
pub fn theta_e_level(bundle_id: &str) -> Option<u32> {
    let level: u32 = bundle_id.strip_prefix("thetae")?.parse().ok()?;
    ISOBARIC_LEVELS_HPA.contains(&level).then_some(level)
}

/// The published scalars of `scalar_ids` whose series has no analysis frame:
/// the precipitation rate a source derives by de-accumulating or
/// de-averaging, and any scalar read from a record the source lists under
/// `optional_at_analysis`. Mirrors `analysis_optional_ids` in
/// `xuebuild/binconvert.py`.
pub fn analysis_optional_ids<'a>(source: &SourceSpec, scalar_ids: &[&'a str]) -> Vec<&'a str> {
    scalar_ids
        .iter()
        .copied()
        .filter(|variable_id| {
            (*variable_id == "prate"
                && (source.accumulated_precipitation || source.averaged_precipitation))
                || bundle_input_ids(source, variable_id)
                    .iter()
                    .any(|input_id| source.optional_at_analysis.contains(&input_id.as_str()))
        })
        .collect()
}

/// The source inputs a derived scalar is built from, or `None` for a scalar
/// read from a record.
pub fn derived_scalar_inputs(bundle_id: &str) -> Option<Vec<String>> {
    let level = theta_e_level(bundle_id)?;
    Some(vec![format!("tmp{level}"), format!("spfh{level}")])
}

/// The isobaric surface of a `qflux<level>` bundle, or `None`.
pub fn vapour_flux_level(bundle_id: &str) -> Option<u32> {
    let level: u32 = bundle_id.strip_prefix("qflux")?.parse().ok()?;
    ISOBARIC_LEVELS_HPA.contains(&level).then_some(level)
}

/// Whether a vector bundle is derived rather than read: a vapour flux pair
/// or the wave vector. A wind pair is its own two components. Mirrors
/// `DERIVED_VECTORS` in `xuebuild/binconvert.py`.
pub fn is_derived_vector(bundle_id: &str) -> bool {
    bundle_id == WAVE_BUNDLE_ID || vapour_flux_level(bundle_id).is_some()
}

/// The source inputs one vector bundle is built from: a wind pair is its own
/// two components; a vapour flux pair is derived from the specific humidity
/// and both wind components on the same surface; the wave vector from the
/// significant wave height and the primary wave direction.
pub fn vector_input_ids(bundle_id: &str) -> Vec<String> {
    if let Some(level) = vapour_flux_level(bundle_id) {
        return vec![format!("spfh{level}"), format!("ugrd{level}"), format!("vgrd{level}")];
    }
    if bundle_id == WAVE_BUNDLE_ID {
        return WAVE_INPUT_IDS.iter().map(|id| (*id).to_string()).collect();
    }
    match vector_components(bundle_id) {
        Some((u, v)) => vec![u, v],
        None => vec![bundle_id.to_string()],
    }
}
/// Linear-codebook fields are smooth enough for the six-frame ANCHOR groups;
/// precipitation stays independent RAW planes.
/// Precipitation and radar reflectivity move with weather systems, so
/// temporal differencing makes them larger, not smaller: their chunks stack
/// the codes RAW. Every linear-codebook field chains against the previous
/// frame inside its chunk. Mirrors `RAW_VARIABLE_IDS` in
/// `xuebuild/binconvert.py`.
const RAW_VARIABLE_IDS: [&str; 2] = ["prate", "cref"];
/// The pressure family: mean sea level pressure and the isobaric geopotential
/// heights. The frontend draws them as contour lines, so they ship bundles
/// only — no poster (it would paint a filled field the view never shows) and
/// no H.264 companion. Mirrors `PRESSURE_BUNDLE_IDS` in
/// `xuebuild/binconvert.py`.
const PRESSURE_BUNDLE_IDS: [&str; 9] = [
    "prmsl", "hgt1000", "hgt925", "hgt850", "hgt700", "hgt500", "hgt300", "hgt250", "hgt200",
];
/// Every source names its precipitation input differently.
const PRECIPITATION_INPUT_IDS: [&str; 3] = ["prate", "tp", "prate_ave"];
/// The raw precipitation inputs a frame differences against its predecessor.
const DERIVED_PRECIPITATION_IDS: [&str; 2] = ["tp", "prate_ave"];

pub const DEFAULT_ZSTD_LEVEL: i32 = 15;

/// One file's frames, keyed by variable id, in the source's own order.
type FileFrames = Vec<(String, SourceFrame)>;

fn frame_of<'a>(frames: &'a FileFrames, variable_id: &str) -> Option<&'a SourceFrame> {
    frames
        .iter()
        .find(|(id, _)| id == variable_id)
        .map(|(_, frame)| frame)
}

#[derive(Debug, Clone)]
pub struct ConvertOptions {
    pub profile: String,
    pub zstd_level: i32,
    pub require_complete: bool,
    pub expected_hours: i64,
    pub manifest_path: Option<PathBuf>,
    pub latest_path: Option<PathBuf>,
    pub run_id: Option<String>,
    pub force: bool,
    pub skip_variants: bool,
    pub model: String,
    pub bbox: Option<(f64, f64, f64, f64)>,
    pub bundle_ids: Option<Vec<String>>,
    pub last_hour: Option<i64>,
    pub extract_workers: usize,
    pub compress_workers: usize,
    pub verbose: bool,
}

impl Default for ConvertOptions {
    fn default() -> Self {
        let cpus = std::thread::available_parallelism().map_or(4, std::num::NonZeroUsize::get);
        Self {
            profile: "quality".into(),
            zstd_level: DEFAULT_ZSTD_LEVEL,
            require_complete: false,
            expected_hours: 120,
            manifest_path: None,
            latest_path: None,
            run_id: None,
            force: false,
            skip_variants: false,
            model: "gfs".into(),
            bbox: None,
            bundle_ids: None,
            last_hour: None,
            // GRIB unpacking is CPU-bound: one worker per core.
            extract_workers: cpus.min(16),
            compress_workers: cpus,
            verbose: false,
        }
    }
}

macro_rules! log {
    ($options:expr, $($argument:tt)*) => {
        if $options.verbose {
            eprintln!("INFO {}", format!($($argument)*));
        }
    };
}

// -- input discovery ---------------------------------------------------------

/// The GRIB files a conversion reads: one file, every file in a directory, or
/// exactly the files given.
pub fn discover_inputs(inputs: &[PathBuf]) -> Result<Vec<PathBuf>> {
    if inputs.is_empty() {
        return Err(EncodeError::conversion("no GRIB files given"));
    }
    if inputs.len() == 1 {
        let input = &inputs[0];
        if input.is_file() {
            return Ok(vec![input.clone()]);
        }
        if !input.is_dir() {
            return Err(EncodeError::conversion(format!(
                "input does not exist: {}",
                input.display()
            )));
        }
        let mut files: Vec<PathBuf> = std::fs::read_dir(input)
            .map_err(|error| {
                EncodeError::conversion(format!("cannot list {}: {error}", input.display()))
            })?
            .filter_map(std::result::Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.is_file()
                    && path.extension().is_some_and(|extension| {
                        SUPPORTED_EXTENSIONS
                            .contains(&extension.to_string_lossy().to_lowercase().as_str())
                    })
            })
            .collect();
        files.sort();
        if files.is_empty() {
            return Err(EncodeError::conversion(format!(
                "no GRIB files found in {}",
                input.display()
            )));
        }
        return Ok(files);
    }
    let mut files = inputs.to_vec();
    files.sort();
    if let Some(missing) = files.iter().find(|path| !path.is_file()) {
        return Err(EncodeError::conversion(format!(
            "input does not exist: {}",
            missing.display()
        )));
    }
    Ok(files)
}

/// The source input variables one published bundle is built from.
///
/// Every source names its precipitation input differently (GFS `prate`, ECMWF
/// `tp`, sflux `prate_ave`), so the prate bundle's input is resolved off the
/// source's own list rather than hard-coded per model.
pub fn bundle_input_ids(source: &SourceSpec, bundle_id: &str) -> Vec<String> {
    if vector_components(bundle_id).is_some() {
        return vector_input_ids(bundle_id);
    }
    if let Some(inputs) = derived_scalar_inputs(bundle_id) {
        return inputs;
    }
    if bundle_id == "prate" {
        return source
            .input_variable_ids
            .iter()
            .copied()
            .filter(|id| PRECIPITATION_INPUT_IDS.contains(id))
            .take(1)
            .map(str::to_string)
            .collect();
    }
    vec![bundle_id.to_string()]
}

/// Every bundle a source can publish, in manifest order: its scalars, then
/// each listed vector bundle whose inputs the source fetches. A derived
/// scalar counts the same way — listed, it ships only when its inputs are.
pub fn published_bundle_ids(source: &SourceSpec) -> Vec<&'static str> {
    let mut ids: Vec<&'static str> = source
        .bundle_scalar_ids
        .iter()
        .copied()
        .filter(|bundle_id| {
            bundle_input_ids(source, bundle_id)
                .iter()
                .all(|id| source.input_variable_ids.contains(&id.as_str()))
        })
        .collect();
    for bundle_id in source.bundle_vector_ids {
        let inputs = vector_input_ids(bundle_id);
        if inputs
            .iter()
            .all(|id| source.input_variable_ids.contains(&id.as_str()))
        {
            ids.push(bundle_id);
        }
    }
    ids
}

// -- grid --------------------------------------------------------------------

/// The grid, from the geotransform GDAL holds — the doubles themselves, not
/// a printed rounding of them.
///
/// The reference encoder reads the same doubles: `xuebuild.gdal.dataset_info`
/// inspects through the wheel's `gdal_info` (`gdalio::info_json`) whenever
/// the wheel is installed, and the `gdalinfo -json` it falls back to without
/// one prints the geotransform at a precision that has changed between GDAL
/// releases (16 significant digits in 3.8, every digit later). Rounding here
/// to imitate one of those texts once held the two encoders byte-comparable
/// on the rounded side; it now put the native grid an ulp off the reference
/// on any origin the rounding touched — a regional crop of the GFS-Wave
/// grid, whose step is a hair over 0.25° — while the clean 0.25° origins
/// every published grid has never noticed either way.
///
/// A file on a map projection describes the projected grid, and the
/// published one is the regular grid `source.regrid` asks for over its
/// footprint (`reproject.rs`); a source that declares no regrid must not be
/// projected, and one that does must be, so a file of the wrong kind is
/// refused rather than misread.
fn grid_info(path: &Path, source: &SourceSpec) -> Result<GridInfo> {
    let dataset = Dataset::open(path)?;
    let (width, height) = dataset.size();
    let transform = dataset.geo_transform()?;
    if transform[2] != 0.0 || transform[4] != 0.0 {
        return Err(EncodeError::conversion(format!(
            "rotated grids are unsupported: {}",
            path.display()
        )));
    }
    let projection = lambert_conformal_from_wkt(&dataset.projection_wkt())?;
    if let Some(projection) = projection {
        let Some(regrid) = source.regrid else {
            return Err(EncodeError::conversion(format!(
                "{} is on a map projection, which this source does not declare",
                path.display()
            )));
        };
        if transform[1] <= 0.0 || transform[5] >= 0.0 {
            return Err(EncodeError::conversion(format!(
                "grid must run west-to-east and north-to-south: {}",
                path.display()
            )));
        }
        // To the millimetre, as the reference encoder takes them: GDAL
        // releases place a projected grid's origin a few nanometres apart,
        // and every resampled coordinate descends from these four numbers.
        let [x0, dx, y0, dy] = [transform[0], transform[1], transform[3], transform[5]].map(round3);
        let resampler = build_resampler(
            ProjectedGrid {
                projection,
                width,
                height,
                x0: x0 + dx / 2.0,
                y0: y0 + dy / 2.0,
                dx,
                dy: -dy,
            },
            regrid,
        )?;
        let mut grid = GridInfo::new(
            resampler.width,
            resampler.height,
            resampler.first_longitude,
            resampler.first_latitude,
            resampler.step,
            -resampler.step,
        );
        grid.resample = Some(Arc::new(resampler));
        return Ok(grid);
    }
    if source.regrid.is_some() {
        return Err(EncodeError::conversion(format!(
            "{} is a regular grid, but this source declares a projected one",
            path.display()
        )));
    }
    let (longitude_step, latitude_step) = (transform[1], transform[5]);
    if longitude_step <= 0.0 || latitude_step >= 0.0 {
        return Err(EncodeError::conversion(format!(
            "grid must run west-to-east and north-to-south: {}",
            path.display()
        )));
    }
    let grid = normalize_longitudes(snap_regional_steps(snap_global_longitudes(GridInfo::new(
        width,
        height,
        transform[0] + longitude_step / 2.0,
        transform[3] + latitude_step / 2.0,
        longitude_step,
        latitude_step,
    ))));
    match source.downsample {
        Some(downsample) => grid.downsampled(downsample.factor, path),
        None => Ok(grid),
    }
}

/// Round to three decimal places the way Python's `round(value, 3)` does:
/// correct decimal rounding, ties to even.
fn round3(value: f64) -> f64 {
    format!("{value:.3}").parse().unwrap_or(value)
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

// -- extraction --------------------------------------------------------------

fn convert_units(variable_id: &str, unit: &str, values: &mut [f64]) -> Result<()> {
    match variable_id {
        // The 2 m temperature, dew point, apparent temperature and the
        // surface (skin) temperature all take the Celsius rule
        // (`SURFACE_TEMPERATURE_IDS`).
        "tmp2m" | "dpt2m" | "aptmp2m" | "tmpsfc" => match normalize_unit(unit)? {
            "K" => values.iter_mut().for_each(|value| *value -= 273.15),
            "F" => values
                .iter_mut()
                .for_each(|value| *value = (*value - 32.0) * 5.0 / 9.0),
            _ => {}
        },
        // kg m⁻² s⁻¹ (a millimetre per second) to mm/h; the MRMS rate is
        // already mm/h.
        "prate" if !precipitation_rate_is_mm_per_hour(unit) => {
            values.iter_mut().for_each(|value| *value *= 3600.0)
        }
        "prate" => {}
        // ECMWF run-total precipitation accumulation, metres -> mm; the rate
        // derivation happens later against the previous frame.
        "tp" => values.iter_mut().for_each(|value| *value *= 1000.0),
        // GRIB2 carries mean sea level pressure in pascals; the codebook
        // quantizes hectopascals.
        "prmsl" => values.iter_mut().for_each(|value| *value /= 100.0),
        // GRIB2 carries visibility in metres; the codebook quantizes km.
        "vis" => values.iter_mut().for_each(|value| *value /= 1000.0),
        // GRIB2 carries sea ice cover as a 0–1 proportion; the codebook
        // quantizes percent.
        "icec" => values.iter_mut().for_each(|value| *value *= 100.0),
        // Cloud cover: percent as pgrb2 carries it, or the 0–1 fraction
        // ECMWF writes (GDAL spells that unit "-"), scaled up.
        "tcdc" | "lcdc" | "mcdc" | "hcdc" => {
            if unit
                .trim()
                .trim_matches(|character| "[]()".contains(character))
                != "%"
            {
                values.iter_mut().for_each(|value| *value *= 100.0);
            }
        }
        // A direction in degrees true: a record can carry 360, which is the
        // codebook's 0 — reduce it there so the wrap never clamps. Every
        // value is non-negative here, so this is numpy's `mod`.
        "dirpw" => values
            .iter_mut()
            .for_each(|value| *value = value.rem_euclid(360.0)),
        other => match isobaric_variable(other) {
            // Isobaric temperature follows the 2 m rule.
            Some(("tmp", _)) => match normalize_unit(unit)? {
                "K" => values.iter_mut().for_each(|value| *value -= 273.15),
                "F" => values
                    .iter_mut()
                    .for_each(|value| *value = (*value - 32.0) * 5.0 / 9.0),
                _ => {}
            },
            // GRIB2 carries specific humidity as a mass ratio (kg/kg); the
            // codebook quantizes g/kg.
            Some(("spfh", _)) => values.iter_mut().for_each(|value| *value *= 1000.0),
            // Wind components, geopotential heights, relative humidity,
            // cloud cover, CAPE, vertical velocity, ice thickness and the
            // wave height and period are already in their output units.
            _ => {}
        },
    }
    Ok(())
}

/// The smallest specific humidity the derivation sees, in kg/kg: a dry
/// stratospheric cell can carry zero, whose vapour pressure has no logarithm.
const THETA_E_MINIMUM_Q: f64 = 1e-7;

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

/// Extract every requested band of one file, as float64 planes in physical
/// units, cropped and rolled into the published layout.
fn extract_planes(
    frames: &FileFrames,
    grid: &GridInfo,
    plane_source: &PlaneSource,
) -> Result<Vec<(String, Vec<f64>)>> {
    let source = &frames[0].1.path;
    // The netCDF driver is not thread-safe; the guard is held for the whole
    // extraction, open included, and is a no-op for every GRIB source.
    let _serial = needs_serial_access(source).then(netcdf_guard);
    let dataset = Dataset::open(source)?;
    let (source_height, source_width) = grid.source_shape();
    if dataset.size() != (source_width, source_height) {
        return Err(EncodeError::conversion(format!(
            "extracted plane size mismatch for {}",
            source.display()
        )));
    }
    let mut planes = Vec::with_capacity(frames.len());
    for (variable_id, frame) in frames {
        let mut plane = dataset.read_band_f64(frame.band)?;
        if plane_source.unscale {
            let band = dataset.band_info(frame.band)?;
            if band.scale != 1.0 || band.offset != 0.0 {
                for value in &mut plane {
                    *value = *value * band.scale + band.offset;
                }
            }
        }
        if grid.column_roll > 0 {
            let roll = grid.column_roll;
            let mut rolled = vec![0f64; plane.len()];
            for row in 0..source_height {
                let base = row * source_width;
                for column in 0..source_width {
                    rolled[base + column] =
                        plane[base + (column + source_width - roll) % source_width];
                }
            }
            plane = rolled;
        }
        // Missing data becomes a value before the plane is resampled, so a
        // fill never blends into its neighbours; then the projected plane
        // lands on the regular grid, and only then is a regional window cut.
        plane_source.apply_fill(&mut plane);
        fill_missing(variable_id, &mut plane)?;
        if let Some(resample) = &grid.resample {
            plane = resample.take(&plane)?;
        }
        if let Some(downsample) = &grid.downsample {
            plane = downsample.take(&plane)?;
        }
        if let Some(crop) = grid.crop {
            plane = crop.take(&plane);
        }
        if plane.iter().any(|value| !value.is_finite()) {
            return Err(EncodeError::conversion(format!(
                "Xue v1 requires complete planes, found non-finite values in {}",
                source.display()
            )));
        }
        convert_units(variable_id, &frame.unit, &mut plane)?;
        planes.push((variable_id.clone(), plane));
    }
    Ok(planes)
}

/// Map the points a record does not cover to the bottom of the variable's
/// codebook — a value, not a gap. Which values mark them is the variable's
/// own to declare (`VariableSpec::fill_values`: GDAL's 9999 for a GRIB2
/// bitmap); a variable that covers its grid declares none and passes through
/// untouched. Runs before unit conversion, on the raw record values — the
/// port of `_fill_missing` in `xuebuild/binconvert.py`.
fn fill_missing(variable_id: &str, plane: &mut [f64]) -> Result<()> {
    let spec = variable_spec(variable_id)?;
    if spec.fill_values.is_empty() {
        return Ok(());
    }
    PlaneSource {
        unscale: false,
        fill_values: spec.fill_values.to_vec(),
        fill_replacement: f64::from(spec.value_range.0),
    }
    .apply_fill(plane);
    Ok(())
}

// -- per-file quantization ---------------------------------------------------

#[derive(Debug, Clone)]
struct PlaneStats {
    variable_id: String,
    max_abs_error: f64,
    clamped_points: u64,
    overflow_points: u64,
}

/// A raw precipitation plane one file publishes for its successor to
/// difference against. Filled once, read once, then dropped.
#[derive(Default)]
struct PlaneSlot {
    state: Mutex<Option<std::result::Result<Arc<Vec<f64>>, String>>>,
    ready: Condvar,
}

impl PlaneSlot {
    fn publish(&self, value: std::result::Result<Arc<Vec<f64>>, String>) {
        *self.state.lock().expect("plane slot") = Some(value);
        self.ready.notify_all();
    }

    fn wait(&self) -> Result<Arc<Vec<f64>>> {
        let mut state = self.state.lock().expect("plane slot");
        while state.is_none() {
            state = self.ready.wait(state).expect("plane slot");
        }
        state
            .clone()
            .expect("published")
            .map_err(EncodeError::Conversion)
    }
}

/// One file's quantized planes: its lead time, the codes per variable, and the
/// per-variable quantization statistics the acceptance gate reads.
type QuantizedFile = (i64, Vec<(String, Vec<u8>)>, Vec<PlaneStats>);

fn quantize_file(
    frames: &FileFrames,
    grid: &GridInfo,
    profile: &str,
    plane_source: &PlaneSource,
    average_window_hours: i64,
    previous: Option<(i64, Arc<PlaneSlot>)>,
    own: Option<Arc<PlaneSlot>>,
    derived_vector_ids: &[&str],
    drop_ids: &[String],
    derived_scalar_ids: &[&str],
) -> Result<QuantizedFile> {
    let lead = frames[0].1.lead_seconds;
    // The precipitation derivations below are GRIB-only, and every GRIB record
    // is a whole hour out, so they can work in hours.
    let hour = lead / HOUR_SECONDS;

    let extracted = match extract_planes(frames, grid, plane_source) {
        Ok(planes) => planes,
        Err(error) => {
            // Unblock the successor waiting on this worker's plane.
            if let Some(slot) = own {
                slot.publish(Err(error.to_string()));
            }
            return Err(error);
        }
    };
    let mut values: Vec<(String, Vec<f64>)> = extracted;

    let raw_precipitation_id = DERIVED_PRECIPITATION_IDS
        .iter()
        .find(|id| values.iter().any(|(name, _)| name == *id))
        .copied();
    if let (Some(slot), Some(raw_id)) = (own, raw_precipitation_id) {
        let plane = values
            .iter()
            .find(|(name, _)| name == raw_id)
            .map(|(_, plane)| Arc::new(plane.clone()))
            .expect("published raw precipitation plane");
        slot.publish(Ok(plane));
    }
    let mut previous_plane: Option<Arc<Vec<f64>>> = None;
    let mut previous_hour: Option<i64> = None;
    if let Some((hour, slot)) = previous {
        previous_hour = Some(hour);
        previous_plane = Some(slot.wait()?);
    }

    if let Some(raw_id) = raw_precipitation_id {
        let position = values
            .iter()
            .position(|(name, _)| name == raw_id)
            .expect("raw precipitation plane");
        let (_, raw) = values.remove(position);
        let derived = if raw_id == "tp" {
            // ECMWF: replace the run-total accumulation (already mm) with the
            // mean rate over the step that ends at this frame (mm/h). The step
            // is the actual distance to the previous frame.
            let step = previous_hour.map_or(1, |previous| hour - previous);
            deaccumulate_precipitation(&raw, previous_plane.as_deref().map(Vec::as_slice), step)
        } else {
            // sflux: PRATE is the window-cumulative mean rate (kg/m^2 s);
            // derive the per-step rate against the previous frame of the same
            // averaging window.
            deaverage_precipitation(
                &raw,
                hour,
                previous_plane.as_deref().map(Vec::as_slice),
                previous_hour,
                average_window_hours,
            )?
        };
        values.insert(position, ("prate".to_string(), derived));
    }

    // The derived vector bundles — the vapour flux, the wave vector — from
    // the planes just extracted; the inputs that served only such a
    // derivation (and are not published themselves) are released here
    // rather than quantized and carried through the whole run.
    for bundle_id in derived_vector_ids {
        let inputs = vector_input_ids(bundle_id);
        let plane = |name: &str| -> Result<&Vec<f64>> {
            values
                .iter()
                .find(|(id, _)| id == name)
                .map(|(_, plane)| plane)
                .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for {bundle_id}")))
        };
        let (component_u, component_v) = if *bundle_id == WAVE_BUNDLE_ID {
            derive_wave_vector(plane(&inputs[0])?, plane(&inputs[1])?)
        } else {
            derive_vapour_flux(plane(&inputs[0])?, plane(&inputs[1])?, plane(&inputs[2])?)
        };
        let (u_id, v_id) = vector_components(bundle_id).expect("a derived vector bundle is a vector");
        values.push((u_id, component_u));
        values.push((v_id, component_v));
    }
    // The equivalent potential temperatures, after the vapour flux and
    // before the derivation-only inputs go: both read the specific humidity.
    for bundle_id in derived_scalar_ids {
        let inputs = derived_scalar_inputs(bundle_id).expect("a derived scalar");
        let level = theta_e_level(bundle_id).expect("a derived scalar has a level");
        let plane = |name: &str| -> Result<&Vec<f64>> {
            values
                .iter()
                .find(|(id, _)| id == name)
                .map(|(_, plane)| plane)
                .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for {bundle_id}")))
        };
        let derived = derive_theta_e(plane(&inputs[0])?, plane(&inputs[1])?, level);
        values.push(((*bundle_id).to_string(), derived));
    }
    values.retain(|(name, _)| !drop_ids.contains(name));

    let mut codes = Vec::with_capacity(values.len());
    let mut stats = Vec::with_capacity(values.len());
    for (variable_id, plane_values) in &values {
        let book = codebook(profile, variable_id)?;
        let mut plane_codes = vec![0u8; plane_values.len()];
        book.quantize(plane_values, &mut plane_codes)?;
        match book {
            Codebook::Linear(linear) => {
                let mut clamped = 0u64;
                let mut max_error = 0.0f64;
                for (value, code) in plane_values.iter().zip(&plane_codes) {
                    if *value < linear.minimum || *value > linear.maximum {
                        clamped += 1;
                    } else {
                        max_error = max_error.max((linear.decode(*code) - value).abs());
                    }
                }
                stats.push(PlaneStats {
                    variable_id: variable_id.clone(),
                    max_abs_error: max_error,
                    clamped_points: clamped,
                    overflow_points: 0,
                });
            }
            Codebook::Precipitation(precipitation) => {
                let overflow = plane_codes
                    .iter()
                    .filter(|code| u16::from(**code) == precipitation.overflow_code)
                    .count() as u64;
                stats.push(PlaneStats {
                    variable_id: variable_id.clone(),
                    max_abs_error: 0.0,
                    clamped_points: 0,
                    overflow_points: overflow,
                });
            }
        }
        codes.push((variable_id.clone(), plane_codes));
    }
    Ok((lead, codes, stats))
}

// -- payload assembly --------------------------------------------------------


/// The tile size one bundle is cut with.
///
/// A half-resolution variant halves the source tile, so tile number n covers
/// the same ground in both tiers and a viewport keeps its tile rectangle
/// across a tier switch. Either size is then clamped to the grid, because the
/// format requires `1 <= tile <= grid` so that a single-tile file states its
/// grid size exactly — and a regional crop is routinely smaller than the
/// source's tile (a six-degree showcase window is 24 x 24 cells against the
/// 0.25-degree grid's 48 x 52 tile). Clamping makes such a file one tile,
/// which is the right answer: there is nothing left to subdivide.
///
/// Mirrors `_bundle_tile` in `xuebuild/binconvert.py`.
fn bundle_tile(tile: (usize, usize), grid: &GridInfo, half: bool) -> (usize, usize) {
    let tile = if half {
        (tile.0.div_ceil(2), tile.1.div_ceil(2))
    } else {
        tile
    };
    (tile.0.min(grid.width), tile.1.min(grid.height))
}

/// The v2 index tables and uncompressed chunks of one bundle.
///
/// Every variable of a bundle shares the file's one axis and therefore its
/// temporal groups, and the chunks come out in the physical order the spec
/// fixes — group, then tile row-major, then variable. That order is what puts
/// the two components of the wind bundle next to each other inside a tile, so
/// one range request still covers a wind frame while a viewport's tile row
/// and a cell's series each stay one narrow span.
///
/// `variableId` is file-local and assigned here exactly the way
/// [`build_metadata`](crate::encode::metadata::build_metadata) assigns it —
/// 1..n by position in `variable_ids` — so the index and the metadata agree
/// by construction, and the ascending id order the spec fixes is the bundle's
/// own variable order.
fn bundle_chunks(
    variable_ids: &[&str],
    offsets: &[i64],
    codes: &BTreeMap<i64, Vec<(String, Vec<u8>)>>,
    geometry: &TileGeometry,
) -> Result<ChunkTables> {
    let variables: Vec<VariableEntry> = variable_ids
        .iter()
        .enumerate()
        .map(|(index, variable_id)| VariableEntry {
            variable_id: index as u8 + 1,
            predictor: if RAW_VARIABLE_IDS.contains(variable_id) {
                Predictor::Raw
            } else {
                Predictor::Previous
            },
        })
        .collect();

    // Numeric id back to the name the code planes are keyed by.
    let names: BTreeMap<u8, &str> = variable_ids
        .iter()
        .enumerate()
        .map(|(index, variable_id)| (index as u8 + 1, *variable_id))
        .collect();
    let plane = |offset: i64, variable_id: u8| -> Result<&[u8]> {
        let name = names[&variable_id];
        codes
            .get(&offset)
            .and_then(|planes| planes.iter().find(|(id, _)| id == name))
            .map(|(_, plane)| plane.as_slice())
            .ok_or_else(|| {
                EncodeError::conversion(format!("missing {name} plane at offset {offset}"))
            })
    };
    let (groups, raw) = build_chunks(offsets, &variables, &plane, geometry)?;
    Ok(ChunkTables {
        variables,
        groups,
        chunks: raw
            .into_iter()
            .map(|(entry, payload)| ChunkPayload { entry, payload })
            .collect(),
    })
}

/// Half-resolution copy of one quantized plane (rows/columns 0, 2, 4, …),
/// matching the poster decimation so every tier shares the same sample sites.
fn decimate_codes(codes: &[u8], grid: &GridInfo) -> Vec<u8> {
    let mut half = Vec::with_capacity(grid.width.div_ceil(2) * grid.height.div_ceil(2));
    for row in (0..grid.height).step_by(2) {
        for column in (0..grid.width).step_by(2) {
            half.push(codes[row * grid.width + column]);
        }
    }
    half
}

/// HLS `STREAM-INF` style bandwidth hint: average bits per second needed to
/// keep up with the 12 fps playback rate while downloading the whole tier.
fn playback_bandwidth(byte_length: usize, frame_count: usize) -> u64 {
    let fps = 12.0;
    let value = byte_length as f64 * 8.0 * fps / frame_count.max(1) as f64;
    (value.round() as u64).max(1)
}

fn crc32_hex(bytes: &[u8]) -> String {
    format!("{:08x}", binformat::crc32_plane(bytes))
}

// -- bundle writing ----------------------------------------------------------

fn write_variable_bundle(
    variable_id: &str,
    output: &Path,
    metadata: &Value,
    geometry: &TileGeometry,
    tables: ChunkTables,
    frame_count: u32,
    zstd_level: i32,
    options: &ConvertOptions,
) -> Result<Value> {
    log!(
        options,
        "compressing {} {variable_id} chunks at zstd level {zstd_level}",
        tables.chunks.len()
    );
    let tables = compress_all(tables, zstd_level, options)?;
    let metadata_json = serde_json::to_string(metadata).expect("serializable metadata");
    let bundle_bytes =
        binformat::write_bundle_v2(output, &metadata_json, geometry, &tables, frame_count)?;
    log!(
        options,
        "wrote {} ({:.2} MB)",
        output.display(),
        bundle_bytes.len() as f64 / 1e6
    );

    // Read the complete file back and reconstruct every chunk before
    // publishing stats — the same acceptance gate the Python encoder applies,
    // run through the production decoder crate.
    verify_bundle_bytes(&bundle_bytes)?;
    log!(
        options,
        "verified {} {variable_id} chunks by full read-back decode",
        tables.chunks.len()
    );

    let mut report = Map::new();
    report.insert("variable".into(), json!(variable_id));
    report.insert("output".into(), json!(output.display().to_string()));
    report.insert("byteLength".into(), json!(bundle_bytes.len()));
    report.insert("crc32".into(), json!(crc32_hex(&bundle_bytes)));
    Ok(Value::Object(report))
}

fn compress_all(
    tables: ChunkTables,
    zstd_level: i32,
    options: &ConvertOptions,
) -> Result<ChunkTables> {
    use rayon::prelude::*;

    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(options.compress_workers.max(1))
        .build()
        .map_err(|error| EncodeError::conversion(format!("cannot start zstd pool: {error}")))?;
    let ChunkTables { variables, groups, chunks } = tables;
    let chunks = pool.install(|| {
        chunks
            .into_par_iter()
            .map(|chunk| {
                let payload = zstd_compress(&chunk.payload, zstd_level)?;
                Ok(ChunkPayload {
                    entry: ChunkEntry {
                        compressed_length: payload.len() as u32,
                        crc32: chunk.entry.crc32,
                    },
                    payload,
                })
            })
            .collect::<Result<Vec<_>>>()
    })?;
    Ok(ChunkTables { variables, groups, chunks })
}

fn zstd_compress(payload: &[u8], level: i32) -> Result<Vec<u8>> {
    // One-shot `ZSTD_compress2` with an explicit checksum, which is what the
    // Python encoder's `compression.zstd.compress` calls. The streaming
    // encoder would produce a frame without the pledged source size and pick
    // different window parameters, so the two would not agree byte for byte.
    use zstd::zstd_safe::{CCtx, CParameter};

    let mut context = CCtx::create();
    for parameter in [
        CParameter::CompressionLevel(level),
        CParameter::ChecksumFlag(true),
    ] {
        context.set_parameter(parameter).map_err(|code| {
            EncodeError::conversion(format!("zstd parameter rejected: {}", error_name(code)))
        })?;
    }
    let mut output = Vec::with_capacity(zstd::zstd_safe::compress_bound(payload.len()));
    context.compress2(&mut output, payload).map_err(|code| {
        EncodeError::conversion(format!("zstd compression failed: {}", error_name(code)))
    })?;
    Ok(output)
}

fn error_name(code: usize) -> &'static str {
    zstd::zstd_safe::get_error_name(code)
}

/// Decode every plane of a freshly written bundle with the decoder crate.
fn verify_bundle_bytes(bytes: &[u8]) -> Result<()> {
    let mut bundle = crate::Bundle::open(bytes)
        .map_err(|error| EncodeError::bundle(format!("read-back parse failed: {}", error.0)))?;
    let variables: Vec<u8> = bundle.variable_ids().to_vec();
    let offsets: Vec<u16> = bundle.frame_offsets().to_vec();
    for variable_id in variables {
        for frame_offset in &offsets {
            match bundle.decode_frame(crate::FrameRequest {
                variable_id,
                frame_offset: *frame_offset,
            }) {
                Ok(_) => {}
                Err(error) => {
                    // A variable need not carry every offset on the bundle's
                    // axis only when the bundle mixes axes, which the encoder
                    // never writes; anything else is a real failure.
                    return Err(EncodeError::bundle(format!(
                        "read-back decode failed for variable {variable_id} at offset {frame_offset}: {}",
                        error.0
                    )));
                }
            }
        }
    }
    Ok(())
}

// -- the conversion ----------------------------------------------------------

/// Convert a GRIB run (or one observation NetCDF file) into per-variable Xue
/// bundles.
///
/// Writes `<output_dir>/<variable>.xue` for every scalar variable plus the
/// two-variable `wind10m.xue` bundle when the input files carry the 10 m wind
/// components, per-variable posters, half-resolution `.half.xue` variants, and
/// returns build statistics. When `latest_path` and `run_id` are given, also
/// (re)writes the mutable live pointer aimed at the freshly written manifest.
///
/// `bbox` crops every plane to a region and `bundle_ids` restricts which
/// bundles are built — the two knobs the historical showcase cases use.
/// `last_hour` trims an observation source's series to a leading window.
///
/// The optional H.264 companion artifacts are not built here; this
/// experimental encoder leaves them to the Python pipeline, and the frontend
/// treats them as optional by design.
pub fn convert_bin(
    inputs: &[PathBuf],
    output_dir: &Path,
    options: &ConvertOptions,
) -> Result<Value> {
    let source = source_spec(&options.model)?;
    if !crate::encode::quantize::PROFILES.contains(&options.profile.as_str()) {
        return Err(EncodeError::conversion(format!(
            "unknown profile: {}",
            options.profile
        )));
    }
    let published = published_bundle_ids(source);
    if let Some(bundle_ids) = &options.bundle_ids {
        let unsupported = bundle_ids
            .iter()
            .any(|id| !published.contains(&id.as_str()));
        if unsupported || bundle_ids.is_empty() {
            return Err(EncodeError::conversion(format!(
                "{} publishes {published:?}, not {bundle_ids:?}",
                source.manifest_model
            )));
        }
    }

    // -- frame discovery ----------------------------------------------------
    let mut per_file: Vec<FileFrames>;
    let variable_ids: Vec<String>;
    let available_vector_ids: Vec<&'static str>;
    let available_derived_ids: Vec<&'static str>;
    let drop_ids: Vec<String>;
    let grid_path: PathBuf;
    let plane_source: PlaneSource;

    if source.observation && !source.fetched() {
        // A local-file observation source is one NetCDF file holding the
        // whole series, one band per time. There are no records to match, no
        // wind pair, and no published cadence to validate the axis against —
        // the file's own times are the axis, gaps included. (A fetched
        // observation is one GRIB per frame and takes the record path
        // below, re-keyed onto its window's axis.)
        if inputs.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "a {} build takes exactly one NetCDF file",
                source.manifest_model
            )));
        }
        if options.require_complete {
            return Err(EncodeError::conversion(format!(
                "{} has no complete run to require",
                source.manifest_model
            )));
        }
        let series = inspect_observation(&inputs[0], source)?;
        per_file = series.frames;
        // `last_hour` trims the series to a leading window of the file, and
        // the frame it stops on must exist — a case's declared range is never
        // silently shortened.
        if let Some(last_hour) = options.last_hour {
            let cutoff = last_hour * HOUR_SECONDS;
            let ends_at = per_file
                .last()
                .map_or(0, |frames| frames[0].1.lead_seconds);
            per_file.retain(|frames| frames[0].1.lead_seconds <= cutoff);
            if per_file.last().is_none_or(|frames| frames[0].1.lead_seconds != cutoff) {
                return Err(EncodeError::conversion(format!(
                    "{} has no frame exactly at hour {last_hour}; its series ends at hour {}",
                    inputs[0].display(),
                    ends_at as f64 / HOUR_SECONDS as f64
                )));
            }
        }
        variable_ids = source
            .input_variable_ids
            .iter()
            .map(|id| (*id).to_string())
            .collect();
        available_vector_ids = Vec::new();
        available_derived_ids = Vec::new();
        drop_ids = Vec::new();
        grid_path = series.dataset;
        plane_source = series.plane_source;
    } else {
        let paths = discover_inputs(inputs)?;
        // One real GDAL inspection pass over the first file: it probes which
        // vector bundles can be built (their inputs are optional, so runs
        // fetched before the wind components joined the download set still
        // build cleanly) and serves as the per-run cross-check reference for
        // the GRIB2 header index used on every file.
        let mut inspect_ids: Vec<&str> = source.input_variable_ids.to_vec();
        if let Some(bundle_ids) = &options.bundle_ids {
            let needed: Vec<String> = bundle_ids
                .iter()
                .flat_map(|bundle_id| bundle_input_ids(source, bundle_id))
                .collect();
            inspect_ids.retain(|id| needed.iter().any(|needed| needed == id));
        }
        let requested_vector_ids: Vec<&'static str> = published
            .iter()
            .copied()
            .filter(|id| vector_components(id).is_some())
            .filter(|id| {
                options
                    .bundle_ids
                    .as_ref()
                    .is_none_or(|ids| ids.iter().any(|wanted| wanted == id))
            })
            .collect();
        let requested_derived_ids: Vec<&'static str> = published
            .iter()
            .copied()
            .filter(|id| derived_scalar_inputs(id).is_some())
            .filter(|id| {
                options
                    .bundle_ids
                    .as_ref()
                    .is_none_or(|ids| ids.iter().any(|wanted| wanted == id))
            })
            .collect();
        // An input that only feeds a derivation — a vector bundle, a derived
        // scalar — may be absent; one that is also a published scalar may not.
        let mut vector_only_ids: Vec<String> = Vec::new();
        for bundle_id in requested_vector_ids.iter().chain(&requested_derived_ids) {
            for id in bundle_input_ids(source, bundle_id) {
                if !source.bundle_scalar_ids.contains(&id.as_str()) && !vector_only_ids.contains(&id) {
                    vector_only_ids.push(id);
                }
            }
        }
        let mut optional: Vec<&str> = source.optional_at_analysis.to_vec();
        optional.extend(vector_only_ids.iter().map(String::as_str));
        let reference_frames = inspect_grib_multi(&paths[0], &inspect_ids, &optional)?;

        available_vector_ids = requested_vector_ids
            .iter()
            .copied()
            .filter(|bundle_id| {
                vector_input_ids(bundle_id)
                    .iter()
                    .all(|id| reference_frames.iter().any(|(name, _)| name == id))
            })
            .collect();
        available_derived_ids = requested_derived_ids
            .iter()
            .copied()
            .filter(|bundle_id| {
                bundle_input_ids(source, bundle_id)
                    .iter()
                    .all(|id| reference_frames.iter().any(|(name, _)| name == id))
            })
            .collect();
        for bundle_id in requested_vector_ids.iter().chain(&requested_derived_ids) {
            if !available_vector_ids.contains(bundle_id) && !available_derived_ids.contains(bundle_id) {
                eprintln!(
                    "WARNING building without the {bundle_id} bundle, {} are not all in {}",
                    bundle_input_ids(source, bundle_id).join(", "),
                    paths[0].display()
                );
            }
        }

        // The variables read from the GRIB inputs: every scalar input (ECMWF
        // carries the accumulated tp instead of a rate and sflux the
        // window-averaged prate_ave, which the per-file stage derives into
        // prate), plus the inputs of each vector bundle that can be built.
        let mut ordered: Vec<String> = inspect_ids
            .iter()
            .filter(|id| !vector_only_ids.iter().any(|only| only == *id))
            .map(|id| (*id).to_string())
            .collect();
        let scalar_count = ordered.len();
        for bundle_id in available_vector_ids.iter().chain(&available_derived_ids) {
            for id in bundle_input_ids(source, bundle_id) {
                if !ordered.contains(&id) {
                    ordered.push(id);
                }
            }
        }
        // The inputs that only serve a derivation (spfh850 under the vapour
        // flux) are released once it is done, rather than quantized and held
        // for the whole run.
        let components: Vec<String> = available_vector_ids
            .iter()
            .filter_map(|bundle_id| vector_components(bundle_id))
            .flat_map(|(u, v)| [u, v])
            .collect();
        drop_ids = ordered[scalar_count..]
            .iter()
            .filter(|id| !components.contains(id))
            .cloned()
            .collect();
        // The first variable is the run's reference: every file is keyed by
        // its forecast hour, so it must be one no file can lack. Stable-sorting
        // the analysis-optional inputs to the back is enough unless nothing
        // else was asked for.
        ordered.sort_by_key(|id| source.optional_at_analysis.contains(&id.as_str()));
        if ordered.is_empty() || source.optional_at_analysis.contains(&ordered[0].as_str()) {
            return Err(EncodeError::conversion(format!(
                "a {} build needs at least one variable present in every file, including the \
                 analysis; {ordered:?} is not enough",
                source.manifest_model
            )));
        }
        variable_ids = ordered;
        let ordered_refs: Vec<&str> = variable_ids.iter().map(String::as_str).collect();
        per_file = prepare_frames_all(
            &paths,
            &ordered_refs,
            source.optional_at_analysis,
            &reference_frames,
            source.cadence_seconds,
            options,
        )?;
        grid_path = paths[0].clone();
        plane_source = PlaneSource::grib();
    }

    // -- the time axis ------------------------------------------------------
    let reference_id = variable_ids[0].as_str();
    let lead_seconds: Vec<i64> = per_file
        .iter()
        .map(|frames| {
            frame_of(frames, reference_id)
                .map(|frame| frame.lead_seconds)
                .ok_or_else(|| EncodeError::conversion(format!("missing {reference_id} record")))
        })
        .collect::<Result<_>>()?;
    let unit_seconds = axis_unit_seconds(&lead_seconds);
    let offsets: Vec<i64> = lead_seconds
        .iter()
        .map(|seconds| seconds / unit_seconds)
        .collect();
    if *offsets.last().expect("non-empty axis") >= i64::from(NO_DEPENDENCY) {
        return Err(EncodeError::conversion(format!(
            "the axis needs {} steps of {unit_seconds} s, past the u16 frame offset range",
            offsets.last().expect("non-empty axis")
        )));
    }
    if !source.observation {
        // The input hours must be a contiguous run of the source's published
        // axis, so no frame is missing and every step matches the cadence.
        let axis = source.forecast_hours(*offsets.last().expect("non-empty axis"))?;
        let tail: Vec<i64> = axis
            .iter()
            .copied()
            .filter(|hour| *hour >= offsets[0])
            .collect();
        if offsets != tail {
            return Err(EncodeError::conversion(format!(
                "forecast hours must be a contiguous run of the {} axis",
                source.manifest_model
            )));
        }
        if options.require_complete {
            let expected = source.forecast_hours(options.expected_hours)?;
            if offsets != expected {
                return Err(EncodeError::conversion(format!(
                    "complete build requires forecast hours 0 through {} on the {} axis",
                    options.expected_hours, source.manifest_model
                )));
            }
        }
    }
    let run_time: OffsetDateTime = frame_of(&per_file[0], reference_id)
        .expect("reference frame")
        .run_time
        .to_offset(time::UtcOffset::UTC);

    // -- the grid -----------------------------------------------------------
    let mut grid = grid_info(&grid_path, source)?;
    if options.require_complete && (grid.width, grid.height) != source.production_grid {
        return Err(EncodeError::conversion(format!(
            "production build requires a {}x{} grid",
            source.production_grid.0, source.production_grid.1
        )));
    }
    if let Some(bbox) = options.bbox {
        grid = crop_grid(grid, bbox)?;
        log!(
            options,
            "cropped to {}x{} from {:.4},{:.4}",
            grid.width,
            grid.height,
            grid.first_longitude,
            grid.first_latitude
        );
    }

    // -- extract and quantize ------------------------------------------------
    log!(
        options,
        "extracting and quantizing {} files with {} workers",
        per_file.len(),
        options.extract_workers
    );
    let raw_precipitation_id = DERIVED_PRECIPITATION_IDS
        .iter()
        .find(|id| variable_ids.iter().any(|name| name == *id))
        .copied();
    let plan = sharing_plan(&per_file, raw_precipitation_id, source)?;
    let derived_vector_ids: Vec<&str> = available_vector_ids
        .iter()
        .copied()
        .filter(|bundle_id| is_derived_vector(bundle_id))
        .collect();
    let derived_scalar_ids: Vec<&str> = available_derived_ids.clone();
    let results = for_each_ordered(per_file.len(), options.extract_workers, |index| {
        let (previous, own) = plan[index].clone();
        quantize_file(
            &per_file[index],
            &grid,
            &options.profile,
            &plane_source,
            source.average_window_hours,
            previous,
            own,
            &derived_vector_ids,
            &drop_ids,
            &derived_scalar_ids,
        )
    })?;

    let mut stats: Vec<PlaneStats> = Vec::new();
    let mut codes_by_offset: BTreeMap<i64, Vec<(String, Vec<u8>)>> = BTreeMap::new();
    for (lead, codes, file_stats) in results {
        codes_by_offset.insert(lead / unit_seconds, codes);
        stats.extend(file_stats);
    }
    log!(
        options,
        "quantized {} planes",
        codes_by_offset.values().map(Vec::len).sum::<usize>()
    );

    // -- what gets published -------------------------------------------------
    // A derived scalar whose inputs the run lacked is left out like a vector
    // bundle is.
    let mut scalar_variable_ids: Vec<&str> = source
        .bundle_scalar_ids
        .iter()
        .copied()
        .filter(|id| derived_scalar_inputs(id).is_none() || available_derived_ids.contains(id))
        .collect();
    if let Some(bundle_ids) = &options.bundle_ids {
        scalar_variable_ids.retain(|id| bundle_ids.iter().any(|wanted| wanted == id));
    }
    let mut encoded_variable_ids: Vec<String> =
        scalar_variable_ids.iter().map(|id| (*id).to_string()).collect();
    for bundle_id in &available_vector_ids {
        let (u, v) = vector_components(bundle_id).expect("a vector bundle");
        encoded_variable_ids.push(u);
        encoded_variable_ids.push(v);
    }
    // Scalars that also ship a poster — every published scalar but the
    // contour-drawn pressure family, which a filled first-frame poster would
    // misrepresent (the native encoder writes no video at all; xuebuild's
    // `native.py` adds those afterwards, and skips the same set).
    let companion_variable_ids: Vec<&str> = scalar_variable_ids
        .iter()
        .copied()
        .filter(|id| !PRESSURE_BUNDLE_IDS.contains(id))
        .collect();

    // Per-variable time axes. A variable with no data for the analysis frame
    // starts at the first real step, and every artifact of it carries its
    // own shorter axis: the rate on a derived-precipitation source, whose
    // interval would precede the run, and any scalar whose record the source
    // lists as optional at the analysis (ECMWF's gust). Mirrors
    // `analysis_optional_ids` in `xuebuild/binconvert.py`.
    let mut variable_offsets: BTreeMap<String, Vec<i64>> = encoded_variable_ids
        .iter()
        .map(|id| (id.clone(), offsets.clone()))
        .collect();
    if offsets.len() > 1 {
        for variable_id in analysis_optional_ids(source, &scalar_variable_ids) {
            variable_offsets.insert(variable_id.to_string(), offsets[1..].to_vec());
        }
    }

    // -- posters --------------------------------------------------------------
    std::fs::create_dir_all(output_dir).map_err(|error| {
        EncodeError::conversion(format!("cannot create {}: {error}", output_dir.display()))
    })?;
    let mut poster_reports: BTreeMap<&str, Value> = BTreeMap::new();
    for variable_id in &companion_variable_ids {
        let first = variable_offsets[*variable_id][0];
        let plane = codes_by_offset[&first]
            .iter()
            .find(|(name, _)| name == variable_id)
            .map(|(_, plane)| plane)
            .ok_or_else(|| {
                EncodeError::conversion(format!("missing {variable_id} plane at the first frame"))
            })?;
        let (payload, poster_grid) = encode_poster(plane, &grid)?;
        let poster_path = output_dir.join(format!("{variable_id}.poster.bin"));
        binformat::write_atomic(&poster_path, &payload)?;
        let metadata = build_metadata(
            run_time,
            &variable_offsets[*variable_id],
            &poster_grid,
            &options.profile,
            &[variable_id],
            source,
            unit_seconds,
        )?;
        poster_reports.insert(
            variable_id,
            json!({
                "path": poster_path.display().to_string(),
                "width": poster_grid.width,
                "height": poster_grid.height,
                "byteLength": payload.len(),
                "crc32": crc32_hex(&payload),
                "metadataJson": to_spaced_json(&metadata),
            }),
        );
        log!(
            options,
            "wrote {} ({:.1} KB)",
            poster_path.display(),
            payload.len() as f64 / 1e3
        );
    }

    // -- bundles and the half-resolution ladder --------------------------------
    let half_grid = grid.decimated();
    let half_codes_by_offset: BTreeMap<i64, Vec<(String, Vec<u8>)>> = if options.skip_variants {
        BTreeMap::new()
    } else {
        codes_by_offset
            .iter()
            .map(|(offset, planes)| {
                (
                    *offset,
                    planes
                        .iter()
                        .map(|(name, codes)| (name.clone(), decimate_codes(codes, &grid)))
                        .collect(),
                )
            })
            .collect()
    };

    // Vector bundles first (the largest), scalars after; reports keep the
    // scalars-then-vectors manifest order regardless.
    let mut submit_order: Vec<&str> = available_vector_ids.clone();
    submit_order.extend_from_slice(&scalar_variable_ids);
    let mut report_order: Vec<&str> = scalar_variable_ids.clone();
    report_order.extend_from_slice(&available_vector_ids);

    let mut full_reports: BTreeMap<&str, Value> = BTreeMap::new();
    let mut variant_reports: BTreeMap<&str, Vec<Value>> = BTreeMap::new();
    for bundle_id in &submit_order {
        let bundle_variables: Vec<String> = match vector_components(bundle_id) {
            Some((u, v)) => vec![u, v],
            None => vec![(*bundle_id).to_string()],
        };
        let variables: Vec<&str> = bundle_variables.iter().map(String::as_str).collect();
        let bundle_offsets: Vec<i64> = variable_offsets[variables[0]].clone();
        for (suffix, bundle_grid, codes) in [
            ("", &grid, &codes_by_offset),
            (".half", &half_grid, &half_codes_by_offset),
        ] {
            if suffix == ".half" && options.skip_variants {
                continue;
            }
            let metadata = build_metadata(
                run_time,
                &bundle_offsets,
                &bundle_grid,
                &options.profile,
                &variables,
                source,
                unit_seconds,
            )?;
            let tile = bundle_tile(source.tile, &bundle_grid, !suffix.is_empty());
            let geometry = TileGeometry::new(
                bundle_grid.width as u32,
                bundle_grid.height as u32,
                tile.0 as u32,
                tile.1 as u32,
            )
            .map_err(|error| EncodeError::conversion(error.0))?;
            let tables = bundle_chunks(&variables, &bundle_offsets, codes, &geometry)?;
            let output = output_dir.join(format!("{bundle_id}{suffix}.xue"));
            let mut report = write_variable_bundle(
                bundle_id,
                &output,
                &metadata,
                &geometry,
                tables,
                bundle_offsets.len() as u32,
                options.zstd_level,
                options,
            )?;
            if suffix.is_empty() {
                full_reports.insert(bundle_id, report);
            } else {
                let byte_length = report["byteLength"].as_u64().unwrap_or(0) as usize;
                let object = report.as_object_mut().expect("report object");
                object.insert("width".into(), json!(bundle_grid.width));
                object.insert("height".into(), json!(bundle_grid.height));
                object.insert(
                    "bandwidth".into(),
                    json!(playback_bandwidth(byte_length, bundle_offsets.len())),
                );
                variant_reports.entry(bundle_id).or_default().push(report);
            }
        }
    }

    let bundle_reports: Vec<Value> = report_order
        .iter()
        .map(|bundle_id| full_reports[bundle_id].clone())
        .collect();

    // -- report ---------------------------------------------------------------
    let stat_max = |variables: &[&str]| {
        stats
            .iter()
            .filter(|item| variables.contains(&item.variable_id.as_str()))
            .map(|item| item.max_abs_error)
            .fold(0.0f64, f64::max)
    };
    let stat_clamped = |variables: &[&str]| -> u64 {
        stats
            .iter()
            .filter(|item| variables.contains(&item.variable_id.as_str()))
            .map(|item| item.clamped_points)
            .sum()
    };
    let mut report = Map::new();
    report.insert("outputDir".into(), json!(output_dir.display().to_string()));
    report.insert("model".into(), json!(source.manifest_model));
    report.insert("grid".into(), Value::Object(grid.metadata()));
    report.insert("profile".into(), json!(options.profile));
    report.insert("zstdLevel".into(), json!(options.zstd_level));
    report.insert("zstdVersion".into(), json!(zstd_version()));
    report.insert("bundles".into(), json!(bundle_reports));
    report.insert(
        "variants".into(),
        json!(variant_reports.values().flatten().collect::<Vec<_>>()),
    );
    report.insert(
        "posters".into(),
        json!(poster_reports.values().collect::<Vec<_>>()),
    );
    report.insert("videos".into(), json!([]));
    report.insert(
        "byteLength".into(),
        json!(bundle_reports
            .iter()
            .filter_map(|bundle| bundle["byteLength"].as_u64())
            .sum::<u64>()),
    );
    report.insert("temperatureMaxAbsError".into(), json!(stat_max(&["tmp2m"])));
    report.insert(
        "temperatureClampedPoints".into(),
        json!(stat_clamped(&["tmp2m"])),
    );
    report.insert(
        "precipitationOverflowPoints".into(),
        json!(stats
            .iter()
            .filter(|item| item.variable_id == "prate")
            .map(|item| item.overflow_points)
            .sum::<u64>()),
    );
    if available_vector_ids.contains(&WIND_BUNDLE_ID) {
        report.insert("windMaxAbsError".into(), json!(stat_max(&WIND_COMPONENT_IDS)));
        report.insert(
            "windClampedPoints".into(),
            json!(stat_clamped(&WIND_COMPONENT_IDS)),
        );
    }

    // Quantization acceptance runs over the *encoded* variables (prate is the
    // derived output on ECMWF/sflux; the raw input has no codebook).
    for variable_id in &encoded_variable_ids {
        let Some(linear) = codebook(&options.profile, variable_id)?.as_linear().copied() else {
            continue;
        };
        let worst = stat_max(&[variable_id]);
        if worst > 0.5001 * linear.step {
            return Err(EncodeError::conversion(format!(
                "{variable_id} quantization error exceeds half a step"
            )));
        }
    }

    // -- manifest and live pointer --------------------------------------------
    if let Some(manifest_path) = &options.manifest_path {
        // The source's core bundles (`core_bundle_ids`: the tmp2m/prate pair
        // on a forecast, the reflectivity on a radar mosaic) are what a
        // complete run must publish. A restricted build ships only the
        // bundles it was asked for.
        let require_core = options.bundle_ids.is_none();
        let manifest_dir = manifest_path.parent().unwrap_or(Path::new("."));
        let entries: Vec<Value> = bundle_reports
            .iter()
            .map(|bundle| {
                let variable = bundle["variable"].as_str().unwrap_or_default();
                bundle_manifest_entry(
                    bundle,
                    manifest_dir,
                    poster_reports.get(variable),
                    variant_reports.get(variable),
                )
            })
            .collect::<Result<_>>()?;
        let expected_hours = if offsets.len() > 1 {
            lead_hours(*offsets.last().expect("non-empty axis"), unit_seconds)
        } else {
            options.expected_hours
        };
        let payload = build_bin_manifest(
            run_time,
            entries,
            expected_hours,
            source.manifest_model,
            source.product,
            require_core,
        )?;
        write_json(manifest_path, &payload, options.force)?;
        log!(options, "wrote manifest {}", manifest_path.display());
        if let (Some(latest_path), Some(run_id)) = (&options.latest_path, &options.run_id) {
            let manifest_bytes = serialize_json(&payload);
            let relative = manifest_path
                .strip_prefix(latest_path.parent().unwrap_or(Path::new(".")))
                .unwrap_or(manifest_path);
            let pointer = build_latest_pointer(
                run_id,
                run_time,
                &relative.to_string_lossy().replace('\\', "/"),
                &crc32_hex(manifest_bytes.as_bytes()),
                source.manifest_model,
                source.product,
            );
            write_json(latest_path, &pointer, true)?;
            log!(
                options,
                "wrote live pointer {} -> run {run_id}",
                latest_path.display()
            );
        }
    }

    Ok(Value::Object(report))
}

fn zstd_version() -> String {
    let version = zstd::zstd_safe::version_number();
    format!(
        "{}.{}.{}",
        version / 10_000,
        (version / 100) % 100,
        version % 100
    )
}

fn bundle_manifest_entry(
    bundle: &Value,
    manifest_dir: &Path,
    poster: Option<&Value>,
    variants: Option<&Vec<Value>>,
) -> Result<Value> {
    let relative = |path: &str| -> Result<String> {
        let path = Path::new(path);
        Ok(path
            .strip_prefix(manifest_dir)
            .map_err(|_| {
                EncodeError::manifest(format!(
                    "{} is not inside the manifest directory {}",
                    path.display(),
                    manifest_dir.display()
                ))
            })?
            .to_string_lossy()
            .replace('\\', "/"))
    };
    let mut entry = Map::new();
    entry.insert("variable".into(), bundle["variable"].clone());
    entry.insert(
        "path".into(),
        json!(relative(bundle["output"].as_str().unwrap_or_default())?),
    );
    entry.insert("byteLength".into(), bundle["byteLength"].clone());
    entry.insert("crc32".into(), bundle["crc32"].clone());
    if let Some(variants) = variants.filter(|variants| !variants.is_empty()) {
        // Resolution ladder: STREAM-INF style alternate renditions of the same
        // variable; the top-level path stays the canonical full-res tier.
        let tiers: Vec<Value> = variants
            .iter()
            .map(|variant| {
                Ok(json!({
                    "path": relative(variant["output"].as_str().unwrap_or_default())?,
                    "width": variant["width"],
                    "height": variant["height"],
                    "byteLength": variant["byteLength"],
                    "crc32": variant["crc32"],
                    "bandwidth": variant["bandwidth"],
                }))
            })
            .collect::<Result<_>>()?;
        entry.insert("variants".into(), Value::Array(tiers));
    }
    if let Some(poster) = poster {
        entry.insert(
            "poster".into(),
            json!({
                "path": relative(poster["path"].as_str().unwrap_or_default())?,
                "width": poster["width"],
                "height": poster["height"],
                "byteLength": poster["byteLength"],
                "crc32": poster["crc32"],
                "metadataJson": poster["metadataJson"],
            }),
        );
    }
    Ok(Value::Object(entry))
}

/// Yield `(previous (hour, slot), own slot)` per file. A slot is created only
/// when the next file will difference against this file's raw plane, so each
/// shared plane is freed once its consumer finishes.
type SharingPlan = Vec<(Option<(i64, Arc<PlaneSlot>)>, Option<Arc<PlaneSlot>>)>;

fn sharing_plan(
    per_file: &[FileFrames],
    raw_precipitation_id: Option<&str>,
    source: &SourceSpec,
) -> Result<SharingPlan> {
    let mut plan: SharingPlan = Vec::with_capacity(per_file.len());
    let mut previous_slot: Option<Arc<PlaneSlot>> = None;
    for (index, frames) in per_file.iter().enumerate() {
        let frame = raw_precipitation_id.and_then(|id| frame_of(frames, id));
        let previous = match (&previous_slot, frame) {
            (Some(slot), Some(_)) => Some((
                frame_of(&per_file[index - 1], raw_precipitation_id.expect("id"))
                    .expect("predecessor frame")
                    .lead_seconds
                    / HOUR_SECONDS,
                Arc::clone(slot),
            )),
            _ => None,
        };
        let mut own = None;
        if let (Some(frame), Some(raw_id)) = (frame, raw_precipitation_id) {
            if index + 1 < per_file.len() {
                if let Some(successor) = frame_of(&per_file[index + 1], raw_id) {
                    let hour = frame.lead_seconds / HOUR_SECONDS;
                    let shares = raw_id == "tp"
                        || average_window_start(
                            successor.lead_seconds / HOUR_SECONDS,
                            source.average_window_hours,
                        )? < hour;
                    if shares {
                        own = Some(Arc::new(PlaneSlot::default()));
                    }
                }
            }
        }
        previous_slot = own.clone();
        plan.push((previous, own));
    }
    Ok(plan)
}

/// Inspect every file once for all variables, in parallel across files.
///
/// Variables in `optional_at_analysis` may be absent from the f000 file only.
/// Inspection uses the GRIB2 header index; the first file is cross-checked
/// against `reference_frames` (a real GDAL pass) and a run whose files the
/// header index cannot parse falls back to GDAL inspection.
fn prepare_frames_all(
    paths: &[PathBuf],
    variable_ids: &[&str],
    optional_at_analysis: &[&str],
    reference_frames: &FileFrames,
    cadence_seconds: Option<i64>,
    options: &ConvertOptions,
) -> Result<Vec<FileFrames>> {
    use rayon::prelude::*;

    let fast: Result<Vec<FileFrames>> = paths
        .par_iter()
        .map(|path| inspect_grib_fast(path, variable_ids, optional_at_analysis))
        .collect::<Result<Vec<_>>>()
        .and_then(|per_file| {
            check_reference_frames(&per_file[0], reference_frames, variable_ids)?;
            Ok(per_file)
        });
    let mut per_file = match fast {
        Ok(per_file) => per_file,
        Err(error) => {
            eprintln!(
                "WARNING GRIB2 header index unavailable ({error}); falling back to GDAL inspection"
            );
            paths
                .par_iter()
                .map(|path| inspect_grib_multi(path, variable_ids, optional_at_analysis))
                .collect::<Result<Vec<_>>>()?
        }
    };
    log!(options, "indexed {} files", per_file.len());
    if let Some(cadence_seconds) = cadence_seconds {
        per_file = snap_observation_frames(per_file, cadence_seconds)?;
    }

    for frames in &per_file {
        let mut leads: Vec<i64> = frames.iter().map(|(_, frame)| frame.lead_seconds).collect();
        leads.dedup();
        if leads.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "variables disagree on the lead time in {}",
                frames[0].1.path.display()
            )));
        }
    }
    per_file.sort_by_key(|frames| {
        frame_of(frames, variable_ids[0])
            .map(|frame| frame.lead_seconds)
            .unwrap_or(i64::MAX)
    });
    for variable_id in variable_ids {
        for frames in &per_file {
            if frame_of(frames, variable_id).is_some() {
                continue;
            }
            let lead = frames[0].1.lead_seconds;
            if !optional_at_analysis.contains(variable_id) || lead != 0 {
                return Err(EncodeError::conversion(format!(
                    "missing {variable_id} record at forecast hour {}",
                    lead / HOUR_SECONDS
                )));
            }
        }
        let mut leads: Vec<i64> = per_file
            .iter()
            .filter_map(|frames| frame_of(frames, variable_id))
            .map(|frame| frame.lead_seconds)
            .collect();
        let count = leads.len();
        leads.sort_unstable();
        leads.dedup();
        if leads.len() != count {
            return Err(EncodeError::conversion(format!(
                "duplicate lead times for {variable_id}"
            )));
        }
        let mut runs: Vec<i64> = per_file
            .iter()
            .filter_map(|frames| frame_of(frames, variable_id))
            .map(|frame| frame.run_time.unix_timestamp())
            .collect();
        runs.sort_unstable();
        runs.dedup();
        if runs.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "input files contain multiple run times for {variable_id}"
            )));
        }
    }
    Ok(per_file)
}

/// Re-key the frames of a fetched observation onto the window's axis — the
/// port of `_snap_observation_frames` in `xuebuild/binconvert.py`.
///
/// A GRIB observation names only its own time: each MRMS composite is its
/// own reference time, some forty seconds past a two-minute mark, with a
/// zero forecast time. Every observation time is snapped *down* to its
/// `cadence_seconds` slot (the products of one frame are stamped apart and
/// agree on the slot), the run time is the whole hour the first slot falls
/// in, and a frame's lead is its slot's distance from it.
fn snap_observation_frames(per_file: Vec<FileFrames>, cadence_seconds: i64) -> Result<Vec<FileFrames>> {
    let mut slots = Vec::with_capacity(per_file.len());
    for frames in &per_file {
        let mut file_slots: Vec<i64> = frames
            .iter()
            .map(|(_, frame)| frame.valid_time.unix_timestamp().div_euclid(cadence_seconds) * cadence_seconds)
            .collect();
        file_slots.sort_unstable();
        file_slots.dedup();
        if file_slots.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "variables disagree on the observation slot in {}",
                frames[0].1.path.display()
            )));
        }
        slots.push(file_slots[0]);
    }
    let window_start = slots.iter().copied().min().unwrap_or(0).div_euclid(HOUR_SECONDS) * HOUR_SECONDS;
    let run_time = OffsetDateTime::from_unix_timestamp(window_start)
        .map_err(|error| EncodeError::conversion(format!("invalid observation time: {error}")))?;
    let mut snapped = Vec::with_capacity(per_file.len());
    for (frames, slot) in per_file.into_iter().zip(slots) {
        let valid_time = OffsetDateTime::from_unix_timestamp(slot)
            .map_err(|error| EncodeError::conversion(format!("invalid observation time: {error}")))?;
        snapped.push(
            frames
                .into_iter()
                .map(|(variable_id, frame)| {
                    (
                        variable_id,
                        SourceFrame {
                            run_time,
                            valid_time,
                            lead_seconds: slot - window_start,
                            ..frame
                        },
                    )
                })
                .collect(),
        );
    }
    Ok(snapped)
}

/// Raise if the GRIB2 header index disagrees with GDAL on the per-run
/// reference file. The header index locates the bands every extraction reads,
/// so a mismatch must never pass silently; the error drops the whole run into
/// the GDAL fallback path.
fn check_reference_frames(
    fast_frames: &FileFrames,
    reference_frames: &FileFrames,
    variable_ids: &[&str],
) -> Result<()> {
    for variable_id in variable_ids {
        let fast = frame_of(fast_frames, variable_id);
        let reference = frame_of(reference_frames, variable_id);
        match (fast, reference) {
            (None, None) => continue,
            (None, Some(_)) | (Some(_), None) => {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 header index and GDAL disagree on the presence of {variable_id}"
                )))
            }
            (Some(fast), Some(reference)) => {
                if fast.band != reference.band
                    || fast.run_time != reference.run_time
                    || fast.valid_time != reference.valid_time
                    || fast.lead_seconds != reference.lead_seconds
                    || raster_expression(variable_id, &fast.unit)?
                        != raster_expression(variable_id, &reference.unit)?
                {
                    return Err(EncodeError::conversion(format!(
                        "GRIB2 header index disagrees with GDAL for {variable_id} in {}",
                        fast.path.display()
                    )));
                }
            }
        }
    }
    Ok(())
}
