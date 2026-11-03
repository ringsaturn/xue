//! The places the published objects are read from, one [`ObjectSource`] each.
//!
//! On the Workers runtime `DATA_SOURCE` picks between two:
//!
//! * `r2` (default), [`r2`]: the `DATA` R2 binding, `get(key).range(…)` — an
//!   exact range, no whole-object fill, deterministic on a cold object.
//! * `cdn`, [`cdn`]: the public origin (`DATA_ORIGIN`), the same ranged GET the
//!   browser makes. Used for local development (`wrangler dev`, where the local
//!   bucket is empty) and as a fallback when an object is served faster from
//!   the edge cache than from the bucket's own region.
//!
//! Natively there is the same origin read over `reqwest` ([`http`], the
//! `server` feature, the `xue-api-server` binary), and [`memory`], which is
//! how the tests feed the handlers a fixture bucket and count the reads a
//! query costs. Any other [`ObjectSource`] a caller supplies (a mirror on
//! local disk, another store) is served by the same routes through
//! `server::serve`.

#[cfg(target_arch = "wasm32")]
pub mod cdn;
#[cfg(all(feature = "server", not(target_arch = "wasm32")))]
pub mod http;
#[cfg(test)]
pub mod memory;
#[cfg(target_arch = "wasm32")]
pub mod r2;

#[cfg(any(target_arch = "wasm32", feature = "server"))]
use crate::bucket::ReadRange;
#[cfg(any(target_arch = "wasm32", feature = "server"))]
use crate::error::HttpError;

/// The URL of `path` under a public origin.
#[cfg(any(target_arch = "wasm32", feature = "server"))]
fn origin_url(origin: &str, path: &str) -> Result<url::Url, HttpError> {
    let url = format!(
        "{}/{}",
        origin.trim_end_matches('/'),
        path.trim_start_matches('/')
    );
    url::Url::parse(&url).map_err(|error| {
        HttpError::new(
            502,
            "upstream_failed",
            format!("{url} is not a URL: {error}"),
        )
    })
}

/// An origin answer's status as the handlers see it: a missing object is
/// `404 not_found`, anything else outside 2xx an upstream failure.
#[cfg(any(target_arch = "wasm32", feature = "server"))]
fn origin_status(path: &str, url: &url::Url, status: u16) -> Result<(), HttpError> {
    if status == 404 {
        return Err(HttpError::new(
            404,
            "not_found",
            format!("no object {path}"),
        ));
    }
    if !(200..300).contains(&status) {
        return Err(HttpError::new(
            502,
            "upstream_failed",
            format!("GET {url} -> {status}"),
        ));
    }
    Ok(())
}

/// The requested bytes of an origin answer. A `206` honoured the range; an
/// origin that ignored `Range` sent the whole object, sliced here as the point
/// products do.
#[cfg(any(target_arch = "wasm32", feature = "server"))]
fn origin_body(bytes: Vec<u8>, range: Option<ReadRange>, status: u16) -> Vec<u8> {
    match range {
        Some(range) if status != 206 => range.slice(&bytes).to_vec(),
        _ => bytes,
    }
}
