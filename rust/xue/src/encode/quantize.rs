//! Xue v1 quantization codebooks — the port of `xuebuild/quantize.py`.
//!
//! A codebook is built from the `quantization.<profile>` block of a
//! variable's registry entry, exactly as the metadata writes it, and hands
//! the block back verbatim for the metadata (`Codebook::metadata`), so a
//! store's `attributes.xue` is the document's block byte for byte. The port
//! of `codebook_from_metadata` in `xuebuild/quantize.py`, refusals included.
//!
//! All rounding is round-half-up: `round(x) = floor(x + 0.5)`. Every rounded
//! quantity here is non-negative, so this equals round-half-away-from-zero.
//! Round-half-even (Rust's `f64::round_ties_even`, and the IEEE 754 default)
//! must not be used; it changes codes for values landing exactly on a half
//! step.

use serde_json::{Map, Value};

use crate::encode::errors::{EncodeError, Result};

/// Linear uint8 codebook. Not temperature-specific: it quantizes any linear
/// field; the wind components reuse it with a symmetric m/s range.
#[derive(Debug, Clone)]
pub struct LinearCodebook {
    pub minimum: f64,
    /// `minimum + maximum_code × step`, the top of the clamp.
    pub maximum: f64,
    pub step: f64,
    pub nodata_code: u16,
    /// The variable's id, for the error a non-finite plane raises.
    pub name: String,
}

