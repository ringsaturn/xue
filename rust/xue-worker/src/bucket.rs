//! Reading the published data.
//!
//! Two interchangeable backends on the Workers runtime, chosen by
//! `DATA_SOURCE`:
//!
//! * `r2` (default): the `DATA` R2 binding, `get(key).range(…)` — an exact
//!   range, no whole-object fill, deterministic on a cold object.
//! * `cdn`: the public origin (`DATA_ORIGIN`), the same ranged GET the browser
//!   makes. Used for local development (`wrangler dev`, where the local bucket
//!   is empty) and as a fallback when an object is served faster from the
//!   edge cache than from the bucket's own region.
//!
//! Natively there is the same origin read over `reqwest` (the `server`
//! feature, the `xue-api-server` binary), and one in memory, which is how the
//! tests feed the handlers a fixture bucket and count the reads a query costs.
//!
//! The object layout and the resolution chain above this (`collection →
//! pointer → manifest`) are the data contract, not the API's.

use serde::de::DeserializeOwned;
#[cfg(not(target_arch = "wasm32"))]
use std::{cell::RefCell, collections::HashMap};
#[cfg(target_arch = "wasm32")]
use worker::{Bucket, Env, Fetch, Headers, Method, Range, Request, RequestInit};

use crate::cache;
use crate::error::HttpError;

enum Backend {
    #[cfg(target_arch = "wasm32")]
    R2(Bucket),
    #[cfg(target_arch = "wasm32")]
    Cdn,
    #[cfg(all(feature = "server", not(target_arch = "wasm32")))]
    Http(reqwest::Client),
    /// Objects by key (no prefix), and the log of every read as
    /// `(path, kind)`, kind one of `whole`, `range`, `suffix`.
    #[cfg(not(target_arch = "wasm32"))]
    #[cfg_attr(feature = "server", allow(dead_code))]
    Memory {
        objects: HashMap<String, Vec<u8>>,
        reads: RefCell<Vec<(String, &'static str)>>,
    },
}

/// An explicit byte range: a known length, or a suffix with no object length.
enum ReadRange {
    Offset { offset: u64, length: u64 },
    Suffix { suffix: u64 },
}

impl ReadRange {
    #[cfg_attr(not(any(target_arch = "wasm32", feature = "server")), allow(dead_code))]
    fn header(&self) -> String {
        match self {
            ReadRange::Offset { offset, length } => {
                format!("bytes={offset}-{}", offset + length - 1)
            }
            ReadRange::Suffix { suffix } => format!("bytes=-{suffix}"),
        }
    }

    /// This range of a whole object.
    fn slice<'a>(&self, bytes: &'a [u8]) -> &'a [u8] {
        match *self {
            ReadRange::Offset { offset, length } => {
                let start = offset.min(bytes.len() as u64) as usize;
                let end = (offset + length).min(bytes.len() as u64) as usize;
                &bytes[start..end]
            }
            ReadRange::Suffix { suffix } => &bytes[bytes.len().saturating_sub(suffix as usize)..],
        }
    }
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

#[cfg(all(feature = "server", not(target_arch = "wasm32")))]
fn upstream(error: reqwest::Error) -> HttpError {
    HttpError::new(502, "upstream_failed", error.to_string())
}

/// A handle on the public dataset, with the prefix every key shares.
pub struct Data {
    backend: Backend,
    #[cfg_attr(not(target_arch = "wasm32"), allow(dead_code))]
    prefix: String,
    /// The public origin a client can read the same objects from directly;
    /// reported by `/v1/catalog` so a consumer can pin a run itself.
    pub origin: String,
}

impl Data {
    #[cfg(target_arch = "wasm32")]
    pub fn from_env(env: &Env) -> Result<Data, HttpError> {
        let prefix = env
            .var("DATA_PREFIX")
            .map(|value| value.to_string())
            .unwrap_or_else(|_| "xue".to_owned());
        let origin = env
            .var("DATA_ORIGIN")
            .map(|value| value.to_string())
            .unwrap_or_else(|_| "https://dataset.ringsaturn.me/xue/".to_owned());
        let cdn = env
            .var("DATA_SOURCE")
            .map(|value| value.to_string())
            .map(|value| value == "cdn")
            .unwrap_or(false);
        let backend = if cdn {
            Backend::Cdn
        } else {
            Backend::R2(
                env.bucket("DATA")
                    .map_err(|error| HttpError::new(502, "binding_missing", error.to_string()))?,
            )
        };
        Ok(Data {
            backend,
            prefix,
            origin,
        })
    }

