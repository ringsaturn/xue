//! Bundle metadata JSON — schema version 3.
//!
//! Key order and number formatting matter: the Python encoder writes this with
//! `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`, and the two
//! encoders are meant to produce the same bytes, so the maps here are built in
//! insertion order (`serde_json`'s `preserve_order` feature) and integers stay
//! integers.

use serde_json::{json, Map, Value};
use time::OffsetDateTime;

use crate::encode::binformat::HOUR_SECONDS;
use crate::encode::errors::Result;
use crate::encode::grid::GridInfo;
use crate::encode::quantize::codebook;
use crate::encode::sources::SourceSpec;
use crate::encode::variables::variable_spec;
use crate::format::gcd;

/// Bundle metadata schema this encoder writes: every variable descriptor
/// carries its GRIB2 parameter identity. Earlier versions remain readable;
/// nothing new is written at them.
pub const METADATA_SCHEMA_VERSION: u32 = 3;

/// `2026-08-14T06:00:00Z`, the shape every timestamp in the published
/// contract takes.
pub fn iso_z(value: OffsetDateTime) -> String {
    let value = value.to_offset(time::UtcOffset::UTC);
    let format = time::macros::format_description!(
        "[year]-[month]-[day]T[hour]:[minute]:[second]Z"
    );
    value.format(&format).expect("UTC timestamp formats")
}

/// The coarsest unit that expresses every frame's lead time exactly.
///
/// An hour for every forecast source, and whatever the file carries for an
/// observation series — six minutes for the radar mosaic. Capping at an hour
/// keeps a whole-hour axis indexed by its forecast hours.
pub fn axis_unit_seconds(lead_seconds: &[i64]) -> i64 {
    lead_seconds.iter().fold(HOUR_SECONDS, |unit, lead| {
        gcd(unit as u64, lead.unsigned_abs()) as i64
    })
}

/// `offset` as a whole number of hours, rounded up — how far a run reaches,
/// for the manifest's coarse `forecastHours`.
pub fn lead_hours(offset: i64, unit_seconds: i64) -> i64 {
    let seconds = offset * unit_seconds;
    (seconds + HOUR_SECONDS - 1) / HOUR_SECONDS
}

/// The metadata `time` block: offsets on a declared unit, uniform ones
/// declaring a `frameStep` and the rest listing their offsets outright.
fn time_metadata(offsets: &[i64], unit_seconds: i64) -> Map<String, Value> {
    let mut steps: Vec<i64> = offsets.windows(2).map(|pair| pair[1] - pair[0]).collect();
    steps.sort_unstable();
    steps.dedup();
    let mut block = Map::new();
    block.insert("unitSeconds".into(), json!(unit_seconds));
    block.insert("firstFrameOffset".into(), json!(offsets[0]));
    block.insert("frameCount".into(), json!(offsets.len()));
    if steps.len() <= 1 {
        block.insert("frameStep".into(), json!(steps.first().copied().unwrap_or(1)));
    } else {
        block.insert("frameOffsets".into(), json!(offsets));
    }
    block
}

/// One schema v3 variable descriptor: what the field is (GRIB2 parameter and
/// fixed surface), what its values mean, and how they are quantized.
///
/// `numeric_id` is the file-local `variableId` handle that ties this
/// descriptor to the index (docs/format.md): the variable's 1-based position
/// in the bundle's variable list, assigned by [`build_metadata`].
fn variable_metadata(
    variable_id: &str,
    numeric_id: u8,
    source: &SourceSpec,
    profile: &str,
) -> Result<Value> {
    let spec = variable_spec(variable_id)?;
    let mut parameter = spec.parameter_metadata();
    if let Some((_, process)) = source
        .statistical_processes
        .iter()
        .find(|(statistical_id, _)| *statistical_id == variable_id)
    {
        // A statistic over the step, not the instantaneous field GFS pgrb2
        // carries under the same parameter: the rate derived from a
        // run-total accumulation (ECMWF) or a window average (sflux) is a
        // mean, ECMWF's gust a maximum.
        parameter.insert("typeOfStatisticalProcessing".into(), json!(process));
    }
    let mut block = Map::new();
    block.insert("numericId".into(), json!(numeric_id));
    block.insert("id".into(), json!(variable_id));
    block.insert("label".into(), json!(spec.label));
    block.insert("unit".into(), json!(spec.output_unit));
    block.insert("parameter".into(), Value::Object(parameter));
    block.insert(
        "quantization".into(),
        Value::Object(codebook(profile, variable_id)?.metadata()),
    );
    Ok(Value::Object(block))
}

