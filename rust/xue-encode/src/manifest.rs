//! The public manifest and the live pointer — the port of `xue/manifest.py`'s
//! builders.
//!
//! Two layers, both versioned: the only mutable object per model is a tiny
//! live pointer at the data root (schema v1), and everything it names — the
//! run's `manifest.json` (schema v5) and every artifact — is immutable.
//!
//! Validation stays with the Python implementation, which is the contract's
//! reference; what this module guarantees is that the bytes it writes are the
//! ones the Python builders would have written.

use std::path::Path;

use serde_json::{json, Map, Value};
use time::OffsetDateTime;

use crate::binformat::write_atomic;
use crate::errors::{EncodeError, Result};
use crate::metadata::iso_z;

/// Schema v5 bundle registry, in manifest order.
pub const BIN_BUNDLE_VARIABLES: &[&str] = &["tmp2m", "prate", "dswrf", "cref", "wind10m"];
pub const REQUIRED_BIN_BUNDLE_VARIABLES: &[&str] = &["tmp2m", "prate"];

/// Build a schema v5 manifest describing one `.xue` bundle per variable.
///
/// Each entry in `bundles` carries `variable`, `path`, `byteLength` and
/// `crc32`, and may carry `variants`, `video` and `poster` descriptors.
pub fn build_bin_manifest(
    run_time: OffsetDateTime,
    bundles: Vec<Value>,
    expected_hours: i64,
    model: &str,
    product: &str,
    require_core_variables: bool,
) -> Result<Value> {
    let mut payload = Map::new();
    payload.insert("schemaVersion".into(), json!(5));
    payload.insert("model".into(), json!(model));
    payload.insert("product".into(), json!(product));
    payload.insert("runTime".into(), json!(iso_z(run_time)));
    payload.insert("forecastHours".into(), json!(expected_hours));
    payload.insert("bundles".into(), Value::Array(bundles));
    let payload = Value::Object(payload);
    validate_bin_manifest(&payload, require_core_variables)?;
    Ok(payload)
}

/// The structural rules a reader depends on. Deliberately a subset of the
/// Python validator — the bundle paths, the variable registry, and the core
/// pair — since the Python side stays the contract's reference.
fn validate_bin_manifest(payload: &Value, require_core_variables: bool) -> Result<()> {
    let bundles = payload["bundles"]
        .as_array()
        .ok_or_else(|| EncodeError::manifest("manifest bundles must be a list"))?;
    let mut seen = Vec::new();
    for bundle in bundles {
        let variable = bundle["variable"].as_str().unwrap_or_default();
        if !BIN_BUNDLE_VARIABLES.contains(&variable) {
            return Err(EncodeError::manifest(format!(
                "manifest bundle variable {variable} is not registered"
            )));
        }
        let path = bundle["path"].as_str().unwrap_or_default();
        if !path.ends_with(".xue")
            || path.starts_with('/')
            || path.starts_with("http:")
            || path.starts_with("https:")
            || path.split('/').any(|part| part == "..")
        {
            return Err(EncodeError::manifest(format!(
                "manifest bundle path must be a relative .xue path for {variable}"
            )));
        }
        if seen.contains(&variable) {
            return Err(EncodeError::manifest(
                "manifest contains duplicate bundle variables",
            ));
        }
        seen.push(variable);
    }
    if require_core_variables {
        for variable in REQUIRED_BIN_BUNDLE_VARIABLES {
            if !seen.contains(variable) {
                return Err(EncodeError::manifest(format!(
                    "manifest is missing the required {variable} bundle"
                )));
            }
        }
    }
    Ok(())
}

/// Build the tiny mutable `latest.json` live pointer.
///
/// The pointer is the only mutable object in the dataset: it names the current
/// run and where that run's immutable manifest lives (relative to the pointer
/// itself), plus the manifest's CRC32 so clients can fetch the manifest
/// through immutable `?v=` caching.
pub fn build_latest_pointer(
    run_id: &str,
    run_time: OffsetDateTime,
    manifest_path: &str,
    manifest_crc32: &str,
    model: &str,
    product: &str,
) -> Value {
    let mut payload = Map::new();
    payload.insert("schemaVersion".into(), json!(1));
    payload.insert("model".into(), json!(model));
    payload.insert("product".into(), json!(product));
    payload.insert("run".into(), json!(run_id));
    payload.insert("runTime".into(), json!(iso_z(run_time)));
    payload.insert("manifestPath".into(), json!(manifest_path));
    payload.insert("manifestCrc32".into(), json!(manifest_crc32));
    Value::Object(payload)
}

/// Serialize a manifest or pointer exactly as the Python writer does:
/// `json.dump(..., ensure_ascii=False, indent=2)` plus a trailing newline.
pub fn serialize_json(payload: &Value) -> String {
    let mut text = serde_json::to_string_pretty(payload).expect("serializable payload");
    text.push('\n');
    text
}

/// Atomically write a manifest. An immutable object: an existing file that
/// differs is an error unless `force` is set, which is the Python writer's
/// guard against silently republishing a run under a changed description.
pub fn write_json(path: &Path, payload: &Value, force: bool) -> Result<()> {
    let text = serialize_json(payload);
    if path.exists() && !force {
        let existing = std::fs::read_to_string(path).map_err(|error| {
            EncodeError::manifest(format!(
                "existing manifest is invalid, pass --force to replace it: {} ({error})",
                path.display()
            ))
        })?;
        let parsed: std::result::Result<Value, _> = serde_json::from_str(&existing);
        return match parsed {
            Ok(existing) if existing == *payload => Ok(()),
            Ok(_) => Err(EncodeError::manifest(format!(
                "existing manifest describes different data, pass --force to replace it: {}",
                path.display()
            ))),
            Err(error) => Err(EncodeError::manifest(format!(
                "existing manifest is invalid, pass --force to replace it: {} ({error})",
                path.display()
            ))),
        };
    }
    write_atomic(path, text.as_bytes())
}
