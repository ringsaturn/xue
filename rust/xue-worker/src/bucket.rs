//! Reading the published data.
//!
//! [`Data`] is a handle on the dataset over one [`ObjectSource`]; which source
//! that is (R2 binding, public origin, in-memory fixture, a caller's own) is
//! `backend/`'s concern.
//!
//! The object layout and the resolution chain above this (`collection →
//! pointer → manifest`) are the data contract, not the API's.

use serde::de::DeserializeOwned;
use std::{future::Future, pin::Pin};
#[cfg(target_arch = "wasm32")]
use worker::Env;

use crate::cache;
use crate::error::HttpError;

/// An explicit byte range: a known length, or a suffix with no object length.
pub enum ReadRange {
    Offset { offset: u64, length: u64 },
    Suffix { suffix: u64 },
}

impl ReadRange {
    /// The `Range` request header for this range.
    pub fn header(&self) -> String {
        match self {
            ReadRange::Offset { offset, length } => {
                format!("bytes={offset}-{}", offset + length - 1)
            }
            ReadRange::Suffix { suffix } => format!("bytes=-{suffix}"),
        }
    }

    /// This range of a whole object.
    pub fn slice<'a>(&self, bytes: &'a [u8]) -> &'a [u8] {
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

/// The objects of the published dataset, read from somewhere this crate does
/// not know about. `path` is relative to the data root (`gfs/collection.json`,
/// `gfs.2026103000/tmp2m.zarr/…`); `range`, when given, is the bytes to return.
/// A missing object is `HttpError` 404 `not_found`; anything else a source
/// cannot read is a 502 `upstream_failed`.
///
/// The returned future need not be `Send`: the server runs on one thread, and
/// the Workers runtime's own futures are not `Send`.
pub trait ObjectSource {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<ReadRange>,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, HttpError>> + 'a>>;
}

/// A handle on the public dataset.
pub struct Data {
    source: Box<dyn ObjectSource>,
    /// The public origin a client can read the same objects from directly;
    /// reported by `/v1/catalog` so a consumer can pin a run itself.
    pub origin: String,
    #[cfg(test)]
    reads: crate::backend::memory::Reads,
}

impl Data {
    fn new(source: Box<dyn ObjectSource>, origin: &str) -> Data {
        Data {
            source,
            origin: origin.to_owned(),
            #[cfg(test)]
            reads: Default::default(),
        }
    }

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
        let source: Box<dyn ObjectSource> = if cdn {
            Box::new(crate::backend::cdn::Cdn {
                origin: origin.clone(),
            })
        } else {
            Box::new(crate::backend::r2::R2 {
                bucket: env
                    .bucket("DATA")
                    .map_err(|error| HttpError::new(502, "binding_missing", error.to_string()))?,
                prefix,
            })
        };
        Ok(Data::new(source, &origin))
    }

    /// The public origin over HTTP, for the native server.
    #[cfg(all(feature = "server", not(target_arch = "wasm32")))]
    pub fn from_origin(client: reqwest::Client, origin: &str) -> Data {
        Data::new(
            Box::new(crate::backend::http::Http {
                client,
                origin: origin.to_owned(),
            }),
            origin,
        )
    }

    /// Objects from a caller's own source; `origin` is the public origin
    /// `/v1/catalog` reports for clients to read the same objects directly.
    #[cfg(not(target_arch = "wasm32"))]
    pub fn from_source(source: Box<dyn ObjectSource>, origin: &str) -> Data {
        Data::new(source, origin)
    }

    /// A bucket held in memory, keyed by path under the data root.
    #[cfg(test)]
    pub fn in_memory(objects: std::collections::HashMap<String, Vec<u8>>) -> Data {
        let reads = crate::backend::memory::Reads::default();
        let memory = crate::backend::memory::Memory {
            objects,
            reads: reads.clone(),
        };
        let mut data = Data::new(Box::new(memory), "https://dataset.example/xue/");
        data.reads = reads;
        data
    }

    /// Every read so far, `(path, kind)`, oldest first.
    #[cfg(test)]
    pub fn reads(&self) -> Vec<(String, &'static str)> {
        self.reads.borrow().clone()
    }

    /// Read an object (or a range of it). A missing object is `404 not_found`;
    /// callers that need a contract code (`unknown_source`, `unknown_run`) remap
    /// it.
    async fn read(&self, path: &str, range: Option<ReadRange>) -> Result<Vec<u8>, HttpError> {
        self.source.read(path, range).await
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
