//! The point-product handlers over the products' committed goldens
//! (`tests/fixtures/{sounding,airport,synop,aq,alerts,tc}/expected/`), laid
//! out in an in-memory bucket the way the publishers lay them out on R2: the
//! pointer at the root, the issue under the directory the pointer names.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use futures_executor::block_on;
use serde_json::{json, Value};
use url::Url;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::point::{parse_query, read_point};
use crate::source::read_source;
use crate::stations::{AQ, SYNOP};
use crate::{airport, alerts, sounding, stations, tc};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}

fn put(objects: &mut HashMap<String, Vec<u8>>, key: &str, file: &Path) {
    objects.insert(
        key.to_owned(),
        std::fs::read(file).unwrap_or_else(|error| panic!("{}: {error}", file.display())),
    );
}

fn put_json(objects: &mut HashMap<String, Vec<u8>>, key: &str, value: &Value) {
    objects.insert(key.to_owned(), serde_json::to_vec(value).expect("json"));
}

fn pointer_dir(objects: &HashMap<String, Vec<u8>>, pointer: &str) -> String {
    let pointer: Value = serde_json::from_slice(&objects[pointer]).expect("pointer");
    let path = pointer["path"].as_str().expect("path");
    path.rsplit_once('/').map(|(dir, _)| dir.to_owned()).unwrap()
}

/// Every `<product>.<digits>` directory under `dir`, at any depth, into the
/// bucket under its path from `root`.
fn put_rounds(objects: &mut HashMap<String, Vec<u8>>, product: &str, root: &Path, dir: &Path) {
    for entry in std::fs::read_dir(dir).unwrap_or_else(|error| panic!("{}: {error}", dir.display())) {
        let path = entry.expect("entry").path();
        if !path.is_dir() {
            continue;
        }
        let name = path.file_name().and_then(|name| name.to_str()).expect("name");
        let is_round = name
            .strip_prefix(product)
            .and_then(|rest| rest.strip_prefix('.'))
            .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()));
        if !is_round {
            put_rounds(objects, product, root, &path);
            continue;
        }
        let prefix = path.strip_prefix(root).expect("under root").to_str().expect("utf-8").replace('\\', "/");
        for file in std::fs::read_dir(&path).expect("round") {
            let file = file.expect("entry").file_name().into_string().expect("name");
            if file.ends_with(".json") || file.ends_with(".jsonl") {
                put(objects, &format!("{prefix}/{file}"), &path.join(&file));
            }
        }
    }
}

/// Every product, live, plus the previous airport, synop, aq and alerts
/// rounds for the `issue=` pin, and a Collection per product as the catalog
/// publishes.
fn bucket() -> HashMap<String, Vec<u8>> {
    let root = fixtures();
    let mut objects = HashMap::new();

    let sounding = root.join("sounding/expected");
    put(&mut objects, "latest-sounding.json", &sounding.join("latest-sounding.json"));
    let dir = pointer_dir(&objects, "latest-sounding.json");
    assert_eq!(dir, "sounding/2026/09/14/sounding.2026091402");
    for file in ["index.json", "soundings.jsonl"] {
        put(&mut objects, &format!("{dir}/{file}"), &sounding.join(file));
    }

    let tc = root.join("tc/expected");
    put(&mut objects, "latest-tc.json", &tc.join("latest-tc.json"));
    let dir = pointer_dir(&objects, "latest-tc.json");
    for file in [
        "index.json",
        "EP142026.json",
        "x-ep-2026091012-1.json",
        "x-wp-2026091200-1.json",
    ] {
        put(&mut objects, &format!("{dir}/{file}"), &tc.join(file));
    }

    // Every round of the minute-round products, at the path it has under
    // the golden's root (flat `<product>.<round>/` or filed under its day,
    // `<product>/YYYY/MM/DD/<product>.<round>/`), every file of each: a v1
    // synop round is the index and the network files, a v2 round adds a
    // station table per network.
    for product in ["airport", "synop", "aq", "alerts"] {
        let expected = root.join(product).join("expected");
        let pointer = format!("latest-{product}.json");
        put(&mut objects, &pointer, &expected.join(&pointer));
        put_rounds(&mut objects, product, &expected, &expected);
        let live = pointer_dir(&objects, &pointer);
        assert!(objects.contains_key(&format!("{live}/index.json")), "{live}");
    }

    for product in ["sounding", "airport", "synop", "tc", "aq", "alerts"] {
        put_json(
            &mut objects,
            &format!("{product}/collection.json"),
            &json!({ "id": product, "xue:pointer": format!("latest-{product}.json") }),
        );
    }
    objects
}

type Handler = fn(&Data, &Url) -> Result<(Value, String), HttpError>;

fn list(product: &str) -> Handler {
    match product {
        "soundings" => |data, url| block_on(sounding::list(data, url)),
        "airports" => |data, url| block_on(airport::list(data, url)),
        "synop" => |data, url| block_on(stations::list(&SYNOP, data, url)),
        "aq" => |data, url| block_on(stations::list(&AQ, data, url)),
        "alerts" => |data, url| block_on(alerts::list(data, url)),
        _ => |data, url| block_on(tc::list(data, url)),
    }
}

fn station(product: &str, id: &str, data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    match product {
        "soundings" => block_on(sounding::station(data, id, url)),
        "airports" => block_on(airport::station(data, id, url)),
        "synop" => block_on(stations::station(&SYNOP, data, id, url)),
        "aq" => block_on(stations::station(&AQ, data, id, url)),
        "alerts" => block_on(alerts::alert(data, id, url)),
        _ => block_on(tc::storm(data, id, url)),
    }
}

/// `GET /v1/<route>?<query>`: the route is `soundings`, `airports/RJTT`, ….
fn get(data: &Data, route: &str, query: &str) -> Result<Value, HttpError> {
    let url = Url::parse(&format!("https://api.example/v1/{route}?{query}")).expect("url");
    let (product, id) = match route.split_once('/') {
        Some((product, id)) => (product, Some(id)),
        None => (route, None),
    };
    match id {
        None => list(product)(data, &url),
        Some(id) => station(product, id, data, &url),
    }
    .map(|(body, _)| body)
}

fn ok(data: &Data, route: &str, query: &str) -> Value {
    get(data, route, query)
        .unwrap_or_else(|error| panic!("{route}?{query}: {} {}", error.code, error.message))
}

fn err(data: &Data, route: &str, query: &str) -> HttpError {
    match get(data, route, query) {
        Ok(body) => panic!("{route}?{query}: expected an error, got {body}"),
        Err(error) => error,
    }
}

fn ids(body: &Value, list: &str, key: &str) -> Vec<String> {
    body[list]
        .as_array()
        .expect(list)
        .iter()
        .map(|row| row[key].as_str().expect(key).to_owned())
        .collect()
}

fn range_reads(data: &Data, suffix: &str) -> usize {
    data.reads()
        .iter()
        .filter(|(path, kind)| *kind == "range" && path.ends_with(suffix))
        .count()
}

// ── soundings ───────────────────────────────────────────────────────────

