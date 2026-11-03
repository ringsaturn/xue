//! A bucket held in memory, for the tests.

use std::cell::RefCell;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::rc::Rc;

use crate::bucket::{ObjectSource, ReadRange};
use crate::error::HttpError;

/// Every read as `(path, kind)`, kind one of `whole`, `range`, `suffix`.
pub type Reads = Rc<RefCell<Vec<(String, &'static str)>>>;

/// Objects by key (no prefix).
pub struct Memory {
    pub objects: HashMap<String, Vec<u8>>,
    pub reads: Reads,
}

impl ObjectSource for Memory {
    fn read<'a>(
        &'a self,
        path: &'a str,
        range: Option<ReadRange>,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<u8>, HttpError>> + 'a>> {
        Box::pin(async move {
            let kind = match range {
                None => "whole",
                Some(ReadRange::Offset { .. }) => "range",
                Some(ReadRange::Suffix { .. }) => "suffix",
            };
            self.reads.borrow_mut().push((path.to_owned(), kind));
            let bytes = self
                .objects
                .get(path.trim_start_matches('/'))
                .ok_or_else(|| HttpError::new(404, "not_found", format!("no object {path}")))?;
            Ok(match range {
                Some(range) => range.slice(bytes).to_vec(),
                None => bytes.clone(),
            })
        })
    }
}
