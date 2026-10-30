//! The Workers entry point: the edge cache and the HTTP framing of the
//! router's answers (`route.rs`). Only this module and `error`'s framing touch
//! the Workers runtime; everything below returns plain values, so it also runs
//! natively under `cargo test` and in the `xue-api-server` binary.

use worker::{event, Cache, Context, Env, Method, Request, Response};

use crate::bucket::Data;
use crate::error::{self, HttpError};
use crate::route;

#[event(fetch)]
async fn fetch(request: Request, env: Env, _ctx: Context) -> worker::Result<Response> {
    let started = js_sys::Date::now();
    let response = match handle(request, &env).await {
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

async fn handle(request: Request, env: &Env) -> Result<Response, HttpError> {
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
    let path = route::normalize(url.path());
    // Every data answer is edge-cached by URL. The Swagger UI under `public/`
    // is served by the static-assets server, so it never reaches this route.
    let cacheable = method == Method::Get && route::cacheable(path);
    if cacheable {
        if let Some(hit) = edge_get(&request).await {
            return Ok(hit);
        }
    }
    let data = Data::from_env(env)?;
    let answer = route::dispatch(path, &url, &data, js_sys::Date::now()).await?;
    let extra: Vec<(&str, &str)> = answer
        .headers
        .iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect();
    let mut response = error::json(answer.body, 200, answer.cache_control, &extra)?;
    if cacheable {
        if let Ok(clone) = response.cloned() {
            edge_put(&request, clone).await;
        }
    }
    Ok(response)
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
