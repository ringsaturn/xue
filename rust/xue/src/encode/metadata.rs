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
    lead_seconds.iter().fold(HOUR_SECONDS, |unit, lead| gcd(unit, *lead))
}

fn gcd(left: i64, right: i64) -> i64 {
    let (mut left, mut right) = (left.abs(), right.abs());
    while right != 0 {
        (left, right) = (right, left % right);
    }
    left
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
fn variable_metadata(variable_id: &str, source: &SourceSpec, profile: &str) -> Result<Value> {
    let spec = variable_spec(variable_id)?;
    let mut parameter = spec.parameter_metadata();
    if variable_id == "prate" && (source.accumulated_precipitation || source.averaged_precipitation)
    {
        // The published rate is the mean over the step, derived from the
        // source's run-total accumulation (ECMWF) or window average (sflux) —
        // a statistic over the interval, not the instantaneous field GFS
        // pgrb2 carries under the same parameter.
        parameter.insert("typeOfStatisticalProcessing".into(), json!(0));
    }
    let mut block = Map::new();
    block.insert("numericId".into(), json!(spec.numeric_id));
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
    block.insert(
        "variables".into(),
        Value::Array(
            variable_ids
                .iter()
                .map(|variable_id| variable_metadata(variable_id, source, profile))
                .collect::<Result<Vec<_>>>()?,
        ),
    );
    Ok(Value::Object(block))
}

/// Serialize a value the way Python's `json.dumps` does *by default*: one line,
/// but with a space after every separator.
///
/// The bundles embed their metadata with compact separators, and the encoder
/// matches that with plain `serde_json::to_string`. The `metadataJson` strings
/// the manifest carries for posters and videos are the one place the reference
/// leaves `json.dumps` at its defaults, so they need the spaced form.
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
}
