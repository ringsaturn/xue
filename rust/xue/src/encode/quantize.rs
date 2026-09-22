//! Xue v1 quantization codebooks — the port of `xuebuild/quantize.py`.
//!
//! All rounding is round-half-up: `round(x) = floor(x + 0.5)`. Every rounded
//! quantity here is non-negative, so this equals round-half-away-from-zero.
//! Round-half-even (Rust's `f64::round_ties_even`, and the IEEE 754 default)
//! must not be used; it changes codes for values landing exactly on a half
//! step.

use serde_json::{json, Map, Value};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::variables::{isobaric_variable, AEROSOL_VARIABLE_IDS, SATELLITE_CHANNEL_IDS};

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

/// Logarithmic (`log1p`) codebook: sparse, long-tailed fields keep their
/// light-rain resolution. Not precipitation-specific either: it quantizes
/// any one-sided field whose interesting range spans orders of magnitude —
/// the aerosol optical depth and the particulate matter concentrations
/// reuse it with their own trace, scale and maximum.
#[derive(Debug, Clone, Copy)]
pub struct PrecipitationCodebook {
    pub trace: f64,
    pub scale: f64,
    pub maximum: f64,
    pub maximum_code: u16,
    pub overflow_code: u16,
    pub nodata_code: u16,
    pub name: &'static str,
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
    name: "precipitation",
};
const COMPACT_PRECIPITATION: PrecipitationCodebook = PrecipitationCodebook {
    maximum_code: 125,
    overflow_code: 126,
    nodata_code: 127,
    ..QUALITY_PRECIPITATION
};
// The aerosol fields, three log1p codebooks. Their interesting range spans
// orders of magnitude the way a rain rate's does — a clear sky reads an
// optical depth of 0.05, a dust plume 2, and a linear step fine enough for
// the haze would spend the codes on the plume — so each takes the
// precipitation codebook's shape with its own numbers. The optical depth
// runs to 5 (a severe dust storm's ceiling; the converter clamps there, so
// the overflow code is never written) at a scale of 0.05; PM2.5 runs to
// 1000 µg/m³ and the two PM10 fields to 2000, at a scale of 5 µg/m³ with a
// trace of half a microgram. The compact profile halves the code space as
// it does for precipitation; the balanced profile takes the quality
// codebooks, since the fields are smooth. Mirror `QUALITY_AOD` /
// `QUALITY_PM25` / `QUALITY_PM10` and their compact twins in
// `xuebuild/quantize.py`; the name is the variable's own there, as here.
const fn aerosol_codebook(variable_id: &'static str, compact: bool) -> PrecipitationCodebook {
    let (trace, scale, maximum) = match variable_id.as_bytes() {
        [b'a', b'o', b'd', ..] => (0.005, 0.05, 5.0),
        b"pm25" => (0.5, 5.0, 1000.0),
        _ => (0.5, 5.0, 2000.0),
    };
    PrecipitationCodebook {
        trace,
        scale,
        maximum,
        maximum_code: if compact { 125 } else { 253 },
        overflow_code: if compact { 126 } else { 254 },
        nodata_code: if compact { 127 } else { 255 },
        name: variable_id,
    }
}
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
// The satellite infrared window: brightness temperature from 180 K (also
// what the cells outside the disk become) to 331.8 K at 0.6 K, the full
// code space; the compact profile doubles the step and stops a code short.
// Mirrors `QUALITY_BRIGHTNESS_TEMPERATURE` in `xuebuild/quantize.py`.
const QUALITY_BRIGHTNESS_TEMPERATURE: LinearCodebook = LinearCodebook {
    minimum: 180.0,
    maximum: 331.8,
    step: 0.6,
    nodata_code: 255,
    name: "ir104",
};
const COMPACT_BRIGHTNESS_TEMPERATURE: LinearCodebook = LinearCodebook {
    maximum: 331.2,
    step: 1.2,
    ..QUALITY_BRIGHTNESS_TEMPERATURE
};
/// Every infrared window shares the two brightness-temperature codebooks;
/// the name is the channel's own. Mirrors `_brightness_temperature` in
/// `xuebuild/quantize.py`.
fn brightness_temperature(channel_id: &'static str, compact: bool) -> LinearCodebook {
    let base = if compact { COMPACT_BRIGHTNESS_TEMPERATURE } else { QUALITY_BRIGHTNESS_TEMPERATURE };
    LinearCodebook { name: channel_id, ..base }
}
// A Dust RGB gun is a stretched, gamma-corrected number in 0–1 (the
// `shachen` producer in the fetch stage): 250 steps of 0.004 across the
// range, more than a display resolves. The codebook starts one step
// *below* zero so that code 0 is "no data" — a cell the disk does not
// cover, or one an input channel lacked, which the shell leaves unpainted
// when any gun carries it — while black (0, 0, 0) stays a value at code 1.
// One codebook for every profile: the quantity is already a display value,
// and a coarser one would band the picture. Mirrors `DUST_RGB_GUN` in
// `xuebuild/quantize.py`.
const DUST_RGB_GUN: LinearCodebook = LinearCodebook {
    minimum: -0.004,
    maximum: 1.0,
    step: 0.004,
    nodata_code: 255,
    name: "dustrgb",
};
// The DEBRA confidence is a number in 0–1 too, and takes the guns'
// codebook for the same reasons: code 0 is "no data", 0.0 confidence is
// code 1. Mirrors `DUST_CF` in `xuebuild/quantize.py`.
const DUST_CF: LinearCodebook = LinearCodebook {
    minimum: -0.004,
    maximum: 1.0,
    step: 0.004,
    nodata_code: 255,
    name: "dustcf",
};
// Wind gust: one-sided, at the 10 m components' step over the isobaric
// wind's 127 m/s ceiling, spending the full 0..254 code space.
const QUALITY_GUST: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 127.0,
    step: 0.5,
    nodata_code: 255,
    name: "gust",
};
const COMPACT_GUST: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_GUST
};
// Total cloud cover: 0–100 % at half a percent, relative humidity's numbers;
// like it, balanced takes the 1 % step.
const QUALITY_CLOUD: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 100.0,
    step: 0.5,
    nodata_code: 255,
    name: "tcdc",
};
const COMPACT_CLOUD: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_CLOUD
};
// CAPE: 0–6350 J/kg at 25 J/kg spends the full 0..254 code space; the rare
// extreme past it clamps.
const QUALITY_CAPE: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 6350.0,
    step: 25.0,
    nodata_code: 255,
    name: "cape",
};
const COMPACT_CAPE: LinearCodebook = LinearCodebook {
    step: 50.0,
    ..QUALITY_CAPE
};
// Convective inhibition: cape's twin one-sided the other way, from the zero
// the whole column shares down to the -1016 J/kg a strong cap reaches at
// 4 J/kg — finer than cape's 25 J/kg because the interesting range (0 to
// -200) is a fraction of cape's, and coarse enough to spend the full 0..254
// code space. Mirrors `QUALITY_CIN` in `xuebuild/quantize.py`.
const QUALITY_CIN: LinearCodebook = LinearCodebook {
    minimum: -1016.0,
    maximum: 0.0,
    step: 4.0,
    nodata_code: 255,
    name: "cin",
};
const COMPACT_CIN: LinearCodebook = LinearCodebook {
    step: 8.0,
    ..QUALITY_CIN
};
// Precipitable water: 0–127 kg/m² at half a kilogram spends the full code
// space over the ~80 mm a saturated tropical column holds; the compact
// profile keeps the range at a whole kg/m². Mirrors `QUALITY_PWAT`.
const QUALITY_PWAT: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 127.0,
    step: 0.5,
    nodata_code: 255,
    name: "pwat",
};
const COMPACT_PWAT: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_PWAT
};
// Planetary boundary layer height: 0–5080 m at 20 m spends the full code
// space over the ~5 km a deep boundary layer reaches; doubled in compact.
// Mirrors `QUALITY_PBL`.
const QUALITY_PBL: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 5080.0,
    step: 20.0,
    nodata_code: 255,
    name: "hpbl",
};
const COMPACT_PBL: LinearCodebook = LinearCodebook {
    step: 40.0,
    ..QUALITY_PBL
};
// Orography: the model terrain, −430 m (the Dead Sea shore) to 8968 m at a
// 37 m step, spending the full 0..254 code space. Static, so the step is a
// display precision rather than a forecast resolution. Mirrors
// `QUALITY_OROGRAPHY` in `xuebuild/quantize.py`.
const QUALITY_OROGRAPHY: LinearCodebook = LinearCodebook {
    minimum: -430.0,
    maximum: 8968.0,
    step: 37.0,
    nodata_code: 255,
    name: "orog",
};
const COMPACT_OROGRAPHY: LinearCodebook = LinearCodebook {
    step: 74.0,
    ..QUALITY_OROGRAPHY
};
// Precipitation type: a categorical codebook of WMO code table 4.201's own
// values (1 rain, 3 freezing rain, 5 snow, 8 ice pellets) with 0 for none —
// integer codes with no error budget, the same book in every profile. Mirrors
// `PTYPE` in `xuebuild/quantize.py`.
const PTYPE: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 8.0,
    step: 1.0,
    nodata_code: 255,
    name: "ptype",
};
// Visibility in kilometres: 0–25.4 km at 100 m spends the full code space
// over GFS's ~24 km ceiling.
const QUALITY_VISIBILITY: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 25.4,
    step: 0.1,
    nodata_code: 255,
    name: "vis",
};
const COMPACT_VISIBILITY: LinearCodebook = LinearCodebook {
    step: 0.2,
    ..QUALITY_VISIBILITY
};
// 2 m dew point: the temperature's step over a range shifted ten degrees down.
const QUALITY_DEW_POINT: LinearCodebook = LinearCodebook {
    minimum: -70.0,
    maximum: 40.0,
    step: 0.5,
    nodata_code: 255,
    name: "dpt2m",
};
const COMPACT_DEW_POINT: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_DEW_POINT
};
// Apparent temperature spans 150 K, which no half-degree codebook holds, so
// it takes a whole degree.
const QUALITY_APPARENT: LinearCodebook = LinearCodebook {
    minimum: -90.0,
    maximum: 60.0,
    step: 1.0,
    nodata_code: 255,
    name: "aptmp2m",
};
const COMPACT_APPARENT: LinearCodebook = LinearCodebook {
    step: 2.0,
    ..QUALITY_APPARENT
};
// The ocean fields (see `xuebuild/quantize.py` for the reasoning behind each
// range). The surface (skin) temperature keeps the 2 m temperature's step
// and floor and runs to 67 °C, where a desert skin goes.
const QUALITY_SURFACE_TEMPERATURE: LinearCodebook = LinearCodebook {
    minimum: -60.0,
    maximum: 67.0,
    step: 0.5,
    nodata_code: 255,
    name: "tmpsfc",
};
const COMPACT_SURFACE_TEMPERATURE: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_SURFACE_TEMPERATURE
};
// Sea ice cover in percent: the cloud cover's numbers and its balanced rule.
const QUALITY_ICE_COVER: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 100.0,
    step: 0.5,
    nodata_code: 255,
    name: "icec",
};
const COMPACT_ICE_COVER: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_ICE_COVER
};
// Sea ice thickness: 0–5.08 m at 2 cm over the 5 m GFS caps the field at.
const QUALITY_ICE_THICKNESS: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 5.08,
    step: 0.02,
    nodata_code: 255,
    name: "icetk",
};
const COMPACT_ICE_THICKNESS: LinearCodebook = LinearCodebook {
    step: 0.04,
    ..QUALITY_ICE_THICKNESS
};
// Significant wave height and primary wave period: 0–25.4 at a tenth.
const QUALITY_WAVE_HEIGHT: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 25.4,
    step: 0.1,
    nodata_code: 255,
    name: "htsgw",
};
const COMPACT_WAVE_HEIGHT: LinearCodebook = LinearCodebook {
    step: 0.2,
    ..QUALITY_WAVE_HEIGHT
};
const QUALITY_WAVE_PERIOD: LinearCodebook = LinearCodebook {
    name: "perpw",
    ..QUALITY_WAVE_HEIGHT
};
const COMPACT_WAVE_PERIOD: LinearCodebook = LinearCodebook {
    step: 0.2,
    ..QUALITY_WAVE_PERIOD
};
// Primary wave direction: 1.5° over 0–358.5; the compact profile stops one
// code short of 360 as well (357°), so no code aliases 0 in either.
const QUALITY_WAVE_DIRECTION: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 358.5,
    step: 1.5,
    nodata_code: 255,
    name: "dirpw",
};
const COMPACT_WAVE_DIRECTION: LinearCodebook = LinearCodebook {
    maximum: 357.0,
    step: 3.0,
    ..QUALITY_WAVE_DIRECTION
};
// The wave vector components: symmetric over the height codebook's ±25.4 m
// at 0.2 m, the full 0..254 code space; the compact profile stops at ±25.2
// so that 0 — land, which must quantize to (0, 0) exactly — stays on the
// grid (see `xuebuild/quantize.py`).
const QUALITY_WAVE_VECTOR: LinearCodebook = LinearCodebook {
    minimum: -25.4,
    maximum: 25.4,
    step: 0.2,
    nodata_code: 255,
    name: "wave vector",
};
const COMPACT_WAVE_VECTOR: LinearCodebook = LinearCodebook {
    minimum: -25.2,
    maximum: 25.2,
    step: 0.4,
    ..QUALITY_WAVE_VECTOR
};
// The cloud layers take the total's codebook, and its balanced rule.
const fn cloud_layer_codebook(name: &'static str, compact: bool) -> LinearCodebook {
    LinearCodebook {
        minimum: 0.0,
        maximum: 100.0,
        step: if compact { 1.0 } else { 0.5 },
        nodata_code: 255,
        name,
    }
}

