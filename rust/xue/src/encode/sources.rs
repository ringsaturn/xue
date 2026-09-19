//! Per-model source registry — the port of `xuebuild/sources.py`.
//!
//! The models share one output contract: whatever the source, the bundles
//! carry the same data variable ids, so the decoder and frontend never care
//! which model produced them. Not every source is a forecast: an
//! `observation` source holds a series of observed analyses with no cycle
//! and an axis that is whatever times the observations carry — the NOAA
//! MRMS mosaic one fetched GRIB per two-minute frame, thinned onto a
//! coarser grid (`Downsample`), the JMA precipitation nowcast and the CMA
//! radar mosaic one fetched NetCDF series per window (`series_file`),
//! assembled from the agency's tiles by the jma-radar tool and read back
//! out of a daily Zarr archive of the mosaics respectively. A `series_file`
//! source need not be an observation: ECMWF's IFS HRES on its native 9 km
//! grid (`ifshres`) arrives as one NetCDF series per variable too, resampled
//! off the Open-Meteo bucket by the `om2nc` tool, and is an ordinary
//! forecast cycle in every other respect.

use crate::encode::errors::{EncodeError, Result};
use crate::encode::reproject::Regrid;

/// A second file family of the same cycle some of a source's inputs are read
/// from — GFS-Wave beside the pgrb2 atmosphere, ECMWF's `wave` stream beside
/// `oper`. The fetcher (`xuebuild/fetch.py`) appends its records to the
/// frame's GRIB after the primary file's, so the converter still sees one
/// file per frame; the native encoder carries the table so the two
/// registries stay one. Mirrors `CompanionFile` in `xuebuild/sources.py`.
#[derive(Debug, Clone, Copy)]
pub struct CompanionFile {
    /// The family: `wave` for GFS-Wave and for the `wave` stream of ECMWF
    /// open data.
    pub id: &'static str,
    /// Which of the source's `input_variable_ids` come from this family, in
    /// assembly order.
    pub variable_ids: &'static [&'static str],
}

/// The spectral band a satellite image variable was measured in, in the
/// fields of GRIB2 product definition template 4.31: the spacecraft and
/// instrument by their WMO common code table numbers (C-5 and C-8) and the
/// band's central wave number in m⁻¹. Written verbatim as the variable's
/// `band` block (docs/format.md §"Band and Producer"). Mirrors
/// `SatelliteBand` in `xuebuild/sources.py`.
#[derive(Debug, Clone, Copy, PartialEq)]
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
/// describe the thinned grid. Mirrors `Downsample` in `xuebuild/sources.py`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Downsample {
    /// Cells per block along each axis; the source dimensions must divide
    /// by it.
    pub factor: usize,
}

