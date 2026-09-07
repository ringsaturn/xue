//! Record discovery through GDAL's own band metadata — the port of
//! `xue/gdal.py`'s `inspect_grib_multi`.
//!
//! This is the reference matcher: the converter runs it once per run on the
//! first file, both to probe wind availability and to cross-check the much
//! faster GRIB2 header index (`gribindex.rs`) that locates the bands every
//! extraction reads.

use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use time::OffsetDateTime;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::gdalio::{BandInfo, Dataset};
use crate::encode::model::SourceFrame;
use crate::encode::variables::variable_spec;

static HEIGHT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^0-9])2(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground").expect("valid regex")
});
static TEN_METRE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^0-9])10(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground").expect("valid regex")
});

pub const SUPPORTED_EXTENSIONS: &[&str] = &["grb", "grb2", "grib2"];

/// Collapse a temperature unit string to `K`, `C` or `F`.
pub fn normalize_unit(unit: &str) -> Result<&'static str> {
    let compact = unit
        .trim()
        .trim_matches(|character| "[]()".contains(character))
        .to_lowercase()
        .replace('°', "")
        .replace("degrees", "")
        .replace("degree", "");
    let compact = compact.split_whitespace().collect::<Vec<_>>().join(" ");
    match compact.as_str() {
        "k" | "kelvin" => Ok("K"),
        "c" | "celsius" | "degc" => Ok("C"),
        "f" | "fahrenheit" | "degf" => Ok("F"),
        _ => Err(EncodeError::conversion(format!(
            "unsupported temperature unit: {}",
            if unit.is_empty() { "<missing>" } else { unit }
        ))),
    }
}

fn compact_unit(unit: &str) -> String {
    unit.trim()
        .to_lowercase()
        .replace('²', "^2")
        .chars()
        .filter(|character| !" *()[]".contains(*character))
        .collect()
}

