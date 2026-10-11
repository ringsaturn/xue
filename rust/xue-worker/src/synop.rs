//! The surface stations (`docs/synop.md`): the index's compact rows as
//! objects keyed by element, and one station's 24 h window sliced out of its
//! network's file and answered in the shape `/v1/point` uses — an ISO time
//! axis and a flat `variables` map — so a client reads a station and a grid
//! cell with one type.

use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, nearest, round_km, text, Issue, Query, Spatial};
use crate::timezone;

const PRODUCT: &str = "synop";

/// The fixed leading columns of an index row; the elements follow, then the
/// byte span.
const LEAD: [&str; 8] = [
    "id", "network", "lat", "lon", "elev", "rank", "name", "obsTime",
];

/// The contract's elements with their labels and units. An element the
/// index lists that is not here is admitted with a null label and unit.
const ELEMENTS: [(&str, &str, &str); 12] = [
    ("t", "air temperature", "°C"),
    ("rh", "relative humidity", "%"),
    ("p", "station pressure", "hPa"),
    ("slp", "sea-level pressure", "hPa"),
    ("wd", "wind direction (from); null when calm or variable", "°"),
    ("ws", "mean wind speed", "m/s"),
    ("gust", "gust", "m/s"),
    ("pr", "precipitation over the network's period", "mm"),
    ("pr1h", "precipitation over the hour", "mm"),
    ("sun", "sunshine over the network's period", "min"),
    ("snow", "snow depth", "cm"),
    ("vis", "visibility", "m"),
];

fn element_meta(key: &str) -> (Value, Value) {
    ELEMENTS
        .iter()
        .find(|(held, _, _)| *held == key)
        .map(|(_, label, unit)| (json!(label), json!(unit)))
        .unwrap_or((Value::Null, Value::Null))
}

