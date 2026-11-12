//! GRIB2 record discovery through [grib-rs](https://github.com/noritada/grib-rs)
//! — the port of `xuebuild/grib2.py`.
//!
//! Reads only the identification and product-definition sections of every
//! message, which is what locates each variable's band number and its
//! reference/valid time without decoding any data. `inspect.rs` (real GDAL
//! band metadata) stays the reference: the converter cross-checks the first
//! file of every run against it and falls back to it wholesale when a run's
//! files do not parse here.

use std::fs::File;
use std::io::BufReader;
use std::path::Path;

use grib::{Code, ForecastTime};
use time::{Date, Duration, Month, OffsetDateTime, Time, UtcOffset};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::inspect::ENSEMBLE_TEMPLATES;
use crate::encode::model::SourceFrame;
use crate::encode::registry::{GdalRule, Registry};
use crate::encode::variables::{AerosolIdentity, EnsembleIdentity, VariableSpec};

/// Product definition templates whose octets 10-34 share the 4.0 layout and
/// that we know how to time-stamp. 4.8 adds the statistical interval. 4.48
/// (aerosol optical properties: GEFS-Aerosols' whole output) inserts the
/// aerosol type and two intervals — 24 octets — between the parameter
/// number and the generating process; grib-rs knows where its time and
/// surface octets sit, and the aerosol octets are read here. 4.44 and 4.46,
/// the aerosol templates without the optical fields, are its subsets and
/// are not read by any source yet. The ensemble products (4.2, 4.5, 4.6 and
/// their interval forms 4.12, 4.9, 4.10) carry their identity after octet
/// 34 and are told apart by it (`ensemble_identity`).
const INSTANTANEOUS_TEMPLATES: &[u16] = &[0, 1, 2, 5, 6];
const STATISTICAL_TEMPLATES: &[u16] = &[8, 9, 10, 11, 12];
const AEROSOL_TEMPLATES: &[u16] = &[48];
const MISSING_U32: u32 = 0xFFFF_FFFF;
/// Section 4 payload starts at octet 6, so the template's first octet (10)
/// sits at payload index 4.
const START_OF_PROD_TEMPLATE: usize = 4;

/// Identity of one GRIB2 message, 1-based band order.
#[derive(Debug, Clone)]
pub struct MessageInfo {
    pub band: usize,
    pub discipline: u8,
    pub parameter_category: u8,
    pub parameter_number: u8,
    pub level_type: u8,
    pub level_value: Option<f64>,
    pub reference_time: OffsetDateTime,
    pub valid_time: OffsetDateTime,
    /// Code table 4.10 process (0 average, 1 accumulation) for statistical
    /// templates (the 4.8 family); `None` for instantaneous products.
    pub statistical_process: Option<u8>,
    /// The aerosol type and intervals of an aerosol product (template
    /// 4.48); `None` for every other template, which no aerosol variable
    /// matches.
    pub aerosol: Option<AerosolIdentity>,
    /// The ensemble identity of an ensemble product (`ENSEMBLE_TEMPLATES`),
    /// and whether it is the template over a time interval; `None` for a
    /// single forecast.
    pub ensemble: Option<(EnsembleIdentity, bool)>,
    /// A statistical template's time ranges: their count, and the first's
    /// unit (code table 4.4) and length in it.
    pub time_range: Option<(u8, u8, u32)>,
}

/// Octet 35 on, per template: where the end of the overall interval sits
/// in a statistical template — after the fields a template inserts beside
/// 4.8's layout (4.11's three ensemble octets, 4.12's two, 4.10's
/// percentile, 4.9's thirteen probability octets).
fn interval_end_octet(template: u16) -> u16 {
    match template {
        9 => 48,
        10 => 36,
        11 => 38,
        12 => 37,
        _ => 35,
    }
}

/// A GRIB2 sign-and-magnitude octet, as a scale factor is written.
fn signed_octet(octet: u8) -> i8 {
    let magnitude = (octet & 0x7F) as i8;
    if octet & 0x80 != 0 {
        -magnitude
    } else {
        magnitude
    }
}

