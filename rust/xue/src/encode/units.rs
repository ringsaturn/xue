//! The unit rules of `docs/registry.md` §`conversion`: for each rule name,
//! the unit spellings a record is accepted under, the raster expression the
//! two inspection paths compare, and the arithmetic that takes a plane to
//! the published unit. The port of `xuebuild/rules.py`, function for
//! function: `accepts`, `expression` and `convert`, with `scaled` deciding
//! whether a rule's arithmetic applies under a spelling — as opposed to the
//! identity its other spellings, or a series already in the published unit,
//! take.

use crate::encode::errors::{EncodeError, Result};
use crate::encode::registry::Conversion;
use crate::encode::variables::STANDARD_GRAVITY;

/// Spellings are compared after compaction: lower-cased, whitespace and
/// `*()[]` removed, `²` written `^2`. Mirrors `rules.compact_unit`.
pub fn compact_unit(unit: &str) -> String {
    unit.trim()
        .to_lowercase()
        .replace('²', "^2")
        .chars()
        .filter(|character| !character.is_whitespace() && !"*()[]".contains(*character))
        .collect()
}

/// A rule's own compaction on top of [`compact_unit`]: a temperature drops
/// the degree sign and the word, the geopotential drops `^` too, a code
/// table drops `codetable`. Mirrors `rules._compact_for`.
fn compact_for(rule: &Conversion, unit: &str) -> String {
    let compact = compact_unit(unit);
    match rule {
        Conversion::Celsius | Conversion::SoilTemperature => {
            compact.replace('°', "").replace("degrees", "").replace("degree", "")
        }
        Conversion::GeopotentialToMetres => compact.replace('^', ""),
        Conversion::CodeTable { .. } => compact.replace("codetable", ""),
        _ => compact,
    }
}

/// The spellings of a temperature, by the letter the converter reads it
/// through.
const TEMPERATURE_SPELLINGS: [(&str, &str); 8] = [
    ("k", "K"),
    ("kelvin", "K"),
    ("c", "C"),
    ("celsius", "C"),
    ("degc", "C"),
    ("f", "F"),
    ("fahrenheit", "F"),
    ("degf", "F"),
];

/// One of `K`, `C`, `F` from whichever spelling a temperature record
/// reports. Mirrors `rules.temperature_unit`.
pub fn temperature_unit(unit: &str) -> Result<&'static str> {
    let compact = compact_for(&Conversion::Celsius, unit);
    TEMPERATURE_SPELLINGS
        .iter()
        .find(|(spelling, _)| *spelling == compact)
        .map(|(_, letter)| *letter)
        .ok_or_else(|| unsupported("temperature", unit))
}

fn unsupported(what: &str, unit: &str) -> EncodeError {
    EncodeError::conversion(format!(
        "unsupported {what} unit: {}",
        if unit.is_empty() { "<missing>" } else { unit }
    ))
}

/// Above this a temperature the GDAL GRIB driver labels Celsius is kelvin
/// (`soil_temperature_unit`). Mirrors `SOIL_KELVIN_THRESHOLD` in
/// `xuebuild/binconvert.py`.
pub const SOIL_KELVIN_THRESHOLD: f64 = 150.0;

