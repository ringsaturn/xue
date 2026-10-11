//! The radiosonde soundings (`docs/sounding.md`): the index's station list,
//! and one station's ascents sliced out of `soundings.jsonl` by the span the
//! index gives, with the fixed-point level arrays decoded to physical units.

use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, fixed, nearest, round_km, text, Issue, Query, Spatial};
use crate::timezone;

const PRODUCT: &str = "sounding";

/// The published level arrays with the label and unit they decode to (`""`
/// for a unitless flag word).
const LEVELS: [(&str, &str, &str); 7] = [
    ("p", "pressure", "hPa"),
    ("z", "geopotential height", "gpm"),
    ("t", "temperature", "°C"),
    ("td", "dew-point temperature", "°C"),
    ("wd", "wind direction (from)", "°"),
    ("ws", "wind speed", "m/s"),
    ("sig", "BUFR 0 08 042 significance flags", ""),
];

pub fn units() -> Value {
    let mut units = Map::new();
    for (key, label, unit) in LEVELS {
        units.insert(
            key.to_owned(),
            json!({ "label": label, "unit": if unit.is_empty() { Value::Null } else { json!(unit) } }),
        );
    }
    Value::Object(units)
}

fn station_json(row: &Value, distance: Option<f64>) -> Value {
    let mut station = Map::new();
    for key in ["id", "wmo", "name", "lat", "lon", "elev", "latest", "times", "headline"] {
        station.insert(key.to_owned(), row.get(key).cloned().unwrap_or(Value::Null));
    }
    if distance.is_some() {
        station.insert("distanceKm".into(), round_km(distance));
    }
    Value::Object(station)
}

fn position(row: &Value) -> Option<(f64, f64)> {
    Some((row.get("lat")?.as_f64()?, row.get("lon")?.as_f64()?))
}

async fn resolve(data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    products::resolve_issue(
        data,
        products::product(PRODUCT).expect("registered"),
        query.issue().as_deref(),
    )
    .await
}

/// `GET /v1/soundings`: the stations of the live (or pinned) issue.
pub async fn list(data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let spatial = Spatial::parse(&query)?;
    let issue = resolve(data, &query).await?;
    let rows = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let stations: Vec<Value> = spatial
        .select(rows, position)
        .iter()
        .map(|(row, distance)| station_json(row, *distance))
        .collect();
    let mut body = issue.envelope();
    body.insert("request".into(), spatial.request_json());
    body.insert("count".into(), json!(stations.len()));
    body.insert("stations".into(), Value::Array(stations));
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// Decode one ascent: the seven parallel integer arrays to `levels`, in
/// physical units, `-32768` to `null`.
fn decode_sounding(sounding: &Value) -> Value {
    let mut out = Map::new();
    for key in [
        "time",
        "launched",
        "sondeType",
        "bulletin",
        "gateway",
        "arrived",
        "n",
        "reported",
    ] {
        out.insert(
            key.to_owned(),
            sounding.get(key).cloned().unwrap_or(Value::Null),
        );
    }
    let mut levels = Map::new();
    for (key, _, _) in LEVELS {
        let decoded: Vec<Value> = sounding
            .get(key)
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .map(|value| match key {
                        "p" => fixed(value, 0.01, 2),
                        // K × 100 to °C: shift by 273.15 K in the integer
                        // domain so 29515 reads exactly 22.
                        "t" | "td" => match value.as_i64() {
                            Some(-32768) | None => Value::Null,
                            Some(code) => json!(((code - 27315) as f64) / 100.0),
                        },
                        "ws" => fixed(value, 0.1, 1),
                        _ => fixed(value, 1.0, 0),
                    })
                    .collect()
            })
            .unwrap_or_default();
        levels.insert(key.to_owned(), Value::Array(decoded));
    }
    out.insert("levels".into(), Value::Object(levels));
    out.insert(
        "derived".into(),
        sounding.get("derived").cloned().unwrap_or(Value::Null),
    );
    Value::Object(out)
}

/// `GET /v1/soundings/{station}`: one station's ascents. `station` is the
/// WIGOS id the product keys on, or the five-digit WMO number.
pub async fn station(data: &Data, id: &str, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let target = query.time()?;
    let issue = resolve(data, &query).await?;
    let rows = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let row = rows
        .iter()
        .find(|row| text(row, "id") == id)
        .or_else(|| rows.iter().find(|row| text(row, "wmo") == id))
        .ok_or_else(|| {
            HttpError::new(
                404,
                "unknown_station",
                format!("{} has no station {id}", issue.id),
            )
        })?;
    let (path, crc32) = issue.file(
        issue.index.get("soundings").unwrap_or(&Value::Null),
        "soundings",
    )?;
    let line = products::read_slice(data, &path, &crc32, row, 0).await?;

    let mut soundings: Vec<&Value> = line
        .get("soundings")
        .and_then(Value::as_array)
        .map(|items| items.iter().collect())
        .unwrap_or_default();
    if let Some(target) = target {
        let times: Vec<f64> = soundings
            .iter()
            .map(|sounding| zarr::parse_iso(text(sounding, "time")).unwrap_or(f64::NAN))
            .collect();
        soundings = nearest(&times, target)
            .map(|index| vec![soundings[index]])
            .unwrap_or_default();
    }
    let decoded: Vec<Value> = soundings.iter().map(|s| decode_sounding(s)).collect();

    let (lat, lon) = position(&line).or_else(|| position(row)).unwrap_or((f64::NAN, f64::NAN));
    let mut station = Map::new();
    for key in ["id", "wmo", "name", "lat", "lon", "elev"] {
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
    body.insert("soundings".into(), Value::Array(decoded));
    Ok((Value::Object(body), issue.id))
}
