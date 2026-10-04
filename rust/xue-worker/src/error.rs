//! The API's error shape and JSON responses.
//!
//! Every failure a handler can raise is an [`HttpError`], rendered as
//! `{"error": {"code", "message", "detail?}}` under the HTTP status the
//! handler chose (`400 invalid_parameter`, `404 unknown_source` /
//! `unknown_run` / `unknown_variable` / `no_live_run`, `405 method_not_allowed`,
//! `422 point_off_grid`, `502 upstream_failed`). Anything a binding returns is
//! an upstream failure unless a caller says otherwise.

use serde_json::Value;
#[cfg(target_arch = "wasm32")]
use worker::{Headers, Response};

#[derive(Debug)]
pub struct HttpError {
    pub status: u16,
    pub code: String,
    pub message: String,
    pub detail: Option<Value>,
}

impl HttpError {
    pub fn new(status: u16, code: &str, message: impl Into<String>) -> HttpError {
        HttpError {
            status,
            code: code.to_owned(),
            message: message.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: Value) -> HttpError {
        self.detail = Some(detail);
        self
    }
}

#[cfg(target_arch = "wasm32")]
impl From<worker::Error> for HttpError {
    fn from(error: worker::Error) -> HttpError {
        HttpError::new(502, "upstream_failed", error.to_string())
    }
}

#[cfg(target_arch = "wasm32")]
/// The CORS headers every response carries; the API is credential-free and
/// cross-origin is the norm.
pub fn cors_headers() -> Result<Headers, HttpError> {
    let headers = Headers::new();
    headers
        .set("access-control-allow-origin", "*")
        .map_err(HttpError::from)?;
    Ok(headers)
}

#[cfg(target_arch = "wasm32")]
fn base_headers(extra: &[(&str, &str)]) -> Result<Headers, HttpError> {
    let headers = cors_headers()?;
    headers
        .set("content-type", "application/json; charset=utf-8")
        .map_err(HttpError::from)?;
    headers
        .set("x-xue-api", env!("CARGO_PKG_VERSION"))
        .map_err(HttpError::from)?;
    for (key, value) in extra {
        headers.set(key, value).map_err(HttpError::from)?;
    }
    Ok(headers)
}

#[cfg(target_arch = "wasm32")]
/// A JSON response, `Content-Type` and CORS included.
pub fn json(
    value: &Value,
    status: u16,
    cache_control: Option<&str>,
    extra: &[(&str, &str)],
) -> Result<Response, HttpError> {
    let headers = base_headers(extra)?;
    if let Some(cache_control) = cache_control {
        headers
            .set("cache-control", cache_control)
            .map_err(HttpError::from)?;
    }
    Ok(Response::from_json(value)
        .map_err(HttpError::from)?
        .with_status(status)
        .with_headers(headers))
}

#[cfg(target_arch = "wasm32")]
/// The `204` an `OPTIONS` preflight gets.
pub fn preflight() -> Result<Response, HttpError> {
    let headers = cors_headers()?;
    headers
        .set("access-control-allow-methods", "GET, HEAD, OPTIONS")
        .map_err(HttpError::from)?;
    headers
        .set("access-control-allow-headers", "*")
        .map_err(HttpError::from)?;
    headers
        .set("access-control-max-age", "86400")
        .map_err(HttpError::from)?;
    Ok(Response::empty()
        .map_err(HttpError::from)?
        .with_status(204)
        .with_headers(headers))
}

#[cfg(target_arch = "wasm32")]
/// Render any handler failure as the contract's error document. Built with
/// `worker::Result` directly so it cannot itself need an error handler.
pub fn error_response(error: HttpError) -> worker::Result<Response> {
    let mut body = serde_json::json!({
        "error": { "code": error.code, "message": error.message }
    });
    if let Some(detail) = error.detail {
        body["error"]["detail"] = detail;
    }
    let headers = Headers::new();
    headers.set("content-type", "application/json; charset=utf-8")?;
    headers.set("access-control-allow-origin", "*")?;
    headers.set("x-xue-api", env!("CARGO_PKG_VERSION"))?;
    Ok(Response::from_json(&body)?
        .with_status(error.status)
        .with_headers(headers))
}
