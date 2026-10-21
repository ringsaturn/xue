//! Record discovery through GDAL's own band metadata — the port of
//! `xuebuild/gdal.py`'s `inspect_grib_multi`.
//!
//! This is the reference matcher: the converter runs it once per run on the
//! first file, both to probe wind availability and to cross-check the much
//! faster GRIB2 header index (`gribindex.rs`) that locates the bands every
//! extraction reads. A variable's record is recognised by the `match.gdal`
//! rules of its registry entry (`docs/registry.md` §`match`), any one of
//! which accepting the band; each rule here carries the exact semantics of
//! the `_is_*` helper in `xuebuild/gdal.py` it transcribes.

use std::path::Path;

use regex::Regex;
use time::OffsetDateTime;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::gdalio::{BandInfo, Dataset};
use crate::encode::model::SourceFrame;
use crate::encode::registry::{Conversion, GdalRule, Registry};
use crate::encode::units::temperature_unit;
use crate::encode::variables::{AerosolIdentity, VariableSpec};

pub const SUPPORTED_EXTENSIONS: &[&str] = &["grb", "grb2", "grib2"];

/// The unit rule's arithmetic as a GDAL raster expression over the band
/// `A`, or an error when the rule does not accept `unit`. Only its
/// *identity* matters: the converter compares the string of the
/// header-index frame against the GDAL frame, and nothing writes it.
/// Mirrors `rules.expression`.
pub fn raster_expression(spec: &VariableSpec, unit: &str) -> Result<String> {
    spec.conversion.expression(unit)
}

/// Whether one band carries the given element on the given isobaric surface.
///
/// The surface value is pascals in GRIB2 itself, and that is what GDAL
/// reports: the 850 hPa record comes back as short name `85000-ISBL` with the
/// description `85000[Pa] ISBL="Isobaric surface"`. Hectopascals are accepted
/// too — that is how the level is spelled in an `.idx` phrase and in every
/// human-facing description. Whichever unit, it must name *this* level: a
/// matcher that let 500 also match 1000 (or 50000 Pa also match 100000) would
/// silently pick the wrong plane. Mirrors `_is_isobaric_record`.
fn is_isobaric_record(band: &BandInfo, element: &str, level_hpa: u32) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != element {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    let level_pa = level_hpa * 100;
    if short_name == format!("{level_hpa}-ISBL") || short_name == format!("{level_pa}-ISBL") {
        return true;
    }
    let text = [
        band.item("GRIB_COMMENT"),
        band.item("GRIB_LEVEL"),
        &band.description,
    ]
    .join(" ");
    Regex::new(&format!(
        r"(?i)(?:^|[^0-9])(?:{level_hpa}\s*\[?(?:mb|hpa)\]?|{level_pa}\s*\[?pa\]?)(?:$|[^a-z0-9])"
    ))
    .expect("valid regex")
    .is_match(&text)
}

/// The short name, comment, level and description joined — what every
/// prose rule searches.
fn searchable(band: &BandInfo) -> String {
    [
        band.item("GRIB_SHORT_NAME").to_uppercase().as_str(),
        band.item("GRIB_COMMENT"),
        band.item("GRIB_LEVEL"),
        &band.description,
    ]
    .join(" ")
}

/// The comment, level and description joined, lower-cased: the text the
/// rules that read the raw parameter triple out of the comment search,
/// which never includes the short name.
fn prose(band: &BandInfo) -> String {
    [band.item("GRIB_COMMENT"), band.item("GRIB_LEVEL"), &band.description]
        .join(" ")
        .to_lowercase()
}

/// `GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES` as integers, empty when absent or
/// not integers.
fn assembled_values(band: &BandInfo) -> Vec<i64> {
    band.item("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES")
        .split_whitespace()
        .map(str::parse::<i64>)
        .collect::<std::result::Result<_, _>>()
        .unwrap_or_default()
}

/// `GRIB_DISCIPLINE` as an integer: GDAL writes it `0(Meteorological)`.
fn discipline(band: &BandInfo) -> Option<i64> {
    band.item("GRIB_DISCIPLINE")
        .split_once('(')
        .map_or(band.item("GRIB_DISCIPLINE"), |(number, _)| number)
        .parse::<i64>()
        .ok()
}