#[test]
fn the_sounding_list_is_the_index_without_its_byte_spans() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "soundings", "");
    assert_eq!(body["product"], "sounding");
    assert_eq!(body["issue"], "sounding.2026091402");
    assert_eq!(body["issued"], "2026-09-14T02:00:00Z");
    assert_eq!(body["count"], 19);
    let first = &body["stations"][0];
    assert_eq!(first["id"], "0-20000-0-47401");
    assert_eq!(first["headline"]["t500"], -13.7);
    assert!(first.get("offset").is_none() && first.get("distanceKm").is_none());
    assert!(body["sources"].is_array());
    // The pointer, then the index: nothing else for a list.
    let paths: Vec<String> = data.reads().into_iter().map(|(path, _)| path).collect();
    assert_eq!(
        paths,
        vec![
            "latest-sounding.json".to_owned(),
            "sounding/2026/09/14/sounding.2026091402/index.json".to_owned()
        ]
    );
}

#[test]
fn a_list_near_a_point_sorts_by_distance_and_honours_radius_and_limit() {
    let data = Data::in_memory(bucket());
    // Sapporo: 47412 is the station there, then Kushiro (47418) and
    // Wakkanai (47401), ~250 and ~265 km away.
    let body = ok(&data, "soundings", "lat=43.06&lon=141.33");
    let near = ids(&body, "stations", "id");
    assert_eq!(&near[..3], ["0-20000-0-47412", "0-20000-0-47418", "0-20000-0-47401"]);
    assert_eq!(body["stations"][0]["distanceKm"], 0.1);
    let distances: Vec<f64> = body["stations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["distanceKm"].as_f64().unwrap())
        .collect();
    assert!(distances.windows(2).all(|pair| pair[0] <= pair[1]));
    assert!((260.0..270.0).contains(&distances[2]), "{}", distances[2]);
    assert_eq!(body["request"]["lat"], 43.06);

    let within = ok(&data, "soundings", "lat=43.06&lon=141.33&radius=100");
    assert_eq!(within["count"], 1);
    let within = ok(&data, "soundings", "lat=43.06&lon=141.33&radius=260");
    assert_eq!(within["count"], 2);
    let two = ok(&data, "soundings", "lat=43.06&lon=141.33&limit=2");
    assert_eq!(two["count"], 2);
    // The whole list is in index order: a limit alone is its head.
    let head = ok(&data, "soundings", "limit=3");
    assert_eq!(
        ids(&head, "stations", "id"),
        ids(&ok(&data, "soundings", ""), "stations", "id")[..3]
    );
}

#[test]
fn a_bbox_keeps_the_stations_inside_it() {
    let data = Data::in_memory(bucket());
    // Hokkaido only: 47401, 47412, 47418.
    let body = ok(&data, "soundings", "bbox=139,41,146,46");
    assert_eq!(body["count"], 3);
    // Across the antimeridian, west > east: the whole Pacific side of Japan
    // is still inside, China (98 °E) is not.
    let body = ok(&data, "soundings", "bbox=130,-60,-150,80");
    assert!(ids(&body, "stations", "id").contains(&"0-20000-0-47401".to_owned()));
    assert!(!ids(&body, "stations", "id").contains(&"0-20000-0-52533".to_owned()));
}

#[test]
fn a_sounding_is_one_range_read_decoded_to_physical_units() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "soundings/0-20000-0-47401", "");
    assert_eq!(body["station"]["id"], "0-20000-0-47401");
    assert_eq!(body["station"]["wmo"], "47401");
    assert_eq!(body["station"]["timezone"], "Asia/Tokyo");
    let sounding = &body["soundings"][0];
    assert_eq!(sounding["time"], "2026-09-14T00:00:00Z");
    assert_eq!(sounding["n"], 15);
    let levels = &sounding["levels"];
    // 100800 Pa, 29515 K×100, 46 m/s×10, a missing height.
    assert_eq!(levels["p"][0], 1008.0);
    assert_eq!(levels["t"][0], 22.0);
    assert_eq!(levels["td"][0], 17.2);
    assert_eq!(levels["ws"][0], 4.6);
    assert_eq!(levels["wd"][0], 210);
    assert_eq!(levels["z"][0], Value::Null);
    assert_eq!(levels["z"][1], 70);
    assert_eq!(levels["sig"][0], 131072);
    assert_eq!(levels["p"].as_array().unwrap().len(), 15);
    assert_eq!(sounding["derived"]["pw"], 26.3);
    assert_eq!(body["units"]["t"]["unit"], "°C");
    assert_eq!(range_reads(&data, "soundings.jsonl"), 1);

    // The WMO number reaches the same station; the slice is served from the
    // isolate cache the second time.
    let by_wmo = ok(&data, "soundings/47401", "");
    assert_eq!(by_wmo["soundings"], body["soundings"]);
    assert_eq!(range_reads(&data, "soundings.jsonl"), 1);
}

#[test]
fn time_keeps_the_nearest_ascent() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "soundings/47401", "time=2026-09-14T05:00:00Z");
    assert_eq!(body["soundings"].as_array().unwrap().len(), 1);
    assert_eq!(body["soundings"][0]["time"], "2026-09-14T00:00:00Z");
    let error = err(&data, "soundings/47401", "time=noon");
    assert_eq!((error.status, error.code.as_str()), (400, "invalid_parameter"));
}

