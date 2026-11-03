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
use crate::encode::variables::VariableSpec;
use crate::encode::gdalio::{needs_serial_access, netcdf_guard, Dataset};
use crate::encode::gribpng::GribPngFile;
use crate::encode::grid::{
    crop_grid, normalize_longitudes, snap_global_longitudes, snap_regional_steps, GridInfo,
};
use crate::encode::metadata::{axis_unit_seconds, build_metadata, to_spaced_json};
use crate::encode::model::{PlaneSource, SourceFrame};
use crate::encode::parallel::for_each_ordered;
use crate::encode::poster::encode_poster;
use crate::encode::quantize::Book;
use crate::encode::registry::{Companion, DerivationKind, Registry};
use crate::encode::reproject::{build_resampler, lambert_conformal_from_wkt, ProjectedGrid};
use crate::encode::sources::{family_frame_path, SourceSpec};
use crate::encode::temporal::build_chunks;
use crate::encode::zarrstore;
use crate::encode::derive::{
    derive_cat_plane, derive_ptype, derive_theta_e, derive_vapour_flux, derive_wave_vector,
    interval_rate,
};
use crate::encode::discover::{discover_records, discover_series, frame_of, Discovered, FileFrames};
use crate::encode::ladder::{build_ladder, bundle_tile, playback_bandwidth, Rung, Tier};
use crate::encode::report::{build_report, write_manifest};

// The precipitation arithmetic stays reachable where `tests/encoder.rs`
// has always imported it from.
pub use crate::encode::derive::{
    average_window_start, deaccumulate_precipitation, deaverage_precipitation,
};

pub const DEFAULT_ZSTD_LEVEL: i32 = 15;

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
    /// Write each bundle's Zarr store (`docs/zarr-profile.md`) from the
    /// codes, and name it in the manifest.
    pub zarr: bool,
    /// Write each bundle's `.xue` container as well. A build needs at least
    /// one of the two.
    pub container: bool,
    /// The full-resolution bundles that also ship a series store
    /// (`SourceSpec.series_bundle_ids` in `xuebuild/sources.py`, handed in by
    /// the caller); only read when `zarr` is on.
    pub series_bundle_ids: Vec<String>,
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
            // The store alone, the way every run publishes; the container
            // is opt-in.
            zarr: true,
            container: false,
            series_bundle_ids: Vec::new(),
        }
    }
}

/// The id of the grid family one input is read from, or `None` for an input
/// on the source's own grid (`SourceSpec::grid_family_of`). Mirrors
/// `grid_family_of` in `xuebuild/binconvert.py`.
pub fn grid_family_of<'a>(source: &'a SourceSpec, variable_id: &str) -> Option<&'a str> {
    source.grid_family_of(variable_id).map(|companion| companion.id.as_str())
}

/// The grid family one bundle is built on: the family every one of its inputs
/// is read from, so the bundle's grid, tile, variants and poster are that
/// family's.
///
/// A bundle whose inputs span two grids could not be written at all — its
/// planes would not be the same shape — so a source that declares one is a
/// registry bug, and it is refused here rather than half-built. (The vapour
/// flux at 850 hPa reads the specific humidity and the wind pair there, all
/// three from the CFSv2 pressure-level family, which is what makes it one
/// bundle's worth of one grid.) Mirrors `bundle_grid_family` in
/// `xuebuild/binconvert.py`.
pub fn bundle_grid_family<'a>(
    registry: &Registry,
    source: &'a SourceSpec,
    bundle_id: &str,
) -> Result<Option<&'a str>> {
    let mut families: Vec<Option<&'a str>> = Vec::new();
    for variable_id in registry.bundle_input_ids(source, bundle_id) {
        let family = grid_family_of(source, &variable_id);
        if !families.contains(&family) {
            families.push(family);
        }
    }
    if families.len() > 1 {
        let mut named: Vec<&str> = families
            .iter()
            .map(|family| family.unwrap_or("the primary file"))
            .collect();
        named.sort_unstable();
        return Err(EncodeError::conversion(format!(
            "the {bundle_id} bundle of {} reads {}: every variable of a bundle must be on one grid",
            source.manifest_model,
            named.join(", ")
        )));
    }
    Ok(families.first().copied().flatten())
}