// Sea level pressure and the pressure-level geopotential heights. The three
// rules that fix these numbers are documented in `xuebuild/quantize.py`; the
// one a reader of this file needs is **half-code alignment**: every standard
// contour value falls exactly halfway between two codes, so a contour never
// coincides with a code value and lights up a flat plateau wholesale. It is
// an encoder rule — nothing in the container depends on it.
//
// The quality profile spends all 254 in-range codes from the offset; the
// compact profile covers the same range at twice the step (and gives the
// alignment up along with half the code space).
//
// This table must stay identical to `_PRESSURE_STEPS` in
// `xuebuild/quantize.py`, or the two encoders stop being byte-identical.
const PRESSURE_STEPS: &[(&str, f64, f64)] = &[
    // (variable id, offset, quality step)
    ("prmsl", 870.5, 1.0),
    // 1000 hPa takes a 10 m step rather than the 6 m the other 30 m-interval
    // levels use: its envelope spans 1474 m and 6 x 254 leaves no usable
    // margin. 10 divides 30, so the alignment rule still holds.
    ("hgt1000", -905.0, 10.0),
    ("hgt925", -249.0, 6.0),
    ("hgt850", 423.0, 6.0),
    ("hgt700", 1911.0, 6.0),
    ("hgt500", 4252.0, 8.0),
    ("hgt300", 7505.0, 10.0),
    ("hgt250", 8598.0, 12.0),
    ("hgt200", 10086.0, 12.0),
];

