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
    /// Other `(discipline, category, number)` triples that carry this same
    /// quantity on the same surface, accepted when matching a record and
    /// never written: the primary triple is the identity a bundle declares.
    /// ECMWF encodes `msl` as plain pressure (0/3/0) on the mean sea level
    /// surface where NCEP writes PRMSL (0/3/1).
    pub grib2_aliases: &'static [(u8, u8, u8)],
    /// Unit string GDAL's GRIB driver reports for this record (it normalizes
    /// temperatures to Celsius).
    pub gdal_unit: &'static str,
    /// Values GDAL hands back where the record carries no data: a GRIB2
    /// record with a bitmap (the GFS-Wave fields, which cover water only)
    /// comes out of the GRIB driver with its masked points set to 9999. The
    /// format carries no bitmap, so the converter maps them to the bottom of
    /// the variable's codebook (`value_range.0`) before anything else touches
    /// the plane. Empty for a field that covers its whole grid.
    pub fill_values: &'static [f64],
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
    "hgt", "tmp", "rh", "spfh", "ugrd", "vgrd", "uqflx", "vqflx", "vvel", "thetae",
];

/// Standard gravity, the `g` in the water vapour flux `q·V/g`.
pub const STANDARD_GRAVITY: f64 = 9.80665;

/// The ocean set — three pgrb2 fields and three GFS-Wave fields — held to
/// the Python encoder by `tests/fixtures/ocean-registry.json`. Mirrors
/// `OCEAN_VARIABLE_IDS` in `xuebuild/variables.py`.
#[allow(dead_code)] // read by the registry test; the Python side keys its fixture on it
pub const OCEAN_VARIABLE_IDS: &[&str] = &["tmpsfc", "icec", "icetk", "htsgw", "perpw", "dirpw"];
/// The two components of the derived wave vector bundle, in the same
/// fixture. Mirrors `WAVE_VECTOR_COMPONENT_IDS` in `xuebuild/variables.py`.
pub const WAVE_VECTOR_COMPONENT_IDS: [&str; 2] = ["uwave", "vwave"];

