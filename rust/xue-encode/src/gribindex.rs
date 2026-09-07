//! GRIB2 record discovery through [grib-rs](https://github.com/noritada/grib-rs)
//! — the port of `xue/grib2.py`.
//!
//! Reads only the identification and product-definition sections of every
//! message, which is what locates each variable's band number and its
//! reference/valid time without decoding any data. `inspect.rs` (real GDAL
//! band metadata) stays the reference: the converter cross-checks the first
//! file of every run against it and falls back to it wholesale when a run's
//! files do not parse here.

use std::fs::File;
use std::io::BufReader;
use std::path::Path;

use grib::{Code, ForecastTime};
use time::{Date, Duration, Month, OffsetDateTime, Time, UtcOffset};

use crate::errors::{EncodeError, Result};
use crate::model::SourceFrame;
use crate::variables::{variable_spec, VariableSpec};

/// Product definition templates whose octets 10-34 share the 4.0 layout and
/// that we know how to time-stamp. 4.8 adds the statistical interval.
const INSTANTANEOUS_TEMPLATES: &[u16] = &[0, 1, 2];
const STATISTICAL_TEMPLATES: &[u16] = &[8, 11, 12];
/// Section 4 payload starts at octet 6, so the template's first octet (10)
/// sits at payload index 4.
const START_OF_PROD_TEMPLATE: usize = 4;

/// Identity of one GRIB2 message, 1-based band order.
#[derive(Debug, Clone)]
pub struct MessageInfo {
    pub band: usize,
    pub discipline: u8,
    pub parameter_category: u8,
    pub parameter_number: u8,
    pub level_type: u8,
    pub level_value: Option<f64>,
    pub reference_time: OffsetDateTime,
    pub valid_time: OffsetDateTime,
    /// Code table 4.10 process (0 average, 1 accumulation) for statistical
    /// templates (the 4.8 family); `None` for instantaneous products.
    pub statistical_process: Option<u8>,
}

/// Code table 4.4 (indicator of unit of time range) for the units our products
/// use; anything else is rejected and triggers the GDAL fallback.
fn time_unit_seconds(code: u8) -> Option<i64> {
    Some(match code {
        0 => 60,
        1 => 3600,
        2 => 86_400,
        10 => 3 * 3600,
        11 => 6 * 3600,
        12 => 12 * 3600,
        13 => 1,
        _ => return None,
    })
}

fn build_time(
    year: u16,
    month: u8,
    day: u8,
    hour: u8,
    minute: u8,
    second: u8,
    path: &Path,
) -> Result<OffsetDateTime> {
    let invalid = || {
        EncodeError::conversion(format!(
            "invalid GRIB2 timestamp in {}: {year}-{month}-{day} {hour}:{minute}:{second}",
            path.display()
        ))
    };
    let month = Month::try_from(month).map_err(|_| invalid())?;
    let date = Date::from_calendar_date(i32::from(year), month, day).map_err(|_| invalid())?;
    let clock = Time::from_hms(hour, minute, second).map_err(|_| invalid())?;
    Ok(date.with_time(clock).assume_offset(UtcOffset::UTC))
}