#[test]
fn an_unknown_station_is_404() {
    let data = Data::in_memory(bucket());
    let error = err(&data, "soundings/0-20000-0-00000", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_station"));
    let error = err(&data, "airports/XXXX", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_station"));
    let error = err(&data, "synop/amedas:00000", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_station"));
}

// ── airports ────────────────────────────────────────────────────────────

#[test]
fn the_airport_list_names_the_columns_and_filters_by_category() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "airports", "");
    assert_eq!(body["issue"], "airport.202609161440");
    assert_eq!(body["count"], 234);
    let first = &body["stations"][0];
    assert_eq!(first["icao"], "FABL");
    assert_eq!(first["t"], 23.0);
    assert_eq!(first["vis"], 10000);
    assert_eq!(first["category"], "VFR");
    assert_eq!(first["tafPresent"], false);
    assert_eq!(body["stations"][1]["tafPresent"], true);
    assert!(first.get("offset").is_none());

    let vfr = ok(&data, "airports", "category=vfr");
    assert_eq!(vfr["count"], 194);
    assert_eq!(vfr["request"]["category"], "VFR");
}

#[test]
fn an_airport_answers_its_metars_and_taf() {
    let data = Data::in_memory(bucket());
    // Lower case is accepted.
    let body = ok(&data, "airports/fact", "");
    assert_eq!(body["station"]["icao"], "FACT");
    assert_eq!(body["station"]["iata"], "CPT");
    assert_eq!(body["station"]["timezone"], "Africa/Johannesburg");
    assert_eq!(body["metars"][0]["t"], 16.0);
    assert_eq!(body["metars"][0]["cloud"], json!([["FEW", 1010]]));
    assert_eq!(body["taf"]["periods"].as_array().unwrap().len(), 4);
    assert_eq!(range_reads(&data, "history.jsonl"), 1);

    let none = ok(&data, "airports/FABL", "");
    assert_eq!(none["taf"], Value::Null);

    // KSRB holds two reports; `time=` keeps the nearer.
    let one = ok(&data, "airports/KSRB", "time=2026-09-16T14:20:00Z");
    assert_eq!(one["metars"].as_array().unwrap().len(), 1);
    assert_eq!(one["metars"][0]["time"], "2026-09-16T14:15:00Z");
}

// ── synop ───────────────────────────────────────────────────────────────

#[test]
fn the_synop_list_keys_the_values_by_element() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "synop", "");
    assert_eq!(body["issue"], "synop.202610050010");
    // amedas 14, dwd 6, hko 51 in the golden.
    assert_eq!(body["count"], 71);
    assert_eq!(body["networks"][0]["id"], "amedas");
    assert!(body["networks"][0].get("file").is_none());
    assert_eq!(body["elements"]["t"]["unit"], "°C");
    let first = &body["stations"][0];
    assert_eq!(first["id"], "amedas:11001");
    assert_eq!(first["network"], "amedas");
    assert_eq!(first["name"], "Cape Soya");
    assert_eq!(first["values"]["t"], 16.6);
    assert_eq!(first["values"]["rh"], 63);
    assert_eq!(first["values"]["p"], Value::Null);
    assert_eq!(first["values"]["vis"], Value::Null);

    let amedas = ok(&data, "synop", "network=amedas");
    assert_eq!(amedas["count"], 14);
    let error = err(&data, "synop", "network=metar");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_network"));
    assert_eq!(error.detail.unwrap()["available"], json!(["amedas", "dwd", "hko"]));
}

#[test]
fn a_synop_station_answers_in_the_point_shape() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "synop/amedas:11016", "");
    assert_eq!(body["station"]["name"], "Wakkanai");
    assert_eq!(body["station"]["names"]["ja"], "稚内");
    assert_eq!(body["station"]["network"], "amedas");
    assert_eq!(body["station"]["timezone"], "Asia/Tokyo");
    assert!(!body["network"]["attribution"].as_str().unwrap().is_empty());
    assert_eq!(body["time"]["unitSeconds"], 600);
    assert_eq!(
        body["time"]["times"],
        json!([
            "2026-10-04T23:50:00.000Z",
            "2026-10-05T00:00:00.000Z",
            "2026-10-05T00:10:00.000Z"
        ])
    );
    assert_eq!(body["variables"]["t"]["values"], json!([15.0, 15.4, 15.7]));
    assert_eq!(body["variables"]["t"]["unit"], "°C");
    assert_eq!(body["variables"]["vis"]["values"], json!([20000, 20000, 20000]));
    // An element the station never reported is present, all null.
    assert_eq!(body["variables"]["gust"]["values"], json!([null, null, null]));
    assert_eq!(
        body["variables"].as_object().unwrap().len(),
        ok(&data, "synop", "")["elements"].as_object().unwrap().len()
    );
    assert_eq!(range_reads(&data, "amedas.jsonl"), 1);

    let one = ok(&data, "synop/amedas:11016", "time=2026-10-05T00:03:00Z");
    assert_eq!(one["time"]["times"], json!(["2026-10-05T00:00:00.000Z"]));
    assert_eq!(one["variables"]["t"]["values"], json!([15.4]));
}

/// The live synop round in both index versions, whichever one the golden
/// was written in: `(v1 index, v2 index, v2 station tables by file name)`.
/// A v1 row's network column decides its table and leaves the row; a v2
/// table's rows get it back.
fn synop_versions(objects: &HashMap<String, Vec<u8>>) -> (Value, Value, Vec<(String, Value)>) {
    let dir = pointer_dir(objects, "latest-synop.json");
    let index: Value =
        serde_json::from_slice(&objects[&format!("{dir}/index.json")]).expect("index");
    if index["schemaVersion"].as_u64() == Some(1) {
        let mut v2 = index.clone();
        let mut tables = Vec::new();
        let networks = v2["networks"].as_array_mut().expect("networks");
        for (position, network) in networks.iter_mut().enumerate() {
            let id = network["id"].as_str().expect("id").to_owned();
            let rows: Vec<Value> = index["stations"]
                .as_array()
                .expect("stations")
                .iter()
                .filter(|row| row[1].as_u64() == Some(position as u64))
                .map(|row| {
                    let mut row = row.as_array().expect("row").clone();
                    row.remove(1);
                    Value::Array(row)
                })
                .collect();
            let name = format!("{id}.stations.json");
            network["stations"] = json!({
                "path": name, "byteLength": 1, "crc32": "00000000", "count": rows.len()
            });
            tables.push((
                name,
                json!({
                    "schemaVersion": 2, "network": id, "issued": index["issued"],
                    "elements": index["elements"], "stations": rows
                }),
            ));
        }
        v2["schemaVersion"] = json!(2);
        v2.as_object_mut().expect("index").remove("stations");
        return (index, v2, tables);
    }
    let mut v1 = index.clone();
    let mut tables = Vec::new();
    let mut rows = Vec::new();
    let networks = v1["networks"].as_array_mut().expect("networks");
    for (position, network) in networks.iter_mut().enumerate() {
        let name = network["stations"]["path"].as_str().expect("path").to_owned();
        let table: Value =
            serde_json::from_slice(&objects[&format!("{dir}/{name}")]).expect("table");
        for row in table["stations"].as_array().expect("stations") {
            let mut row = row.as_array().expect("row").clone();
            row.insert(1, json!(position));
            rows.push(Value::Array(row));
        }
        network.as_object_mut().expect("network").remove("stations");
        tables.push((name, table));
    }
    rows.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    v1["schemaVersion"] = json!(1);
    v1["stations"] = Value::Array(rows);
    (v1, index, tables)
}

/// The bucket with its live synop round replaced by one in the given index
/// version. `tag` keeps each test's round and crcs apart from every other
/// test's: the isolate caches immutable files by path and crc.
fn synop_bucket(version: u64, tag: u64) -> HashMap<String, Vec<u8>> {
    let mut objects = bucket();
    let live = pointer_dir(&objects, "latest-synop.json");
    let (v1, mut v2, tables) = synop_versions(&objects);
    let round = format!("synop.20261005{:02}{:02}", 10 + tag, version * 10);
    let jsonl: Vec<(String, Vec<u8>)> = objects
        .iter()
        .filter(|(key, _)| key.starts_with(&format!("{live}/")) && key.ends_with(".jsonl"))
        .map(|(key, bytes)| (key.rsplit('/').next().unwrap().to_owned(), bytes.clone()))
        .collect();
    for (name, bytes) in jsonl {
        objects.insert(format!("{round}/{name}"), bytes);
    }
    if version == 1 {
        put_json(&mut objects, &format!("{round}/index.json"), &v1);
    } else {
        for network in v2["networks"].as_array_mut().expect("networks") {
            network["stations"]["crc32"] = json!(format!("{:04x}{:04x}", 0x100 + tag, version));
        }
        put_json(&mut objects, &format!("{round}/index.json"), &v2);
        for (name, table) in &tables {
            put_json(&mut objects, &format!("{round}/{name}"), table);
        }
    }
    put_json(
        &mut objects,
        "latest-synop.json",
        &json!({
            "schemaVersion": 1, "product": "synop", "issued": v1["issued"],
            "path": format!("{round}/index.json"), "byteLength": 1,
            "crc32": format!("{tag:04x}{version:04x}")
        }),
    );
    objects
}