/// The per-variable unit acceptance the Python converter expresses as a GDAL
/// raster expression. Only its *identity* matters here — the converter
/// compares the string of the header-index frame against the gdalinfo frame —
/// so the expressions are reproduced verbatim.
pub fn raster_expression(variable_id: &str, unit: &str) -> Result<String> {
    let rate_units = ["kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1"];
    match variable_id {
        "tmp2m" => {
            let value = match normalize_unit(unit)? {
                "K" => "A-273.15",
                "F" => "(A-32)*5/9",
                _ => "A",
            };
            Ok(format!("maximum(-60,minimum(50,{value}))"))
        }
        "prate" | "prate_ave" => {
            if !rate_units.contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported precipitation rate unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok(if variable_id == "prate" {
                "maximum(0,minimum(50,A*3600))".into()
            } else {
                "A".into()
            })
        }
        "tp" => {
            let compact = unit
                .trim()
                .trim_matches(|character| "[]()".contains(character))
                .to_lowercase();
            if !matches!(compact.as_str(), "-" | "m" | "") {
                return Err(EncodeError::conversion(format!(
                    "unsupported precipitation accumulation unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("A".into())
        }
        "dswrf" => {
            if !["w/m^2", "w/m2", "wm^-2", "wm-2"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported radiative flux unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(1270,A))".into())
        }
        "ugrd10m" | "vgrd10m" => {
            let compact: String = unit
                .trim()
                .to_lowercase()
                .chars()
                .filter(|character| !" *()[]".contains(*character))
                .collect();
            if !["m/s", "m/sec", "ms-1", "ms^-1", "mps"].contains(&compact.as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported wind component unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(-64,minimum(64,A))".into())
        }
        other => Err(EncodeError::conversion(format!(
            "unsupported variable: {other}"
        ))),
    }
}

fn searchable(band: &BandInfo) -> String {
    [
        band.item("GRIB_SHORT_NAME"),
        band.item("GRIB_COMMENT"),
        band.item("GRIB_LEVEL"),
        &band.description,
    ]
    .join(" ")
}

fn band_matches(variable_id: &str, band: &BandInfo) -> Result<bool> {
    let element = band.item("GRIB_ELEMENT").to_uppercase();
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    Ok(match variable_id {
        "tmp2m" => {
            element == "TMP"
                && (matches!(short_name.as_str(), "2-HTGL" | "2-M-HTGL")
                    || HEIGHT_RE.is_match(&searchable(band)))
        }
        // sflux files carry only the interval-averaged PRATE record, pgrb2
        // fetches only the instantaneous one — the same surface matcher hits
        // exactly the record its source provides.
        "prate" | "prate_ave" => {
            element == "PRATE"
                && (short_name == "0-SFC" || searchable(band).to_lowercase().contains("surface"))
        }
        // ECMWF open data tp: GDAL's tables do not know the local parameter
        // 0/1/193, so GRIB_ELEMENT is "unknown" and the comment carries the
        // raw triple.
        "tp" => {
            let comment = band.item("GRIB_COMMENT").to_string();
            let text = [comment.as_str(), band.item("GRIB_LEVEL"), &band.description]
                .join(" ")
                .to_lowercase();
            matches!(element.to_lowercase().as_str(), "unknown" | "tp" | "apcp")
                && (short_name == "0-SFC" || text.contains("surface"))
                && (comment.contains("cat 1, subcat 193")
                    || text.contains("total precipitation"))
        }
        "dswrf" => {
            element == variable_spec(variable_id)?.grib_element
                && (short_name == "0-SFC" || searchable(band).to_lowercase().contains("surface"))
        }
        "ugrd10m" | "vgrd10m" => {
            element == variable_spec(variable_id)?.grib_element
                && (matches!(short_name.as_str(), "10-HTGL" | "10-M-HTGL")
                    || TEN_METRE_RE.is_match(&searchable(band)))
        }
        other => {
            return Err(EncodeError::conversion(format!(
                "unsupported variable: {other}"
            )))
        }
    })
}

fn timestamp(band: &BandInfo, key: &str) -> Option<OffsetDateTime> {
    let raw: f64 = band.metadata.get(key)?.parse().ok()?;
    OffsetDateTime::from_unix_timestamp(raw as i64).ok()
}

fn frame_from_band(path: &Path, variable_id: &str, band: &BandInfo) -> Result<SourceFrame> {
    let raw_unit = if band.item("GRIB_UNIT").is_empty() {
        band.item("GRIB_COMMENT")
            .rsplit_once('[')
            .map_or("", |(_, tail)| tail)
            .trim_end_matches(']')
            .to_string()
    } else {
        band.item("GRIB_UNIT").to_string()
    };
    let unit = if variable_id == "tmp2m" {
        normalize_unit(&raw_unit)?.to_string()
    } else {
        raw_unit
            .trim()
            .trim_matches(|character| "[]".contains(character))
            .to_string()
    };
    raster_expression(variable_id, &unit)?;
    let valid_time = timestamp(band, "GRIB_VALID_TIME");
    let run_time = timestamp(band, "GRIB_REF_TIME").or_else(|| {
        let forecast: f64 = band.metadata.get("GRIB_FORECAST_SECONDS")?.parse().ok()?;
        OffsetDateTime::from_unix_timestamp(valid_time?.unix_timestamp() - forecast as i64).ok()
    });
    let (Some(run_time), Some(valid_time)) = (run_time, valid_time) else {
        return Err(EncodeError::conversion(format!(
            "missing GRIB_REF_TIME or GRIB_VALID_TIME metadata in {}",
            path.display()
        )));
    };
    let delta = valid_time.unix_timestamp() - run_time.unix_timestamp();
    if delta < 0 || delta % 3600 != 0 {
        return Err(EncodeError::conversion(format!(
            "forecast time is not a non-negative whole hour in {}",
            path.display()
        )));
    }
    Ok(SourceFrame {
        path: path.to_path_buf(),
        band: band.number,
        variable_id: variable_id.to_string(),
        run_time,
        valid_time,
        lead_seconds: delta,
        unit,
    })
}

/// Locate every requested variable in one GDAL pass over the file.
///
/// Variables in `optional_ids` may be absent (they are simply omitted from the
/// result); more than one match is still an error for every variable.
pub fn inspect_grib_multi(
    path: &Path,
    variable_ids: &[&str],
    optional_ids: &[&str],
) -> Result<Vec<(String, SourceFrame)>> {
    if !path.is_file() {
        return Err(EncodeError::conversion(format!(
            "GRIB input does not exist: {}",
            path.display()
        )));
    }
    let dataset = Dataset::open(path)?;
    let bands = dataset.bands()?;
    let mut frames = Vec::new();
    for variable_id in variable_ids {
        let matches: Vec<&BandInfo> = bands
            .iter()
            .filter(|band| band_matches(variable_id, band).unwrap_or(false))
            .collect();
        if matches.is_empty() && optional_ids.contains(variable_id) {
            continue;
        }
        if matches.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "expected exactly one {} band for {variable_id} in {}, found {}",
                variable_spec(variable_id)?.grib_element,
                path.display(),
                matches.len()
            )));
        }
        frames.push((
            (*variable_id).to_string(),
            frame_from_band(path, variable_id, matches[0])?,
        ));
    }
    Ok(frames)
}