/// Band-ordered identities of every message in a GRIB2 file.
pub fn index_messages(path: &Path) -> Result<Vec<MessageInfo>> {
    let file = File::open(path).map_err(|error| {
        EncodeError::conversion(format!("cannot read GRIB input {}: {error}", path.display()))
    })?;
    let grib2 = grib::from_reader(BufReader::new(file)).map_err(|error| {
        EncodeError::conversion(format!("cannot parse GRIB2 in {}: {error}", path.display()))
    })?;

    let mut messages = Vec::new();
    for (band, (_index, submessage)) in grib2.iter().enumerate() {
        let discipline = submessage.indicator().discipline;
        let reference = submessage.identification().ref_time_unchecked();
        let reference_time = build_time(
            reference.year,
            reference.month,
            reference.day,
            reference.hour,
            reference.minute,
            reference.second,
            path,
        )?;

        let product = submessage.prod_def();
        let template = product.prod_tmpl_num();
        let statistical = if INSTANTANEOUS_TEMPLATES.contains(&template) {
            None
        } else if STATISTICAL_TEMPLATES.contains(&template) {
            Some(())
        } else {
            return Err(EncodeError::conversion(format!(
                "unsupported GRIB2 product definition template 4.{template} in {}",
                path.display()
            )));
        };
        let payload: Vec<u8> = product.iter().copied().collect();

        let (category, number) = (
            product.parameter_category(),
            product.parameter_number(),
        );
        let (Some(category), Some(number)) = (category, number) else {
            return Err(EncodeError::conversion(format!(
                "GRIB2 product definition section is too short in {}",
                path.display()
            )));
        };
        let Some((first_surface, _second)) = product.fixed_surfaces() else {
            return Err(EncodeError::conversion(format!(
                "GRIB2 product definition carries no fixed surface in {}",
                path.display()
            )));
        };
        let level_value = {
            let value = first_surface.value();
            value.is_finite().then_some(value)
        };

        let valid_time = if statistical.is_some() {
            // Statistical products are valid at the end of the overall
            // interval (section 4 octets 35-41); the forecast time octets hold
            // the interval start.
            let start = START_OF_PROD_TEMPLATE + 25;
            if payload.len() < START_OF_PROD_TEMPLATE + 38 {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 product definition section is too short in {}",
                    path.display()
                )));
            }
            build_time(
                u16::from_be_bytes([payload[start], payload[start + 1]]),
                payload[start + 2],
                payload[start + 3],
                payload[start + 4],
                payload[start + 5],
                payload[start + 6],
                path,
            )?
        } else {
            let Some(ForecastTime { unit, value }) = product.forecast_time() else {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 product definition carries no forecast time in {}",
                    path.display()
                )));
            };
            let code = match unit {
                Code::Name(name) => u8::from(name),
                Code::Num(number) => number,
            };
            let Some(seconds) = time_unit_seconds(code) else {
                return Err(EncodeError::conversion(format!(
                    "unsupported GRIB2 time unit {code} in {}",
                    path.display()
                )));
            };
            reference_time + Duration::seconds(i64::from(value) * seconds)
        };
        let statistical_process = statistical.map(|()| payload[START_OF_PROD_TEMPLATE + 37]);

        messages.push(MessageInfo {
            band: band + 1,
            discipline,
            parameter_category: category,
            parameter_number: number,
            level_type: first_surface.surface_type,
            level_value,
            reference_time,
            valid_time,
            statistical_process,
        });
    }
    if messages.is_empty() {
        return Err(EncodeError::conversion(format!(
            "no GRIB2 messages in {}",
            path.display()
        )));
    }
    Ok(messages)
}

fn matches(spec: &VariableSpec, message: &MessageInfo) -> bool {
    if message.discipline != spec.grib2_discipline
        || message.parameter_category != spec.grib2_category
        || message.parameter_number != spec.grib2_number
        || message.level_type != spec.grib2_level_type
    {
        return false;
    }
    if let Some(expected) = spec.grib2_level_value {
        if message.level_value != Some(expected) {
            return false;
        }
    }
    message.statistical_process == spec.grib2_statistical
}

/// Locate every requested variable from the GRIB2 headers alone.
///
/// Mirrors [`crate::inspect::inspect_grib_multi`]: variables in `optional_ids`
/// may be absent, more than one match is an error. Units are the fixed
/// GDAL-normalized strings from the variable table, validated against a real
/// GDAL pass once per run by the converter.
pub fn inspect_grib_fast(
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
    let messages = index_messages(path)?;
    let mut frames = Vec::new();
    for variable_id in variable_ids {
        let spec = variable_spec(variable_id)?;
        let found: Vec<&MessageInfo> = messages
            .iter()
            .filter(|message| matches(spec, message))
            .collect();
        if found.is_empty() && optional_ids.contains(variable_id) {
            continue;
        }
        if found.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "expected exactly one {} band for {variable_id} in {}, found {}",
                spec.grib_element,
                path.display(),
                found.len()
            )));
        }
        let message = found[0];
        let delta = message.valid_time.unix_timestamp() - message.reference_time.unix_timestamp();
        if delta < 0 || delta % 3600 != 0 {
            return Err(EncodeError::conversion(format!(
                "forecast time is not a non-negative whole hour in {}",
                path.display()
            )));
        }
        frames.push((
            (*variable_id).to_string(),
            SourceFrame {
                path: path.to_path_buf(),
                band: message.band,
                variable_id: (*variable_id).to_string(),
                run_time: message.reference_time,
                valid_time: message.valid_time,
                lead_seconds: delta,
                unit: spec.gdal_unit.to_string(),
            },
        ));
    }
    Ok(frames)
}