/// The ensemble identity of an ensemble product's section 4 payload
/// (payload index = octet − 6): the derived forecast (4.2, 4.12) or the
/// percentile (4.6, 4.10) at octet 35; a probability's (4.5, 4.9) type at
/// 37 and its limits at 38–42 and 43–47, a limit whose scale factor or
/// scaled value is all ones missing, normalized by the type as the GDAL
/// matcher reads them (`inspect::assembled_ensemble`).
fn ensemble_identity(template: u16, payload: &[u8]) -> Option<EnsembleIdentity> {
    let at = |octet: usize| payload.get(octet - 6).copied();
    match template {
        2 | 12 => Some(EnsembleIdentity::Derived(at(35)?)),
        6 | 10 => Some(EnsembleIdentity::Percentile(at(35)?)),
        5 | 9 => {
            let limit = |octet: usize| -> Option<Option<(i8, u32)>> {
                let scale = at(octet)?;
                let value = u32::from_be_bytes([at(octet + 1)?, at(octet + 2)?, at(octet + 3)?, at(octet + 4)?]);
                Some((scale != 0xFF && value != MISSING_U32).then(|| (signed_octet(scale), value)))
            };
            Some(EnsembleIdentity::probability(at(37)?, limit(38)?, limit(43)?))
        }
        _ => None,
    }
}

/// One `(scaleFactor, scaledValue)` limit of an aerosol interval at
/// `offset` into the section payload, `None` when the value is the missing
/// pattern. Mirrors `_parse_limit` in `xuebuild/grib2.py`.
fn parse_limit(payload: &[u8], offset: usize) -> Option<(i8, u32)> {
    let scale = payload[offset] as i8;
    let scaled_value = u32::from_be_bytes([
        payload[offset + 1],
        payload[offset + 2],
        payload[offset + 3],
        payload[offset + 4],
    ]);
    (scaled_value != MISSING_U32).then_some((scale, scaled_value))
}

/// The aerosol fields of a template 4.48 payload: octets 12-35, the type
/// then the size and wavelength intervals, each a code table 4.91 type and
/// two limits. An interval of the missing type carries no limits whatever
/// the octets hold (docs/format.md §"Band, Producer and Aerosol"). Mirrors
/// `_parse_aerosol` in `xuebuild/grib2.py`; the payload starts at octet 6,
/// so octet `n` is index `n - 6`.
fn parse_aerosol(payload: &[u8]) -> AerosolIdentity {
    let at = |octet: usize| octet - 6;
    let aerosol_type = u16::from_be_bytes([payload[at(12)], payload[at(13)]]);
    let size_type = payload[at(14)];
    let wavelength_type = payload[at(25)];
    let missing = AerosolIdentity::MISSING_TYPE;
    AerosolIdentity {
        aerosol_type,
        size_type,
        size_first: (size_type != missing).then(|| parse_limit(payload, at(15))).flatten(),
        size_second: (size_type != missing).then(|| parse_limit(payload, at(20))).flatten(),
        wavelength_type,
        wavelength_first: (wavelength_type != missing).then(|| parse_limit(payload, at(26))).flatten(),
        wavelength_second: (wavelength_type != missing).then(|| parse_limit(payload, at(31))).flatten(),
    }
}

/// Code table 4.4 (indicator of unit of time range) for the units our products
/// use; anything else is rejected and triggers the GDAL fallback.
fn time_unit_seconds(code: u8) -> Option<i64> {
    Some(match code {
        0 => 60,
        1 => 3600,
        2 => 86_400,
        10 => 3 * 3600,
        11 => 6 * 3600,
        12 => 12 * 3600,
        13 => 1,
        _ => return None,
    })
}

fn build_time(
    year: u16,
    month: u8,
    day: u8,
    hour: u8,
    minute: u8,
    second: u8,
    path: &Path,
) -> Result<OffsetDateTime> {
    let invalid = || {
        EncodeError::conversion(format!(
            "invalid GRIB2 timestamp in {}: {year}-{month}-{day} {hour}:{minute}:{second}",
            path.display()
        ))
    };
    let month = Month::try_from(month).map_err(|_| invalid())?;
    let date = Date::from_calendar_date(i32::from(year), month, day).map_err(|_| invalid())?;
    let clock = Time::from_hms(hour, minute, second).map_err(|_| invalid())?;
    Ok(date.with_time(clock).assume_offset(UtcOffset::UTC))
}

