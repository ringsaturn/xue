//! Record discovery through GDAL's own band metadata — the port of
//! `xuebuild/gdal.py`'s `inspect_grib_multi`.
//!
//! This is the reference matcher: the converter runs it once per run on the
//! first file, both to probe wind availability and to cross-check the much
//! faster GRIB2 header index (`gribindex.rs`) that locates the bands every
//! extraction reads.

use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use time::OffsetDateTime;

use crate::encode::errors::{EncodeError, Result};
use crate::encode::gdalio::{BandInfo, Dataset};
use crate::encode::model::SourceFrame;
use crate::encode::variables::{isobaric_variable, variable_spec, SURFACE_TEMPERATURE_IDS};

static HEIGHT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^0-9])2(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground").expect("valid regex")
});
static TEN_METRE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[^0-9])10(?:\.0+)?\s*m(?:eter)?s?\s+above\s+ground").expect("valid regex")
});

pub const SUPPORTED_EXTENSIONS: &[&str] = &["grb", "grb2", "grib2"];

/// Collapse a temperature unit string to `K`, `C` or `F`.
pub fn normalize_unit(unit: &str) -> Result<&'static str> {
    let compact = unit
        .trim()
        .trim_matches(|character| "[]()".contains(character))
        .to_lowercase()
        .replace('°', "")
        .replace("degrees", "")
        .replace("degree", "");
    let compact = compact.split_whitespace().collect::<Vec<_>>().join(" ");
    match compact.as_str() {
        "k" | "kelvin" => Ok("K"),
        "c" | "celsius" | "degc" => Ok("C"),
        "f" | "fahrenheit" | "degf" => Ok("F"),
        _ => Err(EncodeError::conversion(format!(
            "unsupported temperature unit: {}",
            if unit.is_empty() { "<missing>" } else { unit }
        ))),
    }
}

/// Whether a precipitation rate record is already in mm/h (MRMS), as
/// opposed to the kg m⁻² s⁻¹ every NWP record carries. Mirrors
/// `precipitation_rate_is_mm_per_hour` in `xuebuild/gdal.py`.
pub fn precipitation_rate_is_mm_per_hour(unit: &str) -> bool {
    matches!(compact_unit(unit).as_str(), "mm/hr" | "mm/h")
}

fn compact_unit(unit: &str) -> String {
    unit.trim()
        .to_lowercase()
        .replace('²', "^2")
        .chars()
        .filter(|character| !" *()[]".contains(*character))
        .collect()
}

