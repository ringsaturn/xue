//! The point-product handlers over the products' committed goldens
//! (`tests/fixtures/{sounding,airport,synop,tc}/expected/`), laid out in an
//! in-memory bucket the way the publishers lay them out on R2: the pointer
//! at the root, the issue under the directory the pointer names.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use futures_executor::block_on;
use serde_json::{json, Value};
use url::Url;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::point::{parse_query, read_point};
use crate::source::read_source;
use crate::{airport, sounding, synop, tc};

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

/// All four products, live, plus the previous airport and synop rounds for
/// the `issue=` pin, and a Collection per product as the catalog publishes.
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

    let airport = root.join("airport/expected");
    put(&mut objects, "latest-airport.json", &airport.join("latest-airport.json"));
    for round in ["airport.202609161430", "airport.202609161440"] {
        for file in ["index.json", "history.jsonl"] {
            put(&mut objects, &format!("{round}/{file}"), &airport.join(round).join(file));
        }
    }

    let synop = root.join("synop/expected");
    put(&mut objects, "latest-synop.json", &synop.join("latest-synop.json"));
    for round in ["synop.202610050000", "synop.202610050010"] {
        for file in ["index.json", "amedas.jsonl"] {
            put(&mut objects, &format!("{round}/{file}"), &synop.join(round).join(file));
        }
    }

    for product in ["sounding", "airport", "synop", "tc"] {
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
        "synop" => |data, url| block_on(synop::list(data, url)),
        _ => |data, url| block_on(tc::list(data, url)),
    }
}

fn station(product: &str, id: &str, data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    match product {
        "soundings" => block_on(sounding::station(data, id, url)),
        "airports" => block_on(airport::station(data, id, url)),
        "synop" => block_on(synop::station(data, id, url)),
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
    assert_eq!(body["count"], 14);
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
    assert_eq!(error.detail.unwrap()["available"], json!(["amedas"]));
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
    assert_eq!(body["variables"].as_object().unwrap().len(), 12);
    assert_eq!(range_reads(&data, "amedas.jsonl"), 1);

    let one = ok(&data, "synop/amedas:11016", "time=2026-10-05T00:03:00Z");
    assert_eq!(one["time"]["times"], json!(["2026-10-05T00:00:00.000Z"]));
    assert_eq!(one["variables"]["t"]["values"], json!([15.4]));
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
    assert!(paths.contains(&"airport.202609161430/index.json".to_owned()));
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
    let gfs = block_on(read_source(&data, "gfs")).expect("summary");
    assert_eq!(gfs["kind"], "grid");
    assert_eq!(gfs["endpoint"], "/v1/point");
}
