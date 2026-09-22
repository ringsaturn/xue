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

/// Another record the same quantity arrives as at another centre, when it
/// differs from the registered identity in more than the parameter number:
/// another fixed surface (ECMWF's most-unstable CAPE departs from surface
/// type 17, its skin temperature is 0/0/17 with no surface value), a
/// statistical product (its `10fg` is the maximum gust over the interval
/// ending at the frame), a unit GDAL spells differently (its `tcc` is a 0–1
/// fraction). Accepted when matching a record and never written. Mirrors
/// `RecordAlternate` in `xuebuild/variables.py`.
#[derive(Debug, Clone, Copy)]
pub struct RecordAlternate {
    pub discipline: u8,
    pub category: u8,
    pub number: u8,
    pub level_type: u8,
    /// As `VariableSpec::grib2_level_value`: `None` accepts any.
    pub level_value: Option<f64>,
    /// As `VariableSpec::grib2_statistical`: 2 for a maximum.
    pub statistical: Option<u8>,
    /// The unit GDAL reports for this record when it differs from the
    /// primary's (`VariableSpec::gdal_unit`); empty for the same.
    pub gdal_unit: &'static str,
}

impl RecordAlternate {
    pub fn triple(&self) -> (u8, u8, u8) {
        (self.discipline, self.category, self.number)
    }
}

#[derive(Debug, Clone, Copy)]
pub struct VariableSpec {
    pub id: &'static str,
    /// English label carried in bundle metadata.
    pub label: &'static str,
    /// Unit of the values a bundle's codebook quantizes.
    pub output_unit: &'static str,
    pub value_range: (f64, f64),
    pub grib_element: &'static str,
    /// The name Open-Meteo gives this quantity in its open data
    /// (`temperature_2m`, `wind_gusts_10m`), empty when the variable is not
    /// fetched from there. It is another centre's spelling: the Python fetch
    /// names the variable with it (`om2nc fetch --var`) and this encoder
    /// opens the series by it, since the NetCDF variable inside the file
    /// keeps the Open-Meteo name while the file itself is named by the Xue
    /// id (`observation::series_variable_name`). Mirrors `open_meteo` in
    /// `xuebuild/variables.py`.
    pub open_meteo: &'static str,
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
    /// Whole other record identities the same quantity arrives as, when an
    /// alias triple is not enough ([`RecordAlternate`]): accepted when
    /// matching, never written.
    pub grib2_alternates: &'static [RecordAlternate],
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
    /// For a field an algorithm derived from other variables rather than an
    /// instrument measured — a satellite composite — the `producer.id` of
    /// the block written beside its `parameter` (docs/format.md §"Band and
    /// Producer"): `shachen` for the Dust RGB guns. The algorithm's
    /// *version* is not registered: the fetch stage stamps it on the series
    /// it produced, the converters read it off the series, and a series
    /// whose producer is not this one is refused. Such a variable's
    /// parameter is in GRIB2's local-use range and means nothing without
    /// the producer. Mirrors `producer_id` in `xuebuild/variables.py`.
    pub producer_id: Option<&'static str>,
    /// For a GRIB2 aerosol product (template 4.48 and its subsets), the
    /// rest of its identity: the aerosol type and the size and wavelength
    /// intervals ([`AerosolIdentity`]), matched whole beside the parameter
    /// and written as the `aerosol` block beside it. `None` for every
    /// other variable, which matches no aerosol record. An aerosol
    /// variable takes no aliases and no alternates. Mirrors
    /// `grib2_aerosol` in `xuebuild/variables.py`.
    pub grib2_aerosol: Option<AerosolIdentity>,
}

/// What a GRIB2 aerosol product carries beyond its parameter: the fields of
/// product definition template 4.48 (and of 4.44 / 4.46, its subsets)
/// between the parameter number and the generating process — the aerosol
/// type (code table 4.233), a size interval and a wavelength interval (each
/// a code table 4.91 type with two limits). A parameter triple on a surface
/// names every one of GEFS-Aerosols' optical thicknesses at once (0/20/102
/// on the entire atmosphere is the total, the dust, the sea salt, ... at
/// seven wavelengths), so for such a record the identity is the parameter
/// block *and* this one, written beside it as the metadata's `aerosol`
/// block (docs/format.md §"Band, Producer and Aerosol") and compared whole
/// when matching a record.
///
/// A limit is a `(scaleFactor, scaledValue)` pair, the value being
/// `scaledValue × 10^−scaleFactor` metres, or `None` where GRIB2 encodes it
/// as missing. An interval whose type is 255 (missing) carries no limits:
/// both pairs are `None` here whatever the record wrote — NCEP writes zeros
/// rather than the missing pattern for the wavelength of a particulate
/// matter record — so the record matches the registered identity and the
/// file never carries the zeros. Mirrors `AerosolIdentity` in
/// `xuebuild/variables.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AerosolIdentity {
    /// Code table 4.233: 62000 total aerosol, 62001 dust, 62006 sulphate,
    /// 62008 sea salt, 62009 black carbon, 62010 particulate organic matter.
    pub aerosol_type: u16,
    /// Code table 4.91 type of the size interval: 0 "smaller than first
    /// limit", 7 "between first and second limit", 255 missing.
    pub size_type: u8,
    pub size_first: Option<(i8, u32)>,
    pub size_second: Option<(i8, u32)>,
    pub wavelength_type: u8,
    pub wavelength_first: Option<(i8, u32)>,
    pub wavelength_second: Option<(i8, u32)>,
}

impl AerosolIdentity {
    /// Code table 4.91's missing interval type.
    pub const MISSING_TYPE: u8 = 255;

    /// The identity as the schema v3 `aerosol` block: every field of
    /// template 4.48, a missing limit written as GRIB2 encodes it, `null`
    /// for both halves.
    pub fn metadata(&self) -> Map<String, Value> {
        fn pair(limit: Option<(i8, u32)>) -> (Value, Value) {
            match limit {
                Some((scale_factor, scaled_value)) => (json!(scale_factor), json!(scaled_value)),
                None => (Value::Null, Value::Null),
            }
        }
        let (size_first_scale, size_first_value) = pair(self.size_first);
        let (size_second_scale, size_second_value) = pair(self.size_second);
        let (wavelength_first_scale, wavelength_first_value) = pair(self.wavelength_first);
        let (wavelength_second_scale, wavelength_second_value) = pair(self.wavelength_second);
        let mut block = Map::new();
        block.insert("aerosolType".into(), json!(self.aerosol_type));
        block.insert("typeOfSizeInterval".into(), json!(self.size_type));
        block.insert("scaleFactorOfFirstSize".into(), size_first_scale);
        block.insert("scaledValueOfFirstSize".into(), size_first_value);
        block.insert("scaleFactorOfSecondSize".into(), size_second_scale);
        block.insert("scaledValueOfSecondSize".into(), size_second_value);
        block.insert("typeOfWavelengthInterval".into(), json!(self.wavelength_type));
        block.insert("scaleFactorOfFirstWavelength".into(), wavelength_first_scale);
        block.insert("scaledValueOfFirstWavelength".into(), wavelength_first_value);
        block.insert("scaleFactorOfSecondWavelength".into(), wavelength_second_scale);
        block.insert("scaledValueOfSecondWavelength".into(), wavelength_second_value);
        block
    }
}

