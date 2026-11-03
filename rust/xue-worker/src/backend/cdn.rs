//! The public origin, fetched from the Workers runtime.

use std::future::Future;
use std::pin::Pin;

use worker::{Fetch, Headers, Method, Request, RequestInit};

use super::{origin_body, origin_status, origin_url};
use crate::bucket::{ObjectSource, ReadRange};
use crate::error::HttpError;

pub struct Cdn {
    pub origin: String,
}

impl ObjectSource for Cdn {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<ReadRange>,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, HttpError>> + 'a>> {
        Box::pin(async move {
            let url = origin_url(&self.origin, path)?;
            let mut init = RequestInit::new();
            init.with_method(Method::Get);
            if let Some(header) = range.as_ref().map(ReadRange::header) {
                let headers = Headers::new();
                headers.set("range", &header).map_err(HttpError::from)?;
                init.with_headers(headers);
            }
            let request = Request::new_with_init(url.as_str(), &init).map_err(HttpError::from)?;
            let mut response = Fetch::Request(request)
                .send()
                .await
                .map_err(HttpError::from)?;
            let status = response.status_code();
            origin_status(path, &url, status)?;
            let bytes = response.bytes().await.map_err(HttpError::from)?;
            Ok(origin_body(bytes, range, status))
        })
    }
}
