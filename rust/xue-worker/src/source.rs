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
    summary.insert("bundles".into(), Value::Null);

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
            summary.insert("bundles".into(), bundles_from_item(&item));
        }
        Err(_) => {
            // No live STAC Item (an older bucket or a local build): fall back to
            // the manifest's bundle ids, which the point path already reads.
            if let Ok(Run { manifest, .. }) = resolve_run(data, source, None).await {
                let bundles: Map<String, Value> = manifest
                    .bundles
                    .iter()
                    .map(|bundle| {
                        (
                            bundle.variable.clone(),
                            json!({ "label": Value::Null, "unit": Value::Null }),
                        )
                    })
                    .collect();
                summary.insert("bundles".into(), Value::Object(bundles));
            }
        }
    }
    Ok(Value::Object(summary))
}

/// Group an Item's `cube:variables` by the bundle each array belongs to.
fn bundles_from_item(item: &Value) -> Value {
    let mut groups: Vec<(String, Vec<(&str, Value)>)> = Vec::new();
    if let Some(variables) = item
        .pointer("/properties/cube:variables")
        .and_then(Value::as_object)
    {
        for (array_id, variable) in variables {
            let bundle_id = variable
                .get("xue:bundle")
                .and_then(Value::as_str)
                .unwrap_or(array_id);
            let record = json!({
                "id": array_id,
                "label": variable
                    .get("description")
                    .or_else(|| variable.get("title"))
                    .cloned()
                    .unwrap_or(Value::Null),
                "unit": variable.get("unit").cloned().unwrap_or(Value::Null),
            });
            match groups.iter_mut().find(|(id, _)| id == bundle_id) {
                Some((_, arrays)) => arrays.push((array_id, record)),
                None => groups.push((bundle_id.to_owned(), vec![(array_id, record)])),
            }
        }
    }
    let bundles: Map<String, Value> = groups
        .into_iter()
        .map(|(bundle_id, arrays)| {
            let only = &arrays[0].1;
            let entry = if arrays.len() == 1
                && only.get("id").and_then(Value::as_str) == Some(bundle_id.as_str())
            {
                json!({
                    "label": only.get("label").cloned().unwrap_or(Value::Null),
                    "unit": only.get("unit").cloned().unwrap_or(Value::Null),
                })
            } else {
                let components: Map<String, Value> = arrays
                    .iter()
                    .map(|(id, record)| {
                        (
                            (*id).to_owned(),
                            json!({
                                "label": record.get("label").cloned().unwrap_or(Value::Null),
                                "unit": record.get("unit").cloned().unwrap_or(Value::Null),
                            }),
                        )
                    })
                    .collect();
                json!({ "components": components })
            };
            (bundle_id, entry)
        })
        .collect();
    Value::Object(bundles)
}