/// The ids the Celsius rule applies to at the surface: GDAL normalizes every
/// GRIB temperature to Celsius, and the converter accepts K and F as well.
/// Mirrors `SURFACE_TEMPERATURE_IDS` in `xuebuild/variables.py`.
pub const SURFACE_TEMPERATURE_IDS: &[&str] = &["tmp2m", "dpt2m", "aptmp2m", "tmpsfc"];

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
            grib2_aliases: &[],
            gdal_unit: $gdal_unit,
            fill_values: &[],
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
// Vertical velocity in pressure coordinates, ω = dp/dt: negative is ascent.
macro_rules! vertical_velocity_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-6, 6), "Pa/s", "VVEL", 2, 8, "Pa/s")
    };
}
// Equivalent potential temperature, Bolton (1980), derived by the converter
// from the temperature and the specific humidity on the same surface, never
// fetched. GRIB2's own number for the quantity is 0/0/3 (EPOT).
macro_rules! theta_e_spec {
    ($id:literal, $label:literal, $level_pa:literal, $range:expr) => {
        isobaric_spec!($id, $label, $level_pa, $range, "K", "", 0, 3, "")
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
        grib2_aliases: &[],
        gdal_unit: "C",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "kg/(m^2 s)",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "-",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "kg/(m^2 s)",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "W/(m^2)",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "m/s",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "m/s",
        fill_values: &[],
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
        grib2_aliases: &[],
        gdal_unit: "",
        fill_values: &[],
    },
    // Three more surface diagnostics, each a GRIB record of its own with no
    // unit conversion. Registered from the GFS pgrb2 set; ECMWF open data
    // carries neighbours rather than equivalents (`10fg` is the interval
    // *maximum* gust on the 10 m surface, `tcc` a 0–1 fraction). No source
    // ships them yet (sources.rs).
    //
    // Wind gust: the instantaneous surface gust diagnostic, 0/2/22 on the
    // ground surface.
    VariableSpec {
        id: "gust",
        label: "Wind gust",
        output_unit: "m/s",
        value_range: (0, 127),
        grib_element: "GUST",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 22,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "m/s",
        fill_values: &[],
    },
    // Total cloud cover over the whole column, 0/6/1 on the entire
    // atmosphere (surface type 10, no value); the identity's missing
    // statistical process is what rejects pgrb2's interval average of the
    // same field.
    VariableSpec {
        id: "tcdc",
        label: "Total cloud cover",
        output_unit: "%",
        value_range: (0, 100),
        grib_element: "TCDC",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 1,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "%",
        fill_values: &[],
    },
    // Surface-based convective available potential energy, 0/7/6 on the
    // ground surface — not the mixed-layer variants on surface type 108.
    VariableSpec {
        id: "cape",
        label: "Convective available potential energy",
        output_unit: "J/kg",
        value_range: (0, 6350),
        grib_element: "CAPE",
        grib2_discipline: 0,
        grib2_category: 7,
        grib2_number: 6,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "J/kg",
        fill_values: &[],
    },
    // Surface visibility, 0/19/0 on the ground surface: GRIB2 carries metres,
    // the codebook quantizes kilometres.
    VariableSpec {
        id: "vis",
        label: "Visibility",
        output_unit: "km",
        value_range: (0, 25),
        grib_element: "VIS",
        grib2_discipline: 0,
        grib2_category: 19,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "m",
        fill_values: &[],
    },
    // 2 m dew point, 0/0/6 on the 2 m surface — the 2 m temperature's own
    // matching and unit rules.
    VariableSpec {
        id: "dpt2m",
        label: "2 meter dew point temperature",
        output_unit: "°C",
        value_range: (-70, 40),
        grib_element: "DPT",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 6,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "C",
        fill_values: &[],
    },
    // NCEP's apparent temperature, 0/0/21 on the 2 m surface.
    VariableSpec {
        id: "aptmp2m",
        label: "2 meter apparent temperature",
        output_unit: "°C",
        value_range: (-90, 60),
        grib_element: "APTMP",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 21,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "C",
        fill_values: &[],
    },
    // The cloud layers: three parameters of their own (0/6/3, 0/6/4, 0/6/5),
    // each on its own layer surface (214 low, 224 middle, 234 high), which
    // carries no value.
    VariableSpec {
        id: "lcdc",
        label: "Low cloud cover",
        output_unit: "%",
        value_range: (0, 100),
        grib_element: "LCDC",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 3,
        grib2_level_type: 214,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "%",
        fill_values: &[],
    },
    VariableSpec {
        id: "mcdc",
        label: "Middle cloud cover",
        output_unit: "%",
        value_range: (0, 100),
        grib_element: "MCDC",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 4,
        grib2_level_type: 224,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "%",
        fill_values: &[],
    },
    VariableSpec {
        id: "hcdc",
        label: "High cloud cover",
        output_unit: "%",
        value_range: (0, 100),
        grib_element: "HCDC",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 5,
        grib2_level_type: 234,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "%",
        fill_values: &[],
    },
    // The ocean fields of the pgrb2 set (see `xuebuild/variables.py`): the
    // ground-or-water skin temperature, 0/0/0 on surface type 1 — the SST
    // over the sea — and the two sea ice fields of GRIB2's oceanographic
    // discipline (10): cover (10/2/0), a 0–1 proportion the codebook takes
    // in percent, and thickness (10/2/1) in metres.
    VariableSpec {
        id: "tmpsfc",
        label: "Surface temperature",
        output_unit: "°C",
        value_range: (-60, 67),
        grib_element: "TMP",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "C",
        fill_values: &[],
    },
    VariableSpec {
        id: "icec",
        label: "Sea ice cover",
        output_unit: "%",
        value_range: (0, 100),
        grib_element: "ICEC",
        grib2_discipline: 10,
        grib2_category: 2,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "Proportion",
        fill_values: &[],
    },
    VariableSpec {
        id: "icetk",
        label: "Sea ice thickness",
        output_unit: "m",
        value_range: (0, 5),
        grib_element: "ICETK",
        grib2_discipline: 10,
        grib2_category: 2,
        grib2_number: 1,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "m",
        fill_values: &[],
    },
    // The GFS-Wave fields, appended to the frame from the cycle's second
    // file family: significant wave height (10/0/3), primary wave mean
    // period (10/0/11) and direction (10/0/10, degrees true, from). All on
    // the water surface, which WAVEWATCH III writes with a value of 1 where
    // pgrb2 writes 0 — so no value is declared and either is accepted. The
    // records carry a bitmap: land arrives as GDAL's nodata value, 9999.
    VariableSpec {
        id: "htsgw",
        label: "Significant wave height",
        output_unit: "m",
        value_range: (0, 25),
        grib_element: "HTSGW",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 3,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "m",
        fill_values: &[9999.0],
    },
    VariableSpec {
        id: "perpw",
        label: "Primary wave mean period",
        output_unit: "s",
        value_range: (0, 25),
        grib_element: "PERPW",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 11,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "s",
        fill_values: &[9999.0],
    },
    VariableSpec {
        id: "dirpw",
        label: "Primary wave direction",
        output_unit: "°",
        value_range: (0, 358),
        grib_element: "DIRPW",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 10,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "Degree true",
        fill_values: &[9999.0],
    },
    // The wave vector: the significant wave height laid along the direction
    // the waves travel, as an eastward and a northward component in metres,
    // derived by the converter from `htsgw` and `dirpw`
    // (`convert::derive_wave_vector`) and never fetched, so the
    // record-matching fields stay empty. GRIB2 has no parameter for such a
    // pair; 250 / 251 are local-use numbers of our own in the waves
    // category, as the vapour flux components are in the moisture category.
    // Same water surface as the inputs, no value declared.
    VariableSpec {
        id: "uwave",
        label: "U wave vector component",
        output_unit: "m",
        value_range: (-25, 25),
        grib_element: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 250,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "",
        fill_values: &[],
    },
    VariableSpec {
        id: "vwave",
        label: "V wave vector component",
        output_unit: "m",
        value_range: (-25, 25),
        grib_element: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 251,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        gdal_unit: "",
        fill_values: &[],
    },
    // Mean sea level pressure. NCEP publishes two reductions; PRMSL (0/3/1)
    // is the same quantity ECMWF calls `msl` — encoded there as plain
    // pressure (0/3/0) on the mean sea level surface, hence the alias — so
    // both sources carry the same field under one identity. MSLET (0/3/192,
    // the NCEP-local Shuell reduction) is a different quantity and is
    // deliberately not registered. Surface 101 ("mean sea level") carries no
    // value.
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
        grib2_aliases: &[(0, 3, 0)],
        gdal_unit: "Pa",
        fill_values: &[],
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
    vertical_velocity_spec!("vvel1000", "1000 hPa vertical velocity", 100000.0),
    vertical_velocity_spec!("vvel925", "925 hPa vertical velocity", 92500.0),
    vertical_velocity_spec!("vvel850", "850 hPa vertical velocity", 85000.0),
    vertical_velocity_spec!("vvel700", "700 hPa vertical velocity", 70000.0),
    vertical_velocity_spec!("vvel500", "500 hPa vertical velocity", 50000.0),
    vertical_velocity_spec!("vvel300", "300 hPa vertical velocity", 30000.0),
    vertical_velocity_spec!("vvel250", "250 hPa vertical velocity", 25000.0),
    vertical_velocity_spec!("vvel200", "200 hPa vertical velocity", 20000.0),
    // Value ranges are the level's codebook coverage (quantize.rs).
    theta_e_spec!("thetae1000", "1000 hPa equivalent potential temperature", 100000.0, (235, 362)),
    theta_e_spec!("thetae925", "925 hPa equivalent potential temperature", 92500.0, (232, 359)),
    theta_e_spec!("thetae850", "850 hPa equivalent potential temperature", 85000.0, (230, 357)),
    theta_e_spec!("thetae700", "700 hPa equivalent potential temperature", 70000.0, (235, 362)),
    theta_e_spec!("thetae500", "500 hPa equivalent potential temperature", 50000.0, (250, 377)),
    theta_e_spec!("thetae300", "300 hPa equivalent potential temperature", 30000.0, (285, 412)),
    theta_e_spec!("thetae250", "250 hPa equivalent potential temperature", 25000.0, (295, 422)),
    theta_e_spec!("thetae200", "200 hPa equivalent potential temperature", 20000.0, (305, 432)),
];

