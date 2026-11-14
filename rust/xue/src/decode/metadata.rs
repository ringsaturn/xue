//! The bundle's metadata JSON: the grid, the time axis and the variable set.
//!
//! Validation here is what turns a JSON blob into the dimensions the rest of
//! the decoder trusts — the plane length every entry is checked against, and
//! the materialized frame-offset axis every plane is addressed by. Both the
//! axis and the variable set have exactly one valid encoding at exactly one
//! schema version, so a file cannot describe the same series two ways. The
//! grid's kind is checked here too, so a container and a store refuse the same
//! projected or Gaussian grid.

use crate::format::{err, gcd, DecodeError, HOUR_SECONDS, MAX_PLANE_LENGTH};
use crate::projection::{GaussianRows, LambertConformal};

/// The schemaVersion 3 time block. Versions 1 and 2 use firstForecastHour
/// with stepHours or hours instead; mixing the two shapes is invalid.
const V3_TIME_FIELDS: [&str; 4] =
    ["unitSeconds", "firstFrameOffset", "frameStep", "frameOffsets"];


pub(crate) struct Metadata {
    pub(crate) json: String,
    /// The lowest schema version able to express this axis and variable set.
    pub(crate) schema_version: u8,
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) plane_length: u32,
    pub(crate) frame_count: u32,
    /// Seconds per axis unit: an hour for every forecast source, finer for a
    /// sub-hourly observation series.
    pub(crate) unit_seconds: u32,
    /// The materialized frame-offset axis, ascending. Uniform axes are
    /// expanded from their step; axes that list their offsets outright are
    /// taken as listed.
    pub(crate) offsets: Vec<u16>,
    pub(crate) variable_ids: Vec<u8>,
}

/// The frame offsets from the metadata `time` block, the axis unit in
/// seconds, and the lowest schema version able to express them.
///
/// Schema versions 1 and 2 describe a whole-hour axis with
/// `firstForecastHour` plus one of `stepHours` (uniform, version 1) and
/// `hours` (listed outright, version 2). Version 3 replaces the block with a
/// unit-neutral one — `unitSeconds` plus offsets in that unit — so a series
/// finer than an hour has an exact axis.
fn parse_time_axis(
    time: &serde_json::Map<String, serde_json::Value>,
    frame_count: u64,
    schema_version: u64,
) -> Result<(u32, Vec<u16>, u64), DecodeError> {
    if schema_version >= 3 {
        return parse_offset_axis(time, frame_count);
    }
    if V3_TIME_FIELDS.iter().any(|field| time.contains_key(*field)) {
        return Err(err("a unit-neutral time axis requires schemaVersion 3"));
    }
    let first_hour = time
        .get("firstForecastHour")
        .and_then(|v| v.as_u64())
        .filter(|&hour| hour <= u16::MAX as u64)
        .ok_or_else(|| err("metadata firstForecastHour is invalid"))?;
    let step_hours = time.get("stepHours");
    let listed_hours = time.get("hours");
    match (step_hours, listed_hours) {
        (Some(_), Some(_)) | (None, None) => {
            Err(err("metadata time must declare exactly one of stepHours and hours"))
        }
        (Some(step), None) => {
            let step = step
                .as_u64()
                .filter(|&step| step > 0 && step <= u16::MAX as u64)
                .ok_or_else(|| err("metadata stepHours is invalid"))?;
            Ok((HOUR_SECONDS, uniform_offsets(first_hour, step, frame_count)?, 1))
        }
        (None, Some(listed)) => Ok((HOUR_SECONDS, listed_offsets(listed, frame_count, first_hour)?, 2)),
    }
}

/// The schemaVersion 3 time block: offsets on a declared unit.
fn parse_offset_axis(
    time: &serde_json::Map<String, serde_json::Value>,
    frame_count: u64,
) -> Result<(u32, Vec<u16>, u64), DecodeError> {
    for key in time.keys() {
        if key != "frameCount" && !V3_TIME_FIELDS.contains(&key.as_str()) {
            return Err(err("metadata time block has an unknown field"));
        }
    }
    let unit_seconds = time
        .get("unitSeconds")
        .and_then(|v| v.as_u64())
        .filter(|&unit| unit >= 1 && unit <= HOUR_SECONDS as u64 && HOUR_SECONDS as u64 % unit == 0)
        .ok_or_else(|| err("metadata unitSeconds must be a whole divisor of 3600"))?
        as u32;
    let first_offset = time
        .get("firstFrameOffset")
        .and_then(|v| v.as_u64())
        .filter(|&offset| offset <= u16::MAX as u64)
        .ok_or_else(|| err("metadata firstFrameOffset is invalid"))?;
    let frame_step = time.get("frameStep");
    let listed = time.get("frameOffsets");
    let offsets = match (frame_step, listed) {
        (Some(_), Some(_)) | (None, None) => {
            return Err(err("metadata time must declare exactly one of frameStep and frameOffsets"))
        }
        (Some(step), None) => {
            let step = step
                .as_u64()
                .filter(|&step| step > 0 && step <= u16::MAX as u64)
                .ok_or_else(|| err("metadata frameStep is invalid"))?;
            uniform_offsets(first_offset, step, frame_count)?
        }
        (None, Some(listed)) => listed_offsets(listed, frame_count, first_offset)?,
    };
    // The unit is the coarsest one that expresses every offset exactly, so an
    // axis has one encoding rather than one per divisor of its step.
    let mut divisor = u64::from(HOUR_SECONDS / unit_seconds);
    for &offset in &offsets {
        divisor = gcd(divisor, u64::from(offset));
    }
    if divisor != 1 {
        return Err(err("metadata unitSeconds is finer than the axis needs"));
    }
    Ok((unit_seconds, offsets, 3))
}

fn uniform_offsets(first: u64, step: u64, frame_count: u64) -> Result<Vec<u16>, DecodeError> {
    let last = first
        .checked_add((frame_count - 1).checked_mul(step).ok_or_else(|| err("offset overflow"))?)
        .ok_or_else(|| err("offset overflow"))?;
    if last > u16::MAX as u64 - 1 {
        return Err(err("frame offsets exceed the u16 range"));
    }
    Ok((0..frame_count).map(|frame| (first + frame * step) as u16).collect())
}

fn listed_offsets(
    listed: &serde_json::Value,
    frame_count: u64,
    first: u64,
) -> Result<Vec<u16>, DecodeError> {
    let listed = listed.as_array().ok_or_else(|| err("metadata listed time axis is invalid"))?;
    if listed.len() as u64 != frame_count {
        return Err(err("metadata listed time axis is invalid"));
    }
    let mut offsets = Vec::with_capacity(listed.len());
    for value in listed {
        let offset = value
            .as_u64()
            .filter(|&offset| offset <= u16::MAX as u64 - 1)
            .ok_or_else(|| err("metadata listed time axis is invalid"))?;
        if let Some(&previous) = offsets.last() {
            if offset as u16 <= previous {
                return Err(err("metadata listed time axis must be strictly increasing"));
            }
        }
        offsets.push(offset as u16);
    }
    if offsets[0] as u64 != first {
        return Err(err("metadata listed time axis must begin with the declared first offset"));
    }
    let mut steps = offsets.windows(2).map(|pair| pair[1] - pair[0]);
    let first_step = steps.next();
    if steps.all(|step| Some(step) == first_step) {
        return Err(err("a uniform axis must be encoded as a step"));
    }
    Ok(offsets)
}

/// Schema v3 variable identity: the GRIB2 parameter triple and fixed surface
/// every variable declares, plus the optional statistical process (code table
/// 4.10) a derived field carries.
const PARAMETER_CODE_FIELDS: [&str; 4] = [
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
];
const PARAMETER_NULLABLE_FIELDS: [&str; 3] = [
    "scaleFactorOfFirstFixedSurface",
    "scaledValueOfFirstFixedSurface",
    "typeOfStatisticalProcessing",
];