pub fn build_metadata(
    run_time: OffsetDateTime,
    offsets: &[i64],
    grid: &GridInfo,
    profile: &str,
    variable_ids: &[&str],
    source: &SourceSpec,
    unit_seconds: i64,
) -> Result<Value> {
    let mut block = Map::new();
    block.insert("schemaVersion".into(), json!(METADATA_SCHEMA_VERSION));
    block.insert("model".into(), json!(source.manifest_model));
    block.insert("product".into(), json!(source.product));
    block.insert("runTime".into(), json!(iso_z(run_time)));
    block.insert("profile".into(), json!(profile));
    block.insert("time".into(), Value::Object(time_metadata(offsets, unit_seconds)));
    block.insert("grid".into(), Value::Object(grid.metadata()));
    // variableId is a file-local handle: 1..n by position in the bundle's
    // variable list, which is the same list that fixes chunk order in
    // `bundle_chunks`. Nothing outside one file reads these numbers — a
    // variable's identity is its GRIB2 parameter block.
    block.insert(
        "variables".into(),
        Value::Array(
            variable_ids
                .iter()
                .enumerate()
                .map(|(index, variable_id)| {
                    variable_metadata(variable_id, index as u8 + 1, source, profile)
                })
                .collect::<Result<Vec<_>>>()?,
        ),
    );
    Ok(Value::Object(block))
}

/// Serialize a value the way Python's `json.dumps` does *by default*: one line,
/// a space after every separator, and every non-ASCII character escaped.
///
/// The bundles embed their metadata with compact separators and raw UTF-8, and
/// the encoder matches that with plain `serde_json::to_string`. The
/// `metadataJson` strings the manifest carries for posters and videos are the
/// one place the reference leaves `json.dumps` at its defaults, so they need
/// both the spaced form and `ensure_ascii`.
pub fn to_spaced_json(value: &Value) -> String {
    let mut output = Vec::new();
    let mut serializer =
        serde_json::Serializer::with_formatter(&mut output, SpacedFormatter);
    serde::Serialize::serialize(value, &mut serializer).expect("serializable value");
    String::from_utf8(output).expect("UTF-8 JSON")
}

struct SpacedFormatter;

impl serde_json::ser::Formatter for SpacedFormatter {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        writer.write_all(if first { b"" } else { b", " })
    }

    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        writer.write_all(if first { b"" } else { b", " })
    }

    fn begin_object_value<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
    ) -> std::io::Result<()> {
        writer.write_all(b": ")
    }

    /// `ensure_ascii`: every character outside ASCII becomes a `\uXXXX`
    /// escape, in lowercase hex, an astral one as its UTF-16 surrogate pair.
    /// The degree sign in the temperature unit is the one that shows up in
    /// practice, and the manifest is compared byte for byte.
    fn write_string_fragment<W: ?Sized + std::io::Write>(
        &mut self,
        writer: &mut W,
        fragment: &str,
    ) -> std::io::Result<()> {
        if fragment.is_ascii() {
            return writer.write_all(fragment.as_bytes());
        }
        let mut start = 0;
        for (index, character) in fragment.char_indices() {
            if character.is_ascii() {
                continue;
            }
            writer.write_all(&fragment.as_bytes()[start..index])?;
            start = index + character.len_utf8();
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                write!(writer, "\\u{unit:04x}")?;
            }
        }
        writer.write_all(&fragment.as_bytes()[start..])
    }
}

