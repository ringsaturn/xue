//! The weather warnings (`docs/alerts.md`): the index's compact rows as
//! objects, filtered by network, category, level and place, and one alert's
//! full record sliced out of its network's file by the span its row gives.
//!
//! A point is tested against an alert's own polygon, which is in the line
//! and not in the index: the rows whose box contains the point are read (one
//! range each, held in the isolate) and tested. An alert drawn only by area
//! codes has no polygon here; it matches a point when its box does, and an
//! alert with no box matches no place at all — the boundary sets the codes
//! resolve against are PMTiles a client reads, not something this API opens.

use futures_util::future::join_all;
use serde_json::{json, Map, Value};
use url::Url;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, text, Issue, Query, Spatial};

const PRODUCT: &str = "alerts";

/// The newest index version this reader implements.
const INDEX_VERSION: u64 = 1;

/// An index row: `id, network, category, level, event, sent, onset,
/// expires, west, south, east, north, offset, length`.
const ROW_WIDTH: usize = 14;
const BBOX: usize = 8;
const OFFSET: usize = 12;

/// One row of the index, its fields picked out.
struct Row<'a> {
    raw: &'a Value,
    id: &'a str,
    network: usize,
    category: &'a str,
    level: u64,
    /// `[west, south, east, north]`: the polygon's box, or the area codes'
    /// where the round knows their boundaries; `None` when neither.
    bbox: Option<[f64; 4]>,
}

impl Row<'_> {
    fn json(&self, networks: &[Value]) -> Value {
        let item = |index: usize| self.raw.get(index).cloned().unwrap_or(Value::Null);
        json!({
            "id": self.id,
            "network": text(&networks[self.network], "id"),
            "category": self.category,
            "level": self.level,
            "event": item(4),
            "sent": item(5),
            "onset": item(6),
            "expires": item(7),
            "bbox": self.bbox,
        })
    }

    fn contains_point(&self, lat: f64, lon: f64) -> bool {
        self.bbox.is_some_and(|[west, south, east, north]| {
            lat >= south && lat <= north && lon >= west && lon <= east
        })
    }
}

fn refuse(issue: &Issue, why: impl std::fmt::Display) -> HttpError {
    HttpError::new(502, "upstream_failed", format!("{}: {why}", issue.id))
}

fn networks(index: &Value) -> Vec<Value> {
    index
        .get("networks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn categories(index: &Value) -> Vec<&str> {
    index
        .get("categories")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

/// The index's rows, every one checked for the shape the profile gives it.
fn rows<'a>(issue: &'a Issue, networks: &[Value]) -> Result<Vec<Row<'a>>, HttpError> {
    let Some(rows) = issue.index.get("alerts").and_then(Value::as_array) else {
        return Err(refuse(issue, "the index has no alerts"));
    };
    rows.iter()
        .map(|raw| {
            let items = raw
                .as_array()
                .filter(|items| items.len() == ROW_WIDTH)
                .ok_or_else(|| refuse(issue, format!("an alert row is not {ROW_WIDTH} values")))?;
            let id = items[0]
                .as_str()
                .ok_or_else(|| refuse(issue, "an alert row has no id"))?;
            let network = items[1]
                .as_u64()
                .map(|position| position as usize)
                .filter(|position| *position < networks.len())
                .ok_or_else(|| refuse(issue, format!("{id} names no network")))?;
            let bounds: Vec<f64> = items[BBOX..BBOX + 4]
                .iter()
                .filter_map(Value::as_f64)
                .collect();
            Ok(Row {
                raw,
                id,
                network,
                category: items[2].as_str().unwrap_or_default(),
                level: items[3].as_u64().unwrap_or_default(),
                bbox: (bounds.len() == 4).then(|| [bounds[0], bounds[1], bounds[2], bounds[3]]),
            })
        })
        .collect()
}