#[test]
fn a_v2_synop_round_answers_as_a_v1_one() {
    let v1 = Data::in_memory(synop_bucket(1, 1));
    let v2 = Data::in_memory(synop_bucket(2, 1));
    for (route, query) in [
        ("synop", ""),
        ("synop", "network=amedas"),
        ("synop", "lat=35.69&lon=139.75&limit=3"),
        ("synop/amedas:11016", ""),
        ("synop/amedas:50066", "time=2026-10-05T00:03:00Z"),
    ] {
        let mut one = ok(&v1, route, query);
        let mut two = ok(&v2, route, query);
        // The round names differ by construction; nothing else may.
        one["issue"] = Value::Null;
        two["issue"] = Value::Null;
        assert_eq!(one, two, "{route}?{query}");
    }
    let body = ok(&v2, "synop", "");
    assert!(body["networks"][0].get("stations").is_none());
    assert!(body["networks"][0].get("file").is_none());
    assert_eq!(body["stations"][0]["network"], "amedas");
    assert_eq!(err(&v2, "synop/amedas:00000", "").code, "unknown_station");
    assert_eq!(err(&v2, "synop/metar:RJTT", "").code, "unknown_station");
    let paths: Vec<String> = v2.reads().into_iter().map(|(path, _)| path).collect();
    assert!(paths.iter().any(|path| path.ends_with("/amedas.stations.json")));
}

#[test]
fn the_element_table_knows_the_dew_point() {
    let mut objects = synop_bucket(2, 2);
    let round = pointer_dir(&objects, "latest-synop.json");
    let mut index: Value =
        serde_json::from_slice(&objects[&format!("{round}/index.json")]).expect("index");
    let has_td = index["elements"]
        .as_array()
        .unwrap()
        .iter()
        .any(|key| key.as_str() == Some("td"));
    if !has_td {
        // A golden from before `td`: widen it the way v2 does, second.
        index["elements"].as_array_mut().unwrap().insert(1, json!("td"));
        let elements = index["elements"].clone();
        for network in index["networks"].as_array().unwrap().clone() {
            let name = network["stations"]["path"].as_str().unwrap().to_owned();
            let key = format!("{round}/{name}");
            let mut table: Value = serde_json::from_slice(&objects[&key]).expect("table");
            table["elements"] = elements.clone();
            for row in table["stations"].as_array_mut().unwrap() {
                row.as_array_mut().unwrap().insert(8, json!(-3.4));
            }
            put_json(&mut objects, &key, &table);
        }
        put_json(&mut objects, &format!("{round}/index.json"), &index);
    }
    let data = Data::in_memory(objects);
    let body = ok(&data, "synop", "");
    assert_eq!(body["elements"]["td"]["label"], "dew point");
    assert_eq!(body["elements"]["td"]["unit"], "°C");
    assert!(body["stations"][0]["values"].get("td").is_some());
    let station = ok(&data, "synop/amedas:11016", "");
    assert_eq!(station["variables"]["td"]["unit"], "°C");
}

#[test]
fn a_v2_network_whose_table_fails_is_left_out() {
    let mut objects = synop_bucket(2, 3);
    let round = pointer_dir(&objects, "latest-synop.json");
    let key = format!("{round}/amedas.stations.json");
    let mut table: Value = serde_json::from_slice(&objects[&key]).expect("table");
    table["network"] = json!("other");
    put_json(&mut objects, &key, &table);
    let data = Data::in_memory(objects);
    let body = ok(&data, "synop", "");
    let ids: Vec<&str> = body["networks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|network| network["id"].as_str().unwrap())
        .collect();
    assert!(!ids.contains(&"amedas"));
    assert!(body["stations"]
        .as_array()
        .unwrap()
        .iter()
        .all(|station| !station["id"].as_str().unwrap().starts_with("amedas:")));
    let error = err(&data, "synop/amedas:11016", "");
    assert_eq!((error.status, error.code.as_str()), (502, "upstream_failed"));
}

// ── element tables ──────────────────────────────────────────────────────

/// The live round's directory and index of a product.
fn live_index(objects: &HashMap<String, Vec<u8>>, product: &str) -> (String, Value) {
    let dir = pointer_dir(objects, &format!("latest-{product}.json"));
    let index = serde_json::from_slice(&objects[&format!("{dir}/index.json")]).expect("index");
    (dir, index)
}

/// Replace the live index of a product, with a pointer crc no other test
/// uses: the isolate holds an index by its pointer's crc.
fn replace_index(objects: &mut HashMap<String, Vec<u8>>, product: &str, index: &Value, crc32: &str) {
    let dir = pointer_dir(objects, &format!("latest-{product}.json"));
    put_json(objects, &format!("{dir}/index.json"), index);
    let pointer = format!("latest-{product}.json");
    let mut held: Value = serde_json::from_slice(&objects[&pointer]).expect("pointer");
    held["crc32"] = json!(crc32);
    put_json(objects, &pointer, &held);
}

#[test]
fn an_element_table_in_the_index_gives_the_units() {
    for (product, element, fallback, crc32) in
        [("synop", "t", "°C", "e1e00001"), ("aq", "pm25", "µg/m³", "e1e00002")]
    {
        // Without a table (or with this reader's own), the reader's unit.
        let data = Data::in_memory(bucket());
        let body = ok(&data, product, "");
        let entry = &body["elements"][element];
        assert!(entry.get("cell").is_some() && entry.get("period").is_some());
        let (_, index) = live_index(&bucket(), product);
        if index.get("elementTable").is_none() {
            assert_eq!(entry["unit"], fallback, "{product}");
            assert_eq!(entry["cell"], Value::Null);
        } else {
            assert_eq!(entry["unit"], index["elementTable"][element]["unit"]);
        }

        // A table's unit, cell method and period win and are passed through.
        let mut objects = bucket();
        let (_, mut index) = live_index(&objects, product);
        let mut table = serde_json::Map::new();
        for key in index["elements"].as_array().unwrap() {
            table.insert(
                key.as_str().unwrap().to_owned(),
                json!({ "unit": format!("u-{}", key.as_str().unwrap()), "cell": "mean", "period": "network" }),
            );
        }
        table.insert(element.to_owned(), json!({ "unit": "X", "cell": "point", "period": null }));
        index["elementTable"] = Value::Object(table);
        replace_index(&mut objects, product, &index, crc32);
        let data = Data::in_memory(objects);
        let body = ok(&data, product, "");
        assert_eq!(
            body["elements"][element],
            json!({ "label": entry["label"], "unit": "X", "cell": "point", "period": null })
        );
        let other = index["elements"]
            .as_array()
            .unwrap()
            .iter()
            .map(|key| key.as_str().unwrap())
            .find(|key| *key != element)
            .unwrap();
        assert_eq!(body["elements"][other]["period"], "network");
        let station = body["stations"][0]["id"].as_str().unwrap().to_owned();
        let record = ok(&data, &format!("{product}/{station}"), "");
        assert_eq!(record["variables"][element]["unit"], "X");
        assert_eq!(record["variables"][element]["cell"], "point");
    }
}

// ── aq ──────────────────────────────────────────────────────────────────

/// The live aq round's station tables, in network order.
fn aq_tables(objects: &HashMap<String, Vec<u8>>) -> Vec<Value> {
    let (dir, index) = live_index(objects, "aq");
    index["networks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|network| {
            let path = network["stations"]["path"].as_str().unwrap();
            serde_json::from_slice(&objects[&format!("{dir}/{path}")]).expect("table")
        })
        .collect()
}