/// One element on the ground or water surface. GDAL spells the surface
/// `0-SFC`; the GFS-Wave records carry a surface value of 1 and come out
/// `1-SFC`, so any value on the SFC surface is accepted. The prose test
/// looks for the surface's own name rather than the word "surface", which
/// the isobaric levels the same TMP element is fetched on also carry
/// ("Isobaric surface"). Mirrors `_is_surface_record`.
fn is_surface_record(band: &BandInfo, elements: &[String]) -> bool {
    let element = band.item("GRIB_ELEMENT").to_uppercase();
    if !elements.contains(&element) {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    let text = searchable(band).to_lowercase();
    short_name.ends_with("-SFC") || text.contains("sfc=\"") || text.contains("ground or water surface")
}

/// One element on the `metres` m surface (type 103), which GDAL spells
/// `<metres>-HTGL`. Mirrors `_is_two_metre_record`, `_is_ten_metre_wind`
/// and their siblings, one regex per height.
fn is_height_record(band: &BandInfo, element: &str, metres: u32) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != element {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    if short_name == format!("{metres}-HTGL") || short_name == format!("{metres}-M-HTGL") {
        return true;
    }
    Regex::new(&format!(
        r"(?i)(?:^|[^0-9]){metres}(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground"
    ))
    .expect("valid regex")
    .is_match(&searchable(band))
}

/// One element on the entire atmosphere (surface type 10), which GDAL
/// spells `0-EATM`; the phrase fallback catches drivers that do not, and
/// NCEP's local type 200, which GDAL renders like type 10 and describes in
/// full. Mirrors `_is_entire_atmosphere_record`.
fn is_entire_atmosphere_record(band: &BandInfo, element: &str) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != element {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    short_name == "0-EATM" || searchable(band).to_lowercase().contains("entire atmosphere")
}

/// One of `elements` on GRIB2 surface 101 (mean sea level), which GDAL
/// spells `0-MSL`; the phrase fallback catches drivers that do not. Mirrors
/// `_is_mean_sea_level_pressure`.
fn is_mean_sea_level_record(band: &BandInfo, elements: &[String]) -> bool {
    if !elements.contains(&band.item("GRIB_ELEMENT").to_uppercase()) {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    short_name == "0-MSL" || searchable(band).to_lowercase().contains("mean sea level")
}

/// One cloud cover element on its own layer surface (code table 4.5 types
/// 214 / 224 / 234, which GDAL spells `0-LCY` / `0-MCY` / `0-HCY`), or on a
/// surface of its own named by the layer's phrase in the comment. Mirrors
/// `_is_cloud_layer_record`.
fn is_cloud_layer_record(band: &BandInfo, element: &str, token: &str, phrases: &[String]) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != element {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    let text = searchable(band).to_lowercase();
    short_name == token.to_uppercase() || phrases.iter().any(|phrase| text.contains(phrase.as_str()))
}

/// The elements GDAL names a run-total precipitation record by, lower-cased:
/// ECMWF open data's local 0/1/193 is unknown to its tables (so the comment
/// carries the raw triple), AIFS's WMO 0/1/52 it names TPRATE.
const TOTAL_PRECIPITATION_ELEMENTS: [&str; 4] = ["unknown", "tp", "apcp", "tprate"];

/// A run-total precipitation record on the surface, by one of the elements
/// above and the local triple or the phrase in its comment. Mirrors
/// `_is_total_precipitation`.
fn is_total_precipitation(band: &BandInfo) -> bool {
    if !TOTAL_PRECIPITATION_ELEMENTS.contains(&band.item("GRIB_ELEMENT").to_lowercase().as_str()) {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    let comment = band.item("GRIB_COMMENT");
    let text = prose(band);
    (short_name == "0-SFC" || text.contains("surface"))
        && (comment.contains("cat 1, subcat 193") || text.contains("total precipitation"))
}

/// A centre-local parameter on the ground surface GDAL's tables do not
/// know (ECMWF's `tcc`, 0/6/192): GRIB_ELEMENT is "unknown" and the comment
/// carries the raw triple. Mirrors `_is_ecmwf_total_cloud_cover`.
fn is_local_parameter(band: &BandInfo, category: u8, number: u8) -> bool {
    if band.item("GRIB_ELEMENT").to_lowercase() != "unknown" {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    (short_name == "0-SFC" || prose(band).contains("ground or water surface"))
        && band
            .item("GRIB_COMMENT")
            .contains(&format!("cat {category}, subcat {number}"))
}

/// One element described by a phrase rather than a surface GDAL names
/// (ECMWF's most-unstable CAPE departs from surface type 17, which GDAL
/// describes as such and gives no short name of its own). Mirrors
/// `_is_most_unstable_cape`.
fn is_phrase_record(band: &BandInfo, element: &str, phrases: &[String]) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != element {
        return false;
    }
    let text = prose(band);
    phrases.iter().any(|phrase| text.contains(phrase.as_str()))
}

/// The surface geopotential on the ground surface, which GDAL names `GP`
/// and describes only in the comment. Mirrors the geopotential branch of
/// `_is_orography_record`.
fn is_geopotential_surface_record(band: &BandInfo, elements: &[String]) -> bool {
    if !elements.contains(&band.item("GRIB_ELEMENT").to_uppercase()) {
        return false;
    }
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    let text = searchable(band).to_lowercase();
    (short_name.ends_with("-SFC") || text.contains("ground or water surface")) && text.contains("geopotential")
}

/// One MRMS product under the registry's MRMS identity for the variable —
/// its own (the 3D mosaic's reflectivity levels) or its discipline-209
/// alternate (the composite, the rate): GDAL names the record by the product
/// (its local table for centre 161 — `MergedReflectivityQCComposite`,
/// `PrecipRate`, `MergedReflectivityQC`), on the MRMS-local discipline 209,
/// at a height above mean sea level. The element is not enough: the 3D
/// mosaic's 33 levels share one, so the level must be this identity's too —
/// read off the assembled template (the parameter at indices 0–1, the type
/// of first fixed surface, its scale factor and scaled value at 9–11 of
/// template 4.0), or, where the driver did not assemble it, off the short
/// name GDAL spells it with (`3000-GPML`). Mirrors `_is_mrms_record`.
fn is_mrms_record(band: &BandInfo, spec: &VariableSpec) -> bool {
    let Some(element) = spec.matching.mrms_product.split('_').next().filter(|element| !element.is_empty()) else {
        return false;
    };
    if band.item("GRIB_ELEMENT") != element || band.item("GRIB_DISCIPLINE") != "209" {
        return false;
    }
    let (category, number, level_type, level_value) = spec
        .matching
        .alternates
        .iter()
        .find(|alternate| alternate.discipline == 209)
        .map_or(
            (spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value),
            |alternate| (alternate.category, alternate.number, alternate.level_type, alternate.level_value),
        );
    let values = assembled_values(band);
    if band.item("GRIB_PDS_PDTN") == "0" && values.len() >= 12 {
        if (values[0], values[1], values[9]) != (i64::from(category), i64::from(number), i64::from(level_type)) {
            return false;
        }
        let Some(expected) = level_value else {
            return true;
        };
        let (scale, scaled_value) = (values[10], values[11]);
        return scaled_value != i64::from(u32::MAX) && scaled_value as f64 / 10f64.powi(scale as i32) == expected;
    }
    let Some(expected) = level_value.filter(|_| level_type == 102) else {
        return true;
    };
    band.item("GRIB_SHORT_NAME").to_uppercase() == format!("{expected}-GPML")
}

/// One GRIB2 aerosol product (template 4.48) under the registry's parameter
/// *and* aerosol identity. The driver's tables name the fields by element
/// (`AOTK`, `PMTF`) and surface, which a dozen of GEFS-Aerosols' records
/// share; what tells them apart is in the template itself, which GDAL
/// exposes whole as `GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES` beside
/// `GRIB_PDS_PDTN`. A record of another template, or one the driver did not
/// assemble, is not this record. Mirrors `_is_aerosol_record`.
fn is_aerosol_record(band: &BandInfo, spec: &VariableSpec) -> bool {
    let Some(identity) = spec.grib2_aerosol else {
        return false;
    };
    if band.item("GRIB_PDS_PDTN") != "48" {
        return false;
    }
    let Ok(values) = band
        .item("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES")
        .split_whitespace()
        .map(str::parse::<i64>)
        .collect::<std::result::Result<Vec<_>, _>>()
    else {
        return false;
    };
    let Some(discipline) = discipline(band) else {
        return false;
    };
    if values.len() < 23 {
        return false;
    }
    if (discipline, values[0], values[1])
        != (
            i64::from(spec.grib2_discipline),
            i64::from(spec.grib2_category),
            i64::from(spec.grib2_number),
        )
    {
        return false;
    }
    if values[20] != i64::from(spec.grib2_level_type) {
        return false;
    }
    if let Some(expected) = spec.grib2_level_value {
        let (scale, scaled_value) = (values[21], values[22]);
        if scaled_value == i64::from(u32::MAX) || scaled_value as f64 / 10f64.powi(scale as i32) != expected {
            return false;
        }
    }
    assembled_aerosol(&values) == identity
}

/// The aerosol identity of template 4.48's assembled values: type at index
/// 2, the size interval at 3–7, the wavelength interval at 8–12. An
/// interval of the missing type carries no limits, as the header index
/// reads it (`gribindex.rs`). Mirrors `_assembled_aerosol`.
fn assembled_aerosol(values: &[i64]) -> AerosolIdentity {
    let missing = i64::from(AerosolIdentity::MISSING_TYPE);
    let limit = |index: usize| -> Option<(i8, u32)> {
        let (scale, scaled_value) = (values[index], values[index + 1]);
        (scaled_value != i64::from(u32::MAX)).then_some((scale as i8, scaled_value as u32))
    };
    let (size_type, wavelength_type) = (values[3], values[8]);
    AerosolIdentity {
        aerosol_type: values[2] as u16,
        size_type: size_type as u8,
        size_first: (size_type != missing).then(|| limit(4)).flatten(),
        size_second: (size_type != missing).then(|| limit(6)).flatten(),
        wavelength_type: wavelength_type as u8,
        wavelength_first: (wavelength_type != missing).then(|| limit(9)).flatten(),
        wavelength_second: (wavelength_type != missing).then(|| limit(11)).flatten(),
    }
}

/// A record under the registry's identity or an alternate, read off the
/// assembled template 4.0 values (the type of first fixed surface, its
/// scale factor and scaled value at indices 9–11) beside the discipline: a
/// soil layer, whose element and short name do not name the layer in a form
/// two centres share. A record of another template is not one. Mirrors
/// `_is_soil_record`.
fn is_template_record(band: &BandInfo, spec: &VariableSpec) -> bool {
    if band.item("GRIB_PDS_PDTN") != "0" {
        return false;
    }
    let Ok(values) = band
        .item("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES")
        .split_whitespace()
        .map(str::parse::<i64>)
        .collect::<std::result::Result<Vec<_>, _>>()
    else {
        return false;
    };
    let Some(discipline) = discipline(band) else {
        return false;
    };
    if values.len() < 12 {
        return false;
    }
    let identities = std::iter::once((
        spec.grib2_discipline,
        spec.grib2_category,
        spec.grib2_number,
        spec.grib2_level_type,
        spec.grib2_level_value,
    ))
    .chain(spec.matching.alternates.iter().map(|alternate| {
        (
            alternate.discipline,
            alternate.category,
            alternate.number,
            alternate.level_type,
            alternate.level_value,
        )
    }));
    let (scale, scaled_value) = (values[10], values[11]);
    for (identity_discipline, category, number, level_type, level_value) in identities {
        if (discipline, values[0], values[1], values[9])
            != (
                i64::from(identity_discipline),
                i64::from(category),
                i64::from(number),
                i64::from(level_type),
            )
        {
            continue;
        }
        let Some(expected) = level_value else {
            return true;
        };
        if scaled_value != i64::from(u32::MAX) && scaled_value as f64 / 10f64.powi(scale as i32) == expected {
            return true;
        }
    }
    false
}

/// One rank of an ordered sequence (code table 4.5 type 241, local to
/// NCEP): a GFS-Wave swell partition, under the registry's parameter and
/// rank. The three partitions share element, parameter and grid and differ
/// in the surface value alone, which GDAL spells `1-RESERVED(241)`, so the
/// rank is read off the assembled template 4.0 values beside the
/// discipline and parameter; where the driver did not assemble the
/// template, the short name is the fallback, rank included. Mirrors
/// `_is_ordered_sequence_record`.
fn is_rank_record(band: &BandInfo, spec: &VariableSpec) -> bool {
    if band.item("GRIB_ELEMENT").to_uppercase() != spec.matching.grib_element {
        return false;
    }
    let rank = spec.grib2_level_value.unwrap_or(f64::NAN);
    let values = band
        .item("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES")
        .split_whitespace()
        .map(str::parse::<i64>)
        .collect::<std::result::Result<Vec<_>, _>>();
    let (values, discipline) = match (values, discipline(band)) {
        (Ok(values), Some(discipline)) => (values, discipline),
        _ => (Vec::new(), -1),
    };
    if band.item("GRIB_PDS_PDTN") == "0" && values.len() >= 12 {
        if (discipline, values[0], values[1], values[9])
            != (
                i64::from(spec.grib2_discipline),
                i64::from(spec.grib2_category),
                i64::from(spec.grib2_number),
                i64::from(spec.grib2_level_type),
            )
        {
            return false;
        }
        let (scale, scaled_value) = (values[10], values[11]);
        return scaled_value != i64::from(u32::MAX) && scaled_value as f64 / 10f64.powi(scale as i32) == rank;
    }
    band.item("GRIB_SHORT_NAME").to_uppercase() == format!("{rank}-RESERVED({})", spec.grib2_level_type)
}

/// Whether one rule accepts the band for `spec`.
fn rule_matches(rule: &GdalRule, spec: &VariableSpec, band: &BandInfo) -> bool {
    match rule {
        GdalRule::Surface { elements } => is_surface_record(band, elements),
        GdalRule::Height { element, metres } => is_height_record(band, element, *metres),
        GdalRule::EntireAtmosphere { element } => is_entire_atmosphere_record(band, element),
        GdalRule::MeanSeaLevel { elements } => is_mean_sea_level_record(band, elements),
        GdalRule::Isobaric { element, hpa } => is_isobaric_record(band, element, *hpa),
        GdalRule::CloudLayer {
            element,
            short_name,
            phrases,
        } => is_cloud_layer_record(band, element, short_name, phrases),
        GdalRule::Template => is_template_record(band, spec),
        GdalRule::TemplateAerosol => is_aerosol_record(band, spec),
        GdalRule::TemplateRank => is_rank_record(band, spec),
        GdalRule::TemplateMrms => is_mrms_record(band, spec),
        GdalRule::TotalPrecipitation => is_total_precipitation(band),
        GdalRule::LocalParameter { category, number } => is_local_parameter(band, *category, *number),
        GdalRule::Phrase { element, phrases } => is_phrase_record(band, element, phrases),
        GdalRule::GeopotentialSurface { elements } => is_geopotential_surface_record(band, elements),
    }
}

/// Whether the band is the variable's record: any one of its `match.gdal`
/// rules accepting. A field no GRIB band carries (a derived field, a
/// series-file variable) has no rules and matches nothing. Mirrors
/// `_band_matches`.
pub(crate) fn band_matches(spec: &VariableSpec, band: &BandInfo) -> bool {
    spec.matching
        .gdal
        .iter()
        .any(|rule| rule_matches(rule, spec, band))
}

fn timestamp(band: &BandInfo, key: &str) -> Option<OffsetDateTime> {
    let raw: f64 = band.metadata.get(key)?.parse().ok()?;
    OffsetDateTime::from_unix_timestamp(raw as i64).ok()
}

fn frame_from_band(path: &Path, spec: &VariableSpec, band: &BandInfo) -> Result<SourceFrame> {
    let raw_unit = if band.item("GRIB_UNIT").is_empty() {
        band.item("GRIB_COMMENT")
            .rsplit_once('[')
            .map_or("", |(_, tail)| tail)
            .trim_end_matches(']')
            .to_string()
    } else {
        band.item("GRIB_UNIT").to_string()
    };
    // A temperature's unit is carried as the one letter its rule reads it
    // through; any other as GDAL spelled it, brackets off.
    let unit = if matches!(spec.conversion, Conversion::Celsius | Conversion::SoilTemperature) {
        temperature_unit(&raw_unit)?.to_string()
    } else {
        raw_unit
            .trim()
            .trim_matches(|character| "[]".contains(character))
            .to_string()
    };
    raster_expression(spec, &unit)?;
    let valid_time = timestamp(band, "GRIB_VALID_TIME");
    let run_time = timestamp(band, "GRIB_REF_TIME").or_else(|| {
        let forecast: f64 = band.metadata.get("GRIB_FORECAST_SECONDS")?.parse().ok()?;
        OffsetDateTime::from_unix_timestamp(valid_time?.unix_timestamp() - forecast as i64).ok()
    });
    let (Some(run_time), Some(valid_time)) = (run_time, valid_time) else {
        return Err(EncodeError::conversion(format!(
            "missing GRIB_REF_TIME or GRIB_VALID_TIME metadata in {}",
            path.display()
        )));
    };
    let delta = valid_time.unix_timestamp() - run_time.unix_timestamp();
    if delta < 0 || delta % 3600 != 0 {
        return Err(EncodeError::conversion(format!(
            "forecast time is not a non-negative whole hour in {}",
            path.display()
        )));
    }
    Ok(SourceFrame {
        path: path.to_path_buf(),
        band: band.number,
        variable_id: spec.id.clone(),
        run_time,
        valid_time,
        lead_seconds: delta,
        unit,
    })
}

/// Locate every requested variable in one GDAL pass over the file.
///
/// Variables in `optional_ids` may be absent (they are simply omitted from the
/// result); more than one match is still an error for every variable.
pub fn inspect_grib_multi(
    path: &Path,
    variable_ids: &[&str],
    optional_ids: &[&str],
    registry: &Registry,
) -> Result<Vec<(String, SourceFrame)>> {
    if !path.is_file() {
        return Err(EncodeError::conversion(format!(
            "GRIB input does not exist: {}",
            path.display()
        )));
    }
    let dataset = Dataset::open(path)?;
    let bands = dataset.bands()?;
    let mut frames = Vec::new();
    for variable_id in variable_ids {
        let spec = registry.variable(variable_id)?;
        let matches: Vec<&BandInfo> = bands
            .iter()
            .filter(|band| band_matches(spec, band))
            .collect();
        if matches.is_empty() && optional_ids.contains(variable_id) {
            continue;
        }
        if matches.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "expected exactly one {} band for {variable_id} in {}, found {}",
                spec.matching.grib_element,
                path.display(),
                matches.len()
            )));
        }
        frames.push((
            (*variable_id).to_string(),
            frame_from_band(path, spec, matches[0])?,
        ));
    }
    Ok(frames)
}