/// The unit a soil temperature plane is really in. GDAL's GRIB driver
/// relabels every kelvin parameter Celsius but converts only those its
/// tables mark for conversion, and the soil temperatures (NCEP's 2/0/2, the
/// WMO 2/3/18 ECMWF writes) are not marked: their values come back in kelvin
/// under a `C` label. A plane labelled Celsius that holds a value above 150
/// is therefore kelvin. Mirrors `soil_temperature_unit` in
/// `xuebuild/binconvert.py`.
fn soil_temperature_unit(unit: &'static str, values: &[f64]) -> &'static str {
    if unit == "C" && values.iter().any(|value| *value > SOIL_KELVIN_THRESHOLD) {
        "K"
    } else {
        unit
    }
}

// How GDAL spells a rate already in millimetres per hour: the MRMS
// radar-derived precipitation rate arrives that way, and takes no scaling.
const MM_PER_HOUR: [&str; 2] = ["mm/h", "mm/hr"];
const KG_PER_M2_S: [&str; 4] = ["kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1"];
// A precipitation accumulation in metres: ECMWF open data's tp, whose
// ECMWF-local parameter GDAL reports under "-".
const METRES_ACCUMULATION: [&str; 3] = ["-", "m", ""];
// One already in millimetres (kg/m²): AIFS writes its run total that way,
// under the WMO 0/1/52 whose GDAL table entry is a rate, so the unit GDAL
// reports carries a per-second the values do not have.
const MM_ACCUMULATION: [&str; 9] = [
    "kg/m^2", "kg/m2", "kgm^-2", "kgm-2", "mm", "kg/m^2s", "kg/m^2*s", "kg/m2s", "kg/m2*s",
];
const PASCALS: [&str; 1] = ["pa"];
const METRES: [&str; 5] = ["m", "metre", "meter", "metres", "meters"];
const MASS_RATIO: [&str; 1] = ["kg/kg"];
const PROPORTION: [&str; 5] = ["proportion", "fraction", "1", "-", ""];
const CLOUD_FRACTION: [&str; 5] = ["-", "1", "fraction", "proportion", "0-1"];
const DEGREES_TRUE: [&str; 6] = ["degreetrue", "degtrue", "degrees", "degree", "deg", "degreestrue"];
// The geopotential height NCEP writes (gpm, numerically metres) and the
// geopotential itself (m² s⁻²) ECMWF open data writes, in whichever
// punctuation the driver reports it ("(m^2)/(s^2)" from the GRIB driver,
// "m**2 s**-2" from the header index): the rule's compaction drops `^` too,
// so both come out one way.
const GEOPOTENTIAL_METRES: [&str; 2] = ["gpm", "m"];
const GEOPOTENTIAL: [&str; 2] = ["m2/s2", "m2s-2"];

/// What a refusal calls the quantity. Mirrors `rules._NOUNS`.
fn noun(rule: &Conversion) -> Option<&'static str> {
    Some(match rule {
        Conversion::Identity { .. } => return None,
        Conversion::Celsius => "temperature",
        Conversion::SoilTemperature => "soil temperature",
        Conversion::PrecipitationRate => "precipitation rate",
        Conversion::PrecipitationAccumulation => "precipitation accumulation",
        Conversion::PascalToHectopascal => "pressure",
        Conversion::MetreToKilometre => "distance",
        Conversion::MassRatioToGPerKg => "specific humidity",
        Conversion::ProportionToPercent => "proportion",
        Conversion::CloudCover => "cloud cover",
        Conversion::DegreesTrue => "direction",
        Conversion::GeopotentialToMetres => "geopotential height",
        Conversion::CodeTable { .. } => "code table",
    })
}

impl Conversion {
    /// The compacted spellings the rule accepts a record under, or `None`
    /// for a `none` rule that accepts any. A `code-table` rule also accepts
    /// any spelling that contains its table or `0=no`. Mirrors
    /// `rules.accepted_units`.
    pub fn accepted_units(&self) -> Option<Vec<&str>> {
        Some(match self {
            Self::Identity { accepted_units: None } => return None,
            Self::Identity {
                accepted_units: Some(units),
            } => units.iter().map(String::as_str).collect(),
            Self::CodeTable { table } => vec!["1", "-", "", table.as_str()],
            Self::Celsius | Self::SoilTemperature => {
                TEMPERATURE_SPELLINGS.iter().map(|(spelling, _)| *spelling).collect()
            }
            Self::PrecipitationRate => KG_PER_M2_S.iter().chain(&MM_PER_HOUR).copied().collect(),
            Self::PrecipitationAccumulation => {
                METRES_ACCUMULATION.iter().chain(&MM_ACCUMULATION).copied().collect()
            }
            Self::PascalToHectopascal => PASCALS.to_vec(),
            Self::MetreToKilometre => METRES.to_vec(),
            Self::MassRatioToGPerKg => MASS_RATIO.to_vec(),
            Self::ProportionToPercent => PROPORTION.to_vec(),
            Self::CloudCover => std::iter::once("%").chain(CLOUD_FRACTION).collect(),
            Self::DegreesTrue => DEGREES_TRUE.to_vec(),
            Self::GeopotentialToMetres => GEOPOTENTIAL_METRES.iter().chain(&GEOPOTENTIAL).copied().collect(),
        })
    }