/// Validate a variable's GRIB2 identity block. The block is what
/// schemaVersion 3 introduces, so it must be present in a version 3 file and
/// absent below — the declared version is always the lowest able to express
/// the metadata (docs/bundle.md).
fn parse_parameter(
    parameter: Option<&serde_json::Value>,
    schema_version: u64,
) -> Result<(), DecodeError> {
    if schema_version < 3 {
        return match parameter {
            None => Ok(()),
            Some(_) => Err(err("a GRIB2 parameter block requires schemaVersion 3")),
        };
    }
    let parameter = parameter
        .and_then(|value| value.as_object())
        .ok_or_else(|| err("schemaVersion 3 requires a parameter block on every variable"))?;
    for key in parameter.keys() {
        if !PARAMETER_CODE_FIELDS.contains(&key.as_str())
            && !PARAMETER_NULLABLE_FIELDS.contains(&key.as_str())
        {
            return Err(err("metadata parameter block has an unknown field"));
        }
    }
    for field in PARAMETER_CODE_FIELDS {
        parameter
            .get(field)
            .and_then(|value| value.as_u64())
            .filter(|&code| code <= 255)
            .ok_or_else(|| err("metadata parameter code is invalid"))?;
    }
    let scale_factor = parameter
        .get("scaleFactorOfFirstFixedSurface")
        .ok_or_else(|| err("metadata parameter fixed surface value is incomplete"))?;
    let scaled_value = parameter
        .get("scaledValueOfFirstFixedSurface")
        .ok_or_else(|| err("metadata parameter fixed surface value is incomplete"))?;
    // GRIB2 encodes a surface with no value by writing both as missing; one
    // of the two alone describes nothing.
    if scale_factor.is_null() != scaled_value.is_null() {
        return Err(err("metadata parameter fixed surface must be wholly present or wholly null"));
    }
    if !scale_factor.is_null() {
        scale_factor
            .as_i64()
            .filter(|&factor| (-127..=127).contains(&factor))
            .ok_or_else(|| err("metadata parameter scaleFactorOfFirstFixedSurface is invalid"))?;
        scaled_value
            .as_u64()
            .filter(|&value| value <= 0xFFFF_FFFE)
            .ok_or_else(|| err("metadata parameter scaledValueOfFirstFixedSurface is invalid"))?;
    }
    if let Some(statistical) = parameter.get("typeOfStatisticalProcessing") {
        if !statistical.is_null() {
            statistical
                .as_u64()
                .filter(|&code| code <= 255)
                .ok_or_else(|| err("metadata parameter typeOfStatisticalProcessing is invalid"))?;
        }
    }
    Ok(())
}

/// The optional blocks beside `parameter` (docs/bundle.md §"Band, Producer
/// and Aerosol"): the spectral band a satellite image was taken in, in the
/// fields of GRIB2 product definition template 4.31 with their byte widths,
/// the algorithm that derived a composite field, and an aerosol product's
/// type and intervals in the fields of template 4.48. Each is present whole
/// or absent, needs schemaVersion 3, and raises no version floor.
const BAND_FIELDS: [(&str, i64, i64); 5] = [
    ("satelliteSeries", 0, 0xFFFF),
    ("satelliteNumber", 0, 0xFFFF),
    ("instrumentType", 0, 0xFFFF),
    ("scaleFactorOfCentralWaveNumber", -127, 127),
    ("scaledValueOfCentralWaveNumber", 0, 0xFFFF_FFFE),
];

fn parse_band(variable: &serde_json::Value, schema_version: u64) -> Result<(), DecodeError> {
    let Some(band) = variable.get("band") else {
        return Ok(());
    };
    if schema_version < 3 {
        return Err(err("a band block requires schemaVersion 3"));
    }
    let band = band.as_object().ok_or_else(|| err("metadata band must be an object"))?;
    if band.len() != BAND_FIELDS.len() {
        return Err(err("metadata band block must carry exactly its five fields"));
    }
    for (field, low, high) in BAND_FIELDS {
        band.get(field)
            .and_then(|value| value.as_i64())
            .filter(|value| (low..=high).contains(value))
            .ok_or_else(|| err("metadata band field is invalid"))?;
    }
    Ok(())
}

/// The producer block: the algorithm's id and its version, nothing else.
const PRODUCER_FIELDS: [&str; 2] = ["id", "version"];

fn parse_producer(variable: &serde_json::Value, schema_version: u64) -> Result<(), DecodeError> {
    let Some(producer) = variable.get("producer") else {
        return Ok(());
    };
    if schema_version < 3 {
        return Err(err("a producer block requires schemaVersion 3"));
    }
    let producer = producer
        .as_object()
        .ok_or_else(|| err("metadata producer must be an object"))?;
    if producer.len() != PRODUCER_FIELDS.len() {
        return Err(err("metadata producer block must carry exactly id and version"));
    }
    let [id_field, version_field] = PRODUCER_FIELDS;
    let id = producer
        .get(id_field)
        .and_then(|value| value.as_str())
        .ok_or_else(|| err("metadata producer id is invalid"))?;
    let mut chars = id.chars();
    let well_formed = chars.next().is_some_and(|c| c.is_ascii_lowercase())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    if !well_formed {
        return Err(err("metadata producer id is invalid"));
    }
    producer
        .get(version_field)
        .and_then(|value| value.as_str())
        .filter(|version| !version.is_empty())
        .ok_or_else(|| err("metadata producer version is invalid"))?;
    Ok(())
}

/// The aerosol block's type fields, plain integers with their byte widths;
/// each interval is a code table 4.91 type and two limits, a limit a
/// (scale factor, scaled value) pair that is whole or null like the fixed
/// surface's, and null throughout when the interval's type is 255
/// (missing).
const AEROSOL_MISSING_INTERVAL: i64 = 255;
const AEROSOL_TYPE_FIELDS: [(&str, i64); 3] = [
    ("aerosolType", 0xFFFF),
    ("typeOfSizeInterval", 0xFF),
    ("typeOfWavelengthInterval", 0xFF),
];
const AEROSOL_INTERVALS: [(&str, [(&str, &str); 2]); 2] = [
    (
        "typeOfSizeInterval",
        [
            ("scaleFactorOfFirstSize", "scaledValueOfFirstSize"),
            ("scaleFactorOfSecondSize", "scaledValueOfSecondSize"),
        ],
    ),
    (
        "typeOfWavelengthInterval",
        [
            ("scaleFactorOfFirstWavelength", "scaledValueOfFirstWavelength"),
            ("scaleFactorOfSecondWavelength", "scaledValueOfSecondWavelength"),
        ],
    ),
];

fn parse_aerosol(variable: &serde_json::Value, schema_version: u64) -> Result<(), DecodeError> {
    let Some(aerosol) = variable.get("aerosol") else {
        return Ok(());
    };
    if schema_version < 3 {
        return Err(err("an aerosol block requires schemaVersion 3"));
    }
    let aerosol = aerosol
        .as_object()
        .ok_or_else(|| err("metadata aerosol must be an object"))?;
    // Exactly the eleven fields: the three types and the four limits.
    if aerosol.len() != AEROSOL_TYPE_FIELDS.len() + 4 * 2 {
        return Err(err("metadata aerosol block must carry exactly its eleven fields"));
    }
    for (field, high) in AEROSOL_TYPE_FIELDS {
        aerosol
            .get(field)
            .and_then(|value| value.as_i64())
            .filter(|value| (0..=high).contains(value))
            .ok_or_else(|| err("metadata aerosol type field is invalid"))?;
    }
    for (type_field, pairs) in AEROSOL_INTERVALS {
        let missing = aerosol[type_field].as_i64() == Some(AEROSOL_MISSING_INTERVAL);
        for (scale_field, value_field) in pairs {
            let scale_factor = aerosol
                .get(scale_field)
                .ok_or_else(|| err("metadata aerosol limit is incomplete"))?;
            let scaled_value = aerosol
                .get(value_field)
                .ok_or_else(|| err("metadata aerosol limit is incomplete"))?;
            if scale_factor.is_null() != scaled_value.is_null() {
                return Err(err("metadata aerosol limit must be wholly present or wholly null"));
            }
            if scale_factor.is_null() != missing {
                return Err(err("metadata aerosol limit must be null exactly when its interval is missing"));
            }
            if missing {
                continue;
            }
            scale_factor
                .as_i64()
                .filter(|factor| (-127..=127).contains(factor))
                .ok_or_else(|| err("metadata aerosol scale factor is invalid"))?;
            scaled_value
                .as_u64()
                .filter(|&value| value <= 0xFFFF_FFFE)
                .ok_or_else(|| err("metadata aerosol scaled value is invalid"))?;
        }
    }
    Ok(())
}

