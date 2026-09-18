//! Gridded observation series: a NetCDF file as a run of Xue frames — the port
//! of `xuebuild/observation.py`.
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
//! forecast run's is. For a local archive file (the CMA mosaic) the first
//! frame is the series' run time. For a fetched window (`cadence_seconds`
//! set: the JMA nowcast) the window is the axis, the rule the MRMS frames
//! follow (`convert::snap_observation_frames`): each time is snapped down to
//! its cadence slot, the run time is the whole hour the first slot falls in
//! — the hour the run id names — and a frame's offset is its slot's distance
//! from it.
//!
//! **A forecast series** (`series_file` on a source that is not an
//! observation: the ECMWF IFS HRES run the Python fetch stage resamples off
//! the Open-Meteo bucket) is read here too, and differs in three ways. Its
//! run time is not its first frame but the epoch of the `time` coordinate —
//! `hours since <cycle>` — which is the cycle itself, so a variable whose
//! series starts at the first step still carries the lead times of that
//! cycle; the file's own `forecast_reference_time` must agree with it. Its
//! variables need not all cover the axis: one the source lists under
//! `optional_at_analysis` (an interval total, a mean, a maximum — none of
//! which exists at the analysis) may lack exactly the lead-zero frame, and
//! each returned frame mapping then holds only the variables that time has.
//! And the file may spell a unit another way than the registry does
//! ([`accepted_series_units`]). Everything downstream is the ordinary path,
//! and the forecast axis itself is validated against the source's published
//! steps by the converter, as any cycle's is.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use time::{Duration, OffsetDateTime};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::gdalio::{BandInfo, Dataset};
use crate::encode::model::{PlaneSource, SourceFrame};
use crate::encode::sources::SourceSpec;
use crate::encode::variables::{isobaric_variable, variable_spec, VariableSpec, SURFACE_TEMPERATURE_IDS};

pub const NETCDF_EXTENSIONS: &[&str] = &["nc", "nc4", "cdf"];

/// Everything the converter needs to read one observation window.
pub struct ObservationSeries {
    /// The GDAL dataset each variable's bands live in, by variable id — for
    /// NetCDF, the `NETCDF:"<file>":<variable>` subdataset rather than the
    /// file itself. One file per variable (a satellite window, one series
    /// per channel or produced component), or one file carrying several
    /// subdatasets (the radar mosaics' single variable). In the order the
    /// variables were asked for.
    pub datasets: Vec<(String, PathBuf)>,
    /// One entry per time, in axis order, keyed by variable id exactly like
    /// the per-file mapping the GRIB inspector produces. Every variable of a
    /// window is in every entry, since every series of a window carries the
    /// same axis; on a forecast series an analysis-optional variable is
    /// absent from the lead-zero entry, exactly as its record is absent from
    /// an f000 GRIB.
    pub frames: Vec<Vec<(String, SourceFrame)>>,
    /// How each variable's bands are read: its own packing and fill.
    pub plane_sources: Vec<(String, PlaneSource)>,
    /// For a produced variable, the `(id, version)` its series was stamped
    /// with — the `producer` block the bundle metadata carries.
    pub producers: Vec<(String, (String, String))>,
}