/// The pressure-family codebook for one variable, or `None` when the variable
/// is not in that family.
fn pressure_codebook(variable_id: &str, compact: bool) -> Option<LinearCodebook> {
    let &(name, offset, step) = PRESSURE_STEPS
        .iter()
        .find(|(id, _, _)| *id == variable_id)?;
    Some(LinearCodebook {
        minimum: offset,
        maximum: offset + step * 254.0,
        step: if compact { step * 2.0 } else { step },
        nodata_code: 255,
        name,
    })
}

// The filled isobaric families — temperature, relative humidity, specific
// humidity, the wind components and the water vapour flux components on the
// same eight surfaces. None is contoured, so none carries the half-code rule;
// what they share with the pressure family is fixed coverage per variable for
// all time, and a compact profile at twice the step. The tables must stay
// identical to their namesakes in `xuebuild/quantize.py`.
//
// Temperature keeps tmp2m's 0.5 °C step and takes its range per level: the
// low end holds the Antarctic winter at every surface, the high end the
// below-ground extrapolation the lowest surfaces take under high terrain.
const ISOBARIC_TEMPERATURE_RANGES: &[(u32, f64, f64)] = &[
    (1000, -60.0, 60.0),
    (925, -65.0, 50.0),
    (850, -70.0, 45.0),
    (700, -75.0, 35.0),
    (500, -85.0, 15.0),
    (300, -95.0, 0.0),
    (250, -100.0, -5.0),
    (200, -100.0, -10.0),
];
// Specific humidity spans two orders of magnitude between the surface and the
// upper troposphere, so its step follows the level; every range spends the
// full 0..254 code space.
const SPECIFIC_HUMIDITY_STEPS: &[(u32, f64)] = &[
    (1000, 0.2),
    (925, 0.2),
    (850, 0.1),
    (700, 0.1),
    (500, 0.02),
    (300, 0.01),
    (250, 0.005),
    (200, 0.005),
];
// Relative humidity: 0–100 % at half a percent, one codebook for every level.
const QUALITY_HUMIDITY: LinearCodebook = LinearCodebook {
    minimum: 0.0,
    maximum: 100.0,
    step: 0.5,
    nodata_code: 255,
    name: "rh",
};
const COMPACT_HUMIDITY: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_HUMIDITY
};
// Isobaric wind components: a jet core passes 100 m/s, so the isobaric pair
// takes ±127 m/s at a 1 m/s step.
const QUALITY_ISOBARIC_WIND: LinearCodebook = LinearCodebook {
    minimum: -127.0,
    maximum: 127.0,
    step: 1.0,
    nodata_code: 255,
    name: "isobaric wind",
};
const COMPACT_ISOBARIC_WIND: LinearCodebook = LinearCodebook {
    step: 2.0,
    ..QUALITY_ISOBARIC_WIND
};
// Water vapour flux components, q·V/g in g·cm⁻¹·hPa⁻¹·s⁻¹: the 10 m wind's
// own numbers cover everything but a typhoon core, which clamps.
const QUALITY_VAPOUR_FLUX: LinearCodebook = LinearCodebook {
    minimum: -63.5,
    maximum: 63.5,
    step: 0.5,
    nodata_code: 255,
    name: "vapour flux",
};
const COMPACT_VAPOUR_FLUX: LinearCodebook = LinearCodebook {
    step: 1.0,
    ..QUALITY_VAPOUR_FLUX
};
// Vertical velocity ω in Pa/s, symmetric like the wind components: 0.05 Pa/s
// gives synoptic ascent forty codes, and ±6.35 keeps a convective core
// distinct before the rare grid-point storm past it clamps.
const QUALITY_VERTICAL_VELOCITY: LinearCodebook = LinearCodebook {
    minimum: -6.35,
    maximum: 6.35,
    step: 0.05,
    nodata_code: 255,
    name: "vvel",
};
const COMPACT_VERTICAL_VELOCITY: LinearCodebook = LinearCodebook {
    step: 0.1,
    ..QUALITY_VERTICAL_VELOCITY
};
// Equivalent potential temperature: the temperature's 0.5 K step over a
// 127 K window placed per level. Must stay identical to `_THETA_E_OFFSETS`
// in `xuebuild/quantize.py`.
const THETA_E_OFFSETS: &[(u32, f64)] = &[
    (1000, 235.0),
    (925, 232.0),
    (850, 230.0),
    (700, 235.0),
    (500, 250.0),
    (300, 285.0),
    (250, 295.0),
    (200, 305.0),
];