    /// Whether a record under `unit` is one this rule reads. Mirrors
    /// `rules.accepts`.
    pub fn accepts(&self, unit: &str) -> bool {
        let Some(accepted) = self.accepted_units() else {
            return true;
        };
        let compact = compact_for(self, unit);
        if accepted.contains(&compact.as_str()) {
            return true;
        }
        match self {
            Self::CodeTable { table } => compact.contains(table.as_str()) || compact.contains("0=no"),
            _ => false,
        }
    }

    fn refuse(&self, unit: &str) -> EncodeError {
        match noun(self) {
            Some(noun) => unsupported(noun, unit),
            None => EncodeError::conversion(format!(
                "unsupported unit: {} (expected one of {})",
                if unit.is_empty() { "<missing>" } else { unit },
                self.accepted_units().unwrap_or_default().join(", ")
            )),
        }
    }

    /// Whether the rule's arithmetic applies under `unit` — as opposed to
    /// the identity the rule's other spellings (or, on a series, the
    /// published unit itself) take. Mirrors `rules._scaled`.
    fn scaled(&self, unit: &str) -> bool {
        let compact = compact_for(self, unit);
        let within = |list: &[&str]| list.contains(&compact.as_str());
        match self {
            Self::PrecipitationRate => !within(&MM_PER_HOUR),
            Self::PrecipitationAccumulation => within(&METRES_ACCUMULATION),
            Self::PascalToHectopascal => within(&PASCALS),
            Self::MetreToKilometre => within(&METRES),
            Self::MassRatioToGPerKg => within(&MASS_RATIO),
            Self::ProportionToPercent => within(&PROPORTION),
            Self::CloudCover => within(&CLOUD_FRACTION),
            Self::GeopotentialToMetres => within(&GEOPOTENTIAL),
            _ => false,
        }
    }

    /// The raster expression one record takes under `unit`: the rule's
    /// arithmetic alone, no clamp, in GDAL's `A` spelling. The two
    /// inspection paths (`gdal.py`, `inspect.rs`) compare it for identity;
    /// nothing writes it. An error for a spelling the rule does not accept.
    /// Mirrors `rules.expression`.
    pub fn expression(&self, unit: &str) -> Result<String> {
        if !self.accepts(unit) {
            return Err(self.refuse(unit));
        }
        if matches!(self, Self::Celsius | Self::SoilTemperature) {
            return Ok(match temperature_unit(unit)? {
                "K" => "A-273.15",
                "F" => "(A-32)*5/9",
                _ => "A",
            }
            .to_string());
        }
        if !self.scaled(unit) {
            return Ok("A".to_string());
        }
        Ok(match self {
            Self::PrecipitationRate => "A*3600".to_string(),
            Self::PrecipitationAccumulation | Self::MassRatioToGPerKg => "A*1000".to_string(),
            Self::PascalToHectopascal => "A/100".to_string(),
            Self::MetreToKilometre => "A/1000".to_string(),
            Self::ProportionToPercent | Self::CloudCover => "A*100".to_string(),
            Self::GeopotentialToMetres => format!("A/{STANDARD_GRAVITY}"),
            _ => unreachable!("a rule that scales is one of the above"),
        })
    }