#[cfg(test)]
mod tests {
    use super::{axis_unit_seconds, build_metadata, lead_hours, to_spaced_json};
    use crate::encode::grid::GridInfo;
    use crate::encode::sources::source_spec;

    #[test]
    fn the_axis_unit_is_the_coarsest_that_fits() {
        assert_eq!(axis_unit_seconds(&[0, 3600, 7200]), 3600);
        // The radar mosaic publishes every six minutes.
        assert_eq!(axis_unit_seconds(&[0, 360, 720, 1440]), 360);
        assert_eq!(lead_hours(2, 3600), 2);
        // A partial hour rounds up: the manifest's forecastHours is coarse.
        assert_eq!(lead_hours(11, 360), 2);
    }

    #[test]
    fn metadata_is_schema_version_three_and_compact() {
        let source = source_spec("gfs").expect("gfs");
        let grid = GridInfo::new(16, 8, -180.0, 90.0, 22.5, -22.5);
        let run_time = time::macros::datetime!(2026-08-14 06:00:00 UTC);
        let uniform = build_metadata(run_time, &[0, 1, 2], &grid, "quality", &["tmp2m"], source, 3600)
            .expect("metadata");
        let text = serde_json::to_string(&uniform).expect("json");
        assert!(text.starts_with(r#"{"schemaVersion":3,"model":"GFS","product":"pgrb2.0p25""#));
        assert!(text.contains(r#""time":{"unitSeconds":3600,"firstFrameOffset":0,"frameCount":3,"frameStep":1}"#));
        assert!(text.contains(r#""parameter":{"discipline":0,"parameterCategory":0,"parameterNumber":0,"typeOfFirstFixedSurface":103,"scaleFactorOfFirstFixedSurface":0,"scaledValueOfFirstFixedSurface":2}"#));

        // A mixed-cadence axis lists its offsets rather than declaring a step.
        let mixed = build_metadata(run_time, &[0, 1, 2, 5], &grid, "quality", &["tmp2m"], source, 3600)
            .expect("metadata");
        let text = serde_json::to_string(&mixed).expect("json");
        assert!(text.contains(r#""frameOffsets":[0,1,2,5]"#));
        assert!(!text.contains("frameStep"));
    }

    #[test]
    fn a_derived_rate_declares_its_statistical_process() {
        let grid = GridInfo::new(16, 8, -180.0, 90.0, 22.5, -22.5);
        let run_time = time::macros::datetime!(2026-08-14 06:00:00 UTC);
        for (model, expected) in [("gfs", false), ("ecmwf", true), ("sflux", true)] {
            let source = source_spec(model).expect("source");
            let metadata =
                build_metadata(run_time, &[3], &grid, "quality", &["prate"], source, 3600)
                    .expect("metadata");
            let text = serde_json::to_string(&metadata).expect("json");
            assert_eq!(
                text.contains(r#""typeOfStatisticalProcessing":0"#),
                expected,
                "{model}"
            );
        }
    }

    #[test]
    fn spaced_json_matches_python_json_dumps_defaults() {
        // The manifest's poster and video descriptors carry a variable's metadata
        // as a string written by `json.dumps` with nothing overridden: a space
        // after every separator, and every non-ASCII character escaped. The degree
        // sign in the temperature unit is the one that occurs in practice.
        let value = serde_json::json!({
            "unit": "°C",
            "labels": ["雪", "\u{1f300}"],
            "quoted": "a \"b\"\n",
            "plain": 1,
        });
        assert_eq!(
            to_spaced_json(&value),
            "{\"unit\": \"\\u00b0C\", \"labels\": [\"\\u96ea\", \"\\ud83c\\udf00\"], \
             \"quoted\": \"a \\\"b\\\"\\n\", \"plain\": 1}"
        );
    }
}
