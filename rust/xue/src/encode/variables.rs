//! The variable registry, in GRIB2's own terms — the port of
//! `xuebuild/variables.py`.
//!
//! Every variable is identified the way GRIB2 identifies a field: a parameter
//! triple (discipline, category, number) and a fixed surface. That identity is
//! both what record matching uses and what a bundle's schema v3 metadata
//! carries, so there is one description of a variable rather than one per
//! pipeline stage.

use serde_json::{json, Map, Value};

use crate::encode::errors::{EncodeError, Result};

#[derive(Debug, Clone, Copy)]
pub struct VariableSpec {
    pub id: &'static str,
    /// English label carried in bundle metadata.
    pub label: &'static str,
    /// Unit of the values a bundle's codebook quantizes.
    pub output_unit: &'static str,
    pub value_range: (i32, i32),
    pub grib_element: &'static str,
    pub grib2_discipline: u8,
    pub grib2_category: u8,
    pub grib2_number: u8,
    /// Code table 4.5 type of first fixed surface.
    pub grib2_level_type: u8,
    /// First fixed surface value in that surface's own unit; `None` when the
    /// surface carries none — GRIB2 encodes that as a missing scale factor and
    /// value, so matching on `None` also accepts any.
    pub grib2_level_value: Option<f64>,
    /// Code table 4.10 statistical process required of the record (0 average,
    /// 1 accumulation); `None` requires an instantaneous product.
    pub grib2_statistical: Option<u8>,
    /// Unit string GDAL's GRIB driver reports for this record (it normalizes
    /// temperatures to Celsius).
    pub gdal_unit: &'static str,
}

impl VariableSpec {
    /// The variable's GRIB2 identity, as a schema v3 metadata block.
    ///
    /// A fixed surface with no value is written as GRIB2 encodes it: a missing
    /// scale factor and scaled value, `null` in JSON.
    pub fn parameter_metadata(&self) -> Map<String, Value> {
        let mut block = Map::new();
        block.insert("discipline".into(), json!(self.grib2_discipline));
        block.insert("parameterCategory".into(), json!(self.grib2_category));
        block.insert("parameterNumber".into(), json!(self.grib2_number));
        block.insert(
            "typeOfFirstFixedSurface".into(),
            json!(self.grib2_level_type),
        );
        match self.grib2_level_value {
            Some(value) => {
                let (scale_factor, scaled_value) = scaled_surface_value(value);
                block.insert("scaleFactorOfFirstFixedSurface".into(), json!(scale_factor));
                block.insert("scaledValueOfFirstFixedSurface".into(), json!(scaled_value));
            }
            None => {
                block.insert("scaleFactorOfFirstFixedSurface".into(), Value::Null);
                block.insert("scaledValueOfFirstFixedSurface".into(), Value::Null);
            }
        }
        block
    }
}

/// `(scaleFactor, scaledValue)` with `value = scaledValue * 10^-scaleFactor`,
/// using the smallest scale factor that represents the value exactly.
fn scaled_surface_value(value: f64) -> (i32, i64) {
    for scale_factor in 0..7 {
        let scaled = value * 10f64.powi(scale_factor);
        if (scaled - scaled.round()).abs() < 1e-9 {
            return (scale_factor, scaled.round() as i64);
        }
    }
    panic!("fixed surface value is not representable: {value}");
}

/// The standard isobaric surfaces every isobaric family is registered on, in
/// hPa, in level order. Mirrors `ISOBARIC_LEVELS_HPA` in
/// `xuebuild/variables.py`.
pub const ISOBARIC_LEVELS_HPA: &[u32] = &[1000, 925, 850, 700, 500, 300, 250, 200];

/// The id prefixes of the isobaric families. Mirrors `ISOBARIC_FAMILIES` in
/// `xuebuild/variables.py`.
pub const ISOBARIC_FAMILIES: &[&str] = &[
    "hgt", "tmp", "rh", "spfh", "ugrd", "vgrd", "uqflx", "vqflx",
];

