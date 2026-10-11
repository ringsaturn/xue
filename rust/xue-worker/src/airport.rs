//! The airport reports (`docs/airport.md`): the index's compact rows as
//! objects, and one station's 24 h of METARs and current TAF sliced out of
//! `history.jsonl`.

use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, nearest, round_km, text, Issue, Query, Spatial};
use crate::timezone;

const PRODUCT: &str = "airport";

/// The index row's columns, in the contract's order; the last two are the
/// byte span and stay out of the response.
const COLUMNS: [&str; 16] = [
    "icao",
    "lat",
    "lon",
    "elev",
    "obsTime",
    "t",
    "td",
    "wd",
    "ws",
    "gust",
    "vis",
    "qnh",
    "category",
    "tafPresent",
    "offset",
    "length",
];
const OFFSET: usize = 14;

pub fn units() -> Value {
    json!({
        "t": { "label": "air temperature", "unit": "°C" },
        "td": { "label": "dew-point temperature", "unit": "°C" },
        "wd": { "label": "wind direction (from); null when variable", "unit": "°" },
        "ws": { "label": "wind speed", "unit": "m/s" },
        "gust": { "label": "gust", "unit": "m/s" },
        "vis": { "label": "visibility; 10000 means at least 10 km", "unit": "m" },
        "qnh": { "label": "altimeter setting", "unit": "hPa" },
        "slp": { "label": "sea-level pressure", "unit": "hPa" },
        "cloud": { "label": "[cover, base above ground] layers", "unit": "m" },
        "elev": { "label": "elevation", "unit": "m" },
    })
}

fn column(row: &Value, name: &str) -> Value {
    COLUMNS
        .iter()
        .position(|held| *held == name)
        .and_then(|index| row.get(index))
        .cloned()
        .unwrap_or(Value::Null)
}

fn position(row: &Value) -> Option<(f64, f64)> {
    Some((row.get(1)?.as_f64()?, row.get(2)?.as_f64()?))
}

fn station_json(row: &Value, distance: Option<f64>) -> Value {
    let mut station = Map::new();
    for name in &COLUMNS[..OFFSET] {
        let value = column(row, name);
        let value = if *name == "tafPresent" {
            json!(value.as_i64() == Some(1))
        } else {
            value
        };
        station.insert((*name).to_owned(), value);
    }
    if distance.is_some() {
        station.insert("distanceKm".into(), round_km(distance));
    }
    Value::Object(station)
}

async fn resolve(data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    products::resolve_issue(
        data,
        products::product(PRODUCT).expect("registered"),
        query.issue().as_deref(),
    )
    .await
}

/// `GET /v1/airports`: every station's newest observation.
pub async fn list(data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let spatial = Spatial::parse(&query)?;
    let category = query.get("category").map(|value| value.to_ascii_uppercase());
    let issue = resolve(data, &query).await?;
    let rows: Vec<Value> = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|row| match &category {
            Some(wanted) => column(row, "category").as_str() == Some(wanted.as_str()),
            None => true,
        })
        .collect();
    let stations: Vec<Value> = spatial
        .select(rows, position)
        .iter()
        .map(|(row, distance)| station_json(row, *distance))
        .collect();
    let mut body = issue.envelope();
    let mut request = spatial.request_json();
    request["category"] = json!(category);
    body.insert("request".into(), request);
    body.insert("count".into(), json!(stations.len()));
    body.insert("units".into(), units());
    body.insert("stations".into(), Value::Array(stations));
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// `GET /v1/airports/{icao}`: one station's METARs (newest first) and TAF.
pub async fn station(data: &Data, icao: &str, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let target = query.time()?;
    let icao = icao.to_ascii_uppercase();
    let issue = resolve(data, &query).await?;
    let rows = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let row = rows
        .iter()
        .find(|row| row.get(0).and_then(Value::as_str) == Some(icao.as_str()))
        .ok_or_else(|| {
            HttpError::new(
                404,
                "unknown_station",
                format!("{} has no station {icao}", issue.id),
            )
        })?;
    let (path, crc32) = issue.file(issue.index.get("history").unwrap_or(&Value::Null), "history")?;
    let line = products::read_slice(data, &path, &crc32, row, OFFSET).await?;

    let mut metars: Vec<Value> = line
        .get("metars")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if let Some(target) = target {
        let times: Vec<f64> = metars
            .iter()
            .map(|metar| zarr::parse_iso(text(metar, "time")).unwrap_or(f64::NAN))
            .collect();
        metars = nearest(&times, target)
            .map(|index| vec![metars[index].clone()])
            .unwrap_or_default();
    }

    let (lat, lon) = position(row).unwrap_or((f64::NAN, f64::NAN));
    let mut station = Map::new();
    for key in ["icao", "name", "lat", "lon", "elev", "iata", "wmo"] {
        station.insert(key.to_owned(), line.get(key).cloned().unwrap_or(Value::Null));
    }
    station.insert(
        "timezone".into(),
        if lat.is_finite() {
            json!(timezone::name(lon, lat))
        } else {
            Value::Null
        },
    );

    let mut body = issue.envelope();
    body.insert("station".into(), Value::Object(station));
    body.insert("units".into(), units());
    body.insert("metars".into(), Value::Array(metars));
    body.insert(
        "taf".into(),
        line.get("taf").cloned().unwrap_or(Value::Null),
    );
    Ok((Value::Object(body), issue.id))
}