/// `ended[]` as objects: `[id, network, reason]` with an optional fourth
/// item, the id that replaced it (`by`), or the same as an object.
fn ended(index: &Value, networks: &[Value]) -> Vec<Value> {
    let network = |position: Option<u64>| {
        position
            .and_then(|position| networks.get(position as usize))
            .map(|network| json!(text(network, "id")))
            .unwrap_or(Value::Null)
    };
    index
        .get("ended")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| match item {
                    Value::Array(items) => Some(json!({
                        "id": items.first()?.as_str()?,
                        "network": network(items.get(1).and_then(Value::as_u64)),
                        "reason": items.get(2).cloned().unwrap_or(Value::Null),
                        "by": items.get(3).cloned().unwrap_or(Value::Null),
                    })),
                    Value::Object(map) => Some(json!({
                        "id": map.get("id")?.as_str()?,
                        "network": network(map.get("network").and_then(Value::as_u64)),
                        "reason": map.get("reason").cloned().unwrap_or(Value::Null),
                        "by": map.get("by").cloned().unwrap_or(Value::Null),
                    })),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

/// `../alerts-areas/x.pmtiles` from `alerts.<round>/` (or
/// `../../../../../alerts-areas/x.pmtiles` from the round filed under its
/// day), as a path under the data root.
fn resolve_path(dir: &str, path: &str) -> String {
    let mut parts: Vec<&str> = dir.split('/').filter(|part| !part.is_empty()).collect();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            part => parts.push(part),
        }
    }
    parts.join("/")
}

/// A network's entry without its file descriptor, which is the product's
/// internal addressing; its boundary set's `path` resolved against the data
/// root, since a client of the API has no index to resolve it against.
fn network_json(issue: &Issue, network: &Value) -> Value {
    let mut out = network.as_object().cloned().unwrap_or_default();
    out.remove("file");
    if let Some(Value::Object(areas)) = out.get_mut("areas") {
        if let Some(path) = areas.get("path").and_then(Value::as_str) {
            let resolved = resolve_path(&issue.dir, path);
            areas.insert("path".into(), json!(resolved));
        }
    }
    Value::Object(out)
}

/// Whether one ring winds around the point (even–odd).
fn ring_crosses(ring: &Value, lat: f64, lon: f64) -> bool {
    let points: Vec<(f64, f64)> = ring
        .as_array()
        .map(|points| {
            points
                .iter()
                .filter_map(|point| Some((point.get(0)?.as_f64()?, point.get(1)?.as_f64()?)))
                .collect()
        })
        .unwrap_or_default();
    let mut inside = false;
    if points.is_empty() {
        return inside;
    }
    let mut previous = points[points.len() - 1];
    for &current in &points {
        let ((x1, y1), (x2, y2)) = (previous, current);
        if (y1 > lat) != (y2 > lat) && lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1) {
            inside = !inside;
        }
        previous = current;
    }
    inside
}

/// Whether a GeoJSON Polygon or MultiPolygon contains the point: inside an
/// outer ring and outside its holes, which even–odd over all of a polygon's
/// rings is.
pub(crate) fn geometry_contains(geometry: &Value, lat: f64, lon: f64) -> bool {
    let polygon = |rings: &Value| {
        rings.as_array().is_some_and(|rings| {
            rings
                .iter()
                .filter(|ring| ring_crosses(ring, lat, lon))
                .count()
                % 2
                == 1
        })
    };
    let coordinates = geometry.get("coordinates").unwrap_or(&Value::Null);
    match geometry.get("type").and_then(Value::as_str) {
        Some("Polygon") => polygon(coordinates),
        Some("MultiPolygon") => coordinates
            .as_array()
            .is_some_and(|polygons| polygons.iter().any(polygon)),
        _ => false,
    }
}

/// Whether a query box (`west > east` across the antimeridian) meets an
/// alert's plain min/max box.
fn boxes_meet(query: [f64; 4], bbox: [f64; 4]) -> bool {
    let [west, south, east, north] = query;
    let [alert_west, alert_south, alert_east, alert_north] = bbox;
    if alert_north < south || alert_south > north {
        return false;
    }
    if east - west >= 360.0 {
        return true;
    }
    let wrap = |value: f64| ((value + 180.0).rem_euclid(360.0)) - 180.0;
    let (west, east) = (wrap(west), wrap(east));
    let meets = |low: f64, high: f64| alert_east >= low && alert_west <= high;
    if west <= east {
        meets(west, east)
    } else {
        meets(west, 180.0) || meets(-180.0, east)
    }
}

