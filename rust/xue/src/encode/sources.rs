//! One source as the registry document describes it (`docs/registry.md`
//! §Sources): every field of `SourceSpec` in `xuebuild/sources.py`, under
//! its camelCase name.
//!
//! The models share one output contract: whatever the source, the bundles
//! carry the same data variable ids, so the decoder and frontend never care
//! which model produced them. Not every source is a forecast: an
//! `observation` source holds a series of observed analyses with no cycle
//! and an axis that is whatever times the observations carry — the NOAA
//! MRMS mosaic one fetched GRIB per two-minute frame, thinned onto a
//! coarser grid (`Downsample`), the JMA precipitation nowcast and the CMA
//! radar mosaic one fetched NetCDF series per window (`series_file`). A
//! `series_file` source need not be an observation: ECMWF's IFS HRES on its
//! native 9 km grid (`ifshres`) arrives as one NetCDF series per variable
//! too, and is an ordinary forecast cycle in every other respect.

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::reproject::Regrid;

/// A second file family of the same cycle some of a source's inputs are read
/// from — GFS-Wave beside the pgrb2 atmosphere, ECMWF's `wave` stream beside
/// `oper`. The fetcher (`xuebuild/fetch.py`) appends its records to the
/// frame's GRIB after the primary file's, so the converter still sees one
/// file per frame.
///
/// A family that declares a [`CompanionFile::production_grid`] is a *grid
/// family* instead: its records are on another grid than the primary
/// file's, and GDAL cannot read a GRIB whose messages disagree on the grid
/// — it exposes every band at the first message's size and warns that the
/// data access may be incomplete, which is a wrong plane rather than an
/// error. So such a family's records are never appended to the frame; they
/// are written to a sibling frame file of the same name under a directory
/// named by the family id (`<run dir>/<id>/<frame file name>`,
/// [`family_frame_path`]), and the converter reads them from there with a
/// `GridInfo` of the family's own.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CompanionFile {
    /// The family: `wave` for GFS-Wave and for the `wave` stream of ECMWF
    /// open data, `pgb` for the CFSv2 pressure-level series.
    pub id: String,
    /// Which of the source's `input_variable_ids` come from this family, in
    /// assembly order.
    pub variable_ids: Vec<String>,
    /// True when the fetch repacks the family's records; read by the fetch
    /// stage alone.
    #[serde(default)]
    pub repack: bool,
    /// Set when the family is on a grid of its own (CFSv2's 1° pressure-level
    /// series beside its T126 Gaussian surface series): the grid a complete
    /// build must find its records on, the way `SourceSpec::production_grid`
    /// is for the primary file. Setting it makes the family a grid family —
    /// its records live in their own frame file (above) and every bundle
    /// built from them carries this grid. `None` for a family on the
    /// source's own grid, which is the shape the wave families have.
    #[serde(default)]
    pub production_grid: Option<(usize, usize)>,
    /// The container v2 tile size the family's bundles are cut with, as
    /// `(width, height)` in cells of *its* grid. Set together with
    /// [`CompanionFile::production_grid`]; `None` on a family that shares the
    /// source's grid and therefore its `SourceSpec::tile`.
    #[serde(default)]
    pub tile: Option<(usize, usize)>,
}

impl CompanionFile {
    /// Whether the family is a grid family: its records are on their own
    /// grid, in their own frame file.
    pub fn own_grid(&self) -> bool {
        self.production_grid.is_some()
    }
}

/// Where one family's records of a frame live: the frame file itself for the
/// primary family (and for a companion that shares the source's grid, whose
/// records are appended to it), a sibling of the same name under a directory
/// named by the family id for a grid family —
/// `cfs.2026091912/pgb/cfs.2026091912.f006.grib2` beside
/// `cfs.2026091912/cfs.2026091912.f006.grib2`.
///
/// The one place the layout is written down on this side: the Python fetch
/// writes it and both converters read it. Mirrors `family_frame_path` in
/// `xuebuild/sources.py`.
pub fn family_frame_path(frame: &Path, family: Option<&str>) -> PathBuf {
    match family {
        None => frame.to_path_buf(),
        Some(family) => {
            let name = frame.file_name().unwrap_or(frame.as_os_str());
            frame
                .parent()
                .unwrap_or(Path::new(""))
                .join(family)
                .join(name)
        }
    }
}

