//! Routing, shared by both runtimes: the Worker (`runtime.rs`) and the native
//! server (`server.rs`) parse the request, call [`dispatch`] and frame its
//! [`Answer`] in their own response type. Edge caching, CORS and the error
//! document are the framing's job; which handler answers a path, and for how
//! long the answer may be cached, are decided here once.

use serde_json::Value;
use url::Url;

use crate::bucket::Data;
use crate::error::HttpError;
use crate::{airport, docs, point, products, sounding, source, synop, tc};

/// A `200` JSON answer: the serialized body, its `Cache-Control` and any
/// further headers (`x-xue-run`, `x-xue-issue`).
pub struct Answer {
    pub body: Vec<u8>,
    pub cache_control: &'static str,
    pub headers: Vec<(&'static str, String)>,
}

impl Answer {
    fn json(value: &Value, cache_control: &'static str) -> Result<Answer, HttpError> {
        let body = serde_json::to_vec(value)
            .map_err(|error| HttpError::new(502, "internal", error.to_string()))?;
        Ok(Answer {
            body,
            cache_control,
            headers: Vec::new(),
        })
    }

    fn with_header(mut self, name: &'static str, value: String) -> Answer {
        self.headers.push((name, value));
        self
    }
}

/// The request path with trailing slashes dropped, `/` for the root.
pub fn normalize(path: &str) -> &str {
    let path = path.trim_end_matches('/');
    if path.is_empty() {
        "/"
    } else {
        path
    }
}

/// Whether a `GET` of this path may be served from a response cache: every
/// data answer, but not the contract, which is generated and held per isolate.
pub fn cacheable(path: &str) -> bool {
    path.starts_with("/v1/") && path != "/v1/openapi.json"
}

/// Answer a `GET` of `path` (already [`normalize`]d). `now` is milliseconds
/// since the epoch.
pub async fn dispatch(path: &str, url: &Url, data: &Data, now: f64) -> Result<Answer, HttpError> {
    match path {
        "/health" => {
            let body = serde_json::json!({
                "status": "ok",
                "build": env!("CARGO_PKG_VERSION"),
                "now": xue::zarr::format_iso_millis(now),
            });
            Answer::json(&body, "no-store")
        }
        "/openapi.json" | "/v1/openapi.json" => Ok(Answer {
            body: docs::openapi(data, now).await.into_bytes(),
            cache_control: "public, max-age=60",
            headers: Vec::new(),
        }),
        "/v1/catalog" => Answer::json(&source::catalog(data).await?, "public, max-age=60"),
        "/v1/point" => {
            let query = point::parse_query(url);
            let has_run = query.run.is_some();
            let (body, run) = point::read_point(data, &query).await?;
            let cache = if has_run {
                "public, max-age=86400"
            } else {
                "public, max-age=30"
            };
            Ok(Answer::json(&body, cache)?.with_header("x-xue-run", run))
        }
        "/v1/cell" => {
            // `run` is required, so the answer is immutable for its URL.
            let (body, run) = point::read_cell(data, &point::parse_cell_query(url)).await?;
            Ok(Answer::json(&body, "public, max-age=86400")?.with_header("x-xue-run", run))
        }
        "/v1/soundings" => product_json(sounding::list(data, url).await?, url),
        "/v1/airports" => product_json(airport::list(data, url).await?, url),
        "/v1/synop" => product_json(synop::list(data, url).await?, url),
        "/v1/storms" => product_json(tc::list(data, url).await?, url),
        _ => {
            if let Some(source_id) = path.strip_prefix("/v1/sources/") {
                if !source_id.is_empty() && !source_id.contains('/') {
                    return Answer::json(
                        &source::read_source(data, source_id).await?,
                        "public, max-age=60",
                    );
                }
            }
            for (prefix, product) in [
                ("/v1/soundings/", "sounding"),
                ("/v1/airports/", "airport"),
                ("/v1/synop/", "synop"),
                ("/v1/storms/", "tc"),
            ] {
                if let Some(id) = path.strip_prefix(prefix) {
                    if id.is_empty() || id.contains('/') {
                        break;
                    }
                    // The path is percent-encoded; a station id may carry a
                    // colon (`amedas:50066`) a client encoded.
                    let id = percent_encoding::percent_decode_str(id)
                        .decode_utf8()
                        .map_err(|_| {
                            HttpError::new(400, "invalid_parameter", "the id is not UTF-8")
                        })?;
                    let answer = match product {
                        "sounding" => sounding::station(data, &id, url).await?,
                        "airport" => airport::station(data, &id, url).await?,
                        "synop" => synop::station(data, &id, url).await?,
                        _ => tc::storm(data, &id, url).await?,
                    };
                    return product_json(answer, url);
                }
            }
            Err(HttpError::new(
                404,
                "not_found",
                format!("no route for {path}"),
            ))
        }
    }
}

/// A point product's answer: a minute live (the fastest product rebuilds
/// every ten), a day when `issue=` pins one, since an issue's objects never
/// change under it.
fn product_json((body, issue): (Value, String), url: &Url) -> Result<Answer, HttpError> {
    let pinned = products::Query::new(url).issue().is_some();
    let cache = if pinned {
        "public, max-age=86400"
    } else {
        "public, max-age=60"
    };
    Ok(Answer::json(&body, cache)?.with_header("x-xue-issue", issue))
}