#[test]
fn the_aq_list_keys_the_values_by_element_and_names_the_agency() {
    let objects = bucket();
    let (_, index) = live_index(&objects, "aq");
    let tables = aq_tables(&objects);
    let data = Data::in_memory(objects);
    let body = ok(&data, "aq", "");
    assert_eq!(body["product"], "aq");
    let rows: Vec<&Value> = tables
        .iter()
        .flat_map(|table| table["stations"].as_array().unwrap())
        .collect();
    assert_eq!(body["count"], rows.len());
    let mut expected: Vec<String> = rows.iter().map(|row| row[0].as_str().unwrap().to_owned()).collect();
    expected.sort();
    assert_eq!(ids(&body, "stations", "id"), expected);
    let networks: Vec<&str> = index["networks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|network| network["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        ids(&body, "networks", "id"),
        networks.iter().map(|id| id.to_string()).collect::<Vec<_>>()
    );
    assert!(body["networks"][0].get("file").is_none());
    assert!(body["networks"][0].get("stations").is_none());
    assert!(body["networks"][0]["avgPeriod"].is_number());
    assert_eq!(
        body["elements"].as_object().unwrap().keys().cloned().collect::<Vec<_>>().len(),
        index["elements"].as_array().unwrap().len()
    );

    // Every station is its table row, named: the agency in its own column,
    // the values after it.
    let elements = index["elements"].as_array().unwrap();
    for station in body["stations"].as_array().unwrap() {
        let row = rows.iter().find(|row| row[0] == station["id"]).unwrap();
        assert_eq!(station["lat"], row[1]);
        assert_eq!(station["name"], row[5]);
        assert_eq!(station["agency"], row[6]);
        assert_eq!(station["obsTime"], row[7]);
        for (offset, key) in elements.iter().enumerate() {
            assert_eq!(station["values"][key.as_str().unwrap()], row[8 + offset]);
        }
        let network = station["id"].as_str().unwrap().split_once(':').unwrap().0;
        assert_eq!(station["network"], network);
    }

    let network = networks[networks.len() - 1];
    let one = ok(&data, "aq", &format!("network={network}"));
    assert!(ids(&one, "stations", "id").iter().all(|id| id.starts_with(&format!("{network}:"))));
    assert_eq!(one["networks"].as_array().unwrap().len(), 1);
    let error = err(&data, "aq", "network=amedas");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_network"));
    assert_eq!(error.detail.unwrap()["available"], json!(networks));
}