/// Band-ordered identities of every message in a GRIB2 file.
pub fn index_messages(path: &Path) -> Result<Vec<MessageInfo>> {
    let file = File::open(path).map_err(|error| {
        EncodeError::conversion(format!("cannot read GRIB input {}: {error}", path.display()))
    })?;
    let grib2 = grib::from_reader(BufReader::new(file)).map_err(|error| {
        EncodeError::conversion(format!("cannot parse GRIB2 in {}: {error}", path.display()))
    })?;

    let mut messages = Vec::new();
    for (band, (_index, submessage)) in grib2.iter().enumerate() {
        let discipline = submessage.indicator().discipline;
        let reference = submessage.identification().ref_time_unchecked();
        let reference_time = build_time(
            reference.year,
            reference.month,
            reference.day,
            reference.hour,
            reference.minute,
            reference.second,
            path,
        )?;

        let product = submessage.prod_def();
        let template = product.prod_tmpl_num();
        let statistical = if INSTANTANEOUS_TEMPLATES.contains(&template)
            || AEROSOL_TEMPLATES.contains(&template)
        {
            None
        } else if STATISTICAL_TEMPLATES.contains(&template) {
            Some(())
        } else {
            return Err(EncodeError::conversion(format!(
                "unsupported GRIB2 product definition template 4.{template} in {}",
                path.display()
            )));
        };
        let payload: Vec<u8> = product.iter().copied().collect();
        let aerosol = if AEROSOL_TEMPLATES.contains(&template) {
            // Octets 12-35 hold the aerosol fields, then the 4.0 layout
            // resumes 24 octets on: the section must reach the surface.
            if payload.len() < START_OF_PROD_TEMPLATE + 24 + 25 {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 product definition section is too short in {}",
                    path.display()
                )));
            }
            Some(parse_aerosol(&payload))
        } else {
            None
        };
        let ensemble = if ENSEMBLE_TEMPLATES.contains(&template) {
            let identity = ensemble_identity(template, &payload).ok_or_else(|| {
                EncodeError::conversion(format!(
                    "GRIB2 product definition section is too short in {}",
                    path.display()
                ))
            })?;
            Some((identity, statistical.is_some()))
        } else {
            None
        };

        let (category, number) = (
            product.parameter_category(),
            product.parameter_number(),
        );
        let (Some(category), Some(number)) = (category, number) else {
            return Err(EncodeError::conversion(format!(
                "GRIB2 product definition section is too short in {}",
                path.display()
            )));
        };
        let Some((first_surface, _second)) = product.fixed_surfaces() else {
            return Err(EncodeError::conversion(format!(
                "GRIB2 product definition carries no fixed surface in {}",
                path.display()
            )));
        };
        let level_value = {
            let value = first_surface.value();
            value.is_finite().then_some(value)
        };

        let valid_time = if statistical.is_some() {
            // Statistical products are valid at the end of the overall
            // interval (section 4 octets 35-41); the forecast time octets hold
            // the interval start.
            let end_octet = usize::from(interval_end_octet(template));
            let start = end_octet - 6;
            // Through the first range's length, octets end + 15 to end + 18.
            if payload.len() < end_octet + 19 - 6 {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 product definition section is too short in {}",
                    path.display()
                )));
            }
            build_time(
                u16::from_be_bytes([payload[start], payload[start + 1]]),
                payload[start + 2],
                payload[start + 3],
                payload[start + 4],
                payload[start + 5],
                payload[start + 6],
                path,
            )?
        } else {
            let Some(ForecastTime { unit, value }) = product.forecast_time() else {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 product definition carries no forecast time in {}",
                    path.display()
                )));
            };
            let code = match unit {
                Code::Name(name) => u8::from(name),
                Code::Num(number) => number,
            };
            let Some(seconds) = time_unit_seconds(code) else {
                return Err(EncodeError::conversion(format!(
                    "unsupported GRIB2 time unit {code} in {}",
                    path.display()
                )));
            };
            reference_time + Duration::seconds(i64::from(value) * seconds)
        };
        let statistical_process = statistical.map(|()| {
            payload[usize::from(interval_end_octet(template)) + 12 - 6]
        });
        let time_range = statistical.map(|()| {
            let end = usize::from(interval_end_octet(template)) - 6;
            (
                payload[end + 7],
                payload[end + 14],
                u32::from_be_bytes([payload[end + 15], payload[end + 16], payload[end + 17], payload[end + 18]]),
            )
        });

        messages.push(MessageInfo {
            band: band + 1,
            discipline,
            parameter_category: category,
            parameter_number: number,
            level_type: first_surface.surface_type,
            level_value,
            reference_time,
            valid_time,
            statistical_process,
            aerosol,
            ensemble,
            time_range,
        });
    }
    if messages.is_empty() {
        return Err(EncodeError::conversion(format!(
            "no GRIB2 messages in {}",
            path.display()
        )));
    }
    Ok(messages)
}