/// Standard gravity, the `g` in the water vapour flux `q·V/g`.
pub const STANDARD_GRAVITY: f64 = 9.80665;

/// `(family, level in hPa)` of an isobaric variable, or `None` for anything
/// else — including a level that is not registered (`hgt550`). This is what
/// record matching needs beyond the family's element: every level of a family
/// is the same GRIB2 parameter.
pub fn isobaric_variable(variable_id: &str) -> Option<(&'static str, u32)> {
    for family in ISOBARIC_FAMILIES {
        if let Some(rest) = variable_id.strip_prefix(family) {
            if !rest.is_empty() && rest.bytes().all(|byte| byte.is_ascii_digit()) {
                let level: u32 = rest.parse().ok()?;
                return ISOBARIC_LEVELS_HPA.contains(&level).then_some((*family, level));
            }
        }
    }
    None
}

/// One isobaric entry. Every level of a family shares its parameter on
/// surface type 100 and differs only in the surface's pressure (Pa), so the
/// entries below are tables of numbers rather than hand-written blocks.
macro_rules! isobaric_spec {
    ($id:literal, $label:literal, $level_pa:literal, $range:expr,
     $unit:literal, $element:literal, $category:literal, $number:literal, $gdal_unit:literal) => {
        VariableSpec {
            id: $id,
            label: $label,
            output_unit: $unit,
            value_range: $range,
            grib_element: $element,
            grib2_discipline: 0,
            grib2_category: $category,
            grib2_number: $number,
            grib2_level_type: 100,
            grib2_level_value: Some($level_pa),
            grib2_statistical: None,
            gdal_unit: $gdal_unit,
        }
    };
}

macro_rules! height_spec {
    ($id:literal, $label:literal, $level_pa:literal, $range:expr) => {
        isobaric_spec!($id, $label, $level_pa, $range, "m", "HGT", 3, 5, "gpm")
    };
}
// GDAL normalizes every GRIB temperature to Celsius, isobaric TMP included.
macro_rules! temperature_spec {
    ($id:literal, $label:literal, $level_pa:literal, $range:expr) => {
        isobaric_spec!($id, $label, $level_pa, $range, "°C", "TMP", 0, 0, "C")
    };
}
macro_rules! humidity_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (0, 100), "%", "RH", 1, 1, "%")
    };
}
// GRIB2 carries kg/kg; the codebook quantizes g/kg.
macro_rules! specific_humidity_spec {
    ($id:literal, $label:literal, $level_pa:literal, $range:expr) => {
        isobaric_spec!($id, $label, $level_pa, $range, "g/kg", "SPFH", 1, 0, "kg/kg")
    };
}
macro_rules! u_wind_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-127, 127), "m/s", "UGRD", 2, 2, "m/s")
    };
}
macro_rules! v_wind_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-127, 127), "m/s", "VGRD", 2, 3, "m/s")
    };
}
// Water vapour flux, q·V/g in g·cm⁻¹·hPa⁻¹·s⁻¹ — derived by the converter
// from the specific humidity and the wind on the same surface, never fetched,
// so the record-matching fields stay empty. GRIB2 has no standard parameter
// for a per-level horizontal vapour flux; 250 / 251 are local-use numbers of
// our own in the moisture category.
macro_rules! vapour_flux_spec {
    ($id:literal, $label:literal, $level_pa:literal, $number:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-64, 64), "g/(cm·hPa·s)", "", 1, $number, "")
    };
}