#[test]
fn an_aq_station_answers_in_the_point_shape() {
    let objects = bucket();
    let (dir, index) = live_index(&objects, "aq");
    let tables = aq_tables(&objects);
    for (network, table) in index["networks"].as_array().unwrap().iter().zip(&tables) {
        let row = &table["stations"][0];
        let id = row[0].as_str().unwrap();
        let width = row.as_array().unwrap().len();
        let (offset, length) = (
            row[width - 2].as_u64().unwrap() as usize,
            row[width - 1].as_u64().unwrap() as usize,
        );
        let file = &objects[&format!("{dir}/{}", network["file"]["path"].as_str().unwrap())];
        let line: Value = serde_json::from_slice(&file[offset..offset + length]).expect("line");

        let data = Data::in_memory(bucket());
        let body = ok(&data, &format!("aq/{id}"), "");
        assert_eq!(body["product"], "aq");
        assert_eq!(body["station"]["id"], id);
        assert_eq!(body["station"]["network"], network["id"]);
        assert_eq!(body["station"]["agency"], line["agency"]);
        assert_eq!(body["station"]["names"], line["names"]);
        assert!(body["station"]["timezone"].is_string());
        assert!(body["network"].get("file").is_none());
        assert_eq!(body["time"]["unitSeconds"], network["cadence"]);
        let times = line["time"].as_array().unwrap();
        assert_eq!(body["time"]["times"].as_array().unwrap().len(), times.len());
        let variables = body["variables"].as_object().unwrap();
        assert_eq!(variables.len(), index["elements"].as_array().unwrap().len());
        for (key, variable) in variables {
            let values = variable["values"].as_array().unwrap();
            assert_eq!(values.len(), times.len(), "{key}");
            match line["obs"].get(key) {
                Some(series) => assert_eq!(&variable["values"], series, "{key}"),
                None => assert!(values.iter().all(Value::is_null), "{key}"),
            }
        }
        assert_eq!(range_reads(&data, network["file"]["path"].as_str().unwrap()), 1);

        // `time=` keeps the nearest hour.
        let last = times.last().unwrap().as_f64().unwrap();
        let at = xue::zarr::format_iso_millis(last * 1000.0 - 60_000.0);
        let one = ok(&data, &format!("aq/{id}"), &format!("time={at}"));
        assert_eq!(
            one["time"]["times"],
            json!([xue::zarr::format_iso_millis(last * 1000.0)])
        );
    }
    let data = Data::in_memory(bucket());
    let error = err(&data, "aq/airnow:000000000", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_station"));
    assert_eq!(err(&data, "aq/amedas:11016", "").code, "unknown_station");
}

/// The id of the newest round before the live one, flat or filed under
/// its day.
fn earlier_round(objects: &HashMap<String, Vec<u8>>, product: &str) -> String {
    let live = pointer_dir(objects, &format!("latest-{product}.json"));
    let live = live.rsplit('/').next().unwrap().to_owned();
    let mut rounds: Vec<String> = objects
        .keys()
        .filter_map(|key| key.strip_suffix("/index.json"))
        .map(|dir| dir.rsplit('/').next().unwrap().to_owned())
        .filter(|round| round.starts_with(&format!("{product}.")) && *round != live)
        .collect();
    rounds.sort();
    rounds.pop().unwrap_or_else(|| panic!("an earlier {product} round"))
}

#[test]
fn an_aq_round_can_be_pinned() {
    let objects = bucket();
    let round = earlier_round(&objects, "aq");
    let data = Data::in_memory(objects);
    assert_eq!(ok(&data, "aq", &format!("issue={round}"))["issue"], json!(round));
}

// ── alerts ──────────────────────────────────────────────────────────────

/// The live alerts round's rows.
fn alert_rows(objects: &HashMap<String, Vec<u8>>) -> (Value, Vec<Value>) {
    let (_, index) = live_index(objects, "alerts");
    let rows = index["alerts"].as_array().unwrap().clone();
    (index, rows)
}

/// One alert's line, sliced out of its network's file by its row.
fn alert_line(objects: &HashMap<String, Vec<u8>>, index: &Value, row: &Value) -> Value {
    let dir = pointer_dir(objects, "latest-alerts.json");
    let network = &index["networks"][row[1].as_u64().unwrap() as usize];
    let file = &objects[&format!("{dir}/{}", network["file"]["path"].as_str().unwrap())];
    let (offset, length) = (row[12].as_u64().unwrap() as usize, row[13].as_u64().unwrap() as usize);
    serde_json::from_slice(&file[offset..offset + length]).expect("line")
}

#[test]
fn the_alert_list_is_the_index_rows_named() {
    let objects = bucket();
    let (index, rows) = alert_rows(&objects);
    let data = Data::in_memory(objects);
    let body = ok(&data, "alerts", "");
    assert_eq!(body["product"], "alerts");
    assert_eq!(body["count"], rows.len());
    assert_eq!(body["categories"], index["categories"]);
    let expected: Vec<String> = rows.iter().map(|row| row[0].as_str().unwrap().to_owned()).collect();
    assert_eq!(ids(&body, "alerts", "id"), expected);
    for (alert, row) in body["alerts"].as_array().unwrap().iter().zip(&rows) {
        let network = &index["networks"][row[1].as_u64().unwrap() as usize];
        assert_eq!(alert["network"], network["id"]);
        assert_eq!(alert["category"], row[2]);
        assert_eq!(alert["level"], row[3]);
        assert_eq!(alert["event"], row[4]);
        assert_eq!(alert["expires"], row[7]);
        if row[8].is_null() {
            assert_eq!(alert["bbox"], Value::Null);
        } else {
            assert_eq!(alert["bbox"], json!([row[8], row[9], row[10], row[11]]));
        }
        assert!(alert.get("offset").is_none());
    }
    for network in body["networks"].as_array().unwrap() {
        assert!(network.get("file").is_none());
        // A boundary set's path is under the data root, not the index.
        if let Some(path) = network["areas"]["path"].as_str() {
            assert!(path.starts_with("alerts-areas/") && path.ends_with(".pmtiles"), "{path}");
        }
    }
    let ended = body["ended"].as_array().unwrap();
    assert_eq!(ended.len(), index["ended"].as_array().unwrap().len());
    for (item, raw) in ended.iter().zip(index["ended"].as_array().unwrap()) {
        let raw_id = raw.get(0).or_else(|| raw.get("id")).unwrap();
        assert_eq!(&item["id"], raw_id);
        assert!(item["network"].is_string());
        assert!(item["reason"].is_string());
        assert!(item.get("by").is_some());
    }
    assert!(body["sources"].is_array());
    // The pointer and the index: a list without a point reads no line.
    assert_eq!(data.reads().len(), 2);
}

#[test]
fn the_alert_list_filters_by_network_category_and_level() {
    let objects = bucket();
    let (index, rows) = alert_rows(&objects);
    let data = Data::in_memory(objects);
    let networks: Vec<&str> = index["networks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|network| network["id"].as_str().unwrap())
        .collect();
    for (position, network) in networks.iter().enumerate() {
        let body = ok(&data, "alerts", &format!("network={network}"));
        let expected = rows.iter().filter(|row| row[1].as_u64() == Some(position as u64)).count();
        assert_eq!(body["count"], expected, "{network}");
        assert!(body["ended"]
            .as_array()
            .unwrap()
            .iter()
            .all(|item| item["network"] == *network));
        assert_eq!(body["networks"].as_array().unwrap().len(), 1);
    }
    let category = rows[0][2].as_str().unwrap();
    let body = ok(&data, "alerts", &format!("category={category}"));
    assert_eq!(body["count"], rows.iter().filter(|row| row[2] == category).count());
    for level in 1..=5u64 {
        let body = ok(&data, "alerts", &format!("level={level}"));
        let expected = rows.iter().filter(|row| row[3].as_u64().unwrap() >= level).count();
        assert_eq!(body["count"], expected, "level {level}");
        assert_eq!(body["request"]["level"], level);
    }
    assert_eq!(ok(&data, "alerts", "limit=1")["count"], 1.min(rows.len()));

    for query in ["level=0", "level=6", "level=high", "category=weather", "lat=35&lon=140&radius=10"] {
        let error = err(&data, "alerts", query);
        assert_eq!((error.status, error.code.as_str()), (400, "invalid_parameter"), "{query}");
    }
    let error = err(&data, "alerts", "network=metar");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_network"));
    assert_eq!(error.detail.unwrap()["available"], json!(networks));
}

#[test]
fn a_bbox_keeps_the_alerts_whose_box_meets_it() {
    let objects = bucket();
    let (_, rows) = alert_rows(&objects);
    let data = Data::in_memory(objects);
    let boxed: Vec<&Value> = rows.iter().filter(|row| !row[8].is_null()).collect();
    let row = boxed.first().expect("an alert with a box");
    let query = format!("bbox={},{},{},{}", row[8], row[9], row[10], row[11]);
    let body = ok(&data, "alerts", &query);
    assert!(ids(&body, "alerts", "id").contains(&row[0].as_str().unwrap().to_owned()));
    // An alert with no box is placed nowhere.
    assert!(body["alerts"].as_array().unwrap().iter().all(|alert| !alert["bbox"].is_null()));
    // The whole world holds every boxed alert.
    let world = ok(&data, "alerts", "bbox=-180,-90,180,90");
    assert_eq!(world["count"], boxed.len());
    // Across the antimeridian (west > east) is a valid box.
    assert!(ok(&data, "alerts", "bbox=170,-10,-170,10")["count"].is_u64());
}

#[test]
fn a_point_is_tested_against_the_polygon_or_else_the_box() {
    let objects = bucket();
    let (index, rows) = alert_rows(&objects);
    // An alert with its own polygon: find a point inside it and one in its
    // box but outside it, on a grid over the box.
    let drawn = rows
        .iter()
        .find(|row| !row[8].is_null() && !alert_line(&objects, &index, row)["geometry"].is_null())
        .expect("an alert with a polygon");
    let geometry = alert_line(&objects, &index, drawn)["geometry"].clone();
    let [west, south, east, north] =
        [8, 9, 10, 11].map(|item| drawn[item].as_f64().unwrap());
    let mut inside = None;
    let mut outside = None;
    for i in 1..40 {
        for j in 1..40 {
            let lat = south + (north - south) * f64::from(i) / 40.0;
            let lon = west + (east - west) * f64::from(j) / 40.0;
            if alerts::geometry_contains(&geometry, lat, lon) {
                inside.get_or_insert((lat, lon));
            } else {
                outside.get_or_insert((lat, lon));
            }
        }
    }
    let id = drawn[0].as_str().unwrap().to_owned();
    let data = Data::in_memory(bucket());
    let (lat, lon) = inside.expect("a point inside the polygon");
    let body = ok(&data, "alerts", &format!("lat={lat}&lon={lon}"));
    assert!(ids(&body, "alerts", "id").contains(&id));
    assert_eq!(body["request"]["lat"], lat);
    if let Some((lat, lon)) = outside {
        let body = ok(&data, "alerts", &format!("lat={lat}&lon={lon}"));
        assert!(!ids(&body, "alerts", "id").contains(&id));
    }

    // An alert drawn by area codes alone stands on its box: give one a box.
    let mut objects = bucket();
    let (_, mut index) = live_index(&objects, "alerts");
    let position = rows
        .iter()
        .position(|row| alert_line(&objects, &index, row)["geometry"].is_null())
        .expect("an area-coded alert");
    let coded = rows[position][0].as_str().unwrap().to_owned();
    for (item, value) in [(8, 10.0), (9, 10.0), (10, 11.0), (11, 11.0)] {
        index["alerts"][position][item] = json!(value);
    }
    replace_index(&mut objects, "alerts", &index, "a1e00001");
    let data = Data::in_memory(objects);
    let body = ok(&data, "alerts", "lat=10.5&lon=10.5");
    assert_eq!(ids(&body, "alerts", "id"), vec![coded.clone()]);
    assert_eq!(ok(&data, "alerts", "lat=12&lon=10.5")["count"], 0);
}

#[test]
fn an_alert_is_its_line_with_its_box() {
    let objects = bucket();
    let (index, rows) = alert_rows(&objects);
    let data = Data::in_memory(bucket());
    for row in &rows {
        let id = row[0].as_str().unwrap();
        let body = ok(&data, &format!("alerts/{id}"), "");
        let mut line = alert_line(&objects, &index, row);
        line["bbox"] = if row[8].is_null() {
            Value::Null
        } else {
            json!([row[8], row[9], row[10], row[11]])
        };
        assert_eq!(body["alert"], line, "{id}");
        let network = &index["networks"][row[1].as_u64().unwrap() as usize];
        assert_eq!(body["network"]["id"], network["id"]);
        assert!(body["network"].get("file").is_none());
    }
    let reads = data.reads().iter().filter(|(_, kind)| *kind == "range").count();
    assert_eq!(reads, rows.len());

    let error = err(&data, "alerts/nws:urn:oid:0", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_alert"));
    assert!(error.detail.is_none());
    // An alert that left with this round is still 404, and says how it left.
    if let Some(ended) = index["ended"].as_array().and_then(|items| items.first()) {
        let id = ended.get(0).or_else(|| ended.get("id")).unwrap().as_str().unwrap();
        let error = err(&data, &format!("alerts/{id}"), "");
        assert_eq!(error.code, "unknown_alert");
        assert_eq!(error.detail.unwrap()["ended"]["id"], id);
    }
}

#[test]
fn an_alert_replaced_under_a_new_id_names_its_successor() {
    let mut objects = bucket();
    let (_, mut index) = live_index(&objects, "alerts");
    let by = index["alerts"][0][0].clone();
    index["ended"] = json!([
        ["nws:old.1", 0, "replaced", by],
        ["nws:old.2", 0, "gone"],
        { "id": "nws:old.3", "network": 0, "reason": "replaced", "by": by }
    ]);
    replace_index(&mut objects, "alerts", &index, "a1e00002");
    let data = Data::in_memory(objects);
    let body = ok(&data, "alerts", "");
    let network = index["networks"][0]["id"].clone();
    assert_eq!(
        body["ended"],
        json!([
            { "id": "nws:old.1", "network": network, "reason": "replaced", "by": by },
            { "id": "nws:old.2", "network": network, "reason": "gone", "by": null },
            { "id": "nws:old.3", "network": network, "reason": "replaced", "by": by }
        ])
    );
    let error = err(&data, "alerts/nws:old.1", "");
    assert_eq!(error.detail.unwrap()["ended"]["by"], by);
}

#[test]
fn an_alerts_round_can_be_pinned_and_a_newer_index_is_refused() {
    let objects = bucket();
    let round = earlier_round(&objects, "alerts");
    let data = Data::in_memory(objects);
    assert_eq!(ok(&data, "alerts", &format!("issue={round}"))["issue"], json!(round));

    let mut objects = bucket();
    let (_, mut index) = live_index(&objects, "alerts");
    index["schemaVersion"] = json!(2);
    replace_index(&mut objects, "alerts", &index, "a1e00003");
    let error = err(&Data::in_memory(objects), "alerts", "");
    assert_eq!((error.status, error.code.as_str()), (502, "upstream_failed"));
}

// ── storms ──────────────────────────────────────────────────────────────

#[test]
fn the_storm_list_filters_by_basin_and_level() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "storms", "");
    assert_eq!(body["issue"], "tc.2026091206");
    assert_eq!(body["count"], 3);
    assert_eq!(body["storms"][0]["id"], "EP142026");
    assert_eq!(body["storms"][0]["name"], "NORBERT");
    assert!(body["storms"][0].get("path").is_none());
    assert_eq!(body["crosswalk"], json!({}));

    assert_eq!(ok(&data, "storms", "basin=ep")["count"], 2);
    assert_eq!(ok(&data, "storms", "level=B")["count"], 2);
    assert_eq!(ok(&data, "storms", "basin=WP&level=A")["count"], 0);
}

#[test]
fn a_storm_unpacks_its_ensembles_into_member_tracks() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "storms/EP142026", "");
    assert_eq!(body["id"], "EP142026");
    assert_eq!(body["resolvedFrom"], Value::Null);
    assert!(body["best"]["usa"]["source"].is_string());
    assert!(body["agencies"]["nhc"]["points"].is_array());
    // A deterministic model is passed through.
    assert!(body["models"]["gfs"]["points"].is_array());
    // GEFS: 1690 → 16.9, 216 → 21.6, 9960 → 996, at base + lead.
    let gefs = &body["models"]["gefs"];
    assert_eq!(gefs["leads"][1], 21600);
    let control = &gefs["members"][0];
    assert_eq!(control["member"], 0);
    assert_eq!(control["points"][0]["lead"], 0);
    assert_eq!(control["points"][0]["time"], "2026-09-12T00:00:00.000Z");
    assert_eq!(control["points"][1]["time"], "2026-09-12T06:00:00.000Z");
    assert_eq!(control["points"][0]["lat"], 16.9);
    assert_eq!(control["points"][0]["vmax"], 21.6);
    assert_eq!(control["points"][0]["pmin"], 996.0);
    assert!(gefs.get("lat").is_none(), "the flat arrays are unpacked");

    // Against the raw file: every member track is the raw arrays with the
    // missing positions left out.
    let raw: Value = serde_json::from_slice(
        &std::fs::read(fixtures().join("tc/expected/EP142026.json")).unwrap(),
    )
    .unwrap();
    let ens = &raw["models"]["ecmwfens"];
    let width = ens["leads"].as_array().unwrap().len();
    let members = ens["members"].as_array().unwrap();
    let got = body["models"]["ecmwfens"]["members"].as_array().unwrap();
    assert_eq!(got.len(), members.len());
    for (position, member) in members.iter().enumerate() {
        let expected: Vec<f64> = (0..width)
            .filter_map(|lead| {
                let lat = ens["lat"][position * width + lead].as_i64().unwrap();
                let lon = ens["lon"][position * width + lead].as_i64().unwrap();
                (lat != -32768 && lon != -32768).then_some(lat as f64 / 100.0)
            })
            .collect();
        assert_eq!(got[position]["member"], *member);
        let points: Vec<f64> = got[position]["points"]
            .as_array()
            .unwrap()
            .iter()
            .map(|point| point["lat"].as_f64().unwrap())
            .collect();
        assert_eq!(points, expected, "member {member}");
    }
}

