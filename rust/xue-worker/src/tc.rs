//! The tropical cyclones (`docs/tc.md`): the issue's storm list, and one
//! storm's document with its ensembles unpacked from the flattened
//! fixed-point arrays into one track per member.

use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::products::{self, text, Issue, Query};

const PRODUCT: &str = "tc";

/// The storm document's sections `include=` may narrow to.
const SECTIONS: [&str; 5] = ["best", "agencies", "models", "alert", "impact"];

async fn resolve(data: &Data, query: &Query<'_>) -> Result<Issue, HttpError> {
    products::resolve_issue(
        data,
        products::product(PRODUCT).expect("registered"),
        query.issue().as_deref(),
    )
    .await
}

fn storms(index: &Value) -> Vec<Value> {
    index
        .get("storms")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

/// An index row without the file descriptor (the product's addressing).
fn storm_json(row: &Value) -> Value {
    let mut out = row.as_object().cloned().unwrap_or_default();
    for key in ["path", "byteLength", "crc32"] {
        out.remove(key);
    }
    Value::Object(out)
}

/// `GET /v1/storms`: the systems of the live (or pinned) issue.
pub async fn list(data: &Data, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let basin = query.get("basin").map(|value| value.to_ascii_uppercase());
    let level = query.get("level").map(|value| value.to_ascii_uppercase());
    let issue = resolve(data, &query).await?;
    let rows: Vec<Value> = storms(&issue.index)
        .iter()
        .filter(|row| basin.as_deref().is_none_or(|wanted| text(row, "basin") == wanted))
        .filter(|row| level.as_deref().is_none_or(|wanted| text(row, "level") == wanted))
        .map(storm_json)
        .collect();
    let mut body = issue.envelope();
    body.insert(
        "request".into(),
        json!({ "basin": basin, "level": level }),
    );
    body.insert("count".into(), json!(rows.len()));
    body.insert("storms".into(), Value::Array(rows));
    body.insert(
        "crosswalk".into(),
        issue.index.get("crosswalk").cloned().unwrap_or(json!({})),
    );
    body.insert("sources".into(), issue.sources());
    Ok((Value::Object(body), issue.id))
}

/// A member set (`leads`, `members`, flattened member-major arrays) as one
/// track per member: `lat`/`lon` ÷ 100, `vmax`/`pmin` ÷ 10, −32768 missing.
/// A lead at which a member has no position is left out of its track.
fn unpack_ensemble(ensemble: &Value) -> Value {
    let leads: Vec<i64> = ensemble
        .get("leads")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_i64).collect())
        .unwrap_or_default();
    let members: Vec<Value> = ensemble
        .get("members")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let array = |key: &str| -> Vec<Value> {
        ensemble
            .get(key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    };
    let (lat, lon, vmax, pmin) = (array("lat"), array("lon"), array("vmax"), array("pmin"));
    let base_ms = zarr::parse_iso(text(ensemble, "base"));
    let width = leads.len();
    let tracks: Vec<Value> = members
        .iter()
        .enumerate()
        .map(|(member_position, member)| {
            let points: Vec<Value> = leads
                .iter()
                .enumerate()
                .filter_map(|(lead_position, &lead)| {
                    let index = member_position * width + lead_position;
                    let latitude = products::fixed(lat.get(index)?, 0.01, 2);
                    let longitude = products::fixed(lon.get(index)?, 0.01, 2);
                    if latitude.is_null() || longitude.is_null() {
                        return None;
                    }
                    Some(json!({
                        "lead": lead,
                        "time": base_ms.map(|base| zarr::format_iso_millis(base + lead as f64 * 1000.0)),
                        "lat": latitude,
                        "lon": longitude,
                        "vmax": vmax.get(index).map(|v| products::fixed(v, 0.1, 1)).unwrap_or(Value::Null),
                        "pmin": pmin.get(index).map(|v| products::fixed(v, 0.1, 1)).unwrap_or(Value::Null),
                    }))
                })
                .collect();
            json!({ "member": member, "points": points })
        })
        .collect();
    let mut out = Map::new();
    for key in ["issued", "base", "run"] {
        out.insert(key.to_owned(), ensemble.get(key).cloned().unwrap_or(Value::Null));
    }
    out.insert("leads".into(), json!(leads));
    out.insert("members".into(), Value::Array(tracks));
    out.insert(
        "mean".into(),
        ensemble.get("mean").cloned().unwrap_or(Value::Null),
    );
    Value::Object(out)
}