/// The ensemble block's three shapes (docs/bundle.md §"Ensemble"): which
/// keys it carries says which statistic of the members the variable is.
pub(crate) const ENSEMBLE_DERIVED_FIELD: &str = "derivedForecast";
pub(crate) const ENSEMBLE_PERCENTILE_FIELD: &str = "percentileValue";
pub(crate) const ENSEMBLE_PROBABILITY_TYPE_FIELD: &str = "probabilityType";
/// A probability's two limits, each a (scale factor, scaled value) pair.
pub(crate) const ENSEMBLE_LIMITS: [(&str, &str); 2] = [
    ("scaleFactorOfLowerLimit", "scaledValueOfLowerLimit"),
    ("scaleFactorOfUpperLimit", "scaledValueOfUpperLimit"),
];

/// One `(scaleFactor, scaledValue)` limit, `None` where both are null.
pub type EnsembleLimit = Option<(i8, u32)>;

/// What an ensemble product is a statistic of its members as: the identity
/// GRIB2 carries in product definition templates 4.2 / 4.12 (a derived
/// forecast, code table 4.7), 4.6 / 4.10 (a percentile) and 4.5 / 4.9 (a
/// probability, code table 4.9). The metadata block and the registry's
/// `ensemble` blocks parse into it, and a GRIB record's template is read
/// into it to be compared whole.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnsembleIdentity {
    Derived(u8),
    Percentile(u8),
    Probability {
        probability_type: u8,
        lower: EnsembleLimit,
        upper: EnsembleLimit,
    },
}

impl EnsembleIdentity {
    /// Which limits a probability type uses, `(lower, upper)`: types 0, 3
    /// and 5 the lower, 1 and 4 the upper, 2 both.
    pub fn limits_used(probability_type: u8) -> (bool, bool) {
        match probability_type {
            1 | 4 => (false, true),
            2 => (true, true),
            _ => (true, false),
        }
    }

}

/// What only the encoder needs: building an identity off a record and
/// writing it back out.
#[cfg(any(feature = "encoder", test))]
impl EnsembleIdentity {
    /// A probability with the limits its type does not use dropped, whatever
    /// the record wrote there (CMA writes the threshold into both).
    pub fn probability(probability_type: u8, lower: EnsembleLimit, upper: EnsembleLimit) -> Self {
        let (uses_lower, uses_upper) = Self::limits_used(probability_type);
        EnsembleIdentity::Probability {
            probability_type,
            lower: lower.filter(|_| uses_lower),
            upper: upper.filter(|_| uses_upper),
        }
    }

    /// The product definition templates that carry this shape:
    /// `(instantaneous, over a time interval)`.
    pub fn templates(&self) -> (u16, u16) {
        match self {
            EnsembleIdentity::Derived(_) => (2, 12),
            EnsembleIdentity::Percentile(_) => (6, 10),
            EnsembleIdentity::Probability { .. } => (5, 9),
        }
    }

    /// The block as written, keys in the order docs/bundle.md lists them.
    pub fn metadata(&self) -> serde_json::Map<String, serde_json::Value> {
        use serde_json::Value;
        let mut block = serde_json::Map::new();
        match *self {
            EnsembleIdentity::Derived(code) => {
                block.insert(ENSEMBLE_DERIVED_FIELD.into(), code.into());
            }
            EnsembleIdentity::Percentile(value) => {
                block.insert(ENSEMBLE_PERCENTILE_FIELD.into(), value.into());
            }
            EnsembleIdentity::Probability {
                probability_type,
                lower,
                upper,
            } => {
                block.insert(ENSEMBLE_PROBABILITY_TYPE_FIELD.into(), probability_type.into());
                for ((scale_field, value_field), limit) in ENSEMBLE_LIMITS.into_iter().zip([lower, upper]) {
                    let (scale, value) = match limit {
                        Some((scale, value)) => (Value::from(scale), Value::from(value)),
                        None => (Value::Null, Value::Null),
                    };
                    block.insert(scale_field.into(), scale);
                    block.insert(value_field.into(), value);
                }
            }
        }
        block
    }
}

impl EnsembleIdentity {
    /// A block as docs/bundle.md's Error Handling admits it: exactly one of
    /// the three shapes, every value in range, each limit whole or null,
    /// and null exactly where the probability type does not use it.
    pub fn from_block(block: &serde_json::Map<String, serde_json::Value>) -> Result<Self, DecodeError> {
        let code = |field: &str, high: u64| {
            block
                .get(field)
                .and_then(|value| value.as_u64())
                .filter(|&value| value <= high)
                .ok_or_else(|| err(format!("metadata ensemble {field} is invalid")))
        };
        let has = |field: &str| block.contains_key(field);
        if has(ENSEMBLE_DERIVED_FIELD) {
            if block.len() != 1 {
                return Err(err("metadata ensemble derivedForecast must stand alone"));
            }
            return Ok(EnsembleIdentity::Derived(code(ENSEMBLE_DERIVED_FIELD, 255)? as u8));
        }
        if has(ENSEMBLE_PERCENTILE_FIELD) {
            if block.len() != 1 {
                return Err(err("metadata ensemble percentileValue must stand alone"));
            }
            return Ok(EnsembleIdentity::Percentile(code(ENSEMBLE_PERCENTILE_FIELD, 100)? as u8));
        }
        if !has(ENSEMBLE_PROBABILITY_TYPE_FIELD) {
            return Err(err("metadata ensemble block is none of its three shapes"));
        }
        if block.len() != 1 + 2 * ENSEMBLE_LIMITS.len() {
            return Err(err("metadata ensemble probability must carry exactly its five fields"));
        }
        let probability_type = code(ENSEMBLE_PROBABILITY_TYPE_FIELD, 5)? as u8;
        let used = Self::limits_used(probability_type);
        let mut limits: [EnsembleLimit; 2] = [None, None];
        for (index, (scale_field, value_field)) in ENSEMBLE_LIMITS.into_iter().enumerate() {
            let scale_factor = block
                .get(scale_field)
                .ok_or_else(|| err("metadata ensemble limit is incomplete"))?;
            let scaled_value = block
                .get(value_field)
                .ok_or_else(|| err("metadata ensemble limit is incomplete"))?;
            if scale_factor.is_null() != scaled_value.is_null() {
                return Err(err("metadata ensemble limit must be wholly present or wholly null"));
            }
            let uses = if index == 0 { used.0 } else { used.1 };
            if scale_factor.is_null() == uses {
                return Err(err("metadata ensemble limit must be null exactly where its type does not use it"));
            }
            if !uses {
                continue;
            }
            let scale_factor = scale_factor
                .as_i64()
                .filter(|factor| (-127..=127).contains(factor))
                .ok_or_else(|| err("metadata ensemble scale factor is invalid"))?;
            let scaled_value = scaled_value
                .as_u64()
                .filter(|&value| value <= 0xFFFF_FFFE)
                .ok_or_else(|| err("metadata ensemble scaled value is invalid"))?;
            limits[index] = Some((scale_factor as i8, scaled_value as u32));
        }
        Ok(EnsembleIdentity::Probability {
            probability_type,
            lower: limits[0],
            upper: limits[1],
        })
    }
}

fn parse_ensemble(variable: &serde_json::Value, schema_version: u64) -> Result<(), DecodeError> {
    let Some(ensemble) = variable.get("ensemble") else {
        return Ok(());
    };
    if schema_version < 3 {
        return Err(err("an ensemble block requires schemaVersion 3"));
    }
    let ensemble = ensemble
        .as_object()
        .ok_or_else(|| err("metadata ensemble must be an object"))?;
    EnsembleIdentity::from_block(ensemble)?;
    Ok(())
}

/// The fields a regular grid names and a projected one must not.
const REGULAR_GRID_FIELDS: [&str; 4] =
    ["firstLongitude", "firstLatitude", "longitudeStep", "latitudeStep"];
