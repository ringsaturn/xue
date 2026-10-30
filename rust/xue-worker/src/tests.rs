//! The handlers over the web fixtures (`tests/prepare_web_fixture.py`), read
//! through an in-memory bucket so every read a query costs is countable.
//!
//! The reference for a value is the `.xue` container the store was exported
//! from, decoded by `xue::Bundle`: a point read through the series companion,
//! through the map store and straight from the container must agree code for
//! code.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Once;

use futures_executor::block_on;
use serde_json::{json, Value};
use url::Url;

use xue::zarr;
use xue::Bundle;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::point::{parse_cell_query, parse_query, read_cell, read_point};
use crate::source::{catalog, read_source};

/// The run directory the fixture's pointer names; its objects sit flat in the
/// fixture root.
const RUN: &str = "gfs.2026081506";
const FRAMES: usize = 121;
/// Tokyo on the fixture's 2.5° grid.
const LAT: f64 = 35.0;
const LON: f64 = 140.0;

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture_root() -> PathBuf {
    repository_root().join("tests/fixtures/generated/web")
}

/// The fixtures are generated and gitignored; build them once on a fresh
/// checkout, the way the web unit tests do.
fn ensure_fixtures() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        if fixture_root()
            .join("wind10m.series.zarr/zarr.json")
            .exists()
        {
            return;
        }
        let python = std::env::var("PYTHON").unwrap_or_else(|_| ".venv/bin/python".to_owned());
        let status = Command::new(&python)
            .arg("tests/prepare_web_fixture.py")
            .current_dir(repository_root())
            .status()
            .unwrap_or_else(|error| panic!("cannot run {python}: {error}"));
        assert!(status.success(), "tests/prepare_web_fixture.py failed");
    });
}

fn walk(directory: &Path, root: &Path, objects: &mut HashMap<String, Vec<u8>>) {
    for entry in std::fs::read_dir(directory).expect("fixture directory") {
        let path = entry.expect("fixture entry").path();
        if path.is_dir() {
            walk(&path, root, objects);
        } else {
            let relative = path.strip_prefix(root).expect("under the root");
            let key = relative.to_string_lossy().replace('\\', "/");
            objects.insert(
                format!("{RUN}/{key}"),
                std::fs::read(&path).expect("fixture file"),
            );
        }
    }
}

/// The fixture as a bucket: the run under its directory, the pointer at the
/// root, and a Collection naming the pointer as a published one does.
fn bucket() -> HashMap<String, Vec<u8>> {
    ensure_fixtures();
    let root = fixture_root();
    let mut objects = HashMap::new();
    walk(&root, &root, &mut objects);
    objects.insert(
        "latest.json".to_owned(),
        std::fs::read(root.join("latest.json")).expect("pointer"),
    );
    put_json(
        &mut objects,
        "gfs/collection.json",
        &json!({ "xue:pointer": "latest.json" }),
    );
    objects
}

fn put_json(objects: &mut HashMap<String, Vec<u8>>, key: &str, value: &Value) {
    objects.insert(key.to_owned(), serde_json::to_vec(value).expect("json"));
}

fn get(data: &Data, query: &str) -> Result<Value, HttpError> {
    let url = Url::parse(&format!("https://api.example/v1/point?{query}")).expect("url");
    block_on(read_point(data, &parse_query(&url))).map(|(body, _)| body)
}

fn ok(data: &Data, query: &str) -> Value {
    get(data, query).unwrap_or_else(|error| panic!("{query}: {} {}", error.code, error.message))
}

fn err(data: &Data, query: &str) -> HttpError {
    match get(data, query) {
        Ok(body) => panic!("{query}: expected an error, got {body}"),
        Err(error) => error,
    }
}

fn get_cell(data: &Data, query: &str) -> Result<Value, HttpError> {
    let url = Url::parse(&format!("https://api.example/v1/cell?{query}")).expect("url");
    block_on(read_cell(data, &parse_cell_query(&url))).map(|(body, _)| body)
}

fn cell_ok(data: &Data, query: &str) -> Value {
    get_cell(data, query).unwrap_or_else(|error| panic!("{query}: {} {}", error.code, error.message))
}

