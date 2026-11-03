//! The HTTP framing of an answer, independent of the runtime.
//!
//! Handlers produce a [`Reply`] (status, headers, body); the Workers entry
//! point (`runtime.rs`) and the native server (`server.rs`) each turn it into
//! their own response type, so the CORS set, `Content-Type` and `x-xue-api`
//! are written once.

use crate::error::{self, HttpError};

pub struct Reply {
    pub status: u16,
    pub headers: Vec<(&'static str, String)>,
    pub body: Vec<u8>,
}

impl Reply {
    /// Set a header, replacing one of the same name.
    pub fn with_header(mut self, name: &'static str, value: impl Into<String>) -> Reply {
        let value = value.into();
        match self.headers.iter_mut().find(|(have, _)| *have == name) {
            Some(slot) => slot.1 = value,
            None => self.headers.push((name, value)),
        }
        self
    }
}

/// The CORS headers every response carries; the API is credential-free and
/// cross-origin is the norm.
fn cors() -> Vec<(&'static str, String)> {
    vec![
        ("access-control-allow-origin", "*".to_owned()),
        (
            "access-control-expose-headers",
            error::EXPOSE_HEADERS.to_owned(),
        ),
    ]
}

fn framed(
    status: u16,
    body: Vec<u8>,
    cache_control: Option<&str>,
    extra: &[(&'static str, String)],
) -> Reply {
    let mut reply = Reply {
        status,
        headers: cors(),
        body,
    }
    .with_header("content-type", "application/json; charset=utf-8")
    .with_header("x-xue-api", env!("CARGO_PKG_VERSION"));
    if let Some(cache_control) = cache_control {
        reply = reply.with_header("cache-control", cache_control);
    }
    for (name, value) in extra {
        reply = reply.with_header(name, value.as_str());
    }
    reply
}

/// A serialized JSON body, `Content-Type` and CORS included.
pub fn json(
    body: Vec<u8>,
    status: u16,
    cache_control: &str,
    extra: &[(&'static str, String)],
) -> Reply {
    framed(status, body, Some(cache_control), extra)
}

/// The `204` an `OPTIONS` preflight gets.
pub fn preflight() -> Reply {
    Reply {
        status: 204,
        headers: cors(),
        body: Vec::new(),
    }
    .with_header("access-control-allow-methods", "GET, HEAD, OPTIONS")
    .with_header("access-control-allow-headers", "*")
    .with_header("access-control-max-age", "86400")
}

/// The contract's error document for a failure. No `Cache-Control`: a
/// caller that wants one adds it.
pub fn error_reply(error: &HttpError) -> Reply {
    let body = serde_json::to_vec(&error::body(error)).unwrap_or_default();
    framed(error.status, body, None, &[])
}
