//! Shared stores (`docs/zarr-profile.md`, profile 2): one group per
//! resolution tier, every bundle's metadata under `attributes.xue_bundles`,
//! selected by bundle id. Profile 1 stays readable beside it.

use serde_json::{json, Value};

use xue::zarr::{
    self, group_bundle_ids, parse_array_metadata, parse_group_metadata, parse_group_metadata_for,
    StoreKind, PROFILE_SHARED, PROFILE_SINGLE,
};

/// The smallest metadata the container's validator accepts: schema 1, a 4 x 2
/// grid, an hourly axis.
fn metadata(id: &str, first_hour: u64, frames: u64) -> Value {
    json!({
        "schemaVersion": 1,
        "runTime": "2026-10-23T00:00:00Z",
        "grid": {"width": 4, "height": 2, "firstLongitude": 0.0, "firstLatitude": 1.0,
                 "longitudeStep": 1.0, "latitudeStep": -1.0, "wrapLongitude": false},
        "time": {"frameCount": frames, "firstForecastHour": first_hour, "stepHours": 1},
        "variables": [{
            "numericId": 1, "id": id, "unit": "°C",
            "quantization": {"type": "linear", "offset": -60.0, "scale": 0.5,
                             "minimumCode": 0, "maximumCode": 220, "nodataCode": 255}
        }]
    })
}

fn group(attributes: Value) -> String {
    json!({"zarr_format": 3, "node_type": "group", "attributes": attributes}).to_string()
}

fn shared() -> Value {
    json!({
        "xue_profile": 2,
        "xue_bundles": {
            "tmp2m": metadata("tmp2m", 0, 3),
            "prate": metadata("prate", 1, 2),
        }
    })
}

#[test]
fn a_shared_group_selects_each_bundle_by_id() {
    let text = group(shared());
    assert_eq!(zarr::store_kind(&text).unwrap(), StoreKind::Grid);

    let tmp2m = parse_group_metadata_for(&text, "tmp2m").unwrap();
    assert_eq!(tmp2m.profile, PROFILE_SHARED);
    assert_eq!(tmp2m.bundle.as_deref(), Some("tmp2m"));
    assert_eq!(tmp2m.metadata.variables[0].id, "tmp2m");
    assert_eq!(tmp2m.metadata.offsets, vec![0, 1, 2]);

    // Each bundle keeps its own axis: prate starts an hour in.
    let prate = parse_group_metadata_for(&text, "prate").unwrap();
    assert_eq!(prate.metadata.variables[0].id, "prate");
    assert_eq!(prate.metadata.offsets, vec![1, 2]);
    assert_eq!(prate.metadata.grid.width, 4);

    let mut ids = group_bundle_ids(&text).unwrap().expect("a shared group names its bundles");
    ids.sort();
    assert_eq!(ids, ["prate", "tmp2m"]);
}

#[test]
fn a_shared_group_refuses_a_bundle_it_does_not_hold_and_an_unselected_open() {
    let text = group(shared());
    assert!(parse_group_metadata_for(&text, "gust").is_err());
    assert!(parse_group_metadata_for(&text, "").is_err());
    // Several bundles, none named: the single-bundle entry point refuses.
    assert!(parse_group_metadata(&text).is_err());
}

#[test]
fn a_profile_1_group_reads_as_before_and_ignores_the_bundle_id() {
    let text = group(json!({"xue": metadata("tmp2m", 0, 3), "xue_profile": 1}));
    let plain = parse_group_metadata(&text).unwrap();
    assert_eq!(plain.profile, PROFILE_SINGLE);
    assert_eq!(plain.bundle, None);
    let selected = parse_group_metadata_for(&text, "anything").unwrap();
    assert_eq!(selected.metadata_json, plain.metadata_json);
    assert_eq!(selected.bundle, None);
    assert_eq!(group_bundle_ids(&text).unwrap(), None);
}

#[test]
fn a_group_off_the_two_profiles_is_refused() {
    let cases = [
        // Profile 2 without its block, or with an empty or malformed one.
        json!({"xue_profile": 2}),
        json!({"xue_profile": 2, "xue_bundles": {}}),
        json!({"xue_profile": 2, "xue_bundles": []}),
        json!({"xue_profile": 2, "xue": metadata("tmp2m", 0, 3)}),
        // Profile 1's block under profile 2's attribute, and the reverse.
        json!({"xue_profile": 1, "xue_bundles": {"tmp2m": metadata("tmp2m", 0, 3)}}),
        // Both blocks at once.
        json!({"xue_profile": 2, "xue": metadata("tmp2m", 0, 3),
               "xue_bundles": {"tmp2m": metadata("tmp2m", 0, 3)}}),
        // A version this reader does not implement, or none.
        json!({"xue_profile": 3, "xue_bundles": {"tmp2m": metadata("tmp2m", 0, 3)}}),
        json!({"xue_profile": 0, "xue": metadata("tmp2m", 0, 3)}),
        json!({"xue_profile": 3, "xue": metadata("tmp2m", 0, 3)}),
        json!({"xue_bundles": {"tmp2m": metadata("tmp2m", 0, 3)}}),
        // A shared store beside another branch's block.
        json!({"xue_profile": 2, "xue_bundles": {"tmp2m": metadata("tmp2m", 0, 3)},
               "xue_polar": {"version": 1}}),
    ];
    for attributes in cases {
        let text = group(attributes.clone());
        assert!(
            parse_group_metadata_for(&text, "tmp2m").is_err(),
            "{attributes} was accepted"
        );
        assert!(parse_group_metadata(&text).is_err(), "{attributes} was accepted");
    }
}

#[test]
fn a_selected_bundle_is_held_to_the_metadata_rules() {
    let mut attributes = shared();
    attributes["xue_bundles"]["prate"]["schemaVersion"] = json!(9);
    attributes["xue_bundles"]["gust"] = json!("not an object");
    let text = group(attributes);
    // The bad bundle is refused when selected and does not spoil the others.
    assert!(parse_group_metadata_for(&text, "prate").is_err());
    assert!(parse_group_metadata_for(&text, "gust").is_err());
    assert!(parse_group_metadata_for(&text, "tmp2m").is_ok());
}

#[test]
fn an_array_naming_its_bundle_and_axis_reads_like_any_other() {
    let document = json!({
        "zarr_format": 3, "node_type": "array",
        "shape": [2, 2, 4], "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [6, 2, 4]}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": 255,
        "codecs": [{"name": "sharding_indexed", "configuration": {
            "chunk_shape": [6, 2, 4],
            "codecs": [{"name": "bytes"}, {"name": "zstd", "configuration": {"level": 15, "checksum": true}}],
            "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}],
            "index_location": "end"
        }}],
        "attributes": {"xue": {"variable": {"numericId": 1, "id": "prate"}, "predictor": "raw",
                               "bundle": "prate"}},
        "dimension_names": ["time_prate", "latitude", "longitude"]
    });
    let layout = parse_array_metadata(&document.to_string()).unwrap();
    assert_eq!(layout.frame_count, 2);
    assert_eq!(layout.width, 4);
}
