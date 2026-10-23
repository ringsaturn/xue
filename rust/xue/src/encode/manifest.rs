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
use crate::encode::registry::Registry;

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
fn core_bundle_ids<'a>(registry: &'a Registry, model: &str) -> Result<&'a [String]> {
    registry
        .source_by_manifest_model(model)
        .map(|source| source.core_bundle_ids.as_slice())
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

/// The `.xue` container fields of a bundle or a variant — `path`,
/// `byteLength` and `crc32`, one unit. Since the Zarr store the unit may be
/// absent as a whole: an entry names the container, the store, or both,
/// never neither. A `path` that is present is still held to `.xue`.
fn validate_container_fields(entry: &Value, variable: &str, label: &str, paths: &mut Vec<String>) -> Result<()> {
    if entry["path"].is_null() {
        if !entry["byteLength"].is_null() || !entry["crc32"].is_null() {
            return Err(EncodeError::manifest(format!(
                "manifest bundle{label} carries container fields without a path for {variable}"
            )));
        }
        if entry["zarr"].is_null() {
            return Err(EncodeError::manifest(format!(
                "manifest bundle{label} must carry a .xue path or a zarr store for {variable}"
            )));
        }
        return Ok(());
    }
    let path = entry["path"].as_str().unwrap_or_default();
    if !is_relative_artifact_path(path, ".xue") {
        return Err(EncodeError::manifest(format!(
            "manifest bundle{label} path must be a relative .xue path for {variable}"
        )));
    }
    claim_path(path, paths)?;
    if !entry["byteLength"].as_i64().is_some_and(|length| length > 0) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle{label} byteLength must be a positive integer for {variable}"
        )));
    }
    let crc32 = entry["crc32"].as_str().unwrap_or_default();
    if crc32.len() != 8 || !crc32.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle{label} crc32 must be 8 lowercase hex characters for {variable}"
        )));
    }
    Ok(())
}

/// The store stem every bundle of one resolution tier shares (a profile 2
/// store, `docs/zarr-profile.md`); no bundle id may take it.
const SHARED_STORE_STEM: &str = "bundles";

/// Whether a store path names a shared store: its final component is
/// `bundles.zarr` or `bundles.<tier>.zarr`, whether in the run's directory
/// or as a segment under `<model>.segments/<start>/<digest>/`. Several
/// entries, a bundle's and its variants' alike, name the same one.
fn is_shared_store_path(path: &str) -> bool {
    let name = path.rsplit('/').next().unwrap_or_default();
    let Some(stem) = name.strip_suffix(".zarr") else {
        return false;
    };
    match stem.split_once('.') {
        None => stem == SHARED_STORE_STEM,
        Some((head, tier)) => {
            head == SHARED_STORE_STEM
                && !tier.is_empty()
                && tier != "series"
                && tier.bytes().all(|byte| byte.is_ascii_lowercase())
        }
    }
}

/// Record a path an entry names, refusing one an earlier entry already named:
/// two bundles never share an object, except a shared store, which every
/// bundle of its tier names.
fn claim_path(path: &str, paths: &mut Vec<String>) -> Result<()> {
    if paths.iter().any(|held| held == path) {
        return Err(EncodeError::manifest("manifest contains duplicate bundle paths"));
    }
    paths.push(path.to_owned());
    Ok(())
}

/// A segment's store path: a relative `.zarr` path that may climb out of
/// the manifest's directory by at most two leading `..` components (the
/// model's shared segment directory sits beside the run directories) and
/// never after a named one.
fn is_segment_path(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    let climbs = parts.iter().take_while(|part| **part == "..").count();
    climbs <= 2 && climbs < parts.len() && is_relative_artifact_path(&parts[climbs..].join("/"), ".zarr")
}

