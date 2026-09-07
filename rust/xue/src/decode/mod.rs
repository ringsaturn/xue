//! Reading a Xue v1 bundle, in four layers over [`crate::format`]:
//!
//! * [`metadata`] turns the metadata JSON into the grid, the time axis and the
//!   variable set the rest of the decoder trusts.
//! * [`structure`] validates the header geometry, the index and every
//!   dependency chain — the layer that decides a file is well-formed.
//! * [`core`] replays residuals over payload bytes, wherever they came from.
//! * [`bundle`] is the public surface: [`Bundle`] and [`StreamingBundle`].

mod bundle;
mod core;
mod metadata;
mod structure;

pub use bundle::{Bundle, StreamingBundle};