fn cell_err(data: &Data, query: &str) -> HttpError {
    match get_cell(data, query) {
        Ok(body) => panic!("{query}: expected an error, got {body}"),
        Err(error) => error,
    }
}

/// Apply `edit` to every `grid` object in a JSON document; how many it found.
fn edit_grids(value: &mut Value, edit: &dyn Fn(&mut Value)) -> usize {
    match value {
        Value::Object(map) => map
            .iter_mut()
            .map(|(key, child)| {
                if key == "grid" && child.is_object() {
                    edit(child);
                    1
                } else {
                    edit_grids(child, edit)
                }
            })
            .sum(),
        Value::Array(items) => items.iter_mut().map(|item| edit_grids(item, edit)).sum(),
        _ => 0,
    }
}

/// The map store a bundle's manifest entry names: its own store, or the
/// shared one every bundle of the tier names.
fn map_store(bundle: &str) -> String {
    let manifest: Value = serde_json::from_slice(
        &std::fs::read(fixture_root().join("manifest.json")).expect("manifest"),
    )
    .expect("manifest json");
    manifest["bundles"]
        .as_array()
        .expect("bundles")
        .iter()
        .find(|entry| entry["variable"] == bundle)
        .and_then(|entry| entry["zarr"]["path"].as_str())
        .unwrap_or_else(|| panic!("{bundle} names no map store"))
        .to_owned()
}

/// One variable's values at the probed cell, decoded from the container with
/// the store's own codebook.
fn reference(bundle: &str, variable: &str, column: u32, row: u32) -> Vec<Option<f64>> {
    let root = fixture_root();
    let group = zarr::parse_group_metadata_for(
        &std::fs::read_to_string(root.join(format!("{}/zarr.json", map_store(bundle))))
            .expect("group"),
        bundle,
    )
    .expect("group metadata");
    let (position, metadata) = group
        .metadata
        .variables
        .iter()
        .enumerate()
        .find(|(_, held)| held.id == variable)
        .expect("variable in the bundle");
    let bytes = std::fs::read(root.join(format!("{bundle}.xue"))).expect("container");
    let mut container = Bundle::open(&bytes).expect("bundle");
    // Numeric ids are positional, 1..n in the bundle's own order.
    let codes = container
        .decode_series(position as u8 + 1, column, row)
        .expect("series");
    codes
        .into_iter()
        .map(|code| zarr::decode_value(&metadata.quantization, code))
        .collect()
}

fn cell(body: &Value) -> (u32, u32) {
    (
        body["cell"]["column"].as_u64().expect("column") as u32,
        body["cell"]["row"].as_u64().expect("row") as u32,
    )
}

fn values(body: &Value, variable: &str) -> Vec<Option<f64>> {
    body["variables"][variable]["values"]
        .as_array()
        .unwrap_or_else(|| panic!("no values for {variable}"))
        .iter()
        .map(Value::as_f64)
        .collect()
}

/// Ranged chunk reads (not index suffixes) under a store.
fn chunk_reads(data: &Data, store: &str) -> usize {
    data.reads()
        .iter()
        .filter(|(path, kind)| *kind == "range" && path.starts_with(&format!("{RUN}/{store}/")))
        .count()
}

/// The manifest with every bundle's series companion removed, served at the
/// run directory so a `run=` query reads it.
fn without_series(mut objects: HashMap<String, Vec<u8>>) -> HashMap<String, Vec<u8>> {
    let key = format!("{RUN}/manifest.json");
    let mut manifest: Value = serde_json::from_slice(&objects[&key]).expect("manifest");
    for bundle in manifest["bundles"].as_array_mut().expect("bundles") {
        bundle.as_object_mut().expect("bundle").remove("series");
    }
    put_json(&mut objects, &key, &manifest);
    objects
}

#[test]
fn a_linear_series_reads_one_chunk_of_the_companion_and_matches_the_container() {
    let data = Data::in_memory(bucket());
    let body = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m"),
    );
    let (column, row) = cell(&body);
    let got = values(&body, "tmp2m");
    assert_eq!(got.len(), FRAMES);
    assert_eq!(got, reference("tmp2m", "tmp2m", column, row));
    assert!(
        got.iter().all(Option::is_some),
        "the synthetic field has no gaps"
    );
    assert_eq!(body["variables"]["tmp2m"]["bundle"], "tmp2m");
    assert_eq!(chunk_reads(&data, "tmp2m.series.zarr"), 1);
    assert_eq!(chunk_reads(&data, &map_store("tmp2m")), 0);
}

