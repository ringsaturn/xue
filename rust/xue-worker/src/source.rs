//! The catalog and one source's live summary.
//!
//! The cheap place to learn a source's variables is the live STAC Item
//! (`<source>/item.json`): its `cube:variables` is one entry per array the run
//! publishes (a vector bundle's components carry `xue:bundle`), so one read
//! gives the whole vocabulary `/v1/point?variables=` accepts. The Collection
//! does not list variables; the manifest only names bundle ids.

use serde_json::{json, Map, Value};
use worker::Response;

use crate::bucket::Data;
use crate::error::{json as json_response, HttpError};
use crate::point::{resolve_run, Run};

pub async fn catalog(data: &Data) -> Result<Response, HttpError> {
    let root = data.value("catalog.json").await?;
    let collections: Vec<Value> = root
        .get("links")
        .and_then(Value::as_array)
        .map(|links| {
            links
                .iter()
                .filter(|link| link.get("rel").and_then(Value::as_str) == Some("child"))
                .map(|link| {
                    let href = link.get("href").and_then(Value::as_str).unwrap_or_default();
                    json!({
                        "id": href.split('/').next().unwrap_or_default(),
                        "title": link.get("title").cloned().unwrap_or(Value::Null),
                        "href": href,
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    let body = json!({ "dataOrigin": data.origin, "collections": collections });
    json_response(&body, 200, Some("public, max-age=60"), &[])
}

pub async fn detail(data: &Data, source: &str) -> Result<Response, HttpError> {
    let summary = read_source(data, source).await?;
    json_response(&summary, 200, Some("public, max-age=60"), &[])
}

pub async fn read_source(data: &Data, source: &str) -> Result<Value, HttpError> {
    let collection = data
        .value(&format!("{source}/collection.json"))
        .await
        .map_err(|error| {
            if error.status == 404 {
                HttpError::new(404, "unknown_source", format!("unknown source {source}"))
            } else {
                error
            }
        })?;
    let mut summary = Map::new();
    summary.insert("source".into(), json!(source));
    summary.insert(
        "title".into(),
        collection.get("title").cloned().unwrap_or(Value::Null),
    );
    summary.insert(
        "description".into(),
        collection
            .get("description")
            .cloned()
            .unwrap_or(Value::Null),
    );
    summary.insert(
        "license".into(),
        collection.get("license").cloned().unwrap_or(Value::Null),
    );
    summary.insert(
        "run".into(),
        collection.get("xue:live").cloned().unwrap_or(Value::Null),
    );
    // `bundles` is the request vocabulary (`/v1/point?variables=` ids) and
    // `variables` the flat per-variable metadata; both are filled below.
    summary.insert("bundles".into(), json!([]));
    summary.insert("variables".into(), json!({}));

    match data.value(&format!("{source}/item.json")).await {
        Ok(item) => {
            for (key, pointer) in [
                ("run", "/id"),
                ("bbox", "/bbox"),
                ("runTime", "/properties/forecast:reference_datetime"),
            ] {
                if let Some(value) = item.pointer(pointer) {
                    summary.insert(key.into(), value.clone());
                }
            }
            if !summary.contains_key("runTime") {
                if let Some(value) = item.pointer("/properties/datetime") {
                    summary.insert("runTime".into(), value.clone());
                }
            }
            if let Some(value) = item.pointer("/properties/cube:dimensions/time") {
                summary.insert("time".into(), value.clone());
            }
            let (bundles, variables) = variables_from_item(&item);
            summary.insert("bundles".into(), json!(bundles));
            summary.insert("variables".into(), Value::Object(variables));
        }
        Err(_) => {
            // No live STAC Item (an older bucket or a local build): the
            // manifest names the bundle ids but not their variables, so only
            // the request vocabulary can be filled.
            if let Ok(Run { manifest, .. }) = resolve_run(data, source, None).await {
                let bundles: Vec<&str> = manifest
                    .bundles
                    .iter()
                    .map(|bundle| bundle.variable.as_str())
                    .collect();
                summary.insert("bundles".into(), json!(bundles));
            }
        }
    }
    Ok(Value::Object(summary))
}

/// The request vocabulary and the flat variable table an Item carries:
/// `bundles` is the list of bundle ids a caller may request, and `variables` is
/// keyed by array id with the bundle each belongs to, so a vector bundle's
/// components are separate entries rather than a nested `components` object.
fn variables_from_item(item: &Value) -> (Vec<String>, Map<String, Value>) {
    let mut bundles: Vec<String> = Vec::new();
    let mut variables = Map::new();
    if let Some(cube) = item
        .pointer("/properties/cube:variables")
        .and_then(Value::as_object)
    {
        for (array_id, variable) in cube {
            let bundle = variable
                .get("xue:bundle")
                .and_then(Value::as_str)
                .unwrap_or(array_id);
            if !bundles.iter().any(|held| held == bundle) {
                bundles.push(bundle.to_owned());
            }
            variables.insert(
                array_id.clone(),
                json!({
                    "bundle": bundle,
                    "label": variable
                        .get("description")
                        .or_else(|| variable.get("title"))
                        .cloned()
                        .unwrap_or(Value::Null),
                    "unit": variable.get("unit").cloned().unwrap_or(Value::Null),
                }),
            );
        }
    }
    (bundles, variables)
}
