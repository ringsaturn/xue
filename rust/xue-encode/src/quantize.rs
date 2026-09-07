//! Xue v1 quantization codebooks — the port of `xue/quantize.py`.
//!
//! All rounding is round-half-up: `round(x) = floor(x + 0.5)`. Every rounded
//! quantity here is non-negative, so this equals round-half-away-from-zero.
//! Round-half-even (Rust's `f64::round_ties_even`, and the IEEE 754 default)
//! must not be used; it changes codes for values landing exactly on a half
//! step.

use serde_json::{json, Map, Value};

use crate::errors::{EncodeError, Result};

/// Linear uint8 codebook. Not temperature-specific: it quantizes any linear
/// field; the wind components reuse it with a symmetric m/s range.
#[derive(Debug, Clone, Copy)]
pub struct LinearCodebook {
    pub minimum: f64,
    pub maximum: f64,
    pub step: f64,
    pub nodata_code: u16,
    pub name: &'static str,
}

impl LinearCodebook {
    pub fn maximum_code(&self) -> u16 {
        ((self.maximum - self.minimum) / self.step + 0.5).floor() as u16
    }

    pub fn metadata(&self) -> Map<String, Value> {
        let mut block = Map::new();
        block.insert("type".into(), json!("linear"));
        block.insert("offset".into(), json!(self.minimum));
        block.insert("scale".into(), json!(self.step));
        block.insert("minimumCode".into(), json!(0));
        block.insert("maximumCode".into(), json!(self.maximum_code()));
        block.insert("nodataCode".into(), json!(self.nodata_code));
        block
    }

    pub fn quantize(&self, values: &[f64], codes: &mut [u8]) -> Result<()> {
        debug_assert_eq!(values.len(), codes.len());
        for (value, code) in values.iter().zip(codes.iter_mut()) {
            if !value.is_finite() {
                return Err(EncodeError::conversion(format!(
                    "{} plane contains non-finite values",
                    self.name
                )));
            }
            let clamped = value.clamp(self.minimum, self.maximum);
            *code = ((clamped - self.minimum) / self.step + 0.5).floor() as u8;
        }
        Ok(())
    }

    pub fn decode(&self, code: u8) -> f64 {
        self.minimum + f64::from(code) * self.step
    }
}

/// Logarithmic precipitation codebook: sparse, long-tailed fields keep their
/// light-rain resolution.
#[derive(Debug, Clone, Copy)]
pub struct PrecipitationCodebook {
    pub trace: f64,
    pub scale: f64,
    pub maximum: f64,
    pub maximum_code: u16,
    pub overflow_code: u16,
    pub nodata_code: u16,
}

impl PrecipitationCodebook {
    fn lo(&self) -> f64 {
        (self.trace / self.scale).ln_1p()
    }

    fn hi(&self) -> f64 {
        (self.maximum / self.scale).ln_1p()
    }

    fn span(&self) -> f64 {
        f64::from(self.maximum_code - 1)
    }

    pub fn metadata(&self) -> Map<String, Value> {
        let mut block = Map::new();
        block.insert("type".into(), json!("log1p"));
        block.insert("trace".into(), json!(self.trace));
        block.insert("scale".into(), json!(self.scale));
        block.insert("maximum".into(), json!(self.maximum));
        block.insert("minimumCode".into(), json!(1));
        block.insert("maximumCode".into(), json!(self.maximum_code));
        block.insert("zeroCode".into(), json!(0));
        block.insert("overflowCode".into(), json!(self.overflow_code));
        block.insert("nodataCode".into(), json!(self.nodata_code));
        block
    }

