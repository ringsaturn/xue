//! The shapes shared by every input path — the port of `xue/model.py`.

use std::path::PathBuf;

use time::OffsetDateTime;

/// One variable at one time, located in a file GDAL can open: a GRIB2 record,
/// or a band of a NetCDF observation series.
#[derive(Debug, Clone)]
pub struct SourceFrame {
    pub path: PathBuf,
    pub band: usize,
    pub variable_id: String,
    pub run_time: OffsetDateTime,
    pub valid_time: OffsetDateTime,
    /// Seconds from `run_time`. A GRIB record's lead time is always a whole
    /// hour; an observation series can be finer (the radar mosaic is six
    /// minutes), and the encoder derives the bundle's axis unit from these.
    pub lead_seconds: i64,
    pub unit: String,
}

/// How the extraction must read one file's bands.
///
/// GRIB records arrive already in physical units with every point valid, so
/// the defaults do nothing. A packed NetCDF observation file needs its
/// `scale_factor`/`add_offset` applied and its fill value turned into a real
/// number the codebook can quantize — the format carries no bitmap, so missing
/// data has to become a value.
#[derive(Debug, Clone, Default)]
pub struct PlaneSource {
    pub unscale: bool,
    /// Values marking missing data in the extracted plane. GDAL passes a
    /// band's nodata value through unscaling untouched, so the fill can arrive
    /// either raw or scaled; both are listed, and neither is a value the
    /// variable can physically take.
    pub fill_values: Vec<f64>,
    /// What missing data becomes; the bottom of the variable's codebook.
    pub fill_replacement: f64,
}

impl PlaneSource {
    pub fn grib() -> Self {
        Self::default()
    }

    pub fn apply_fill(&self, values: &mut [f64]) {
        if self.fill_values.is_empty() {
            return;
        }
        for value in values.iter_mut() {
            // Scaling is float arithmetic, so match with a relative tolerance
            // rather than for equality (numpy.isclose semantics).
            if self
                .fill_values
                .iter()
                .any(|fill| (*value - fill).abs() <= 1e-6 + 1e-6 * fill.abs())
            {
                *value = self.fill_replacement;
            }
        }
    }
}