#[cfg(test)]
mod tests {
    use super::{band_matches, raster_expression, BandInfo};
    use crate::encode::registry::Registry;
    use std::collections::HashMap;

    /// A band as GDAL's GRIB driver describes it.
    fn band(items: &[(&str, &str)], description: &str) -> BandInfo {
        let metadata: HashMap<String, String> = items
            .iter()
            .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
            .collect();
        BandInfo {
            number: 1,
            description: description.to_string(),
            metadata,
            unit: String::new(),
            scale: 1.0,
            offset: 0.0,
            nodata: None,
        }
    }

    /// One level of the MRMS 3D mosaic as GDAL's GRIB driver describes it.
    fn mrms_level(metres: u32) -> BandInfo {
        band(
            &[
                ("GRIB_ELEMENT", "MergedReflectivityQC"),
                ("GRIB_SHORT_NAME", &format!("{metres}-GPML")),
                ("GRIB_DISCIPLINE", "209"),
                ("GRIB_UNIT", "[dBZ]"),
                ("GRIB_PDS_PDTN", "0"),
                (
                    "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES",
                    &format!("9 0 8 0 97 0 0 0 0 102 0 {metres} 255 1 0"),
                ),
            ],
            "",
        )
    }

    fn matches(variable_id: &str, band: &BandInfo) -> bool {
        band_matches(Registry::embedded().variable(variable_id).unwrap(), band)
    }