/// The closed set of `projection.kind`; a grid of any other kind is one this
/// reader cannot place on the map.
const PROJECTION_KINDS: [&str; 1] = ["lambert-conformal"];
const PROJECTION_NUMBER_FIELDS: [&str; 9] = [
    "radius",
    "standardParallel1",
    "standardParallel2",
    "latitudeOfOrigin",
    "centralMeridian",
    "x0",
    "y0",
    "dx",
    "dy",
];
const GAUSSIAN_FIELDS: [&str; 3] = ["n", "firstRow", "rowStride"];
/// Each latitude costs a few passes of a recurrence of order `2n`, so `n` is
/// bounded before a point read pays for it.
const MAX_GAUSSIAN_N: u64 = 32768;

/// A validated `grid.projection` block.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct ProjectionBlock {
    pub(crate) projection: LambertConformal,
    pub(crate) x0: f64,
    pub(crate) y0: f64,
    pub(crate) dx: f64,
    pub(crate) dy: f64,
}

/// A projected grid's own fields: a closed block of known kind, finite
/// numbers, a cone that exists (its two parallels neither at a pole nor
/// mirrored across the equator, where the constant is zero), and steps in the
/// grid's row and column order.
pub(crate) fn parse_projection(block: &serde_json::Value) -> Result<ProjectionBlock, DecodeError> {
    let block = block
        .as_object()
        .ok_or_else(|| err("metadata grid projection must be an object"))?;
    let kind = block
        .get("kind")
        .and_then(|value| value.as_str())
        .ok_or_else(|| err("metadata grid projection kind is invalid"))?;
    if !PROJECTION_KINDS.contains(&kind) {
        return Err(err("metadata grid projection kind is not one this reader implements"));
    }
    for key in block.keys() {
        if key != "kind" && !PROJECTION_NUMBER_FIELDS.contains(&key.as_str()) {
            return Err(err("metadata grid projection has an unknown field"));
        }
    }
    let number = |field: &str| -> Result<f64, DecodeError> {
        block
            .get(field)
            .and_then(|value| value.as_f64())
            .filter(|value| value.is_finite())
            .ok_or_else(|| err(format!("metadata grid projection {field} is invalid")))
    };
    let radius = number("radius")?;
    let standard_parallel_1 = number("standardParallel1")?;
    let standard_parallel_2 = number("standardParallel2")?;
    let latitude_of_origin = number("latitudeOfOrigin")?;
    let central_meridian = number("centralMeridian")?;
    let dx = number("dx")?;
    let dy = number("dy")?;
    if radius <= 0.0 {
        return Err(err("metadata grid projection radius must be positive"));
    }
    for latitude in [standard_parallel_1, standard_parallel_2, latitude_of_origin] {
        if latitude.abs() >= 90.0 {
            return Err(err("metadata grid projection latitudes must lie between the poles"));
        }
    }
    if standard_parallel_1 + standard_parallel_2 == 0.0 {
        return Err(err("metadata grid projection parallels leave no cone"));
    }
    if central_meridian.abs() > 180.0 {
        return Err(err("metadata grid projection centralMeridian is out of range"));
    }
    if dx <= 0.0 || dy >= 0.0 {
        return Err(err("metadata grid projection steps must run east and south"));
    }
    Ok(ProjectionBlock {
        projection: LambertConformal {
            radius,
            standard_parallel_1,
            standard_parallel_2,
            latitude_of_origin,
            central_meridian,
        },
        x0: number("x0")?,
        y0: number("y0")?,
        dx,
        dy,
    })
}

/// A `grid.gaussian` block: closed, and every one of the grid's `height` rows
/// a row of the Gaussian grid it names.
pub(crate) fn parse_gaussian(
    block: &serde_json::Value,
    height: u64,
) -> Result<GaussianRows, DecodeError> {
    let block = block
        .as_object()
        .ok_or_else(|| err("metadata grid gaussian must be an object"))?;
    if block.len() != GAUSSIAN_FIELDS.len() {
        return Err(err("metadata grid gaussian block must carry exactly n, firstRow and rowStride"));
    }
    let integer = |field: &str| -> Result<u64, DecodeError> {
        block
            .get(field)
            .and_then(|value| value.as_u64())
            .ok_or_else(|| err(format!("metadata grid gaussian {field} is invalid")))
    };
    let n = integer("n")?;
    let first_row = integer("firstRow")?;
    let row_stride = integer("rowStride")?;
    if n == 0 || n > MAX_GAUSSIAN_N {
        return Err(err("metadata grid gaussian n is out of range"));
    }
    if row_stride == 0 {
        return Err(err("metadata grid gaussian rowStride must be positive"));
    }
    let last_row = height
        .checked_sub(1)
        .and_then(|rows| rows.checked_mul(row_stride))
        .and_then(|rows| rows.checked_add(first_row))
        .ok_or_else(|| err("metadata grid gaussian rows overflow"))?;
    if last_row >= 2 * n {
        return Err(err("metadata grid gaussian rows run past the Gaussian grid"));
    }
    Ok(GaussianRows {
        n: n as u32,
        first_row: first_row as u32,
        row_stride: row_stride as u32,
    })
}

/// What kind of grid the block is, and the lowest schema version that kind
/// needs: 4 for a projection, which replaces the regular fields outright, and
/// no floor for a regular grid. A `gaussian` block sits beside the regular
/// fields a reader that ignores it still uses, so it needs version 3 and
/// raises nothing. Versions up to 3 keep whatever regular fields they have.
fn parse_grid_block(
    grid: &serde_json::Map<String, serde_json::Value>,
    height: u64,
    schema_version: u64,
) -> Result<u64, DecodeError> {
    let Some(projection) = grid.get("projection") else {
        if let Some(gaussian) = grid.get("gaussian") {
            if schema_version != 3 {
                return Err(err("a gaussian block requires schemaVersion 3"));
            }
            parse_gaussian(gaussian, height)?;
        }
        return Ok(1);
    };
    parse_projection(projection)?;
    if REGULAR_GRID_FIELDS.iter().any(|field| grid.contains_key(*field)) {
        return Err(err("a projected grid must not carry the regular grid fields"));
    }
    if grid.contains_key("gaussian") {
        return Err(err("a projected grid must not carry a gaussian block"));
    }
    if grid.get("wrapLongitude").and_then(|value| value.as_bool()) != Some(false) {
        return Err(err("a projected grid must declare wrapLongitude false"));
    }
    Ok(4)
}

pub(crate) fn parse_metadata(raw: &[u8]) -> Result<Metadata, DecodeError> {
    let text = std::str::from_utf8(raw).map_err(|_| err("metadata is not UTF-8"))?;
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|_| err("metadata is not valid JSON"))?;
    let object = value.as_object().ok_or_else(|| err("metadata must be a JSON object"))?;
    let schema_version = match object.get("schemaVersion").and_then(|v| v.as_u64()) {
        Some(version @ (1 | 2 | 3 | 4)) => version,
        _ => return Err(err("unsupported metadata schemaVersion")),
    };
    let grid = object
        .get("grid")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("metadata grid missing"))?;
    let width = grid.get("width").and_then(|v| v.as_u64()).ok_or_else(|| err("grid width missing"))?;
    let height = grid.get("height").and_then(|v| v.as_u64()).ok_or_else(|| err("grid height missing"))?;
    if width == 0 || height == 0 {
        return Err(err("grid dimensions must be positive"));
    }
    let plane_length = width
        .checked_mul(height)
        .filter(|&points| points <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("grid exceeds the plane safety limit"))?;
    let grid_version = parse_grid_block(grid, height, schema_version)?;
    let time = object
        .get("time")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("metadata time missing"))?;
    let frame_count = time
        .get("frameCount")
        .and_then(|v| v.as_u64())
        .filter(|&count| count > 0 && count <= u16::MAX as u64)
        .ok_or_else(|| err("metadata frameCount is invalid"))?;
    let (unit_seconds, offsets, axis_version) = parse_time_axis(time, frame_count, schema_version)?;
    let variables = object
        .get("variables")
        .and_then(|v| v.as_array())
        .ok_or_else(|| err("metadata variables missing"))?;
    let mut variable_ids = Vec::new();
    let mut parameters = 0usize;
    for variable in variables {
        let numeric = variable
            .get("numericId")
            .and_then(|v| v.as_u64())
            .filter(|&id| (1..=255).contains(&id))
            .ok_or_else(|| err("variable numericId is invalid"))?;
        if variable.get("id").and_then(|v| v.as_str()).is_none() {
            return Err(err("variable id is invalid"));
        }
        if variable_ids.contains(&(numeric as u8)) {
            return Err(err("duplicate variable numericId"));
        }
        let parameter = variable.get("parameter");
        parse_parameter(parameter, schema_version)?;
        parameters += usize::from(parameter.is_some());
        parse_band(variable, schema_version)?;
        parse_producer(variable, schema_version)?;
        parse_aerosol(variable, schema_version)?;
        parse_ensemble(variable, schema_version)?;
        variable_ids.push(numeric as u8);
    }
    if variable_ids.is_empty() {
        return Err(err("metadata must declare at least one variable"));
    }
    // Every axis, variable set and grid has exactly one valid encoding: the
    // declared version must be the lowest able to express all three.
    let required_version = axis_version.max(if parameters > 0 { 3 } else { 1 }).max(grid_version);
    if schema_version != required_version {
        return Err(err("metadata declares a schemaVersion other than the lowest it needs"));
    }
    Ok(Metadata {
        json: text.to_owned(),
        schema_version: schema_version as u8,
        width: width as u32,
        height: height as u32,
        plane_length: plane_length as u32,
        frame_count: frame_count as u32,
        unit_seconds,
        offsets,
        variable_ids,
    })
}

