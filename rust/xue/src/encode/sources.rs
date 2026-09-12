//! Per-model source registry — the port of `xuebuild/sources.py`.
//!
//! The models share one output contract: whatever the source, the bundles
//! carry the same data variable ids, so the decoder and frontend never care
//! which model produced them. Not every source is a forecast: an
//! `observation` source (the CMA radar mosaic) is a local file holding a
//! series of observed analyses, with no cycle to fetch and an axis that is
//! whatever times the file carries.

use crate::encode::errors::{EncodeError, Result};

/// A second file family of the same cycle some of a source's inputs are read
/// from — GFS-Wave beside the pgrb2 atmosphere. The fetcher
/// (`xuebuild/fetch.py`) appends its records to the frame's GRIB after the
/// primary file's, so the converter still sees one file per frame; the
/// native encoder carries the table so the two registries stay one. Mirrors
/// `CompanionFile` in `xuebuild/sources.py`.
#[derive(Debug, Clone, Copy)]
pub struct CompanionFile {
    /// The family: `wave` for GFS-Wave.
    pub id: &'static str,
    /// Which of the source's `input_variable_ids` come from this family, in
    /// assembly order.
    pub variable_ids: &'static [&'static str],
}

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
    /// Variables fetched from the source, in GRIB assembly order: the primary
    /// file's records first, then each companion family's.
    pub input_variable_ids: &'static [&'static str],
    /// Further file families of the same cycle some inputs come from.
    pub companion_files: &'static [CompanionFile],
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
    /// order.
    pub bundle_scalar_ids: &'static [&'static str],
    /// Two-variable bundles published, in manifest order: `wind10m` for the
    /// 10 m pair, `wind<level>` for an isobaric pair, `qflux<level>` for the
    /// water vapour flux the converter derives on that surface. One ships
    /// only when every input it is built from is in `input_variable_ids`.
    pub bundle_vector_ids: &'static [&'static str],
    /// Grid a complete (`require_complete`) build must arrive on.
    pub production_grid: (usize, usize),
    /// Container v2 tile size as `(width, height)` in grid cells. Mirrors
    /// `SourceSpec.tile` in `xuebuild/sources.py`; the two tables must agree
    /// or the encoders stop being byte-identical.
    pub tile: (usize, usize),
    /// True for a source that is not a forecast at all: one local file holding
    /// a series of observed analyses, read through `observation.rs`.
    pub observation: bool,
}

impl SourceSpec {
    /// Whether the source has a live feed to fetch and point at.
    pub fn live(&self) -> bool {
        self.latest_filename.is_some()
    }

