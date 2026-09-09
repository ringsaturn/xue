//! Reading a Xue bundle, in four layers over [`crate::format`]:
//!
//! * `metadata` turns the metadata JSON into the grid, the time axis and the
//!   variable set the rest of the decoder trusts. It is shared by both
//!   container versions, which differ only below the index.
//! * `structure` validates the header geometry and the index — the layer
//!   that decides a file is well-formed. It parses a v1 plane index (with
//!   its dependency chains) or a v2 tiled one into the layout `core`
//!   dispatches on.
//! * `core` replays residuals over payload bytes, wherever they came from,
//!   and assembles planes and cell series out of tiles.
//! * `bundle` is the public surface: [`Bundle`] and [`StreamingBundle`].

mod bundle;
mod core;
mod metadata;
mod structure;

pub use bundle::{Bundle, StreamingBundle};