#[cfg(test)]
mod time_axis_tests {
    use super::parse_time_axis;

    fn time(json: &str) -> serde_json::Map<String, serde_json::Value> {
        serde_json::from_str::<serde_json::Value>(json)
            .unwrap()
            .as_object()
            .unwrap()
            .clone()
    }

    #[test]
    fn legacy_hour_axes_still_parse() {
        // Schema versions 1 and 2 describe a whole-hour axis; their offsets
        // are the forecast hours themselves.
        let (unit, offsets, version) =
            parse_time_axis(&time(r#"{"firstForecastHour": 6, "stepHours": 3}"#), 4, 1).expect("uniform");
        assert_eq!((unit, offsets, version), (3600, vec![6, 9, 12, 15], 1));
        let (unit, offsets, version) = parse_time_axis(
            &time(r#"{"firstForecastHour": 0, "hours": [0, 1, 2, 3, 6, 9]}"#),
            6,
            2,
        )
        .expect("listed");
        assert_eq!((unit, offsets, version), (3600, vec![0, 1, 2, 3, 6, 9], 2));
    }

    #[test]
    fn version_3_carries_its_own_unit() {
        let (unit, offsets, version) = parse_time_axis(
            &time(r#"{"unitSeconds": 360, "firstFrameOffset": 0, "frameStep": 1}"#),
            4,
            3,
        )
        .expect("six-minute axis");
        assert_eq!((unit, offsets, version), (360, vec![0, 1, 2, 3], 3));
        let (unit, offsets, _) = parse_time_axis(
            &time(r#"{"unitSeconds": 360, "firstFrameOffset": 0, "frameOffsets": [0, 1, 2, 4]}"#),
            4,
            3,
        )
        .expect("gapped six-minute axis");
        assert_eq!((unit, offsets), (360, vec![0, 1, 2, 4]));
    }

    #[test]
    fn the_two_time_block_shapes_never_mix() {
        // A version 3 block in a version 2 file, and the reverse.
        assert!(parse_time_axis(&time(r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameStep": 1}"#), 3, 2).is_err());
        assert!(parse_time_axis(&time(r#"{"firstForecastHour": 0, "stepHours": 1}"#), 3, 3).is_err());
    }

    #[test]
    fn every_axis_has_exactly_one_encoding() {
        // Both fields, or neither.
        assert!(parse_time_axis(
            &time(r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameStep": 1, "frameOffsets": [0, 1, 3]}"#),
            3,
            3
        )
        .is_err());
        assert!(parse_time_axis(&time(r#"{"unitSeconds": 3600, "firstFrameOffset": 0}"#), 3, 3).is_err());
        // A uniform offset list must be encoded as a step.
        assert!(parse_time_axis(
            &time(r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [0, 3, 6]}"#),
            3,
            3
        )
        .is_err());
        // The unit must be the coarsest one the axis needs: an hourly series
        // cannot declare itself six-minute.
        assert!(parse_time_axis(
            &time(r#"{"unitSeconds": 360, "firstFrameOffset": 0, "frameStep": 10}"#),
            3,
            3
        )
        .is_err());
        // ...and the unit must divide an hour.
        assert!(parse_time_axis(
            &time(r#"{"unitSeconds": 7, "firstFrameOffset": 0, "frameStep": 1}"#),
            3,
            3
        )
        .is_err());
    }

    #[test]
    fn invalid_offset_axes_rejected() {
        // Not strictly increasing, wrong length, wrong first element.
        for json in [
            r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [0, 1, 1, 3]}"#,
            r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [0, 1, 3]}"#,
            r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [1, 2, 4]}"#,
            // 65535 is the dependencyOffset sentinel.
            r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [0, 1, 65535]}"#,
            // A field the block does not define.
            r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameStep": 1, "hours": [0, 1, 2]}"#,
        ] {
            assert!(parse_time_axis(&time(json), 4, 3).is_err(), "{json}");
        }
        assert!(parse_time_axis(
            &time(r#"{"unitSeconds": 3600, "firstFrameOffset": 0, "frameOffsets": [0, 1, 65534]}"#),
            3,
            3
        )
        .is_ok());
    }

    #[test]
    fn uniform_axis_u16_bound_enforced() {
        assert!(parse_time_axis(&time(r#"{"unitSeconds": 3600, "firstFrameOffset": 65534, "frameStep": 1}"#), 2, 3).is_err());
        assert!(parse_time_axis(&time(r#"{"unitSeconds": 3600, "firstFrameOffset": 65534, "frameStep": 1}"#), 1, 3).is_ok());
    }
}

#[cfg(test)]
mod parameter_tests {
    use super::parse_parameter;

    fn parameter(json: &str) -> serde_json::Value {
        serde_json::from_str(json).unwrap()
    }

    const TMP2M: &str = r#"{
        "discipline": 0,
        "parameterCategory": 0,
        "parameterNumber": 0,
        "typeOfFirstFixedSurface": 103,
        "scaleFactorOfFirstFixedSurface": 0,
        "scaledValueOfFirstFixedSurface": 2
    }"#;

    #[test]
    fn version_3_requires_the_block_and_below_forbids_it() {
        assert!(parse_parameter(Some(&parameter(TMP2M)), 3).is_ok());
        assert!(parse_parameter(None, 3).is_err());
        assert!(parse_parameter(None, 1).is_ok());
        assert!(parse_parameter(None, 2).is_ok());
        assert!(parse_parameter(Some(&parameter(TMP2M)), 2).is_err());
    }

    #[test]
    fn a_surface_without_a_value_writes_both_halves_null() {
        let entire_atmosphere = parameter(
            r#"{"discipline": 0, "parameterCategory": 16, "parameterNumber": 5,
                "typeOfFirstFixedSurface": 10,
                "scaleFactorOfFirstFixedSurface": null,
                "scaledValueOfFirstFixedSurface": null}"#,
        );
        assert!(parse_parameter(Some(&entire_atmosphere), 3).is_ok());
        let half = parameter(
            r#"{"discipline": 0, "parameterCategory": 16, "parameterNumber": 5,
                "typeOfFirstFixedSurface": 10,
                "scaleFactorOfFirstFixedSurface": 0,
                "scaledValueOfFirstFixedSurface": null}"#,
        );
        assert!(parse_parameter(Some(&half), 3).is_err());
    }

    #[test]
    fn invalid_blocks_rejected() {
        for json in [
            // Out of the u8 code range, wrong type, missing half of the surface,
            // and a field the schema does not define.
            r#"{"discipline": 256, "parameterCategory": 0, "parameterNumber": 0,
                "typeOfFirstFixedSurface": 1, "scaleFactorOfFirstFixedSurface": null,
                "scaledValueOfFirstFixedSurface": null}"#,
            r#"{"discipline": "0", "parameterCategory": 0, "parameterNumber": 0,
                "typeOfFirstFixedSurface": 1, "scaleFactorOfFirstFixedSurface": null,
                "scaledValueOfFirstFixedSurface": null}"#,
            r#"{"discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                "typeOfFirstFixedSurface": 1, "scaleFactorOfFirstFixedSurface": null}"#,
            r#"{"discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                "typeOfFirstFixedSurface": 1, "scaleFactorOfFirstFixedSurface": null,
                "scaledValueOfFirstFixedSurface": null, "levelValue": 2}"#,
        ] {
            assert!(parse_parameter(Some(&parameter(json)), 3).is_err(), "{json}");
        }
    }
}

#[cfg(test)]
mod band_tests {
    use super::{parse_aerosol, parse_band, parse_ensemble, parse_producer, EnsembleIdentity};

    fn variable(json: &str) -> serde_json::Value {
        serde_json::from_str(json).unwrap()
    }

    const IR104: &str = r#"{"band": {
        "satelliteSeries": 0, "satelliteNumber": 174, "instrumentType": 297,
        "scaleFactorOfCentralWaveNumber": 0, "scaledValueOfCentralWaveNumber": 96061}}"#;

    #[test]
    fn a_band_is_optional_and_needs_version_3() {
        assert!(parse_band(&variable("{}"), 1).is_ok());
        assert!(parse_band(&variable("{}"), 3).is_ok());
        assert!(parse_band(&variable(IR104), 3).is_ok());
        assert!(parse_band(&variable(IR104), 2).is_err());
    }

    #[test]
    fn malformed_bands_rejected() {
        for json in [
            // Null, a missing field, one out of range, one of the wrong type,
            // and a key the block does not define.
            r#"{"band": null}"#,
            r#"{"band": {"satelliteSeries": 0, "satelliteNumber": 174, "instrumentType": 297,
                "scaleFactorOfCentralWaveNumber": 0}}"#,
            r#"{"band": {"satelliteSeries": 0, "satelliteNumber": 65536, "instrumentType": 297,
                "scaleFactorOfCentralWaveNumber": 0, "scaledValueOfCentralWaveNumber": 96061}}"#,
            r#"{"band": {"satelliteSeries": 0, "satelliteNumber": "174", "instrumentType": 297,
                "scaleFactorOfCentralWaveNumber": 0, "scaledValueOfCentralWaveNumber": 96061}}"#,
            r#"{"band": {"satelliteSeries": 0, "satelliteNumber": 174, "instrumentType": 297,
                "scaleFactorOfCentralWaveNumber": 0, "scaledValueOfCentralWaveNumber": 96061,
                "channel": 13}}"#,
        ] {
            assert!(parse_band(&variable(json), 3).is_err(), "{json}");
        }
    }

    #[test]
    fn producer_is_an_id_and_a_version() {
        assert!(parse_producer(&variable("{}"), 3).is_ok());
        let dust = r#"{"producer": {"id": "shachen", "version": "0.3.1"}}"#;
        assert!(parse_producer(&variable(dust), 3).is_ok());
        assert!(parse_producer(&variable(dust), 2).is_err());
        for json in [
            r#"{"producer": null}"#,
            r#"{"producer": {"id": "shachen"}}"#,
            r#"{"producer": {"id": "Shachen", "version": "0.3.1"}}"#,
            r#"{"producer": {"id": "shachen", "version": ""}}"#,
            r#"{"producer": {"id": "shachen", "version": "0.3.1", "url": "x"}}"#,
        ] {
            assert!(parse_producer(&variable(json), 3).is_err(), "{json}");
        }
    }

    const DUST_AOD: &str = r#"{"aerosol": {
        "aerosolType": 62001, "typeOfSizeInterval": 0,
        "scaleFactorOfFirstSize": 6, "scaledValueOfFirstSize": 20,
        "scaleFactorOfSecondSize": 0, "scaledValueOfSecondSize": 0,
        "typeOfWavelengthInterval": 7,
        "scaleFactorOfFirstWavelength": 9, "scaledValueOfFirstWavelength": 545,
        "scaleFactorOfSecondWavelength": 9, "scaledValueOfSecondWavelength": 565}}"#;
    const PM25: &str = r#"{"aerosol": {
        "aerosolType": 62000, "typeOfSizeInterval": 0,
        "scaleFactorOfFirstSize": 7, "scaledValueOfFirstSize": 25,
        "scaleFactorOfSecondSize": 0, "scaledValueOfSecondSize": 0,
        "typeOfWavelengthInterval": 255,
        "scaleFactorOfFirstWavelength": null, "scaledValueOfFirstWavelength": null,
        "scaleFactorOfSecondWavelength": null, "scaledValueOfSecondWavelength": null}}"#;