    /// The companion family `variable_id` is read from, or `None` for an
    /// input of the primary file.
    pub fn companion_of(&self, variable_id: &str) -> Option<&'static CompanionFile> {
        self.companion_files
            .iter()
            .find(|companion| companion.variable_ids.contains(&variable_id))
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
        // The pressure family ships mean sea level pressure and the four
        // isobaric levels a synoptic chart is read on (850 / 700 / 500 /
        // 250); the upper-air fills the surfaces those charts carry — 925,
        // 850 and 500 hPa temperature, 850, 700 and 500 hPa relative
        // humidity, the 925 and 850 hPa winds with the vapour flux derived
        // from the 850 hPa one and the specific humidity there (fetched as
        // an input only — it also feeds the 850 hPa equivalent potential
        // temperature), and the 250 hPa wind for the jet; then the surface
        // diagnostics and the vertical velocity on three surfaces; then the
        // ocean — the skin temperature and the sea ice fields from pgrb2,
        // the wave fields from the cycle's GFS-Wave file. Mirrors
        // `xuebuild/sources.py`.
        input_variable_ids: &[
            "tmp2m", "prate", "ugrd10m", "vgrd10m", "prmsl", "hgt850", "hgt700", "hgt500",
            "hgt250", "tmp925", "tmp850", "tmp500", "rh850", "rh700", "rh500", "spfh850",
            "ugrd925", "vgrd925", "ugrd850", "vgrd850", "ugrd250", "vgrd250", "gust", "tcdc",
            "lcdc", "mcdc", "hcdc", "cape", "vis", "dpt2m", "aptmp2m", "vvel850", "vvel700",
            "vvel500", "tmpsfc", "icec", "icetk", "htsgw", "perpw", "dirpw",
        ],
        companion_files: &[CompanionFile {
            id: "wave",
            variable_ids: &["htsgw", "perpw", "dirpw"],
        }],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt850", "hgt700", "hgt500", "hgt250", "tmp925", "tmp850",
            "tmp500", "rh850", "rh700", "rh500", "gust", "tcdc", "lcdc", "mcdc", "hcdc", "cape",
            "vis", "dpt2m", "aptmp2m", "vvel850", "vvel700", "vvel500", "thetae850", "tmpsfc",
            "icec", "icetk", "htsgw", "perpw", "dirpw",
        ],
        bundle_vector_ids: &["wind10m", "wind925", "wind850", "wind250", "qflux850", "wave"],
        production_grid: (1440, 721),
        tile: (48, 52),
        observation: false,
    },
    SourceSpec {
        id: "ecmwf",
        manifest_model: "ECMWF",
        product: "ifs-0p25",
        latest_filename: Some("latest-ecmwf.json"),
        // Three-hourly through 144 hours, then six-hourly through 240.
        steps: &[(144, 3), (240, 6)],
        // The same pressure family and upper-air fills as GFS, from the open
        // data pressure-level records, so the two models offer one set of
        // layers. ECMWF `msl` is matched through the registry's 0/3/0 alias.
        input_variable_ids: &[
            "tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "hgt850", "hgt700", "hgt500", "hgt250",
            "tmp925", "tmp850", "tmp500", "rh850", "rh700", "rh500", "spfh850", "ugrd925",
            "vgrd925", "ugrd850", "vgrd850", "ugrd250", "vgrd250",
        ],
        companion_files: &[],
        accumulated_precipitation: true,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt850", "hgt700", "hgt500", "hgt250", "tmp925", "tmp850",
            "tmp500", "rh850", "rh700", "rh500",
        ],
        bundle_vector_ids: &["wind10m", "wind925", "wind850", "wind250", "qflux850"],
        production_grid: (1440, 721),
        tile: (48, 52),
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
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: true,
        average_window_hours: 6,
        optional_at_analysis: &["prate_ave"],
        bundle_scalar_ids: &["tmp2m", "prate", "dswrf"],
        bundle_vector_ids: &["wind10m"],
        production_grid: (3072, 1536),
        tile: (96, 96),
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
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        bundle_scalar_ids: &["cref"],
        bundle_vector_ids: &[],
        // Tile-grid dependent: the file says what it covers, and nothing here
        // is ever built with require_complete.
        production_grid: (0, 0),
        tile: (64, 64),
        observation: true,
    },
];

pub fn source_spec(model: &str) -> Result<&'static SourceSpec> {
    SOURCES
        .iter()
        .find(|source| source.id == model)
        .ok_or_else(|| EncodeError::conversion(format!("unsupported model: {model}")))
}

#[cfg(test)]
mod tests {
    use super::source_spec;

    #[test]
    fn a_cap_must_land_on_the_published_axis() {
        let gfs = source_spec("gfs").expect("gfs");
        assert_eq!(gfs.forecast_hours(3).expect("axis"), vec![0, 1, 2, 3]);
        let long = gfs.forecast_hours(240).expect("axis");
        assert_eq!(long.len(), 161);
        assert_eq!(long[120], 120);
        assert_eq!(long[121], 123);
        // 121 is past the hourly segment and off the three-hourly one.
        assert!(gfs.forecast_hours(121).is_err());
        // An observation source publishes no forecast axis at all.
        assert!(source_spec("radar").expect("radar").forecast_hours(1).is_err());
    }

    #[test]
    fn the_wave_fields_come_from_the_companion_family() {
        let gfs = source_spec("gfs").expect("gfs");
        let [wave] = gfs.companion_files else { panic!("one companion family") };
        assert_eq!(wave.id, "wave");
        for variable_id in wave.variable_ids {
            assert!(gfs.input_variable_ids.contains(variable_id), "{variable_id} is fetched");
            assert!(gfs.bundle_scalar_ids.contains(variable_id), "{variable_id} is published");
            assert_eq!(gfs.companion_of(variable_id).map(|c| c.id), Some("wave"));
        }
        assert!(gfs.companion_of("tmpsfc").is_none());
        // Assembly order: the companion's records come last.
        assert_eq!(&gfs.input_variable_ids[gfs.input_variable_ids.len() - 3..], wave.variable_ids);
        for model in ["ecmwf", "sflux", "radar"] {
            assert!(source_spec(model).expect(model).companion_files.is_empty(), "{model}");
        }
    }
}
