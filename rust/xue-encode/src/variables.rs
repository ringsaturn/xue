//! The variable registry, in GRIB2's own terms — the port of
//! `xue/variables.py`.
//!
//! Every variable is identified the way GRIB2 identifies a field: a parameter
//! triple (discipline, category, number) and a fixed surface. That identity is
//! both what record matching uses and what a bundle's schema v3 metadata
//! carries, so there is one description of a variable rather than one per
//! pipeline stage.

use serde_json::{json, Map, Value};

use crate::errors::{EncodeError, Result};

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
