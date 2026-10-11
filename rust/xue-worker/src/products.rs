//! What the four point products share: the pointer → index resolution, the
//! `issue=` pin, the spatial selection of an index's stations and the ranged
//! read of one station's line.
//!
//! A point product (`docs/sounding.md`, `docs/airport.md`, `docs/synop.md`,
//! `docs/tc.md`) is a mutable pointer `latest-<product>.json` naming an
//! immutable `<product>.<issue>/index.json`, with the stations' records
//! beside it in one NDJSON file (or one JSON file per storm) that the index
//! addresses by byte span. The handlers in `sounding`, `airport`, `synop` and
//! `tc` turn those bytes into the API's shapes; nothing here knows a
//! product's fields.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;

/// A product the API serves, and where.
pub struct Product {
    pub id: &'static str,
    /// The API route that lists it, `None` for a product the API reads no
    /// further than its Collection.
    pub endpoint: Option<&'static str>,
    /// Issues filed under `<product>/<YYYY>/<MM>/<DD>/` (the hourly products
    /// keep an archive); the ten-minute rounds sit flat under the root.
    archived: bool,
}

pub const PRODUCTS: [Product; 5] = [
    Product {
        id: "sounding",
        endpoint: Some("/v1/soundings"),
        archived: true,
    },
    Product {
        id: "airport",
        endpoint: Some("/v1/airports"),
        archived: false,
    },
    Product {
        id: "synop",
        endpoint: Some("/v1/synop"),
        archived: false,
    },
    Product {
        id: "tc",
        endpoint: Some("/v1/storms"),
        archived: true,
    },
    Product {
        id: "nexrad",
        endpoint: None,
        archived: false,
    },
];

pub fn product(id: &str) -> Option<&'static Product> {
    PRODUCTS.iter().find(|product| product.id == id)
}

#[derive(Debug, Deserialize)]
struct Pointer {
    path: String,
    crc32: String,
}

/// One resolved issue: its id, the directory its files sit in and the index.
pub struct Issue {
    pub product: &'static Product,
    /// `sounding.2026091402`, `airport.202609161440`: the STAC Item id.
    pub id: String,
    /// The index's directory under the data root, `""` for the root.
    pub dir: String,
    pub index: Value,
}

impl Issue {
    pub fn path(&self, file: &str) -> String {
        if self.dir.is_empty() {
            file.to_owned()
        } else {
            format!("{}/{file}", self.dir)
        }
    }

    pub fn issued(&self) -> Value {
        self.index.get("issued").cloned().unwrap_or(Value::Null)
    }

    pub fn generated(&self) -> Value {
        self.index.get("generated").cloned().unwrap_or(Value::Null)
    }

    pub fn sources(&self) -> Value {
        self.index.get("sources").cloned().unwrap_or(Value::Null)
    }

    /// The response fields every product answer opens with.
    pub fn envelope(&self) -> Map<String, Value> {
        let mut map = Map::new();
        map.insert("product".into(), json!(self.product.id));
        map.insert("issue".into(), json!(self.id));
        map.insert("issued".into(), self.issued());
        map.insert("generated".into(), self.generated());
        map
    }

    /// A file the index names beside itself (`soundings.jsonl`, the storm's
    /// `EP142026.json`): its `{path, byteLength, crc32}` descriptor.
    pub fn file(&self, descriptor: &Value, what: &str) -> Result<(String, String), HttpError> {
        let path = descriptor.get("path").and_then(Value::as_str);
        let crc32 = descriptor.get("crc32").and_then(Value::as_str);
        match (path, crc32) {
            (Some(path), Some(crc32)) => Ok((self.path(path), crc32.to_owned())),
            _ => Err(HttpError::new(
                502,
                "upstream_failed",
                format!("{} names no {what} file", self.id),
            )),
        }
    }
}