    pub fn quantize(&self, values: &[f64], codes: &mut [u8]) -> Result<()> {
        debug_assert_eq!(values.len(), codes.len());
        let (lo, hi, span) = (self.lo(), self.hi(), self.span());
        for (rate, code) in values.iter().zip(codes.iter_mut()) {
            if !rate.is_finite() {
                return Err(EncodeError::conversion(
                    "precipitation plane contains non-finite values",
                ));
            }
            let unit = ((rate.clamp(0.0, self.maximum) / self.scale).ln_1p() - lo) / (hi - lo);
            let quantized = 1.0 + (span * unit + 0.5).floor();
            let clamped = quantized.clamp(1.0, f64::from(self.maximum_code)) as u8;
            *code = if *rate < self.trace {
                0
            } else if *rate > self.maximum {
                self.overflow_code as u8
            } else {
                clamped
            };
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub enum Codebook {
    Linear(LinearCodebook),
    Precipitation(PrecipitationCodebook),
}

impl Codebook {
    pub fn metadata(&self) -> Map<String, Value> {
        match self {
            Self::Linear(codebook) => codebook.metadata(),
            Self::Precipitation(codebook) => codebook.metadata(),
        }
    }

    pub fn quantize(&self, values: &[f64], codes: &mut [u8]) -> Result<()> {
        match self {
            Self::Linear(codebook) => codebook.quantize(values, codes),
            Self::Precipitation(codebook) => codebook.quantize(values, codes),
        }
    }

    pub fn as_linear(&self) -> Option<&LinearCodebook> {
        match self {
            Self::Linear(codebook) => Some(codebook),
            Self::Precipitation(_) => None,
        }
    }
}

const QUALITY_TEMPERATURE: LinearCodebook = LinearCodebook {
    minimum: -60.0,
    maximum: 50.0,
    step: 0.5,
    nodata_code: 255,
    name: "temperature",
};
const COMPACT_TEMPERATURE: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_TEMPERATURE
};
const QUALITY_PRECIPITATION: PrecipitationCodebook = PrecipitationCodebook {
    trace: 0.01,
    scale: 0.05,
    maximum: 128.0,
    maximum_code: 253,
    overflow_code: 254,
    nodata_code: 255,
};
const COMPACT_PRECIPITATION: PrecipitationCodebook = PrecipitationCodebook {
    maximum_code: 125,
    overflow_code: 126,
    nodata_code: 127,
    ..QUALITY_PRECIPITATION
};
// ±63.5 m/s covers every 10 m wind with headroom; the 0.5 m/s step is far
// below what a particle animation can resolve.
const QUALITY_WIND: LinearCodebook = LinearCodebook {
    minimum: -63.5,
    maximum: 63.5,
    step: 0.5,
    nodata_code: 255,
    name: "wind",
};
const COMPACT_WIND: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_WIND
};
// Surface downward shortwave radiation: 0–1270 W/m² with a 5 W/m² step uses
// the full 0..254 code space.
const QUALITY_FLUX: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 1270.0,
    step: 5.0,
    nodata_code: 255,
    name: "dswrf",
};
const COMPACT_FLUX: LinearCodebook = LinearCodebook {
    step: 10.0,
    ..QUALITY_FLUX
};
// Radar composite reflectivity. Code 0 is both "no echo" and "no radar
// coverage", deliberately an ordinary linear code rather than a reserved one.
const QUALITY_REFLECTIVITY: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 80.0,
    step: 0.5,
    nodata_code: 255,
    name: "cref",
};
const COMPACT_REFLECTIVITY: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_REFLECTIVITY
};

pub const PROFILES: &[&str] = &["quality", "compact", "balanced"];

/// One profile's codebook for one variable.
///
/// The `balanced` profile (production default) keeps temperature's 0.5 °C step
/// while precipitation drops to the 128-level codebook.
pub fn codebook(profile: &str, variable_id: &str) -> Result<Codebook> {
    let quality = matches!(profile, "quality" | "balanced");
    let book = match (profile, variable_id) {
        (_, "tmp2m") if quality => Codebook::Linear(QUALITY_TEMPERATURE),
        (_, "tmp2m") => Codebook::Linear(COMPACT_TEMPERATURE),
        ("quality", "prate") => Codebook::Precipitation(QUALITY_PRECIPITATION),
        (_, "prate") => Codebook::Precipitation(COMPACT_PRECIPITATION),
        (_, "ugrd10m" | "vgrd10m") if quality => Codebook::Linear(QUALITY_WIND),
        (_, "ugrd10m" | "vgrd10m") => Codebook::Linear(COMPACT_WIND),
        (_, "dswrf") if quality => Codebook::Linear(QUALITY_FLUX),
        (_, "dswrf") => Codebook::Linear(COMPACT_FLUX),
        (_, "cref") if quality => Codebook::Linear(QUALITY_REFLECTIVITY),
        (_, "cref") => Codebook::Linear(COMPACT_REFLECTIVITY),
        _ => {
            return Err(EncodeError::conversion(format!(
                "no {profile} codebook for {variable_id}"
            )))
        }
    };
    if !PROFILES.contains(&profile) {
        return Err(EncodeError::conversion(format!("unknown profile: {profile}")));
    }
    Ok(book)
}