#[test]
fn include_narrows_the_storm_document() {
    let data = Data::in_memory(bucket());
    let body = ok(&data, "storms/EP142026", "include=best,alert");
    assert!(body.get("best").is_some());
    assert!(body.get("alert").is_some());
    assert!(body.get("models").is_none());
    assert!(body.get("agencies").is_none());
    let error = err(&data, "storms/EP142026", "include=tracks");
    assert_eq!((error.status, error.code.as_str()), (400, "invalid_parameter"));
}

#[test]
fn a_storm_is_found_by_alias_and_through_the_crosswalk() {
    let mut objects = bucket();
    let key = "tc/2026/09/12/tc.2026091206/index.json";
    let mut index: Value = serde_json::from_slice(&objects[key]).unwrap();
    index["crosswalk"] = json!({ "x-ep-2026090100-9": "EP142026" });
    put_json(&mut objects, key, &index);
    let data = Data::in_memory(objects);

    let by_alias = ok(&data, "storms/ep97", "");
    assert_eq!(by_alias["id"], "x-ep-2026091012-1");
    assert_eq!(by_alias["resolvedFrom"], "ep97");

    let by_crosswalk = ok(&data, "storms/x-ep-2026090100-9", "");
    assert_eq!(by_crosswalk["id"], "EP142026");
    assert_eq!(by_crosswalk["resolvedFrom"], "x-ep-2026090100-9");

    // Case does not matter for an id either, and is not a resolution.
    let lower = ok(&data, "storms/ep142026", "include=best");
    assert_eq!(lower["id"], "EP142026");
    assert_eq!(lower["resolvedFrom"], Value::Null);

    let error = err(&data, "storms/AL992099", "");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_storm"));
    assert!(error.detail.unwrap()["available"].as_array().unwrap().len() == 3);
}

