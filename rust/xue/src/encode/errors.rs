//! The pipeline's user-actionable error type.
//!
//! Mirrors `xuebuild/errors.py`: everything a user can fix (a bad GRIB file, a
//! missing record, an axis that is not on the model's cadence) is an
//! `EncodeError`, which the CLI turns into `error: …` and exit code 2.
//! Anything else is a bug and panics or bubbles up as `anyhow::Error`.

use std::fmt;

/// One message, whatever stage raised it: nothing matches on the kind, and
/// the CLI prints every one the same way. The constructors name the stage
/// at the call site.
#[derive(Debug)]
pub struct EncodeError(pub String);

impl EncodeError {
    /// A GRIB/NetCDF file or a conversion step was invalid.
    pub fn conversion(message: impl Into<String>) -> Self {
        Self(message.into())
    }

    /// The public manifest violates the versioned contract.
    pub fn manifest(message: impl Into<String>) -> Self {
        Self(message.into())
    }

    /// A Xue bundle or store violates the versioned contract.
    pub fn bundle(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for EncodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for EncodeError {}

pub type Result<T> = std::result::Result<T, EncodeError>;