/// The dimension names a bundle's arrays carry in its run's shared store
/// (`docs/zarr-profile.md`, "Coordinates"), and so the coordinate arrays the
/// bundle uses there. Mirrors `store_dimensions` in `xuebuild/binconvert.py`.
///
/// `time_<bundle id>` for a published scalar whose series has no analysis
/// frame (`Registry::analysis_optional_ids`) and for a static field;
/// `latitude_<family>` / `longitude_<family>` for a bundle on a grid family
/// of its own, `<family>` being exactly what [`bundle_grid_family`] returns.
/// The decision is the rule, not a comparison of axes, so it is a pure
/// function of the source and the bundle id: a fan-out job building a subset
/// of the bundles and a whole build write the same objects.
pub fn store_dimensions(registry: &Registry, source: &SourceSpec, bundle_id: &str) -> Result<[String; 3]> {
    let variable_ids = registry.bundle_variable_ids(bundle_id);
    let own_axis = (source.bundle_scalar_ids.iter().any(|id| id == bundle_id)
        && !registry.analysis_optional_ids(source, &[bundle_id]).is_empty())
        || registry.is_static(&variable_ids[0]);
    let family = bundle_grid_family(registry, source, bundle_id)?;
    if let Some(family) = family {
        if !zarrstore::is_coordinate_suffix(family) {
            return Err(EncodeError::conversion(format!(
                "grid family {family:?} of {} cannot name a coordinate array",
                source.manifest_model
            )));
        }
    }
    Ok([
        if own_axis { format!("time_{bundle_id}") } else { "time".to_owned() },
        family.map_or_else(|| "latitude".to_owned(), |family| format!("latitude_{family}")),
        family.map_or_else(|| "longitude".to_owned(), |family| format!("longitude_{family}")),
    ])
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

// -- extraction --------------------------------------------------------------

/// Take one plane to the variable's unit by its conversion rule
/// (`units.rs`): the arithmetic the rule applies under the record's
/// spelling, once per value, in f64 — the operation `rules.convert` runs
/// for `_convert_units` in `xuebuild/binconvert.py`. The spelling was
/// accepted when the frame was inspected; a spelling the rule does not
/// scale under (a series already in the published unit) takes the identity.
fn convert_units(spec: &VariableSpec, unit: &str, values: &mut [f64]) -> Result<()> {
    spec.conversion.convert(unit, values)
}

/// How the planes of a frame are read: one file's plane source (GRIB: the
/// same for every record) or an observation window's per-variable ones.
/// Mirrors `_plane_source_for` in `xuebuild/binconvert.py`.
pub enum PlaneSources {
    Uniform(PlaneSource),
    PerVariable(Vec<(String, PlaneSource)>),
}

impl PlaneSources {
    fn for_variable(&self, variable_id: &str) -> Result<&PlaneSource> {
        match self {
            PlaneSources::Uniform(source) => Ok(source),
            PlaneSources::PerVariable(sources) => sources
                .iter()
                .find(|(id, _)| id == variable_id)
                .map(|(_, source)| source)
                .ok_or_else(|| {
                    EncodeError::conversion(format!("no plane source for {variable_id}"))
                }),
        }
    }
}

/// One grid per variable of a frame: the run's single grid for every source
/// with one, and — on a source with a grid family — the grid of the family
/// each variable is read from. Mirrors `_grid_for` in
/// `xuebuild/binconvert.py`, where the same thing is a `GridInfo` or a
/// mapping.
pub struct PlaneGrids(Vec<(String, GridInfo)>);

impl PlaneGrids {
    fn for_variable(&self, variable_id: &str) -> Result<&GridInfo> {
        self.0
            .iter()
            .find(|(id, _)| id == variable_id)
            .map(|(_, grid)| grid)
            .ok_or_else(|| EncodeError::conversion(format!("no grid for {variable_id}")))
    }
}

/// Extract every requested band of one frame, as float64 planes in physical
/// units, cropped and rolled into the published layout: one dataset open
/// per file the frame's variables live in — one for a GRIB record set, one
/// per variable for a satellite window whose series are one file each, one
/// per grid family for a source with more than one grid (mirrors
/// `_extract_planes` in `xuebuild/binconvert.py`).
///
/// `grids` is the published grid per variable; a file holds one family's
/// records, so the plane size is read off the grid of whatever the file
/// carries.
///
/// Each of `uncropped_ids` on a cropped grid also comes back in the second
/// list as it was before the crop window was cut, unit-converted the same
/// way — the whole plane a derivation reading neighbours needs
/// ([`derive_cat_plane`]). On an uncropped grid the published plane is that
/// plane already, and is not copied.
///
/// Each band is taken through the whole per-plane chain ([`finish_plane`])
/// as soon as it is read, so a frame never holds more than one
/// full-resolution plane beside the thinned ones already finished: an MRMS
/// 3D frame is 33 levels of 7000 x 3500 doubles, 6.5 GB read whole, and
/// 196 MB a plane. The chain is per plane and the planes come back in
/// `frames` order, so the output is the same either way. A file the netCDF
/// driver reads is the exception: its reads are serialized
/// (`netcdf_guard`), so its planes are read under the guard, the guard
/// released, and only then finished, rather than holding every other
/// worker's reads behind this one's arithmetic.
fn extract_planes(
    frames: &FileFrames,
    grids: &PlaneGrids,
    plane_sources: &PlaneSources,
    uncropped_ids: &[String],
    registry: &Registry,
) -> Result<(Vec<(String, Vec<f64>)>, Vec<(String, Vec<f64>)>)> {
    // The files in first-seen order, each with the positions in `frames` of
    // the variables it holds.
    let mut by_file: Vec<(&PathBuf, Vec<usize>)> = Vec::new();
    for (index, entry) in frames.iter().enumerate() {
        match by_file.iter_mut().find(|(path, _)| **path == entry.1.path) {
            Some((_, held)) => held.push(index),
            None => by_file.push((&entry.1.path, vec![index])),
        }
    }
    let mut planes: Vec<Option<Vec<f64>>> = vec![None; frames.len()];
    let mut wholes: Vec<Option<Vec<f64>>> = vec![None; frames.len()];
    let keep_whole = |position: usize| uncropped_ids.contains(&frames[position].0);
    for (source, positions) in by_file {
        let first_id = &frames[positions[0]].0;
        let (source_height, source_width) = grids.for_variable(first_id)?.source_shape();
        let unscale = plane_sources.for_variable(first_id)?.unscale;
        let serial = needs_serial_access(source);
        let mut unfinished: Vec<(usize, Vec<f64>)> = Vec::new();
        {
            // The netCDF driver is not thread-safe; the guard is held for
            // the file's open and every read, and is a no-op for every GRIB
            // source.
            let _serial = serial.then(netcdf_guard);
            let dataset = Dataset::open(source)?;
            let png = GribPngFile::open(source);
            if dataset.size() != (source_width, source_height) {
                return Err(EncodeError::conversion(format!(
                    "extracted plane size mismatch for {}",
                    source.display()
                )));
            }
            for &position in &positions {
                let frame = &frames[position].1;
                let mut plane = match &png {
                    Some(png) => png.read_band_f64(&dataset, frame.band)?,
                    None => dataset.read_band_f64(frame.band)?,
                };
                if unscale {
                    let band = dataset.band_info(frame.band)?;
                    if band.scale != 1.0 || band.offset != 0.0 {
                        for value in &mut plane {
                            *value = *value * band.scale + band.offset;
                        }
                    }
                }
                if serial {
                    unfinished.push((position, plane));
                } else {
                    let (finished, whole) =
                        finish_plane(&frames[position], plane, grids, plane_sources, keep_whole(position), registry)?;
                    planes[position] = Some(finished);
                    wholes[position] = whole;
                }
            }
        }
        for (position, plane) in unfinished {
            let (finished, whole) =
                finish_plane(&frames[position], plane, grids, plane_sources, keep_whole(position), registry)?;
            planes[position] = Some(finished);
            wholes[position] = whole;
        }
    }
    let uncropped = frames
        .iter()
        .zip(wholes)
        .filter_map(|((variable_id, _), whole)| whole.map(|whole| (variable_id.clone(), whole)))
        .collect();
    let planes = frames
        .iter()
        .zip(planes)
        .map(|((variable_id, _), plane)| (variable_id.clone(), plane.expect("every frame read")))
        .collect();
    Ok((planes, uncropped))
}

/// One extracted plane through the per-plane chain, into the published
/// layout and physical units: the column roll, the fill values, the missing
/// points, the resampling of a projected grid, the block thinning, the
/// regional crop, the completeness check and the unit conversion, in that
/// order. With `keep_whole` on a cropped grid, the plane as it was before
/// the crop also comes back, unit-converted and never checked for
/// completeness, as `_extract_planes` keeps it.
fn finish_plane(
    (variable_id, frame): &(String, SourceFrame),
    mut plane: Vec<f64>,
    grids: &PlaneGrids,
    plane_sources: &PlaneSources,
    keep_whole: bool,
    registry: &Registry,
) -> Result<(Vec<f64>, Option<Vec<f64>>)> {
    let source = &frame.path;
    let spec = registry.variable(variable_id)?;
    let grid = grids.for_variable(variable_id)?;
    let (source_height, source_width) = grid.source_shape();
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
    plane_sources.for_variable(variable_id)?.apply_fill(&mut plane);
    fill_missing(spec, &mut plane);
    if let Some(resample) = &grid.resample {
        plane = resample.take(&plane)?;
    }
    if let Some(downsample) = &grid.downsample {
        plane = downsample.take(&plane)?;
    }
    let mut whole: Option<Vec<f64>> = None;
    if let Some(crop) = grid.crop {
        let cropped = crop.take(&plane);
        if keep_whole {
            whole = Some(plane);
        }
        plane = cropped;
    }
    if plane.iter().any(|value| !value.is_finite()) {
        return Err(EncodeError::conversion(format!(
            "Xue v1 requires complete planes, found non-finite values in {}",
            source.display()
        )));
    }
    convert_units(spec, &frame.unit, &mut plane)?;
    if let Some(whole) = whole.as_mut() {
        convert_units(spec, &frame.unit, whole)?;
    }
    Ok((plane, whole))
}

/// Map the points a record does not cover to the bottom of the variable's
/// codebook — a value, not a gap. Which values mark them is the variable's
/// own to declare (`VariableSpec::fill_values`: GDAL's 9999 for a GRIB2
/// bitmap); a variable that covers its grid declares none and passes through
/// untouched. Runs before unit conversion, on the raw record values — the
/// port of `_fill_missing` in `xuebuild/binconvert.py`.
fn fill_missing(spec: &VariableSpec, plane: &mut [f64]) {
    if spec.fill_values.is_empty() {
        return;
    }
    PlaneSource {
        unscale: false,
        fill_values: spec.fill_values.clone(),
        fill_nan: false,
        fill_replacement: spec.value_range.0,
    }
    .apply_fill(plane);
}

// -- per-file quantization ---------------------------------------------------

#[derive(Debug, Clone)]
pub(super) struct PlaneStats {
    pub variable_id: String,
    pub max_abs_error: f64,
    pub clamped_points: u64,
    pub overflow_points: u64,
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

/// The precipitation rate the per-file stage derives on a source whose
/// precipitation arrives accumulated, averaged or as an interval total:
/// `(bundle id, raw input id)` (`Registry::derived_precipitation`).
type DerivedPrecipitation = (String, String);

#[allow(clippy::too_many_arguments)]
fn quantize_file(
    frames: &FileFrames,
    grids: &PlaneGrids,
    profile: &str,
    plane_source: &PlaneSources,
    source: &SourceSpec,
    precipitation: Option<&DerivedPrecipitation>,
    previous: Option<(i64, Option<Arc<PlaneSlot>>)>,
    own: Option<Arc<PlaneSlot>>,
    derived_vector_ids: &[&str],
    drop_ids: &[String],
    derived_scalar_ids: &[&str],
    registry: &Registry,
) -> Result<QuantizedFile> {
    let lead = frames[0].1.lead_seconds;
    // The precipitation derivations below are GRIB-only, and every GRIB record
    // is a whole hour out, so they can work in hours.
    let hour = lead / HOUR_SECONDS;

    // A derivation reading neighbours (the turbulence index) takes its
    // inputs whole, before any crop.
    let mut uncropped_ids: Vec<String> = Vec::new();
    for bundle_id in derived_scalar_ids {
        let Some(derivation) = registry.scalar_derivation(bundle_id) else { continue };
        if derivation.kind != DerivationKind::ClearAirTurbulence {
            continue;
        }
        for id in &derivation.inputs {
            if !uncropped_ids.contains(id) {
                uncropped_ids.push(id.clone());
            }
        }
    }
    let (extracted, uncropped) = match extract_planes(frames, grids, plane_source, &uncropped_ids, registry) {
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

    let raw_precipitation_id = precipitation
        .filter(|(_, raw_id)| values.iter().any(|(name, _)| name == raw_id))
        .map(|(_, raw_id)| raw_id.as_str());
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
        // An interval total names its interval's start without a plane
        // behind it (`sharing_plan`).
        previous_plane = match slot {
            Some(slot) => Some(slot.wait()?),
            None => None,
        };
    }

    if let (Some(raw_id), Some((bundle_id, _))) = (raw_precipitation_id, precipitation) {
        let position = values
            .iter()
            .position(|(name, _)| name == raw_id)
            .expect("raw precipitation plane");
        let (_, raw) = values.remove(position);
        let derived = if source.accumulated_precipitation {
            // ECMWF: replace the run-total accumulation (already mm) with the
            // mean rate over the step that ends at this frame (mm/h). The step
            // is the actual distance to the previous frame.
            let step = previous_hour.map_or(1, |previous| hour - previous);
            deaccumulate_precipitation(&raw, previous_plane.as_deref().map(Vec::as_slice), step)
        } else if source.interval_precipitation {
            // Open-Meteo: the file already holds the total that fell over the
            // interval since the model's previous native output time, so the
            // rate is that total over the interval's length in hours — the
            // distance to the frame before this one on the axis.
            let previous_hour = previous_hour.ok_or_else(|| {
                EncodeError::conversion(format!(
                    "the interval precipitation frame at hour {hour} names no interval"
                ))
            })?;
            interval_rate(&raw, hour - previous_hour)?
        } else {
            // sflux: PRATE is the window-cumulative mean rate (kg/m^2 s);
            // derive the per-step rate against the previous frame of the same
            // averaging window.
            deaverage_precipitation(
                &raw,
                hour,
                previous_plane.as_deref().map(Vec::as_slice),
                previous_hour,
                source.average_window_hours,
            )?
        };
        values.insert(position, (bundle_id.clone(), derived));
    }

    // The derived vector bundles — the vapour flux, the wave vector — from
    // the planes just extracted; the inputs that served only such a
    // derivation (and are not published themselves) are released here
    // rather than quantized and carried through the whole run.
    for bundle_id in derived_vector_ids {
        let derivation = registry
            .derivation(bundle_id)
            .ok_or_else(|| EncodeError::conversion(format!("{bundle_id} is not a derived bundle")))?;
        let inputs = &derivation.inputs;
        let plane = |name: &str| -> Result<&Vec<f64>> {
            values
                .iter()
                .find(|(id, _)| id == name)
                .map(|(_, plane)| plane)
                .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for {bundle_id}")))
        };
        let (component_u, component_v) = match derivation.kind {
            DerivationKind::WaveVector => derive_wave_vector(plane(&inputs[0])?, plane(&inputs[1])?),
            DerivationKind::VapourFlux => {
                derive_vapour_flux(plane(&inputs[0])?, plane(&inputs[1])?, plane(&inputs[2])?)
            }
            other => {
                return Err(EncodeError::conversion(format!(
                    "{bundle_id} is a vector bundle derived by {other:?}, which is not a vector algorithm"
                )))
            }
        };
        let (u_id, v_id) = registry
            .vector_components(bundle_id)
            .expect("a derived vector bundle is a vector");
        values.push((u_id.to_string(), component_u));
        values.push((v_id.to_string(), component_v));
    }
    // The derived scalars, after the vapour flux and before the
    // derivation-only inputs go: the equivalent potential temperatures read
    // the specific humidity on their surface, the precipitation type the four
    // categorical flags.
    for bundle_id in derived_scalar_ids {
        let derivation = registry
            .scalar_derivation(bundle_id)
            .ok_or_else(|| EncodeError::conversion(format!("{bundle_id} is not a derived scalar")))?;
        let inputs = &derivation.inputs;
        let derived = match derivation.kind {
            DerivationKind::PrecipitationType => derive_ptype(&values, inputs)?,
            DerivationKind::ClearAirTurbulence => {
                let level = derivation.level_hpa.expect("validated: a surface");
                let grid = grids.for_variable(&inputs[0])?;
                // A cropped grid's inputs were kept whole beside the window;
                // an uncropped grid's published planes are the whole planes.
                let plane = |name: &str| -> Result<&[f64]> {
                    uncropped
                        .iter()
                        .chain(values.iter())
                        .find(|(id, _)| id == name)
                        .map(|(_, plane)| plane.as_slice())
                        .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for {bundle_id}")))
                };
                derive_cat_plane(plane, bundle_id, level, inputs, grid, &source.cat_calibration)?
            }
            DerivationKind::ThetaE => {
                let level = derivation.level_hpa.expect("validated: a surface");
                let plane = |name: &str| -> Result<&Vec<f64>> {
                    values
                        .iter()
                        .find(|(id, _)| id == name)
                        .map(|(_, plane)| plane)
                        .ok_or_else(|| EncodeError::conversion(format!("missing {name} plane for {bundle_id}")))
                };
                derive_theta_e(plane(&inputs[0])?, plane(&inputs[1])?, level)
            }
            other => {
                return Err(EncodeError::conversion(format!(
                    "{bundle_id} is a scalar bundle derived by {other:?}, which is not a per-frame algorithm"
                )))
            }
        };
        values.push(((*bundle_id).to_string(), derived));
    }
    values.retain(|(name, _)| !drop_ids.contains(name));

    let mut codes = Vec::with_capacity(values.len());
    let mut stats = Vec::with_capacity(values.len());
    for (variable_id, plane_values) in &values {
        let book = registry.codebook(profile, variable_id)?;
        let mut plane_codes = vec![0u8; plane_values.len()];
        book.quantize(plane_values, &mut plane_codes)?;
        match &book.book {
            Book::Linear(linear) => {
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
            Book::Precipitation(precipitation) => {
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
    registry: &Registry,
) -> Result<ChunkTables> {
    let mut variables: Vec<VariableEntry> = Vec::with_capacity(variable_ids.len());
    for (index, variable_id) in variable_ids.iter().enumerate() {
        variables.push(VariableEntry {
            variable_id: index as u8 + 1,
            predictor: if registry.is_raw(variable_id)? {
                Predictor::Raw
            } else {
                Predictor::Previous
            },
        });
    }

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

pub(super) fn crc32_hex(bytes: &[u8]) -> String {
    format!("{:08x}", binformat::crc32_plane(bytes))
}

// -- bundle writing ----------------------------------------------------------

/// Tiers written at once; the compression pool bounds the CPU, this bounds
/// how many tiers' chunks are alive. Mirrors `_BUNDLE_WRITERS`.
const BUNDLE_WRITERS: usize = 4;

fn write_variable_bundle(
    variable_id: &str,
    output: &Path,
    metadata: &Value,
    geometry: &TileGeometry,
    tables: ChunkTables,
    frame_count: u32,
    zstd_level: i32,
    options: &ConvertOptions,
    pool: &rayon::ThreadPool,
) -> Result<Value> {
    log!(
        options,
        "compressing {} {variable_id} chunks at zstd level {zstd_level}",
        tables.chunks.len()
    );
    let tables = compress_all(tables, zstd_level, pool)?;
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
    pool: &rayon::ThreadPool,
) -> Result<ChunkTables> {
    use rayon::prelude::*;

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

pub(crate) fn zstd_compress(payload: &[u8], level: i32) -> Result<Vec<u8>> {
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
/// components, per-variable posters, the reduced `<bundle>.<tier>.xue`
/// variants of the source's resolution ladder (`.half.xue`, and `.quarter`
/// on the satellite disks), and returns build statistics. When `latest_path` and `run_id` are given, also
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
    registry: &Registry,
) -> Result<Value> {
    let source = registry.source(&options.model)?;
    if !options.container && !options.zarr {
        return Err(EncodeError::conversion(
            "a build without the container needs the store: pass zarr=True",
        ));
    }
    if !registry.knows_profile(&options.profile) {
        return Err(EncodeError::conversion(format!(
            "unknown profile: {}",
            options.profile
        )));
    }
    let published = registry.published_bundle_ids(source);
    // Every published variable must carry the profile asked for, and the
    // document is refused before any input is read when one does not.
    for bundle_id in &published {
        for variable_id in registry.bundle_variable_ids(bundle_id) {
            registry.codebook(&options.profile, &variable_id)?;
        }
    }
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
    let Discovered {
        per_file,
        variable_ids,
        available_vector_ids,
        available_derived_ids,
        available_composite_ids,
        available_volume_ids,
        drop_ids,
        grid_path,
        plane_source,
        producer_versions,
        mut variable_families,
    } = if source.series_file {
        discover_series(inputs, options, source, &published, registry)?
    } else {
        discover_records(inputs, options, source, &published, registry)?
    };


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
    let run_time: OffsetDateTime = frame_of(&per_file[0], reference_id)
        .expect("reference frame")
        .run_time
        .to_offset(time::UtcOffset::UTC);
    if !source.observation {
        // The input hours must be a contiguous run of the source's published
        // axis, so no frame is missing and every step matches the cadence.
        // The run's own cycle decides whether a longer axis applies.
        let cycle = Some(u32::from(run_time.hour()));
        let axis = source.forecast_hours(*offsets.last().expect("non-empty axis"), cycle)?;
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
            let expected = source.forecast_hours(options.expected_hours, cycle)?;
            if offsets != expected {
                return Err(EncodeError::conversion(format!(
                    "complete build requires forecast hours 0 through {} on the {} axis",
                    options.expected_hours, source.manifest_model
                )));
            }
        }
    }

    // -- the grid -----------------------------------------------------------
    // Every published variable's grid family: for a variable the converter
    // derives (the vapour flux pair, the rate) the family its bundle's inputs
    // are read from, for one it reads its own. `None` throughout on every
    // source but CFSv2, so there is one family and one grid as there always
    // was.
    for bundle_id in registry.published_bundle_ids(source) {
        let family = bundle_grid_family(registry, source, bundle_id)?;
        for variable_id in registry.bundle_variable_ids(bundle_id) {
            variable_families.retain(|(id, _)| *id != variable_id);
            variable_families.push((variable_id, family));
        }
    }
    let family_of = |variable_id: &str| -> Option<&str> {
        variable_families
            .iter()
            .find(|(id, _)| id == variable_id)
            .and_then(|(_, family)| *family)
    };
    // One grid per family in play, each read from that family's own file and
    // checked against that family's production grid. The families are in
    // input order, so the primary file's — the run's grid in the build report
    // — comes first wherever this build reads it at all.
    let mut family_order: Vec<Option<&str>> = Vec::new();
    for variable_id in &variable_ids {
        let family = family_of(variable_id);
        if !family_order.contains(&family) {
            family_order.push(family);
        }
    }
    let mut grids: Vec<(Option<&str>, GridInfo)> = Vec::new();
    for family in &family_order {
        let mut family_grid = grid_info(&family_frame_path(&grid_path, *family), source)?;
        if options.require_complete {
            let (production_grid, _) = source.family_grid(*family)?;
            if (family_grid.width, family_grid.height) != production_grid {
                let where_ = match family {
                    Some(family) => format!(" for the {family} family"),
                    None => String::new(),
                };
                return Err(EncodeError::conversion(format!(
                    "production build requires a {}x{} grid{where_}",
                    production_grid.0, production_grid.1
                )));
            }
        }
        if let Some(bbox) = options.bbox {
            family_grid = crop_grid(family_grid, bbox)?;
            log!(
                options,
                "cropped {} to {}x{} from {:.4},{:.4}",
                family.unwrap_or("the run"),
                family_grid.width,
                family_grid.height,
                family_grid.first_longitude,
                family_grid.first_latitude
            );
        }
        grids.push((*family, family_grid));
    }
    let grid_for = |family: Option<&str>| -> &GridInfo {
        &grids
            .iter()
            .find(|(held, _)| *held == family)
            .expect("every family in play has a grid")
            .1
    };
    // The run-level grid — the build report's, and the one every source with
    // a single grid uses throughout.
    let grid = grid_for(family_order[0]).clone();
    // The grid each variable's planes are read and cropped on.
    let read_grids = PlaneGrids(
        variable_ids
            .iter()
            .map(|variable_id| (variable_id.clone(), grid_for(family_of(variable_id)).clone()))
            .collect(),
    );

    // -- extract and quantize ------------------------------------------------
    log!(
        options,
        "extracting and quantizing {} files with {} workers",
        per_file.len(),
        options.extract_workers
    );
    // The rate the per-file stage derives, when this build reads its raw
    // input at all.
    let precipitation: Option<DerivedPrecipitation> = registry
        .derived_precipitation(source)?
        .filter(|(_, raw_id)| variable_ids.contains(raw_id));
    let plan = sharing_plan(&per_file, precipitation.as_ref().map(|(_, raw_id)| raw_id.as_str()), source)?;
    let derived_vector_ids: Vec<&str> = available_vector_ids
        .iter()
        .copied()
        .filter(|bundle_id| registry.is_derived_vector(bundle_id))
        .collect();
    let derived_scalar_ids: Vec<&str> = available_derived_ids.clone();
    let results = for_each_ordered(per_file.len(), options.extract_workers, |index| {
        let (previous, own) = plan[index].clone();
        quantize_file(
            &per_file[index],
            &read_grids,
            &options.profile,
            &plane_source,
            source,
            precipitation.as_ref(),
            previous,
            own,
            &derived_vector_ids,
            &drop_ids,
            &derived_scalar_ids,
            registry,
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
        .map(String::as_str)
        .filter(|id| registry.scalar_derivation(id).is_none() || available_derived_ids.contains(id))
        .collect();
    if let Some(bundle_ids) = &options.bundle_ids {
        scalar_variable_ids.retain(|id| bundle_ids.iter().any(|wanted| wanted == id));
    }
    let mut encoded_variable_ids: Vec<String> =
        scalar_variable_ids.iter().map(|id| (*id).to_string()).collect();
    for bundle_id in available_vector_ids
        .iter()
        .chain(&available_composite_ids)
        .chain(&available_volume_ids)
    {
        encoded_variable_ids.extend(registry.bundle_variable_ids(bundle_id));
    }
    // Scalars that also ship a poster — every published scalar whose entry
    // lists one; the contour-drawn pressure family lists none, since a
    // filled first-frame poster would misrepresent it (the native encoder
    // writes no video at all; xuebuild's `native.py` adds those afterwards).
    let companion_variable_ids: Vec<&str> = scalar_variable_ids
        .iter()
        .copied()
        .filter(|id| {
            registry
                .variable(id)
                .is_ok_and(|spec| spec.companions.contains(&Companion::Poster))
        })
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
    // Only a run that starts at the analysis has an analysis frame to be
    // missing from: a build whose axis already starts at the first step (a
    // job for the rate alone on a source whose precipitation input has no
    // analysis file) carries every frame it read.
    if offsets.len() > 1 && offsets[0] == 0 {
        for variable_id in registry.analysis_optional_ids(source, &scalar_variable_ids) {
            variable_offsets.insert(variable_id.to_string(), offsets[1..].to_vec());
        }
    }
    // A static field does not vary in time: its one frame is the first, and
    // its bundle carries just that offset. Mirrors `xuebuild/binconvert.py`.
    for variable_id in &encoded_variable_ids {
        if registry.is_static(variable_id) {
            variable_offsets.insert(variable_id.clone(), vec![offsets[0]]);
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
        let (payload, poster_grid) = encode_poster(plane, grid_for(family_of(variable_id)))?;
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
            &producer_versions,
            registry,
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

    // -- bundles and the resolution ladder -------------------------------------
    // One rung per factor of the source's ladder, in ascending factor order
    // (half, quarter, eighth): its file suffix, factor, grid and the codes of
    // every plane sampled from the full ones. `skip_variants` skips every
    // rung.
    //
    // A source with more than one grid gets one rung per factor per family:
    // a family's rung is its own grid decimated, so a bundle's tier keeps the
    // tile geometry of its own grid.
    let ladder: Vec<Rung<'_>> = if options.skip_variants {
        Vec::new()
    } else {
        build_ladder(
            &source.variant_factors,
            &grids,
            &codes_by_offset,
            |name| grid_for(family_of(name)),
            &options.profile,
            registry,
        )?
    };

    // Volume, composite and vector bundles first (the largest), scalars
    // after; reports keep the scalars-vectors-composites-volumes manifest
    // order regardless.
    let mut submit_order: Vec<&str> = available_volume_ids.clone();
    submit_order.extend_from_slice(&available_composite_ids);
    submit_order.extend_from_slice(&available_vector_ids);
    submit_order.extend_from_slice(&scalar_variable_ids);
    let mut report_order: Vec<&str> = scalar_variable_ids.clone();
    report_order.extend_from_slice(&available_vector_ids);
    report_order.extend_from_slice(&available_composite_ids);
    report_order.extend_from_slice(&available_volume_ids);

    // Every tier of every bundle is one task: the full tier, then each rung
    // of the ladder, in submit order. Up to `BUNDLE_WRITERS` run at once over
    // one machine-sized compression pool, so one bundle's serial tail (its
    // file writes, its shard index, its container read-back) overlaps the
    // next one's compression — `_BUNDLE_WRITERS` in the reference. A tier's
    // reports come back in task order, so a bundle's variants stay in the
    // ladder's ascending factor order whatever finished first.
    let mut tiers: Vec<Tier<'_>> = Vec::new();
    for bundle_id in &submit_order {
        // The bundle is written on its own family's grid, with that family's
        // tile: one grid for every source but CFSv2, whose pressure-level
        // bundles are 1° where its surface ones are the T126 Gaussian grid.
        let family = family_of(registry.bundle_variable_ids(bundle_id)[0].as_str());
        tiers.push(Tier {
            bundle_id,
            suffix: "",
            factor: 1,
            grid: grid_for(family),
            codes: &codes_by_offset,
        });
        for rung in &ladder {
            tiers.push(Tier {
                bundle_id,
                suffix: rung.suffix.as_str(),
                factor: rung.factor,
                grid: &rung
                    .grids
                    .iter()
                    .find(|(held, _)| *held == family)
                    .expect("every family in play has a rung")
                    .1,
                codes: &rung.codes_by_offset,
            });
        }
    }
    // The shared stores this build writes into, one per tier. A fanned-out
    // job shares them with the run's other jobs, so nothing already in them
    // is removed but the group document, which this build's arrays make
    // stale until the reference's `finalize_store` writes it from the parts.
    let mut shared_stores: Vec<(usize, PathBuf)> = Vec::new();
    if options.zarr {
        let mut tiers_in_play: Vec<(usize, &str)> = vec![(1, "")];
        tiers_in_play.extend(ladder.iter().map(|rung| (rung.factor, rung.suffix.as_str())));
        for (factor, suffix) in tiers_in_play {
            let store = output_dir.join(format!("{}{suffix}.zarr", zarrstore::SHARED_STORE_STEM));
            let group = store.join("zarr.json");
            if group.is_file() {
                std::fs::remove_file(&group).map_err(|error| {
                    EncodeError::conversion(format!("cannot remove {}: {error}", group.display()))
                })?;
            }
            shared_stores.push((factor, store));
        }
    }
    let shared_store = |factor: usize| -> &Path {
        &shared_stores
            .iter()
            .find(|(held, _)| *held == factor)
            .expect("every tier has a shared store")
            .1
    };
    let pool = zarrstore::compression_pool(options.compress_workers)?;
    let write_tier = |tier: &Tier<'_>| -> Result<(Value, Option<zarrstore::BundleWrite>)> {
        let bundle_variables: Vec<String> = registry.bundle_variable_ids(tier.bundle_id);
        let variables: Vec<&str> = bundle_variables.iter().map(String::as_str).collect();
        let bundle_offsets: &[i64] = &variable_offsets[variables[0]];
        let (_, family_tile) = source.family_grid(family_of(variables[0]))?;
        let metadata = build_metadata(
            run_time,
            bundle_offsets,
            tier.grid,
            &options.profile,
            &variables,
            source,
            unit_seconds,
            &producer_versions,
            registry,
        )?;
        let tile = bundle_tile(family_tile, tier.grid, tier.factor);
        let mut report = if options.container {
            let geometry = TileGeometry::new(
                tier.grid.width as u32,
                tier.grid.height as u32,
                tile.0 as u32,
                tile.1 as u32,
            )
            .map_err(|error| EncodeError::conversion(error.0))?;
            let tables = bundle_chunks(&variables, bundle_offsets, tier.codes, &geometry, registry)?;
            write_variable_bundle(
                tier.bundle_id,
                &output_dir.join(format!("{}{}.xue", tier.bundle_id, tier.suffix)),
                &metadata,
                &geometry,
                tables,
                bundle_offsets.len() as u32,
                options.zstd_level,
                options,
                &pool,
            )?
        } else {
            json!({"variable": tier.bundle_id, "container": false})
        };
        let mut bundle_write = None;
        if options.zarr {
            // Written from the codes in memory: a bundle's arrays are a
            // function of the metadata, the codes, the tile, the predictors
            // and its dimension names alone, so they never wait on a
            // container. The part comes after every bundle of the tier.
            let mut store_variables = Vec::with_capacity(variables.len());
            for variable_id in &variables {
                let planes = bundle_offsets
                    .iter()
                    .map(|offset| {
                        tier.codes
                            .get(offset)
                            .and_then(|planes| planes.iter().find(|(id, _)| id == variable_id))
                            .map(|(_, plane)| plane.as_slice())
                            .ok_or_else(|| {
                                EncodeError::conversion(format!(
                                    "missing {variable_id} plane at offset {offset}"
                                ))
                            })
                    })
                    .collect::<Result<Vec<_>>>()?;
                store_variables.push(zarrstore::StoreVariable {
                    id: (*variable_id).to_owned(),
                    previous: !registry.is_raw(variable_id)?,
                    planes,
                });
            }
            let codes = zarrstore::StoreCodes {
                metadata: &metadata,
                variables: store_variables,
                tile,
            };
            let store_path = shared_store(tier.factor);
            let store = zarrstore::write_bundle_arrays(
                &codes,
                store_path,
                tier.bundle_id,
                &store_dimensions(registry, source, tier.bundle_id)?,
                &pool,
            )?;
            log!(
                options,
                "wrote {} into {} ({:.2} MB)",
                tier.bundle_id,
                store_path.display(),
                store.byte_length as f64 / 1e6
            );
            report["zarr"] = store.to_value();
            bundle_write = Some(store);
            if tier.factor == 1 && options.series_bundle_ids.iter().any(|id| id == tier.bundle_id) {
                // The series companion is a full-resolution artifact: a
                // pinned point reads the canonical bundle, never a tier. Its
                // codec chain carries the bundle's own predictor.
                let series_path = output_dir.join(format!("{}.series.zarr", tier.bundle_id));
                let series = zarrstore::write_store(
                    &codes,
                    &series_path,
                    true,
                    Some(zarrstore::SERIES_BLOCK),
                    &pool,
                )?;
                log!(
                    options,
                    "wrote {} ({:.2} MB)",
                    series_path.display(),
                    series.byte_length as f64 / 1e6
                );
                report["series"] = series.to_value();
            }
        }
        if tier.factor > 1 {
            // A tier's playback hint is the delivery it ships: the
            // container where one is written, the store otherwise.
            let byte_length = if options.container {
                report["byteLength"].as_u64()
            } else {
                report["zarr"]["byteLength"].as_u64()
            }
            .unwrap_or(0) as usize;
            let object = report.as_object_mut().expect("report object");
            object.insert("width".into(), json!(tier.grid.width));
            object.insert("height".into(), json!(tier.grid.height));
            object.insert(
                "bandwidth".into(),
                json!(playback_bandwidth(byte_length, bundle_offsets.len())),
            );
        }
        Ok((report, bundle_write))
    };
    let written = for_each_ordered(tiers.len(), BUNDLE_WRITERS, |index| write_tier(&tiers[index]))?;

    let mut full_reports: BTreeMap<&str, Value> = BTreeMap::new();
    let mut variant_reports: BTreeMap<&str, Vec<Value>> = BTreeMap::new();
    let mut store_writes: Vec<((usize, &str), zarrstore::BundleWrite)> = Vec::new();
    for (tier, (report, bundle_write)) in tiers.iter().zip(written) {
        if let Some(bundle_write) = bundle_write {
            store_writes.push(((tier.factor, tier.bundle_id), bundle_write));
        }
        if tier.factor == 1 {
            full_reports.insert(tier.bundle_id, report);
        } else {
            variant_reports.entry(tier.bundle_id).or_default().push(report);
        }
    }

    // Each tier's partial group document, last: over this build's bundles in
    // manifest order. Every entry naming the store carries its CRC-32 until
    // the group document is written from the parts.
    for (factor, store) in &shared_stores {
        let writes = report_order
            .iter()
            .map(|bundle_id| {
                store_writes
                    .iter()
                    .find(|((held_factor, held_id), _)| held_factor == factor && held_id == bundle_id)
                    .map(|(_, bundle_write)| bundle_write)
                    .ok_or_else(|| EncodeError::bundle(format!("{bundle_id} was not written into {}", store.display())))
            })
            .collect::<Result<Vec<_>>>()?;
        let (part, crc32) = zarrstore::write_part(store, &writes)?;
        log!(options, "wrote {}", part.display());
        let rung = ladder.iter().position(|rung| rung.factor == *factor);
        for bundle_id in &report_order {
            let report = match rung {
                None => full_reports.get_mut(bundle_id),
                Some(index) => variant_reports.get_mut(bundle_id).and_then(|reports| reports.get_mut(index)),
            }
            .expect("every bundle has a report on every tier");
            report["zarr"]["crc32"] = json!(crc32);
        }
    }

    let bundle_reports: Vec<Value> = report_order
        .iter()
        .map(|bundle_id| full_reports[bundle_id].clone())
        .collect();

    // -- report ---------------------------------------------------------------
    let report = build_report(
        output_dir,
        source,
        &grid,
        options,
        &bundle_reports,
        &variant_reports,
        &poster_reports,
        &stats,
        &available_vector_ids,
        &encoded_variable_ids,
        registry,
    )?;

    // -- manifest and live pointer --------------------------------------------
    if let Some(manifest_path) = &options.manifest_path {
        write_manifest(
            manifest_path,
            options,
            source,
            &bundle_reports,
            &variant_reports,
            &poster_reports,
            grid_for,
            &offsets,
            unit_seconds,
            run_time,
            registry,
        )?;
    }

    Ok(Value::Object(report))
}

pub fn zstd_version() -> String {
    let version = zstd::zstd_safe::version_number();
    format!(
        "{}.{}.{}",
        version / 10_000,
        (version / 100) % 100,
        version % 100
    )
}

/// Yield `(previous (hour, slot), own slot)` per file. A slot is created only
/// when the next file will difference against this file's raw plane, so each
/// shared plane is freed once its consumer finishes. An interval total
/// (Open-Meteo apcp) needs no predecessor plane at all — only the hour beside
/// the slot, which is then `None`. Mirrors `sharing_plan` in
/// `xuebuild/binconvert.py`.
type SharingPlan = Vec<(Option<(i64, Option<Arc<PlaneSlot>>)>, Option<Arc<PlaneSlot>>)>;

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
            _ if frame.is_some() && source.interval_precipitation => {
                // The interval this total covers is the distance to the frame
                // before it on the axis being built. A series that starts at a
                // step of its own — the rate's, whose analysis frame does not
                // exist — takes the distance from the previous step of the
                // source's published axis instead, which is the interval the
                // model itself accumulated over; and the first step of an axis
                // that itself starts past the analysis (`first_hour`)
                // accumulated from the run time, hour 0. Only a total at the
                // analysis names no interval at all.
                let hour = frame.expect("a frame").lead_seconds / HOUR_SECONDS;
                let interval_start = if index > 0 {
                    per_file[index - 1][0].1.lead_seconds / HOUR_SECONDS
                } else {
                    let cycle = frame
                        .expect("a frame")
                        .run_time
                        .to_offset(time::UtcOffset::UTC)
                        .hour();
                    let axis = source.forecast_hours(hour, Some(u32::from(cycle)))?;
                    match axis.get(axis.len().wrapping_sub(2)) {
                        Some(&start) => start,
                        None if hour > 0 => 0,
                        None => {
                            return Err(EncodeError::conversion(format!(
                                "the interval precipitation frame at hour {hour} names no interval"
                            )))
                        }
                    }
                };
                Some((interval_start, None))
            }
            (Some(slot), Some(_)) => Some((
                frame_of(&per_file[index - 1], raw_precipitation_id.expect("id"))
                    .expect("predecessor frame")
                    .lead_seconds
                    / HOUR_SECONDS,
                Some(Arc::clone(slot)),
            )),
            _ => None,
        };
        let mut own = None;
        if let (Some(frame), Some(raw_id)) = (frame, raw_precipitation_id) {
            if !source.interval_precipitation && index + 1 < per_file.len() {
                if let Some(successor) = frame_of(&per_file[index + 1], raw_id) {
                    let hour = frame.lead_seconds / HOUR_SECONDS;
                    // A run total differences against its predecessor
                    // unconditionally; a window average only against the
                    // previous frame of the same window.
                    let shares = source.accumulated_precipitation
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

#[cfg(test)]
mod grid_family_tests {
    use super::{bundle_grid_family, grid_family_of};
    use crate::encode::registry::Registry;

    /// Which grid each of a source's bundles is built on, from the registry
    /// alone: every CFSv2 pressure-level bundle reads the `pgb` family, every
    /// surface one the primary file, and no bundle of any other source reads
    /// a family at all — the wave companions share their source's grid, so
    /// nothing about them changed.
    #[test]
    fn every_bundle_is_built_on_one_grid() {
        let registry = Registry::embedded();
        let cfs = registry.source("cfs").expect("cfs");
        for bundle_id in ["tmp2m", "prate", "tcdc", "dswrf", "tmpsfc", "icec", "icetk", "wind10m"] {
            assert_eq!(bundle_grid_family(&registry, cfs, bundle_id).expect("one grid"), None, "{bundle_id}");
        }
        for bundle_id in [
            "prmsl", "hgt1000", "hgt850", "hgt700", "hgt500", "hgt200", "tmp1000", "tmp850",
            "tmp700", "tmp500", "tmp250", "tmp200", "vvel500", "wind1000", "wind925", "wind850",
            "wind700", "wind500", "wind250", "wind200", "qflux925", "qflux850", "qflux700",
            "qflux500",
        ] {
            assert_eq!(
                bundle_grid_family(&registry, cfs, bundle_id).expect("one grid"),
                Some("pgb"),
                "{bundle_id}"
            );
        }
        // The vapour flux reads the humidity and the wind pair on its
        // surface, all three from the family, which is what makes it one
        // bundle's worth of one grid.
        for variable_id in &cfs.companion_files[0].variable_ids {
            assert_eq!(grid_family_of(cfs, variable_id), Some("pgb"), "{variable_id}");
        }
        for model in ["gfs", "ecmwf", "ifs", "aifs", "sflux", "hrrr", "gefsaero", "ifshres", "mrms", "mrms3d"] {
            let source = registry.source(model).expect(model);
            for bundle_id in source
                .bundle_scalar_ids
                .iter()
                .chain(&source.bundle_vector_ids)
                .chain(&source.bundle_volume_ids)
            {
                assert_eq!(
                    bundle_grid_family(&registry, source, bundle_id).expect("one grid"),
                    None,
                    "{model} {bundle_id}"
                );
            }
        }
    }
}

#[cfg(test)]
mod composite_tests {
    use crate::encode::registry::Registry;

    #[test]
    fn the_dust_rgb_reads_four_windows_or_three_without_the_11_2_one() {
        // Mirrors `DustRGBProducer.inputs_for`: AHI and ABI carry the 11.2 µm
        // window, FCI does not and the 10.4 µm one stands in — the document's
        // `optionalInputs`, read when the source fetches them.
        let registry = Registry::embedded();
        for model in ["himawari", "himawarijp", "goeseast", "goeswest"] {
            let source = registry.source(model).expect(model);
            assert_eq!(registry.bundle_input_ids(source, "dustrgb"), ["ir086", "ir104", "ir112", "ir123"], "{model}");
        }
        let meteosat = registry.source("meteosat").expect("meteosat");
        assert_eq!(registry.bundle_input_ids(meteosat, "dustrgb"), ["ir086", "ir104", "ir123"]);
        // The components are the same three guns everywhere, and every
        // satellite source publishes ir104 and ir039, then the composite.
        for model in ["himawari", "goeseast", "goeswest", "meteosat"] {
            let source = registry.source(model).expect(model);
            assert_eq!(registry.bundle_variable_ids("dustrgb"), ["dustr", "dustg", "dustb"]);
            assert!(registry.published_bundle_ids(source).starts_with(&["ir104", "ir039", "dustrgb"]), "{model}");
        }
    }

    #[test]
    fn the_debra_confidence_reads_five_windows_and_is_one_variable() {
        // The DEBRA confidence has no stand-in: the same five inputs on
        // every platform, and a source that fetches fewer (Meteosat) does
        // not publish it. The ZHOUYE confidence is composed in the same
        // pass from the same five, its own bundle of one variable after it.
        let registry = Registry::embedded();
        assert_eq!(registry.bundle_variable_ids("dustcf"), ["dustcf"]);
        assert_eq!(registry.bundle_variable_ids("zhouye"), ["zhouye"]);
        for model in ["himawari", "goeseast", "goeswest"] {
            let source = registry.source(model).expect(model);
            for bundle_id in ["dustcf", "zhouye"] {
                assert_eq!(
                    registry.bundle_input_ids(source, bundle_id),
                    ["ir039", "wv062", "ir086", "ir104", "ir123"],
                    "{model} {bundle_id}"
                );
            }
            assert_eq!(
                registry.published_bundle_ids(source),
                ["ir104", "ir039", "dustrgb", "dustcf", "zhouye", "truecolor"],
                "{model}"
            );
        }
        let meteosat = registry.source("meteosat").expect("meteosat");
        assert_eq!(registry.published_bundle_ids(meteosat), ["ir104", "ir039", "dustrgb", "truecolor"]);
    }

    #[test]
    fn the_true_colour_reads_four_bands_or_three_without_the_green_one() {
        // Mirrors `TrueColorProducer.inputs_for`: the AHI has a 0.51 µm
        // band, the ABI does not and its green is synthesized from the
        // other three; the FCI has one, so Meteosat reads the four.
        let registry = Registry::embedded();
        assert_eq!(registry.bundle_variable_ids("truecolor"), ["truer", "trueg", "trueb"]);
        for model in ["himawari", "himawarijp", "meteosat"] {
            let source = registry.source(model).expect(model);
            assert_eq!(
                registry.bundle_input_ids(source, "truecolor"),
                ["vis047", "vis051", "vis064", "nir086", "ir039", "ir104"],
                "{model}"
            );
        }
        assert_eq!(
            registry.published_bundle_ids(registry.source("himawarijp").expect("himawarijp")),
            ["ir104", "ir039", "dustrgb", "truecolor"]
        );
        for model in ["goeseast", "goeswest"] {
            let source = registry.source(model).expect(model);
            assert_eq!(registry.bundle_input_ids(source, "truecolor"), ["vis047", "vis064", "nir086", "ir039", "ir104"], "{model}");
        }
        // Every satellite source fetches what the true colour reads.
        for source in registry.sources().filter(|source| source.bundle_composite_ids.iter().any(|id| id == "truecolor")) {
            for input in registry.bundle_input_ids(source, "truecolor") {
                assert!(source.has_input(&input), "{} {input}", source.id);
            }
        }
        assert!(registry.composite_components("nightmicro").is_none());
    }
}

#[cfg(test)]
mod volume_tests {
    use crate::encode::registry::Registry;

    /// The `refl3d` volume carries the 33 levels bottom to top, reads each
    /// from its own record, publishes after every other kind of bundle and
    /// stacks every level RAW, as the composite does.
    #[test]
    fn the_reflectivity_volume_is_its_33_levels_in_order() {
        let registry = Registry::embedded();
        let source = registry.source("mrms3d").expect("mrms3d");
        let levels = registry.volume_components("refl3d").expect("a volume");
        assert_eq!(levels.len(), 33);
        assert_eq!((levels[0].as_str(), levels[32].as_str()), ("refl500", "refl19000"));
        assert_eq!(registry.bundle_variable_ids("refl3d"), levels);
        assert_eq!(registry.bundle_input_ids(source, "refl3d"), levels);
        assert_eq!(registry.published_bundle_ids(source), ["refl3d"]);
        for variable_id in levels {
            assert!(registry.is_raw(variable_id).unwrap(), "{variable_id}");
        }
        for variable_id in ["prate", "cref", "ptype", "cat300"] {
            assert!(registry.is_raw(variable_id).unwrap(), "{variable_id}");
        }
        for variable_id in ["tmp2m", "ir104", "clw1500"] {
            assert!(!registry.is_raw(variable_id).unwrap(), "{variable_id}");
        }
        assert!(registry.is_raw("refl600").is_err());
        // No other source publishes one.
        for model in ["gfs", "ecmwf", "mrms", "himawari", "cma", "woof"] {
            assert!(
                !registry.published_bundle_ids(registry.source(model).expect(model)).contains(&"refl3d"),
                "{model}"
            );
        }
    }

    /// The `cloud3d` volume carries the 24 cloud water levels bottom to
    /// top, reads each from its own series and publishes after the nest's
    /// scalars and its wind.
    #[test]
    fn the_cloud_water_volume_is_its_24_levels_in_order() {
        let registry = Registry::embedded();
        let source = registry.source("woof").expect("woof");
        let levels = registry.volume_components("cloud3d").expect("a volume");
        assert_eq!(levels.len(), 24);
        assert_eq!((levels[0].as_str(), levels[23].as_str()), ("clw250", "clw12000"));
        assert_eq!(registry.bundle_input_ids(source, "cloud3d"), levels);
        assert_eq!(
            registry.published_bundle_ids(source),
            [
                "tmp2m", "prate", "tmpsfc", "dpt2m", "tcdc", "lcdc", "mcdc", "hcdc", "hpbl",
                "dswrf", "orog", "prmsl", "wind10m", "wind80m", "cloud3d",
            ]
        );
        for model in ["gfs", "ecmwf", "hrrr", "ifshres", "mrms3d", "himawari"] {
            assert!(
                !registry.published_bundle_ids(registry.source(model).expect(model)).contains(&"cloud3d"),
                "{model}"
            );
        }
    }
}

#[cfg(test)]
mod store_dimension_tests {
    use super::store_dimensions;
    use crate::encode::registry::Registry;

    fn names(model: &str, bundle_id: &str) -> [String; 3] {
        let registry = Registry::embedded();
        let source = registry.source(model).expect("source");
        store_dimensions(&registry, source, bundle_id).expect("dimensions")
    }

    /// The reference's `store_dimensions` over the same registry: the run's
    /// axis and grid for most bundles, a time of its own where a rule cuts
    /// the axis (a rate without its analysis frame, an interval maximum, a
    /// static field), and a grid family's pair for the CFSv2 pressure levels.
    #[test]
    fn a_bundle_takes_its_own_names_only_where_a_rule_cuts_it() {
        assert_eq!(names("gfs", "tmp2m"), ["time", "latitude", "longitude"]);
        assert_eq!(names("gfs", "wind10m"), ["time", "latitude", "longitude"]);
        assert_eq!(names("gfs", "prate"), ["time", "latitude", "longitude"]);
        assert_eq!(names("gfs", "orog"), ["time_orog", "latitude", "longitude"]);
        assert_eq!(names("ecmwf", "prate"), ["time_prate", "latitude", "longitude"]);
        assert_eq!(names("ecmwf", "gust"), ["time_gust", "latitude", "longitude"]);
        assert_eq!(names("ifshres", "dswrf"), ["time_dswrf", "latitude", "longitude"]);
        assert_eq!(names("cfs", "tmp2m"), ["time", "latitude", "longitude"]);
        assert_eq!(names("cfs", "hgt500"), ["time", "latitude_pgb", "longitude_pgb"]);
        assert_eq!(names("cfs", "wind850"), ["time", "latitude_pgb", "longitude_pgb"]);
    }
}