/// The codebook of one filled isobaric variable, or `None` when the variable
/// is not one (the pressure family answers through `pressure_codebook`).
fn isobaric_codebook(variable_id: &str, compact: bool) -> Option<LinearCodebook> {
    let (family, level) = isobaric_variable(variable_id)?;
    Some(match family {
        "tmp" => {
            let &(_, minimum, maximum) = ISOBARIC_TEMPERATURE_RANGES
                .iter()
                .find(|(at, _, _)| *at == level)?;
            LinearCodebook {
                minimum,
                maximum,
                step: if compact { 1.0 } else { 0.5 },
                nodata_code: 255,
                name: "isobaric temperature",
            }
        }
        "rh" => {
            if compact {
                COMPACT_HUMIDITY
            } else {
                QUALITY_HUMIDITY
            }
        }
        "spfh" => {
            let &(_, step) = SPECIFIC_HUMIDITY_STEPS.iter().find(|(at, _)| *at == level)?;
            LinearCodebook {
                minimum: 0.0,
                maximum: step * 254.0,
                step: if compact { step * 2.0 } else { step },
                nodata_code: 255,
                name: "specific humidity",
            }
        }
        "ugrd" | "vgrd" => {
            if compact {
                COMPACT_ISOBARIC_WIND
            } else {
                QUALITY_ISOBARIC_WIND
            }
        }
        "uqflx" | "vqflx" => {
            if compact {
                COMPACT_VAPOUR_FLUX
            } else {
                QUALITY_VAPOUR_FLUX
            }
        }
        "vvel" => {
            if compact {
                COMPACT_VERTICAL_VELOCITY
            } else {
                QUALITY_VERTICAL_VELOCITY
            }
        }
        "thetae" => {
            let &(_, offset) = THETA_E_OFFSETS.iter().find(|(at, _)| *at == level)?;
            LinearCodebook {
                minimum: offset,
                maximum: offset + 127.0,
                step: if compact { 1.0 } else { 0.5 },
                nodata_code: 255,
                name: "equivalent potential temperature",
            }
        }
        _ => return None,
    })
}

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
        (_, "ugrd10m" | "vgrd10m" | "ugrd100m" | "vgrd100m") if quality => Codebook::Linear(QUALITY_WIND),
        (_, "ugrd10m" | "vgrd10m" | "ugrd100m" | "vgrd100m") => Codebook::Linear(COMPACT_WIND),
        (_, "dswrf") if quality => Codebook::Linear(QUALITY_FLUX),
        (_, "dswrf") => Codebook::Linear(COMPACT_FLUX),
        (_, "cref") if quality => Codebook::Linear(QUALITY_REFLECTIVITY),
        (_, "cref") => Codebook::Linear(COMPACT_REFLECTIVITY),
        (_, "gust") if quality => Codebook::Linear(QUALITY_GUST),
        (_, "gust") => Codebook::Linear(COMPACT_GUST),
        ("quality", "tcdc") => Codebook::Linear(QUALITY_CLOUD),
        (_, "tcdc") => Codebook::Linear(COMPACT_CLOUD),
        (_, "cape") if quality => Codebook::Linear(QUALITY_CAPE),
        (_, "cape") => Codebook::Linear(COMPACT_CAPE),
        (_, "cin") if quality => Codebook::Linear(QUALITY_CIN),
        (_, "cin") => Codebook::Linear(COMPACT_CIN),
        (_, "vis") if quality => Codebook::Linear(QUALITY_VISIBILITY),
        (_, "vis") => Codebook::Linear(COMPACT_VISIBILITY),
        (_, "dpt2m") if quality => Codebook::Linear(QUALITY_DEW_POINT),
        (_, "dpt2m") => Codebook::Linear(COMPACT_DEW_POINT),
        (_, "aptmp2m") if quality => Codebook::Linear(QUALITY_APPARENT),
        (_, "aptmp2m") => Codebook::Linear(COMPACT_APPARENT),
        (_, "pwat") if quality => Codebook::Linear(QUALITY_PWAT),
        (_, "pwat") => Codebook::Linear(COMPACT_PWAT),
        (_, "hpbl") if quality => Codebook::Linear(QUALITY_PBL),
        (_, "hpbl") => Codebook::Linear(COMPACT_PBL),
        (_, "orog") if quality => Codebook::Linear(QUALITY_OROGRAPHY),
        (_, "orog") => Codebook::Linear(COMPACT_OROGRAPHY),
        // The categorical precipitation type is the same book in every
        // profile: there is no resolution to trade.
        (_, "ptype") => Codebook::Linear(PTYPE),
        ("quality", "lcdc") => Codebook::Linear(cloud_layer_codebook("lcdc", false)),
        (_, "lcdc") => Codebook::Linear(cloud_layer_codebook("lcdc", true)),
        ("quality", "mcdc") => Codebook::Linear(cloud_layer_codebook("mcdc", false)),
        (_, "mcdc") => Codebook::Linear(cloud_layer_codebook("mcdc", true)),
        ("quality", "hcdc") => Codebook::Linear(cloud_layer_codebook("hcdc", false)),
        (_, "hcdc") => Codebook::Linear(cloud_layer_codebook("hcdc", true)),
        (_, "tmpsfc") if quality => Codebook::Linear(QUALITY_SURFACE_TEMPERATURE),
        (_, "tmpsfc") => Codebook::Linear(COMPACT_SURFACE_TEMPERATURE),
        // Ice concentration is read in tenths: balanced takes the 1 % step,
        // as for cloud cover.
        ("quality", "icec") => Codebook::Linear(QUALITY_ICE_COVER),
        (_, "icec") => Codebook::Linear(COMPACT_ICE_COVER),
        (_, "icetk") if quality => Codebook::Linear(QUALITY_ICE_THICKNESS),
        (_, "icetk") => Codebook::Linear(COMPACT_ICE_THICKNESS),
        (_, "htsgw") if quality => Codebook::Linear(QUALITY_WAVE_HEIGHT),
        (_, "htsgw") => Codebook::Linear(COMPACT_WAVE_HEIGHT),
        (_, "perpw") if quality => Codebook::Linear(QUALITY_WAVE_PERIOD),
        (_, "perpw") => Codebook::Linear(COMPACT_WAVE_PERIOD),
        (_, "dirpw") if quality => Codebook::Linear(QUALITY_WAVE_DIRECTION),
        (_, "dirpw") => Codebook::Linear(COMPACT_WAVE_DIRECTION),
        (_, "uwave" | "vwave") if quality => Codebook::Linear(QUALITY_WAVE_VECTOR),
        (_, "uwave" | "vwave") => Codebook::Linear(COMPACT_WAVE_VECTOR),
        (_, "ir039" | "wv062" | "ir086" | "ir104" | "ir112" | "ir123") => {
            let channel_id = SATELLITE_CHANNEL_IDS
                .iter()
                .copied()
                .find(|id| *id == variable_id)
                .expect("matched just above");
            Codebook::Linear(brightness_temperature(channel_id, !quality))
        }
        (_, "dustr" | "dustg" | "dustb") => Codebook::Linear(DUST_RGB_GUN),
        (_, "dustcf") => Codebook::Linear(DUST_CF),
        (_, "aod" | "aoddust" | "aodsalt" | "aodsulf" | "aodorg" | "aodbc" | "pm25" | "pm10" | "pm10dust") => {
            let variable_id = AEROSOL_VARIABLE_IDS
                .iter()
                .copied()
                .find(|id| *id == variable_id)
                .expect("matched just above");
            Codebook::Precipitation(aerosol_codebook(variable_id, !quality))
        }
        _ if pressure_codebook(variable_id, !quality).is_some() => Codebook::Linear(
            pressure_codebook(variable_id, !quality).expect("checked just above"),
        ),
        // Relative humidity is the noisiest field published; balanced takes
        // the 1 % step (see `xuebuild/quantize.py`).
        _ if isobaric_codebook(variable_id, !quality).is_some() => {
            let humidity = matches!(isobaric_variable(variable_id), Some(("rh", _)));
            Codebook::Linear(
                isobaric_codebook(variable_id, !quality || (humidity && profile == "balanced"))
                    .expect("checked just above"),
            )
        }
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