// ── issues, pins and the chain ──────────────────────────────────────────

#[test]
fn issue_pins_a_round_and_the_dated_tree_is_derived_from_the_id() {
    let data = Data::in_memory(bucket());
    let live = ok(&data, "airports", "");
    assert_eq!(live["issue"], "airport.202609161440");
    let pinned = ok(&data, "airports", "issue=airport.202609161430");
    assert_eq!(pinned["issue"], "airport.202609161430");
    assert_eq!(pinned["issued"], "2026-09-16T14:30:00Z");
    let paths: Vec<String> = data.reads().into_iter().map(|(path, _)| path).collect();
    assert!(paths.iter().any(|path| path.ends_with("airport.202609161430/index.json")));
    assert_eq!(
        ok(&data, "synop/amedas:11016", "issue=synop.202610050000")["issue"],
        "synop.202610050000"
    );

    // An hourly product's issue sits under its UTC day.
    let sounding = ok(&data, "soundings", "issue=sounding.2026091402");
    assert_eq!(sounding["count"], 19);
    let paths: Vec<String> = data.reads().into_iter().map(|(path, _)| path).collect();
    assert!(paths.contains(&"sounding/2026/09/14/sounding.2026091402/index.json".to_owned()));
    assert!(!paths.contains(&"sounding.2026091402/index.json".to_owned()));

    for query in ["issue=airport.1", "issue=gfs.2026091614", "issue=airport.2026091614xx"] {
        let error = err(&data, "airports", query);
        assert_eq!(
            (error.status, error.code.as_str()),
            (400, "invalid_parameter"),
            "{query}"
        );
    }
    let error = err(&data, "airports", "issue=airport.202601010000");
    assert_eq!((error.status, error.code.as_str()), (404, "unknown_issue"));
}

#[test]
fn a_product_without_a_pointer_is_404_no_live_run() {
    let mut objects = bucket();
    objects.remove("latest-tc.json");
    let data = Data::in_memory(objects);
    let error = err(&data, "storms", "");
    assert_eq!((error.status, error.code.as_str()), (404, "no_live_run"));
}

#[test]
fn bad_spatial_parameters_are_400() {
    let data = Data::in_memory(bucket());
    for query in [
        "lat=35",
        "lon=140",
        "lat=91&lon=140",
        "lat=35&lon=140&radius=0",
        "radius=100",
        "lat=35&lon=140&limit=0",
        "bbox=1,2,3",
        "bbox=0,50,10,40",
    ] {
        let error = err(&data, "airports", query);
        assert_eq!(
            (error.status, error.code.as_str()),
            (400, "invalid_parameter"),
            "{query}"
        );
    }
}

#[test]
fn the_grid_route_turns_a_point_product_away_with_the_right_route() {
    let data = Data::in_memory(bucket());
    let url = Url::parse("https://api.example/v1/point?source=sounding&lat=35&lon=140").unwrap();
    let error = block_on(read_point(&data, &parse_query(&url))).expect_err("not a grid");
    assert_eq!((error.status, error.code.as_str()), (400, "not_a_grid"));
    assert_eq!(error.detail.unwrap()["endpoint"], "/v1/soundings");
}

#[test]
fn a_source_summary_says_which_route_reads_it() {
    let mut objects = bucket();
    put_json(
        &mut objects,
        "gfs/collection.json",
        &json!({ "title": "NOAA GFS", "xue:pointer": "latest.json" }),
    );
    let data = Data::in_memory(objects);
    let sounding = block_on(read_source(&data, "sounding")).expect("summary");
    assert_eq!(sounding["kind"], "point");
    assert_eq!(sounding["endpoint"], "/v1/soundings");
    for (product, endpoint) in [("synop", "/v1/synop"), ("aq", "/v1/aq"), ("alerts", "/v1/alerts")] {
        let summary = block_on(read_source(&data, product)).expect("summary");
        assert_eq!(summary["kind"], "point", "{product}");
        assert_eq!(summary["endpoint"], endpoint, "{product}");
    }
    let gfs = block_on(read_source(&data, "gfs")).expect("summary");
    assert_eq!(gfs["kind"], "grid");
    assert_eq!(gfs["endpoint"], "/v1/point");
}
