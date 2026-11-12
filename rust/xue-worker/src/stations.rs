//! The station products — the surface stations (`docs/synop.md`) and the
//! air-quality stations (`docs/aq.md`), which share one layout: an index
//! naming per network a table of compact rows and an NDJSON file of each
//! station's window. The list is the rows as objects keyed by element; one
//! station's window is sliced out of its network's file and answered in the
//! shape `/v1/point` uses — an ISO time axis and a flat `variables` map — so
//! a client reads a station and a grid cell with one type.

use futures_util::future::join_all;
use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, nearest, round_km, text, Issue, Query, Spatial};
use crate::timezone;

/// What tells one station product from the other.
pub struct Kind {
    product: &'static str,
    /// The leading columns of a row in v1 index shape (`network` second,
    /// as its position in `networks[]`); the elements follow, then the byte
    /// span. A station table's rows are the same without `network` (the
    /// table is the network's), and are read back into this shape.
    lead: &'static [&'static str],
    /// The station table version the index names beside itself.
    table_version: u64,
    /// Index versions below this carry the rows inline (`stations[]`);
    /// from it on, one station table per network.
    tables_from: u64,
    /// The newest index version this reader implements.
    index_version: u64,
    /// The elements with their labels and fallback units. An element the
    /// index lists that is not here is admitted with a null label and unit.
    elements: &'static [(&'static str, &'static str, &'static str)],
    /// The fields of a station's line copied into the record's `station`.
    station_fields: &'static [&'static str],
}

pub const SYNOP: Kind = Kind {
    product: "synop",
    lead: &[
        "id", "network", "lat", "lon", "elev", "rank", "name", "obsTime",
    ],
    table_version: 2,
    tables_from: 2,
    index_version: 2,
    elements: &[
        ("t", "air temperature", "°C"),
        ("td", "dew point", "°C"),
        ("rh", "relative humidity", "%"),
        ("p", "station pressure", "hPa"),
        ("slp", "sea-level pressure", "hPa"),
        (
            "wd",
            "wind direction (from); null when calm or variable",
            "°",
        ),
        ("ws", "mean wind speed", "m/s"),
        ("gust", "gust", "m/s"),
        ("pr", "precipitation over the network's period", "mm"),
        ("pr1h", "precipitation over the hour", "mm"),
        ("sun", "sunshine over the network's period", "min"),
        ("snow", "snow depth", "cm"),
        ("vis", "visibility", "m"),
        ("tide", "tide height above the network's chart datum", "m"),
    ],
    station_fields: &["name", "names", "lat", "lon", "elev", "wmo", "rank"],
};

pub const AQ: Kind = Kind {
    product: "aq",
    lead: &[
        "id", "network", "lat", "lon", "elev", "rank", "name", "agency", "obsTime",
    ],
    table_version: 1,
    tables_from: 0,
    index_version: 1,
    elements: &[
        ("pm25", "PM2.5 mass concentration, hourly mean", "µg/m³"),
        ("pm10", "PM10 mass concentration, hourly mean", "µg/m³"),
        ("o3", "ozone, hourly mean", "ppb"),
        ("no2", "nitrogen dioxide, hourly mean", "ppb"),
        ("so2", "sulphur dioxide, hourly mean", "ppb"),
        ("co", "carbon monoxide, hourly mean", "ppm"),
    ],
    station_fields: &["name", "names", "lat", "lon", "elev", "rank", "agency"],
};

impl Kind {
    /// An element's `{label, unit, cell, period}`: the label this reader
    /// knows; the unit, cell method and period the index's `elementTable`
    /// gives, the unit falling back to this reader's own where the index
    /// has none (a round from before the table).
    fn element(&self, table: Option<&Map<String, Value>>, key: &str) -> Map<String, Value> {
        let known = self.elements.iter().find(|(held, _, _)| *held == key);
        let entry = table.and_then(|table| table.get(key));
        let field = |name: &str| entry.and_then(|entry| entry.get(name));
        let unit = field("unit")
            .filter(|unit| unit.is_string())
            .cloned()
            .or_else(|| known.map(|(_, _, unit)| json!(unit)))
            .unwrap_or(Value::Null);
        let mut out = Map::new();
        out.insert(
            "label".into(),
            known
                .map(|(_, label, _)| json!(label))
                .unwrap_or(Value::Null),
        );
        out.insert("unit".into(), unit);
        out.insert("cell".into(), field("cell").cloned().unwrap_or(Value::Null));
        out.insert(
            "period".into(),
            field("period").cloned().unwrap_or(Value::Null),
        );
        out
    }