    /// The public origin over HTTP, for the native server.
    #[cfg(all(feature = "server", not(target_arch = "wasm32")))]
    pub fn from_origin(client: reqwest::Client, origin: &str) -> Data {
        Data {
            backend: Backend::Http(client),
            prefix: String::new(),
            origin: origin.to_owned(),
        }
    }

    /// A bucket held in memory, keyed by path under the data root.
    #[cfg(not(target_arch = "wasm32"))]
    #[cfg_attr(feature = "server", allow(dead_code))]
    pub fn in_memory(objects: HashMap<String, Vec<u8>>) -> Data {
        Data {
            backend: Backend::Memory {
                objects,
                reads: RefCell::new(Vec::new()),
            },
            prefix: String::new(),
            origin: "https://dataset.example/xue/".to_owned(),
        }
    }

    /// Every read so far, `(path, kind)`, oldest first.
    #[cfg(not(target_arch = "wasm32"))]
    #[cfg_attr(feature = "server", allow(dead_code))]
    pub fn reads(&self) -> Vec<(String, &'static str)> {
        match &self.backend {
            Backend::Memory { reads, .. } => reads.borrow().clone(),
            #[cfg(feature = "server")]
            Backend::Http(_) => Vec::new(),
        }
    }

    #[cfg(target_arch = "wasm32")]
    fn key(&self, path: &str) -> String {
        format!(
            "{}/{}",
            self.prefix.trim_end_matches('/'),
            path.trim_start_matches('/')
        )
    }

