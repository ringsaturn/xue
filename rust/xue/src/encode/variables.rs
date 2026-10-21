//! One variable as the registry document describes it (`docs/registry.md`
//! §Variables), in GRIB2's own terms.
//!
//! Every variable is identified the way GRIB2 identifies a field: a parameter
//! triple (discipline, category, number) and a fixed surface. That identity is
//! both what record matching uses and what a bundle's schema v3 metadata
//! carries, so there is one description of a variable rather than one per
//! pipeline stage. The table itself is the document's; this module holds
//! the shape it parses into (`registry.rs` builds it).

use std::collections::HashMap;

use serde::Deserialize;
use serde_json::{Map, Value};

use crate::encode::quantize::Codebook;
use crate::encode::registry::{Companion, Conversion, Decimation, GdalRule, Predictor};

/// Standard gravity, the `g` in the water vapour flux `q·V/g` and the
/// divisor of a surface geopotential.
pub const STANDARD_GRAVITY: f64 = 9.80665;

/// Another record the same quantity arrives as at another centre, when it
/// differs from the registered identity in more than the parameter number:
/// another fixed surface (ECMWF's most-unstable CAPE departs from surface
/// type 17, its skin temperature is 0/0/17 with no surface value), a
/// statistical product (its `10fg` is the maximum gust over the interval
/// ending at the frame), a unit GDAL spells differently (its `tcc` is a 0–1
/// fraction). Accepted when matching a record and never written. The
/// `match.alternates` entries of the document.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
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
    /// primary's (`MatchSpec::gdal_unit`); empty for the same.
    pub gdal_unit: String,
}

impl RecordAlternate {
    pub fn triple(&self) -> (u8, u8, u8) {
        (self.discipline, self.category, self.number)
    }
}

/// How a variable's record is found: the fetch-side spellings, the record
/// identities accepted when matching, and the GDAL band rules. The
/// document's `match` block, key for key.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MatchSpec {
    pub grib_element: String,
    pub index_field: String,
    pub excluded_index_phrases: Vec<String>,
    pub index_qualifier: String,
    pub alternate_index_fields: Vec<String>,
    pub ecmwf_param: String,
    pub ecmwf_alternate_params: Vec<String>,
    /// The name Open-Meteo gives this quantity in its open data
    /// (`temperature_2m`), empty when the variable is not fetched from
    /// there. The NetCDF variable inside an `om2nc` series keeps that name
    /// while the file is named by the Xue id
    /// (`observation::series_variable_name`).
    pub open_meteo: String,
    /// The MRMS product name (`MergedReflectivityQC_00.50`), empty for a
    /// variable no MRMS product carries.
    pub mrms_product: String,
    /// Unit string GDAL's GRIB driver reports for this record (it
    /// normalizes temperatures to Celsius).
    pub gdal_unit: String,
    /// Code table 4.10 statistical process required of the record (0
    /// average, 1 accumulation); `None` requires an instantaneous product.
    pub statistical: Option<u8>,
    /// Other `(discipline, category, number)` triples that carry this same
    /// quantity on the same surface, accepted when matching a record and
    /// never written.
    pub aliases: Vec<(u8, u8, u8)>,
    /// Whole other record identities the same quantity arrives as.
    pub alternates: Vec<RecordAlternate>,
    /// How the record is recognised among a GRIB's bands as GDAL describes
    /// them: any one rule accepting is a match. Empty for a field no GRIB
    /// band carries (a derived field, a series-file variable).
    pub gdal: Vec<GdalRule>,
}

/// What a GRIB2 aerosol product carries beyond its parameter: the fields of
/// product definition template 4.48 (and of 4.44 / 4.46, its subsets)
/// between the parameter number and the generating process — the aerosol
/// type (code table 4.233), a size interval and a wavelength interval (each
/// a code table 4.91 type with two limits). A parameter triple on a surface
/// names every one of GEFS-Aerosols' optical thicknesses at once, so for
/// such a record the identity is the parameter block *and* this one,
/// compared whole when matching a record.
///
/// A limit is a `(scaleFactor, scaledValue)` pair, the value being
/// `scaledValue × 10^−scaleFactor` metres, or `None` where GRIB2 encodes it
/// as missing. An interval whose type is 255 (missing) carries no limits:
/// both pairs are `None` here whatever the record wrote. Parsed from the
/// document's `aerosol` block, which the metadata writer copies verbatim.
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
}

/// One variable of the document: the metadata blocks a store carries, the
/// GRIB2 identity derived from them, and the rules the pipeline applies.
#[derive(Debug, Clone)]
pub struct VariableSpec {
    pub id: String,
    /// English label carried in bundle metadata.
    pub label: String,
    /// Unit of the values a bundle's codebook quantizes.
    pub output_unit: String,
    /// `[low, high]` in `output_unit`: the range the pipeline treats as the
    /// field's own; `low` is what a fill value becomes.
    pub value_range: (f64, f64),
    /// Values GDAL hands back where the record carries no data (9999 for a
    /// GRIB2 bitmap), folded to `value_range.0` before anything else touches
    /// the plane. Empty for a field that covers its whole grid.
    pub fill_values: Vec<f64>,
    /// True for a field read at the analysis alone and published as one
    /// frame.
    pub static_: bool,
    pub predictor: Predictor,
    pub decimation: Decimation,
    /// The artifacts this variable's bundle ships beside the store; `video`
    /// further subject to the source's `video` flag.
    pub companions: Vec<Companion>,
    pub conversion: Conversion,
    /// The schema v3 `parameter` block, written verbatim.
    pub parameter: Map<String, Value>,
    pub grib2_discipline: u8,
    pub grib2_category: u8,
    pub grib2_number: u8,
    /// Code table 4.5 type of first fixed surface.
    pub grib2_level_type: u8,
    /// First fixed surface value in that surface's own unit; `None` when the
    /// surface carries none — GRIB2 encodes that as a missing scale factor
    /// and value, so matching on `None` also accepts any.
    pub grib2_level_value: Option<f64>,
    /// For a field an algorithm derived rather than an instrument measured
    /// (a satellite composite), the `producer.id` written beside its
    /// `parameter`; the version is read off the series at build time.
    pub producer_id: Option<String>,
    /// The schema v3 `aerosol` block, written verbatim, and its parse.
    pub aerosol: Option<Map<String, Value>>,
    pub grib2_aerosol: Option<AerosolIdentity>,
    /// One codebook per profile; empty for a variable read and never
    /// published (`tp`, the categorical precipitation flags).
    pub codebooks: HashMap<String, Codebook>,
    pub matching: MatchSpec,
}

impl VariableSpec {
    /// The variable's GRIB2 identity, as the schema v3 metadata block the
    /// document carries.
    pub fn parameter_metadata(&self) -> Map<String, Value> {
        self.parameter.clone()
    }

    /// Whether the variable is published at all: it has a codebook.
    pub fn published(&self) -> bool {
        !self.codebooks.is_empty()
    }
}