    #[test]
    fn an_aerosol_block_is_optional_and_needs_version_3() {
        assert!(parse_aerosol(&variable("{}"), 1).is_ok());
        assert!(parse_aerosol(&variable("{}"), 3).is_ok());
        assert!(parse_aerosol(&variable(DUST_AOD), 3).is_ok());
        assert!(parse_aerosol(&variable(PM25), 3).is_ok());
        assert!(parse_aerosol(&variable(DUST_AOD), 2).is_err());
    }

    #[test]
    fn malformed_aerosol_blocks_rejected() {
        let with = |edit: &str| DUST_AOD.replacen(r#""scaleFactorOfFirstWavelength": 9"#, edit, 1);
        let pm_with = |edit: &str| PM25.replacen(r#""scaleFactorOfFirstWavelength": null"#, edit, 1);
        for json in [
            // Null, a missing field, a key the block does not define, a
            // type out of range or of the wrong type, one half of a limit
            // null, a scale factor or scaled value out of range, and a
            // limit that is present under a missing interval or null under
            // a present one.
            r#"{"aerosol": null}"#.to_string(),
            DUST_AOD.replacen(r#""scaleFactorOfSecondSize": 0, "#, "", 1),
            DUST_AOD.replacen(r#""aerosolType": 62001,"#, r#""aerosolType": 62001, "species": "dust","#, 1),
            DUST_AOD.replacen(r#""aerosolType": 62001"#, r#""aerosolType": 65536"#, 1),
            DUST_AOD.replacen(r#""typeOfSizeInterval": 0"#, r#""typeOfSizeInterval": 256"#, 1),
            DUST_AOD.replacen(r#""aerosolType": 62001"#, r#""aerosolType": "62001""#, 1),
            with(r#""scaleFactorOfFirstWavelength": null"#),
            with(r#""scaleFactorOfFirstWavelength": 128"#),
            with(r#""scaleFactorOfFirstWavelength": -128"#),
            DUST_AOD.replacen(r#""scaledValueOfFirstWavelength": 545"#, r#""scaledValueOfFirstWavelength": 4294967295"#, 1),
            DUST_AOD.replacen(r#""scaledValueOfFirstWavelength": 545"#, r#""scaledValueOfFirstWavelength": -1"#, 1),
            DUST_AOD.replacen(r#""typeOfWavelengthInterval": 7"#, r#""typeOfWavelengthInterval": 255"#, 1),
            PM25.replacen(r#""typeOfWavelengthInterval": 255"#, r#""typeOfWavelengthInterval": 7"#, 1),
            pm_with(r#""scaleFactorOfFirstWavelength": 9"#),
        ] {
            assert!(parse_aerosol(&variable(&json), 3).is_err(), "{json}");
        }
    }

    const SPREAD: &str = r#"{"ensemble": {"derivedForecast": 4}}"#;
    const QUARTILE: &str = r#"{"ensemble": {"percentileValue": 75}}"#;
    const ABOVE_10_MM: &str = r#"{"ensemble": {
        "probabilityType": 3,
        "scaleFactorOfLowerLimit": 2, "scaledValueOfLowerLimit": 1000,
        "scaleFactorOfUpperLimit": null, "scaledValueOfUpperLimit": null}}"#;
    const BETWEEN: &str = r#"{"ensemble": {
        "probabilityType": 2,
        "scaleFactorOfLowerLimit": 0, "scaledValueOfLowerLimit": 1,
        "scaleFactorOfUpperLimit": 0, "scaledValueOfUpperLimit": 5}}"#;
    const BELOW_UPPER: &str = r#"{"ensemble": {
        "probabilityType": 4,
        "scaleFactorOfLowerLimit": null, "scaledValueOfLowerLimit": null,
        "scaleFactorOfUpperLimit": -1, "scaledValueOfUpperLimit": 4294967294}}"#;

    #[test]
    fn an_ensemble_block_is_optional_and_needs_version_3() {
        assert!(parse_ensemble(&variable("{}"), 1).is_ok());
        for json in [SPREAD, QUARTILE, ABOVE_10_MM, BETWEEN, BELOW_UPPER] {
            assert!(parse_ensemble(&variable(json), 3).is_ok(), "{json}");
            assert!(parse_ensemble(&variable(json), 4).is_ok(), "{json}");
            assert!(parse_ensemble(&variable(json), 2).is_err(), "{json}");
        }
        assert!(parse_ensemble(&variable(r#"{"ensemble": {"derivedForecast": 255}}"#), 3).is_ok());
        assert!(parse_ensemble(&variable(r#"{"ensemble": {"percentileValue": 0}}"#), 3).is_ok());
    }

    #[test]
    fn malformed_ensemble_blocks_rejected() {
        let above = |from: &str, to: &str| ABOVE_10_MM.replacen(from, to, 1);
        for json in [
            // Null, not an object, no shape, two shapes at once, a key the
            // shape does not define, values out of range or of the wrong
            // type, a missing limit field, half a limit null, a limit the
            // type uses null, and one it does not use present.
            r#"{"ensemble": null}"#.to_string(),
            r#"{"ensemble": 4}"#.to_string(),
            r#"{"ensemble": {}}"#.to_string(),
            r#"{"ensemble": {"derivedForecast": 4, "percentileValue": 50}}"#.to_string(),
            r#"{"ensemble": {"percentileValue": 50, "members": 31}}"#.to_string(),
            r#"{"ensemble": {"derivedForecast": 256}}"#.to_string(),
            r#"{"ensemble": {"derivedForecast": -1}}"#.to_string(),
            r#"{"ensemble": {"percentileValue": 101}}"#.to_string(),
            r#"{"ensemble": {"percentileValue": "50"}}"#.to_string(),
            r#"{"ensemble": {"percentileValue": 50.5}}"#.to_string(),
            r#"{"ensemble": {"scaleFactorOfLowerLimit": 2, "scaledValueOfLowerLimit": 1000}}"#.to_string(),
            above(r#""probabilityType": 3"#, r#""probabilityType": 6"#),
            above(r#""probabilityType": 3,"#, r#""probabilityType": 3, "derivedForecast": 0,"#),
            above(r#""probabilityType": 3,"#, r#""probabilityType": 3, "members": 31,"#),
            above(r#", "scaledValueOfUpperLimit": null"#, ""),
            above(r#""scaledValueOfLowerLimit": 1000"#, r#""scaledValueOfLowerLimit": null"#),
            above(r#""scaleFactorOfLowerLimit": 2, "scaledValueOfLowerLimit": 1000"#,
                  r#""scaleFactorOfLowerLimit": null, "scaledValueOfLowerLimit": null"#),
            above(r#""scaleFactorOfUpperLimit": null, "scaledValueOfUpperLimit": null"#,
                  r#""scaleFactorOfUpperLimit": 2, "scaledValueOfUpperLimit": 1000"#),
            above(r#""scaleFactorOfLowerLimit": 2"#, r#""scaleFactorOfLowerLimit": 128"#),
            above(r#""scaleFactorOfLowerLimit": 2"#, r#""scaleFactorOfLowerLimit": -128"#),
            above(r#""scaledValueOfLowerLimit": 1000"#, r#""scaledValueOfLowerLimit": 4294967295"#),
            above(r#""scaledValueOfLowerLimit": 1000"#, r#""scaledValueOfLowerLimit": -1"#),
            BETWEEN.replacen(r#""scaleFactorOfUpperLimit": 0, "scaledValueOfUpperLimit": 5"#,
                             r#""scaleFactorOfUpperLimit": null, "scaledValueOfUpperLimit": null"#, 1),
            BELOW_UPPER.replacen(r#""scaleFactorOfLowerLimit": null, "scaledValueOfLowerLimit": null"#,
                                 r#""scaleFactorOfLowerLimit": 0, "scaledValueOfLowerLimit": 1"#, 1),
        ] {
            assert!(parse_ensemble(&variable(&json), 3).is_err(), "{json}");
        }
    }

    /// The block written is the block read, keys in the spec's order, and a
    /// probability drops the limit its type does not use.
    #[test]
    fn an_ensemble_identity_writes_its_block_in_the_spec_s_order() {
        let block = variable(ABOVE_10_MM)["ensemble"].as_object().unwrap().clone();
        let identity = EnsembleIdentity::from_block(&block).unwrap();
        assert_eq!(identity, EnsembleIdentity::probability(3, Some((2, 1000)), Some((2, 1000))));
        let expected = r#"{"probabilityType":3,"scaleFactorOfLowerLimit":2,"scaledValueOfLowerLimit":1000,"scaleFactorOfUpperLimit":null,"scaledValueOfUpperLimit":null}"#;
        // Key order is only kept where the writer runs: the encoder feature
        // turns on serde_json's preserve_order, the decoder build sorts keys.
        #[cfg(feature = "encoder")]
        assert_eq!(serde_json::to_string(&identity.metadata()).unwrap(), expected);
        assert_eq!(
            serde_json::Value::Object(identity.metadata()),
            serde_json::from_str::<serde_json::Value>(expected).unwrap()
        );
        assert_eq!(serde_json::to_string(&EnsembleIdentity::Derived(4).metadata()).unwrap(), r#"{"derivedForecast":4}"#);
        assert_eq!(EnsembleIdentity::Percentile(50).templates(), (6, 10));
    }
}

#[cfg(test)]
mod grid_tests {
    use super::{parse_gaussian, parse_metadata};
    use serde_json::{json, Value};

    fn regular_grid() -> Value {
        json!({
            "width": 4, "height": 3, "layout": "row-major", "rowOrder": "north-to-south",
            "columnOrder": "west-to-east", "firstLongitude": -180.0, "firstLatitude": 90.0,
            "longitudeStep": 0.25, "latitudeStep": -0.25, "wrapLongitude": false,
        })
    }

    fn projected_grid() -> Value {
        json!({
            "width": 4, "height": 3, "layout": "row-major", "rowOrder": "north-to-south",
            "columnOrder": "west-to-east", "wrapLongitude": false,
            "projection": {
                "kind": "lambert-conformal", "radius": 6371229.0,
                "standardParallel1": 38.5, "standardParallel2": 38.5,
                "latitudeOfOrigin": 38.5, "centralMeridian": -97.5,
                "x0": -2697520.142, "y0": 1586693.847, "dx": 3000.0, "dy": -3000.0,
            },
        })
    }

    fn document(version: u64, grid: Value) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "schemaVersion": version,
            "runTime": "2026-11-05T00:00:00Z",
            "time": {"unitSeconds": 3600, "firstFrameOffset": 0, "frameStep": 1, "frameCount": 2},
            "grid": grid,
            "variables": [{
                "numericId": 1, "id": "tmp2m", "parameter": {
                    "discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                    "typeOfFirstFixedSurface": 103, "scaleFactorOfFirstFixedSurface": 0,
                    "scaledValueOfFirstFixedSurface": 2,
                },
            }],
        }))
        .unwrap()
    }

    fn projection_with(field: &str, value: Value) -> Value {
        let mut grid = projected_grid();
        grid["projection"][field] = value;
        grid
    }

    #[test]
    fn a_projected_grid_is_version_4_and_only_version_4() {
        assert_eq!(parse_metadata(&document(4, projected_grid())).unwrap().schema_version, 4);
        assert!(parse_metadata(&document(3, projected_grid())).is_err());
        assert!(parse_metadata(&document(5, projected_grid())).is_err());
    }

    #[test]
    fn a_regular_grid_stays_on_version_3() {
        assert_eq!(parse_metadata(&document(3, regular_grid())).unwrap().schema_version, 3);
        assert!(parse_metadata(&document(4, regular_grid())).is_err());
    }

    #[test]
    fn version_4_with_neither_shape_is_rejected() {
        let mut neither = projected_grid();
        neither.as_object_mut().unwrap().remove("projection");
        assert!(parse_metadata(&document(4, neither)).is_err());
    }

    #[test]
    fn a_projected_grid_carries_no_regular_field() {
        for field in ["firstLongitude", "firstLatitude", "longitudeStep", "latitudeStep"] {
            let mut grid = projected_grid();
            grid[field] = json!(1.0);
            assert!(parse_metadata(&document(4, grid)).is_err(), "{field}");
        }
    }

    #[test]
    fn a_projected_grid_carries_no_gaussian_block_and_does_not_wrap() {
        let mut gaussian = projected_grid();
        gaussian["gaussian"] = json!({"n": 95, "firstRow": 0, "rowStride": 1});
        assert!(parse_metadata(&document(4, gaussian)).is_err());
        let mut wraps = projected_grid();
        wraps["wrapLongitude"] = json!(true);
        assert!(parse_metadata(&document(4, wraps)).is_err());
        let mut silent = projected_grid();
        silent.as_object_mut().unwrap().remove("wrapLongitude");
        assert!(parse_metadata(&document(4, silent)).is_err());
    }

    #[test]
    fn malformed_projections_are_rejected() {
        for (field, value) in [
            ("kind", json!("octahedral-gaussian")),
            ("kind", json!(null)),
            ("radius", json!(0.0)),
            ("radius", json!("6371229")),
            ("standardParallel1", json!(90.0)),
            ("standardParallel2", json!(-38.5)),
            ("latitudeOfOrigin", json!(-90.0)),
            ("centralMeridian", json!(262.5)),
            ("x0", json!(null)),
            ("y0", json!(true)),
            ("dx", json!(-3000.0)),
            ("dx", json!(0.0)),
            ("dy", json!(3000.0)),
            ("dy", json!(0.0)),
            ("zone", json!(12)),
        ] {
            let grid = projection_with(field, value.clone());
            assert!(parse_metadata(&document(4, grid)).is_err(), "{field} = {value}");
        }
        for field in ["kind", "radius", "standardParallel1", "standardParallel2", "latitudeOfOrigin", "centralMeridian", "x0", "y0", "dx", "dy"] {
            let mut grid = projected_grid();
            grid["projection"].as_object_mut().unwrap().remove(field);
            assert!(parse_metadata(&document(4, grid)).is_err(), "without {field}");
        }
        let mut null = projected_grid();
        null["projection"] = json!(null);
        assert!(parse_metadata(&document(4, null)).is_err());
    }

    #[test]
    fn a_gaussian_block_stays_on_version_3_and_is_validated() {
        let mut grid = regular_grid();
        grid["gaussian"] = json!({"n": 95, "firstRow": 2, "rowStride": 2});
        assert_eq!(parse_metadata(&document(3, grid.clone())).unwrap().schema_version, 3);
        assert!(parse_metadata(&document(4, grid.clone())).is_err());
        for block in [
            json!(null),
            json!({"n": 95, "firstRow": 0}),
            json!({"n": 95, "firstRow": 0, "rowStride": 1, "rows": 190}),
            json!({"n": 0, "firstRow": 0, "rowStride": 1}),
            json!({"n": 32769, "firstRow": 0, "rowStride": 1}),
            json!({"n": 95.5, "firstRow": 0, "rowStride": 1}),
            json!({"n": 95, "firstRow": -1, "rowStride": 1}),
            json!({"n": 95, "firstRow": 0, "rowStride": 0}),
            // The grid's three rows run off the 190 of the Gaussian grid.
            json!({"n": 95, "firstRow": 188, "rowStride": 1}),
            json!({"n": 95, "firstRow": 0, "rowStride": 95}),
            json!({"n": 95, "firstRow": 0, "rowStride": u64::MAX}),
        ] {
            let mut grid = regular_grid();
            grid["gaussian"] = block.clone();
            assert!(parse_metadata(&document(3, grid)).is_err(), "{block}");
        }
    }

    #[test]
    fn the_last_gaussian_row_is_in_range() {
        let block = json!({"n": 95, "firstRow": 187, "rowStride": 1});
        assert!(parse_gaussian(&block, 3).is_ok());
        assert!(parse_gaussian(&block, 4).is_err());
    }

    #[test]
    fn a_gaussian_block_needs_version_3() {
        let mut grid = regular_grid();
        grid["gaussian"] = json!({"n": 95, "firstRow": 0, "rowStride": 1});
        let text = serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 2},
            "grid": grid,
            "variables": [{"numericId": 1, "id": "tmp2m"}],
        }))
        .unwrap();
        assert!(parse_metadata(&text).is_err());
    }

    #[test]
    fn older_versions_keep_reading_without_the_regular_fields() {
        let text = serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 2},
            "grid": {"width": 4, "height": 3},
            "variables": [{"numericId": 1, "id": "tmp2m"}],
        }))
        .unwrap();
        assert_eq!(parse_metadata(&text).unwrap().schema_version, 1);
        let mut bare = regular_grid();
        for field in ["firstLongitude", "firstLatitude", "longitudeStep", "latitudeStep"] {
            bare.as_object_mut().unwrap().remove(field);
        }
        assert_eq!(parse_metadata(&document(3, bare)).unwrap().schema_version, 3);
    }
}

#[cfg(test)]
mod schema_tie_tests {
    //! The field names read here are the ones
    //! `docs/schemas/bundle-metadata.schema.json` names, and the blocks the
    //! format closes name exactly the fields this reader knows. Versions 1
    //! and 2 have no schema; their hour-named time fields are this reader's.
    use super::{
        AEROSOL_INTERVALS, AEROSOL_TYPE_FIELDS, BAND_FIELDS, GAUSSIAN_FIELDS, PARAMETER_CODE_FIELDS,
        PARAMETER_NULLABLE_FIELDS, PRODUCER_FIELDS, PROJECTION_NUMBER_FIELDS, REGULAR_GRID_FIELDS,
        V3_TIME_FIELDS,
    };
    use crate::schematie::{assert_exactly, assert_named};

    const SCHEMA: &str = "bundle-metadata";

    #[test]
    fn the_fields_read_are_the_schemas() {
        assert_named(SCHEMA, "", &["schemaVersion", "grid", "time", "variables"]);
        assert_named(SCHEMA, "/$defs/Grid", &["width", "height", "projection", "gaussian", "wrapLongitude"]);
        assert_named(SCHEMA, "/$defs/Grid", &REGULAR_GRID_FIELDS);
        assert_named("manifest", "/$defs/Grid", &["width", "height", "projection", "gaussian", "wrapLongitude"]);
        assert_named("manifest", "/$defs/Grid", &REGULAR_GRID_FIELDS);
        assert_named(
            SCHEMA,
            "/$defs/Variable",
            &["numericId", "id", "parameter", "band", "producer", "aerosol", "ensemble"],
        );
    }

    #[test]
    fn the_closed_blocks_are_the_schemas() {
        let time: Vec<&str> = V3_TIME_FIELDS.iter().copied().chain(["frameCount"]).collect();
        assert_exactly(SCHEMA, "/$defs/Time", &time);
        let parameter: Vec<&str> =
            PARAMETER_CODE_FIELDS.iter().chain(PARAMETER_NULLABLE_FIELDS.iter()).copied().collect();
        assert_exactly(SCHEMA, "/$defs/Parameter", &parameter);
        assert_exactly(SCHEMA, "/$defs/Band", &BAND_FIELDS.map(|(field, _, _)| field));
        let projection: Vec<&str> = PROJECTION_NUMBER_FIELDS.iter().copied().chain(["kind"]).collect();
        assert_exactly(SCHEMA, "/$defs/Projection", &projection);
        assert_exactly(SCHEMA, "/$defs/Gaussian", &GAUSSIAN_FIELDS);
        assert_exactly(SCHEMA, "/$defs/Producer", &PRODUCER_FIELDS);
        let mut aerosol: Vec<&str> = AEROSOL_TYPE_FIELDS.iter().map(|(field, _)| *field).collect();
        for (_, pairs) in AEROSOL_INTERVALS {
            for (scale, value) in pairs {
                aerosol.extend([scale, value]);
            }
        }
        assert_exactly(SCHEMA, "/$defs/Aerosol", &aerosol);
    }
}