/// `<product>.<10 or 12 digits>`: an issue id a client may pin.
fn issue_digits<'a>(product: &Product, issue: &'a str) -> Option<&'a str> {
    let digits = issue.strip_prefix(product.id)?.strip_prefix('.')?;
    ((digits.len() == 10 || digits.len() == 12) && digits.bytes().all(|b| b.is_ascii_digit()))
        .then_some(digits)
}

/// Resolve the live issue through `latest-<product>.json`, or a pinned one
/// by its id. A pinned index is read uncached (its crc is not known up
/// front); the live one is immutable for the pointer's crc.
pub async fn resolve_issue(
    data: &Data,
    product: &'static Product,
    issue: Option<&str>,
) -> Result<Issue, HttpError> {
    if let Some(issue) = issue {
        let digits = issue_digits(product, issue).ok_or_else(|| {
            HttpError::new(
                400,
                "invalid_parameter",
                format!("invalid issue {issue}: expected {}.<YYYYMMDDHH[MM]>", product.id),
            )
        })?;
        let not_found = || {
            HttpError::new(
                404,
                "unknown_issue",
                format!("{} publishes no issue {issue}", product.id),
            )
        };
        // The hourly products file an issue under its UTC day; one published
        // before that layout, and every ten-minute round, sits flat.
        let mut candidates = Vec::new();
        if product.archived {
            candidates.push(format!(
                "{}/{}/{}/{}/{issue}",
                product.id,
                &digits[0..4],
                &digits[4..6],
                &digits[6..8]
            ));
        }
        candidates.push(issue.to_owned());
        for dir in candidates {
            match data.value(&format!("{dir}/index.json")).await {
                Ok(index) => {
                    return Ok(Issue {
                        product,
                        id: issue.to_owned(),
                        dir,
                        index,
                    })
                }
                Err(error) if error.status == 404 => continue,
                Err(error) => return Err(error),
            }
        }
        return Err(not_found());
    }

    let pointer: Pointer = data
        .json(&format!("latest-{}.json", product.id))
        .await
        .map_err(|error| {
            if error.status == 404 {
                HttpError::new(
                    404,
                    "no_live_run",
                    format!("{} publishes no live issue", product.id),
                )
            } else {
                error
            }
        })?;
    let index: Value = data.json_cached(&pointer.path, &pointer.crc32).await?;
    let dir = pointer
        .path
        .rsplit_once('/')
        .map(|(dir, _)| dir.to_owned())
        .unwrap_or_default();
    let id = dir.rsplit('/').next().unwrap_or(&dir).to_owned();
    Ok(Issue {
        product,
        id,
        dir,
        index,
    })
}

/// One station's line, sliced by the span the index gives and parsed. The
/// file is immutable for its crc, so the slice is held in the isolate.
pub async fn read_slice(
    data: &Data,
    path: &str,
    crc32: &str,
    row: &Value,
    offset_key: usize,
) -> Result<Value, HttpError> {
    let span = |index: usize| -> Option<u64> {
        match row {
            Value::Array(items) => items.get(index).and_then(Value::as_u64),
            Value::Object(map) => map
                .get(if index == offset_key { "offset" } else { "length" })
                .and_then(Value::as_u64),
            _ => None,
        }
    };
    let (Some(offset), Some(length)) = (span(offset_key), span(offset_key + 1)) else {
        return Err(HttpError::new(
            502,
            "upstream_failed",
            format!("{path}: the index row carries no byte span"),
        ));
    };
    let bytes = data.range_cached(path, offset, length, crc32).await?;
    serde_json::from_slice(&bytes).map_err(|error| {
        HttpError::new(
            502,
            "upstream_failed",
            format!("{path}[{offset}+{length}] is not a JSON object: {error}"),
        )
    })
}

/// A query-string accessor with the contract's error for a malformed value.
pub struct Query<'a> {
    url: &'a Url,
}