#[test]
fn without_a_companion_the_map_store_gives_the_same_series_one_chunk_per_time_chunk() {
    let data = Data::in_memory(without_series(bucket()));
    let body = ok(
        &data,
        &format!("source=gfs&run={RUN}&lat={LAT}&lon={LON}&variables=tmp2m"),
    );
    let (column, row) = cell(&body);
    assert_eq!(
        values(&body, "tmp2m"),
        reference("tmp2m", "tmp2m", column, row)
    );
    assert_eq!(chunk_reads(&data, "tmp2m.series.zarr"), 0);
    // Six frames to a time chunk: 121 frames span 21 of them.
    assert_eq!(chunk_reads(&data, &map_store("tmp2m")), FRAMES.div_ceil(6));
}

/// Where [`shared`] puts the run's one shared store.
const SHARED: &str = "repacked/bundles.zarr";

/// The run with every bundle's map store folded into one shared (profile 2)
/// store: each bundle's metadata under `xue_bundles`, its arrays and
/// coordinates copied in, and every entry naming the one store. Series
/// companions are dropped so the map store is what a query reads.
fn shared(objects: HashMap<String, Vec<u8>>) -> HashMap<String, Vec<u8>> {
    let mut objects = without_series(objects);
    let key = format!("{RUN}/manifest.json");
    let mut manifest: Value = serde_json::from_slice(&objects[&key]).expect("manifest");
    let mut held = serde_json::Map::new();
    let mut copies = Vec::new();
    for entry in manifest["bundles"].as_array_mut().expect("bundles") {
        let Some(path) = entry["zarr"]["path"].as_str().map(str::to_owned) else {
            continue;
        };
        let id = entry["variable"].as_str().expect("variable").to_owned();
        let base = format!("{RUN}/{path}/");
        let group = zarr::parse_group_metadata_for(
            std::str::from_utf8(&objects[&format!("{base}zarr.json")]).expect("UTF-8"),
            &id,
        )
        .expect("group");
        held.insert(
            id,
            serde_json::from_str(&group.metadata_json).expect("metadata"),
        );
        for (name, bytes) in &objects {
            if let Some(rest) = name.strip_prefix(&base) {
                if rest != "zarr.json" {
                    copies.push((format!("{RUN}/{SHARED}/{rest}"), bytes.clone()));
                }
            }
        }
        // One crc on every entry, as the group document's is.
        entry["zarr"] = json!({ "path": SHARED, "byteLength": 1, "crc32": "5ba7ed00" });
    }
    objects.extend(copies);
    put_json(
        &mut objects,
        &format!("{RUN}/{SHARED}/zarr.json"),
        &json!({
            "zarr_format": 3,
            "node_type": "group",
            "attributes": { "xue_profile": 2, "xue_bundles": held },
        }),
    );
    put_json(&mut objects, &key, &manifest);
    objects
}

#[test]
fn a_shared_store_answers_as_the_per_bundle_stores_do_and_reads_its_group_once() {
    let query = format!("source=gfs&run={RUN}&lat={LAT}&lon={LON}&variables=tmp2m,prate,wind10m");
    let plain = ok(&Data::in_memory(without_series(bucket())), &query);
    let data = Data::in_memory(shared(bucket()));
    let body = ok(&data, &query);
    assert_eq!(body["variables"], plain["variables"]);
    assert_eq!(body["time"], plain["time"]);
    assert_eq!(body["cell"], plain["cell"]);
    assert_eq!(body["variables"]["ugrd10m"]["bundle"], "wind10m");
    let group_reads = data
        .reads()
        .iter()
        .filter(|(path, _)| *path == format!("{RUN}/{SHARED}/zarr.json"))
        .count();
    assert_eq!(group_reads, 1, "the shared group is read once for every bundle");
    // Every bundle's frames come out of the one store.
    assert!(chunk_reads(&data, SHARED) >= 3 * FRAMES.div_ceil(6));
}