impl ObservationSeries {
    /// The first variable's dataset: the grid every series shares.
    pub fn dataset(&self) -> &Path {
        &self.datasets[0].1
    }
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

/// The NetCDF variable one series is read from. Every tool this pipeline
/// drives names the variable by its Xue id, except `om2nc`, which keeps
/// Open-Meteo's own name inside the file while the file beside it is named by
/// the Xue id (`VariableSpec::open_meteo`). The port of
/// `series_variable_name` in `xuebuild/observation.py`.
pub fn series_variable_name(variable_id: &str) -> Result<&str> {
    let spec = variable_spec(variable_id)?;
    Ok(if spec.open_meteo.is_empty() {
        variable_id
    } else {
        spec.open_meteo
    })
}

/// Units one file may spell another way than the registry does, by the
/// registry's own spelling. udunits writes a product with a space and an
/// exponent with a minus sign, so `m s-1` is `m/s` and `W m-2` is `W/m²` —
/// the same unit, and no conversion follows from accepting it. Mirrors
/// `_UNIT_SPELLINGS` in `xuebuild/observation.py`.
const UNIT_SPELLINGS: [(&str, &[&str]); 5] = [
    ("m/s", &["m s-1"]),
    ("W/m²", &["W m-2"]),
    ("J/kg", &["J kg-1"]),
    ("mm", &["kg m-2"]),
    ("°C", &["degC"]),
];

/// Every unit a series file may report for one variable.
///
/// A GRIB record is matched on its identity and its unit is checked against
/// the registry's; a series file has no identity to match, so the unit is the
/// whole of the check and it must be strict — a number quantized in the wrong
/// unit is silent. The rule is: the registry's `output_unit`, the same unit
/// spelled the way udunits spells it ([`UNIT_SPELLINGS`]), or an input unit
/// the converter already knows how to turn into it for *this* variable
/// (`convert::convert_units`) — kelvin or fahrenheit for a temperature,
/// pascals for the sea level pressure, metres for the visibility. Anything
/// else is refused. The port of `accepted_series_units` in
/// `xuebuild/observation.py`, which must answer the same list in the same
/// order.
pub fn accepted_series_units(spec: &VariableSpec) -> Vec<&'static str> {
    let mut units: Vec<&'static str> = vec![spec.output_unit];
    for (output_unit, spellings) in UNIT_SPELLINGS {
        if output_unit == spec.output_unit {
            units.extend_from_slice(spellings);
        }
    }
    let isobaric_temperature = matches!(isobaric_variable(spec.id), Some(("tmp", _)));
    if SURFACE_TEMPERATURE_IDS.contains(&spec.id) || isobaric_temperature {
        // The three spellings normalize_unit accepts, which is what the
        // converter reads a temperature through.
        units.extend_from_slice(&["K", "C", "F"]);
    } else if spec.id == "prmsl" {
        units.push("Pa");
    } else if spec.id == "vis" {
        units.push("m");
    }
    let mut seen: Vec<&'static str> = Vec::with_capacity(units.len());
    for unit in units {
        if !seen.contains(&unit) {
            seen.push(unit);
        }
    }
    seen
}

/// Whether the bands declare a CF `_FillValue` of NaN in their own
/// attributes. The port of `_declares_nan_fill` in
/// `xuebuild/observation.py`.
fn declares_nan_fill(bands: &[BandInfo]) -> bool {
    bands[0]
        .metadata
        .get("_FillValue")
        .and_then(|raw| raw.trim().parse::<f64>().ok())
        .is_some_and(f64::is_nan)
}

/// Hold a forecast series' `forecast_reference_time` to the epoch of its time
/// coordinate. Both name the cycle, and the run is read from the epoch, so a
/// file where they disagree is one this converter has misunderstood rather
/// than one to build from. A file that carries no such attribute passes: it
/// is CF's business, not the format's. The port of `_check_reference_time` in
/// `xuebuild/observation.py`.
fn check_reference_time(
    attributes: &HashMap<String, String>,
    epoch: OffsetDateTime,
    dataset: &Path,
) -> Result<()> {
    let Some(raw) = attributes.get("NC_GLOBAL#forecast_reference_time") else {
        return Ok(());
    };
    let text = raw.trim().replace('Z', "+00:00").replacen(' ', "T", 1);
    let reference = parse_iso(&text).ok_or_else(|| {
        EncodeError::conversion(format!(
            "{} has an unreadable forecast_reference_time '{raw}'",
            dataset.display()
        ))
    })?;
    if reference != epoch {
        return Err(EncodeError::conversion(format!(
            "{} was made for {} but its time axis counts from {}",
            dataset.display(),
            iso(reference),
            iso(epoch)
        )));
    }
    Ok(())
}

/// A timestamp the way `datetime.isoformat` writes it, for the one message
/// that quotes two of them.
fn iso(time: OffsetDateTime) -> String {
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        time.year(),
        u8::from(time.month()),
        time.day(),
        time.hour(),
        time.minute(),
        time.second()
    )
}

fn is_netcdf(path: &Path) -> bool {
    path.extension().is_some_and(|extension| {
        NETCDF_EXTENSIONS.contains(&extension.to_string_lossy().to_lowercase().as_str())
    })
}