impl<'a> Query<'a> {
    pub fn new(url: &'a Url) -> Query<'a> {
        Query { url }
    }

    pub fn get(&self, key: &str) -> Option<String> {
        self.url
            .query_pairs()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.trim().to_owned())
            .filter(|value| !value.is_empty())
    }

    pub fn number(&self, key: &str) -> Result<Option<f64>, HttpError> {
        match self.get(key) {
            None => Ok(None),
            Some(text) => text
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite())
                .map(Some)
                .ok_or_else(|| {
                    HttpError::new(400, "invalid_parameter", format!("{key} must be a number"))
                }),
        }
    }

    pub fn count(&self, key: &str) -> Result<Option<usize>, HttpError> {
        match self.get(key) {
            None => Ok(None),
            Some(text) => text
                .parse::<usize>()
                .ok()
                .filter(|value| *value >= 1)
                .map(Some)
                .ok_or_else(|| {
                    HttpError::new(
                        400,
                        "invalid_parameter",
                        format!("{key} must be a positive integer"),
                    )
                }),
        }
    }

    /// `time=`, as milliseconds since the epoch.
    pub fn time(&self) -> Result<Option<f64>, HttpError> {
        match self.get("time") {
            None => Ok(None),
            Some(text) => zarr::parse_iso(&text).map(Some).ok_or_else(|| {
                HttpError::new(
                    400,
                    "invalid_parameter",
                    format!("time must be ISO 8601, got {text}"),
                )
            }),
        }
    }

    pub fn issue(&self) -> Option<String> {
        self.get("issue")
    }
}

/// The spatial selection a list endpoint accepts: a point to sort by
/// distance from (with an optional radius), a bounding box, a row cap.
pub struct Spatial {
    pub near: Option<(f64, f64)>,
    pub radius_km: Option<f64>,
    pub bbox: Option<[f64; 4]>,
    pub limit: Option<usize>,
}

impl Spatial {
    pub fn parse(query: &Query) -> Result<Spatial, HttpError> {
        let lat = query.number("lat")?;
        let lon = query.number("lon")?;
        let near = match (lat, lon) {
            (None, None) => None,
            (Some(lat), Some(lon)) => {
                if !(-90.0..=90.0).contains(&lat) {
                    return Err(HttpError::new(
                        400,
                        "invalid_parameter",
                        "lat must be within [-90, 90]",
                    ));
                }
                if !(-180.0..=360.0).contains(&lon) {
                    return Err(HttpError::new(
                        400,
                        "invalid_parameter",
                        "lon must be within [-180, 360]",
                    ));
                }
                Some((lat, if lon > 180.0 { lon - 360.0 } else { lon }))
            }
            _ => {
                return Err(HttpError::new(
                    400,
                    "invalid_parameter",
                    "lat and lon go together",
                ))
            }
        };
        let radius_km = query.number("radius")?;
        if let Some(radius) = radius_km {
            if near.is_none() {
                return Err(HttpError::new(
                    400,
                    "invalid_parameter",
                    "radius needs lat and lon",
                ));
            }
            if radius <= 0.0 {
                return Err(HttpError::new(
                    400,
                    "invalid_parameter",
                    "radius must be positive (km)",
                ));
            }
        }
        let bbox = match query.get("bbox") {
            None => None,
            Some(text) => {
                let parts: Vec<f64> = text
                    .split(',')
                    .filter_map(|part| part.trim().parse::<f64>().ok())
                    .filter(|value| value.is_finite())
                    .collect();
                if parts.len() != 4
                    || !(-90.0..=90.0).contains(&parts[1])
                    || !(-90.0..=90.0).contains(&parts[3])
                    || parts[1] > parts[3]
                {
                    return Err(HttpError::new(
                        400,
                        "invalid_parameter",
                        "bbox must be west,south,east,north in degrees",
                    ));
                }
                Some([parts[0], parts[1], parts[2], parts[3]])
            }
        };
        Ok(Spatial {
            near,
            radius_km,
            bbox,
            limit: query.count("limit")?,
        })
    }

