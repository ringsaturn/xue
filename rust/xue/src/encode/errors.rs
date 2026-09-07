//! The pipeline's user-actionable error type.
//!
//! Mirrors `xue/errors.py`: everything a user can fix (a bad GRIB file, a
//! missing record, an axis that is not on the model's cadence) is an
//! `EncodeError`, which the CLI turns into `error: …` and exit code 2.
//! Anything else is a bug and panics or bubbles up as `anyhow::Error`.

use std::fmt;

#[derive(Debug)]
pub enum EncodeError {
    /// A GRIB/NetCDF file or a conversion step was invalid.
    Conversion(String),
    /// The public manifest violates the versioned contract.
    Manifest(String),
    /// A Xue bundle violates the versioned binary contract.
    Bundle(String),
}

impl EncodeError {
    pub fn conversion(message: impl Into<String>) -> Self {
        Self::Conversion(message.into())
    }

    pub fn manifest(message: impl Into<String>) -> Self {
        Self::Manifest(message.into())
    }

    pub fn bundle(message: impl Into<String>) -> Self {
        Self::Bundle(message.into())
    }
}

impl fmt::Display for EncodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Conversion(message) | Self::Manifest(message) | Self::Bundle(message) => {
                formatter.write_str(message)
            }
        }
    }
}

impl std::error::Error for EncodeError {}

pub type Result<T> = std::result::Result<T, EncodeError>;