fn elements(index: &Value) -> Vec<String> {
    index
        .get("elements")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn networks(index: &Value) -> Vec<Value> {
    index
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn network_id(networks: &[Value], row: &Value) -> Option<String> {
    let position = row.get(1)?.as_u64()? as usize;
    Some(text(networks.get(position)?, "id").to_owned())
}

fn position(row: &Value) -> Option<(f64, f64)> {
    Some((row.get(2)?.as_f64()?, row.get(3)?.as_f64()?))
}

fn station_json(
    row: &Value,
    networks: &[Value],
    elements: &[String],
    distance: Option<f64>,
) -> Value {
    let mut station = Map::new();
    for (index, key) in LEAD.iter().enumerate() {
        let value = row.get(index).cloned().unwrap_or(Value::Null);
        let value = if *key == "network" {
            json!(network_id(networks, row))
        } else {
            value
        };
        station.insert((*key).to_owned(), value);
    }
    let mut values = Map::new();
    for (offset, key) in elements.iter().enumerate() {
        values.insert(
            key.clone(),
            row.get(LEAD.len() + offset).cloned().unwrap_or(Value::Null),
        );
    }
    station.insert("values".into(), Value::Object(values));
    if distance.is_some() {
        station.insert("distanceKm".into(), round_km(distance));
    }
    Value::Object(station)
}

/// A network's entry without its file descriptor, which is the product's
/// internal addressing.
fn network_json(network: &Value) -> Value {
    let mut out = network.as_object().cloned().unwrap_or_default();
    out.remove("file");
    Value::Object(out)
}

async fn resolve(data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    products::resolve_issue(
        data,
        products::product(PRODUCT).expect("registered"),
        query.issue().as_deref(),
    )
    .await
}

/// `GET /v1/synop`: every station's current values.
pub async fn list(data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let spatial = Spatial::parse(&query)?;
    let wanted = query.get("network");
    let issue = resolve(data, &query).await?;
    let networks = networks(&issue.index);
    let elements = elements(&issue.index);
    if let Some(wanted) = &wanted {
        if !networks.iter().any(|network| text(network, "id") == wanted) {
            let available: Vec<&str> = networks.iter().map(|network| text(network, "id")).collect();
            return Err(HttpError::new(
                404,
                "unknown_network",
                format!("{} has no network {wanted}", issue.id),
            )
            .with_detail(json!({ "available": available })));
        }
    }
    let rows: Vec<Value> = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|row| match &wanted {
            Some(wanted) => network_id(&networks, row).as_deref() == Some(wanted.as_str()),
            None => true,
        })
        .collect();
    let stations: Vec<Value> = spatial
        .select(rows, position)
        .iter()
        .map(|(row, distance)| station_json(row, &networks, &elements, *distance))
        .collect();

    let mut element_table = Map::new();
    for key in &elements {
        let (label, unit) = element_meta(key);
        element_table.insert(key.clone(), json!({ "label": label, "unit": unit }));
    }
    let mut body = issue.envelope();
    let mut request = spatial.request_json();
    request["network"] = json!(wanted);
    body.insert("request".into(), request);
    body.insert(
        "networks".into(),
        Value::Array(
            networks
                .iter()
                .filter(|network| match &wanted {
                    Some(wanted) => text(network, "id") == wanted,
                    None => true,
                })
                .map(network_json)
                .collect(),
        ),
    );
    body.insert("elements".into(), Value::Object(element_table));
    body.insert("count".into(), json!(stations.len()));
    body.insert("stations".into(), Value::Array(stations));
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// `GET /v1/synop/{station}`: one station's window as a time axis and a
/// flat `variables` map, one entry per element of the index (all `null` for
/// an element the station never reported).
pub async fn station(data: &Data, id: &str, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let target = query.time()?;
    let issue = resolve(data, &query).await?;
    let networks = networks(&issue.index);
    let elements = elements(&issue.index);
    let rows = issue
        .index
        .get("stations")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let row = rows
        .iter()
        .find(|row| row.get(0).and_then(Value::as_str) == Some(id))
        .ok_or_else(|| {
            HttpError::new(
                404,
                "unknown_station",
                format!("{} has no station {id}", issue.id),
            )
        })?;
    let network = row
        .get(1)
        .and_then(Value::as_u64)
        .and_then(|position| networks.get(position as usize))
        .ok_or_else(|| {
            HttpError::new(
                502,
                "upstream_failed",
                format!("{}: station {id} names no network", issue.id),
            )
        })?;
    let (path, crc32) = issue.file(network.get("file").unwrap_or(&Value::Null), "network")?;
    let line = products::read_slice(data, &path, &crc32, row, LEAD.len() + elements.len()).await?;

    // Epoch seconds to the API's ISO instants; `time=` keeps the nearest.
    let epoch: Vec<f64> = line
        .get("time")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_f64).collect())
        .unwrap_or_default();
    let keep: Vec<usize> = match target {
        Some(target) => {
            let millis: Vec<f64> = epoch.iter().map(|seconds| seconds * 1000.0).collect();
            nearest(&millis, target).into_iter().collect()
        }
        None => (0..epoch.len()).collect(),
    };
    let times: Vec<Value> = keep
        .iter()
        .map(|&index| json!(zarr::format_iso_millis(epoch[index] * 1000.0)))
        .collect();
    let obs = line.get("obs").and_then(Value::as_object);
    let mut variables = Map::new();
    for key in &elements {
        let series = obs.and_then(|obs| obs.get(key)).and_then(Value::as_array);
        let values: Vec<Value> = keep
            .iter()
            .map(|&index| {
                series
                    .and_then(|series| series.get(index))
                    .cloned()
                    .unwrap_or(Value::Null)
            })
            .collect();
        let (label, unit) = element_meta(key);
        variables.insert(
            key.clone(),
            json!({ "label": label, "unit": unit, "values": values }),
        );
    }

    let (lat, lon) = position(row).unwrap_or((f64::NAN, f64::NAN));
    let mut station = Map::new();
    station.insert("id".into(), json!(id));
    station.insert("network".into(), json!(text(network, "id")));
    for key in ["name", "names", "lat", "lon", "elev", "wmo", "rank"] {
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
    body.insert("network".into(), network_json(network));
    body.insert(
        "time".into(),
        json!({
            "unitSeconds": network.get("cadence").cloned().unwrap_or(Value::Null),
            "times": times,
        }),
    );
    body.insert("variables".into(), Value::Object(variables));
    Ok((Value::Object(body), issue.id))
}
