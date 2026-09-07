//! An experimental native encoder for the Xue v1 bundle format.
//!
//! The production encoder is Python (`xue/`), orchestrating GDAL, zstd and
//! ffmpeg as CLI subprocesses. This crate is the same pipeline in Rust with
//! the external tools linked in process instead:
//!
//! * **GDAL** through `gdal-sys` (georust) replaces `gdalinfo -json` and
//!   `gdal_translate -of ENVI` — a production GFS run wrote and re-read
//!   several gigabytes of intermediate ENVI files per build, which the
//!   Python-side profiling identified as the remaining wall.
//! * **grib-rs** replaces the hand-rolled GRIB2 header index that locates each
//!   variable's band.
//! * **zstd** is linked rather than piped through the CLI.
//! * The **`xue` decoder crate** is the read-back verifier, so every bundle
//!   this encoder writes is decoded by the same code the browser runs.
//!
//! Scope is `convert-bin`: gridded input to bundles, half-resolution variants,
//! posters and the manifest. Fetching, the showcase driver and the optional
//! H.264 companion artifacts stay in Python.
//!
//! `docs/format.md` is the normative spec for everything written here, and
//! `xue/binconvert.py` is the reference the outputs are diffed against.

pub mod binformat;
pub mod convert;
pub mod errors;
pub mod gdalio;
pub mod grid;
pub mod gribindex;
pub mod inspect;
pub mod manifest;
pub mod metadata;
pub mod model;
pub mod observation;
pub mod parallel;
pub mod poster;
pub mod quantize;
pub mod sources;
pub mod temporal;
pub mod variables;

pub use convert::{convert_bin, ConvertOptions};
pub use errors::{EncodeError, Result};