    /// Every level is the same element on discipline 209: the level read
    /// off the assembled template is what tells them apart, and a level is
    /// never taken for its neighbour (the composite's matcher is
    /// level-blind and must stay out of the family).
    #[test]
    fn an_mrms_level_matches_its_own_variable_only() {
        let level = mrms_level(3000);
        assert!(matches("refl3000", &level));
        for other in ["refl500", "refl3500"] {
            assert!(!matches(other, &level), "{other}");
        }
        assert!(!matches("cref", &level));
        // Without the assembled values, the short name says the same.
        let mut bare = mrms_level(500);
        bare.metadata.remove("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES");
        assert!(matches("refl500", &bare));
        assert!(!matches("refl750", &bare));
        // Another surface type at the same value is not the level.
        let mut wrong_surface = mrms_level(3000);
        wrong_surface.metadata.insert(
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES".into(),
            "9 0 8 0 97 0 0 0 0 103 0 3000 255 1 0".into(),
        );
        assert!(!matches("refl3000", &wrong_surface));
        // Nor is another discipline.
        let mut wrong_discipline = mrms_level(3000);
        wrong_discipline.metadata.insert("GRIB_DISCIPLINE".into(), "0".into());
        assert!(!matches("refl3000", &wrong_discipline));
        // The composite and the rate match their discipline-209 alternates.
        let composite = band(
            &[
                ("GRIB_ELEMENT", "MergedReflectivityQCComposite"),
                ("GRIB_SHORT_NAME", "500-GPML"),
                ("GRIB_DISCIPLINE", "209"),
                ("GRIB_PDS_PDTN", "0"),
                ("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "10 0 8 0 97 0 0 0 0 102 0 500 255 1 0"),
            ],
            "",
        );
        assert!(matches("cref", &composite) && !matches("refl500", &composite));
    }

    /// One band of each rule, as GDAL spells the records the sources carry.
    #[test]
    fn each_gdal_rule_accepts_the_band_it_transcribes() {
        // surface: pgrb2's instantaneous rate, the wave records' `1-SFC`,
        // ECMWF's SKINT spelling of the skin temperature.
        let prate = band(&[("GRIB_ELEMENT", "PRATE"), ("GRIB_SHORT_NAME", "0-SFC")], "0[-] SFC=\"Ground or water surface\"");
        assert!(matches("prate", &prate) && !matches("prate_ave", &band(&[("GRIB_ELEMENT", "PRATE"), ("GRIB_SHORT_NAME", "85000-ISBL")], "")));
        assert!(matches("htsgw", &band(&[("GRIB_ELEMENT", "HTSGW"), ("GRIB_SHORT_NAME", "1-SFC")], "")));
        assert!(matches("tmpsfc", &band(&[("GRIB_ELEMENT", "SKINT")], "0[-] SFC=\"Ground or water surface\"")));
        assert!(!matches("tmpsfc", &band(&[("GRIB_ELEMENT", "TMP"), ("GRIB_SHORT_NAME", "85000-ISBL")], "Isobaric surface")));
        // height: the 2 m, 10 m, 80 m and 100 m surfaces by short name or
        // phrase, each refusing the others.
        assert!(matches("tmp2m", &band(&[("GRIB_ELEMENT", "TMP"), ("GRIB_SHORT_NAME", "2-HTGL")], "")));
        assert!(matches("tmp2m", &band(&[("GRIB_ELEMENT", "TMP")], "2.0 m above ground")));
        assert!(!matches("tmp2m", &band(&[("GRIB_ELEMENT", "TMP")], "12 m above ground")));
        assert!(matches("ugrd10m", &band(&[("GRIB_ELEMENT", "UGRD"), ("GRIB_SHORT_NAME", "10-M-HTGL")], "")));
        assert!(!matches("ugrd10m", &band(&[("GRIB_ELEMENT", "UGRD"), ("GRIB_SHORT_NAME", "100-HTGL")], "")));
        assert!(matches("ugrd80m", &band(&[("GRIB_ELEMENT", "UGRD"), ("GRIB_SHORT_NAME", "80-HTGL")], "")));
        assert!(matches("vgrd100m", &band(&[("GRIB_ELEMENT", "VGRD")], "100 meters above ground")));
        // The gust: pgrb2's on the surface, ECMWF's on the 10 m surface.
        assert!(matches("gust", &band(&[("GRIB_ELEMENT", "GUST"), ("GRIB_SHORT_NAME", "0-SFC")], "")));
        assert!(matches("gust", &band(&[("GRIB_ELEMENT", "GUST"), ("GRIB_SHORT_NAME", "10-HTGL")], "")));
        // entire-atmosphere and the cloud layers.
        assert!(matches("tcdc", &band(&[("GRIB_ELEMENT", "TCDC"), ("GRIB_SHORT_NAME", "0-EATM")], "")));
        assert!(matches("pwat", &band(&[("GRIB_ELEMENT", "PWAT")], "entire atmosphere (considered as a single layer)")));
        assert!(matches("lcdc", &band(&[("GRIB_ELEMENT", "LCDC"), ("GRIB_SHORT_NAME", "0-LCY")], "")));
        assert!(matches("mcdc", &band(&[("GRIB_ELEMENT", "MCDC"), ("GRIB_SHORT_NAME", "80000-45000-ISBL")], "Medium cloud cover")));
        assert!(!matches("hcdc", &band(&[("GRIB_ELEMENT", "HCDC"), ("GRIB_SHORT_NAME", "0-LCY")], "")));
        // local-parameter: ECMWF's tcc, which GDAL does not know.
        let tcc = band(&[("GRIB_ELEMENT", "unknown"), ("GRIB_SHORT_NAME", "0-SFC"), ("GRIB_COMMENT", "unknown [-] (disc 0, cat 6, subcat 192)")], "");
        assert!(matches("tcdc", &tcc));
        // mean-sea-level: NCEP's PRMSL, ECMWF's PRES, HRRR's MSLMA.
        for element in ["PRMSL", "PRES", "MSLMA"] {
            assert!(matches("prmsl", &band(&[("GRIB_ELEMENT", element), ("GRIB_SHORT_NAME", "0-MSL")], "")), "{element}");
        }
        assert!(!matches("prmsl", &band(&[("GRIB_ELEMENT", "PRES"), ("GRIB_SHORT_NAME", "0-SFC")], "")));
        // isobaric: pascals or hectopascals, this level and no other.
        assert!(matches("rh850", &band(&[("GRIB_ELEMENT", "RH"), ("GRIB_SHORT_NAME", "85000-ISBL")], "")));
        assert!(matches("rh850", &band(&[("GRIB_ELEMENT", "RH")], "850 mb")));
        assert!(!matches("rh500", &band(&[("GRIB_ELEMENT", "RH"), ("GRIB_SHORT_NAME", "100000-ISBL")], "1000[Pa] ISBL")));
        // total-precipitation: ECMWF's local triple and AIFS's TPRATE.
        assert!(matches("tp", &band(&[("GRIB_ELEMENT", "unknown"), ("GRIB_SHORT_NAME", "0-SFC"), ("GRIB_COMMENT", "unknown [-] (disc 0, cat 1, subcat 193)")], "")));
        assert!(matches("tp", &band(&[("GRIB_ELEMENT", "TPRATE"), ("GRIB_COMMENT", "Total precipitation rate")], "surface")));
        assert!(!matches("tp", &band(&[("GRIB_ELEMENT", "PRATE"), ("GRIB_SHORT_NAME", "0-SFC")], "")));
        // phrase: the most-unstable CAPE.
        assert!(matches("cape", &band(&[("GRIB_ELEMENT", "CAPE"), ("GRIB_COMMENT", "Most unstable CAPE")], "")));
        assert!(!matches("cape", &band(&[("GRIB_ELEMENT", "CAPE"), ("GRIB_SHORT_NAME", "9000-18000-SPDL")], "")));
        // geopotential-surface: ECMWF's z, and NCEP's HGT by the surface rule.
        assert!(matches("orog", &band(&[("GRIB_ELEMENT", "GP"), ("GRIB_SHORT_NAME", "0-SFC"), ("GRIB_COMMENT", "Geopotential [(m^2)/(s^2)]")], "")));
        assert!(matches("orog", &band(&[("GRIB_ELEMENT", "HGT"), ("GRIB_SHORT_NAME", "0-SFC")], "")));
        assert!(!matches("orog", &band(&[("GRIB_ELEMENT", "HGT"), ("GRIB_SHORT_NAME", "85000-ISBL")], "")));
        // template: a soil layer by its assembled surface, under either
        // centre's identity.
        let tsoil = band(&[("GRIB_ELEMENT", "TSOIL"), ("GRIB_DISCIPLINE", "2(Land Surface)"), ("GRIB_PDS_PDTN", "0"), ("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "0 2 2 0 81 0 0 1 0 106 0 0 106 1 1")], "");
        assert!(matches("tsoil1", &tsoil) && !matches("tsoil2", &tsoil));
        let sot = band(&[("GRIB_ELEMENT", "SOILTMP"), ("GRIB_DISCIPLINE", "2"), ("GRIB_PDS_PDTN", "0"), ("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "3 18 0 0 0 0 1 0 0 151 0 0 151 0 1")], "");
        assert!(matches("tsoil1", &sot) && !matches("soilw1", &sot));
        // template-rank: a swell partition by its rank, with the short-name
        // fallback.
        let swell = band(&[("GRIB_ELEMENT", "SWELL"), ("GRIB_DISCIPLINE", "10"), ("GRIB_PDS_PDTN", "0"), ("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "0 8 0 0 0 0 1 0 0 241 0 1 255 0 0")], "");
        assert!(matches("swhgt", &swell));
        assert!(matches("swhgt", &band(&[("GRIB_ELEMENT", "SWELL"), ("GRIB_SHORT_NAME", "1-RESERVED(241)")], "")));
        assert!(!matches("swhgt", &band(&[("GRIB_ELEMENT", "SWELL"), ("GRIB_SHORT_NAME", "2-RESERVED(241)")], "")));
        assert!(!matches("swhgt", &band(&[("GRIB_ELEMENT", "HTSGW"), ("GRIB_SHORT_NAME", "1-RESERVED(241)")], "")));
        // template-aerosol: the identity whole, type and intervals included.
        let aod = band(&[("GRIB_ELEMENT", "AOTK"), ("GRIB_DISCIPLINE", "0(Meteorological)"), ("GRIB_PDS_PDTN", "48"), ("GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES", "20 102 62001 0 6 20 0 0 7 9 545 9 565 2 0 0 1 0 0 0 10 0 0 255 0 0")], "");
        assert!(matches("aoddust", &aod) && !matches("aod", &aod) && !matches("aodsalt", &aod));
        assert!(!matches("tcdc", &aod));
        // A derived field has no rule and matches no band; that is a miss,
        // not an error.
        assert!(!matches("ptype", &prate));
        assert!(!matches("thetae850", &prate));
        assert!(!matches("ir039", &prate));
    }

    #[test]
    fn the_raster_expression_is_the_rule_s_arithmetic_alone() {
        let registry = Registry::embedded();
        let expression = |variable_id: &str, unit: &str| raster_expression(registry.variable(variable_id).unwrap(), unit);
        assert_eq!(expression("refl19000", "dBZ").unwrap(), expression("cref", "dBZ").unwrap());
        assert!(expression("refl500", "mm/h").is_err());
        assert_eq!(expression("tmp2m", "K").unwrap(), "A-273.15");
        assert_eq!(expression("tmp850", "[C]").unwrap(), "A");
        assert_eq!(expression("prate", "kg/(m^2 s)").unwrap(), "A*3600");
        assert_eq!(expression("prate", "mm/hr").unwrap(), "A");
        assert_eq!(expression("tp", "-").unwrap(), "A*1000");
        assert_eq!(expression("tp", "").unwrap(), "A*1000");
        assert_eq!(expression("tp", "kg/(m^2*s)").unwrap(), "A");
        assert_eq!(expression("prmsl", "Pa").unwrap(), "A/100");
        assert!(expression("prmsl", "hPa").is_err());
        assert_eq!(expression("vis", "m").unwrap(), "A/1000");
        assert_eq!(expression("spfh850", "kg/kg").unwrap(), "A*1000");
        assert_eq!(expression("icec", "Proportion").unwrap(), "A*100");
        assert_eq!(expression("orog", "(m^2)/(s^2)").unwrap(), "A/9.80665");
        assert_eq!(expression("dirpw", "Degree true").unwrap(), "A");
        assert_eq!(expression("ir039", "anything").unwrap(), "A");
        assert!(expression("ugrd10m", "knots").is_err());
    }
}
