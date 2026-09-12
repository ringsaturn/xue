//! An experimental native encoder for the Xue v1 bundle format.
//!
//! The production encoder is Python (`xue/` at the repository root),
//! orchestrating GDAL, zstd and ffmpeg as CLI subprocesses. This module is the
//! same pipeline in Rust with the external tools linked in process instead:
//!
//! * **GDAL** through `gdal-sys` (georust) replaces `gdalinfo -json` and
//!   `gdal_translate -of ENVI` — a production GFS run wrote and re-read
//!   several gigabytes of intermediate ENVI files per build, which the
//!   Python-side profiling identified as the remaining wall.
//! * **grib-rs** replaces the hand-rolled GRIB2 header index that locates each
//!   variable's band.
//! * **zstd** is linked rather than piped through the CLI.
//! * The decoder in this same crate is the read-back verifier, so every bundle
//!   the encoder writes is decoded by the code the browser runs.
//!
//! Scope is `convert-bin`: gridded input to bundles, half-resolution variants,
//! posters and the manifest. Fetching, the showcase driver and the optional
//! H.264 companion artifacts stay in Python.
//!
//! It is behind the off-by-default `encoder` feature: it links GDAL, which the
//! decoder does not, and nothing that only decodes should have to find a GDAL
//! to build. `docs/format.md` is the normative spec for everything written
//! here, and `xuebuild/binconvert.py` is the reference the outputs are diffed
//! against.

// Only what something outside this crate actually links against is public:
// `xue-py` imports convert, gdalio, grid, poster, quantize and temporal, and
// the xue-encode binary — a separate crate against the lib, not part of it —
// adds errors. The rest is pipeline interior, crate-private so it stays free
// to move; what used to be tested through `tests/encoder.rs` is tested from
// inside those modules instead.
pub(crate) mod binformat;
pub mod convert;
pub mod errors;
pub mod gdalio;
pub mod grid;
pub(crate) mod gribindex;
pub(crate) mod inspect;
pub(crate) mod manifest;
pub(crate) mod metadata;
pub(crate) mod model;
pub(crate) mod observation;
pub(crate) mod parallel;
pub mod poster;
pub mod quantize;
pub(crate) mod reproject;
pub(crate) mod sources;
pub mod temporal;
pub(crate) mod variables;

pub use convert::{convert_bin, ConvertOptions};
pub use errors::{EncodeError, Result};