    /// A station table row's columns in the order this reader takes them:
    /// the lead without `network`, the elements, the byte span. A table
    /// without `columns` lays its rows out in exactly this order.
    fn table_columns(&self, elements: &[String]) -> Vec<String> {
        self.lead
            .iter()
            .filter(|key| **key != "network")
            .map(|key| (*key).to_owned())
            .chain(elements.iter().cloned())
            .chain(["offset".to_owned(), "length".to_owned()])
            .collect()
    }

    /// Where each of [`Kind::table_columns`] sits in a table's rows, and
    /// the rows' width: found by name in the table's `columns`, or in
    /// place for a table written before that field.
    fn column_order(&self, table: &Value, elements: &[String]) -> Result<(Vec<usize>, usize), String> {
        let wanted = self.table_columns(elements);
        let Some(columns) = table.get("columns") else {
            return Ok(((0..wanted.len()).collect(), wanted.len()));
        };
        let names: Vec<&str> = columns
            .as_array()
            .and_then(|items| items.iter().map(Value::as_str).collect())
            .ok_or("names its columns with something other than strings")?;
        let order = wanted
            .iter()
            .map(|name| {
                let mut found = names
                    .iter()
                    .enumerate()
                    .filter(|(_, held)| **held == name.as_str());
                match (found.next(), found.next()) {
                    (Some((position, _)), None) => Ok(position),
                    (None, _) => Err(format!("has no {name} column")),
                    _ => Err(format!("names the {name} column twice")),
                }
            })
            .collect::<Result<Vec<usize>, String>>()?;
        Ok((order, names.len()))
    }
}

fn element_table(index: &Value) -> Option<&Map<String, Value>> {
    index.get("elementTable").and_then(Value::as_object)
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
    kind: &Kind,
    row: &Value,
    networks: &[Value],
    elements: &[String],
    distance: Option<f64>,
) -> Value {
    let mut station = Map::new();
    for (index, key) in kind.lead.iter().enumerate() {
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
            row.get(kind.lead.len() + offset)
                .cloned()
                .unwrap_or(Value::Null),
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

/// Whether the index carries its rows inline rather than in station tables.
fn inline_rows(kind: &Kind, index: &Value) -> bool {
    index_version(index) < kind.tables_from
}

/// One network's v2 station table, its rows put back into v1's shape with
/// `position` as their network. A table that is not the one the index
/// names — another version, another network, other elements, another row
/// count, rows out of order or outside the network — is refused whole.
async fn network_rows(
    kind: &Kind,
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
    if table.get("schemaVersion").and_then(Value::as_u64) != Some(kind.table_version) {
        return Err(refuse(&format!(
            "is not a v{} station table",
            kind.table_version
        )));
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
            return Err(refuse(&format!(
                "has {} rows, the index says {count}",
                rows.len()
            )));
        }
    }
    let (order, width) = kind
        .column_order(&table, elements)
        .map_err(|why| refuse(&why))?;
    let prefix = format!("{id}:");
    let byte_length = network
        .get("file")
        .and_then(|file| file.get("byteLength"))
        .and_then(Value::as_u64);
    let mut previous: Option<&str> = None;
    let mut end = 0u64;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let held = row
            .as_array()
            .filter(|items| items.len() == width)
            .ok_or_else(|| refuse(&format!("has a row that is not {width} values")))?;
        let items: Vec<&Value> = order.iter().map(|&column| &held[column]).collect();
        let last = items.len() - 1;
        let station = items[0]
            .as_str()
            .filter(|station| station.starts_with(&prefix))
            .ok_or_else(|| refuse("has a row outside its network"))?;
        if previous.is_some_and(|previous| station <= previous) {
            return Err(refuse("is not sorted by id and unique"));
        }
        previous = Some(station);
        let (Some(offset), Some(length)) = (items[last - 1].as_u64(), items[last].as_u64())
        else {
            return Err(refuse(&format!("gives {station} no byte span")));
        };
        if offset < end || byte_length.is_some_and(|bytes| offset + length > bytes) {
            return Err(refuse(&format!(
                "gives {station} a span out of order or past its file"
            )));
        }
        end = offset + length;
        let mut v1 = Vec::with_capacity(items.len() + 1);
        v1.push(items[0].clone());
        v1.push(json!(position));
        v1.extend(items[1..].iter().map(|&value| value.clone()));
        out.push(Value::Array(v1));
    }
    Ok(out)
}