async fn resolve(data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    let issue = products::resolve_issue(
        data,
        products::product(PRODUCT).expect("registered"),
        query.issue().as_deref(),
    )
    .await?;
    let version = issue
        .index
        .get("schemaVersion")
        .and_then(Value::as_u64)
        .unwrap_or(1);
    if version > INDEX_VERSION {
        return Err(refuse(
            &issue,
            format!("index schemaVersion {version} is newer than this reader's {INDEX_VERSION}"),
        ));
    }
    Ok(issue)
}

/// One alert's line, by the span its row gives, checked to be that alert's.
async fn read_line(
    data: &Data,
    issue: &Issue,
    networks: &[Value],
    row: &Row<'_>,
) -> Result<Value, HttpError> {
    let network = &networks[row.network];
    let descriptor = network.get("file").unwrap_or(&Value::Null);
    let (path, crc32) = issue.file(descriptor, &format!("{} alert", text(network, "id")))?;
    let line = products::read_slice(data, &path, &crc32, row.raw, OFFSET).await?;
    if line.get("id").and_then(Value::as_str) != Some(row.id) {
        return Err(refuse(
            issue,
            format!("the span of {} is another alert's", row.id),
        ));
    }
    Ok(line)
}

/// `GET /v1/alerts`: the alerts in force, filtered.
pub async fn list(data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let spatial = Spatial::parse(&query)?;
    if spatial.radius_km.is_some() {
        return Err(HttpError::new(
            400,
            "invalid_parameter",
            "radius does not apply to alerts: lat and lon select the alerts in force at that point",
        ));
    }
    let level = match query.get("level") {
        None => None,
        Some(text) => Some(
            text.parse::<u64>()
                .ok()
                .filter(|level| (1..=5).contains(level))
                .ok_or_else(|| {
                    HttpError::new(
                        400,
                        "invalid_parameter",
                        "level must be an integer from 1 to 5",
                    )
                })?,
        ),
    };
    let wanted_network = query.get("network");
    let wanted_category = query.get("category");

    let issue = resolve(data, &query).await?;
    let networks = networks(&issue.index);
    let categories = categories(&issue.index);
    if let Some(wanted) = &wanted_network {
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
    if let Some(wanted) = &wanted_category {
        if !categories.contains(&wanted.as_str()) {
            return Err(HttpError::new(
                400,
                "invalid_parameter",
                format!("category must be one of the index's categories, got {wanted}"),
            )
            .with_detail(json!({ "available": categories })));
        }
    }

    let rows = rows(&issue, &networks)?;
    let mut kept: Vec<Row> = rows
        .into_iter()
        .filter(|row| match &wanted_network {
            Some(wanted) => text(&networks[row.network], "id") == wanted,
            None => true,
        })
        .filter(|row| match &wanted_category {
            Some(wanted) => row.category == wanted,
            None => true,
        })
        .filter(|row| level.is_none_or(|level| row.level >= level))
        .filter(|row| match spatial.bbox {
            Some(query) => row.bbox.is_some_and(|bbox| boxes_meet(query, bbox)),
            None => true,
        })
        .collect();
    if let Some((lat, lon)) = spatial.near {
        // The rows whose box holds the point, then each one's own polygon
        // where it has one; an area-coded alert stands on its box.
        kept.retain(|row| row.contains_point(lat, lon));
        let lines = join_all(
            kept.iter()
                .map(|row| read_line(data, &issue, &networks, row)),
        )
        .await;
        let mut inside = Vec::with_capacity(kept.len());
        for (row, line) in kept.into_iter().zip(lines) {
            let line = line?;
            let keep = match line.get("geometry") {
                Some(geometry) if !geometry.is_null() => geometry_contains(geometry, lat, lon),
                _ => true,
            };
            if keep {
                inside.push(row);
            }
        }
        kept = inside;
    }
    if let Some(limit) = spatial.limit {
        kept.truncate(limit);
    }
    let alerts: Vec<Value> = kept.iter().map(|row| row.json(&networks)).collect();
    let ended: Vec<Value> = ended(&issue.index, &networks)
        .into_iter()
        .filter(|item| match &wanted_network {
            Some(wanted) => item["network"].as_str() == Some(wanted.as_str()),
            None => true,
        })
        .collect();

    let mut body = issue.envelope();
    body.insert(
        "request".into(),
        json!({
            "network": wanted_network,
            "category": wanted_category,
            "level": level,
            "lat": spatial.near.map(|(lat, _)| lat),
            "lon": spatial.near.map(|(_, lon)| lon),
            "bbox": spatial.bbox,
            "limit": spatial.limit,
        }),
    );
    body.insert(
        "networks".into(),
        Value::Array(
            networks
                .iter()
                .filter(|network| match &wanted_network {
                    Some(wanted) => text(network, "id") == wanted,
                    None => true,
                })
                .map(|network| network_json(&issue, network))
                .collect(),
        ),
    );
    body.insert("categories".into(), json!(categories));
    body.insert("count".into(), json!(alerts.len()));
    body.insert("alerts".into(), Value::Array(alerts));
    body.insert("ended".into(), Value::Array(ended));
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// `GET /v1/alerts/{id}`: one alert in force, its line verbatim with its
/// row's box added.
pub async fn alert(data: &Data, id: &str, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let issue = resolve(data, &query).await?;
    let networks = networks(&issue.index);
    let rows = rows(&issue, &networks)?;
    let Some(row) = rows.iter().find(|row| row.id == id) else {
        let mut error = HttpError::new(
            404,
            "unknown_alert",
            format!("{} has no alert {id} in force", issue.id),
        );
        // An alert that left with this round says how.
        if let Some(item) = ended(&issue.index, &networks)
            .into_iter()
            .find(|item| item["id"].as_str() == Some(id))
        {
            error = error.with_detail(json!({ "ended": item }));
        }
        return Err(error);
    };
    let mut line = read_line(data, &issue, &networks, row).await?;
    if let Value::Object(map) = &mut line {
        map.insert("bbox".into(), json!(row.bbox));
    }
    let mut body: Map<String, Value> = issue.envelope();
    body.insert(
        "network".into(),
        network_json(&issue, &networks[row.network]),
    );
    body.insert("alert".into(), line);
    Ok((Value::Object(body), issue.id))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_polygon_holds_its_inside_and_not_its_hole() {
        let square =
            |w: f64, s: f64, e: f64, n: f64| json!([[w, s], [e, s], [e, n], [w, n], [w, s]]);
        let polygon = json!({ "type": "Polygon", "coordinates": [square(0.0, 0.0, 4.0, 4.0), square(1.0, 1.0, 2.0, 2.0)] });
        assert!(geometry_contains(&polygon, 3.0, 3.0));
        assert!(!geometry_contains(&polygon, 1.5, 1.5));
        assert!(!geometry_contains(&polygon, 5.0, 3.0));
        let multi = json!({
            "type": "MultiPolygon",
            "coordinates": [[square(0.0, 0.0, 1.0, 1.0)], [square(10.0, 10.0, 11.0, 11.0)]]
        });
        assert!(geometry_contains(&multi, 10.5, 10.5));
        assert!(!geometry_contains(&multi, 5.0, 5.0));
        assert!(!geometry_contains(&Value::Null, 0.5, 0.5));
    }

    #[test]
    fn boxes_meet_across_the_antimeridian() {
        let aleutians = [172.0, 51.0, 179.9, 53.0];
        let alaska = [-170.0, 55.0, -160.0, 60.0];
        let pacific = [170.0, 40.0, -150.0, 70.0];
        assert!(boxes_meet(pacific, aleutians));
        assert!(boxes_meet(pacific, alaska));
        assert!(!boxes_meet([0.0, 40.0, 10.0, 70.0], alaska));
        assert!(!boxes_meet(pacific, [-140.0, 55.0, -130.0, 60.0]));
        assert!(boxes_meet([-180.0, -90.0, 180.0, 90.0], alaska));
        assert!(!boxes_meet([-180.0, 0.0, 180.0, 10.0], alaska));
    }

    #[test]
    fn an_areas_path_is_resolved_against_the_data_root() {
        assert_eq!(
            resolve_path(
                "alerts.202611010005",
                "../alerts-areas/nws-zones.20260416.pmtiles"
            ),
            "alerts-areas/nws-zones.20260416.pmtiles"
        );
        assert_eq!(
            resolve_path(
                "alerts/2026/11/01/alerts.202611010005",
                "../../../../../alerts-areas/nws-zones.20260416.pmtiles"
            ),
            "alerts-areas/nws-zones.20260416.pmtiles"
        );
        assert_eq!(resolve_path("", "x.pmtiles"), "x.pmtiles");
    }
}
