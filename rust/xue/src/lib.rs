//! Xue v1 bundle parser and frame decoder.
//!
//! The binary contract is defined in `docs/format.md` and mirrored by the
//! Python reference implementation in `xuebuild/binformat.py`. Every
//! integer computation on untrusted input uses checked arithmetic, and no
//! allocation is sized from a file value before it is validated against the
//! metadata grid and the file length.
//!
//! Two readers share the same structural validation and decode logic:
//! [`Bundle`] opens a complete file, while [`StreamingBundle`] opens only the
//! structural prefix (header + metadata + index) and accepts payload bytes
//! incrementally as HTTP range responses arrive.
//!
//! The crate is arranged as the format in the middle and the two directions
//! around it:
//!
//! * [`format`] — the container's byte layout, and nothing else: the
//!   constants, the enums, and the pack/unpack pair for each structure. Both
//!   directions go through it, so a field cannot drift between them.
//! * [`decode`] — reading, re-exported here. Links nothing, compiles to wasm.
//! * [`encode`] — the native encoder, behind the off-by-default `encoder`
//!   feature because it links GDAL.

pub mod format;

mod decode;

pub use decode::{Bundle, StreamingBundle};
pub use format::{
    align8, Compression, DecodeError, FixedHeader, FrameRequest, IndexHeader, PlaneEntry,
    Predictor, ENTRY_SIZE, FLAG_ZSTD_CHECKSUM, HEADER_SIZE, HOUR_SECONDS, INDEX_HEADER_SIZE,
    INDEX_MAGIC, INDEX_VERSION, MAGIC, MAX_PLANE_LENGTH, NO_DEPENDENCY, VERSION,
};

// The experimental native encoder, behind an off-by-default feature because it
// links GDAL. Everything else in this crate is the decoder, which links nothing.
#[cfg(feature = "encoder")]
pub mod encode;