/// One store's `path`, `byteLength` and `crc32`, the path judged by `path_ok`.
fn validate_store(store: &Value, variable: &str, path_ok: fn(&str) -> bool, paths: &mut Vec<String>) -> Result<u64> {
    let path = store["path"].as_str().unwrap_or_default();
    if !store.is_object() || !path_ok(path) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr path must be a relative .zarr path for {variable}"
        )));
    }
    if !is_shared_store_path(path) {
        claim_path(path, paths)?;
    }
    let Some(length) = store["byteLength"].as_u64().filter(|length| *length > 0) else {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr byteLength must be a positive integer for {variable}"
        )));
    };
    let crc32 = store["crc32"].as_str().unwrap_or_default();
    if crc32.len() != 8 || !crc32.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle zarr crc32 must be 8 lowercase hex characters for {variable}"
        )));
    }
    Ok(length)
}

/// The optional `zarr` descriptor a bundle or a variant may carry: the
/// bundle's Zarr store (`docs/zarr-profile.md`) as a relative `.zarr` root,
/// the sum of its objects and the CRC-32 of its root `zarr.json` — or, on a
/// rolling window, a segmented store: `segments`, the time-ordered stores
/// the axis is cut into, and `byteLength`, their sum, with no `path` or
/// `crc32` of its own. The native encoder never writes a segmented one — a
/// window is cut into segments on the Python side after the build — but
/// the validator mirrors `xuebuild/manifest.py` so a manifest that carries
/// one is judged the same way everywhere.
fn validate_zarr_descriptor(store: &Value, variable: &str, paths: &mut Vec<String>) -> Result<()> {
    if store.is_null() {
        return Ok(());
    }
    if store.get("segments").is_none() {
        return validate_store(store, variable, |path| is_relative_artifact_path(path, ".zarr"), paths).map(|_| ());
    }
    if !store["path"].is_null() || !store["crc32"].is_null() {
        return Err(EncodeError::manifest(format!(
            "manifest bundle segmented zarr store carries a path or a crc32 for {variable}"
        )));
    }
    let segments = store["segments"].as_array().filter(|segments| !segments.is_empty()).ok_or_else(|| {
        EncodeError::manifest(format!("manifest bundle zarr segments must be a non-empty list for {variable}"))
    })?;
    let mut total = 0u64;
    for segment in segments {
        total = total.saturating_add(validate_store(segment, variable, is_segment_path, paths)?);
    }
    if store["byteLength"].as_u64() != Some(total) {
        return Err(EncodeError::manifest(format!(
            "manifest bundle segmented zarr byteLength must be the sum of its segments for {variable}"
        )));
    }
    Ok(())
}

/// Build a schema v5 manifest describing one bundle per variable.
///
/// Each entry in `bundles` carries `variable` and at least one of its two
/// deliveries — the `.xue` container as `path`, `byteLength` and `crc32`
/// (one unit), the Zarr store as a `zarr` descriptor — and may carry
/// `variants`, `video` and `poster` descriptors (a variant may carry a
/// `zarr` descriptor of its own).
pub fn build_bin_manifest(
    run_time: OffsetDateTime,
    bundles: Vec<Value>,
    expected_hours: i64,
    model: &str,
    product: &str,
    require_core_variables: bool,
    registry: &Registry,
) -> Result<Value> {
    let mut payload = Map::new();
    payload.insert("schemaVersion".into(), json!(5));
    payload.insert("model".into(), json!(model));
    payload.insert("product".into(), json!(product));
    payload.insert("runTime".into(), json!(iso_z(run_time)));
    payload.insert("forecastHours".into(), json!(expected_hours));
    payload.insert("bundles".into(), Value::Array(bundles));
    let payload = Value::Object(payload);
    validate_bin_manifest(&payload, require_core_variables, registry)?;
    Ok(payload)
}