/// The spectral band a satellite image variable was measured in — GRIB2
/// template 4.31's identification of the instrument's channel: the
/// satellite series and number, the instrument type (BUFR code table
/// 0 02 019) and the band's central wave number in m⁻¹. Written verbatim as
/// the variable's `band` block (docs/format.md §"Band and Producer").
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SatelliteBand {
    pub satellite_series: u16,
    pub satellite_number: u16,
    pub instrument_type: u16,
    /// `scaledValueOfCentralWaveNumber`; the scale factor written is 0.
    pub central_wavenumber: u32,
}

impl SatelliteBand {
    pub fn metadata(&self) -> serde_json::Map<String, serde_json::Value> {
        let mut block = serde_json::Map::new();
        block.insert("satelliteSeries".into(), self.satellite_series.into());
        block.insert("satelliteNumber".into(), self.satellite_number.into());
        block.insert("instrumentType".into(), self.instrument_type.into());
        block.insert("scaleFactorOfCentralWaveNumber".into(), 0.into());
        block.insert("scaledValueOfCentralWaveNumber".into(), self.central_wavenumber.into());
        block
    }
}

/// How a source's planes are thinned onto the grid its bundles carry: every
/// `factor` x `factor` block of source cells becomes its **maximum** — a
/// composite reflectivity is already the column maximum, and keeping the
/// strongest return of each block is how a radar product is thinned. Runs
/// after the fill rules and before any crop; `production_grid` and `tile`
/// describe the thinned grid.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
pub struct Downsample {
    /// Cells per block along each axis; the source dimensions must divide
    /// by it.
    pub factor: usize,
}