/// Mirrors `SourceSpec` in `xuebuild/sources.py`, field for field but
/// three: its `video` switch is read where ffmpeg runs, and the native
/// encoder writes no video; its `platform` and `grid_step` name the
/// satellite registry the Python fetch stage warps a window through, and
/// the native encoder reads the series that stage wrote.
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
    /// True when precipitation arrives as the total that fell over the
    /// interval since the previous frame (Open-Meteo's `precipitation`, in
    /// millimetres) and becomes a rate by one division
    /// (`convert::interval_rate`). The third arrival shape beside
    /// `accumulated_precipitation` (a run total, differenced) and
    /// `averaged_precipitation` (a window average, de-averaged); a source
    /// declares at most one. Mirrors `interval_precipitation` in
    /// `xuebuild/sources.py`.
    pub interval_precipitation: bool,
    pub average_window_hours: i64,
    /// Input variables absent from the analysis (f000) file.
    pub optional_at_analysis: &'static [&'static str],
    /// Published scalars whose values are a statistic over the step ending
    /// at the frame rather than the instantaneous field their identity
    /// names, with the code table 4.10 process (0 mean, 2 maximum); written
    /// as `typeOfStatisticalProcessing`. Mirrors `statistical_processes` in
    /// `xuebuild/sources.py`.
    pub statistical_processes: &'static [(&'static str, u8)],
    /// Published variables that are satellite image channels, each with the
    /// band it was measured in, written as the variable's `band` block
    /// beside `parameter`. Mirrors `bands` in `xuebuild/sources.py`.
    pub bands: &'static [(&'static str, SatelliteBand)],
    /// Scalar variables published as single-variable bundles, in manifest
    /// order.
    pub bundle_scalar_ids: &'static [&'static str],
    /// The bundles a complete run of this source must publish — what a
    /// live manifest is refused without (`manifest.rs`): the temperature
    /// and precipitation pair on a forecast, the composite reflectivity on
    /// a radar mosaic. Mirrors `core_bundle_ids` in `xuebuild/sources.py`.
    pub core_bundle_ids: &'static [&'static str],
    /// Two-variable bundles published, in manifest order: `wind10m` for the
    /// 10 m pair, `wind<level>` for an isobaric pair, `qflux<level>` for the
    /// water vapour flux the converter derives on that surface. One ships
    /// only when every input it is built from is in `input_variable_ids`.
    pub bundle_vector_ids: &'static [&'static str],
    /// Composite bundles published after the vectors, in manifest order: a
    /// bundle of the variables an algorithm derived from the source's
    /// channels in the fetch stage — `dustrgb`, the Dust RGB's three guns,
    /// and `dustcf`, the DEBRA confidence, a bundle of one variable
    /// (`convert::composite_components`). Listing one publishes it
    /// only when every channel its producer reads is in
    /// `input_variable_ids`; the converter reads the produced components
    /// off the series the fetch stage wrote and never derives them itself.
    /// Mirrors `bundle_composite_ids` in `xuebuild/sources.py`.
    pub bundle_composite_ids: &'static [&'static str],
    /// Grid a complete (`require_complete`) build must arrive on.
    pub production_grid: (usize, usize),
    /// Container v2 tile size as `(width, height)` in grid cells. Mirrors
    /// `SourceSpec.tile` in `xuebuild/sources.py`; the two tables must agree
    /// or the encoders stop being byte-identical.
    pub tile: (usize, usize),
    /// The resolution ladder below the full tier: one reduced variant per
    /// factor, the grid decimated log2(factor) times and named `half` (2),
    /// `quarter` (4) or `eighth` (8). Ascending powers of two, 2 or more;
    /// every forecast and radar source publishes the half tier alone, the
    /// 3000 x 3000 satellite disks three rungs. Mirrors
    /// `SourceSpec.variant_factors` in `xuebuild/sources.py`.
    pub variant_factors: &'static [usize],
    /// True for a source that is not a forecast at all: a series of
    /// observed analyses with no cycle and no lead time. Read from one local
    /// file through `observation.rs` when `window_hours` is `None`, fetched
    /// from a bucket frame by frame otherwise.
    pub observation: bool,
    /// Set for an observation source whose frames are fetched rather than
    /// read from a local file: the length of the window one build takes, in
    /// hours. A run of such a source is the window starting at the run's
    /// hour; the run time is that hour. Mirrors `window_hours` in
    /// `xuebuild/sources.py`.
    pub window_hours: Option<i64>,
    /// For a fetched observation: the product's nominal interval, which
    /// every frame's observation time is snapped *down* to before it becomes
    /// a frame offset (MRMS stamps each two-minute composite some forty
    /// seconds past the mark). Mirrors `cadence_seconds` in
    /// `xuebuild/sources.py`.
    pub cadence_seconds: Option<i64>,
    /// True when a run of the source is one NetCDF file holding the whole
    /// series, one band per time, read through `observation.rs` (the CMA
    /// mosaic's local file; the JMA window the fetch writes), rather than
    /// one GRIB per frame. Orthogonal to `observation`: `ifshres` is a
    /// forecast whose frames arrive this way, so its series carry a run time
    /// and lead times like any cycle's. Orthogonal to `fetched` too. Mirrors
    /// `series_file` in `xuebuild/sources.py`.
    pub series_file: bool,
    /// For a source fetched from the Open-Meteo open data bucket, the model
    /// directory under `data_spatial/` its runs live in (`ecmwf_ifs`). The
    /// Python fetch stage dispatches on it and asks the `om2nc` tool for one
    /// CF NetCDF series per variable, each named with the registry's
    /// `VariableSpec::open_meteo`, which is what this encoder reads the
    /// series by (`observation::series_variable_name`). None for every other
    /// source. Mirrors `open_meteo` in `xuebuild/sources.py`.
    pub open_meteo: Option<&'static str>,
    /// Set when the source is published on a grid coarser than it arrives
    /// on ([`Downsample`]). Mirrors `downsample` in `xuebuild/sources.py`.
    pub downsample: Option<Downsample>,
    /// Set when the source's records are on a map projection rather than a
    /// regular latitude/longitude grid, and must be resampled onto one of
    /// this step before anything else reads them (`reproject.rs`). The grid
    /// the bundles carry is then the regular one; `production_grid` and
    /// `tile` describe it. Mirrors `SourceSpec.regrid` in
    /// `xuebuild/sources.py`.
    pub regrid: Option<Regrid>,
}

impl SourceSpec {
    /// Whether the source has a live feed to fetch and point at.
    pub fn live(&self) -> bool {
        self.latest_filename.is_some()
    }

