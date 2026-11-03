//! The `DATA` R2 binding.

use std::future::Future;
use std::pin::Pin;

use worker::{Bucket, Range};

use crate::bucket::{ObjectSource, ReadRange};
use crate::error::HttpError;

pub struct R2 {
    pub bucket: Bucket,
    /// The leading path segment every key in the bucket shares.
    pub prefix: String,
}

impl R2 {
    fn key(&self, path: &str) -> String {
        format!(
            "{}/{}",
            self.prefix.trim_end_matches('/'),
            path.trim_start_matches('/')
        )
    }
}

impl ObjectSource for R2 {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<ReadRange>,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, HttpError>> + 'a>> {
        Box::pin(async move {
            let request = self.bucket.get(self.key(path));
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
        })
    }
}