#[test]
fn a_bundle_a_shared_store_does_not_hold_is_502_upstream_failed() {
    let mut objects = shared(bucket());
    let key = format!("{RUN}/{SHARED}/zarr.json");
    let mut group: Value = serde_json::from_slice(&objects[&key]).expect("group");
    group["attributes"]["xue_bundles"]
        .as_object_mut()
        .expect("xue_bundles")
        .remove("prate");
    put_json(&mut objects, &key, &group);
    // A crc of its own, so the isolate cache cannot answer with the whole group.
    let manifest_key = format!("{RUN}/manifest.json");
    let mut manifest: Value = serde_json::from_slice(&objects[&manifest_key]).expect("manifest");
    for entry in manifest["bundles"].as_array_mut().expect("bundles") {
        if entry["zarr"]["path"] == SHARED {
            entry["zarr"]["crc32"] = json!("0dd5e700");
        }
    }
    put_json(&mut objects, &manifest_key, &manifest);
    let error = err(
        &Data::in_memory(objects),
        &format!("source=gfs&run={RUN}&lat={LAT}&lon={LON}&variables=prate"),
    );
    assert_eq!(
        (error.status, error.code.as_str()),
        (502, "upstream_failed")
    );
}

#[test]
fn a_log1p_series_decodes_through_its_codebook() {
    let data = Data::in_memory(bucket());
    let body = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=prate"),
    );
    let (column, row) = cell(&body);
    assert_eq!(
        values(&body, "prate"),
        reference("prate", "prate", column, row)
    );
}

#[test]
fn a_vector_bundle_answers_as_its_components_each_naming_the_bundle() {
    let data = Data::in_memory(bucket());
    let body = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=wind10m,tmp2m"),
    );
    let (column, row) = cell(&body);
    for component in ["ugrd10m", "vgrd10m"] {
        assert_eq!(body["variables"][component]["bundle"], "wind10m");
        assert_eq!(
            values(&body, component),
            reference("wind10m", component, column, row)
        );
    }
    assert_eq!(body["request"]["variables"], json!(["wind10m", "tmp2m"]));
    // A repeated parameter is the same request.
    let repeated = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=wind10m&variables=tmp2m"),
    );
    assert_eq!(repeated["variables"], body["variables"]);
}

#[test]
fn the_live_chain_resolves_collection_pointer_and_manifest() {
    let data = Data::in_memory(bucket());
    let body = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m"),
    );
    assert_eq!(body["source"], "gfs");
    assert_eq!(body["run"], RUN);
    assert_eq!(body["runTime"], "2026-08-15T06:00:00Z");
    assert_eq!(body["time"]["unitSeconds"], 3600);
    let times = body["time"]["times"].as_array().expect("times");
    assert_eq!(times.len(), FRAMES);
    assert_eq!(times[0], "2026-08-15T06:00:00.000Z");
    assert_eq!(times[1], "2026-08-15T07:00:00.000Z");
    let paths: Vec<String> = data.reads().into_iter().map(|(path, _)| path).collect();
    assert_eq!(paths[0], "gfs/collection.json");
    assert_eq!(paths[1], "latest.json");
    assert_eq!(paths[2], format!("{RUN}/manifest.json"));
}

#[test]
fn time_picks_the_nearest_frame() {
    let data = Data::in_memory(bucket());
    let all = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m"),
    );
    let one = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m&time=2026-08-15T08:20:00Z"),
    );
    assert_eq!(one["time"]["times"], json!(["2026-08-15T08:00:00.000Z"]));
    assert_eq!(values(&one, "tmp2m"), vec![values(&all, "tmp2m")[2]]);
}

#[test]
fn without_variables_the_first_bundle_is_read() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, &format!("source=gfs&lat={LAT}&lon={LON}"));
    assert_eq!(body["request"]["variables"], json!(["tmp2m"]));
}

#[test]
fn the_point_carries_its_iana_zone_and_open_sea_the_nautical_one() {
    let data = Data::in_memory(bucket());
    let tokyo = ok(&data, &format!("source=gfs&lat={LAT}&lon={LON}"));
    assert_eq!(tokyo["timezone"], json!("Asia/Tokyo"));
    // The zone follows the point, wrapped like the cell is: New York at
    // -74 and at 286 are the same place.
    assert_eq!(
        ok(&data, "source=gfs&lat=40.7&lon=-74")["timezone"],
        json!("America/New_York")
    );
    assert_eq!(
        ok(&data, "source=gfs&lat=40.7&lon=286")["timezone"],
        json!("America/New_York")
    );
    // tzf-dist's lite file covers the oceans with the nautical zones.
    let pacific = ok(&data, "source=gfs&lat=0&lon=-150");
    assert_eq!(pacific["timezone"], json!("Etc/GMT+10"));
}

