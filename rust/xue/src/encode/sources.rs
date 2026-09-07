//! Per-model source registry — the port of `xue/sources.py`.
//!
//! The models share one output contract: whatever the source, the bundles
//! carry the same data variable ids, so the decoder and frontend never care
//! which model produced them. Not every source is a forecast: an
//! `observation` source (the CMA radar mosaic) is a local file holding a
//! series of observed analyses, with no cycle to fetch and an axis that is
//! whatever times the file carries.

use crate::encode::errors::{EncodeError, Result};

#[derive(Debug, Clone, Copy)]
pub struct SourceSpec {
    /// CLI / URL / directory id.
    pub id: &'static str,
    /// The manifest and bundle-metadata `model` string.
    pub manifest_model: &'static str,
    /// The manifest `product` string.
    pub product: &'static str,
    /// Per-model mutable live pointer at the data root; `None` for a source
    /// with no live feed.
    pub latest_filename: Option<&'static str>,
    /// The published time axis as `(last_hour, step_hours)` segments.
    pub steps: &'static [(i64, i64)],
    /// Variables fetched from the source, in GRIB assembly order.
    pub input_variable_ids: &'static [&'static str],
    /// True when precipitation arrives as a run-total accumulation (ECMWF
    /// `tp`, metres) and must be de-accumulated into a rate.
    pub accumulated_precipitation: bool,
    /// True when precipitation arrives as an interval-averaged rate whose
    /// window resets every `average_window_hours` (GFS sflux `PRATE ave`).
    pub averaged_precipitation: bool,
    pub average_window_hours: i64,
    /// Input variables absent from the analysis (f000) file.
    pub optional_at_analysis: &'static [&'static str],
    /// Scalar variables published as single-variable bundles, in manifest
    /// order (the wind pair always ships as the combined wind10m bundle).
    pub bundle_scalar_ids: &'static [&'static str],
    /// Grid a complete (`require_complete`) build must arrive on.
    pub production_grid: (usize, usize),
    /// True for a source that is not a forecast at all: one local file holding
    /// a series of observed analyses, read through `observation.rs`.
    pub observation: bool,
}

impl SourceSpec {
    /// Whether the source has a live feed to fetch and point at.
    pub fn live(&self) -> bool {
        self.latest_filename.is_some()
    }

    /// The published axis from the analysis through `last_hour`.
    ///
    /// `last_hour` must itself lie on the axis — a cap that lands between
    /// steps (or beyond the published range) has no complete final frame and
    /// is rejected outright.
    pub fn forecast_hours(&self, last_hour: i64) -> Result<Vec<i64>> {
        if self.observation {
            return Err(EncodeError::conversion(format!(
                "{} is an observation source and publishes no forecast axis",
                self.manifest_model
            )));
        }
        let mut hours = vec![0i64];
        for &(boundary, step) in self.steps {
            while *hours.last().expect("non-empty") < boundary.min(last_hour) {
                hours.push(hours.last().expect("non-empty") + step);
            }
            if *hours.last().expect("non-empty") >= last_hour {
                break;
            }
        }
        if *hours.last().expect("non-empty") != last_hour {
            let published = self
                .steps
                .iter()
                .map(|(boundary, step)| format!("{step}-hourly to f{boundary:03}"))
                .collect::<Vec<_>>()
                .join(", then ");
            return Err(EncodeError::conversion(format!(
                "forecast hour {last_hour} is not on the {} axis ({published})",
                self.manifest_model
            )));
        }
        Ok(hours)
    }
}

pub const SOURCES: &[SourceSpec] = &[
    SourceSpec {
        id: "gfs",
        manifest_model: "GFS",
        product: "pgrb2.0p25",
        latest_filename: Some("latest.json"),
        // Hourly through f120, then three-hourly through f240.
        steps: &[(120, 1), (240, 3)],
        input_variable_ids: &["tmp2m", "prate", "ugrd10m", "vgrd10m"],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &["tmp2m", "prate"],
        production_grid: (1440, 721),
        observation: false,
    },
    SourceSpec {
        id: "ecmwf",
        manifest_model: "ECMWF",
        product: "ifs-0p25",
        latest_filename: Some("latest-ecmwf.json"),
        // Three-hourly through 144 hours, then six-hourly through 240.
        steps: &[(144, 3), (240, 6)],
        input_variable_ids: &["tmp2m", "tp", "ugrd10m", "vgrd10m"],
        accumulated_precipitation: true,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &["tmp2m", "prate"],
        production_grid: (1440, 721),
        observation: false,
    },
    // GFS surface flux files on the native ~13 km T1534 Gaussian grid. Adds
    // the dswrf layer; prate is de-averaged from window-cumulative averages.
    SourceSpec {
        id: "sflux",
        manifest_model: "GFS-SFLUX",
        product: "sfluxgrb",
        latest_filename: Some("latest-sflux.json"),
        steps: &[(120, 1), (240, 3)],
        input_variable_ids: &["tmp2m", "prate_ave", "ugrd10m", "vgrd10m", "dswrf"],
        accumulated_precipitation: false,
        averaged_precipitation: true,
        average_window_hours: 6,
        optional_at_analysis: &["prate_ave"],
        bundle_scalar_ids: &["tmp2m", "prate", "dswrf"],
        production_grid: (3072, 1536),
        observation: false,
    },
    // CMA weather radar level-3 mosaic composite reflectivity: an observation
    // source, one local NetCDF file per event.
    SourceSpec {
        id: "radar",
        manifest_model: "CMA-RADAR",
        product: "l3-mst-cref",
        latest_filename: None,
        steps: &[],
        input_variable_ids: &["cref"],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &["cref"],
        // Tile-grid dependent: the file says what it covers, and nothing here
        // is ever built with require_complete.
        production_grid: (0, 0),
        observation: true,
    },
];

pub fn source_spec(model: &str) -> Result<&'static SourceSpec> {
    SOURCES
        .iter()
        .find(|source| source.id == model)
        .ok_or_else(|| EncodeError::conversion(format!("unsupported model: {model}")))
}