fn is_ensemble(forecast: &Value) -> bool {
    forecast.get("members").is_some_and(Value::is_array)
        && forecast.get("leads").is_some_and(Value::is_array)
}

/// `GET /v1/storms/{storm}`: one system's document. `storm` is its id, an
/// id the issue's `crosswalk` maps onto a current one, or one of the
/// storm's aliases (an invest number, a model's own number).
pub async fn storm(data: &Data, id: &str, url: &Url) -> Result<(Value, String), HttpError> {
    let query = Query::new(url);
    let include: Option<Vec<String>> = query.get("include").map(|value| {
        value
            .split(',')
            .map(|part| part.trim().to_owned())
            .filter(|part| !part.is_empty())
            .collect()
    });
    if let Some(include) = &include {
        if let Some(unknown) = include.iter().find(|part| !SECTIONS.contains(&part.as_str())) {
            return Err(HttpError::new(
                400,
                "invalid_parameter",
                format!("include names no section {unknown}"),
            )
            .with_detail(json!({ "available": SECTIONS })));
        }
    }
    let issue = resolve(data, &query).await?;
    let rows = storms(&issue.index);
    let crosswalk = issue.index.get("crosswalk").and_then(Value::as_object);

    // Ids are matched without regard to case: ATCF ids are upper case and
    // the synthetic ones lower, and a client should not have to know which.
    let same = |a: &str, b: &str| a.eq_ignore_ascii_case(b);
    let mut resolved_from: Option<&str> = None;
    let mut wanted = id;
    if !rows.iter().any(|row| same(text(row, "id"), wanted)) {
        if let Some(target) = crosswalk
            .and_then(|map| map.iter().find(|(old, _)| same(old, wanted)))
            .and_then(|(_, target)| target.as_str())
        {
            resolved_from = Some(id);
            wanted = target;
        }
    }
    let row = rows
        .iter()
        .find(|row| same(text(row, "id"), wanted))
        .or_else(|| {
            rows.iter().find(|row| {
                row.get("aliases")
                    .and_then(Value::as_object)
                    .is_some_and(|aliases| aliases.keys().any(|alias| same(alias, id)))
            })
        })
        .ok_or_else(|| {
            let available: Vec<&str> = rows.iter().map(|row| text(row, "id")).collect();
            HttpError::new(
                404,
                "unknown_storm",
                format!("{} has no storm {id}", issue.id),
            )
            .with_detail(json!({ "available": available }))
        })?;
    if !same(text(row, "id"), id) && resolved_from.is_none() {
        resolved_from = Some(id);
    }

    let (path, crc32) = issue.file(row, "storm")?;
    let document: Value = data.json_cached(&path, &crc32).await?;

    let mut body = issue.envelope();
    body.insert("resolvedFrom".into(), json!(resolved_from));
    for key in ["id", "level", "basin", "name", "sid", "intl", "aliases"] {
        body.insert(key.to_owned(), document.get(key).cloned().unwrap_or(Value::Null));
    }
    for section in SECTIONS {
        if include.as_ref().is_some_and(|wanted| !wanted.iter().any(|part| part == section)) {
            continue;
        }
        let mut value = document.get(section).cloned().unwrap_or(Value::Null);
        if section == "models" {
            if let Some(models) = value.as_object_mut() {
                for forecast in models.values_mut() {
                    if is_ensemble(forecast) {
                        *forecast = unpack_ensemble(forecast);
                    }
                }
            }
        }
        body.insert(section.to_owned(), value);
    }
    body.insert("sources".into(), document.get("sources").cloned().unwrap_or(Value::Null));
    Ok((Value::Object(body), issue.id))
}
