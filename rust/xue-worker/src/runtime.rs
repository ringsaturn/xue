//! The Workers entry point: routing, the edge cache and the HTTP framing of
//! the handlers' JSON. Only this module and `docs` touch the Workers runtime;
//! the handlers below them return plain values so they also run natively
//! under `cargo test`.

use worker::{event, Cache, Context, Env, Method, Request, Response};

use crate::bucket::Data;
use crate::error::{self, HttpError};
use crate::{docs, point, source};

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
    // The contract is generated and held per isolate; the Swagger UI under
    // `public/` is served by the static-assets server, so it never reaches
    // this route.
    let cacheable = method == Method::Get
        && (path == "/v1/catalog"
            || path == "/v1/point"
            || path.strip_prefix("/v1/sources/").is_some());
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
            Err(HttpError::new(
                404,
                "not_found",
                format!("no route for {path}"),
            ))
        }
    }
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
