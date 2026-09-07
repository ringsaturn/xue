//! Gridded observation series: a NetCDF file as a run of Xue frames — the port
//! of `xue/observation.py`.
//!
//! A forecast source is a cycle on a bucket, fetched one record per forecast
//! hour. An observation source is the opposite shape: one local file that
//! already holds the whole series, one band per time. Two things differ from
//! GRIB and are carried in the returned [`PlaneSource`]: the values are
//! packed, so extraction unscales them; and points outside the instrument's
//! coverage carry a fill value, which becomes the bottom of the variable's
//! codebook — the value a renderer paints as nothing.
//!
//! The time axis is whatever the file carries. Observation series have gaps,
//! so the axis is *not* validated against a published cadence the way a
//! forecast run's is.

use std::path::{Path, PathBuf};

use time::{Duration, OffsetDateTime};

use crate::errors::{EncodeError, Result};
use crate::gdalio::Dataset;
use crate::model::{PlaneSource, SourceFrame};
use crate::sources::SourceSpec;
use crate::variables::variable_spec;

pub const NETCDF_EXTENSIONS: &[&str] = &["nc", "nc4", "cdf"];

/// Everything the converter needs to read one observation file.
pub struct ObservationSeries {
    /// The GDAL dataset the bands live in — for NetCDF, the
    /// `NETCDF:"<file>":<variable>` subdataset rather than the file itself.
    pub dataset: PathBuf,
    /// One entry per time, in axis order, keyed by variable id exactly like
    /// the per-file mapping the GRIB inspector produces.
    pub frames: Vec<Vec<(String, SourceFrame)>>,
    pub plane_source: PlaneSource,
}

/// The GDAL connection string for one variable of a NetCDF file. It is not a
/// filesystem path; it is carried as one because that is what every downstream
/// dataset open takes.
pub fn netcdf_dataset(path: &Path, variable: &str) -> PathBuf {
    PathBuf::from(format!("NETCDF:\"{}\":{variable}", path.display()))
}

/// `"<unit> since <ISO timestamp>"`, the CF convention for a time coordinate.
fn reference_time(units: &str, source: &Path) -> Result<(OffsetDateTime, i64)> {
    let unsupported = || {
        EncodeError::conversion(format!(
            "unsupported time units {units:?} in {}",
            source.display()
        ))
    };
    let (unit, epoch) = units.trim().split_once(" since ").ok_or_else(unsupported)?;
    let scale = match unit.trim().to_lowercase().as_str() {
        "seconds" => 1,
        "minutes" => 60,
        "hours" => 3600,
        "days" => 86_400,
        _ => return Err(unsupported()),
    };
    let text = epoch.trim().replace('Z', "+00:00").replacen(' ', "T", 1);
    let epoch = parse_iso(&text).ok_or_else(|| {
        EncodeError::conversion(format!(
            "unsupported time epoch {epoch:?} in {}",
            source.display()
        ))
    })?;
    Ok((epoch, scale))
}

/// Parse the ISO-8601 shapes a CF time epoch uses, with or without an offset;
/// a naive timestamp is UTC, as `datetime.fromisoformat` plus the encoder's
/// own normalization treats it.
fn parse_iso(text: &str) -> Option<OffsetDateTime> {
    let with_offset = time::format_description::well_known::Rfc3339;
    if let Ok(parsed) = OffsetDateTime::parse(text, &with_offset) {
        return Some(parsed.to_offset(time::UtcOffset::UTC));
    }
    let naive = time::macros::format_description!(
        "[year]-[month]-[day]T[hour]:[minute]:[second][optional [.[subsecond]]]"
    );
    time::PrimitiveDateTime::parse(text, &naive)
        .ok()
        .map(|value| value.assume_utc())
}