#[test]
fn a_longitude_past_180_wraps_onto_the_same_cell() {
    let data = Data::in_memory(bucket());
    let west = ok(&data, "source=gfs&lat=10&lon=-100&variables=tmp2m");
    let east = ok(&data, "source=gfs&lat=10&lon=260&variables=tmp2m");
    assert_eq!(cell(&west), cell(&east));
}

#[test]
fn bad_parameters_are_400_invalid_parameter() {
    let data = Data::in_memory(bucket());
    for query in [
        "lat=35&lon=140",
        "source=gfs&lon=140",
        "source=gfs&lat=91&lon=140",
        "source=gfs&lat=35&lon=-181",
        "source=gfs&lat=35&lon=361",
        "source=gfs&lat=north&lon=140",
        "source=gfs&lat=35&lon=140&time=tomorrow",
        "source=gfs&lat=35&lon=140&run=../gfs",
    ] {
        let error = err(&data, query);
        assert_eq!(
            (error.status, error.code.as_str()),
            (400, "invalid_parameter"),
            "{query}"
        );
    }
}

#[test]
fn unknown_names_are_404_with_their_own_codes() {
    let mut objects = bucket();
    put_json(
        &mut objects,
        "quiet/collection.json",
        &json!({ "title": "no live run" }),
    );
    let data = Data::in_memory(objects);

    let error = err(&data, "source=nowhere&lat=35&lon=140");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_source"));

    let error = err(&data, "source=quiet&lat=35&lon=140");
    assert_eq!((error.status, error.code.as_str()), (404, "no_live_run"));

    let error = err(&data, "source=gfs&lat=35&lon=140&run=gfs.2000010100");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_run"));

    let error = err(&data, "source=gfs&lat=35&lon=140&variables=snow");
    assert_eq!(
        (error.status, error.code.as_str()),
        (404, "unknown_variable")
    );
    let available = error.detail.expect("detail")["available"].clone();
    assert!(available
        .as_array()
        .expect("list")
        .contains(&json!("tmp2m")));

    // A bundle the manifest names but that ships no store to read.
    let error = err(&data, "source=gfs&lat=35&lon=140&variables=gust");
    assert_eq!(
        (error.status, error.code.as_str()),
        (404, "unknown_variable")
    );
}

#[test]
fn a_broken_store_is_502_upstream_failed() {
    let mut objects = bucket();
    objects.insert(
        format!("{RUN}/tmp2m.series.zarr/zarr.json"),
        b"{not json".to_vec(),
    );
    // A crc no other test uses, so the isolate cache cannot answer for the
    // broken document with the good one.
    let key = format!("{RUN}/manifest.json");
    let mut manifest: Value = serde_json::from_slice(&objects[&key]).expect("manifest");
    manifest["bundles"][0]["series"]["crc32"] = json!("broken00");
    put_json(&mut objects, &key, &manifest);
    let data = Data::in_memory(objects);
    let error = err(
        &data,
        &format!("source=gfs&run={RUN}&lat=35&lon=140&variables=tmp2m"),
    );
    assert_eq!(
        (error.status, error.code.as_str()),
        (502, "upstream_failed")
    );
}

#[test]
fn the_catalog_lists_the_child_collections() {
    let mut objects = bucket();
    put_json(
        &mut objects,
        "catalog.json",
        &json!({
            "links": [
                { "rel": "self", "href": "catalog.json" },
                { "rel": "child", "href": "gfs/collection.json", "title": "NOAA GFS" },
                { "rel": "child", "href": "mrms/collection.json", "title": "NOAA MRMS" },
            ]
        }),
    );
    let body = block_on(catalog(&Data::in_memory(objects))).expect("catalog");
    assert_eq!(
        body["collections"],
        json!([
            { "id": "gfs", "title": "NOAA GFS", "href": "gfs/collection.json" },
            { "id": "mrms", "title": "NOAA MRMS", "href": "mrms/collection.json" },
        ])
    );
    assert!(body["dataOrigin"].is_string());
}