pub fn variable_spec(variable_id: &str) -> Result<&'static VariableSpec> {
    VARIABLES
        .iter()
        .find(|spec| spec.id == variable_id)
        .ok_or_else(|| EncodeError::conversion(format!("unsupported variable: {variable_id}")))
}

#[cfg(test)]
mod tests {
    use super::{
        isobaric_variable, variable_spec, ISOBARIC_FAMILIES, ISOBARIC_LEVELS_HPA, OCEAN_VARIABLE_IDS,
        WAVE_VECTOR_COMPONENT_IDS,
    };
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
        assert_eq!(entries.len(), 9 * ISOBARIC_LEVELS_HPA.len());
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

    /// `tests/fixtures/surface-registry.json`: the surface diagnostics
    /// (wind gust, total cloud cover, CAPE), held to the Python encoder and
    /// the frontend the same way.
    #[test]
    fn the_surface_registry_matches_the_shared_fixture() {
        let entries = registry("surface-registry.json");
        assert_eq!(
            entries.keys().collect::<Vec<_>>(),
            ["gust", "tcdc", "cape", "vis", "dpt2m", "aptmp2m", "lcdc", "mcdc", "hcdc"],
            "nine variables, in the fixture's order"
        );
        for (variable_id, entry) in entries {
            let spec = variable_spec(&variable_id).unwrap_or_else(|_| panic!("{variable_id}"));
            assert_eq!(json!(spec.label), entry["label"], "{variable_id}");
            assert_eq!(json!(spec.output_unit), entry["unit"], "{variable_id}");
            assert_eq!(
                Value::Object(spec.parameter_metadata()),
                entry["parameter"],
                "{variable_id} GRIB2 identity"
            );
            // balanced is quality everywhere but cloud cover — the total
            // and the layers — which takes the compact 1 % step like
            // relative humidity.
            let balanced_key = if variable_id.ends_with("cdc") { "compact" } else { "quality" };
            for (profile, key) in [("quality", "quality"), ("compact", "compact"), ("balanced", balanced_key)] {
                let book = codebook(profile, &variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(Value::Object(book.metadata()), entry[key], "{variable_id} {profile} codebook");
            }
        }
    }

    /// `tests/fixtures/ocean-registry.json`: the surface temperature, the
    /// sea ice fields and the wave fields, held to the Python encoder the
    /// same way — plus the two derived wave vector components.
    #[test]
    fn the_ocean_registry_matches_the_shared_fixture() {
        let entries = registry("ocean-registry.json");
        let expected: Vec<&str> = OCEAN_VARIABLE_IDS
            .iter()
            .chain(&WAVE_VECTOR_COMPONENT_IDS)
            .copied()
            .collect();
        assert_eq!(
            entries.keys().collect::<Vec<_>>(),
            expected,
            "eight variables, in the fixture's order"
        );
        for (variable_id, entry) in entries {
            let spec = variable_spec(&variable_id).unwrap_or_else(|_| panic!("{variable_id}"));
            assert_eq!(json!(spec.label), entry["label"], "{variable_id}");
            assert_eq!(json!(spec.output_unit), entry["unit"], "{variable_id}");
            assert_eq!(
                Value::Object(spec.parameter_metadata()),
                entry["parameter"],
                "{variable_id} GRIB2 identity"
            );
            // balanced is quality everywhere but ice cover, read in tenths,
            // which takes the compact 1 % step like cloud cover.
            let balanced_key = if variable_id == "icec" { "compact" } else { "quality" };
            let wave = matches!(variable_id.as_str(), "htsgw" | "perpw" | "dirpw");
            let derived = WAVE_VECTOR_COMPONENT_IDS.contains(&variable_id.as_str());
            for (profile, key) in [("quality", "quality"), ("compact", "compact"), ("balanced", balanced_key)] {
                let book = codebook(profile, &variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(Value::Object(book.metadata()), entry[key], "{variable_id} {profile} codebook");
                let linear = book.as_linear().expect("linear");
                if derived {
                    // Symmetric, with 0 — land, (0, 0) exactly — on the
                    // grid in every profile.
                    assert_eq!(-linear.minimum, linear.maximum, "{variable_id} {profile}");
                    let mut code = [0u8];
                    book.quantize(&[0.0], &mut code).expect("in range");
                    assert_eq!(u16::from(code[0]), linear.maximum_code() / 2, "{variable_id} {profile}: land");
                } else {
                    // A bitmap-masked point becomes the bottom of the
                    // codebook, so that must be the value the registry says
                    // it is.
                    assert_eq!(linear.minimum, f64::from(spec.value_range.0), "{variable_id}");
                }
            }
            assert_eq!(spec.fill_values, if wave { &[9999.0][..] } else { &[][..] }, "{variable_id}");
            assert_eq!(
                spec.grib2_level_value.is_none(),
                wave || derived,
                "{variable_id}: WAVEWATCH III's surface value"
            );
            assert_eq!(spec.grib_element.is_empty(), derived, "{variable_id}: derived, never matched");
        }
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