pub const VARIABLES: &[VariableSpec] = &[
    VariableSpec {
        id: "tmp2m",
        label: "2 meter temperature",
        output_unit: "°C",
        value_range: (-60, 50),
        grib_element: "TMP",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 0,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        gdal_unit: "C",
    },
    VariableSpec {
        id: "prate",
        label: "Precipitation rate",
        output_unit: "mm/h",
        value_range: (0, 50),
        grib_element: "PRATE",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 7,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        gdal_unit: "kg/(m^2 s)",
    },
    // ECMWF open data has no rate field: tp is the run-total accumulation
    // (metres, ECMWF-local GRIB2 parameter 0/1/193). Input-only — the
    // converter de-accumulates it into prate.
    VariableSpec {
        id: "tp",
        label: "Total precipitation",
        output_unit: "m",
        value_range: (0, 1),
        grib_element: "unknown",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 193,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: Some(1),
        gdal_unit: "-",
    },
    // GFS sflux PRATE is the mean rate over an averaging window that resets
    // every 6 hours. Input-only — the converter de-averages it into prate.
    VariableSpec {
        id: "prate_ave",
        label: "Window-averaged precipitation rate",
        output_unit: "kg/m^2s",
        value_range: (0, 1),
        grib_element: "PRATE",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 7,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: Some(0),
        gdal_unit: "kg/(m^2 s)",
    },
    VariableSpec {
        id: "dswrf",
        label: "Downward shortwave radiation flux",
        output_unit: "W/m²",
        value_range: (0, 1270),
        grib_element: "DSWRF",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 192,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        gdal_unit: "W/(m^2)",
    },
    VariableSpec {
        id: "ugrd10m",
        label: "10 meter U wind component",
        output_unit: "m/s",
        value_range: (-64, 64),
        grib_element: "UGRD",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 2,
        grib2_level_type: 103,
        grib2_level_value: Some(10.0),
        grib2_statistical: None,
        gdal_unit: "m/s",
    },
    VariableSpec {
        id: "vgrd10m",
        label: "10 meter V wind component",
        output_unit: "m/s",
        value_range: (-64, 64),
        grib_element: "VGRD",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 3,
        grib2_level_type: 103,
        grib2_level_value: Some(10.0),
        grib2_statistical: None,
        gdal_unit: "m/s",
    },
    // Radar composite reflectivity: the column maximum, so its fixed surface
    // is the entire atmosphere (type 10, which carries no value). The only
    // variable not fetched from GRIB.
    VariableSpec {
        id: "cref",
        label: "Composite radar reflectivity",
        output_unit: "dBZ",
        value_range: (0, 80),
        grib_element: "",
        grib2_discipline: 0,
        grib2_category: 16,
        grib2_number: 5,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        gdal_unit: "",
    },
    // Mean sea level pressure. NCEP publishes two reductions; PRMSL (0/3/1)
    // is the one ECMWF also calls `msl`, so both sources carry the same
    // field. MSLET (0/3/192, the NCEP-local Shuell reduction) is a different
    // quantity and is deliberately not registered. Surface 101 ("mean sea
    // level") carries no value.
    VariableSpec {
        id: "prmsl",
        label: "Mean sea level pressure",
        output_unit: "hPa",
        value_range: (870, 1125),
        grib_element: "PRMSL",
        grib2_discipline: 0,
        grib2_category: 3,
        grib2_number: 1,
        grib2_level_type: 101,
        grib2_level_value: None,
        grib2_statistical: None,
        gdal_unit: "Pa",
    },
    // The isobaric families, eight levels each. Value ranges are the level's
    // codebook coverage (quantize.rs), truncated to integers.
    height_spec!("hgt1000", "1000 hPa geopotential height", 100000.0, (-905, 1635)),
    height_spec!("hgt925", "925 hPa geopotential height", 92500.0, (-249, 1275)),
    height_spec!("hgt850", "850 hPa geopotential height", 85000.0, (423, 1947)),
    height_spec!("hgt700", "700 hPa geopotential height", 70000.0, (1911, 3435)),
    height_spec!("hgt500", "500 hPa geopotential height", 50000.0, (4252, 6284)),
    height_spec!("hgt300", "300 hPa geopotential height", 30000.0, (7505, 10045)),
    height_spec!("hgt250", "250 hPa geopotential height", 25000.0, (8598, 11646)),
    height_spec!("hgt200", "200 hPa geopotential height", 20000.0, (10086, 13134)),
    temperature_spec!("tmp1000", "1000 hPa temperature", 100000.0, (-60, 60)),
    temperature_spec!("tmp925", "925 hPa temperature", 92500.0, (-65, 50)),
    temperature_spec!("tmp850", "850 hPa temperature", 85000.0, (-70, 45)),
    temperature_spec!("tmp700", "700 hPa temperature", 70000.0, (-75, 35)),
    temperature_spec!("tmp500", "500 hPa temperature", 50000.0, (-85, 15)),
    temperature_spec!("tmp300", "300 hPa temperature", 30000.0, (-95, 0)),
    temperature_spec!("tmp250", "250 hPa temperature", 25000.0, (-100, -5)),
    temperature_spec!("tmp200", "200 hPa temperature", 20000.0, (-100, -10)),
    humidity_spec!("rh1000", "1000 hPa relative humidity", 100000.0),
    humidity_spec!("rh925", "925 hPa relative humidity", 92500.0),
    humidity_spec!("rh850", "850 hPa relative humidity", 85000.0),
    humidity_spec!("rh700", "700 hPa relative humidity", 70000.0),
    humidity_spec!("rh500", "500 hPa relative humidity", 50000.0),
    humidity_spec!("rh300", "300 hPa relative humidity", 30000.0),
    humidity_spec!("rh250", "250 hPa relative humidity", 25000.0),
    humidity_spec!("rh200", "200 hPa relative humidity", 20000.0),
    specific_humidity_spec!("spfh1000", "1000 hPa specific humidity", 100000.0, (0, 50)),
    specific_humidity_spec!("spfh925", "925 hPa specific humidity", 92500.0, (0, 50)),
    specific_humidity_spec!("spfh850", "850 hPa specific humidity", 85000.0, (0, 25)),
    specific_humidity_spec!("spfh700", "700 hPa specific humidity", 70000.0, (0, 25)),
    specific_humidity_spec!("spfh500", "500 hPa specific humidity", 50000.0, (0, 5)),
    specific_humidity_spec!("spfh300", "300 hPa specific humidity", 30000.0, (0, 2)),
    specific_humidity_spec!("spfh250", "250 hPa specific humidity", 25000.0, (0, 1)),
    specific_humidity_spec!("spfh200", "200 hPa specific humidity", 20000.0, (0, 1)),
    u_wind_spec!("ugrd1000", "1000 hPa U wind component", 100000.0),
    u_wind_spec!("ugrd925", "925 hPa U wind component", 92500.0),
    u_wind_spec!("ugrd850", "850 hPa U wind component", 85000.0),
    u_wind_spec!("ugrd700", "700 hPa U wind component", 70000.0),
    u_wind_spec!("ugrd500", "500 hPa U wind component", 50000.0),
    u_wind_spec!("ugrd300", "300 hPa U wind component", 30000.0),
    u_wind_spec!("ugrd250", "250 hPa U wind component", 25000.0),
    u_wind_spec!("ugrd200", "200 hPa U wind component", 20000.0),
    v_wind_spec!("vgrd1000", "1000 hPa V wind component", 100000.0),
    v_wind_spec!("vgrd925", "925 hPa V wind component", 92500.0),
    v_wind_spec!("vgrd850", "850 hPa V wind component", 85000.0),
    v_wind_spec!("vgrd700", "700 hPa V wind component", 70000.0),
    v_wind_spec!("vgrd500", "500 hPa V wind component", 50000.0),
    v_wind_spec!("vgrd300", "300 hPa V wind component", 30000.0),
    v_wind_spec!("vgrd250", "250 hPa V wind component", 25000.0),
    v_wind_spec!("vgrd200", "200 hPa V wind component", 20000.0),
    vapour_flux_spec!("uqflx1000", "1000 hPa U water vapour flux component", 100000.0, 250),
    vapour_flux_spec!("uqflx925", "925 hPa U water vapour flux component", 92500.0, 250),
    vapour_flux_spec!("uqflx850", "850 hPa U water vapour flux component", 85000.0, 250),
    vapour_flux_spec!("uqflx700", "700 hPa U water vapour flux component", 70000.0, 250),
    vapour_flux_spec!("uqflx500", "500 hPa U water vapour flux component", 50000.0, 250),
    vapour_flux_spec!("uqflx300", "300 hPa U water vapour flux component", 30000.0, 250),
    vapour_flux_spec!("uqflx250", "250 hPa U water vapour flux component", 25000.0, 250),
    vapour_flux_spec!("uqflx200", "200 hPa U water vapour flux component", 20000.0, 250),
    vapour_flux_spec!("vqflx1000", "1000 hPa V water vapour flux component", 100000.0, 251),
    vapour_flux_spec!("vqflx925", "925 hPa V water vapour flux component", 92500.0, 251),
    vapour_flux_spec!("vqflx850", "850 hPa V water vapour flux component", 85000.0, 251),
    vapour_flux_spec!("vqflx700", "700 hPa V water vapour flux component", 70000.0, 251),
    vapour_flux_spec!("vqflx500", "500 hPa V water vapour flux component", 50000.0, 251),
    vapour_flux_spec!("vqflx300", "300 hPa V water vapour flux component", 30000.0, 251),
    vapour_flux_spec!("vqflx250", "250 hPa V water vapour flux component", 25000.0, 251),
    vapour_flux_spec!("vqflx200", "200 hPa V water vapour flux component", 20000.0, 251),
];