/// The per-variable unit acceptance the Python converter expresses as a GDAL
/// raster expression. Only its *identity* matters here — the converter
/// compares the string of the header-index frame against the gdalinfo frame —
/// so the expressions are reproduced verbatim.
pub fn raster_expression(variable_id: &str, unit: &str) -> Result<String> {
    let rate_units = ["kg/m^2s", "kg/m2s", "kgm^-2s^-1", "kgm-2s-1"];
    match variable_id {
        // A rate already in mm/h (the MRMS radar-derived rate) takes no
        // scaling. Mirrors `precipitation_expression` in `xuebuild/gdal.py`.
        "prate" if precipitation_rate_is_mm_per_hour(unit) => {
            Ok("maximum(0,minimum(50,A))".into())
        }
        "tmp2m" => {
            let value = match normalize_unit(unit)? {
                "K" => "A-273.15",
                "F" => "(A-32)*5/9",
                _ => "A",
            };
            Ok(format!("maximum(-60,minimum(50,{value}))"))
        }
        "prate" | "prate_ave" => {
            if !rate_units.contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported precipitation rate unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok(if variable_id == "prate" {
                "maximum(0,minimum(50,A*3600))".into()
            } else {
                "A".into()
            })
        }
        "tp" => {
            let compact = unit
                .trim()
                .trim_matches(|character| "[]()".contains(character))
                .to_lowercase();
            if !matches!(compact.as_str(), "-" | "m" | "") {
                return Err(EncodeError::conversion(format!(
                    "unsupported precipitation accumulation unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("A".into())
        }
        "dswrf" => {
            if !["w/m^2", "w/m2", "wm^-2", "wm-2"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported radiative flux unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(1270,A))".into())
        }
        "ugrd10m" | "vgrd10m" => wind_expression(unit),
        // The 2 m dew point, the apparent temperature and the surface (skin)
        // temperature take the temperature's rule over their own codebook
        // ranges.
        "dpt2m" | "aptmp2m" | "tmpsfc" => {
            let value = match normalize_unit(unit)? {
                "K" => "A-273.15",
                "F" => "(A-32)*5/9",
                _ => "A",
            };
            Ok(match variable_id {
                "dpt2m" => format!("maximum(-70,minimum(40,{value}))"),
                "aptmp2m" => format!("maximum(-90,minimum(60,{value}))"),
                _ => format!("maximum(-60,minimum(67,{value}))"),
            })
        }
        // Sea ice cover: GRIB2 carries a 0–1 proportion (GDAL spells the
        // unit "Proportion"), the codebook quantizes percent.
        "icec" => {
            if !["proportion", "fraction", "1", "-", ""].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported sea ice cover unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(100,A*100))".into())
        }
        // Sea ice thickness and significant wave height, in metres.
        "icetk" | "htsgw" => {
            if !["m", "metre", "meter", "metres", "meters"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported {} unit: {}",
                    if variable_id == "icetk" { "sea ice thickness" } else { "wave height" },
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok(if variable_id == "icetk" {
                "maximum(0,minimum(5.08,A))".into()
            } else {
                "maximum(0,minimum(25.4,A))".into()
            })
        }
        // Wave period in seconds.
        "perpw" => {
            if !["s", "sec", "second", "seconds"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported wave period unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(25.4,A))".into())
        }
        // Wave direction in degrees true (GDAL spells it "Degree true"); the
        // converter reduces it modulo 360 itself.
        "dirpw" => {
            if !["degreetrue", "degtrue", "degrees", "degree", "deg", "degreestrue"]
                .contains(&compact_unit(unit).as_str())
            {
                return Err(EncodeError::conversion(format!(
                    "unsupported wave direction unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("A".into())
        }
        // Composite reflectivity in dBZ, which GDAL's GRIB tables spell
        // "dB" and the radar mosaic's NetCDF spells in full; sub-zero
        // returns are below the codebook, no echo either way.
        "cref" => {
            if !["db", "dbz"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported reflectivity unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(80,A))".into())
        }
        // Visibility: GRIB2 carries metres, the codebook quantizes km.
        "vis" => {
            if !["m", "metre", "meter", "metres", "meters"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported visibility unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("A/1000".into())
        }
        // Wind gust: a speed in the wind components' unit, one-sided.
        "gust" => {
            let compact: String = unit
                .trim()
                .to_lowercase()
                .chars()
                .filter(|character| !" *()[]".contains(*character))
                .collect();
            if !["m/s", "m/sec", "ms-1", "ms^-1", "mps"].contains(&compact.as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported wind gust unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(127,A))".into())
        }
        // Cloud cover — the total and the layers — in percent as pgrb2
        // carries it, or scaled up from the 0–1 fraction ECMWF writes (GDAL
        // spells that unit "-").
        "tcdc" | "lcdc" | "mcdc" | "hcdc" => {
            match unit
                .trim()
                .trim_matches(|character| "[]()".contains(character))
                .to_lowercase()
                .as_str()
            {
                "%" => Ok("maximum(0,minimum(100,A))".into()),
                "-" | "1" | "fraction" | "proportion" | "(0 - 1)" | "0-1" => {
                    Ok("maximum(0,minimum(100,A*100))".into())
                }
                _ => Err(EncodeError::conversion(format!(
                    "unsupported cloud cover unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                ))),
            }
        }
        // CAPE in J/kg.
        "cape" => {
            if !["j/kg", "jkg-1", "jkg^-1"].contains(&compact_unit(unit).as_str()) {
                return Err(EncodeError::conversion(format!(
                    "unsupported CAPE unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("maximum(0,minimum(6350,A))".into())
        }
        // Mean sea level pressure: GRIB2 carries pascals, the codebook
        // quantizes hectopascals. Only Pa is accepted — a file already in
        // hPa would divide twice, and no source publishes one.
        "prmsl" => {
            if unit.trim().trim_matches(|character| "[]()".contains(character)) != "Pa" {
                return Err(EncodeError::conversion(format!(
                    "unsupported pressure unit: {}",
                    if unit.is_empty() { "<missing>" } else { unit }
                )));
            }
            Ok("A/100".into())
        }
        // The isobaric families, by family: geopotential height is already
        // in metres (GDAL reports GFS HGT as "gpm"; a source spelling it "m"
        // differs by less than the codebook step), temperature takes the 2 m
        // temperature's rule, the wind components the 10 m pair's, relative
        // humidity is percent, and specific humidity is a kg/kg mass ratio
        // the codebook quantizes in g/kg.
        other if isobaric_variable(other).is_some() => {
            let (family, _) = isobaric_variable(other).expect("checked");
            let compact = unit.trim().trim_matches(|character| "[]()".contains(character));
            match family {
                "hgt" => {
                    if !matches!(compact, "gpm" | "m") {
                        return Err(EncodeError::conversion(format!(
                            "unsupported geopotential height unit: {}",
                            if unit.is_empty() { "<missing>" } else { unit }
                        )));
                    }
                    Ok("A".into())
                }
                "tmp" => {
                    let value = match normalize_unit(unit)? {
                        "K" => "A-273.15",
                        "F" => "(A-32)*5/9",
                        _ => "A",
                    };
                    Ok(format!("maximum(-60,minimum(50,{value}))"))
                }
                "rh" => {
                    if compact != "%" {
                        return Err(EncodeError::conversion(format!(
                            "unsupported relative humidity unit: {}",
                            if unit.is_empty() { "<missing>" } else { unit }
                        )));
                    }
                    Ok("maximum(0,minimum(100,A))".into())
                }
                "spfh" => {
                    let compact: String = unit
                        .trim()
                        .to_lowercase()
                        .chars()
                        .filter(|character| !" *()[]".contains(*character))
                        .collect();
                    if compact != "kg/kg" {
                        return Err(EncodeError::conversion(format!(
                            "unsupported specific humidity unit: {}",
                            if unit.is_empty() { "<missing>" } else { unit }
                        )));
                    }
                    Ok("A*1000".into())
                }
                "ugrd" | "vgrd" => wind_expression(unit),
                // Vertical velocity in pressure coordinates, already Pa/s.
                "vvel" => {
                    if !["pa/s", "pas-1", "pas^-1"].contains(&compact_unit(unit).as_str()) {
                        return Err(EncodeError::conversion(format!(
                            "unsupported vertical velocity unit: {}",
                            if unit.is_empty() { "<missing>" } else { unit }
                        )));
                    }
                    Ok("maximum(-6.35,minimum(6.35,A))".into())
                }
                _ => Err(EncodeError::conversion(format!(
                    "unsupported variable: {other}"
                ))),
            }
        }
        other => Err(EncodeError::conversion(format!(
            "unsupported variable: {other}"
        ))),
    }
}

fn wind_expression(unit: &str) -> Result<String> {
    let compact: String = unit
        .trim()
        .to_lowercase()
        .chars()
        .filter(|character| !" *()[]".contains(*character))
        .collect();
    if !["m/s", "m/sec", "ms-1", "ms^-1", "mps"].contains(&compact.as_str()) {
        return Err(EncodeError::conversion(format!(
            "unsupported wind component unit: {}",
            if unit.is_empty() { "<missing>" } else { unit }
        )));
    }
    Ok("maximum(-64,minimum(64,A))".into())
}

/// Whether one band carries the given element on the given isobaric surface.
///
/// The surface value is pascals in GRIB2 itself, and that is what GDAL
/// reports: the 850 hPa record comes back as short name `85000-ISBL` with the
/// description `85000[Pa] ISBL="Isobaric surface"`. Hectopascals are accepted
/// too — that is how the level is spelled in an `.idx` phrase and in every
/// human-facing description. Whichever unit, it must name *this* level: a
/// matcher that let 500 also match 1000 (or 50000 Pa also match 100000) would
/// silently pick the wrong plane.
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

fn searchable(band: &BandInfo) -> String {
    [
        band.item("GRIB_SHORT_NAME"),
        band.item("GRIB_COMMENT"),
        band.item("GRIB_LEVEL"),
        &band.description,
    ]
    .join(" ")
}

/// One MRMS product under the registry's MRMS alternate: GDAL names the
/// record by the product (its local table for centre 161 —
/// `MergedReflectivityQCComposite`, `PrecipRate`), on the MRMS-local
/// discipline 209. The level is part of the product name, so the element
/// alone is unambiguous. Mirrors `_is_mrms_record` in `xuebuild/gdal.py`;
/// `element` is the product's name in upper case, as `band_matches` compares.
fn is_mrms_record(band: &BandInfo, element: &str) -> bool {
    band.item("GRIB_ELEMENT").to_uppercase() == element && band.item("GRIB_DISCIPLINE") == "209"
}

fn band_matches(variable_id: &str, band: &BandInfo) -> Result<bool> {
    let element = band.item("GRIB_ELEMENT").to_uppercase();
    let short_name = band.item("GRIB_SHORT_NAME").to_uppercase();
    Ok(match variable_id {
        // One element on the 2 m surface: the temperature, the dew point,
        // the apparent temperature.
        "tmp2m" | "dpt2m" | "aptmp2m" => {
            element == variable_spec(variable_id)?.grib_element
                && (matches!(short_name.as_str(), "2-HTGL" | "2-M-HTGL")
                    || HEIGHT_RE.is_match(&searchable(band)))
        }
        // sflux files carry only the interval-averaged PRATE record, pgrb2
        // fetches only the instantaneous one — the same surface matcher hits
        // exactly the record its source provides; the MRMS rate is its own
        // product.
        "prate" | "prate_ave" => {
            (element == "PRATE"
                && (short_name == "0-SFC" || searchable(band).to_lowercase().contains("surface")))
                || (variable_id == "prate" && is_mrms_record(band, "PRECIPRATE"))
        }
        // ECMWF open data tp: GDAL's tables do not know the local parameter
        // 0/1/193, so GRIB_ELEMENT is "unknown" and the comment carries the
        // raw triple.
        "tp" => {
            let comment = band.item("GRIB_COMMENT").to_string();
            let text = [comment.as_str(), band.item("GRIB_LEVEL"), &band.description]
                .join(" ")
                .to_lowercase();
            matches!(element.to_lowercase().as_str(), "unknown" | "tp" | "apcp")
                && (short_name == "0-SFC" || text.contains("surface"))
                && (comment.contains("cat 1, subcat 193")
                    || text.contains("total precipitation"))
        }
        // One element on the ground or water surface: the fetched files
        // carry only the instantaneous surface record of each, so element +
        // surface is unambiguous. GDAL spells the surface `0-SFC`; the
        // GFS-Wave records carry a surface value of 1 and come out `1-SFC`,
        // so any value on the SFC surface is accepted. The prose test looks
        // for the surface's own name rather than the word "surface", which
        // the isobaric levels the same TMP element is fetched on also carry
        // ("Isobaric surface"). Beside the registry's element, the one GDAL
        // names another centre's spelling by: ECMWF's skin temperature is
        // SKINT, its peak wave period PWPER and its mean wave direction
        // WWSDIR (the registry's alias triples). Two ECMWF records sit on
        // another surface altogether and are matched by their own rule: the
        // gust on the 10 m surface (the interval maximum, `10fg`) and the
        // most-unstable CAPE departing from surface type 17, which GDAL
        // describes as such and gives no short name of its own.
        "dswrf" | "gust" | "cape" | "vis" | "tmpsfc" | "icec" | "icetk" | "htsgw" | "perpw"
        | "dirpw" => {
            let text = searchable(band).to_lowercase();
            let registered = variable_spec(variable_id)?.grib_element;
            let aliases: &[&str] = match registered {
                "TMP" => &["SKINT"],
                "PERPW" => &["PWPER"],
                "DIRPW" => &["WWSDIR"],
                _ => &[],
            };
            let on_surface = (element == registered || aliases.contains(&element.as_str()))
                && (short_name.ends_with("-SFC")
                    || text.contains("sfc=\"")
                    || text.contains("ground or water surface"));
            on_surface
                || (variable_id == "gust"
                    && element == "GUST"
                    && (matches!(short_name.as_str(), "10-HTGL" | "10-M-HTGL")
                        || TEN_METRE_RE.is_match(&searchable(band))))
                || (variable_id == "cape"
                    && element == "CAPE"
                    && (text.contains("most unstable") || text.contains("mudl")))
        }
        // One cloud cover element on its own layer surface (code table 4.5
        // types 214 / 224 / 234, which GDAL spells `0-LCY` / `0-MCY` /
        // `0-HCY`); the interval averages are never downloaded.
        "lcdc" | "mcdc" | "hcdc" => {
            let (token, phrases): (&str, &[&str]) = match variable_id {
                "lcdc" => ("0-LCY", &["low cloud"]),
                "mcdc" => ("0-MCY", &["middle cloud", "medium cloud"]),
                _ => ("0-HCY", &["high cloud"]),
            };
            let text = searchable(band).to_lowercase();
            element == variable_spec(variable_id)?.grib_element
                && (short_name == token || phrases.iter().any(|phrase| text.contains(phrase)))
        }
        // The entire atmosphere (surface type 10), which GDAL spells
        // `0-EATM`; the per-layer cloud covers are never downloaded. ECMWF
        // `tcc` is the ECMWF-local 0/6/192 on the ground surface — GDAL's
        // tables do not know it, so GRIB_ELEMENT is "unknown" and the
        // comment carries the raw triple, the way it does for `tp`.
        "tcdc" | "cref" => {
            let text = searchable(band).to_lowercase();
            let comment = band.item("GRIB_COMMENT");
            (element == variable_spec(variable_id)?.grib_element
                && (short_name == "0-EATM" || text.contains("entire atmosphere")))
                || (variable_id == "tcdc"
                    && element.to_lowercase() == "unknown"
                    && (short_name == "0-SFC" || text.contains("ground or water surface"))
                    && comment.contains("cat 6, subcat 192"))
                || (variable_id == "cref" && is_mrms_record(band, "MERGEDREFLECTIVITYQCCOMPOSITE"))
        }
        "ugrd10m" | "vgrd10m" => {
            element == variable_spec(variable_id)?.grib_element
                && (matches!(short_name.as_str(), "10-HTGL" | "10-M-HTGL")
                    || TEN_METRE_RE.is_match(&searchable(band)))
        }
        // PRMSL on GRIB2 surface 101 (mean sea level); GDAL spells that
        // short name `0-MSL`, and the phrase fallback catches drivers that
        // do not. ECMWF `msl` is plain pressure on that surface (the
        // registry's 0/3/0 alias), which GDAL names PRES, and HRRR's is the
        // MAPS reduction MSLMA (the 0/3/198 alias) — the surface is what
        // makes them the same field.
        "prmsl" => {
            matches!(element.as_str(), "PRMSL" | "PRES" | "MSLMA")
                && (short_name == "0-MSL"
                    || searchable(band).to_lowercase().contains("mean sea level"))
        }
        other if isobaric_variable(other).is_some() => {
            let (_, level) = isobaric_variable(other).expect("checked");
            let spec = variable_spec(other)?;
            if spec.grib_element.is_empty() {
                return Err(EncodeError::conversion(format!(
                    "{other} is derived, not a GRIB record"
                )));
            }
            is_isobaric_record(band, spec.grib_element, level)
        }
        other => {
            return Err(EncodeError::conversion(format!(
                "unsupported variable: {other}"
            )))
        }
    })
}

fn timestamp(band: &BandInfo, key: &str) -> Option<OffsetDateTime> {
    let raw: f64 = band.metadata.get(key)?.parse().ok()?;
    OffsetDateTime::from_unix_timestamp(raw as i64).ok()
}

fn frame_from_band(path: &Path, variable_id: &str, band: &BandInfo) -> Result<SourceFrame> {
    let raw_unit = if band.item("GRIB_UNIT").is_empty() {
        band.item("GRIB_COMMENT")
            .rsplit_once('[')
            .map_or("", |(_, tail)| tail)
            .trim_end_matches(']')
            .to_string()
    } else {
        band.item("GRIB_UNIT").to_string()
    };
    let unit = if SURFACE_TEMPERATURE_IDS.contains(&variable_id) {
        normalize_unit(&raw_unit)?.to_string()
    } else {
        raw_unit
            .trim()
            .trim_matches(|character| "[]".contains(character))
            .to_string()
    };
    raster_expression(variable_id, &unit)?;
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
        variable_id: variable_id.to_string(),
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
        let matches: Vec<&BandInfo> = bands
            .iter()
            .filter(|band| band_matches(variable_id, band).unwrap_or(false))
            .collect();
        if matches.is_empty() && optional_ids.contains(variable_id) {
            continue;
        }
        if matches.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "expected exactly one {} band for {variable_id} in {}, found {}",
                variable_spec(variable_id)?.grib_element,
                path.display(),
                matches.len()
            )));
        }
        frames.push((
            (*variable_id).to_string(),
            frame_from_band(path, variable_id, matches[0])?,
        ));
    }
    Ok(frames)
}