/// The issue's networks and its stations as v1 index rows, whichever
/// version the round wrote. A v2 round's tables are read in parallel; a
/// network whose table fails is left out (the rest renumbered), as the
/// shell leaves it out, rather than failing the whole list.
async fn stations_v1(
    kind: &Kind,
    data: &Data,
    issue: &Issue,
    elements: &[String],
) -> (Vec<Value>, Vec<Value>) {
    let networks = networks(&issue.index);
    if inline_rows(kind, &issue.index) {
        let rows = issue
            .index
            .get("stations")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        return (networks, rows);
    }
    let tables =
        join_all(networks.iter().enumerate().map(|(position, network)| {
            network_rows(kind, data, issue, network, position, elements)
        }))
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

async fn resolve(kind: &Kind, data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    let issue = products::resolve_issue(
        data,
        products::product(kind.product).expect("registered"),
        query.issue().as_deref(),
    )
    .await?;
    let version = index_version(&issue.index);
    if version > kind.index_version {
        return Err(HttpError::new(
            502,
            "upstream_failed",
            format!(
                "{}: index schemaVersion {version} is newer than this reader's {}",
                issue.id, kind.index_version
            ),
        ));
    }
    Ok(issue)
}

/// `GET /v1/synop`, `GET /v1/aq`: every station's current values.
pub async fn list(kind: &Kind, data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let spatial = Spatial::parse(&query)?;
    let wanted = query.get("network");
    let issue = resolve(kind, data, &query).await?;
    let elements = elements(&issue.index);
    let (networks, rows) = stations_v1(kind, data, &issue, &elements).await;
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
        .map(|(row, distance)| station_json(kind, row, &networks, &elements, *distance))
        .collect();

    let mut table = Map::new();
    let held = element_table(&issue.index);
    for key in &elements {
        table.insert(key.clone(), Value::Object(kind.element(held, key)));
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
    body.insert("elements".into(), Value::Object(table));
    body.insert("count".into(), json!(stations.len()));
    body.insert("stations".into(), Value::Array(stations));
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// `GET /v1/synop/{station}`, `GET /v1/aq/{station}`: one station's window
/// as a time axis and a flat `variables` map, one entry per element of the
/// index (all `null` for an element the station never reported).
pub async fn station(
    kind: &Kind,
    data: &Data,
    id: &str,
    url: &Url,
) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let target = query.time()?;
    let issue = resolve(kind, data, &query).await?;
    let networks = networks(&issue.index);
    let elements = elements(&issue.index);
    let rows = if inline_rows(kind, &issue.index) {
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
                network_rows(kind, data, &issue, &networks[position], position, &elements).await?
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
    let line =
        products::read_slice(data, &path, &crc32, row, kind.lead.len() + elements.len()).await?;

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
    let held = element_table(&issue.index);
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
        let mut variable = kind.element(held, key);
        variable.insert("values".into(), Value::Array(values));
        variables.insert(key.clone(), Value::Object(variable));
    }

    let (lat, lon) = position(row).unwrap_or((f64::NAN, f64::NAN));
    let mut station = Map::new();
    station.insert("id".into(), json!(id));
    station.insert("network".into(), json!(text(network, "id")));
    for key in kind.station_fields {
        station.insert(
            (*key).to_owned(),
            line.get(*key).cloned().unwrap_or(Value::Null),
        );
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The columns this reader takes a row's lead from are the ones the
    /// normative schema names (`docs/schemas/aq-stations.schema.json`), so
    /// a column renamed there cannot leave the worker reading another.
    #[test]
    fn the_aq_lead_is_the_schemas() {
        let schema: Value = serde_json::from_str(include_str!(
            "../../../docs/schemas/aq-stations.schema.json"
        ))
        .expect("schema");
        let named: Vec<String> = schema["properties"]["columns"]["prefixItems"]
            .as_array()
            .expect("prefixItems")
            .iter()
            .map(|item| item["const"].as_str().expect("const").to_owned())
            .collect();
        let columns = AQ.table_columns(&[]);
        assert_eq!(&columns[..named.len()], named.as_slice());
        assert_eq!(&columns[named.len()..], ["offset", "length"]);
    }

    /// The same for synop (`docs/schemas/synop-stations.schema.json`).
    #[test]
    fn the_synop_lead_is_the_schemas() {
        let schema: Value = serde_json::from_str(include_str!(
            "../../../docs/schemas/synop-stations.schema.json"
        ))
        .expect("schema");
        let named: Vec<String> = schema["properties"]["columns"]["prefixItems"]
            .as_array()
            .expect("prefixItems")
            .iter()
            .map(|item| item["const"].as_str().expect("const").to_owned())
            .collect();
        let columns = SYNOP.table_columns(&[]);
        assert_eq!(&columns[..named.len()], named.as_slice());
        assert_eq!(&columns[named.len()..], ["offset", "length"]);
    }
}