/// Mirrors `SourceSpec` in `xuebuild/sources.py`, field for field. Some
/// fields are the fetch stage's alone (`platform`, `grid_step`,
/// `fetch_concurrency`, `object_cadence_seconds`, `cycle_hours`, `video`):
/// this encoder carries them and reads none of them.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SourceSpec {
    /// CLI / URL / directory id.
    pub id: String,
    /// The manifest and bundle-metadata `model` string.
    pub manifest_model: String,
    /// The manifest `product` string.
    pub product: String,
    /// Per-model mutable live pointer at the data root; `None` for a source
    /// with no live feed (the WOOF nest, a forecast that exists only as
    /// showcase cases built from a local dataset).
    pub latest_filename: Option<String>,
    /// The published time axis as `(last_hour, step_hours)` segments.
    pub steps: Vec<(i64, i64)>,
    /// Variables fetched from the source, in GRIB assembly order: the primary
    /// file's records first, then each companion family's.
    pub input_variable_ids: Vec<String>,
    /// True when precipitation arrives as a run-total accumulation (ECMWF
    /// `tp`, metres) and must be de-accumulated into a rate.
    pub accumulated_precipitation: bool,
    /// Further file families of the same cycle some inputs come from.
    #[serde(default)]
    pub companion_files: Vec<CompanionFile>,
    /// True when precipitation arrives as an interval-averaged rate whose
    /// window resets every `average_window_hours` (GFS sflux `PRATE ave`).
    #[serde(default)]
    pub averaged_precipitation: bool,
    /// True when precipitation arrives as the total that fell over the
    /// interval since the previous frame (Open-Meteo's `precipitation`, in
    /// millimetres) and becomes a rate by one division
    /// (`convert::interval_rate`). A source declares at most one of the
    /// three arrival shapes.
    #[serde(default)]
    pub interval_precipitation: bool,
    #[serde(default = "default_average_window_hours")]
    pub average_window_hours: i64,
    /// The first forecast hour the source publishes: zero for every source
    /// whose cycle ships an analysis file, 6 for CFSv2, 1 for the WOOF nest.
    /// [`SourceSpec::forecast_hours`] counts from here.
    #[serde(default)]
    pub first_hour: i64,
    /// Input variables absent from the analysis (f000) file.
    #[serde(default)]
    pub optional_at_analysis: Vec<String>,
    /// Published scalars whose values are a statistic over the step ending
    /// at the frame rather than the instantaneous field their identity
    /// names, with the code table 4.10 process (0 mean, 2 maximum); written
    /// as `typeOfStatisticalProcessing`.
    #[serde(default)]
    pub statistical_processes: Vec<(String, u8)>,
    /// Published variables that are satellite image channels, each with the
    /// band it was measured in, written as the variable's `band` block.
    #[serde(default)]
    pub bands: Vec<(String, SatelliteBand)>,
    #[serde(default)]
    pub platform: Option<String>,
    /// For a source fetched from the Open-Meteo open data bucket, the model
    /// directory its runs live in (`ecmwf_ifs`): the series are then read by
    /// the registry's Open-Meteo names (`observation::series_variable_name`).
    #[serde(default)]
    pub open_meteo: Option<String>,
    #[serde(default)]
    pub grid_step: Option<f64>,
    /// For a satellite source that publishes a box smaller than its
    /// platform's disk, the box as `(west, south, east, north)` in degrees.
    #[serde(default)]
    pub region: Option<(f64, f64, f64, f64)>,
    /// Scalar variables published as single-variable bundles, in manifest
    /// order.
    pub bundle_scalar_ids: Vec<String>,
    /// The bundles a complete run of this source must publish — what a
    /// live manifest is refused without (`manifest.rs`).
    pub core_bundle_ids: Vec<String>,
    /// Two-variable bundles published, in manifest order. One ships only
    /// when every input it is built from is in `input_variable_ids`.
    #[serde(default)]
    pub bundle_vector_ids: Vec<String>,
    /// Composite bundles published after the vectors, in manifest order:
    /// the variables an algorithm derived from the source's channels in the
    /// fetch stage, read off the series and never derived here.
    #[serde(default)]
    pub bundle_composite_ids: Vec<String>,
    /// Volume bundles published after the composites, in manifest order:
    /// many variables each read directly from its own record.
    #[serde(default)]
    pub bundle_volume_ids: Vec<String>,
    /// Grid a complete (`require_complete`) build must arrive on.
    pub production_grid: (usize, usize),
    /// Container v2 tile size as `(width, height)` in grid cells.
    pub tile: (usize, usize),
    /// The resolution ladder below the full tier: one reduced variant per
    /// factor, the grid decimated log2(factor) times and named `half` (2),
    /// `quarter` (4) or `eighth` (8). Ascending powers of two, 2 or more.
    #[serde(default)]
    pub variant_factors: Vec<usize>,
    /// The full-resolution bundles that also ship a series store.
    #[serde(default)]
    pub series_bundle_ids: Vec<String>,
    #[serde(default)]
    pub fetch_concurrency: usize,
    /// True for a source that is not a forecast at all: a series of
    /// observed analyses with no cycle and no lead time.
    #[serde(default)]
    pub observation: bool,
    /// Set for an observation source whose frames are fetched rather than
    /// read from a local file: the length of the window one build takes, in
    /// hours.
    #[serde(default)]
    pub window_hours: Option<i64>,
    /// For a fetched observation: the product's nominal interval, which
    /// every frame's observation time is snapped *down* to before it becomes
    /// a frame offset.
    #[serde(default)]
    pub cadence_seconds: Option<i64>,
    #[serde(default)]
    pub object_cadence_seconds: Option<i64>,
    /// True when a run of the source is one NetCDF file holding the whole
    /// series, one band per time, read through `observation.rs`, rather
    /// than one GRIB per frame. Orthogonal to `observation`.
    #[serde(default)]
    pub series_file: bool,
    /// Set when the source is published on a grid coarser than it arrives
    /// on ([`Downsample`]).
    #[serde(default)]
    pub downsample: Option<Downsample>,
    #[serde(default)]
    pub cycle_hours: i64,
    /// The UTC cycle hours that run past the end of `steps` (HRRR's
    /// synoptic cycles run to F48, the others to F18).
    #[serde(default)]
    pub long_cycles: Vec<u32>,
    /// The axis segments, in `steps`'s form, that continue `steps` on a
    /// `long_cycles` cycle.
    #[serde(default)]
    pub long_cycle_steps: Vec<(i64, i64)>,
    /// Set when the source's records are on a map projection rather than a
    /// regular latitude/longitude grid, and must be resampled onto one of
    /// this step before anything else reads them (`reproject.rs`).
    #[serde(default)]
    pub regrid: Option<Regrid>,
    /// Whether the H.264 companions are built; read where ffmpeg runs.
    #[serde(default)]
    pub video: bool,
    /// `(level_hpa, mean, standard deviation)` of ln TI1 (s^-2) on each
    /// surface the source publishes a clear-air turbulence bundle for: the
    /// lognormal fit the index is projected onto the climatological EDR
    /// distribution through (`convert::derive_cat`).
    #[serde(default)]
    pub cat_calibration: Vec<(u32, f64, f64)>,
}

fn default_average_window_hours() -> i64 {
    6
}

impl SourceSpec {
    /// Whether the source has a live feed to fetch and point at.
    pub fn live(&self) -> bool {
        self.latest_filename.is_some()
    }