/// The GDAL unit of `message` when it carries `spec`'s quantity — under the
/// registered identity or one of its alias triples (the same surface, the
/// same statistical process), or under one of its whole alternate
/// identities — and `None` when it does not. The unit is the identity's own:
/// an alternate may spell it differently (ECMWF's cloud cover fraction).
/// An aerosol product matches on its aerosol identity too, whole, and only
/// a variable registered with one matches it. Mirrors `_matching_unit` in
/// `xuebuild/grib2.py`.
fn matching_unit<'a>(spec: &'a VariableSpec, message: &MessageInfo) -> Option<&'a str> {
    if let Some((ensemble, interval)) = &message.ensemble {
        // An ensemble product is only ever a `template-ensemble` rule's,
        // as the GDAL matcher takes it (`inspect::band_matches`).
        let accepted = spec.matching.gdal.iter().any(|rule| {
            let GdalRule::TemplateEnsemble {
                discipline,
                category,
                number,
                level_type,
                level_value,
                ensemble: expected,
                hours,
                statistical,
            } = rule
            else {
                return false;
            };
            (message.discipline, message.parameter_category, message.parameter_number, message.level_type)
                == (*discipline, *category, *number, *level_type)
                && level_value.is_none_or(|expected| message.level_value == Some(expected))
                && ensemble == expected
                && *interval == hours.is_some()
                && match (hours, message.time_range) {
                    (None, _) => true,
                    (Some(hours), Some((count, unit, length))) => {
                        count == 1 && unit == 1 && length == *hours && message.statistical_process == *statistical
                    }
                    (Some(_), None) => false,
                }
        });
        return accepted.then_some(spec.matching.gdal_unit.as_str());
    }
    if spec.ensemble.is_some() {
        // A spread, a quartile, a probability is never a single forecast's
        // record, whatever its parameter.
        return None;
    }
    let triple = (
        message.discipline,
        message.parameter_category,
        message.parameter_number,
    );
    let identity = &spec.matching;
    let primary = (triple == (spec.grib2_discipline, spec.grib2_category, spec.grib2_number)
        || identity.aliases.contains(&triple))
        && message.level_type == spec.grib2_level_type
        && spec
            .grib2_level_value
            .is_none_or(|expected| message.level_value == Some(expected))
        && message.statistical_process == identity.statistical
        && message.aerosol == spec.grib2_aerosol;
    if primary {
        return Some(&identity.gdal_unit);
    }
    if message.aerosol.is_some() {
        return None;
    }
    identity
        .alternates
        .iter()
        .find(|alternate| {
            triple == alternate.triple()
                && message.level_type == alternate.level_type
                && alternate
                    .level_value
                    .is_none_or(|expected| message.level_value == Some(expected))
                && message.statistical_process == alternate.statistical
        })
        .map(|alternate| {
            if alternate.gdal_unit.is_empty() {
                identity.gdal_unit.as_str()
            } else {
                alternate.gdal_unit.as_str()
            }
        })
}

fn matches(spec: &VariableSpec, message: &MessageInfo) -> bool {
    matching_unit(spec, message).is_some()
}

/// Locate every requested variable from the GRIB2 headers alone.
///
/// Mirrors [`crate::encode::inspect::inspect_grib_multi`]: variables in `optional_ids`
/// may be absent, more than one match is an error. Units are the fixed
/// GDAL-normalized strings from the variable table — the identity's own, or
/// the alternate's the record matched under — validated against a real GDAL
/// pass once per run by the converter.
pub fn inspect_grib_fast(
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
    let messages = index_messages(path)?;
    let mut frames = Vec::new();
    for variable_id in variable_ids {
        let spec = registry.variable(variable_id)?;
        let found: Vec<&MessageInfo> = messages
            .iter()
            .filter(|message| matches(spec, message))
            .collect();
        if found.is_empty() && optional_ids.contains(variable_id) {
            continue;
        }
        if found.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "expected exactly one {} band for {variable_id} in {}, found {}",
                spec.matching.grib_element,
                path.display(),
                found.len()
            )));
        }
        let message = found[0];
        let unit = matching_unit(spec, message).expect("found by matching");
        let delta = message.valid_time.unix_timestamp() - message.reference_time.unix_timestamp();
        if delta < 0 || delta % 3600 != 0 {
            return Err(EncodeError::conversion(format!(
                "forecast time is not a non-negative whole hour in {}",
                path.display()
            )));
        }
        frames.push((
            (*variable_id).to_string(),
            SourceFrame {
                path: path.to_path_buf(),
                band: message.band,
                variable_id: (*variable_id).to_string(),
                run_time: message.reference_time,
                valid_time: message.valid_time,
                lead_seconds: delta,
                unit: unit.to_string(),
            },
        ));
    }
    Ok(frames)
}

