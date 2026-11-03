//! The API's error shape.
//!
//! Every failure a handler can raise is an [`HttpError`], rendered as
//! `{"error": {"code", "message", "detail?}}` under the HTTP status the
//! handler chose (`400 invalid_parameter`, `404 unknown_source` /
//! `unknown_run` / `unknown_variable` / `no_live_run`, `405 method_not_allowed`,
//! `422 point_off_grid`, `502 upstream_failed`). Anything a binding returns is
//! an upstream failure unless a caller says otherwise.

use serde_json::Value;

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

/// The contract's error document for a failure.
pub fn body(error: &HttpError) -> Value {
    let mut body = serde_json::json!({
        "error": { "code": error.code, "message": error.message }
    });
    if let Some(detail) = &error.detail {
        body["error"]["detail"] = detail.clone();
    }
    body
}

#[cfg(target_arch = "wasm32")]
impl From<worker::Error> for HttpError {
    fn from(error: worker::Error) -> HttpError {
        HttpError::new(502, "upstream_failed", error.to_string())
    }
}

/// The response headers a cross-origin page may read beyond the CORS
/// safelist: what the request resolved to, the diagnostics and the edge
/// cache's own, so a browser client sees what `curl -i` shows.
pub const EXPOSE_HEADERS: &str =
    "x-xue-api, x-xue-ms, x-xue-run, x-xue-issue, retry-after, age, cf-cache-status";