/// The structural rules a reader depends on. Deliberately a subset of the
/// Python validator — the bundle paths and their uniqueness (a shared store
/// excepted), the shape of a bundle name, and the core set — since the Python
/// side stays the contract's reference.
fn validate_bin_manifest(payload: &Value, require_core_variables: bool, registry: &Registry) -> Result<()> {
    let bundles = payload["bundles"]
        .as_array()
        .ok_or_else(|| EncodeError::manifest("manifest bundles must be a list"))?;
    let mut seen = Vec::new();
    let mut paths = Vec::new();
    for bundle in bundles {
        let variable = bundle["variable"].as_str().unwrap_or_default();
        if !is_bundle_variable_name(variable) {
            return Err(EncodeError::manifest(format!(
                "manifest bundle variable is not a bundle name: {variable:?}"
            )));
        }
        validate_container_fields(bundle, variable, "", &mut paths)?;
        if let Some(variants) = bundle["variants"].as_array() {
            for variant in variants {
                validate_container_fields(variant, variable, " variant", &mut paths)?;
                validate_zarr_descriptor(&variant["zarr"], variable, &mut paths)?;
            }
        }
        validate_zarr_descriptor(&bundle["zarr"], variable, &mut paths)?;
        if seen.contains(&variable) {
            return Err(EncodeError::manifest(
                "manifest contains duplicate bundle variables",
            ));
        }
        seen.push(variable);
    }
    if require_core_variables {
        for variable in core_bundle_ids(registry, payload["model"].as_str().unwrap_or_default())? {
            if !seen.contains(&variable.as_str()) {
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

    /// The same descriptor one tier down: every store path's `.zarr` becomes
    /// `.half.zarr`, so a variant names a store of its own.
    fn half(store: &Value) -> Value {
        match store {
            Value::Object(fields) => Value::Object(
                fields
                    .iter()
                    .map(|(key, value)| {
                        let value = match (key.as_str(), value.as_str()) {
                            ("path", Some(path)) if path.ends_with(".zarr") => {
                                json!(format!("{}.half.zarr", path.trim_end_matches(".zarr")))
                            }
                            _ => half(value),
                        };
                        (key.clone(), value)
                    })
                    .collect(),
            ),
            Value::Array(items) => Value::Array(items.iter().map(half).collect()),
            other => other.clone(),
        }
    }

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
            entry["variants"][0]["zarr"] = half(&store);
            entry["zarr"] = store;
        }
        entry
    }

    fn manifest(entries: Vec<Value>) -> Result<Value> {
        let mut bundles = entries;
        bundles.push(json!({"variable": "prate", "path": "prate.xue", "byteLength": 10, "crc32": "0123abcd"}));
        let registry = crate::encode::registry::Registry::embedded();
        build_bin_manifest(datetime!(2026-08-15 06:00 UTC), bundles, 120, "GFS", "pgrb2.0p25", true, &registry)
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
        assert_eq!(payload["bundles"][0]["variants"][0]["zarr"]["path"], "tmp2m.half.zarr");
    }

    #[test]
    fn a_store_only_entry_is_accepted_and_neither_is_rejected() {
        let store = json!({"path": "tmp2m.zarr", "byteLength": 12, "crc32": "deadbeef"});
        let mut entry = bundle(Some(store));
        for key in ["path", "byteLength", "crc32"] {
            entry.as_object_mut().unwrap().remove(key);
            entry["variants"][0].as_object_mut().unwrap().remove(key);
        }
        let payload = manifest(vec![entry.clone()]).expect("a store-only entry is valid");
        assert!(payload["bundles"][0]["path"].is_null());
        assert_eq!(payload["bundles"][0]["variants"][0]["zarr"]["path"], "tmp2m.half.zarr");

        let mut neither = entry.clone();
        neither.as_object_mut().unwrap().remove("zarr");
        assert!(manifest(vec![neither]).is_err(), "an entry with neither delivery was accepted");
        let mut neither_variant = entry.clone();
        neither_variant["variants"][0].as_object_mut().unwrap().remove("zarr");
        assert!(manifest(vec![neither_variant]).is_err(), "a variant with neither delivery was accepted");
        let mut half_unit = entry.clone();
        half_unit["byteLength"] = json!(10);
        assert!(manifest(vec![half_unit]).is_err(), "container fields without a path were accepted");
        let mut wrong_suffix = entry;
        wrong_suffix["path"] = json!("tmp2m.zarr");
        wrong_suffix["byteLength"] = json!(10);
        wrong_suffix["crc32"] = json!("0123abcd");
        assert!(manifest(vec![wrong_suffix]).is_err(), "a .zarr path was accepted as the container");
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

    fn segment(path: &str, length: u64) -> Value {
        json!({"path": path, "byteLength": length, "crc32": "deadbeef"})
    }

    #[test]
    fn a_segmented_store_is_accepted_on_a_bundle_and_on_a_variant() {
        let store = json!({
            "byteLength": 30,
            "segments": [
                segment("../../himawari.segments/202610120200/0123abcd/tmp2m.zarr", 10),
                segment("../../himawari.segments/202610120300/89abcdef/tmp2m.zarr", 20),
            ],
        });
        let payload = manifest(vec![bundle(Some(store))]).expect("valid manifest");
        assert_eq!(payload["bundles"][0]["zarr"]["segments"][1]["byteLength"], 20);
        assert_eq!(payload["bundles"][0]["variants"][0]["zarr"]["byteLength"], 30);

        let mut store_only = bundle(Some(json!({"byteLength": 10, "segments": [segment("seg/tmp2m.zarr", 10)]})));
        for key in ["path", "byteLength", "crc32"] {
            store_only.as_object_mut().unwrap().remove(key);
            store_only["variants"][0].as_object_mut().unwrap().remove(key);
        }
        assert!(manifest(vec![store_only]).is_ok(), "a segmented store-only entry was rejected");
    }

    #[test]
    fn a_malformed_segmented_store_is_rejected() {
        for store in [
            json!({"path": "tmp2m.zarr", "crc32": "deadbeef", "byteLength": 10, "segments": [segment("a/tmp2m.zarr", 10)]}),
            json!({"byteLength": 0, "segments": []}),
            json!({"byteLength": 11, "segments": [segment("a/tmp2m.zarr", 10)]}),
            json!({"byteLength": 10, "segments": [segment("../../../a/tmp2m.zarr", 10)]}),
            json!({"byteLength": 10, "segments": [segment("../a/../tmp2m.zarr", 10)]}),
            json!({"byteLength": 10, "segments": [segment("..", 10)]}),
            json!({"byteLength": 10, "segments": [segment("/a/tmp2m.zarr", 10)]}),
            json!({"byteLength": 10, "segments": [{"path": "a/tmp2m.zarr", "byteLength": 10, "crc32": "DEADBEEF"}]}),
            json!({"path": "../tmp2m.zarr", "byteLength": 10, "crc32": "deadbeef"}),
        ] {
            assert!(manifest(vec![bundle(Some(store.clone()))]).is_err(), "{store} was accepted");
        }
    }

    /// A store-only entry for `variable` on the shared stores of a run:
    /// `bundles.zarr` and its half tier, as profile 2 writes them.
    fn shared_entry(variable: &str, zarr: Value, half_zarr: Value) -> Value {
        json!({
            "variable": variable,
            "zarr": zarr,
            "variants": [{"width": 720, "height": 361, "bandwidth": 1, "zarr": half_zarr}],
        })
    }

    fn store(path: &str) -> Value {
        json!({"path": path, "byteLength": 12, "crc32": "deadbeef"})
    }

    fn manifest_of(bundles: Vec<Value>) -> Result<Value> {
        let registry = crate::encode::registry::Registry::embedded();
        build_bin_manifest(datetime!(2026-08-15 06:00 UTC), bundles, 120, "GFS", "pgrb2.0p25", true, &registry)
    }

    #[test]
    fn every_bundle_of_a_tier_names_the_one_shared_store() {
        let payload = manifest_of(vec![
            shared_entry("tmp2m", store("bundles.zarr"), store("bundles.half.zarr")),
            shared_entry("prate", store("bundles.zarr"), store("bundles.half.zarr")),
            shared_entry("wind10m", store("bundles.zarr"), store("bundles.half.zarr")),
        ])
        .expect("entries sharing a store are valid");
        assert_eq!(payload["bundles"][2]["zarr"]["path"], "bundles.zarr");
        // The satellite disks' lower tiers share theirs the same way.
        assert!(manifest_of(vec![
            shared_entry("tmp2m", store("bundles.quarter.zarr"), store("bundles.eighth.zarr")),
            shared_entry("prate", store("bundles.quarter.zarr"), store("bundles.eighth.zarr")),
        ])
        .is_ok());
    }

    #[test]
    fn segments_of_a_shared_store_are_shared_too() {
        let segmented = |tier: &str| {
            json!({
                "byteLength": 30,
                "segments": [
                    segment(&format!("../../himawari.segments/202610120200/0123abcd/bundles{tier}.zarr"), 10),
                    segment(&format!("../../himawari.segments/202610120300/89abcdef/bundles{tier}.zarr"), 20),
                ],
            })
        };
        assert!(manifest_of(vec![
            shared_entry("tmp2m", segmented(""), segmented(".half")),
            shared_entry("prate", segmented(""), segmented(".half")),
        ])
        .is_ok());
    }

    #[test]
    fn a_duplicated_path_outside_a_shared_store_is_rejected() {
        let duplicated = [
            // Two bundles naming one profile 1 store.
            vec![
                shared_entry("tmp2m", store("tmp2m.zarr"), store("tmp2m.half.zarr")),
                shared_entry("prate", store("tmp2m.zarr"), store("prate.half.zarr")),
            ],
            // A bundle's store named again by its own variant.
            vec![
                shared_entry("tmp2m", store("tmp2m.zarr"), store("tmp2m.zarr")),
                shared_entry("prate", store("bundles.zarr"), store("bundles.half.zarr")),
            ],
            // A store whose name only resembles the shared one.
            vec![
                shared_entry("tmp2m", store("mybundles.zarr"), store("bundles.half.zarr")),
                shared_entry("prate", store("mybundles.zarr"), store("bundles.half.zarr")),
            ],
            vec![
                shared_entry("tmp2m", store("bundles.series.zarr"), store("bundles.half.zarr")),
                shared_entry("prate", store("bundles.series.zarr"), store("bundles.half.zarr")),
            ],
            // One segment named by two profile 1 bundles.
            vec![
                shared_entry(
                    "tmp2m",
                    json!({"byteLength": 10, "segments": [segment("../../m.segments/202610120200/0123abcd/tmp2m.zarr", 10)]}),
                    store("bundles.half.zarr"),
                ),
                shared_entry(
                    "prate",
                    json!({"byteLength": 10, "segments": [segment("../../m.segments/202610120200/0123abcd/tmp2m.zarr", 10)]}),
                    store("bundles.half.zarr"),
                ),
            ],
        ];
        for bundles in duplicated {
            let shown = serde_json::to_string(&bundles).unwrap();
            assert!(manifest_of(bundles).is_err(), "{shown} was accepted");
        }
        // Containers stay one per entry.
        let mut first = bundle(None);
        first["variants"][0]["path"] = json!("tmp2m.xue");
        assert!(manifest(vec![first]).is_err(), "a container named twice was accepted");
    }

    #[test]
    fn the_shared_store_stem_is_matched_on_the_final_component() {
        for path in [
            "bundles.zarr",
            "bundles.half.zarr",
            "bundles.quarter.zarr",
            "bundles.eighth.zarr",
            "../../goeswest.segments/202610120200/0123abcd/bundles.half.zarr",
        ] {
            assert!(is_shared_store_path(path), "{path}");
        }
        for path in [
            "tmp2m.zarr",
            "tmp2m.half.zarr",
            "bundles.series.zarr",
            "bundles.xue",
            "bundles..zarr",
            "mybundles.zarr",
            "bundles.zarr/tmp2m.zarr",
            "bundles.Half.zarr",
        ] {
            assert!(!is_shared_store_path(path), "{path}");
        }
    }
}