pub fn variable_spec(variable_id: &str) -> Result<&'static VariableSpec> {
    VARIABLES
        .iter()
        .find(|spec| spec.id == variable_id)
        .ok_or_else(|| EncodeError::conversion(format!("unsupported variable: {variable_id}")))
}

#[cfg(test)]
mod tests {
    use super::{isobaric_variable, variable_spec, ISOBARIC_FAMILIES, ISOBARIC_LEVELS_HPA};
    use crate::encode::quantize::codebook;
    use serde_json::{json, Value};
    use std::path::PathBuf;

    fn registry(name: &str) -> serde_json::Map<String, Value> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("../../tests/fixtures/{name}"));
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("missing {path:?}: {error}"));
        serde_json::from_str::<Value>(&text)
            .expect("valid JSON")
            .as_object()
            .expect("an object")
            .clone()
    }

    /// `tests/fixtures/isobaric-registry.json`: the upper-air fills, held
    /// to the Python encoder and the frontend the same way the pressure
    /// family is.
    #[test]
    fn the_isobaric_registry_matches_the_shared_fixture() {
        let entries = registry("isobaric-registry.json");
        assert_eq!(entries.len(), 7 * ISOBARIC_LEVELS_HPA.len());
        for (variable_id, entry) in entries {
            let spec = variable_spec(&variable_id).unwrap_or_else(|_| panic!("{variable_id}"));
            assert_eq!(json!(spec.label), entry["label"], "{variable_id}");
            assert_eq!(json!(spec.output_unit), entry["unit"], "{variable_id}");
            assert_eq!(
                Value::Object(spec.parameter_metadata()),
                entry["parameter"],
                "{variable_id} GRIB2 identity"
            );
            // balanced is quality everywhere but relative humidity, which it
            // takes at the compact 1 % step.
            let balanced_key = if variable_id.starts_with("rh") { "compact" } else { "quality" };
            for (profile, key) in [("quality", "quality"), ("compact", "compact"), ("balanced", balanced_key)] {
                let book = codebook(profile, &variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(Value::Object(book.metadata()), entry[key], "{variable_id} {profile} codebook");
            }
        }
    }

    #[test]
    fn every_family_is_registered_at_every_level() {
        for family in ISOBARIC_FAMILIES {
            for level in ISOBARIC_LEVELS_HPA {
                let id = format!("{family}{level}");
                let spec = variable_spec(&id).unwrap_or_else(|_| panic!("{id}"));
                assert_eq!(spec.grib2_level_type, 100, "{id}");
                assert_eq!(spec.grib2_level_value, Some(f64::from(*level) * 100.0), "{id}");
                assert_eq!(isobaric_variable(&id), Some((*family, *level)));
            }
        }
        assert_eq!(isobaric_variable("tmp550"), None, "not a registered level");
        assert_eq!(isobaric_variable("tmp2m"), None);
        assert_eq!(isobaric_variable("prmsl"), None);
        assert_eq!(isobaric_variable("rh"), None);
    }

    /// `tests/fixtures/pressure-registry.json`, the committed golden the
    /// Python encoder (`tests/test_pressure.py`) and the frontend read too.
    /// Nine variables that change nothing about the container and everything
    /// about the registries; this fixture is what stops one implementation
    /// drifting from the others.
    fn pressure_registry() -> serde_json::Map<String, Value> {
        registry("pressure-registry.json")
    }

    #[test]
    fn the_pressure_codebooks_match_the_shared_registry() {
        for (variable_id, entry) in pressure_registry() {
            for (profile, key) in [
                ("quality", "quality"),
                ("compact", "compact"),
                // balanced is the production profile: quality everywhere here.
                ("balanced", "quality"),
            ] {
                let book = codebook(profile, &variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(
                    Value::Object(book.metadata()),
                    entry[key],
                    "{variable_id} {profile} codebook"
                );
            }
        }
    }

    #[test]
    fn the_pressure_identities_match_the_shared_registry() {
        for (variable_id, entry) in pressure_registry() {
            let spec = variable_spec(&variable_id).unwrap_or_else(|_| panic!("{variable_id}"));
            assert_eq!(json!(spec.label), entry["label"], "{variable_id}");
            assert_eq!(json!(spec.output_unit), entry["unit"], "{variable_id}");
            assert_eq!(
                Value::Object(spec.parameter_metadata()),
                entry["parameter"],
                "{variable_id} GRIB2 identity"
            );
        }
    }

    #[test]
    fn every_pressure_contour_lands_half_a_code_off() {
        // The one rule with no home in the container: a contour that
        // coincides with a code value turns each flat pair of cells into a
        // plateau a `fract`-based renderer lights up wholesale.
        for (variable_id, entry) in pressure_registry() {
            let book = codebook("quality", &variable_id).expect("codebook");
            let linear = book.as_linear().expect("linear");
            let mut contours: Vec<f64> = entry["emphasisContours"]
                .as_array()
                .map(|values| values.iter().map(|v| v.as_f64().expect("number")).collect())
                .unwrap_or_default();
            let mut intervals = vec![entry["contourInterval"].as_f64().expect("interval")];
            if let Some(emphasis) = entry.get("emphasisInterval") {
                intervals.push(emphasis.as_f64().expect("interval"));
            }
            for interval in intervals {
                let first = (linear.minimum / interval).ceil() as i64;
                let last = (linear.maximum / interval).floor() as i64;
                contours.extend((first..=last).map(|step| interval * step as f64));
            }
            assert!(contours.len() > 8, "{variable_id}");
            for contour in contours {
                let offset = (contour - linear.minimum) / linear.step;
                let fraction = offset - offset.floor();
                assert!(
                    (fraction - 0.5).abs() < 1e-9,
                    "{variable_id}: contour {contour} sits at code offset {offset}"
                );
            }
        }
    }

    #[test]
    fn an_isobaric_level_is_only_recognised_where_it_is_registered() {
        assert_eq!(isobaric_variable("hgt500"), Some(("hgt", 500)));
        assert_eq!(isobaric_variable("hgt550"), None, "not a registered level");
        assert_eq!(isobaric_variable("prmsl"), None);
        assert_eq!(isobaric_variable("tmp2m"), None);
        for level in ISOBARIC_LEVELS_HPA {
            let id = format!("hgt{level}");
            let spec = variable_spec(&id).expect("registered");
            assert_eq!(isobaric_variable(&id), Some(("hgt", *level)));
            assert_eq!(spec.grib2_level_value, Some(f64::from(*level) * 100.0));
        }
    }
}