#[cfg(test)]
mod tests {
    use super::{ensemble_identity, interval_end_octet, matches, matching_unit, MessageInfo};
    use crate::encode::registry::Registry;
    use crate::encode::variables::{AerosolIdentity, EnsembleIdentity};
    use time::OffsetDateTime;

    fn message(category: u8, number: u8, level_type: u8) -> MessageInfo {
        MessageInfo {
            band: 1,
            discipline: 0,
            parameter_category: category,
            parameter_number: number,
            level_type,
            level_value: None,
            reference_time: OffsetDateTime::UNIX_EPOCH,
            valid_time: OffsetDateTime::UNIX_EPOCH,
            statistical_process: None,
            aerosol: None,
            ensemble: None,
            time_range: None,
        }
    }

    /// An aerosol record is its parameter *and* its aerosol identity: the
    /// dust optical depth and the total are the same triple on the same
    /// surface, told apart by the type; a PM record under another size
    /// limit is another field; and an ordinary variable never matches an
    /// aerosol product, nor an aerosol variable an ordinary record.
    #[test]
    fn an_aerosol_record_matches_on_its_aerosol_identity_whole() {
        let registry = Registry::embedded();
        let aod = registry.variable("aod").unwrap();
        let dust = registry.variable("aoddust").unwrap();
        let mut total = message(20, 102, 10);
        total.aerosol = aod.grib2_aerosol;
        assert_eq!(matching_unit(aod, &total), Some("Numeric"));
        assert_eq!(matching_unit(dust, &total), None);
        total.aerosol = dust.grib2_aerosol;
        assert_eq!(matching_unit(dust, &total), Some("Numeric"));
        assert_eq!(matching_unit(aod, &total), None);
        total.aerosol = None;
        assert_eq!(matching_unit(aod, &total), None);
        let tcdc = registry.variable("tcdc").unwrap();
        let mut cloud = message(6, 1, 10);
        cloud.aerosol = aod.grib2_aerosol;
        assert_eq!(matching_unit(tcdc, &cloud), None);

        let pm10 = registry.variable("pm10").unwrap();
        let pm10dust = registry.variable("pm10dust").unwrap();
        let mut coarse = message(13, 192, 1);
        coarse.level_value = Some(0.0);
        coarse.aerosol = pm10.grib2_aerosol;
        assert_eq!(matching_unit(pm10, &coarse), Some("10^-6g/m^3"));
        assert_eq!(matching_unit(pm10dust, &coarse), None);
        coarse.aerosol = Some(AerosolIdentity { size_first: Some((7, 25)), ..pm10.grib2_aerosol.unwrap() });
        assert_eq!(matching_unit(pm10, &coarse), None);
    }

    /// An ensemble product's identity off its section 4 octets: CMA's
    /// threshold in both limits of a type 3 probability keeps the lower
    /// alone, a sign-and-magnitude scale factor reads negative, and an
    /// all-ones limit is missing.
    #[test]
    fn an_ensemble_identity_is_read_off_its_octets() {
        let mut payload = vec![0u8; 80];
        let mut put = |octet: usize, bytes: &[u8]| payload[octet - 6..octet - 6 + bytes.len()].copy_from_slice(bytes);
        put(35, &[3, 6, 3]);
        put(38, &[2]);
        put(39, &1000u32.to_be_bytes());
        put(43, &[2]);
        put(44, &1000u32.to_be_bytes());
        assert_eq!(ensemble_identity(9, &payload), Some(EnsembleIdentity::probability(3, Some((2, 1000)), None)));
        assert_eq!(ensemble_identity(6, &payload), Some(EnsembleIdentity::Percentile(3)));
        assert_eq!(ensemble_identity(12, &payload), Some(EnsembleIdentity::Derived(3)));
        assert_eq!(ensemble_identity(0, &payload), None);
        payload[37 - 6] = 2;
        payload[38 - 6] = 0x82;
        payload[43 - 6] = 0xFF;
        assert_eq!(
            ensemble_identity(5, &payload),
            Some(EnsembleIdentity::Probability { probability_type: 2, lower: Some((-2, 1000)), upper: None })
        );
        assert_eq!(ensemble_identity(5, &payload[..30]), None);
        assert_eq!((interval_end_octet(8), interval_end_octet(9), interval_end_octet(10), interval_end_octet(12)), (35, 48, 36, 37));
    }