    #[cfg(any(target_arch = "wasm32", feature = "server"))]
    fn url(&self, path: &str) -> Result<url::Url, HttpError> {
        let url = format!(
            "{}/{}",
            self.origin.trim_end_matches('/'),
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

    /// Read an object (or a range of it). A missing object is `404 not_found`;
    /// callers that need a contract code (`unknown_source`, `unknown_run`) remap
    /// it.
    async fn read(&self, path: &str, range: Option<ReadRange>) -> Result<Vec<u8>, HttpError> {
        match &self.backend {
            #[cfg(target_arch = "wasm32")]
            Backend::R2(bucket) => {
                let request = bucket.get(self.key(path));
                let request = match range {
                    Some(ReadRange::Offset { offset, length }) => {
                        request.range(Range::OffsetWithLength { offset, length })
                    }
                    Some(ReadRange::Suffix { suffix }) => request.range(Range::Suffix { suffix }),
                    None => request,
                };
                let object = request
                    .execute()
                    .await
                    .map_err(HttpError::from)?
                    .ok_or_else(|| HttpError::new(404, "not_found", format!("no object {path}")))?;
                let body = object.body().ok_or_else(|| {
                    HttpError::new(502, "upstream_failed", format!("object {path} has no body"))
                })?;
                body.bytes().await.map_err(HttpError::from)
            }
            #[cfg(target_arch = "wasm32")]
            Backend::Cdn => {
                let url = self.url(path)?;
                let mut init = RequestInit::new();
                init.with_method(Method::Get);
                if let Some(header) = range.as_ref().map(ReadRange::header) {
                    let headers = Headers::new();
                    headers.set("range", &header).map_err(HttpError::from)?;
                    init.with_headers(headers);
                }
                let request =
                    Request::new_with_init(url.as_str(), &init).map_err(HttpError::from)?;
                let mut response = Fetch::Request(request)
                    .send()
                    .await
                    .map_err(HttpError::from)?;
                let status = response.status_code();
                origin_status(path, &url, status)?;
                let bytes = response.bytes().await.map_err(HttpError::from)?;
                Ok(origin_body(bytes, range, status))
            }
            #[cfg(all(feature = "server", not(target_arch = "wasm32")))]
            Backend::Http(client) => {
                let url = self.url(path)?;
                let mut request = client.get(url.clone());
                if let Some(header) = range.as_ref().map(ReadRange::header) {
                    request = request.header("range", header);
                }
                let response = request.send().await.map_err(upstream)?;
                let status = response.status().as_u16();
                origin_status(path, &url, status)?;
                let bytes = response.bytes().await.map_err(upstream)?;
                Ok(origin_body(bytes.to_vec(), range, status))
            }
            #[cfg(not(target_arch = "wasm32"))]
            Backend::Memory { objects, reads } => {
                let kind = match range {
                    None => "whole",
                    Some(ReadRange::Offset { .. }) => "range",
                    Some(ReadRange::Suffix { .. }) => "suffix",
                };
                reads.borrow_mut().push((path.to_owned(), kind));
                let bytes = objects
                    .get(path.trim_start_matches('/'))
                    .ok_or_else(|| HttpError::new(404, "not_found", format!("no object {path}")))?;
                Ok(match range {
                    Some(range) => range.slice(bytes).to_vec(),
                    None => bytes.clone(),
                })
            }
        }
    }

    pub async fn text(&self, path: &str) -> Result<String, HttpError> {
        let bytes = self.read(path, None).await?;
        String::from_utf8(bytes).map_err(|error| {
            HttpError::new(
                502,
                "upstream_failed",
                format!("{path} is not UTF-8: {error}"),
            )
        })
    }

    pub async fn json<T: DeserializeOwned>(&self, path: &str) -> Result<T, HttpError> {
        let text = self.text(path).await?;
        serde_json::from_str(&text).map_err(|error| {
            HttpError::new(
                502,
                "upstream_failed",
                format!("{path} is not valid JSON: {error}"),
            )
        })
    }

    /// An immutable object (`?v=<version>` addressed), served from the isolate
    /// cache when a previous request in this isolate read it.
    pub async fn text_cached(&self, path: &str, version: &str) -> Result<String, HttpError> {
        let key = format!("t:{path}#{version}");
        if let Some(value) = cache::text(&key) {
            return Ok(value);
        }
        let value = self.text(path).await?;
        cache::put_text(&key, value.clone());
        Ok(value)
    }

    pub async fn json_cached<T: DeserializeOwned>(
        &self,
        path: &str,
        version: &str,
    ) -> Result<T, HttpError> {
        let text = self.text_cached(path, version).await?;
        serde_json::from_str(&text).map_err(|error| {
            HttpError::new(
                502,
                "upstream_failed",
                format!("{path} is not valid JSON: {error}"),
            )
        })
    }

    pub async fn value(&self, path: &str) -> Result<serde_json::Value, HttpError> {
        self.json(path).await
    }

    /// `length` bytes from `offset`.
    pub async fn range(&self, path: &str, offset: u64, length: u64) -> Result<Vec<u8>, HttpError> {
        self.read(path, Some(ReadRange::Offset { offset, length }))
            .await
    }

    /// A span of an immutable object (a station's line in a product file,
    /// which is `?v=`-addressed), from the isolate cache when held.
    pub async fn range_cached(
        &self,
        path: &str,
        offset: u64,
        length: u64,
        version: &str,
    ) -> Result<Vec<u8>, HttpError> {
        let key = format!("r:{path}#{version}:{offset}+{length}");
        if let Some(value) = cache::bytes(&key) {
            return Ok(value);
        }
        let value = self.range(path, offset, length).await?;
        cache::put_bytes(&key, value.clone());
        Ok(value)
    }

    /// The last `suffix` bytes, without knowing the object's length.
    pub async fn suffix(&self, path: &str, suffix: u64) -> Result<Vec<u8>, HttpError> {
        self.read(path, Some(ReadRange::Suffix { suffix })).await
    }

    /// A shard index, which is immutable for its store's crc, from the isolate
    /// cache when held.
    pub async fn suffix_cached(
        &self,
        path: &str,
        suffix: u64,
        version: &str,
    ) -> Result<Vec<u8>, HttpError> {
        let key = format!("b:{path}#{version}:{suffix}");
        if let Some(value) = cache::bytes(&key) {
            return Ok(value);
        }
        let value = self.suffix(path, suffix).await?;
        cache::put_bytes(&key, value.clone());
        Ok(value)
    }
}
