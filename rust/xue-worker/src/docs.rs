//! The machine-readable contract.
//!
//! `static/openapi.json` is the base OpenAPI document, compiled into the
//! Worker with `include_str!`. It is enriched at serve time from the live
//! dataset: the `source` parameter's enum becomes the live Collections, and
//! `variables` becomes a comma-separated array (no enum — the vocabulary is
//! per source and a union would be hundreds of entries). The exact per-source
//! lists ride the `x-xue-bundles-by-source` extension; `GET
//! /v1/sources/{source}` is the same truth at a URL. OpenAPI cannot vary one
//! parameter's enum by another, so this is the closest a single document gets
//! to "the bundles of the source you picked". The enriched document is held
//! briefly per isolate and cached a minute, so a docs request does not re-read
//! every source.
//!
//! The Swagger UI itself (`public/`) is served as a Worker static asset, not
//! compiled into the wasm.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use futures_util::future::join_all;
use serde_json::{json, Value};
use worker::{Headers, Response};

use crate::bucket::Data;
use crate::error::HttpError;

const OPENAPI: &str = include_str!("../static/openapi.json");

/// How long an isolate reuses a generated contract.
const SPEC_TTL_MS: f64 = 60_000.0;

thread_local! {
    static SPEC: RefCell<Option<(f64, String)>> = const { RefCell::new(None) };
}

fn json(body: &[u8], cache_control: &str) -> Result<Response, HttpError> {
    let headers = Headers::new();
    headers
        .set("content-type", "application/json; charset=utf-8")
        .map_err(HttpError::from)?;
    headers
        .set("access-control-allow-origin", "*")
        .map_err(HttpError::from)?;
    headers
        .set("cache-control", cache_control)
        .map_err(HttpError::from)?;
    Ok(Response::from_bytes(body.to_vec())
        .map_err(HttpError::from)?
        .with_headers(headers))
}

/// The contract, with the live `source` enum and bundle vocabulary filled in.
/// The embedded document is served as-is when the dataset cannot be read.
pub async fn openapi(data: &Data) -> Result<Response, HttpError> {
    let now = js_sys::Date::now();
    if let Some((generated, spec)) = SPEC.with(|cell| cell.borrow().clone()) {
        if now - generated < SPEC_TTL_MS {
            return json(spec.as_bytes(), "public, max-age=60");
        }
    }
    let spec = enrich(data).await.unwrap_or_else(|_| OPENAPI.to_owned());
    SPEC.with(|cell| *cell.borrow_mut() = Some((now, spec.clone())));
    json(spec.as_bytes(), "public, max-age=60")
}

async fn enrich(data: &Data) -> Result<String, HttpError> {
    let catalog = data.value("catalog.json").await?;
    let sources: Vec<String> = catalog
        .get("links")
        .and_then(Value::as_array)
        .map(|links| {
            links
                .iter()
                .filter(|link| link.get("rel").and_then(Value::as_str) == Some("child"))
                .filter_map(|link| link.get("href").and_then(Value::as_str))
                .filter_map(|href| href.split('/').next())
                .filter(|id| !id.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();

    let items = join_all(sources.iter().map(|source| {
        let path = format!("{source}/item.json");
        async move { data.value(&path).await.ok() }
    }))
    .await;

    let mut by_source: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (source, item) in sources.iter().zip(items) {
        let mut bundles = BTreeSet::new();
        if let Some(cube) = item
            .as_ref()
            .and_then(|item| item.pointer("/properties/cube:variables"))
            .and_then(Value::as_object)
        {
            for (array_id, variable) in cube {
                let bundle = variable
                    .get("xue:bundle")
                    .and_then(Value::as_str)
                    .unwrap_or(array_id);
                bundles.insert(bundle.to_owned());
            }
        }
        by_source.insert(source.clone(), bundles);
    }

    let mut spec: Value = serde_json::from_str(OPENAPI)
        .map_err(|error| HttpError::new(502, "internal", error.to_string()))?;
    if let Some(parameters) = spec
        .pointer_mut("/components/parameters")
        .and_then(Value::as_object_mut)
    {
        for name in ["sourcePath", "sourceQuery"] {
            if let Some(parameter) = parameters.get_mut(name) {
                parameter["schema"]["enum"] = json!(&sources);
            }
        }
        if let Some(parameter) = parameters.get_mut("variables") {
            parameter["description"] = json!(
                "Bundle ids, comma-separated (e.g. `tmp2m,wind10m`). Each source publishes its own set: see `GET /v1/sources/{source}` or this document's `x-xue-bundles-by-source`."
            );
            parameter["style"] = json!("form");
            parameter["explode"] = json!(false);
            parameter["example"] = json!(["tmp2m", "wind10m"]);
            parameter["schema"] = json!({
                "type": "array",
                "items": { "type": "string" }
            });
        }
    }
    spec["x-xue-bundles-by-source"] = json!(by_source);
    serde_json::to_string_pretty(&spec)
        .map_err(|error| HttpError::new(502, "internal", error.to_string()))
}