    /// An ensemble product matches a `template-ensemble` rule alone, on its
    /// identity and interval; a variable that is an ensemble statistic
    /// matches no single forecast under its parameter.
    #[test]
    fn an_ensemble_message_matches_its_rule_alone() {
        let registry = Registry::parse(&crate::encode::registry::ensemble_fixture::document().to_string()).unwrap();
        let tmp2m = registry.variable("tmp2m").unwrap();
        let quartile = registry.variable("tmp2mq25").unwrap();
        let mut median = message(0, 0, 103);
        median.level_value = Some(2.0);
        median.ensemble = Some((EnsembleIdentity::Percentile(50), false));
        assert!(matches(tmp2m, &median) && !matches(quartile, &median));
        median.ensemble = Some((EnsembleIdentity::Percentile(25), false));
        assert!(matches(quartile, &median) && !matches(tmp2m, &median));
        median.ensemble = Some((EnsembleIdentity::Percentile(25), true));
        assert!(!matches(quartile, &median), "an interval template is another record");
        let mut single = message(0, 0, 103);
        single.level_value = Some(2.0);
        assert!(matches(tmp2m, &single) && !matches(quartile, &single));

        let above = registry.variable("apcp24gt10").unwrap();
        let mut total = message(1, 8, 1);
        total.ensemble = Some((EnsembleIdentity::probability(3, Some((2, 1000)), None), true));
        total.statistical_process = Some(1);
        total.time_range = Some((1, 1, 24));
        assert!(matches(above, &total));
        assert!(!matches(registry.variable("apcp").unwrap(), &total));
        total.time_range = Some((1, 1, 6));
        assert!(!matches(above, &total));
        total.time_range = Some((2, 1, 24));
        assert!(!matches(above, &total));
        total.time_range = Some((1, 1, 24));
        total.statistical_process = Some(0);
        assert!(!matches(above, &total));
    }

    /// ECMWF `msl`: plain pressure (0/3/0) on the mean sea level surface is
    /// the registry's PRMSL (0/3/1); the same triple at the ground is the
    /// surface pressure, a different field, and MSLET stays unregistered.
    #[test]
    fn an_alias_triple_matches_on_the_same_surface_only() {
        let registry = Registry::embedded();
        let prmsl = registry.variable("prmsl").unwrap();
        assert!(matches(prmsl, &message(3, 1, 101)));
        assert!(matches(prmsl, &message(3, 0, 101)));
        assert!(matches(prmsl, &message(3, 198, 101)));
        assert!(!matches(prmsl, &message(3, 0, 1)));
        assert!(matches(prmsl, &message(3, 192, 101)));
        assert!(!matches(prmsl, &message(3, 192, 1)));
    }

    /// An alternate is a whole identity: ECMWF's gust is the same parameter
    /// on the 10 m surface as an interval maximum, its cloud cover the local
    /// 0/6/192 at the ground under another unit, its skin temperature 0/0/17
    /// with no surface value where pgrb2's declares 0 — and the primary's
    /// surface value is still required of a record under the primary triple.
    #[test]
    fn an_alternate_matches_as_a_whole_identity_with_its_own_unit() {
        let registry = Registry::embedded();
        let gust = registry.variable("gust").unwrap();
        let mut maximum = message(2, 22, 103);
        maximum.level_value = Some(10.0);
        maximum.statistical_process = Some(2);
        assert_eq!(matching_unit(gust, &maximum), Some("m/s"));
        maximum.statistical_process = None;
        assert_eq!(matching_unit(gust, &maximum), None);
        let mut surface = message(2, 22, 1);
        surface.level_value = Some(0.0);
        assert_eq!(matching_unit(gust, &surface), Some("m/s"));
        surface.level_value = None;
        assert_eq!(matching_unit(gust, &surface), None);

        let tcdc = registry.variable("tcdc").unwrap();
        assert_eq!(matching_unit(tcdc, &message(6, 1, 10)), Some("%"));
        assert_eq!(matching_unit(tcdc, &message(6, 192, 1)), Some("-"));
        assert_eq!(matching_unit(tcdc, &message(6, 192, 10)), None);

        let tmpsfc = registry.variable("tmpsfc").unwrap();
        assert_eq!(matching_unit(tmpsfc, &message(0, 17, 1)), Some("C"));
        assert_eq!(matching_unit(tmpsfc, &message(0, 0, 1)), None);
    }
}
