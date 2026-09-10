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
    /// The container's registered `variableId`, or `None` for an input-only
    /// variable that never reaches a bundle (ECMWF `tp`, sflux `prate_ave`).
    pub numeric_id: Option<u8>,
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

/// The standard isobaric surfaces geopotential height is registered on, in
/// hPa, in `variableId` order (8..15). Mirrors `HEIGHT_LEVELS_HPA` in
/// `xuebuild/variables.py`.
pub const HEIGHT_LEVELS_HPA: &[u32] = &[1000, 925, 850, 700, 500, 300, 250, 200];

/// The isobaric surface of a `hgt<level>` variable, in hPa; `None` for
/// anything else. This is what record matching needs beyond the shared HGT
/// element — every level is the same GRIB2 parameter.
pub fn isobaric_level_hpa(variable_id: &str) -> Option<u32> {
    let level: u32 = variable_id.strip_prefix("hgt")?.parse().ok()?;
    HEIGHT_LEVELS_HPA.contains(&level).then_some(level)
}

/// One isobaric geopotential height entry. Every level shares parameter
/// 0/3/5 on surface type 100 and differs only in the surface's pressure (Pa)
/// and the registered `variableId`, so the eight entries below are one table
/// of numbers rather than eight hand-written blocks.
macro_rules! height_spec {
    ($id:literal, $label:literal, $numeric:literal, $level_pa:literal, $range:expr) => {
        VariableSpec {
            id: $id,
            label: $label,
            output_unit: "m",
            value_range: $range,
            numeric_id: Some($numeric),
            grib_element: "HGT",
            grib2_discipline: 0,
            grib2_category: 3,
            grib2_number: 5,
            grib2_level_type: 100,
            grib2_level_value: Some($level_pa),
            grib2_statistical: None,
            gdal_unit: "gpm",
        }
    };
}

pub const VARIABLES: &[VariableSpec] = &[
    VariableSpec {
        id: "tmp2m",
        label: "2 meter temperature",
        output_unit: "°C",
        value_range: (-60, 50),
        numeric_id: Some(1),
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
        numeric_id: Some(2),
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
        numeric_id: None,
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
        numeric_id: None,
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
        numeric_id: Some(5),
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
        numeric_id: Some(3),
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
        numeric_id: Some(4),
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
        numeric_id: Some(6),
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
        numeric_id: Some(7),
        grib_element: "PRMSL",
        grib2_discipline: 0,
        grib2_category: 3,
        grib2_number: 1,
        grib2_level_type: 101,
        grib2_level_value: None,
        grib2_statistical: None,
        gdal_unit: "Pa",
    },
    // Geopotential height on the standard isobaric surfaces. The value range
    // is the level's codebook coverage (quantize.rs).
    height_spec!("hgt1000", "1000 hPa geopotential height", 8, 100000.0, (-905, 1635)),
    height_spec!("hgt925", "925 hPa geopotential height", 9, 92500.0, (-249, 1275)),
    height_spec!("hgt850", "850 hPa geopotential height", 10, 85000.0, (423, 1947)),
    height_spec!("hgt700", "700 hPa geopotential height", 11, 70000.0, (1911, 3435)),
    height_spec!("hgt500", "500 hPa geopotential height", 12, 50000.0, (4252, 6284)),
    height_spec!("hgt300", "300 hPa geopotential height", 13, 30000.0, (7505, 10045)),
    height_spec!("hgt250", "250 hPa geopotential height", 14, 25000.0, (8598, 11646)),
    height_spec!("hgt200", "200 hPa geopotential height", 15, 20000.0, (10086, 13134)),
];

pub fn variable_spec(variable_id: &str) -> Result<&'static VariableSpec> {
    VARIABLES
        .iter()
        .find(|spec| spec.id == variable_id)
        .ok_or_else(|| EncodeError::conversion(format!("unsupported variable: {variable_id}")))
}

/// The container's registered `variableId`, for a variable that reaches a
/// bundle.
pub fn numeric_id(variable_id: &str) -> Result<u8> {
    variable_spec(variable_id)?.numeric_id.ok_or_else(|| {
        EncodeError::conversion(format!("{variable_id} is input-only and has no variableId"))
    })
}

#[cfg(test)]
mod tests {
    use super::{isobaric_level_hpa, variable_spec, HEIGHT_LEVELS_HPA};
    use crate::encode::quantize::codebook;
    use serde_json::{json, Value};
    use std::path::PathBuf;

    /// `tests/fixtures/pressure-registry.json`, the committed golden the
    /// Python encoder (`tests/test_pressure.py`) and the frontend read too.
    /// Nine variables that change nothing about the container and everything
    /// about the registries; this fixture is what stops one implementation
    /// drifting from the others.
    fn pressure_registry() -> serde_json::Map<String, Value> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/pressure-registry.json");
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("missing {path:?}: {error}"));
        serde_json::from_str::<Value>(&text)
            .expect("valid JSON")
            .as_object()
            .expect("an object")
            .clone()
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
            assert_eq!(json!(spec.numeric_id), entry["numericId"], "{variable_id}");
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
        assert_eq!(isobaric_level_hpa("hgt500"), Some(500));
        assert_eq!(isobaric_level_hpa("hgt550"), None, "not a registered level");
        assert_eq!(isobaric_level_hpa("prmsl"), None);
        assert_eq!(isobaric_level_hpa("tmp2m"), None);
        for level in HEIGHT_LEVELS_HPA {
            let id = format!("hgt{level}");
            let spec = variable_spec(&id).expect("registered");
            assert_eq!(isobaric_level_hpa(&id), Some(*level));
            assert_eq!(spec.grib2_level_value, Some(f64::from(*level) * 100.0));
        }
    }
}
