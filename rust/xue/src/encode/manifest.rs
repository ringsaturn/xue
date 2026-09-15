//! The public manifest and the live pointer — the port of `xuebuild/manifest.py`'s
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

use crate::encode::binformat::write_atomic;
use crate::encode::errors::{EncodeError, Result};
use crate::encode::metadata::iso_z;
use crate::encode::sources::SOURCES;

/// A bundle's `variable` is a name, not a registered number: it must look like
/// one (`^[a-z][a-z0-9]*$`) and be unique within the manifest, and nothing
/// more. A name a particular reader does not draw is a layer that reader
/// skips, not a malformed manifest, which is what lets a new bundle ship
/// without every implementation being widened first. Order is whatever the
/// encoder's scalar-then-vector bundle list produces. Mirrors
/// `xuebuild/manifest.py`.
///
/// The core set is the exception, and the only closed set left, and it is
/// the dataset's own (`SourceSpec::core_bundle_ids`, looked up by the
/// manifest's `model` string): a forecast manifest naming neither
/// temperature nor precipitation, or a radar manifest without the
/// reflectivity, describes a run the viewer cannot open.
fn core_bundle_ids(model: &str) -> Result<&'static [&'static str]> {
    SOURCES
        .iter()
        .find(|source| source.manifest_model == model)
        .map(|source| source.core_bundle_ids)
        .ok_or_else(|| EncodeError::manifest(format!("manifest model is not a registered dataset: {model}")))
}

/// `^[a-z][a-z0-9]*$`, spelled out rather than pulling in a regex crate.
fn is_bundle_variable_name(variable: &str) -> bool {
    let mut bytes = variable.bytes();
    match bytes.next() {
        Some(first) if first.is_ascii_lowercase() => {}
        _ => return false,
    }
    bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
}

/// A relative artifact path under the manifest's rules: the given suffix,
/// no absolute or URL form, no `..` segment.
fn is_relative_artifact_path(path: &str, suffix: &str) -> bool {
    path.ends_with(suffix)
        && !path.starts_with('/')
        && !path.starts_with("http:")
        && !path.starts_with("https:")
        && !path.split('/').any(|part| part == "..")
}

/// The optional `zarr` descriptor a bundle or a variant may carry: the
/// bundle's Zarr store (`docs/zarr-profile.md`) as a relative `.zarr` root,
/// the sum of its objects and the CRC-32 of its root `zarr.json`. The native
/// encoder never writes one — the store is derived from the finished bundle
/// on the Python side — but the validator mirrors `xuebuild/manifest.py`
/// so a manifest that carries one is judged the same way everywhere.
fn validate_zarr_descriptor(store: &Value, variable: &str) -> Result<()> {
    if store.is_null() {
        return Ok(());
    }
    let path = store["path"].as_str().unwrap_or_default();
    if !store.is_object() || !is_relative_artifact_path(path, ".zarr") {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr path must be a relative .zarr path for {variable}"
        )));
    }
    if !store["byteLength"].as_i64().is_some_and(|length| length > 0) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr byteLength must be a positive integer for {variable}"
        )));
    }
    let crc32 = store["crc32"].as_str().unwrap_or_default();
    if crc32.len() != 8 || !crc32.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr crc32 must be 8 lowercase hex characters for {variable}"
        )));
    }
    Ok(())
}

/// Build a schema v5 manifest describing one `.xue` bundle per variable.
///
/// Each entry in `bundles` carries `variable`, `path`, `byteLength` and
/// `crc32`, and may carry `variants`, `video`, `poster` and `zarr`
/// descriptors (a variant may carry a `zarr` descriptor of its own).
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
/// Python validator — the bundle paths, the shape of a bundle name, and the
/// core set — since the Python side stays the contract's reference.
fn validate_bin_manifest(payload: &Value, require_core_variables: bool) -> Result<()> {
    let bundles = payload["bundles"]
        .as_array()
        .ok_or_else(|| EncodeError::manifest("manifest bundles must be a list"))?;
    let mut seen = Vec::new();
    for bundle in bundles {
        let variable = bundle["variable"].as_str().unwrap_or_default();
        if !is_bundle_variable_name(variable) {
            return Err(EncodeError::manifest(format!(
                "manifest bundle variable is not a bundle name: {variable:?}"
            )));
        }
        let path = bundle["path"].as_str().unwrap_or_default();
        if !is_relative_artifact_path(path, ".xue") {
            return Err(EncodeError::manifest(format!(
                "manifest bundle path must be a relative .xue path for {variable}"
            )));
        }
        validate_zarr_descriptor(&bundle["zarr"], variable)?;
        if let Some(variants) = bundle["variants"].as_array() {
            for variant in variants {
                validate_zarr_descriptor(&variant["zarr"], variable)?;
            }
        }
        if seen.contains(&variable) {
            return Err(EncodeError::manifest(
                "manifest contains duplicate bundle variables",
            ));
        }
        seen.push(variable);
    }
    if require_core_variables {
        for variable in core_bundle_ids(payload["model"].as_str().unwrap_or_default())? {
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

#[cfg(test)]
mod tests {
    use super::*;
    use time::macros::datetime;

    fn bundle(zarr: Option<Value>) -> Value {
        let mut entry = json!({
            "variable": "tmp2m",
            "path": "tmp2m.xue",
            "byteLength": 10,
            "crc32": "0123abcd",
            "variants": [{
                "path": "tmp2m.half.xue",
                "width": 720,
                "height": 361,
                "byteLength": 5,
                "crc32": "89abcdef",
                "bandwidth": 1,
            }],
        });
        if let Some(store) = zarr {
            entry["zarr"] = store.clone();
            entry["variants"][0]["zarr"] = store;
        }
        entry
    }

    fn manifest(entries: Vec<Value>) -> Result<Value> {
        let mut bundles = entries;
        bundles.push(json!({"variable": "prate", "path": "prate.xue", "byteLength": 10, "crc32": "0123abcd"}));
        build_bin_manifest(datetime!(2026-08-15 06:00 UTC), bundles, 120, "GFS", "pgrb2.0p25", true)
    }

    #[test]
    fn a_manifest_without_a_store_is_unchanged() {
        assert!(manifest(vec![bundle(None)]).is_ok());
    }

    #[test]
    fn a_zarr_descriptor_is_accepted_on_a_bundle_and_on_a_variant() {
        let store = json!({"path": "tmp2m.zarr", "byteLength": 12, "crc32": "deadbeef"});
        let payload = manifest(vec![bundle(Some(store))]).expect("valid manifest");
        assert_eq!(payload["bundles"][0]["zarr"]["path"], "tmp2m.zarr");
        assert_eq!(payload["bundles"][0]["variants"][0]["zarr"]["path"], "tmp2m.zarr");
    }

    #[test]
    fn a_malformed_zarr_descriptor_is_rejected() {
        for store in [
            json!({"path": "tmp2m.xue", "byteLength": 12, "crc32": "deadbeef"}),
            json!({"path": "/tmp2m.zarr", "byteLength": 12, "crc32": "deadbeef"}),
            json!({"path": "../tmp2m.zarr", "byteLength": 12, "crc32": "deadbeef"}),
            json!({"path": "tmp2m.zarr", "byteLength": 0, "crc32": "deadbeef"}),
            json!({"path": "tmp2m.zarr", "byteLength": 12, "crc32": "DEADBEEF"}),
            json!({"path": "tmp2m.zarr", "byteLength": 12, "crc32": "dead"}),
            json!("tmp2m.zarr"),
        ] {
            assert!(manifest(vec![bundle(Some(store.clone()))]).is_err(), "{store} was accepted");
        }
    }
}