/// Which NetCDF file each variable of an observation window is in.
///
/// A file is every variable's (the mosaics: one file, one variable, and a
/// second variable would be another subdataset of it). A directory is read
/// two ways: one file per variable named `<stem>.<variable>.nc` (a satellite
/// window, `xuebuild/satellite/assemble.py`), else the one NetCDF file it
/// holds for every variable. The port of `series_files` in
/// `xuebuild/observation.py`.
pub fn series_files(path: &Path, variable_ids: &[&str]) -> Result<Vec<(String, PathBuf)>> {
    if path.is_file() {
        return Ok(variable_ids
            .iter()
            .map(|id| ((*id).to_string(), path.to_path_buf()))
            .collect());
    }
    if !path.is_dir() {
        return Err(EncodeError::conversion(format!(
            "observation input does not exist: {}",
            path.display()
        )));
    }
    let mut files: Vec<PathBuf> = std::fs::read_dir(path)
        .map_err(|error| EncodeError::conversion(format!("cannot list {}: {error}", path.display())))?
        .filter_map(std::result::Result::ok)
        .map(|entry| entry.path())
        .filter(|item| item.is_file() && is_netcdf(item))
        .collect();
    files.sort();
    let per_variable: Vec<Vec<&PathBuf>> = variable_ids
        .iter()
        .map(|id| {
            files
                .iter()
                .filter(|item| {
                    let extension = item.extension().map(|e| e.to_string_lossy().to_string()).unwrap_or_default();
                    item.file_name()
                        .map(|name| name.to_string_lossy().to_string())
                        .is_some_and(|name| name.ends_with(&format!(".{id}.{extension}")))
                })
                .collect()
        })
        .collect();
    if per_variable.iter().any(|candidates| !candidates.is_empty()) {
        let mut resolved = Vec::with_capacity(variable_ids.len());
        for (id, candidates) in variable_ids.iter().zip(&per_variable) {
            if candidates.len() != 1 {
                return Err(EncodeError::conversion(format!(
                    "{} must hold exactly one series file for {id} (<stem>.{id}.nc), it holds {}",
                    path.display(),
                    candidates.len()
                )));
            }
            resolved.push(((*id).to_string(), candidates[0].clone()));
        }
        return Ok(resolved);
    }
    if files.len() != 1 {
        return Err(EncodeError::conversion(format!(
            "a run directory holds exactly one NetCDF series, {} holds {}",
            path.display(),
            files.len()
        )));
    }
    Ok(variable_ids
        .iter()
        .map(|id| ((*id).to_string(), files[0].clone()))
        .collect())
}