    /// Whether a run of the source is fetched from a bucket — a forecast
    /// with a live pointer, and an observation with a `window_hours` — as
    /// opposed to read from a local file or directory (the CMA file cases,
    /// the WOOF nest). Mirrors `SourceSpec.fetched` in `xuebuild/sources.py`.
    pub fn fetched(&self) -> bool {
        if self.observation {
            self.window_hours.is_some()
        } else {
            self.live()
        }
    }

    /// Whether `variable_id` is one of the source's inputs.
    pub fn has_input(&self, variable_id: &str) -> bool {
        self.input_variable_ids.iter().any(|id| id == variable_id)
    }

    /// Whether `variable_id` may be absent from the analysis file.
    pub fn is_optional_at_analysis(&self, variable_id: &str) -> bool {
        self.optional_at_analysis.iter().any(|id| id == variable_id)
    }

    /// The companion family `variable_id` is read from, or `None` for an
    /// input of the primary file.
    pub fn companion_of(&self, variable_id: &str) -> Option<&CompanionFile> {
        self.companion_files
            .iter()
            .find(|companion| companion.variable_ids.iter().any(|id| id == variable_id))
    }

    /// The companion families on a grid of their own
    /// ([`CompanionFile::production_grid`]), in declaration order. Empty for
    /// every source but CFSv2. Mirrors `SourceSpec.grid_families`.
    pub fn grid_families(&self) -> impl Iterator<Item = &CompanionFile> {
        self.companion_files
            .iter()
            .filter(|companion| companion.own_grid())
    }

    /// The grid family `variable_id` is read from, or `None` for an input on
    /// the source's own grid — which is every input of every source whose
    /// families share its grid, the wave families included. Mirrors
    /// `SourceSpec.grid_family_of`.
    pub fn grid_family_of(&self, variable_id: &str) -> Option<&CompanionFile> {
        self.companion_of(variable_id)
            .filter(|companion| companion.own_grid())
    }

    /// The `(production_grid, tile)` one family's bundles are built on: the
    /// source's own for the primary family (`None`), the family's for a grid
    /// family. Mirrors `SourceSpec.family_grid`.
    pub fn family_grid(&self, family: Option<&str>) -> Result<((usize, usize), (usize, usize))> {
        let Some(family) = family else {
            return Ok((self.production_grid, self.tile));
        };
        for companion in self.grid_families() {
            if companion.id == family {
                return Ok((
                    companion.production_grid.expect("a grid family has a grid"),
                    companion.tile.expect("a grid family has a tile"),
                ));
            }
        }
        Err(EncodeError::conversion(format!(
            "{} has no {family} grid family",
            self.manifest_model
        )))
    }

    /// The published axis from [`SourceSpec::first_hour`] — the analysis for
    /// every source but CFSv2 — through `last_hour`.
    ///
    /// `last_hour` must itself lie on the axis — a cap that lands between
    /// steps (or beyond the published range) has no complete final frame and
    /// is rejected outright. Past the end of `steps` the axis continues by
    /// `long_cycle_steps` only when `cycle` (the run's UTC hour) is one of
    /// `long_cycles`. Mirrors `forecast_hours` in `xuebuild/sources.py`.
    pub fn forecast_hours(&self, last_hour: i64, cycle: Option<u32>) -> Result<Vec<i64>> {
        if self.observation {
            return Err(EncodeError::conversion(format!(
                "{} is an observation source and publishes no forecast axis",
                self.manifest_model
            )));
        }
        let long = cycle.is_some_and(|cycle| self.long_cycles.contains(&cycle))
            && self.steps.last().is_some_and(|&(boundary, _)| last_hour > boundary);
        let mut hours = vec![self.first_hour];
        let extension: &[(i64, i64)] = if long { &self.long_cycle_steps } else { &[] };
        for &(boundary, step) in self.steps.iter().chain(extension) {
            while *hours.last().expect("non-empty") < boundary.min(last_hour) {
                hours.push(hours.last().expect("non-empty") + step);
            }
            if *hours.last().expect("non-empty") >= last_hour {
                break;
            }
        }
        if *hours.last().expect("non-empty") != last_hour {
            let segments = |steps: &[(i64, i64)]| {
                steps
                    .iter()
                    .map(|(boundary, step)| format!("{step}-hourly to f{boundary:03}"))
                    .collect::<Vec<_>>()
                    .join(", then ")
            };
            let mut published = segments(&self.steps);
            if self.first_hour != 0 {
                published = format!("from f{:03}, {published}", self.first_hour);
            }
            if !self.long_cycles.is_empty() {
                let cycles = self
                    .long_cycles
                    .iter()
                    .map(|hour| format!("{hour:02}"))
                    .collect::<Vec<_>>()
                    .join("/");
                published = format!(
                    "{published}; {} from the {cycles} UTC cycles",
                    segments(&self.long_cycle_steps)
                );
            }
            return Err(EncodeError::conversion(format!(
                "forecast hour {last_hour} is not on the {} axis ({published})",
                self.manifest_model
            )));
        }
        Ok(hours)
    }
}