    /// One plane's values taken to the published unit, by the rule's
    /// arithmetic under `unit`, in place: one operation per value, in f64,
    /// the operation `rules.convert` runs. A temperature's unit is read
    /// through `temperature_unit`, after the soil read-through for a soil
    /// temperature; a direction is reduced modulo 360 so a record's 360
    /// lands on the codebook's 0 (every value is non-negative there, so
    /// this is numpy's `mod`). A spelling the rule does not scale under
    /// takes the identity, which is how a series already in the published
    /// unit is left alone.
    pub fn convert(&self, unit: &str, values: &mut [f64]) -> Result<()> {
        match self {
            Self::Celsius | Self::SoilTemperature => {
                let mut normalized = temperature_unit(unit)?;
                if matches!(self, Self::SoilTemperature) {
                    normalized = soil_temperature_unit(normalized, values);
                }
                match normalized {
                    "K" => values.iter_mut().for_each(|value| *value -= 273.15),
                    "F" => values
                        .iter_mut()
                        .for_each(|value| *value = (*value - 32.0) * 5.0 / 9.0),
                    _ => {}
                }
                return Ok(());
            }
            Self::DegreesTrue => {
                values
                    .iter_mut()
                    .for_each(|value| *value = value.rem_euclid(360.0));
                return Ok(());
            }
            _ => {}
        }
        if !self.scaled(unit) {
            return Ok(());
        }
        match self {
            // kg m⁻² s⁻¹ (a millimetre per second) to mm/h.
            Self::PrecipitationRate => values.iter_mut().for_each(|value| *value *= 3600.0),
            // Metres of accumulated water to millimetres; the de-accumulation
            // into a rate is the converter's, against the previous frame.
            Self::PrecipitationAccumulation | Self::MassRatioToGPerKg => {
                values.iter_mut().for_each(|value| *value *= 1000.0)
            }
            Self::PascalToHectopascal => values.iter_mut().for_each(|value| *value /= 100.0),
            Self::MetreToKilometre => values.iter_mut().for_each(|value| *value /= 1000.0),
            Self::ProportionToPercent | Self::CloudCover => {
                values.iter_mut().for_each(|value| *value *= 100.0)
            }
            Self::GeopotentialToMetres => values.iter_mut().for_each(|value| *value /= STANDARD_GRAVITY),
            _ => unreachable!("a rule that scales is one of the above"),
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{compact_unit, temperature_unit, Conversion};

    fn expression(rule: &Conversion, unit: &str) -> String {
        rule.expression(unit).unwrap_or_else(|error| panic!("{unit:?}: {error}"))
    }

    fn refused(rule: &Conversion, unit: &str) {
        assert!(!rule.accepts(unit), "{rule:?} accepted {unit:?}");
        assert!(rule.expression(unit).is_err());
    }

    fn converted(rule: &Conversion, unit: &str, values: &[f64]) -> Vec<f64> {
        let mut values = values.to_vec();
        rule.convert(unit, &mut values).unwrap_or_else(|error| panic!("{unit:?}: {error}"));
        values
    }

    #[test]
    fn spellings_are_compared_after_compaction() {
        assert_eq!(compact_unit(" Kg/(m^2 s) "), "kg/m^2s");
        assert_eq!(compact_unit("[W/m²]"), "w/m^2");
        assert_eq!(compact_unit("m s-1"), "ms-1");
        assert_eq!(temperature_unit("[C]").unwrap(), "C");
        assert_eq!(temperature_unit("degrees Kelvin").unwrap(), "K");
        assert_eq!(temperature_unit("°F").unwrap(), "F");
        assert_eq!(temperature_unit("deg C").unwrap(), "C");
        assert!(temperature_unit("hPa").is_err());
    }

    #[test]
    fn every_rule_accepts_its_spellings_and_refuses_others() {
        let any = Conversion::Identity { accepted_units: None };
        assert_eq!(expression(&any, "whatever"), "A");
        assert_eq!(any.accepted_units(), None);
        let wind = Conversion::Identity {
            accepted_units: Some(["m/s", "m/sec", "ms-1", "ms^-1", "mps"].map(String::from).to_vec()),
        };
        assert_eq!(expression(&wind, "[m/s]"), "A");
        assert_eq!(expression(&wind, "m s-1"), "A");
        refused(&wind, "knots");
        assert!(wind.expression("knots").unwrap_err().to_string().contains("m/sec"));

        assert_eq!(expression(&Conversion::Celsius, "[C]"), "A");
        assert_eq!(expression(&Conversion::Celsius, "K"), "A-273.15");
        assert_eq!(expression(&Conversion::Celsius, "degF"), "(A-32)*5/9");
        refused(&Conversion::Celsius, "Pa");
        assert_eq!(Conversion::Celsius.accepted_units().unwrap().len(), 8);
        // The soil read-through: a Celsius label over kelvins is kelvins.
        let soil = Conversion::SoilTemperature;
        assert_eq!(converted(&soil, "C", &[280.0, 290.0]), [6.850000000000023, 16.850000000000023]);
        assert_eq!(converted(&soil, "C", &[10.0, 20.0]), [10.0, 20.0]);
        assert_eq!(expression(&soil, "C"), "A");

        let rate = Conversion::PrecipitationRate;
        assert_eq!(expression(&rate, "kg/(m^2 s)"), "A*3600");
        assert_eq!(expression(&rate, "mm/hr"), "A");
        refused(&rate, "in/h");
        assert_eq!(converted(&rate, "kg/m^2 s", &[1.0]), [3600.0]);
        assert_eq!(converted(&rate, "mm/h", &[1.0]), [1.0]);

        let accumulation = Conversion::PrecipitationAccumulation;
        assert_eq!(expression(&accumulation, "-"), "A*1000");
        assert_eq!(expression(&accumulation, "m"), "A*1000");
        assert_eq!(expression(&accumulation, ""), "A*1000");
        assert_eq!(expression(&accumulation, "kg/(m^2*s)"), "A");
        assert_eq!(expression(&accumulation, "mm"), "A");
        refused(&accumulation, "in");

        assert_eq!(expression(&Conversion::PascalToHectopascal, "[Pa]"), "A/100");
        refused(&Conversion::PascalToHectopascal, "hPa");
        // A series already in the published unit is left as it is.
        assert_eq!(converted(&Conversion::PascalToHectopascal, "hPa", &[1013.0]), [1013.0]);
        assert_eq!(converted(&Conversion::PascalToHectopascal, "Pa", &[101300.0]), [1013.0]);
        assert_eq!(expression(&Conversion::MetreToKilometre, "metres"), "A/1000");
        refused(&Conversion::MetreToKilometre, "km");
        assert_eq!(converted(&Conversion::MetreToKilometre, "km", &[2.0]), [2.0]);
        assert_eq!(expression(&Conversion::MassRatioToGPerKg, "kg/kg"), "A*1000");
        refused(&Conversion::MassRatioToGPerKg, "g/kg");
        assert_eq!(converted(&Conversion::MassRatioToGPerKg, "g/kg", &[5.0]), [5.0]);
        assert_eq!(expression(&Conversion::ProportionToPercent, "Proportion"), "A*100");
        refused(&Conversion::ProportionToPercent, "%");
        assert_eq!(converted(&Conversion::ProportionToPercent, "%", &[50.0]), [50.0]);
        assert_eq!(expression(&Conversion::CloudCover, "%"), "A");
        assert_eq!(expression(&Conversion::CloudCover, "-"), "A*100");
        assert_eq!(expression(&Conversion::CloudCover, "(0 - 1)"), "A*100");
        refused(&Conversion::CloudCover, "okta");
        assert_eq!(expression(&Conversion::DegreesTrue, "Degree true"), "A");
        refused(&Conversion::DegreesTrue, "rad");
        assert_eq!(converted(&Conversion::DegreesTrue, "Degree true", &[360.0, 370.0, 90.0]), [0.0, 10.0, 90.0]);
        let orography = Conversion::GeopotentialToMetres;
        assert_eq!(expression(&orography, "gpm"), "A");
        assert_eq!(expression(&orography, "(m^2)/(s^2)"), "A/9.80665");
        assert_eq!(expression(&orography, "m**2 s**-2"), "A/9.80665");
        refused(&orography, "ft");
        assert_eq!(converted(&orography, "m**2 s**-2", &[9.80665]), [1.0]);
        let flags = Conversion::CodeTable { table: "4.222".into() };
        assert_eq!(expression(&flags, "0=no; 1=yes"), "A");
        assert_eq!(expression(&flags, "(Code table 4.222)"), "A");
        assert_eq!(expression(&flags, ""), "A");
        refused(&flags, "4.201");
        assert_eq!(flags.accepted_units().unwrap(), ["1", "-", "", "4.222"]);
    }

    #[test]
    fn the_arithmetic_is_one_operation_per_value() {
        assert_eq!(converted(&Conversion::Celsius, "K", &[273.15, 283.15]), [0.0, 10.0]);
        assert_eq!(converted(&Conversion::Celsius, "F", &[32.0, 212.0]), [0.0, 100.0]);
        assert_eq!(converted(&Conversion::PrecipitationAccumulation, "m", &[0.001]), [1.0]);
        assert_eq!(converted(&Conversion::CloudCover, "-", &[0.5]), [50.0]);
        assert_eq!(converted(&Conversion::CloudCover, "%", &[50.0]), [50.0]);
        assert!(Conversion::Celsius.convert("Pa", &mut [1.0]).is_err());
    }
}