/// Read one observation file's frames, times and packing.
///
/// The source declares exactly one input variable; the file must carry it as a
/// variable of its own (a NetCDF subdataset), one band per time.
pub fn inspect_observation(path: &Path, source: &SourceSpec) -> Result<ObservationSeries> {
    if !source.observation {
        return Err(EncodeError::conversion(format!(
            "{} is not an observation source",
            source.manifest_model
        )));
    }
    if source.input_variable_ids.len() != 1 {
        return Err(EncodeError::conversion(format!(
            "{} must declare exactly one observation variable",
            source.manifest_model
        )));
    }
    let variable_id = source.input_variable_ids[0];
    if !path.is_file() {
        return Err(EncodeError::conversion(format!(
            "observation input does not exist: {}",
            path.display()
        )));
    }
    let extension = path
        .extension()
        .map(|value| value.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if !NETCDF_EXTENSIONS.contains(&extension.as_str()) {
        return Err(EncodeError::conversion(format!(
            "observation input must be a NetCDF file: {}",
            path.display()
        )));
    }

    let dataset_name = netcdf_dataset(path, variable_id);
    let dataset = Dataset::open(&dataset_name)?;
    let (epoch, scale) = reference_time(
        dataset.metadata("").get("time#units").map_or("", |v| v),
        path,
    )?;
    let bands = dataset.bands()?;
    if bands.is_empty() {
        return Err(EncodeError::conversion(format!(
            "{} carries no bands",
            dataset_name.display()
        )));
    }

    let spec = variable_spec(variable_id)?;
    let unit = bands[0].unit.trim().to_string();
    if unit != spec.output_unit {
        return Err(EncodeError::conversion(format!(
            "{} reports unit {}, expected {}",
            dataset_name.display(),
            if unit.is_empty() { "<missing>" } else { &unit },
            spec.output_unit
        )));
    }

    let mut times = Vec::with_capacity(bands.len());
    for band in &bands {
        let raw: f64 = band
            .metadata
            .get("NETCDF_DIM_time")
            .ok_or_else(|| {
                EncodeError::conversion(format!(
                    "band {} of {} carries no time coordinate",
                    band.number,
                    path.display()
                ))
            })?
            .parse()
            .map_err(|_| {
                EncodeError::conversion(format!(
                    "band {} of {} has an invalid time coordinate",
                    band.number,
                    path.display()
                ))
            })?;
        times.push(epoch + Duration::seconds_f64(raw * scale as f64));
    }

    let run_time = times[0];
    let mut frames: Vec<Vec<(String, SourceFrame)>> = Vec::with_capacity(bands.len());
    for (band, valid_time) in bands.iter().zip(&times) {
        let delta = (*valid_time - run_time).as_seconds_f64();
        if delta < 0.0 || delta.fract() != 0.0 {
            return Err(EncodeError::conversion(format!(
                "{} time is not a whole second after the first",
                dataset_name.display()
            )));
        }
        let delta = delta as i64;
        if frames
            .last()
            .is_some_and(|previous| delta <= previous[0].1.lead_seconds)
        {
            return Err(EncodeError::conversion(format!(
                "{} times are not strictly increasing",
                dataset_name.display()
            )));
        }
        frames.push(vec![(
            variable_id.to_string(),
            SourceFrame {
                path: dataset_name.clone(),
                band: band.number,
                variable_id: variable_id.to_string(),
                run_time,
                valid_time: *valid_time,
                lead_seconds: delta,
                unit: unit.clone(),
            },
        )]);
    }

    // One fill value for the whole series, in both the raw and scaled forms
    // GDAL can hand back.
    let scales: Vec<f64> = bands.iter().map(|band| band.scale).collect();
    let offsets: Vec<f64> = bands.iter().map(|band| band.offset).collect();
    let fills: Vec<Option<f64>> = bands.iter().map(|band| band.nodata).collect();
    let uniform = |values: &[f64]| values.windows(2).all(|pair| pair[0] == pair[1]);
    if !uniform(&scales) || !uniform(&offsets) || fills.windows(2).any(|pair| pair[0] != pair[1]) {
        return Err(EncodeError::conversion(format!(
            "{} bands disagree on packing or fill value",
            dataset_name.display()
        )));
    }
    let fill_replacement = f64::from(spec.value_range.0);
    let plane_source = match fills[0] {
        None => PlaneSource {
            unscale: true,
            fill_values: Vec::new(),
            fill_replacement,
        },
        Some(fill) => PlaneSource {
            unscale: true,
            fill_values: vec![fill, fill * scales[0] + offsets[0]],
            fill_replacement,
        },
    };

    Ok(ObservationSeries {
        dataset: dataset_name,
        frames,
        plane_source,
    })
}