#[cfg(test)]
mod tests {
    use super::{family_frame_path, Path};
    use crate::encode::registry::Registry;

    #[test]
    fn a_cap_must_land_on_the_published_axis() {
        let registry = Registry::embedded();
        let gfs = registry.source("gfs").unwrap();
        assert_eq!(gfs.forecast_hours(3, None).unwrap(), [0, 1, 2, 3]);
        let axis = gfs.forecast_hours(240, None).unwrap();
        assert_eq!(axis.len(), 121 + 40);
        assert_eq!(axis[120..123], [120, 123, 126]);
        let error = gfs.forecast_hours(241, None).unwrap_err().to_string();
        assert!(error.contains("241") && error.contains("GFS"), "{error}");
        assert!(gfs.forecast_hours(122, None).is_err());
        assert!(registry.source("mrms").unwrap().forecast_hours(1, None).is_err());
    }

    #[test]
    fn hrrr_reaches_f048_only_from_a_synoptic_cycle() {
        let registry = Registry::embedded();
        let hrrr = registry.source("hrrr").unwrap();
        assert_eq!(hrrr.forecast_hours(18, Some(7)).unwrap().len(), 19);
        assert_eq!(hrrr.forecast_hours(48, Some(6)).unwrap().len(), 49);
        let error = hrrr.forecast_hours(48, Some(7)).unwrap_err().to_string();
        assert!(error.contains("00/06/12/18"), "{error}");
    }

    #[test]
    fn the_cfs_axis_starts_at_its_first_step() {
        let registry = Registry::embedded();
        let cfs = registry.source("cfs").unwrap();
        assert_eq!(cfs.first_hour, 6);
        assert_eq!(cfs.forecast_hours(18, None).unwrap(), [6, 12, 18]);
        let error = cfs.forecast_hours(0, None).unwrap_err().to_string();
        assert!(error.contains("from f006"), "{error}");
        // The pressure levels are a grid family of their own; the wave
        // records of GFS share its grid.
        assert_eq!(cfs.grid_families().map(|family| family.id.as_str()).collect::<Vec<_>>(), ["pgb"]);
        assert_eq!(cfs.family_grid(Some("pgb")).unwrap(), ((360, 181), (90, 91)));
        assert_eq!(cfs.family_grid(None).unwrap(), (cfs.production_grid, cfs.tile));
        assert!(cfs.family_grid(Some("wave")).is_err());
        assert_eq!(cfs.grid_family_of("hgt500").map(|family| family.id.as_str()), Some("pgb"));
        assert_eq!(cfs.grid_family_of("tmp2m"), None);
        let gfs = registry.source("gfs").unwrap();
        assert_eq!(gfs.grid_families().count(), 0);
        assert!(gfs.companion_of("htsgw").is_some() && gfs.grid_family_of("htsgw").is_none());
        assert!(gfs.fetched() && !registry.source("woof").unwrap().fetched());
        assert!(registry.source("mrms").unwrap().fetched() && !registry.source("cma").unwrap().fetched());
    }

    #[test]
    fn a_family_s_records_live_in_a_sibling_frame_file() {
        let frame = Path::new("data/raw/cfs.2026091912/cfs.2026091912.f006.grib2");
        assert_eq!(family_frame_path(frame, None), frame);
        assert_eq!(
            family_frame_path(frame, Some("pgb")),
            Path::new("data/raw/cfs.2026091912/pgb/cfs.2026091912.f006.grib2")
        );
    }

    #[test]
    fn every_ladder_is_ascending_powers_of_two_from_the_half_tier() {
        for source in Registry::embedded().sources() {
            let factors = &source.variant_factors;
            assert!(factors.windows(2).all(|pair| pair[0] < pair[1]), "{}", source.id);
            assert!(
                factors.iter().all(|factor| factor.is_power_of_two() && *factor >= 2),
                "{}",
                source.id
            );
        }
    }
}
