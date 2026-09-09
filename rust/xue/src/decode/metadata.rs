//! The bundle's metadata JSON: the grid, the time axis and the variable set.
//!
//! Validation here is what turns a JSON blob into the dimensions the rest of
//! the decoder trusts — the plane length every entry is checked against, and
//! the materialized frame-offset axis every plane is addressed by. Both the
//! axis and the variable set have exactly one valid encoding at exactly one
//! schema version, so a file cannot describe the same series two ways.

use crate::format::{err, gcd, DecodeError, HOUR_SECONDS, MAX_PLANE_LENGTH};

/// The schemaVersion 3 time block. Versions 1 and 2 use firstForecastHour
/// with stepHours or hours instead; mixing the two shapes is invalid.
const V3_TIME_FIELDS: [&str; 4] =
    ["unitSeconds", "firstFrameOffset", "frameStep", "frameOffsets"];


pub(crate) struct Metadata {
    pub(crate) json: String,
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
/// the metadata (docs/format.md).
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

pub(crate) fn parse_metadata(raw: &[u8]) -> Result<Metadata, DecodeError> {
    let text = std::str::from_utf8(raw).map_err(|_| err("metadata is not UTF-8"))?;
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|_| err("metadata is not valid JSON"))?;
    let object = value.as_object().ok_or_else(|| err("metadata must be a JSON object"))?;
    let schema_version = match object.get("schemaVersion").and_then(|v| v.as_u64()) {
        Some(version @ (1 | 2 | 3)) => version,
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
        variable_ids.push(numeric as u8);
    }
    if variable_ids.is_empty() {
        return Err(err("metadata must declare at least one variable"));
    }
    // Every axis and every variable set has exactly one valid encoding: the
    // declared version must be the lowest able to express both.
    let required_version = axis_version.max(if parameters > 0 { 3 } else { 1 });
    if schema_version != required_version {
        return Err(err("metadata declares a schemaVersion other than the lowest it needs"));
    }
    Ok(Metadata {
        json: text.to_owned(),
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