    /// The request's spatial terms, echoed in the response.
    pub fn request_json(&self) -> Value {
        json!({
            "lat": self.near.map(|(lat, _)| lat),
            "lon": self.near.map(|(_, lon)| lon),
            "radiusKm": self.radius_km,
            "bbox": self.bbox,
            "limit": self.limit,
        })
    }

    fn in_bbox(&self, lat: f64, lon: f64) -> bool {
        let Some([west, south, east, north]) = self.bbox else {
            return true;
        };
        if lat < south || lat > north {
            return false;
        }
        let wrap = |value: f64| ((value + 180.0).rem_euclid(360.0)) - 180.0;
        let (west, east, lon) = (wrap(west), wrap(east), wrap(lon));
        // A box across the antimeridian has west > east.
        if west <= east {
            lon >= west && lon <= east
        } else {
            lon >= west || lon <= east
        }
    }

    /// Keep the admitted rows, each with its distance from `near` (km) when
    /// one was given, nearest first then; capped at `limit`.
    pub fn select<T>(
        &self,
        rows: Vec<T>,
        position: impl Fn(&T) -> Option<(f64, f64)>,
    ) -> Vec<(T, Option<f64>)> {
        let mut kept: Vec<(T, Option<f64>)> = rows
            .into_iter()
            .filter_map(|row| {
                let (lat, lon) = position(&row)?;
                if !self.in_bbox(lat, lon) {
                    return None;
                }
                let distance = self
                    .near
                    .map(|(near_lat, near_lon)| haversine_km(near_lat, near_lon, lat, lon));
                if let (Some(radius), Some(distance)) = (self.radius_km, distance) {
                    if distance > radius {
                        return None;
                    }
                }
                Some((row, distance))
            })
            .collect();
        if self.near.is_some() {
            kept.sort_by(|a, b| {
                a.1.unwrap_or(f64::INFINITY)
                    .total_cmp(&b.1.unwrap_or(f64::INFINITY))
            });
        }
        if let Some(limit) = self.limit {
            kept.truncate(limit);
        }
        kept
    }
}

/// Great-circle distance in kilometres (mean Earth radius 6371 km).
pub fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (phi1, phi2) = (lat1.to_radians(), lat2.to_radians());
    let d_phi = (lat2 - lat1).to_radians();
    let d_lambda = (lon2 - lon1).to_radians();
    let a = (d_phi / 2.0).sin().powi(2) + phi1.cos() * phi2.cos() * (d_lambda / 2.0).sin().powi(2);
    2.0 * 6371.0 * a.sqrt().asin()
}

/// A distance for the response: a tenth of a kilometre is plenty.
pub fn round_km(distance: Option<f64>) -> Value {
    match distance {
        Some(km) => json!((km * 10.0).round() / 10.0),
        None => Value::Null,
    }
}

/// The index of the instant nearest `target` (ms), ties to the earlier.
pub fn nearest(times: &[f64], target: f64) -> Option<usize> {
    let mut best: Option<(usize, f64)> = None;
    for (index, &time) in times.iter().enumerate() {
        let distance = (time - target).abs();
        if best.is_none_or(|(_, held)| distance < held) {
            best = Some((index, distance));
        }
    }
    best.map(|(index, _)| index)
}

/// A fixed-point integer with the products' missing value, as a JSON number
/// scaled by `scale` and rounded to `decimals` (or `null`). An unscaled
/// value stays an integer.
pub fn fixed(value: &Value, scale: f64, decimals: i32) -> Value {
    match value.as_i64() {
        Some(-32768) | None => Value::Null,
        Some(code) if scale == 1.0 => json!(code),
        Some(code) => {
            let factor = 10f64.powi(decimals);
            json!(((code as f64) * scale * factor).round() / factor)
        }
    }
}

/// `v` as a string field of an object, `""` when absent.
pub fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or_default()
}
