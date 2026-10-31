//! The surface stations (`docs/synop.md`): the index's compact rows as
//! objects keyed by element, and one station's 24 h window sliced out of its
//! network's file and answered in the shape `/v1/point` uses — an ISO time
//! axis and a flat `variables` map — so a client reads a station and a grid
//! cell with one type.

use futures_util::future::join_all;
use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, nearest, round_km, text, Issue, Query, Spatial};
use crate::timezone;

const PRODUCT: &str = "synop";

/// The fixed leading columns of a v1 index row; the elements follow, then
/// the byte span. A v2 station table's rows are the same without `network`
/// (the table is the network's), and are read back into this shape.
const LEAD: [&str; 8] = [
    "id", "network", "lat", "lon", "elev", "rank", "name", "obsTime",
];

/// The station table version a v2 index names beside itself.
const STATIONS_SCHEMA_VERSION: u64 = 2;

/// The contract's elements with their labels and units. An element the
/// index lists that is not here is admitted with a null label and unit.
const ELEMENTS: [(&str, &str, &str); 13] = [
    ("t", "air temperature", "°C"),
    ("td", "dew point", "°C"),
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

/// A network's entry without its file descriptors, which are the product's
/// internal addressing.
fn network_json(network: &Value) -> Value {
    let mut out = network.as_object().cloned().unwrap_or_default();
    out.remove("file");
    out.remove("stations");
    Value::Object(out)
}

fn index_version(index: &Value) -> u64 {
    index
        .get("schemaVersion")
        .and_then(Value::as_u64)
        .unwrap_or(1)
}

/// One network's v2 station table, its rows put back into v1's shape with
/// `position` as their network. A table that is not the one the index
/// names — another version, another network, other elements, another row
/// count, rows out of order or outside the network — is refused whole.
async fn network_rows(
    data: &Data,
    issue: &Issue,
    network: &Value,
    position: usize,
    elements: &[String],
) -> Result<Vec<Value>, HttpError> {
    let id = text(network, "id");
    let descriptor = network.get("stations").unwrap_or(&Value::Null);
    let (path, crc32) = issue.file(descriptor, &format!("{id} station"))?;
    let table: Value = data.json_cached(&path, &crc32).await?;
    let refuse = |why: &str| {
        HttpError::new(
            502,
            "upstream_failed",
            format!("{}: {path} {why}", issue.id),
        )
    };
    if table.get("schemaVersion").and_then(Value::as_u64) != Some(STATIONS_SCHEMA_VERSION) {
        return Err(refuse("is not a v2 station table"));
    }
    if table.get("network").and_then(Value::as_str) != Some(id) {
        return Err(refuse(&format!("is not network {id}'s")));
    }
    let listed: Option<Vec<&str>> = table
        .get("elements")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect());
    let expected: Vec<&str> = elements.iter().map(String::as_str).collect();
    if listed.as_ref() != Some(&expected) {
        return Err(refuse("lists other elements than the index"));
    }
    let rows = table
        .get("stations")
        .and_then(Value::as_array)
        .ok_or_else(|| refuse("has no stations"))?;
    if let Some(count) = descriptor.get("count").and_then(Value::as_u64) {
        if rows.len() as u64 != count {
            return Err(refuse(&format!("has {} rows, the index says {count}", rows.len())));
        }
    }
    let width = LEAD.len() - 1 + elements.len() + 2;
    let prefix = format!("{id}:");
    let byte_length = network
        .get("file")
        .and_then(|file| file.get("byteLength"))
        .and_then(Value::as_u64);
    let mut previous: Option<&str> = None;
    let mut end = 0u64;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let items = row
            .as_array()
            .filter(|items| items.len() == width)
            .ok_or_else(|| refuse(&format!("has a row that is not {width} values")))?;
        let station = items[0]
            .as_str()
            .filter(|station| station.starts_with(&prefix))
            .ok_or_else(|| refuse("has a row outside its network"))?;
        if previous.is_some_and(|previous| station <= previous) {
            return Err(refuse("is not sorted by id and unique"));
        }
        previous = Some(station);
        let (Some(offset), Some(length)) = (items[width - 2].as_u64(), items[width - 1].as_u64())
        else {
            return Err(refuse(&format!("gives {station} no byte span")));
        };
        if offset < end || byte_length.is_some_and(|bytes| offset + length > bytes) {
            return Err(refuse(&format!("gives {station} a span out of order or past its file")));
        }
        end = offset + length;
        let mut v1 = Vec::with_capacity(width + 1);
        v1.push(items[0].clone());
        v1.push(json!(position));
        v1.extend(items[1..].iter().cloned());
        out.push(Value::Array(v1));
    }
    Ok(out)
}

/// The issue's networks and its stations as v1 index rows, whichever
/// version the round wrote. A v2 round's tables are read in parallel; a
/// network whose table fails is left out (the rest renumbered), as the
/// shell leaves it out, rather than failing the whole list.
async fn stations_v1(
    data: &Data,
    issue: &Issue,
    elements: &[String],
) -> (Vec<Value>, Vec<Value>) {
    let networks = networks(&issue.index);
    if index_version(&issue.index) < 2 {
        let rows = issue
            .index
            .get("stations")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        return (networks, rows);
    }
    let tables = join_all(
        networks
            .iter()
            .enumerate()
            .map(|(position, network)| network_rows(data, issue, network, position, elements)),
    )
    .await;
    let mut kept = Vec::new();
    let mut rows = Vec::new();
    for (network, table) in networks.into_iter().zip(tables) {
        let Ok(table) = table else { continue };
        let position = kept.len();
        kept.push(network);
        rows.extend(table.into_iter().map(|mut row| {
            row[1] = json!(position);
            row
        }));
    }
    rows.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    (kept, rows)
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
    let elements = elements(&issue.index);
    let (networks, rows) = stations_v1(data, &issue, &elements).await;
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
    let rows: Vec<Value> = rows
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
    let rows = if index_version(&issue.index) < 2 {
        issue
            .index
            .get("stations")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    } else {
        // Only the table of the network the id names: the others are not
        // needed to answer one station.
        let wanted = id.split_once(':').map(|(network, _)| network);
        match networks
            .iter()
            .position(|network| Some(text(network, "id")) == wanted)
        {
            Some(position) => {
                network_rows(data, &issue, &networks[position], position, &elements).await?
            }
            None => Vec::new(),
        }
    };
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
