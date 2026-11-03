//! The public origin, fetched natively over `reqwest`.

use std::future::Future;
use std::pin::Pin;

use super::{origin_body, origin_status, origin_url};
use crate::bucket::{ObjectSource, ReadRange};
use crate::error::HttpError;

pub struct Http {
    pub client: reqwest::Client,
    pub origin: String,
}

fn upstream(error: reqwest::Error) -> HttpError {
    HttpError::new(502, "upstream_failed", error.to_string())
}

impl ObjectSource for Http {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<ReadRange>,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, HttpError>> + 'a>> {
        Box::pin(async move {
            let url = origin_url(&self.origin, path)?;
            let kind = if range.is_some() { "range" } else { "whole" };
            let started = std::time::Instant::now();
            let mut request = self.client.get(url.clone());
            if let Some(header) = range.as_ref().map(ReadRange::header) {
                request = request.header("range", header);
            }
            let read = async {
                let response = request.send().await.map_err(upstream)?;
                let status = response.status().as_u16();
                origin_status(path, &url, status)?;
                let bytes = response.bytes().await.map_err(upstream)?;
                Ok::<_, HttpError>((bytes, status))
            }
            .await;
            let result = match &read {
                Ok(_) => "ok",
                Err(error) if error.status == 404 => "not_found",
                Err(_) => "error",
            };
            let length = read.as_ref().map_or(0, |(bytes, _)| bytes.len());
            crate::metrics::upstream(kind, result, started.elapsed(), length);
            let (bytes, status) = read?;
            Ok(origin_body(bytes.to_vec(), range, status))
        })
    }
}