#[test]
fn a_source_summary_flattens_the_items_variables() {
    let mut objects = bucket();
    put_json(
        &mut objects,
        "gfs/collection.json",
        &json!({ "title": "NOAA GFS", "license": "other", "xue:live": RUN, "xue:pointer": "latest.json" }),
    );
    put_json(
        &mut objects,
        "gfs/item.json",
        &json!({
            "id": RUN,
            "bbox": [-180, -90, 180, 90],
            "properties": {
                "forecast:reference_datetime": "2026-08-15T06:00:00Z",
                "cube:variables": {
                    "tmp2m": {
                        "description": "2 m temperature",
                        "unit": "°C",
                        "xue:quantization": { "type": "linear", "offset": -60.0, "scale": 0.5, "minimumCode": 0, "maximumCode": 220, "nodataCode": 255 },
                    },
                    "ugrd10m": { "description": "10 m u wind", "unit": "m/s", "xue:bundle": "wind10m" },
                }
            }
        }),
    );
    let data = Data::in_memory(objects);
    let body = block_on(read_source(&data, "gfs")).expect("summary");
    assert_eq!(body["run"], RUN);
    assert_eq!(body["runTime"], "2026-08-15T06:00:00Z");
    assert_eq!(
        body["variables"]["tmp2m"],
        json!({
            "bundle": "tmp2m",
            "label": "2 m temperature",
            "unit": "°C",
            "quantization": { "type": "linear", "offset": -60.0, "scale": 0.5, "minimumCode": 0, "maximumCode": 220, "nodataCode": 255 },
        })
    );
    assert_eq!(body["variables"]["ugrd10m"]["bundle"], "wind10m");
    // An Item without a codebook answers `null`, not a missing field.
    assert_eq!(body["variables"]["ugrd10m"]["quantization"], Value::Null);
    assert!(body["variables"]["ugrd10m"]
        .as_object()
        .is_some_and(|entry| entry.contains_key("quantization")));

    let error = block_on(read_source(&data, "nowhere")).expect_err("unknown");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_source"));
}

/// `docs/api.md` is the contract's prose and `static/openapi.json` its
/// machine copy: the two list the same routes.
#[test]
fn the_openapi_document_and_docs_api_md_list_the_same_routes() {
    let openapi: Value =
        serde_json::from_str(include_str!("../static/openapi.json")).expect("openapi");
    let mut documented: Vec<String> = openapi["paths"]
        .as_object()
        .expect("paths")
        .keys()
        .cloned()
        .collect();
    documented.sort();

    let prose =
        std::fs::read_to_string(repository_root().join("docs/api.md")).expect("docs/api.md");
    // The endpoint table's rows: "| `GET /v1/point` | …".
    let mut described: Vec<String> = prose
        .lines()
        .filter_map(|line| line.strip_prefix("| `GET "))
        .filter_map(|rest| rest.split('`').next())
        .map(str::to_owned)
        .collect();
    described.sort();
    assert_eq!(documented, described);
}

#[test]
fn a_cell_reads_what_the_point_inside_it_reads() {
    let data = Data::in_memory(bucket());
    let point = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m,wind10m"),
    );
    let (row, column) = (&point["cell"]["row"], &point["cell"]["column"]);
    let cell = cell_ok(
        &data,
        &format!("source=gfs&run={RUN}&row={row}&column={column}&variables=tmp2m,wind10m"),
    );
    assert_eq!(cell["run"], RUN);
    assert_eq!(cell["cell"], point["cell"]);
    assert_eq!(cell["time"], point["time"]);
    assert_eq!(cell["variables"], point["variables"]);
    assert_eq!(
        cell["request"],
        json!({ "row": row, "column": column, "variables": ["tmp2m", "wind10m"] })
    );
    assert_eq!(cell["timezone"], json!("Asia/Tokyo"));
}