    /// Whether a run of the source is fetched from a bucket — every
    /// forecast, and an observation with a `window_hours` — as opposed to
    /// read from a local file.
    pub fn fetched(&self) -> bool {
        !self.observation || self.window_hours.is_some()
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
        // The pressure family ships mean sea level pressure and the height
        // on all eight registered isobaric surfaces (1000 / 925 / 850 / 700 /
        // 500 / 300 / 250 / 200 hPa); the upper-air fills the temperature,
        // relative humidity and wind on the same eight — the mandatory
        // levels of a radiosonde ascent, so the skew-T's model column is
        // complete — with the vapour flux derived from the 850 hPa wind and
        // the specific humidity there (fetched as an input only — it also
        // feeds the 850 hPa equivalent potential temperature); then the surface
        // diagnostics and the vertical velocity on three surfaces; then the
        // ocean — the skin temperature and the sea ice fields from pgrb2,
        // the wave fields from the cycle's GFS-Wave file. Mirrors
        // `xuebuild/sources.py`.
        input_variable_ids: &[
            "tmp2m", "prate", "ugrd10m", "vgrd10m", "prmsl", "hgt1000", "hgt925", "hgt850",
            "hgt700", "hgt500", "hgt300", "hgt250", "hgt200", "tmp1000", "tmp925", "tmp850",
            "tmp700", "tmp500", "tmp300", "tmp250", "tmp200", "rh1000", "rh925", "rh850", "rh700",
            "rh500", "rh300", "rh250", "rh200", "spfh850", "ugrd1000", "vgrd1000", "ugrd925",
            "vgrd925", "ugrd850", "vgrd850", "ugrd700", "vgrd700", "ugrd500", "vgrd500", "ugrd300",
            "vgrd300", "ugrd250", "vgrd250", "ugrd200", "vgrd200", "gust", "tcdc", "lcdc", "mcdc",
            "hcdc", "cape", "vis", "dpt2m", "aptmp2m", "vvel850", "vvel700", "vvel500", "tmpsfc",
            "icec", "icetk", "htsgw", "perpw", "dirpw",
        ],
        companion_files: &[CompanionFile {
            id: "wave",
            variable_ids: &["htsgw", "perpw", "dirpw"],
        }],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        bands: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt1000", "hgt925", "hgt850", "hgt700", "hgt500", "hgt300",
            "hgt250", "hgt200", "tmp1000", "tmp925", "tmp850", "tmp700", "tmp500", "tmp300",
            "tmp250", "tmp200", "rh1000", "rh925", "rh850", "rh700", "rh500", "rh300", "rh250",
            "rh200", "gust", "tcdc", "lcdc", "mcdc", "hcdc", "cape", "vis", "dpt2m", "aptmp2m",
            "vvel850", "vvel700", "vvel500", "thetae850", "tmpsfc", "icec", "icetk", "htsgw",
            "perpw",
        ],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &[
            "wind10m", "wind1000", "wind925", "wind850", "wind700", "wind500", "wind300", "wind250",
            "wind200", "qflux850", "wave",
        ],
        bundle_composite_ids: &[],
        production_grid: (1440, 721),
        tile: (48, 52),
        variant_factors: &[2],
        regrid: None,
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: false,
        open_meteo: None,
        downsample: None,
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
        // Then as much of the GFS surface, vertical-velocity and ocean set
        // as the open data carries, each under its GFS identity through the
        // registry's alternates: the interval-maximum gust (all zeros at the
        // analysis, hence optional there), the total cloud cover as a
        // fraction, the most-unstable CAPE, the 2 m dew point, the vertical
        // velocity on three surfaces, the skin temperature and the sea ice
        // thickness, and from the cycle's `wave` stream the wave height,
        // peak period and mean direction — the direction an input only,
        // carried by the derived wave vector. Mirrors `xuebuild/sources.py`.
        input_variable_ids: &[
            "tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "hgt1000", "hgt925", "hgt850", "hgt700",
            "hgt500", "hgt300", "hgt250", "hgt200", "tmp1000", "tmp925", "tmp850", "tmp700",
            "tmp500", "tmp300", "tmp250", "tmp200", "rh1000", "rh925", "rh850", "rh700", "rh500",
            "rh300", "rh250", "rh200", "spfh850", "ugrd1000", "vgrd1000", "ugrd925", "vgrd925",
            "ugrd850", "vgrd850", "ugrd700", "vgrd700", "ugrd500", "vgrd500", "ugrd300", "vgrd300",
            "ugrd250", "vgrd250", "ugrd200", "vgrd200", "gust", "tcdc", "cape", "dpt2m", "vvel850",
            "vvel700", "vvel500", "tmpsfc", "icetk", "htsgw", "perpw", "dirpw",
        ],
        companion_files: &[CompanionFile {
            id: "wave",
            variable_ids: &["htsgw", "perpw", "dirpw"],
        }],
        accumulated_precipitation: true,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &["gust"],
        statistical_processes: &[("prate", 0), ("gust", 2)],
        bands: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt1000", "hgt925", "hgt850", "hgt700", "hgt500", "hgt300",
            "hgt250", "hgt200", "tmp1000", "tmp925", "tmp850", "tmp700", "tmp500", "tmp300",
            "tmp250", "tmp200", "rh1000", "rh925", "rh850", "rh700", "rh500", "rh300", "rh250",
            "rh200", "gust", "tcdc", "cape", "dpt2m", "vvel850", "vvel700", "vvel500", "thetae850",
            "tmpsfc", "icetk", "htsgw", "perpw",
        ],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &[
            "wind10m", "wind1000", "wind925", "wind850", "wind700", "wind500", "wind300", "wind250",
            "wind200", "qflux850", "wave",
        ],
        bundle_composite_ids: &[],
        production_grid: (1440, 721),
        tile: (48, 52),
        variant_factors: &[2],
        regrid: None,
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: false,
        open_meteo: None,
        downsample: None,
    },
    // ECMWF's data-driven model, AIFS Single, from the same open data
    // service: an `oper` and a `wave` stream on the same 0.25° grid, every
    // cycle six-hourly to 360 hours. What the IFS source publishes less what
    // AIFS does not carry (no isobaric relative humidity, gust, CAPE, sea
    // ice thickness or peak wave period) plus the three layer cloud covers;
    // its `tp`, `tcc` and cloud layers arrive under identities of their own
    // that the registry's alternates accept. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "aifs",
        manifest_model: "AIFS",
        product: "aifs-single-0p25",
        latest_filename: Some("latest-aifs.json"),
        steps: &[(360, 6)],
        input_variable_ids: &[
            "tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "hgt850", "hgt700", "hgt500", "hgt250",
            "tmp925", "tmp850", "tmp500", "spfh850", "ugrd925", "vgrd925", "ugrd850", "vgrd850",
            "ugrd250", "vgrd250", "tcdc", "lcdc", "mcdc", "hcdc", "dpt2m", "vvel850", "vvel700",
            "vvel500", "tmpsfc", "htsgw", "dirpw",
        ],
        companion_files: &[CompanionFile {
            id: "wave",
            variable_ids: &["htsgw", "dirpw"],
        }],
        accumulated_precipitation: true,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[("prate", 0)],
        bands: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt850", "hgt700", "hgt500", "hgt250", "tmp925", "tmp850",
            "tmp500", "tcdc", "lcdc", "mcdc", "hcdc", "dpt2m", "vvel850", "vvel700", "vvel500",
            "thetae850", "tmpsfc", "htsgw",
        ],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &["wind10m", "wind925", "wind850", "wind250", "qflux850", "wave"],
        bundle_composite_ids: &[],
        production_grid: (1440, 721),
        tile: (48, 52),
        variant_factors: &[2],
        regrid: None,
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: false,
        open_meteo: None,
        downsample: None,
    },
    // ECMWF IFS HRES, the deterministic high-resolution forecast on its
    // native O1280 grid (~9 km), as Open-Meteo redistributes it: one `.om`
    // file per time step on `s3://openmeteo/data_spatial/ecmwf_ifs/`, every
    // variable of a step in one file. The Python fetch stage's `om2nc` tool
    // reads the byte ranges of the variables asked for, resamples the
    // reduced Gaussian grid onto a regular 0.1° one by nearest neighbour and
    // writes one CF NetCDF series per variable, so this is a `series_file`
    // source (the first that is not an observation) and neither encoder ever
    // learns the Gaussian arithmetic. Two cycles a day (00Z and 12Z,
    // `cycle_hours` 12 on the Python side: the 06/18Z cycles reach only 144
    // hours, which one horizon cannot express). The variable set is the GFS
    // surface diagnostics and the sflux radiation, so a layer survives a
    // switch between the models; Open-Meteo carries no pressure levels, no
    // waves and no ice cover, which is what the 0.25° `ecmwf` source keeps
    // providing. Precipitation arrives as the total over the interval since
    // the previous native step (`interval_precipitation`, the registry's
    // `apcp`), the radiation as that interval's mean and the gust as its
    // maximum, which is what `statistical_processes` declares; none of the
    // three exists at the analysis. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "ifshres",
        manifest_model: "ECMWF-HRES",
        product: "ifs-hres-0p1",
        latest_filename: Some("latest-ifshres.json"),
        // Hourly through 90 hours, three-hourly to 144, six-hourly to 360:
        // 145 frames, the run's own native output cadence.
        steps: &[(90, 1), (144, 3), (360, 6)],
        input_variable_ids: &[
            "tmp2m", "apcp", "ugrd10m", "vgrd10m", "dswrf", "prmsl", "gust", "tcdc", "lcdc",
            "mcdc", "hcdc", "cape", "dpt2m", "vis", "tmpsfc", "icetk",
        ],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: true,
        average_window_hours: 6,
        optional_at_analysis: &["apcp", "dswrf", "gust"],
        statistical_processes: &[("prate", 0), ("dswrf", 0), ("gust", 2)],
        bands: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "dswrf", "prmsl", "gust", "tcdc", "lcdc", "mcdc", "hcdc", "cape",
            "vis", "dpt2m", "tmpsfc", "icetk",
        ],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &["wind10m"],
        bundle_composite_ids: &[],
        // The 0.1° global grid om2nc resamples onto: −180 to 179.9 and both
        // poles, the 0.25° grid's shape at two and a half times its step.
        production_grid: (3600, 1801),
        // 90 x 95 cells is 9° x 9.5° — 40 x 19 = 760 tiles, the last row 91
        // cells high (1801 = 18 x 95 + 91). The tidy divisors of 1800 leave a
        // one-row tile at the south pole instead, which is why the height is
        // not one of them.
        tile: (90, 95),
        // Two rungs: a full plane is 6.5 M cells, past the shell's frame
        // budget, so a view of the whole world plays the half (1800 x 901,
        // about a full GFS plane) or the quarter on a small device.
        variant_factors: &[2, 4],
        regrid: None,
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: true,
        open_meteo: Some("ecmwf_ifs"),
        downsample: None,
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
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &["prate_ave"],
        statistical_processes: &[("prate", 0)],
        bands: &[],
        bundle_scalar_ids: &["tmp2m", "prate", "dswrf"],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &["wind10m"],
        bundle_composite_ids: &[],
        production_grid: (3072, 1536),
        tile: (96, 96),
        variant_factors: &[2],
        regrid: None,
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: false,
        open_meteo: None,
        downsample: None,
    },
    // NOAA HRRR: the 3 km convection-allowing model over the contiguous
    // United States, a cycle every hour, hourly to F18. Computed on a
    // Lambert conformal conic grid (1799 x 1059), so every plane is
    // resampled onto a regular 0.03° grid over the domain's footprint and
    // the bundles carry that grid. Read from the 2-D surface file, which
    // carries no 250 hPa height and no isobaric humidity; its sea level
    // pressure is the MAPS reduction (`MSLMA`, the registry's 0/3/198
    // alias) and its composite reflectivity is published under the radar
    // mosaic's `cref`. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "hrrr",
        manifest_model: "HRRR",
        product: "wrfsfc",
        latest_filename: Some("latest-hrrr.json"),
        steps: &[(18, 1)],
        input_variable_ids: &[
            "tmp2m", "prate", "ugrd10m", "vgrd10m", "prmsl", "hgt850", "hgt700", "hgt500",
            "tmp925", "tmp850", "tmp500", "ugrd925", "vgrd925", "ugrd850", "vgrd850", "ugrd250",
            "vgrd250", "gust", "tcdc", "lcdc", "mcdc", "hcdc", "cape", "vis", "dpt2m", "cref",
        ],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        bands: &[],
        bundle_scalar_ids: &[
            "tmp2m", "prate", "prmsl", "hgt850", "hgt700", "hgt500", "tmp925", "tmp850", "tmp500",
            "gust", "tcdc", "lcdc", "mcdc", "hcdc", "cape", "vis", "dpt2m", "cref",
        ],
        core_bundle_ids: &["tmp2m", "prate"],
        bundle_vector_ids: &["wind10m", "wind925", "wind850", "wind250"],
        bundle_composite_ids: &[],
        // The 0.03° grid over the footprint of the 1799 x 1059 domain, from
        // 134.10 W, 52.62 N to 60.90 W, 21.12 N.
        production_grid: (2441, 1051),
        tile: (64, 64),
        variant_factors: &[2],
        regrid: Some(Regrid { step: 0.03 }),
        observation: false,
        window_hours: None,
        cadence_seconds: None,
        series_file: false,
        open_meteo: None,
        downsample: None,
    },
    // CMA weather radar level-3 mosaic composite reflectivity: the national
    // composite every six minutes, kept as one Zarr store per UTC day in a
    // private archive and read back a window at a time as one NetCDF
    // series (xuebuild/cmaarchive.py). An observation
    // source, live like MRMS and JMA: the window's first hour is the run
    // and the six-minute slots the axis (`cadence_seconds`); a showcase case
    // may still be built from a local file; the id changed from `radar`
    // with the shape, so a wheel that knows one is never taken for the
    // other. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "cma",
        manifest_model: "CMA-RADAR",
        product: "l3-mst-cref",
        latest_filename: Some("latest-cma.json"),
        steps: &[],
        input_variable_ids: &["cref"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        bands: &[],
        bundle_scalar_ids: &["cref"],
        core_bundle_ids: &["cref"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &[],
        // The zoom-5 tile grid over the archive's bbox: 0.0439° cells from
        // 67.5E to 146.25E and 56.25N to 11.25N.
        production_grid: (1792, 1024),
        tile: (64, 64),
        variant_factors: &[2],
        regrid: None,
        observation: true,
        window_hours: Some(3),
        cadence_seconds: Some(360),
        series_file: true,
        open_meteo: None,
        downsample: None,
    },
    // NOAA MRMS: the national radar mosaic over the contiguous United
    // States, a composite every two minutes on a regular 0.01° grid, fetched
    // from its bucket one whole GRIB per product per frame. The composite
    // reflectivity is published under the mosaic's `cref` and the rate under
    // `prate`, each through the registry's alternate for the MRMS-local
    // identity (discipline 209), with the product's sentinels folded to the
    // codebook bottom; the 7000 x 3500 grid is thinned two to one by block
    // maximum onto 0.02°, and the jittered observation times are snapped to
    // the two-minute mark. No live pointer yet: a build names the window's
    // first hour as its run. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "mrms",
        manifest_model: "NOAA-MRMS",
        product: "conus-cref",
        latest_filename: None,
        steps: &[],
        input_variable_ids: &["cref", "prate"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        bands: &[],
        bundle_scalar_ids: &["cref", "prate"],
        core_bundle_ids: &["cref"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &[],
        // The thinned 0.02° grid: 130W to 60W, 55N to 20N.
        production_grid: (3500, 1750),
        tile: (64, 64),
        variant_factors: &[2],
        regrid: None,
        observation: true,
        window_hours: Some(3),
        cadence_seconds: Some(120),
        series_file: false,
        open_meteo: None,
        downsample: Some(Downsample { factor: 2 }),
    },
    // JMA 高解像度降水ナウキャスト: the agency's precipitation intensity
    // analysis over Japan, a frame every five minutes, published as map
    // tiles whose palette encodes ten intensity classes. Not a
    // reflectivity, so published under `prate` at each class's
    // representative rate; the jma-radar tool decodes the zoom-8 tiles onto
    // a regular 0.005° grid (the strongest class in each cell) over the
    // radar coverage envelope and writes a window's frames as one NetCDF
    // series, which is read the way the CMA file is, with the window's
    // first hour as the run and the five-minute slots as the axis
    // (`cadence_seconds`). Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "jma",
        manifest_model: "JMA-HRPNS",
        product: "japan-prate",
        latest_filename: Some("latest-jma.json"),
        steps: &[],
        input_variable_ids: &["prate"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        bands: &[],
        bundle_scalar_ids: &["prate"],
        core_bundle_ids: &["prate"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &[],
        // The 0.005° grid over the coverage envelope: 121E to 149E, 45.5N to
        // 20.5N.
        production_grid: (5600, 5000),
        tile: (128, 128),
        variant_factors: &[2],
        regrid: None,
        observation: true,
        window_hours: Some(3),
        cadence_seconds: Some(300),
        series_file: true,
        open_meteo: None,
        downsample: None,
    },
    // Himawari-9 AHI at 140.7°E as NOAA redistributes it (the ISatSS
    // tiles), warped by the Python fetch stage onto a 0.04° plate carrée
    // grid over the disk's useful extent — 80.7°E to 200.7°E, ±60°, which
    // crosses the antimeridian — and stacked into one NetCDF series per
    // window (`xuebuild/satellite/`), so a satellite source is a
    // `series_file` observation like the JMA nowcast here. The source id is
    // the orbital role; the spacecraft and channel are the `band` block on
    // the variable (`bands`, docs/format.md): WMO C-5 174, C-8 297, AHI
    // band 13 at 10.4073 µm.
    SourceSpec {
        id: "himawari",
        manifest_model: "HIMAWARI",
        product: "ahi-fldk-0p04",
        latest_filename: Some("latest-himawari.json"),
        steps: &[],
        input_variable_ids: &["ir039", "wv062", "ir086", "ir104", "ir112", "ir123"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        // The six infrared windows fetched (AHI bands 7, 8, 11, 13, 14, 15),
        // each with its band block; the central wave numbers are
        // round(1e6 / µm) of the AHI's central wavelengths (3.8853, 6.2429,
        // 8.5926, 10.4073, 11.2395, 12.3806 µm), as `Platform::band`
        // computes them.
        bands: &[
            (
                "ir039",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 257380 },
            ),
            (
                "wv062",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 160182 },
            ),
            (
                "ir086",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 116379 },
            ),
            (
                "ir104",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 96086 },
            ),
            (
                "ir112",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 88972 },
            ),
            (
                "ir123",
                SatelliteBand { satellite_series: 0, satellite_number: 174, instrument_type: 297, central_wavenumber: 80772 },
            ),
        ],
        bundle_scalar_ids: &["ir104"],
        core_bundle_ids: &["ir104"],
        bundle_vector_ids: &[],
        // The Dust RGB, composed per slot in the fetch stage from four of
        // the channels, and the DEBRA confidence from five, each read off
        // the series like any channel.
        bundle_composite_ids: &["dustrgb", "dustcf"],
        // The platform's region at 0.04°: 120° x 120°.
        production_grid: (3000, 3000),
        tile: (64, 64),
        // A 9 M cell disk: the half tier is still twice a GFS plane, so
        // the ladder runs to the eighth (375 x 375).
        variant_factors: &[2, 4, 8],
        regrid: None,
        observation: true,
        window_hours: Some(6),
        cadence_seconds: Some(600),
        series_file: true,
        open_meteo: None,
        downsample: None,
    },
    // The two GOES-R imagers, GOES-19 at 75.2°W (East) and GOES-18 at
    // 137.0°W (West), from NOAA's own buckets: the CMIPF product, each
    // channel of each ten-minute full-disk scan as one calibrated netCDF
    // on the geostationary projection (sweep x), read by the same Python
    // stage through `CMIPFReader`. Each source is the himawari one on its
    // own disk: the same four windows fetched, ir104 and the Dust RGB
    // published (the GOES-R Quick Guide's ABI stretches, picked by the
    // producer by instrument), the same grid step and window. The East
    // disk sits on negative longitudes (−135.2 … −15.2); the West disk
    // crosses the antimeridian and is spelled 163 … 283, the shape
    // Himawari's grid already takes. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "goeseast",
        manifest_model: "GOES-EAST",
        product: "abi-fldk-0p04",
        latest_filename: Some("latest-goeseast.json"),
        steps: &[],
        input_variable_ids: &["ir039", "wv062", "ir086", "ir104", "ir112", "ir123"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        // ABI channels 7, 8, 11, 13, 14, 15 at 3.90, 6.19, 8.50, 10.35,
        // 11.2, 12.3 µm; WMO C-5 273, C-8 617.
        bands: &[
            (
                "ir039",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 256410 },
            ),
            (
                "wv062",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 161551 },
            ),
            (
                "ir086",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 117647 },
            ),
            (
                "ir104",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 96618 },
            ),
            (
                "ir112",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 89286 },
            ),
            (
                "ir123",
                SatelliteBand { satellite_series: 0, satellite_number: 273, instrument_type: 617, central_wavenumber: 81301 },
            ),
        ],
        bundle_scalar_ids: &["ir104"],
        core_bundle_ids: &["ir104"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &["dustrgb", "dustcf"],
        production_grid: (3000, 3000),
        tile: (64, 64),
        variant_factors: &[2, 4, 8],
        regrid: None,
        observation: true,
        window_hours: Some(6),
        cadence_seconds: Some(600),
        series_file: true,
        open_meteo: None,
        downsample: None,
    },
    SourceSpec {
        id: "goeswest",
        manifest_model: "GOES-WEST",
        product: "abi-fldk-0p04",
        latest_filename: Some("latest-goeswest.json"),
        steps: &[],
        input_variable_ids: &["ir039", "wv062", "ir086", "ir104", "ir112", "ir123"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        // ABI channels 7, 8, 11, 13, 14, 15 at 3.90, 6.19, 8.50, 10.35,
        // 11.2, 12.3 µm; WMO C-5 272, C-8 617.
        bands: &[
            (
                "ir039",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 256410 },
            ),
            (
                "wv062",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 161551 },
            ),
            (
                "ir086",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 117647 },
            ),
            (
                "ir104",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 96618 },
            ),
            (
                "ir112",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 89286 },
            ),
            (
                "ir123",
                SatelliteBand { satellite_series: 0, satellite_number: 272, instrument_type: 617, central_wavenumber: 81301 },
            ),
        ],
        bundle_scalar_ids: &["ir104"],
        core_bundle_ids: &["ir104"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &["dustrgb", "dustcf"],
        production_grid: (3000, 3000),
        tile: (64, 64),
        variant_factors: &[2, 4, 8],
        regrid: None,
        observation: true,
        window_hours: Some(6),
        cadence_seconds: Some(600),
        series_file: true,
        open_meteo: None,
        downsample: None,
    },
    // Meteosat-12 (MTG-I1) FCI at 0°, EUMETSAT's prime full-disk service,
    // from the EUMETSAT Data Store rather than a public bucket (the Python
    // stage's `FCIReader` turns each chunk's radiances into brightness
    // temperature and georeferences the strips itself). FCI has no 11.2 µm
    // window, so three channels feed the Dust RGB and the green gun reads
    // 10.5 − 8.7 µm, the original SEVIRI recipe (`composite_input_ids`).
    // The imager scans every ten minutes; this source publishes the cycle
    // on the hour alone (`cadence_seconds` 3600 against the platform's
    // 600), because the EUMETSAT data policy releases the hourly Level 1
    // cycle as Core data under CC-BY-4.0 and keeps the sub-hourly cycles
    // under a licence that forbids operational use within an hour of
    // sensing and redistribution of the numbers after it. A 24-hour window
    // at that cadence is 25 frames. Mirrors `xuebuild/sources.py`.
    SourceSpec {
        id: "meteosat",
        manifest_model: "METEOSAT",
        product: "fci-fldk-0p04",
        latest_filename: Some("latest-meteosat.json"),
        steps: &[],
        input_variable_ids: &["ir086", "ir104", "ir123"],
        companion_files: &[],
        accumulated_precipitation: false,
        averaged_precipitation: false,
        interval_precipitation: false,
        average_window_hours: 6,
        optional_at_analysis: &[],
        statistical_processes: &[],
        // FCI IR_87, IR_105, IR_123 at 8.70, 10.50, 12.30 µm; WMO C-5 71
        // (Meteosat-12), C-8 210 (FCI).
        bands: &[
            (
                "ir086",
                SatelliteBand { satellite_series: 0, satellite_number: 71, instrument_type: 210, central_wavenumber: 114943 },
            ),
            (
                "ir104",
                SatelliteBand { satellite_series: 0, satellite_number: 71, instrument_type: 210, central_wavenumber: 95238 },
            ),
            (
                "ir123",
                SatelliteBand { satellite_series: 0, satellite_number: 71, instrument_type: 210, central_wavenumber: 81301 },
            ),
        ],
        bundle_scalar_ids: &["ir104"],
        core_bundle_ids: &["ir104"],
        bundle_vector_ids: &[],
        bundle_composite_ids: &["dustrgb"],
        production_grid: (3000, 3000),
        tile: (64, 64),
        variant_factors: &[2, 4, 8],
        regrid: None,
        observation: true,
        window_hours: Some(24),
        cadence_seconds: Some(3600),
        series_file: true,
        open_meteo: None,
        downsample: None,
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
    use super::{source_spec, SOURCES};

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
        assert!(source_spec("cma").expect("cma").forecast_hours(1).is_err());
    }

    #[test]
    fn the_series_file_sources_are_the_two_netcdf_ones() {
        // The CMA window is read out of its archive, the JMA window decoded
        // from tiles; both are one NetCDF series per run, fetched and live,
        // read through observation.rs with the window's hour as the run.
        let radar = source_spec("cma").expect("cma");
        let jma = source_spec("jma").expect("jma");
        assert!(radar.series_file && radar.fetched() && radar.live());
        assert!(jma.series_file && jma.fetched() && jma.live());
        assert_eq!(radar.cadence_seconds, Some(360));
        assert_eq!(radar.production_grid, (1792, 1024));
        assert_eq!(radar.core_bundle_ids, &["cref"]);
        assert_eq!(jma.cadence_seconds, Some(300));
        assert_eq!(jma.core_bundle_ids, &["prate"]);
        // The three satellite disks are the same shape on their own grids.
        let himawari = source_spec("himawari").expect("himawari");
        for (model, number) in [("goeseast", 273), ("goeswest", 272)] {
            let source = source_spec(model).expect(model);
            assert!(source.series_file && source.fetched() && source.live(), "{model}");
            assert_eq!(source.input_variable_ids, himawari.input_variable_ids, "{model}");
            assert_eq!(source.bundle_scalar_ids, himawari.bundle_scalar_ids, "{model}");
            assert_eq!(source.bundle_composite_ids, himawari.bundle_composite_ids, "{model}");
            assert_eq!(source.production_grid, himawari.production_grid, "{model}");
            assert_eq!(source.cadence_seconds, Some(600), "{model}");
            assert_eq!(source.bands.len(), 6, "{model}");
            for (_, band) in source.bands {
                assert_eq!((band.satellite_number, band.instrument_type), (number, 617), "{model}");
            }
        }
        // Meteosat publishes the hourly cycle alone (the CC-BY-4.0 one) and
        // has no 11.2 µm window: three inputs, the Dust RGB still resolves
        // to its three components, and the DEBRA confidence — five inputs,
        // no stand-in — is not listed.
        let meteosat = source_spec("meteosat").expect("meteosat");
        assert!(meteosat.series_file && meteosat.fetched() && meteosat.live());
        assert_eq!(meteosat.input_variable_ids, &["ir086", "ir104", "ir123"]);
        assert_eq!(meteosat.bundle_scalar_ids, himawari.bundle_scalar_ids);
        assert_eq!(meteosat.bundle_composite_ids, &["dustrgb"]);
        assert_eq!(meteosat.cadence_seconds, Some(3600));
        assert_eq!(meteosat.window_hours, Some(24));
        assert_eq!(meteosat.bands.len(), 3);
        for (_, band) in meteosat.bands {
            assert_eq!((band.satellite_number, band.instrument_type), (71, 210));
        }
        // The IFS HRES run om2nc resamples off the Open-Meteo bucket is the
        // one series-file source that is not an observation: a cycle with a
        // run time and lead times, whose frames happen to arrive as one
        // NetCDF series per variable.
        let ifshres = source_spec("ifshres").expect("ifshres");
        assert!(ifshres.series_file && ifshres.fetched() && ifshres.live());
        assert!(!ifshres.observation && ifshres.cadence_seconds.is_none());
        assert_eq!(ifshres.open_meteo, Some("ecmwf_ifs"));
        assert!(ifshres.interval_precipitation);
        assert_eq!(ifshres.forecast_hours(3).expect("axis"), vec![0, 1, 2, 3]);
        assert_eq!(ifshres.production_grid, (3600, 1801));
        assert_eq!(ifshres.core_bundle_ids, &["tmp2m", "prate"]);
        // Every other forecast source is read record by record.
        for model in ["gfs", "ecmwf", "aifs", "sflux", "hrrr", "mrms"] {
            assert!(!source_spec(model).expect(model).series_file, "{model}");
        }
        // One arrival shape for precipitation per source.
        for source in SOURCES {
            let shapes = u8::from(source.accumulated_precipitation)
                + u8::from(source.averaged_precipitation)
                + u8::from(source.interval_precipitation);
            assert!(shapes <= 1, "{} declares {shapes} precipitation shapes", source.id);
            assert_eq!(source.open_meteo.is_some(), source.id == "ifshres", "{}", source.id);
        }
    }

    #[test]
    fn the_wave_fields_come_from_the_companion_family() {
        for model in ["gfs", "ecmwf"] {
            let source = source_spec(model).expect(model);
            let [wave] = source.companion_files else { panic!("one companion family") };
            assert_eq!(wave.id, "wave");
            for variable_id in wave.variable_ids {
                assert!(source.input_variable_ids.contains(variable_id), "{variable_id} is fetched");
                // The direction is an input only: it ships inside the wave
                // vector derived from it, not as a scalar of its own.
                assert_eq!(
                    source.bundle_scalar_ids.contains(variable_id),
                    *variable_id != "dirpw",
                    "{model} {variable_id}"
                );
                assert_eq!(source.companion_of(variable_id).map(|c| c.id), Some("wave"));
            }
            assert!(source.bundle_vector_ids.contains(&"wave"));
            assert!(source.companion_of("tmpsfc").is_none());
            // Assembly order: the companion's records come last.
            assert_eq!(&source.input_variable_ids[source.input_variable_ids.len() - 3..], wave.variable_ids);
        }
        for model in ["sflux", "hrrr", "cma"] {
            assert!(source_spec(model).expect(model).companion_files.is_empty(), "{model}");
        }
    }

    /// ECMWF's gust record is all zeros at the analysis and not fetched
    /// there, so the series starts at the first step, the way its
    /// de-accumulated rate does.
    #[test]
    fn the_ecmwf_gust_is_optional_at_the_analysis() {
        let ecmwf = source_spec("ecmwf").expect("ecmwf");
        assert_eq!(ecmwf.optional_at_analysis, &["gust"]);
        assert!(ecmwf.bundle_scalar_ids.contains(&"gust"));
    }

    /// The resolution ladder is powers of two from the half tier down,
    /// strictly ascending, so each rung is the one above decimated once more
    /// and the tier names (`half`, `quarter`, `eighth`) are unambiguous.
    /// Mirrors the test over `SourceSpec.variant_factors` in the Python
    /// suite.
    #[test]
    fn every_ladder_is_ascending_powers_of_two_from_the_half_tier() {
        for source in SOURCES {
            assert!(!source.variant_factors.is_empty(), "{} publishes no ladder", source.id);
            let mut previous = 1;
            for &factor in source.variant_factors {
                assert!(factor >= 2 && factor.is_power_of_two(), "{} factor {factor}", source.id);
                assert!(factor > previous, "{} ladder is not ascending", source.id);
                previous = factor;
            }
        }
        for model in ["himawari", "goeseast", "goeswest", "meteosat"] {
            assert_eq!(source_spec(model).expect(model).variant_factors, &[2, 4, 8], "{model}");
        }
        // A 6.5 M cell global plane takes two rungs, the way a 9 M cell
        // satellite disk takes three.
        assert_eq!(source_spec("ifshres").expect("ifshres").variant_factors, &[2, 4]);
        for model in ["gfs", "ecmwf", "aifs", "sflux", "hrrr", "cma", "mrms", "jma"] {
            assert_eq!(source_spec(model).expect(model).variant_factors, &[2], "{model}");
        }
    }
}
