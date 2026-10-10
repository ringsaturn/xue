//! The Xue data API as a Cloudflare Worker.
//!
//! An additional read layer, not part of the pipeline: it resolves a run the
//! way the STAC catalog does (Collection → live pointer → manifest), reads the
//! bundle's Zarr store over ranges and decodes with the same `xue` crate the
//! browser's wasm decoder uses. `docs/api.md` is the wire contract (routes,
//! parameters, response shapes, errors, caching); `docs/zarr-profile.md` is
//! the store layout these objects follow and `docs/stac.md` the catalog the
//! resolution chain is part of.
//!
//! The read paths are cached twice: immutable documents and shard indices in
//! an isolate (module state, `cache.rs`), and whole 200 responses in
//! Cloudflare's edge cache (`Cache::default`). A repeated query is then served
//! from the data center the request landed in, with no R2 round trip and
//! almost no Worker execution.
//!
//! Placement is deliberately left to the request's own data center: a
//! placement hint would forward every request — cached or not — to another
//! region, which costs more on a cache hit than it saves on a miss. The R2
//! binding and the `DATA_SOURCE=cdn` fallback both read the objects from
//! wherever the Worker runs.

// Natively only the tests call the handlers; on wasm32 `runtime` does.
#![cfg_attr(not(target_arch = "wasm32"), allow(dead_code))]

mod bucket;
mod cache;
#[cfg(target_arch = "wasm32")]
mod docs;
mod error;
mod point;
#[cfg(target_arch = "wasm32")]
mod runtime;
mod source;
mod timezone;

#[cfg(test)]
mod tests;