#[test]
fn a_cell_needs_a_run_and_indices_on_the_grid() {
    let data = Data::in_memory(bucket());
    let missing = cell_err(&data, "source=gfs&row=1&column=1");
    assert_eq!((missing.status, missing.code.as_str()), (400, "invalid_parameter"));
    assert!(missing.message.contains("run is required"));
    // A cell never follows the live pointer.
    assert!(data.reads().is_empty());

    let negative = cell_err(&data, &format!("source=gfs&run={RUN}&row=-1&column=1"));
    assert_eq!((negative.status, negative.code.as_str()), (400, "invalid_parameter"));

    let outside = cell_err(
        &data,
        &format!("source=gfs&run={RUN}&row=73&column=0&variables=tmp2m"),
    );
    assert_eq!((outside.status, outside.code.as_str()), (400, "invalid_parameter"));
    assert_eq!(outside.detail, Some(json!({ "rows": 73, "columns": 144 })));

    let unknown = cell_err(&data, "source=gfs&run=gfs.1999010100&row=1&column=1");
    assert_eq!((unknown.status, unknown.code.as_str()), (404, "unknown_run"));
}

#[test]
fn a_cell_reads_a_repeated_bundle_once() {
    let data = Data::in_memory(bucket());
    let body = cell_ok(
        &data,
        &format!("source=gfs&run={RUN}&row=22&column=56&variables=tmp2m,tmp2m"),
    );
    assert_eq!(body["request"]["variables"], json!(["tmp2m"]));
}

#[test]
fn a_cell_refuses_bundles_on_different_grids() {
    let mut objects = bucket();
    // prate is read through its series store; give that a coarser grid.
    let key = format!("{RUN}/prate.series.zarr/zarr.json");
    let mut group: Value =
        serde_json::from_slice(objects.get(&key).expect("prate series group")).expect("json");
    let edited = edit_grids(&mut group, &|grid| {
        grid["width"] = json!(72);
        grid["longitudeStep"] = json!(5.0);
    });
    assert!(edited > 0, "the group carries a grid");
    put_json(&mut objects, &key, &group);
    let data = Data::in_memory(objects);

    let error = cell_err(
        &data,
        &format!("source=gfs&run={RUN}&row=22&column=56&variables=tmp2m,prate"),
    );
    assert_eq!((error.status, error.code.as_str()), (400, "grid_mismatch"));
    assert_eq!(error.detail, Some(json!({ "bundles": ["tmp2m", "prate"] })));
}

/// A caller's own object source answers through the same handlers.
struct FixtureSource(HashMap<String, Vec<u8>>);

impl crate::ObjectSource for FixtureSource {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<crate::ReadRange>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<u8>, HttpError>> + 'a>>
    {
        Box::pin(async move {
            let bytes = self
                .0
                .get(path)
                .ok_or_else(|| HttpError::new(404, "not_found", format!("no object {path}")))?;
            Ok(match range {
                Some(range) => range.slice(bytes).to_vec(),
                None => bytes.clone(),
            })
        })
    }
}

#[test]
fn an_object_source_answers_as_the_bucket_does() {
    let query = format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m,wind10m");
    let from_bucket = ok(&Data::in_memory(bucket()), &query);
    let source = Data::from_source(Box::new(FixtureSource(bucket())), "https://mirror.example/");
    assert_eq!(ok(&source, &query), from_bucket);
    let missing = err(&source, "source=nowhere&lat=0&lon=0");
    assert_eq!((missing.status, missing.code.as_str()), (404, "unknown_source"));
}

/// Each variable carries its codebook, and its values sit on it.
#[test]
fn a_variable_carries_the_codebook_its_values_sit_on() {
    let data = Data::in_memory(bucket());
    let body = ok(
        &data,
        &format!("source=gfs&lat={LAT}&lon={LON}&variables=tmp2m,prate"),
    );
    let tmp2m = &body["variables"]["tmp2m"]["quantization"];
    assert_eq!(tmp2m["type"], "linear");
    let (offset, scale) = (
        tmp2m["offset"].as_f64().expect("offset"),
        tmp2m["scale"].as_f64().expect("scale"),
    );
    assert!(scale > 0.0);
    for value in values(&body, "tmp2m").into_iter().flatten() {
        let steps = (value - offset) / scale;
        assert!((steps - steps.round()).abs() < 1e-6, "{value} is off the {scale} grid");
    }
    assert!(tmp2m["nodataCode"].is_u64());
    assert_eq!(body["variables"]["prate"]["quantization"]["type"], "log1p");
}