/// The identity every GEFS-Aerosols optical thickness at 550 nm shares but
/// for the species: particles smaller than 20 µm (size type 0, the first
/// limit 20 × 10⁻⁶ m, the unused second limit written 0 / 0 as NCEP does),
/// between 545 and 565 nm (wavelength type 7, both limits in 10⁻⁹ m).
/// Mirrors `_aod_550nm_identity` in `xuebuild/variables.py`.
const fn aod_550nm_identity(aerosol_type: u16) -> AerosolIdentity {
    AerosolIdentity {
        aerosol_type,
        size_type: 0,
        size_first: Some((6, 20)),
        size_second: Some((0, 0)),
        wavelength_type: 7,
        wavelength_first: Some((9, 545)),
        wavelength_second: Some((9, 565)),
    }
}

/// A particulate matter record's identity: the species and particles
/// smaller than the size limit, with no wavelength interval (type 255,
/// no limits).
const fn particulate_identity(aerosol_type: u16, size_first: (i8, u32)) -> AerosolIdentity {
    AerosolIdentity {
        aerosol_type,
        size_type: 0,
        size_first: Some(size_first),
        size_second: Some((0, 0)),
        wavelength_type: AerosolIdentity::MISSING_TYPE,
        wavelength_first: None,
        wavelength_second: None,
    }
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
/// The satellite channels, the composite guns and the confidence, held to
/// the Python encoder by `tests/fixtures/satellite-registry.json`. The
/// channels are what a platform's imager measures (a `band` block each
/// when published); the guns are what the Dust RGB producer derives from
/// four of them, the three variables of the `dustrgb` bundle in bundle
/// order. Mirror `SATELLITE_CHANNEL_IDS`, `DUST_RGB_BUNDLE_ID`,
/// `DUST_RGB_COMPONENT_IDS`, `DUST_CF_BUNDLE_ID`, `DUST_CF_COMPONENT_IDS`
/// and `SATELLITE_VARIABLE_IDS` in `xuebuild/variables.py`.
pub const SATELLITE_CHANNEL_IDS: &[&str] = &["ir039", "wv062", "ir086", "ir104", "ir112", "ir123"];
pub const DUST_RGB_BUNDLE_ID: &str = "dustrgb";
pub const DUST_RGB_COMPONENT_IDS: [&str; 3] = ["dustr", "dustg", "dustb"];
/// The DEBRA confidence is a composite bundle of one variable: produced in
/// the fetch stage like the guns, read off the series as a variable of its
/// own, never derived by a converter.
pub const DUST_CF_BUNDLE_ID: &str = "dustcf";
pub const DUST_CF_COMPONENT_IDS: [&str; 1] = ["dustcf"];
#[allow(dead_code)] // read by the registry test; the Python side keys its fixture on it
pub const SATELLITE_VARIABLE_IDS: &[&str] = &[
    "ir039", "wv062", "ir086", "ir104", "ir112", "ir123", "dustr", "dustg", "dustb", "dustcf",
];

/// The aerosol set, in the order GEFS-Aerosols publishes it: the six optical
/// thicknesses, then the three surface concentrations. Held to the Python
/// encoder and the frontend by `tests/fixtures/aerosol-registry.json`, which
/// carries each variable's `aerosol` block beside its parameter. Mirrors
/// `AEROSOL_VARIABLE_IDS` in `xuebuild/variables.py`.
pub const AEROSOL_VARIABLE_IDS: &[&str] = &[
    "aod", "aoddust", "aodsalt", "aodsulf", "aodorg", "aodbc", "pm25", "pm10", "pm10dust",
];

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
            open_meteo: "",
            grib2_discipline: 0,
            grib2_category: $category,
            grib2_number: $number,
            grib2_level_type: 100,
            grib2_level_value: Some($level_pa),
            grib2_statistical: None,
            grib2_aliases: &[],
            grib2_alternates: &[],
            gdal_unit: $gdal_unit,
            fill_values: &[],
            producer_id: None,
            grib2_aerosol: None,
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
        isobaric_spec!($id, $label, $level_pa, (0.0, 100.0), "%", "RH", 1, 1, "%")
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
        isobaric_spec!($id, $label, $level_pa, (-127.0, 127.0), "m/s", "UGRD", 2, 2, "m/s")
    };
}
macro_rules! v_wind_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-127.0, 127.0), "m/s", "VGRD", 2, 3, "m/s")
    };
}
// Water vapour flux, q·V/g in g·cm⁻¹·hPa⁻¹·s⁻¹ — derived by the converter
// from the specific humidity and the wind on the same surface, never fetched,
// so the record-matching fields stay empty. GRIB2 has no standard parameter
// for a per-level horizontal vapour flux; 250 / 251 are local-use numbers of
// our own in the moisture category.
macro_rules! vapour_flux_spec {
    ($id:literal, $label:literal, $level_pa:literal, $number:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-64.0, 64.0), "g/(cm·hPa·s)", "", 1, $number, "")
    };
}
// Vertical velocity in pressure coordinates, ω = dp/dt: negative is ascent.
macro_rules! vertical_velocity_spec {
    ($id:literal, $label:literal, $level_pa:literal) => {
        isobaric_spec!($id, $label, $level_pa, (-6.0, 6.0), "Pa/s", "VVEL", 2, 8, "Pa/s")
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
        value_range: (-60.0, 50.0),
        grib_element: "TMP",
        open_meteo: "temperature_2m",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 0,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "C",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "prate",
        label: "Precipitation rate",
        output_unit: "mm/h",
        value_range: (0.0, 50.0),
        grib_element: "PRATE",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 7,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        // The MRMS radar-derived rate: the MRMS-local 209/6/1 on a "specific
        // altitude above mean sea level" surface at 0 m, already in mm/h
        // (GDAL spells it `mm/hr`), so the kg m⁻² s⁻¹ scaling does not apply
        // to it. Points outside radar coverage carry -3.
        grib2_alternates: &[RecordAlternate {
            discipline: 209,
            category: 6,
            number: 1,
            level_type: 102,
            level_value: Some(0.0),
            statistical: None,
            gdal_unit: "mm/hr",
        }],
        gdal_unit: "kg/(m^2 s)",
        fill_values: &[-3.0],
        producer_id: None,
        grib2_aerosol: None,
    },
    // ECMWF open data has no rate field: tp is the run-total accumulation
    // (metres, ECMWF-local GRIB2 parameter 0/1/193). Input-only — the
    // converter de-accumulates it into prate. AIFS writes the same run total
    // under the WMO 0/1/52 (TPRATE to GDAL's tables, in kg/(m^2*s)) as an
    // accumulation in kg/m², a millimetre: the alternate carries that unit
    // so the converter leaves the values alone where the IFS record's
    // metres are scaled up.
    VariableSpec {
        id: "tp",
        label: "Total precipitation",
        output_unit: "m",
        value_range: (0.0, 1.0),
        grib_element: "unknown",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 193,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: Some(1),
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 1,
            number: 52,
            level_type: 1,
            level_value: None,
            statistical: Some(1),
            gdal_unit: "kg/(m^2*s)",
        }],
        gdal_unit: "-",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Open-Meteo's `precipitation` is a third arrival shape: the total that
    // fell over the interval since the model's *previous native output time*
    // (one hour to +90 h on IFS HRES, three to +144, six beyond), in
    // millimetres. That is GRIB2's APCP, 0/1/8 accumulated (statistical
    // process 1) on the ground surface — an accumulation over an interval
    // rather than over the run, which is why it is not ECMWF's `tp`. Like tp
    // and prate_ave it is an input-only variable: the converter divides it by
    // the interval into prate (`SourceSpec::interval_precipitation`) and apcp
    // itself never reaches a bundle. GFS pgrb2 carries APCP records too; no
    // source fetches them.
    VariableSpec {
        id: "apcp",
        label: "Total precipitation",
        output_unit: "mm",
        value_range: (0.0, 1000.0),
        grib_element: "APCP",
        open_meteo: "precipitation",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 8,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: Some(1),
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "kg/(m^2)",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // GFS sflux PRATE is the mean rate over an averaging window that resets
    // every 6 hours. Input-only — the converter de-averages it into prate.
    VariableSpec {
        id: "prate_ave",
        label: "Window-averaged precipitation rate",
        output_unit: "kg/m^2s",
        value_range: (0.0, 1.0),
        grib_element: "PRATE",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 7,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: Some(0),
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "kg/(m^2 s)",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "dswrf",
        label: "Downward shortwave radiation flux",
        output_unit: "W/m²",
        value_range: (0.0, 1270.0),
        grib_element: "DSWRF",
        open_meteo: "shortwave_radiation",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 192,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "W/(m^2)",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "ugrd10m",
        label: "10 meter U wind component",
        output_unit: "m/s",
        value_range: (-64.0, 64.0),
        grib_element: "UGRD",
        open_meteo: "wind_u_component_10m",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 2,
        grib2_level_type: 103,
        grib2_level_value: Some(10.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m/s",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "vgrd10m",
        label: "10 meter V wind component",
        output_unit: "m/s",
        value_range: (-64.0, 64.0),
        grib_element: "VGRD",
        open_meteo: "wind_v_component_10m",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 3,
        grib2_level_type: 103,
        grib2_level_value: Some(10.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m/s",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // 100 m wind components, the hub height of a modern turbine, delivered
    // together as the `wind100m` bundle: the same parameters on the 100 m
    // surface (type 103, value 100), which pgrb2 writes for wind power.
    // Mirrors `ugrd100m` / `vgrd100m` in `xuebuild/variables.py`.
    VariableSpec {
        id: "ugrd100m",
        label: "100 meter U wind component",
        output_unit: "m/s",
        value_range: (-64.0, 64.0),
        grib_element: "UGRD",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 2,
        grib2_level_type: 103,
        grib2_level_value: Some(100.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m/s",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "vgrd100m",
        label: "100 meter V wind component",
        output_unit: "m/s",
        value_range: (-64.0, 64.0),
        grib_element: "VGRD",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 3,
        grib2_level_type: 103,
        grib2_level_value: Some(100.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m/s",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Composite reflectivity: the column maximum, so its fixed surface is
    // the entire atmosphere (type 10, which carries no value). It arrives
    // two ways under one identity: as the radar mosaic's NetCDF observation
    // series, and as the reflectivity HRRR forecasts — the `REFC` record,
    // NCEP's local 0/16/196 (the alias), which GDAL reports in dB.
    VariableSpec {
        id: "cref",
        label: "Composite radar reflectivity",
        output_unit: "dBZ",
        value_range: (0.0, 80.0),
        grib_element: "REFC",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 16,
        grib2_number: 5,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[(0, 16, 196)],
        // The MRMS mosaic's own composite: the MRMS-local 209/10/0, stamped
        // on a "specific altitude above mean sea level" surface at 500 m, in
        // dBZ. Points outside radar coverage carry -999 and points inside
        // it with no echo -99; both are the codebook bottom.
        grib2_alternates: &[RecordAlternate {
            discipline: 209,
            category: 10,
            number: 0,
            level_type: 102,
            level_value: Some(500.0),
            statistical: None,
            gdal_unit: "dBZ",
        }],
        gdal_unit: "dB",
        fill_values: &[-999.0, -99.0],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Three more surface diagnostics, each a GRIB record of its own with no
    // unit conversion. Registered from the GFS pgrb2 set; ECMWF open data
    // carries neighbours rather than equivalents, each accepted through a
    // `RecordAlternate` so the two models publish one chart under one
    // identity (see `xuebuild/variables.py`).
    //
    // Wind gust: the instantaneous surface gust diagnostic, 0/2/22 on the
    // ground surface. ECMWF's is the same parameter on the 10 m surface as
    // the *maximum* over the interval ending at the frame (template 4.8,
    // statistical process 2); all zeros at the analysis, so a source lists
    // it as optional there.
    VariableSpec {
        id: "gust",
        label: "Wind gust",
        output_unit: "m/s",
        value_range: (0.0, 127.0),
        grib_element: "GUST",
        open_meteo: "wind_gusts_10m",
        grib2_discipline: 0,
        grib2_category: 2,
        grib2_number: 22,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 2,
            number: 22,
            level_type: 103,
            level_value: Some(10.0),
            statistical: Some(2),
            gdal_unit: "",
        }],
        gdal_unit: "m/s",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Total cloud cover over the whole column, 0/6/1 on the entire
    // atmosphere (surface type 10, no value); the identity's missing
    // statistical process is what rejects pgrb2's interval average of the
    // same field. ECMWF `tcc` is the ECMWF-local 0/6/192 as a 0–1 fraction
    // on the ground surface (GDAL reports its unit as "-"), scaled to
    // percent by the converter; AIFS writes it under the WMO 0/6/1 in
    // percent as a layer from the ground surface to the top of the
    // atmosphere. NCEP writes the same "entire atmosphere" layer as its
    // local surface type 200 rather than the WMO type 10 pgrb2 uses in the
    // CFSv2 time series — GDAL renders both `0-EATM` — so that is a third
    // alternate.
    VariableSpec {
        id: "tcdc",
        label: "Total cloud cover",
        output_unit: "%",
        value_range: (0.0, 100.0),
        grib_element: "TCDC",
        open_meteo: "cloud_cover",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 1,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[
            RecordAlternate {
                discipline: 0,
                category: 6,
                number: 192,
                level_type: 1,
                level_value: None,
                statistical: None,
                gdal_unit: "-",
            },
            RecordAlternate {
                discipline: 0,
                category: 6,
                number: 1,
                level_type: 1,
                level_value: None,
                statistical: None,
                gdal_unit: "",
            },
            RecordAlternate {
                discipline: 0,
                category: 6,
                number: 1,
                level_type: 200,
                level_value: None,
                statistical: None,
                gdal_unit: "",
            },
        ],
        gdal_unit: "%",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Surface-based convective available potential energy, 0/7/6 on the
    // ground surface — not the mixed-layer variants on surface type 108.
    // ECMWF open data carries the most-unstable CAPE (`mucape`), the same
    // parameter departing from surface type 17.
    VariableSpec {
        id: "cape",
        label: "Convective available potential energy",
        output_unit: "J/kg",
        value_range: (0.0, 6350.0),
        grib_element: "CAPE",
        open_meteo: "cape",
        grib2_discipline: 0,
        grib2_category: 7,
        grib2_number: 6,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 7,
            number: 6,
            level_type: 17,
            level_value: None,
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "J/kg",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Convective inhibition, 0/7/7 on the ground surface: the negative area
    // under the parcel path, so never positive. GFS caps the field near
    // -1000 J/kg in the strongest cap; the codebook runs to -1016 at 4 J/kg.
    // Mirrors `cin` in `xuebuild/variables.py`.
    VariableSpec {
        id: "cin",
        label: "Convective inhibition",
        output_unit: "J/kg",
        value_range: (-1016.0, 0.0),
        grib_element: "CIN",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 7,
        grib2_number: 7,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "J/kg",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Planetary boundary layer height, published by NCEP under its local
    // 0/3/196 on the ground surface (not the WMO 0/3/18), which is the
    // number pgrb2's `:HPBL:surface:` record carries. Metres.
    VariableSpec {
        id: "hpbl",
        label: "Planetary boundary layer height",
        output_unit: "m",
        value_range: (0.0, 5080.0),
        grib_element: "HPBL",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 3,
        grib2_number: 196,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The four categorical precipitation-type flags pgrb2 carries, each a 0/1
    // field on the ground surface (NCEP-local 0/1/192–195): rain, freezing
    // rain, ice pellets and snow. Input-only — the converter combines them
    // into the one categorical `ptype` a run publishes
    // (`convert::derive_ptype`) — so none ever reaches a bundle.
    VariableSpec {
        id: "crain",
        label: "Categorical rain",
        output_unit: "1",
        value_range: (0.0, 1.0),
        grib_element: "CRAIN",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 192,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "0=no; 1=yes",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "cfrzr",
        label: "Categorical freezing rain",
        output_unit: "1",
        value_range: (0.0, 1.0),
        grib_element: "CFRZR",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 193,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "0=no; 1=yes",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "cicep",
        label: "Categorical ice pellets",
        output_unit: "1",
        value_range: (0.0, 1.0),
        grib_element: "CICEP",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 194,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "0=no; 1=yes",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "csnow",
        label: "Categorical snow",
        output_unit: "1",
        value_range: (0.0, 1.0),
        grib_element: "CSNOW",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 195,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "0=no; 1=yes",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Precipitation type: the four flags above combined into one categorical
    // field, derived by the converter and never fetched, so the
    // record-matching fields stay empty. The values are WMO code table
    // 4.201's own: 1 rain, 3 freezing rain, 5 snow, 8 ice pellets. When two
    // flags disagree the order rain, freezing rain, ice pellets, snow
    // decides, each later flag overriding; both encoders share that order to
    // stay byte-identical.
    VariableSpec {
        id: "ptype",
        label: "Precipitation type",
        output_unit: "1",
        value_range: (0.0, 8.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 19,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Surface visibility, 0/19/0 on the ground surface: GRIB2 carries metres,
    // the codebook quantizes kilometres.
    VariableSpec {
        id: "vis",
        label: "Visibility",
        output_unit: "km",
        value_range: (0.0, 25.0),
        grib_element: "VIS",
        open_meteo: "visibility",
        grib2_discipline: 0,
        grib2_category: 19,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // 2 m dew point, 0/0/6 on the 2 m surface — the 2 m temperature's own
    // matching and unit rules.
    VariableSpec {
        id: "dpt2m",
        label: "2 meter dew point temperature",
        output_unit: "°C",
        value_range: (-70.0, 40.0),
        grib_element: "DPT",
        open_meteo: "dew_point_2m",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 6,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "C",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // NCEP's apparent temperature, 0/0/21 on the 2 m surface.
    VariableSpec {
        id: "aptmp2m",
        label: "2 meter apparent temperature",
        output_unit: "°C",
        value_range: (-90.0, 60.0),
        grib_element: "APTMP",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 21,
        grib2_level_type: 103,
        grib2_level_value: Some(2.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "C",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // Precipitable water: the vertically integrated water vapour of the whole
    // column, 0/1/3 on NCEP's local "entire atmosphere (considered as a single
    // layer)" surface (type 200, which carries no value, like the WMO type 10
    // pgrb2 writes the total cloud cover on). GRIB2 carries it in kg/m², which
    // is a millimetre of liquid water, so no unit conversion applies. Mirrors
    // `pwat` in `xuebuild/variables.py`.
    VariableSpec {
        id: "pwat",
        label: "Precipitable water",
        output_unit: "kg/m²",
        value_range: (0.0, 127.0),
        grib_element: "PWAT",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 1,
        grib2_number: 3,
        grib2_level_type: 200,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "kg/(m^2)",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The cloud layers: three parameters of their own (0/6/3, 0/6/4, 0/6/5),
    // each on its own layer surface (214 low, 224 middle, 234 high), which
    // carries no value. AIFS open data carries the three (`lcc` / `mcc` /
    // `hcc`, in percent) on surfaces of its own — the low layer from the
    // ground surface, the middle from the 800 hPa isobaric surface, the high
    // from 450 hPa — the ECMWF layer boundaries as first fixed surfaces.
    VariableSpec {
        id: "lcdc",
        label: "Low cloud cover",
        output_unit: "%",
        value_range: (0.0, 100.0),
        grib_element: "LCDC",
        open_meteo: "cloud_cover_low",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 3,
        grib2_level_type: 214,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 6,
            number: 3,
            level_type: 1,
            level_value: None,
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "%",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "mcdc",
        label: "Middle cloud cover",
        output_unit: "%",
        value_range: (0.0, 100.0),
        grib_element: "MCDC",
        open_meteo: "cloud_cover_mid",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 4,
        grib2_level_type: 224,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 6,
            number: 4,
            level_type: 100,
            level_value: Some(80000.0),
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "%",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "hcdc",
        label: "High cloud cover",
        output_unit: "%",
        value_range: (0.0, 100.0),
        grib_element: "HCDC",
        open_meteo: "cloud_cover_high",
        grib2_discipline: 0,
        grib2_category: 6,
        grib2_number: 5,
        grib2_level_type: 234,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 6,
            number: 5,
            level_type: 100,
            level_value: Some(45000.0),
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "%",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The ocean fields of the pgrb2 set (see `xuebuild/variables.py`): the
    // ground-or-water skin temperature, 0/0/0 on surface type 1 — the SST
    // over the sea — and the two sea ice fields of GRIB2's oceanographic
    // discipline (10): cover (10/2/0), a 0–1 proportion the codebook takes
    // in percent, and thickness (10/2/1) in metres. ECMWF writes the skin
    // temperature as its own parameter, 0/0/17 (`skt`, GDAL's SKINT), with
    // no surface value, and the ice thickness under the same 10/2/1 with no
    // surface value and a bitmap over land.
    VariableSpec {
        id: "tmpsfc",
        label: "Surface temperature",
        output_unit: "°C",
        value_range: (-60.0, 67.0),
        grib_element: "TMP",
        open_meteo: "surface_temperature",
        grib2_discipline: 0,
        grib2_category: 0,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 0,
            category: 0,
            number: 17,
            level_type: 1,
            level_value: None,
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "C",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "icec",
        label: "Sea ice cover",
        output_unit: "%",
        value_range: (0.0, 100.0),
        grib_element: "ICEC",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 2,
        grib2_number: 0,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Proportion",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "icetk",
        label: "Sea ice thickness",
        output_unit: "m",
        value_range: (0.0, 5.0),
        grib_element: "ICETK",
        open_meteo: "sea_ice_thickness",
        grib2_discipline: 10,
        grib2_category: 2,
        grib2_number: 1,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[RecordAlternate {
            discipline: 10,
            category: 2,
            number: 1,
            level_type: 1,
            level_value: None,
            statistical: None,
            gdal_unit: "",
        }],
        gdal_unit: "m",
        fill_values: &[9999.0],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The wave fields, appended to the frame from the cycle's second file
    // family (GFS-Wave, or the `wave` stream of ECMWF open data):
    // significant wave height (10/0/3), primary wave mean period (10/0/11)
    // and direction (10/0/10, degrees true, from). All on the water
    // surface, which WAVEWATCH III writes with a value of 1 where pgrb2
    // writes 0 and ECMWF none — so no value is declared and any is
    // accepted. ECMWF's peak period (`pp1d`, 10/0/34) and mean direction
    // (`mwd`, 10/0/14) are the nearest neighbours of the WAVEWATCH III
    // quantities and accepted as aliases. The records carry a bitmap: land
    // arrives as GDAL's nodata value, 9999.
    VariableSpec {
        id: "htsgw",
        label: "Significant wave height",
        output_unit: "m",
        value_range: (0.0, 25.0),
        grib_element: "HTSGW",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 3,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "m",
        fill_values: &[9999.0],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "perpw",
        label: "Primary wave mean period",
        output_unit: "s",
        value_range: (0.0, 25.0),
        grib_element: "PERPW",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 11,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[(10, 0, 34)],
        grib2_alternates: &[],
        gdal_unit: "s",
        fill_values: &[9999.0],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "dirpw",
        label: "Primary wave direction",
        output_unit: "°",
        value_range: (0.0, 358.0),
        grib_element: "DIRPW",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 10,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[(10, 0, 14)],
        grib2_alternates: &[],
        gdal_unit: "Degree true",
        fill_values: &[9999.0],
        producer_id: None,
        grib2_aerosol: None,
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
        value_range: (-25.0, 25.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 250,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "vwave",
        label: "V wave vector component",
        output_unit: "m",
        value_range: (-25.0, 25.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 10,
        grib2_category: 0,
        grib2_number: 251,
        grib2_level_type: 1,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The satellite channels: brightness temperature at the nominal top of
    // the atmosphere (0/4/4 on surface 8), one parameter for every infrared
    // band of every imager, so the `band` block beside it (written from the
    // source table's `bands`) says which channel of which instrument this
    // is. Never a GRIB record: a satellite source arrives as the NetCDF
    // series the Python fetch stage warps and stacks, read by
    // `observation.rs`. Cells outside the disk arrive as the product's fill
    // and become the codebook bottom, 180 K, which the renderer paints as
    // nothing. Mirrors `ir104` in `xuebuild/variables.py`.
    VariableSpec {
        id: "ir104",
        label: "Brightness temperature, 10.4 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The three other infrared windows the Dust RGB reads (AHI bands 11, 14
    // and 15; ABI channels 11, 14 and 15), the same parameter and codebook
    // as `ir104`: registered so a source can fetch them for a composite
    // (`SourceSpec::input_variable_ids`) or publish them by a source-table
    // line, and unpublished by every source today. Mirror `ir086`, `ir112`
    // and `ir123` in `xuebuild/variables.py`.
    VariableSpec {
        id: "ir086",
        label: "Brightness temperature, 8.6 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "ir112",
        label: "Brightness temperature, 11.2 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "ir123",
        label: "Brightness temperature, 12.3 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The two further windows DEBRA reads (Miller et al. 2017: the 3.9 µm
    // window for the night thin-cirrus test and the 6.2 µm water vapour
    // band for the deep-convection test; AHI bands 7 and 8, ABI channels 7
    // and 8), the same parameter and codebook again — the 3.9 µm window
    // by day carries reflected sunlight on top of the emission and can
    // read above the top of the codebook over desert, which only the test
    // that saturates at 8 K of contrast ever sees. Fetched for the
    // confidence field, registered variables, published by no source.
    // Mirror `ir039` and `wv062` in `xuebuild/variables.py`.
    VariableSpec {
        id: "ir039",
        label: "Brightness temperature, 3.9 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "wv062",
        label: "Brightness temperature, 6.2 µm",
        output_unit: "K",
        value_range: (180.0, 332.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 4,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The classic Dust RGB, the three guns of one composite bundle,
    // `dustrgb`: red is the 12.3 − 10.4 µm split window, green 11.2 − 8.6 µm
    // with a gamma, blue the 10.4 µm window, each stretched to 0–1 by the
    // `shachen` package in the fetch stage and stored as a plain number. A
    // composite has no GRIB2 parameter: each gun takes a local-use number
    // (discipline 3 space products, category 192, numbers 1–3) that means
    // nothing without the `producer` block beside it, which is why the
    // producer id is registered here. The codebook keeps code 0 below the
    // gun's range so a cell the disk does not cover — or one an input
    // channel lacked — is "no data" for all three guns at once, and black
    // stays a value. Mirror `dustr`, `dustg`, `dustb` in
    // `xuebuild/variables.py`.
    VariableSpec {
        id: "dustr",
        label: "Dust RGB, red gun",
        output_unit: "1",
        value_range: (-0.004, 1.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 3,
        grib2_category: 192,
        grib2_number: 1,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: Some("shachen"),
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "dustg",
        label: "Dust RGB, green gun",
        output_unit: "1",
        value_range: (-0.004, 1.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 3,
        grib2_category: 192,
        grib2_number: 2,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: Some("shachen"),
        grib2_aerosol: None,
    },
    VariableSpec {
        id: "dustb",
        label: "Dust RGB, blue gun",
        output_unit: "1",
        value_range: (-0.004, 1.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 3,
        grib2_category: 192,
        grib2_number: 3,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: Some("shachen"),
        grib2_aerosol: None,
    },
    // The DEBRA dust confidence (Miller et al. 2017, Eqs. 1–22, as the
    // `shachen` package implements them with its ABI retune), the one
    // variable of the `dustcf` bundle: the combined confidence factor in
    // 0–1 with the split-window gate applied — a cell where neither
    // split-window test responded reads 0, since cloud has no split-window
    // signal and dust nearly always does — computed per slot in the fetch
    // stage (`xuebuild/satellite/producers.py`) from five infrared windows,
    // a GFS skin temperature and the CAMEL emissivity climatology. A
    // produced field like the guns: the next local-use number under the
    // same producer, and the guns' codebook, so code 0 — a cell the disk
    // does not cover, an input lacked, or no emissivity is staged for over
    // land — is "no data" and 0.0 confidence stays a value at code 1.
    // Mirror `dustcf` in `xuebuild/variables.py`.
    VariableSpec {
        id: "dustcf",
        label: "DEBRA dust confidence",
        output_unit: "1",
        value_range: (-0.004, 1.0),
        grib_element: "",
        open_meteo: "",
        grib2_discipline: 3,
        grib2_category: 192,
        grib2_number: 4,
        grib2_level_type: 8,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "",
        fill_values: &[],
        producer_id: Some("shachen"),
        grib2_aerosol: None,
    },
    // The GEFS-Aerosols fields (NOAA's GEFS `chem` member, the GOCART
    // aerosol model coupled to the GFS), the first aerosol products this
    // pipeline reads: GRIB2 product definition template 4.48, whose
    // identity is the parameter block plus an `AerosolIdentity`. Six
    // optical thicknesses at 550 nm (WMO 0/20/102, "aerosol optical
    // thickness", on the entire atmosphere, dimensionless), the total and
    // five species, each told apart by the aerosol type alone: the file
    // carries every species over the same size interval (particles smaller
    // than 20 µm) and the same band (545–565 nm, the visible window an AOD
    // is quoted at). Then three surface concentrations under NCEP's local
    // PMTF / PMTC numbers (0/13/193 fine, 0/13/192 coarse), which GDAL
    // reports in µg/m³ already, so no conversion applies: PM2.5 (particles
    // under 2.5 µm) and PM10 (under 10 µm), and the PM10 of dust alone,
    // since a dust storm's surface concentration is what a dust forecast is
    // read for. A PM record's wavelength interval is missing (type 255) and
    // carries no limits. Mirror the nine entries in `xuebuild/variables.py`.
    VariableSpec {
        id: "aod",
        label: "Aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62000)),
    },
    VariableSpec {
        id: "aoddust",
        label: "Dust aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62001)),
    },
    VariableSpec {
        id: "aodsalt",
        label: "Sea salt aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62008)),
    },
    VariableSpec {
        id: "aodsulf",
        label: "Sulphate aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62006)),
    },
    VariableSpec {
        id: "aodorg",
        label: "Organic carbon aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62010)),
    },
    VariableSpec {
        id: "aodbc",
        label: "Black carbon aerosol optical depth at 550 nm",
        output_unit: "1",
        value_range: (0.0, 5.0),
        grib_element: "AOTK",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 20,
        grib2_number: 102,
        grib2_level_type: 10,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "Numeric",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(aod_550nm_identity(62009)),
    },
    VariableSpec {
        id: "pm25",
        label: "PM2.5 surface concentration",
        output_unit: "µg/m³",
        value_range: (0.0, 1000.0),
        grib_element: "PMTF",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 13,
        grib2_number: 193,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "10^-6g/m^3",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(particulate_identity(62000, (7, 25))),
    },
    VariableSpec {
        id: "pm10",
        label: "PM10 surface concentration",
        output_unit: "µg/m³",
        value_range: (0.0, 2000.0),
        grib_element: "PMTC",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 13,
        grib2_number: 192,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "10^-6g/m^3",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(particulate_identity(62000, (6, 10))),
    },
    VariableSpec {
        id: "pm10dust",
        label: "Dust PM10 surface concentration",
        output_unit: "µg/m³",
        value_range: (0.0, 2000.0),
        grib_element: "PMTC",
        open_meteo: "",
        grib2_discipline: 0,
        grib2_category: 13,
        grib2_number: 192,
        grib2_level_type: 1,
        grib2_level_value: Some(0.0),
        grib2_statistical: None,
        grib2_aliases: &[],
        grib2_alternates: &[],
        gdal_unit: "10^-6g/m^3",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: Some(particulate_identity(62001, (6, 10))),
    },
    // Mean sea level pressure. NCEP publishes two reductions; PRMSL (0/3/1)
    // is the same quantity ECMWF calls `msl` — encoded there as plain
    // pressure (0/3/0) on the mean sea level surface, hence the alias — so
    // both sources carry the same field under one identity, and HRRR's MAPS
    // reduction MSLMA (0/3/198, NCEP-local) is accepted under it too. MSLET
    // (0/3/192, the NCEP-local Shuell reduction) is a different quantity and
    // is deliberately not registered. Surface 101 ("mean sea level") carries
    // no value.
    VariableSpec {
        id: "prmsl",
        label: "Mean sea level pressure",
        output_unit: "hPa",
        value_range: (870.0, 1125.0),
        grib_element: "PRMSL",
        open_meteo: "pressure_msl",
        grib2_discipline: 0,
        grib2_category: 3,
        grib2_number: 1,
        grib2_level_type: 101,
        grib2_level_value: None,
        grib2_statistical: None,
        grib2_aliases: &[(0, 3, 0), (0, 3, 198)],
        grib2_alternates: &[],
        gdal_unit: "Pa",
        fill_values: &[],
        producer_id: None,
        grib2_aerosol: None,
    },
    // The isobaric families, eight levels each. Value ranges are the level's
    // codebook coverage (quantize.rs), truncated to integers.
    height_spec!("hgt1000", "1000 hPa geopotential height", 100000.0, (-905.0, 1635.0)),
    height_spec!("hgt925", "925 hPa geopotential height", 92500.0, (-249.0, 1275.0)),
    height_spec!("hgt850", "850 hPa geopotential height", 85000.0, (423.0, 1947.0)),
    height_spec!("hgt700", "700 hPa geopotential height", 70000.0, (1911.0, 3435.0)),
    height_spec!("hgt500", "500 hPa geopotential height", 50000.0, (4252.0, 6284.0)),
    height_spec!("hgt300", "300 hPa geopotential height", 30000.0, (7505.0, 10045.0)),
    height_spec!("hgt250", "250 hPa geopotential height", 25000.0, (8598.0, 11646.0)),
    height_spec!("hgt200", "200 hPa geopotential height", 20000.0, (10086.0, 13134.0)),
    temperature_spec!("tmp1000", "1000 hPa temperature", 100000.0, (-60.0, 60.0)),
    temperature_spec!("tmp925", "925 hPa temperature", 92500.0, (-65.0, 50.0)),
    temperature_spec!("tmp850", "850 hPa temperature", 85000.0, (-70.0, 45.0)),
    temperature_spec!("tmp700", "700 hPa temperature", 70000.0, (-75.0, 35.0)),
    temperature_spec!("tmp500", "500 hPa temperature", 50000.0, (-85.0, 15.0)),
    temperature_spec!("tmp300", "300 hPa temperature", 30000.0, (-95.0, 0.0)),
    temperature_spec!("tmp250", "250 hPa temperature", 25000.0, (-100.0, -5.0)),
    temperature_spec!("tmp200", "200 hPa temperature", 20000.0, (-100.0, -10.0)),
    humidity_spec!("rh1000", "1000 hPa relative humidity", 100000.0),
    humidity_spec!("rh925", "925 hPa relative humidity", 92500.0),
    humidity_spec!("rh850", "850 hPa relative humidity", 85000.0),
    humidity_spec!("rh700", "700 hPa relative humidity", 70000.0),
    humidity_spec!("rh500", "500 hPa relative humidity", 50000.0),
    humidity_spec!("rh300", "300 hPa relative humidity", 30000.0),
    humidity_spec!("rh250", "250 hPa relative humidity", 25000.0),
    humidity_spec!("rh200", "200 hPa relative humidity", 20000.0),
    specific_humidity_spec!("spfh1000", "1000 hPa specific humidity", 100000.0, (0.0, 50.0)),
    specific_humidity_spec!("spfh925", "925 hPa specific humidity", 92500.0, (0.0, 50.0)),
    specific_humidity_spec!("spfh850", "850 hPa specific humidity", 85000.0, (0.0, 25.0)),
    specific_humidity_spec!("spfh700", "700 hPa specific humidity", 70000.0, (0.0, 25.0)),
    specific_humidity_spec!("spfh500", "500 hPa specific humidity", 50000.0, (0.0, 5.0)),
    specific_humidity_spec!("spfh300", "300 hPa specific humidity", 30000.0, (0.0, 2.0)),
    specific_humidity_spec!("spfh250", "250 hPa specific humidity", 25000.0, (0.0, 1.0)),
    specific_humidity_spec!("spfh200", "200 hPa specific humidity", 20000.0, (0.0, 1.0)),
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
    theta_e_spec!("thetae1000", "1000 hPa equivalent potential temperature", 100000.0, (235.0, 362.0)),
    theta_e_spec!("thetae925", "925 hPa equivalent potential temperature", 92500.0, (232.0, 359.0)),
    theta_e_spec!("thetae850", "850 hPa equivalent potential temperature", 85000.0, (230.0, 357.0)),
    theta_e_spec!("thetae700", "700 hPa equivalent potential temperature", 70000.0, (235.0, 362.0)),
    theta_e_spec!("thetae500", "500 hPa equivalent potential temperature", 50000.0, (250.0, 377.0)),
    theta_e_spec!("thetae300", "300 hPa equivalent potential temperature", 30000.0, (285.0, 412.0)),
    theta_e_spec!("thetae250", "250 hPa equivalent potential temperature", 25000.0, (295.0, 422.0)),
    theta_e_spec!("thetae200", "200 hPa equivalent potential temperature", 20000.0, (305.0, 432.0)),
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
        isobaric_variable, variable_spec, AerosolIdentity, AEROSOL_VARIABLE_IDS, DUST_CF_BUNDLE_ID,
        DUST_CF_COMPONENT_IDS, DUST_RGB_BUNDLE_ID, DUST_RGB_COMPONENT_IDS, ISOBARIC_FAMILIES,
        ISOBARIC_LEVELS_HPA, OCEAN_VARIABLE_IDS, SATELLITE_CHANNEL_IDS, SATELLITE_VARIABLE_IDS,
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
    /// (wind gust, the cloud covers, CAPE, CIN, visibility, the dew point,
    /// the apparent temperature, precipitable water, the boundary layer
    /// height and the derived precipitation type), held to the Python
    /// encoder and the frontend the same way.
    #[test]
    fn the_surface_registry_matches_the_shared_fixture() {
        let entries = registry("surface-registry.json");
        assert_eq!(
            entries.keys().collect::<Vec<_>>(),
            [
                "gust", "tcdc", "cape", "cin", "vis", "dpt2m", "aptmp2m", "lcdc", "mcdc", "hcdc",
                "pwat", "hpbl", "ptype"
            ],
            "thirteen variables, in the fixture's order"
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
                    assert_eq!(linear.minimum, spec.value_range.0, "{variable_id}");
                }
            }
            // The wave records carry a bitmap, and so does ECMWF's ice
            // thickness (over land); pgrb2's never reaches the fill value.
            let masked = wave || variable_id == "icetk";
            assert_eq!(spec.fill_values, if masked { &[9999.0][..] } else { &[][..] }, "{variable_id}");
            assert_eq!(
                spec.grib2_level_value.is_none(),
                wave || derived,
                "{variable_id}: WAVEWATCH III's surface value"
            );
            assert_eq!(spec.grib_element.is_empty(), derived, "{variable_id}: derived, never matched");
        }
    }

    /// `tests/fixtures/satellite-registry.json`: the satellite channels,
    /// the Dust RGB guns and the DEBRA confidence, held to the Python
    /// encoder the same way — a channel with the `band` block the Himawari
    /// source writes beside the parameter, read off the source table here;
    /// a produced variable (a gun, the confidence) with the `producer` id
    /// registered on it and no band — plus each composite bundle's
    /// component list.
    #[test]
    fn the_satellite_registry_matches_the_shared_fixture() {
        let fixture = registry("satellite-registry.json");
        let entries = fixture["variables"].as_object().expect("variables");
        assert_eq!(
            entries.keys().collect::<Vec<_>>(),
            SATELLITE_VARIABLE_IDS,
            "the channels, then the guns, then the confidence, in the fixture's order"
        );
        assert_eq!(
            fixture["bundles"],
            json!({ DUST_RGB_BUNDLE_ID: DUST_RGB_COMPONENT_IDS, DUST_CF_BUNDLE_ID: DUST_CF_COMPONENT_IDS }),
            "each composite bundle's components"
        );
        let himawari = crate::encode::sources::source_spec("himawari").expect("himawari");
        for (variable_id, entry) in entries {
            let spec = variable_spec(variable_id).unwrap_or_else(|_| panic!("{variable_id}"));
            assert_eq!(json!(spec.label), entry["label"], "{variable_id}");
            assert_eq!(json!(spec.output_unit), entry["unit"], "{variable_id}");
            assert_eq!(
                Value::Object(spec.parameter_metadata()),
                entry["parameter"],
                "{variable_id} GRIB2 identity"
            );
            let produced = DUST_RGB_COMPONENT_IDS.contains(&variable_id.as_str())
                || DUST_CF_COMPONENT_IDS.contains(&variable_id.as_str());
            assert_eq!(SATELLITE_CHANNEL_IDS.contains(&variable_id.as_str()), !produced, "{variable_id}");
            if produced {
                assert_eq!(json!({ "id": spec.producer_id }), entry["producer"], "{variable_id} producer");
                assert!(entry.get("band").is_none(), "{variable_id}: a composite has no band");
                assert!(himawari.bands.iter().all(|(band_id, _)| band_id != variable_id));
            } else {
                assert!(spec.producer_id.is_none(), "{variable_id}: measured, not produced");
                assert!(entry.get("producer").is_none(), "{variable_id}");
                let (_, band) = himawari
                    .bands
                    .iter()
                    .find(|(band_id, _)| band_id == variable_id)
                    .unwrap_or_else(|| panic!("{variable_id}: the himawari source fetches it"));
                assert_eq!(Value::Object(band.metadata()), entry["band"]["himawari"], "{variable_id} band");
            }
            for (profile, key) in [("quality", "quality"), ("compact", "compact"), ("balanced", "quality")] {
                let book = codebook(profile, variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(Value::Object(book.metadata()), entry[key], "{variable_id} {profile} codebook");
                // The cells outside the disk become the bottom of the
                // codebook, so that must be the value the registry says.
                let linear = book.as_linear().expect("linear");
                assert_eq!(linear.minimum, spec.value_range.0, "{variable_id}");
            }
            // Never a GRIB record: nothing to match on.
            assert!(spec.grib_element.is_empty() && spec.grib2_aliases.is_empty(), "{variable_id}");
        }
        // The published grid is the platform's region at the step, past
        // the antimeridian; the six channels are fetched, one is published
        // as a scalar and the two composites beside it.
        assert_eq!(himawari.production_grid, (3000, 3000));
        assert!(himawari.series_file && himawari.observation);
        assert_eq!(himawari.cadence_seconds, Some(600));
        assert_eq!(himawari.input_variable_ids, SATELLITE_CHANNEL_IDS);
        assert_eq!(himawari.bundle_scalar_ids, &["ir104"]);
        assert_eq!(himawari.bundle_composite_ids, &[DUST_RGB_BUNDLE_ID, DUST_CF_BUNDLE_ID]);
    }

    /// `tests/fixtures/aerosol-registry.json`: the GEFS-Aerosols fields,
    /// held to the Python encoder and the frontend the same way — each with
    /// the `aerosol` block beside its parameter, the rest of a template
    /// 4.48 record's identity, and a log1p codebook.
    #[test]
    fn the_aerosol_registry_matches_the_shared_fixture() {
        let entries = registry("aerosol-registry.json");
        assert_eq!(
            entries.keys().collect::<Vec<_>>(),
            AEROSOL_VARIABLE_IDS,
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
            let aerosol = spec.grib2_aerosol.unwrap_or_else(|| panic!("{variable_id}: an aerosol product"));
            assert_eq!(Value::Object(aerosol.metadata()), entry["aerosol"], "{variable_id} aerosol identity");
            // An interval of the missing type carries no limits, and one
            // that is present carries both: the PM records have no
            // wavelength, the optical depths every field.
            let wavelength_missing = aerosol.wavelength_type == AerosolIdentity::MISSING_TYPE;
            assert_eq!(wavelength_missing, variable_id.starts_with("pm"), "{variable_id}");
            assert_eq!(aerosol.wavelength_first.is_none(), wavelength_missing, "{variable_id}");
            assert_eq!(aerosol.wavelength_second.is_none(), wavelength_missing, "{variable_id}");
            assert!(aerosol.size_first.is_some() && aerosol.size_second.is_some(), "{variable_id}");
            // Neither an alias nor an alternate: the aerosol block is the
            // whole of what tells the records apart.
            assert!(spec.grib2_aliases.is_empty() && spec.grib2_alternates.is_empty(), "{variable_id}");
            // Balanced takes the quality codebook: the fields are smooth.
            for (profile, key) in [("quality", "quality"), ("compact", "compact"), ("balanced", "quality")] {
                let book = codebook(profile, &variable_id)
                    .unwrap_or_else(|error| panic!("{variable_id} {profile}: {error}"));
                assert_eq!(Value::Object(book.metadata()), entry[key], "{variable_id} {profile} codebook");
                assert!(book.as_linear().is_none(), "{variable_id}: a log1p codebook");
            }
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
