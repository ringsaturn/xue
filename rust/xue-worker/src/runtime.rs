//! The Workers entry point: routing, the edge cache and the HTTP framing of
//! the handlers' JSON. Only this module and `docs` touch the Workers runtime;
//! the handlers below them return plain values so they also run natively
//! under `cargo test`.

use worker::{event, Cache, Context, Env, Method, Request, Response};

use crate::bucket::Data;
use crate::error::{self, HttpError};
use crate::{airport, docs, point, products, sounding, source, synop, tc};

#[event(fetch)]
async fn fetch(request: Request, env: Env, _ctx: Context) -> worker::Result<Response> {
    let started = js_sys::Date::now();
    let response = match route(request, &env).await {
        Ok(response) => response,
        Err(error) => error::error_response(error)?,
    };
    // The Worker's own wall time from handler entry to response, including
    // every `cache.get` / R2 subrequest but excluding the client legs on
    // either side. `x-xue-ms` is a diagnostic, not part of the contract.
    let elapsed = (js_sys::Date::now() - started).round().max(0.0) as i64;
    let _ = response.headers().set("x-xue-ms", &elapsed.to_string());
    Ok(response)
}

async fn route(request: Request, env: &Env) -> Result<Response, HttpError> {
    let method = request.method();
    if method == Method::Options {
        return error::preflight();
    }
    if method != Method::Get && method != Method::Head {
        return Err(HttpError::new(
            405,
            "method_not_allowed",
            "only GET is supported",
        ));
    }
    let url = request.url().map_err(HttpError::from)?;
    let path = url.path().trim_end_matches('/');
    let path = if path.is_empty() { "/" } else { path };
    // Every data answer is edge-cached by URL. The contract is generated and
    // held per isolate instead; the Swagger UI under `public/` is served by
    // the static-assets server, so it never reaches this route.
    let cacheable = method == Method::Get
        && path.starts_with("/v1/")
        && path != "/v1/openapi.json";
    if cacheable {
        if let Some(hit) = edge_get(&request).await {
            return Ok(hit);
        }
    }
    let mut response = dispatch(path, &url, env).await?;
    if cacheable && response.status_code() == 200 {
        if let Ok(clone) = response.cloned() {
            edge_put(&request, clone).await;
        }
    }
    Ok(response)
}

async fn dispatch(path: &str, url: &worker::Url, env: &Env) -> Result<Response, HttpError> {
    let data = Data::from_env(env)?;
    match path {
        "/health" => {
            let body = serde_json::json!({
                "status": "ok",
                "build": env!("CARGO_PKG_VERSION"),
                "now": xue::zarr::format_iso_millis(js_sys::Date::now()),
            });
            error::json(&body, 200, Some("no-store"), &[])
        }
        "/openapi.json" | "/v1/openapi.json" => docs::openapi(&data).await,
        "/v1/catalog" => error::json(
            &source::catalog(&data).await?,
            200,
            Some("public, max-age=60"),
            &[],
        ),
        "/v1/point" => {
            let query = point::parse_query(url);
            let has_run = query.run.is_some();
            let (body, run) = point::read_point(&data, &query).await?;
            let cache = if has_run {
                "public, max-age=86400"
            } else {
                "public, max-age=30"
            };
            error::json(&body, 200, Some(cache), &[("x-xue-run", run.as_str())])
        }
        "/v1/soundings" => product_json(sounding::list(&data, url).await?, url),
        "/v1/airports" => product_json(airport::list(&data, url).await?, url),
        "/v1/synop" => product_json(synop::list(&data, url).await?, url),
        "/v1/storms" => product_json(tc::list(&data, url).await?, url),
        _ => {
            if let Some(source_id) = path.strip_prefix("/v1/sources/") {
                if !source_id.is_empty() && !source_id.contains('/') {
                    return error::json(
                        &source::read_source(&data, source_id).await?,
                        200,
                        Some("public, max-age=60"),
                        &[],
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
                        "sounding" => sounding::station(&data, &id, url).await?,
                        "airport" => airport::station(&data, &id, url).await?,
                        "synop" => synop::station(&data, &id, url).await?,
                        _ => tc::storm(&data, &id, url).await?,
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
fn product_json(
    (body, issue): (serde_json::Value, String),
    url: &worker::Url,
) -> Result<Response, HttpError> {
    let pinned = products::Query::new(url).issue().is_some();
    let cache = if pinned {
        "public, max-age=86400"
    } else {
        "public, max-age=60"
    };
    error::json(&body, 200, Some(cache), &[("x-xue-issue", issue.as_str())])
}

/// The edge cache (`caches.default`), keyed by the request URL. A miss is a
/// real request; a hit never reaches this Worker's handlers again.
async fn edge_get(request: &Request) -> Option<Response> {
    Cache::default().get(request, false).await.ok().flatten()
}

/// Store a 200 for later. The response already carries the `Cache-Control`
/// the Cache API needs (`max-age`), which is also the client directive.
async fn edge_put(request: &Request, response: Response) {
    let _ = Cache::default().put(request, response).await;
}