impl LinearCodebook {
    pub fn maximum_code(&self) -> u16 {
        ((self.maximum - self.minimum) / self.step + 0.5).floor() as u16
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

/// Logarithmic (`log1p`) codebook: sparse, long-tailed fields keep their
/// light-rain resolution. Not precipitation-specific either: it quantizes
/// any one-sided field whose interesting range spans orders of magnitude —
/// the aerosol optical depth, the particulate matter concentrations and a
/// river's discharge reuse it with their own trace, scale and maximum.
#[derive(Debug, Clone)]
pub struct PrecipitationCodebook {
    pub trace: f64,
    pub scale: f64,
    pub maximum: f64,
    pub maximum_code: u16,
    pub overflow_code: u16,
    pub nodata_code: u16,
    pub name: String,
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

    pub fn quantize(&self, values: &[f64], codes: &mut [u8]) -> Result<()> {
        debug_assert_eq!(values.len(), codes.len());
        let (lo, hi, span) = (self.lo(), self.hi(), self.span());
        for (rate, code) in values.iter().zip(codes.iter_mut()) {
            if !rate.is_finite() {
                return Err(EncodeError::conversion(format!(
                    "{} plane contains non-finite values",
                    self.name
                )));
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

#[derive(Debug, Clone)]
pub enum Book {
    Linear(LinearCodebook),
    Precipitation(PrecipitationCodebook),
}

/// One profile's codebook for one variable: the arithmetic, and the block
/// it was read from.
#[derive(Debug, Clone)]
pub struct Codebook {
    pub book: Book,
    block: Map<String, Value>,
}

impl Codebook {
    /// The codebook a `quantization` block describes (the inverse of
    /// `metadata`). Refuses a block whose code range does not follow from
    /// its own parameters, since then it does not say one thing. Mirrors
    /// `codebook_from_metadata` in `xuebuild/quantize.py`, check for check.
    pub fn from_metadata(block: &Map<String, Value>, name: &str) -> Result<Codebook> {
        let number = |key: &str| -> Result<f64> {
            block
                .get(key)
                .and_then(Value::as_f64)
                .filter(|value| value.is_finite())
                .ok_or_else(|| EncodeError::conversion(format!("{name} quantization {key} must be a finite number")))
        };
        let code = |key: &str| -> Result<u16> {
            block
                .get(key)
                .and_then(Value::as_u64)
                .filter(|value| *value <= 255)
                .map(|value| value as u16)
                .ok_or_else(|| EncodeError::conversion(format!("{name} quantization {key} must be a byte")))
        };
        let book = match block.get("type").and_then(Value::as_str) {
            Some("linear") => {
                let step = number("scale")?;
                if step <= 0.0 || code("minimumCode")? != 0 {
                    return Err(EncodeError::conversion(format!(
                        "{name} linear quantization needs a positive scale and minimumCode 0"
                    )));
                }
                let offset = number("offset")?;
                let maximum_code = code("maximumCode")?;
                let codebook = LinearCodebook {
                    minimum: offset,
                    maximum: offset + f64::from(maximum_code) * step,
                    step,
                    nodata_code: code("nodataCode")?,
                    name: name.to_string(),
                };
                if codebook.maximum_code() != maximum_code || codebook.nodata_code <= maximum_code {
                    return Err(EncodeError::conversion(format!(
                        "{name} linear quantization code range is inconsistent"
                    )));
                }
                Book::Linear(codebook)
            }
            Some("log1p") => {
                if code("minimumCode")? != 1 || code("zeroCode")? != 0 {
                    return Err(EncodeError::conversion(format!(
                        "{name} log1p quantization needs zeroCode 0 and minimumCode 1"
                    )));
                }
                let codebook = PrecipitationCodebook {
                    trace: number("trace")?,
                    scale: number("scale")?,
                    maximum: number("maximum")?,
                    maximum_code: code("maximumCode")?,
                    overflow_code: code("overflowCode")?,
                    nodata_code: code("nodataCode")?,
                    name: name.to_string(),
                };
                if !(0.0 < codebook.trace && codebook.trace < codebook.maximum)
                    || codebook.scale <= 0.0
                    || !(1 < codebook.maximum_code
                        && codebook.maximum_code < codebook.overflow_code
                        && codebook.overflow_code < codebook.nodata_code)
                {
                    return Err(EncodeError::conversion(format!(
                        "{name} log1p quantization parameters are inconsistent"
                    )));
                }
                Book::Precipitation(codebook)
            }
            other => {
                return Err(EncodeError::conversion(format!(
                    "{name} quantization type {} is not one this encoder quantizes",
                    other.map_or_else(|| "null".to_string(), |kind| format!("{kind:?}"))
                )))
            }
        };
        Ok(Codebook {
            book,
            block: block.clone(),
        })
    }

    /// The block, as the document carries it and the metadata writes it.
    pub fn metadata(&self) -> Map<String, Value> {
        self.block.clone()
    }

    pub fn quantize(&self, values: &[f64], codes: &mut [u8]) -> Result<()> {
        match &self.book {
            Book::Linear(codebook) => codebook.quantize(values, codes),
            Book::Precipitation(codebook) => codebook.quantize(values, codes),
        }
    }

    pub fn as_linear(&self) -> Option<&LinearCodebook> {
        match &self.book {
            Book::Linear(codebook) => Some(codebook),
            Book::Precipitation(_) => None,
        }
    }

    pub fn as_precipitation(&self) -> Option<&PrecipitationCodebook> {
        match &self.book {
            Book::Linear(_) => None,
            Book::Precipitation(codebook) => Some(codebook),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Codebook;
    use crate::encode::registry::Registry;
    use serde_json::{json, Map, Value};

    fn block(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn linear_codebook_rounds_half_up() {
        let book = Registry::embedded().codebook("quality", "tmp2m").unwrap();
        // -60 + 0.5 * 1 = -59.5 is exactly on a step; -59.75 sits exactly
        // half way and must round up, which round-half-even would not do.
        let values = [-60.0, -59.75, -59.5, 0.0, 50.0, 99.0, -99.0];
        let mut codes = [0u8; 7];
        book.quantize(&values, &mut codes).unwrap();
        assert_eq!(codes, [0, 1, 1, 120, 220, 220, 0]);
        assert_eq!(book.as_linear().unwrap().maximum, 50.0);
        assert_eq!(book.as_linear().unwrap().decode(120), 0.0);
        assert!(book.quantize(&[f64::NAN], &mut [0]).unwrap_err().to_string().contains("tmp2m"));
    }

    #[test]
    fn precipitation_codebook_marks_dry_and_overflow() {
        let book = Registry::embedded().codebook("quality", "prate").unwrap();
        let values = [0.0, 0.009, 0.01, 1.0, 128.0, 200.0];
        let mut codes = [0u8; 6];
        book.quantize(&values, &mut codes).unwrap();
        assert_eq!(codes[0], 0, "dry");
        assert_eq!(codes[1], 0, "below trace is dry");
        assert_eq!(codes[2], 1, "trace is the first positive code");
        assert_eq!(codes[4], 253, "the maximum decodes to exactly the maximum");
        assert_eq!(codes[5], 254, "above the maximum is the overflow code");
        assert!(codes[3] > 1 && codes[3] < 253);
        assert_eq!(book.as_precipitation().unwrap().overflow_code, 254);
    }

    /// The block comes back as it went in, and a block that does not say
    /// one thing is refused the way the reference refuses it.
    #[test]
    fn a_codebook_is_its_block_and_nothing_else() {
        let linear = block(json!({"type": "linear", "offset": 0.0, "scale": 0.02, "minimumCode": 0, "maximumCode": 126, "nodataCode": 255}));
        let book = Codebook::from_metadata(&linear, "clw").unwrap();
        assert_eq!(book.metadata(), linear);
        // 2.53 / 0.02 is a hair under 126.5 in binary floating point, so a
        // declared top code of 127 would not follow from the parameters.
        let mut wrong = linear.clone();
        wrong.insert("maximumCode".into(), json!(127));
        assert!(Codebook::from_metadata(&wrong, "clw").unwrap_err().to_string().contains("inconsistent"));
        let mut wrong = linear.clone();
        wrong.insert("nodataCode".into(), json!(100));
        assert!(Codebook::from_metadata(&wrong, "clw").is_err());
        let mut wrong = linear.clone();
        wrong.insert("minimumCode".into(), json!(1));
        assert!(Codebook::from_metadata(&wrong, "clw").is_err());
        let mut wrong = linear.clone();
        wrong.insert("scale".into(), json!("0.02"));
        assert!(Codebook::from_metadata(&wrong, "clw").unwrap_err().to_string().contains("scale"));

        let log = block(json!({"type": "log1p", "trace": 0.01, "scale": 0.05, "maximum": 128.0, "minimumCode": 1, "maximumCode": 253, "zeroCode": 0, "overflowCode": 254, "nodataCode": 255}));
        assert_eq!(Codebook::from_metadata(&log, "prate").unwrap().metadata(), log);
        let mut wrong = log.clone();
        wrong.insert("overflowCode".into(), json!(253));
        assert!(Codebook::from_metadata(&wrong, "prate").is_err());
        let mut wrong = log.clone();
        wrong.insert("zeroCode".into(), json!(1));
        assert!(Codebook::from_metadata(&wrong, "prate").is_err());
        let mut wrong = log.clone();
        wrong.insert("type".into(), json!("cubic"));
        assert!(Codebook::from_metadata(&wrong, "prate").unwrap_err().to_string().contains("cubic"));
    }
}