/// Read one observation window's frames, times, packing and producers.
///
/// `path` is one NetCDF file or a run directory ([`series_files`]).
/// `variable_ids` are the variables to read, the source's inputs when
/// `None`; each must be a NetCDF variable (subdataset) of its file, one band
/// per time, and every variable must carry the same time axis — bar an
/// analysis-optional one on a forecast series, which may lack the lead-zero
/// frame. A variable the registry marks as produced must be stamped with that
/// producer's id, and the version beside it is returned.
pub fn inspect_observation(
    path: &Path,
    source: &SourceSpec,
    variable_ids: Option<&[&str]>,
) -> Result<ObservationSeries> {
    if !source.observation && !source.series_file {
        return Err(EncodeError::conversion(format!(
            "{} is not read from a NetCDF series",
            source.manifest_model
        )));
    }
    let variable_ids: Vec<&str> = match variable_ids {
        Some(ids) => ids.to_vec(),
        None => source.input_variable_ids.to_vec(),
    };
    if variable_ids.is_empty() {
        return Err(EncodeError::conversion(format!(
            "{} declares no observation variable",
            source.manifest_model
        )));
    }
    let files = series_files(path, &variable_ids)?;

    let mut datasets: Vec<(String, PathBuf)> = Vec::with_capacity(variable_ids.len());
    let mut plane_sources: Vec<(String, PlaneSource)> = Vec::with_capacity(variable_ids.len());
    let mut producers: Vec<(String, (String, String))> = Vec::new();
    let mut per_variable_frames: Vec<Vec<SourceFrame>> = Vec::with_capacity(variable_ids.len());
    for (variable_id, file) in &files {
        let variable_id = variable_id.as_str();
        if !is_netcdf(file) {
            return Err(EncodeError::conversion(format!(
                "observation input must be a NetCDF file: {}",
                file.display()
            )));
        }
        let dataset_name = netcdf_dataset(file, series_variable_name(variable_id)?);
        let dataset = Dataset::open(&dataset_name)?;
        let attributes_global = dataset.metadata("");
        let (epoch, scale) =
            reference_time(attributes_global.get("time#units").map_or("", |v| v), file)?;
        let bands = dataset.bands()?;
        if bands.is_empty() {
            return Err(EncodeError::conversion(format!(
                "{} carries no bands",
                dataset_name.display()
            )));
        }

        let spec = variable_spec(variable_id)?;
        let unit = bands[0].unit.trim().to_string();
        let accepted = accepted_series_units(spec);
        if !accepted.contains(&unit.as_str()) {
            return Err(EncodeError::conversion(format!(
                "{} reports unit {}, expected {}",
                dataset_name.display(),
                if unit.is_empty() { "<missing>" } else { &unit },
                accepted.join(" or ")
            )));
        }
        // A produced variable carries the producer that made it as two
        // attributes of the variable (the fetch stage stamps them); the id
        // must be the registry's, the version is whatever ran and goes into
        // the metadata.
        let producer_id = bands[0].metadata.get("producer_id").map(String::as_str);
        let producer_version = bands[0].metadata.get("producer_version").map(|v| v.trim());
        match spec.producer_id {
            Some(registered) => {
                if producer_id != Some(registered) {
                    return Err(EncodeError::conversion(format!(
                        "{} was produced by {}, not {registered}",
                        dataset_name.display(),
                        producer_id.unwrap_or("<nothing>")
                    )));
                }
                let version = match producer_version {
                    Some(version) if !version.is_empty() => version,
                    _ => {
                        return Err(EncodeError::conversion(format!(
                            "{} carries no producer_version",
                            dataset_name.display()
                        )))
                    }
                };
                producers.push((
                    variable_id.to_string(),
                    (registered.to_string(), version.to_string()),
                ));
            }
            None => {
                if let Some(producer_id) = producer_id {
                    return Err(EncodeError::conversion(format!(
                        "{} is stamped by producer '{producer_id}', but {variable_id} is not a produced variable",
                        dataset_name.display()
                    )));
                }
            }
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
                        file.display()
                    ))
                })?
                .parse()
                .map_err(|_| {
                    EncodeError::conversion(format!(
                        "band {} of {} has an invalid time coordinate",
                        band.number,
                        file.display()
                    ))
                })?;
            times.push(epoch + Duration::seconds_f64(raw * scale as f64));
        }

        let (times, run_time) = if !source.observation {
            // A forecast series names its cycle in the time coordinate's own
            // epoch ("hours since <cycle>"), so the run is that epoch and
            // every band is a lead time from it — a variable whose series
            // starts at the first step keeps the cycle's numbering. The
            // file's own reference time must say the same thing.
            check_reference_time(&attributes_global, epoch, &dataset_name)?;
            (times, epoch)
        } else if let Some(cadence) = source.cadence_seconds {
            let snapped = times
                .into_iter()
                .map(|time| {
                    let slot = time.unix_timestamp().div_euclid(cadence) * cadence;
                    OffsetDateTime::from_unix_timestamp(slot).map_err(|error| {
                        EncodeError::conversion(format!("invalid observation time: {error}"))
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            let hour = snapped[0].unix_timestamp().div_euclid(3600) * 3600;
            let run_time = OffsetDateTime::from_unix_timestamp(hour).map_err(|error| {
                EncodeError::conversion(format!("invalid observation time: {error}"))
            })?;
            (snapped, run_time)
        } else {
            let run_time = times[0];
            (times, run_time)
        };
        let mut frames: Vec<SourceFrame> = Vec::with_capacity(bands.len());
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
                .is_some_and(|previous| delta <= previous.lead_seconds)
            {
                return Err(EncodeError::conversion(format!(
                    "{} times are not strictly increasing",
                    dataset_name.display()
                )));
            }
            frames.push(SourceFrame {
                path: dataset_name.clone(),
                band: band.number,
                variable_id: variable_id.to_string(),
                run_time,
                valid_time: *valid_time,
                lead_seconds: delta,
                unit: unit.clone(),
            });
        }

        // One fill value for the whole series, in both the raw and scaled
        // forms GDAL can hand back.
        let scales: Vec<f64> = bands.iter().map(|band| band.scale).collect();
        let offsets: Vec<f64> = bands.iter().map(|band| band.offset).collect();
        let fills: Vec<Option<f64>> = bands.iter().map(|band| band.nodata).collect();
        let uniform = |values: &[f64]| values.windows(2).all(|pair| pair[0] == pair[1]);
        // Two NaN fills are the same fill: the Python side compares the text
        // its `gdalinfo` source reports, where every band's NaN is one
        // spelling.
        let same_fill = |left: &Option<f64>, right: &Option<f64>| match (left, right) {
            (Some(left), Some(right)) => left == right || (left.is_nan() && right.is_nan()),
            (None, None) => true,
            _ => false,
        };
        if !uniform(&scales) || !uniform(&offsets) || fills.windows(2).any(|pair| !same_fill(&pair[0], &pair[1])) {
            return Err(EncodeError::conversion(format!(
                "{} bands disagree on packing or fill value",
                dataset_name.display()
            )));
        }
        let fill_replacement = spec.value_range.0;
        // A CF `_FillValue` of NaN — every om2nc series carries one, and the
        // land under sea ice thickness is written with it. Nothing equals a
        // NaN, so it can never be one of the fill *values*; it is matched as
        // a NaN and becomes the same codebook bottom. It is read off the
        // band's own attributes as well as off the reported nodata, because
        // a NaN is not a JSON number and the `gdalinfo` sources the Python
        // encoder reads carry it differently (`_plane_source` in
        // `xuebuild/observation.py`).
        let nan_fill = match fills[0] {
            Some(fill) => fill.is_nan(),
            None => declares_nan_fill(&bands),
        };
        let plane_source = match fills[0] {
            _ if nan_fill => PlaneSource {
                unscale: true,
                fill_values: Vec::new(),
                fill_nan: true,
                fill_replacement,
            },
            None => PlaneSource {
                unscale: true,
                fill_values: Vec::new(),
                fill_nan: false,
                fill_replacement,
            },
            Some(fill) => PlaneSource {
                unscale: true,
                fill_values: vec![fill, fill * scales[0] + offsets[0]],
                fill_nan: false,
                fill_replacement,
            },
        };
        datasets.push((variable_id.to_string(), dataset_name));
        plane_sources.push((variable_id.to_string(), plane_source));
        per_variable_frames.push(frames);
    }

    // The run's axis is the axis of a variable that carries every frame; a
    // variable the source lists as optional at the analysis may be short of
    // exactly the lead-zero frame and must match the rest one for one. (Only
    // a forecast series has such a variable; a window's series all carry the
    // same times.)
    let reference = variable_ids
        .iter()
        .position(|id| !source.optional_at_analysis.contains(id))
        .unwrap_or(0);
    let axis: Vec<(OffsetDateTime, i64)> = per_variable_frames[reference]
        .iter()
        .map(|frame| (frame.run_time, frame.lead_seconds))
        .collect();
    let analysis_less: Option<&[(OffsetDateTime, i64)]> =
        (axis.first().is_some_and(|(_, lead)| *lead == 0)).then(|| &axis[1..]);
    for (index, frames) in per_variable_frames.iter().enumerate() {
        let other: Vec<(OffsetDateTime, i64)> =
            frames.iter().map(|frame| (frame.run_time, frame.lead_seconds)).collect();
        if other == axis {
            continue;
        }
        if source.optional_at_analysis.contains(&variable_ids[index])
            && analysis_less.is_some_and(|expected| other == expected)
        {
            continue;
        }
        return Err(EncodeError::conversion(format!(
            "{} carries another time axis than {}",
            datasets[index].1.display(),
            datasets[reference].1.display()
        )));
    }
    // Each entry holds only the variables that time has, in the order they
    // were asked for.
    let frames: Vec<Vec<(String, SourceFrame)>> = axis
        .iter()
        .map(|key| {
            per_variable_frames
                .iter()
                .filter_map(|frames| {
                    frames
                        .iter()
                        .find(|frame| (frame.run_time, frame.lead_seconds) == *key)
                        .map(|frame| (frame.variable_id.clone(), frame.clone()))
                })
                .collect()
        })
        .collect();

    Ok(ObservationSeries {
        datasets,
        frames,
        plane_sources,
        producers,
    })
}
