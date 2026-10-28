//! The registry document (`docs/registry.md`): the variables, bundles and
//! sources an encoder builds, read from one JSON document in place of a
//! table of its own.
//!
//! The Python tables author the document (`xue registry` prints it,
//! `xuebuild.registry.document_json` hands it to the wheel); this encoder
//! implements the closed sets of rule names the document dispatches on
//! (`Conversion`, `GdalRule`, `Predictor`, `Decimation`, `Companion`,
//! `BundleKind`, `DerivationKind`, the two codebook types) and refuses a
//! document that names anything else, or whose `registrySchema` it does not
//! implement, before any input is read. A key a reader does not know is
//! ignored, so the document can gain keys without a release.
//!
//! The snapshot `rust/xue/registry.json` is embedded as the CLI's default;
//! `xue registry --output rust/xue/registry.json` regenerates it and
//! `tests/test_registry.py` holds it to the Python tables.

use std::collections::HashMap;
use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Map, Value};

use crate::encode::errors::{EncodeError, Result};
use crate::encode::quantize::Codebook;
use crate::encode::sources::SourceSpec;
use crate::encode::variables::{AerosolIdentity, MatchSpec, VariableSpec};

/// The document schema this reader implements.
pub const REGISTRY_SCHEMA: u64 = 1;

/// The snapshot committed beside the crate: what `xue-encode` reads when no
/// `--registry` is given.
pub const EMBEDDED_DOCUMENT: &str = include_str!("../../registry.json");

// -- the closed sets ----------------------------------------------------------

/// How a variable's chunks are predicted: chained against the previous
/// frame, or stacked as they are.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Predictor {
    Delta,
    Raw,
}

/// How a reduced tier samples the full one: the site's own code, or the
/// warmest code around the site.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Decimation {
    Sample,
    Peak,
}

/// An artifact a variable's bundle ships beside the store.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Companion {
    Poster,
    Video,
}

/// The unit rule of one variable: the arithmetic that takes a record's
/// values to the variable's unit, and the spellings a record may carry.
/// The semantics are in `units.rs`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "rule", rename_all = "kebab-case")]
pub enum Conversion {
    /// Identity; `acceptedUnits` lists the spellings, no list accepts any.
    #[serde(rename = "none")]
    Identity {
        #[serde(default, rename = "acceptedUnits")]
        accepted_units: Option<Vec<String>>,
    },
    Celsius,
    SoilTemperature,
    PrecipitationRate,
    PrecipitationAccumulation,
    PascalToHectopascal,
    MetreToKilometre,
    MassRatioToGPerKg,
    ProportionToPercent,
    CloudCover,
    DegreesTrue,
    GeopotentialToMetres,
    CodeTable {
        table: String,
    },
}

/// How a variable's record is recognised among a GRIB's bands as GDAL
/// describes them. The semantics are in `inspect.rs`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "rule", rename_all = "kebab-case")]
pub enum GdalRule {
    Surface {
        elements: Vec<String>,
    },
    Height {
        element: String,
        metres: u32,
    },
    EntireAtmosphere {
        element: String,
    },
    MeanSeaLevel {
        elements: Vec<String>,
    },
    Isobaric {
        element: String,
        hpa: u32,
    },
    #[serde(rename_all = "camelCase")]
    CloudLayer {
        element: String,
        short_name: String,
        phrases: Vec<String>,
    },
    Template,
    TemplateAerosol,
    TemplateRank,
    TemplateMrms,
    TotalPrecipitation,
    LocalParameter {
        category: u8,
        number: u8,
    },
    Phrase {
        element: String,
        phrases: Vec<String>,
    },
    GeopotentialSurface {
        elements: Vec<String>,
    },
}

/// What a bundle's arrays hold.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BundleKind {
    Scalar,
    Vector,
    Composite,
    Volume,
}

/// The algorithm a derived bundle's members are computed by.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DerivationKind {
    VapourFlux,
    WaveVector,
    ThetaE,
    PrecipitationType,
    ClearAirTurbulence,
    PrecipitationRate,
}

/// A bundle computed in the converter from `inputs`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Derivation {
    pub kind: DerivationKind,
    pub inputs: Vec<String>,
    /// The isobaric surface, where the algorithm takes one.
    #[serde(default)]
    pub level_hpa: Option<u32>,
}

/// One `bundles` entry. A bundle id absent from the document is the scalar
/// bundle of the variable of the same id.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BundleSpec {
    #[serde(skip)]
    pub id: String,
    pub kind: BundleKind,
    /// The variables the bundle's arrays hold, in file order.
    #[serde(default)]
    pub members: Vec<String>,
    #[serde(default)]
    pub derivation: Option<Derivation>,
    /// A composite's producer id, the channels it reads, and the subset a
    /// source may lack.
    #[serde(default)]
    pub producer: Option<String>,
    #[serde(default)]
    pub inputs: Vec<String>,
    #[serde(default)]
    pub optional_inputs: Vec<String>,
}

// -- the raw entries ------------------------------------------------------------

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Document {
    registry_schema: u64,
    variables: Map<String, Value>,
    bundles: Map<String, Value>,
    sources: Map<String, Value>,
}

#[derive(Deserialize)]
struct ProducerBlock {
    id: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct VariableEntry {
    label: String,
    unit: String,
    parameter: Map<String, Value>,
    #[serde(default)]
    producer: Option<ProducerBlock>,
    #[serde(default)]
    aerosol: Option<Map<String, Value>>,
    #[serde(default)]
    quantization: Option<Map<String, Value>>,
    value_range: (f64, f64),
    fill_values: Vec<f64>,
    #[serde(rename = "static")]
    static_: bool,
    predictor: Predictor,
    decimation: Decimation,
    companions: Vec<Companion>,
    conversion: Conversion,
    #[serde(rename = "match")]
    matching: MatchSpec,
}

fn refused(message: impl Into<String>) -> EncodeError {
    EncodeError::conversion(format!("registry document: {}", message.into()))
}

/// An integer field of a metadata block, or an error naming it.
fn block_u64(block: &Map<String, Value>, key: &str, context: &str) -> Result<u64> {
    block
        .get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| refused(format!("{context} {key} must be a non-negative integer")))
}

/// A `(scaleFactor, scaledValue)` pair of a metadata block: `None` when
/// both are null (GRIB2's missing pattern), the pair when both are set.
fn block_limit(block: &Map<String, Value>, scale_key: &str, value_key: &str, context: &str) -> Result<Option<(i64, u64)>> {
    match (block.get(scale_key), block.get(value_key)) {
        (Some(Value::Null), Some(Value::Null)) | (None, None) => Ok(None),
        (Some(scale), Some(value)) => {
            let scale = scale
                .as_i64()
                .ok_or_else(|| refused(format!("{context} {scale_key} must be an integer or null")))?;
            let value = value
                .as_u64()
                .ok_or_else(|| refused(format!("{context} {value_key} must be a non-negative integer or null")))?;
            Ok(Some((scale, value)))
        }
        _ => Err(refused(format!(
            "{context} {scale_key} and {value_key} must both be set or both null"
        ))),
    }
}

/// The aerosol identity a variable's `aerosol` block carries.
fn parse_aerosol(block: &Map<String, Value>, context: &str) -> Result<AerosolIdentity> {
    let limit = |scale_key: &str, value_key: &str| -> Result<Option<(i8, u32)>> {
        Ok(block_limit(block, scale_key, value_key, context)?.map(|(scale, value)| (scale as i8, value as u32)))
    };
    Ok(AerosolIdentity {
        aerosol_type: block_u64(block, "aerosolType", context)? as u16,
        size_type: block_u64(block, "typeOfSizeInterval", context)? as u8,
        size_first: limit("scaleFactorOfFirstSize", "scaledValueOfFirstSize")?,
        size_second: limit("scaleFactorOfSecondSize", "scaledValueOfSecondSize")?,
        wavelength_type: block_u64(block, "typeOfWavelengthInterval", context)? as u8,
        wavelength_first: limit("scaleFactorOfFirstWavelength", "scaledValueOfFirstWavelength")?,
        wavelength_second: limit("scaleFactorOfSecondWavelength", "scaledValueOfSecondWavelength")?,
    })
}

fn variable_spec(id: &str, entry: VariableEntry) -> Result<VariableSpec> {
    let context = format!("variables.{id}: parameter");
    let parameter = &entry.parameter;
    let grib2_discipline = block_u64(parameter, "discipline", &context)? as u8;
    let grib2_category = block_u64(parameter, "parameterCategory", &context)? as u8;
    let grib2_number = block_u64(parameter, "parameterNumber", &context)? as u8;
    let grib2_level_type = block_u64(parameter, "typeOfFirstFixedSurface", &context)? as u8;
    // `scaledValue × 10^−scaleFactor`, the arithmetic every matcher compares
    // a record's surface with.
    let grib2_level_value = block_limit(
        parameter,
        "scaleFactorOfFirstFixedSurface",
        "scaledValueOfFirstFixedSurface",
        &context,
    )?
    .map(|(scale, value)| value as f64 / 10f64.powi(scale as i32));
    let grib2_aerosol = entry
        .aerosol
        .as_ref()
        .map(|block| parse_aerosol(block, &format!("variables.{id}: aerosol")))
        .transpose()?;
    let mut codebooks: HashMap<String, Codebook> = HashMap::new();
    if let Some(quantization) = &entry.quantization {
        for (profile, block) in quantization {
            let block = block
                .as_object()
                .ok_or_else(|| refused(format!("variables.{id}: quantization.{profile} must be an object")))?;
            let book = Codebook::from_metadata(block, id)
                .map_err(|error| refused(format!("variables.{id}: quantization.{profile}: {error}")))?;
            codebooks.insert(profile.clone(), book);
        }
    }
    Ok(VariableSpec {
        id: id.to_string(),
        label: entry.label,
        output_unit: entry.unit,
        value_range: entry.value_range,
        fill_values: entry.fill_values,
        static_: entry.static_,
        predictor: entry.predictor,
        decimation: entry.decimation,
        companions: entry.companions,
        conversion: entry.conversion,
        parameter: entry.parameter,
        grib2_discipline,
        grib2_category,
        grib2_number,
        grib2_level_type,
        grib2_level_value,
        producer_id: entry.producer.map(|producer| producer.id),
        aerosol: entry.aerosol,
        grib2_aerosol,
        codebooks,
        matching: entry.matching,
    })
}

// -- the registry ---------------------------------------------------------------

/// The parsed, validated document.
#[derive(Debug)]
pub struct Registry {
    pub schema: u64,
    variables: HashMap<String, VariableSpec>,
    variable_order: Vec<String>,
    bundles: HashMap<String, BundleSpec>,
    sources: HashMap<String, SourceSpec>,
    source_order: Vec<String>,
}

impl Registry {
    /// Parse and validate one document, refusing the whole of it on the
    /// first problem, with the field and value named.
    pub fn parse(text: &str) -> Result<Arc<Registry>> {
        let document: Document =
            serde_json::from_str(text).map_err(|error| refused(format!("not a registry document: {error}")))?;
        if document.registry_schema != REGISTRY_SCHEMA {
            return Err(refused(format!(
                "registrySchema {} is not one this encoder implements ({REGISTRY_SCHEMA})",
                document.registry_schema
            )));
        }
        let mut variables = HashMap::with_capacity(document.variables.len());
        let mut variable_order = Vec::with_capacity(document.variables.len());
        for (id, value) in document.variables {
            let entry: VariableEntry =
                serde_json::from_value(value).map_err(|error| refused(format!("variables.{id}: {error}")))?;
            variables.insert(id.clone(), variable_spec(&id, entry)?);
            variable_order.push(id);
        }
        let mut bundles = HashMap::with_capacity(document.bundles.len());
        for (id, value) in document.bundles {
            let mut entry: BundleSpec =
                serde_json::from_value(value).map_err(|error| refused(format!("bundles.{id}: {error}")))?;
            entry.id = id.clone();
            bundles.insert(id, entry);
        }
        let mut sources = HashMap::with_capacity(document.sources.len());
        let mut source_order = Vec::with_capacity(document.sources.len());
        for (id, value) in document.sources {
            let entry: SourceSpec =
                serde_json::from_value(value).map_err(|error| refused(format!("sources.{id}: {error}")))?;
            if entry.id != id {
                return Err(refused(format!("sources.{id}: id is {:?}", entry.id)));
            }
            sources.insert(id.clone(), entry);
            source_order.push(id);
        }
        let registry = Registry {
            schema: document.registry_schema,
            variables,
            variable_order,
            bundles,
            sources,
            source_order,
        };
        registry.validate()?;
        Ok(Arc::new(registry))
    }

    /// The snapshot embedded in the crate.
    pub fn embedded() -> Arc<Registry> {
        Self::parse(EMBEDDED_DOCUMENT).expect("the embedded registry document is valid")
    }

    /// `docs/registry.md` §Validation: every id a bundle or a source names
    /// resolves, a vector has two members, a derivation names its surface
    /// where its algorithm takes one.
    fn validate(&self) -> Result<()> {
        let is_variable = |id: &str| self.variables.contains_key(id);
        let is_bundle_id = |id: &str| is_variable(id) || self.bundles.contains_key(id);
        for (id, bundle) in &self.bundles {
            let context = format!("bundles.{id}");
            for member in bundle
                .members
                .iter()
                .chain(&bundle.inputs)
                .chain(&bundle.optional_inputs)
                .chain(bundle.derivation.iter().flat_map(|derivation| &derivation.inputs))
            {
                if !is_variable(member) {
                    return Err(refused(format!("{context} names {member:?}, which is not a variable")));
                }
            }
            match bundle.kind {
                BundleKind::Scalar if !bundle.members.is_empty() => {
                    return Err(refused(format!("{context} is a scalar with members")));
                }
                BundleKind::Vector if bundle.members.len() != 2 => {
                    return Err(refused(format!(
                        "{context} is a vector with {} members, not two",
                        bundle.members.len()
                    )));
                }
                BundleKind::Composite | BundleKind::Volume if bundle.members.is_empty() => {
                    return Err(refused(format!("{context} has no members")));
                }
                _ => {}
            }
            if let Some(derivation) = &bundle.derivation {
                let takes_level = matches!(
                    derivation.kind,
                    DerivationKind::ThetaE | DerivationKind::ClearAirTurbulence
                );
                if takes_level && derivation.level_hpa.is_none() {
                    return Err(refused(format!("{context} derivation needs levelHpa")));
                }
                if derivation.inputs.is_empty() {
                    return Err(refused(format!("{context} derivation has no inputs")));
                }
            }
        }
        for (id, source) in &self.sources {
            let context = format!("sources.{id}");
            for variable_id in source
                .input_variable_ids
                .iter()
                .chain(&source.optional_at_analysis)
                .chain(source.statistical_processes.iter().map(|(id, _)| id))
                .chain(source.bands.iter().map(|(id, _)| id))
                .chain(source.companion_files.iter().flat_map(|companion| &companion.variable_ids))
            {
                if !is_variable(variable_id) {
                    return Err(refused(format!(
                        "{context} names {variable_id:?}, which is not a variable"
                    )));
                }
            }
            for bundle_id in source
                .bundle_scalar_ids
                .iter()
                .chain(&source.core_bundle_ids)
                .chain(&source.bundle_vector_ids)
                .chain(&source.bundle_composite_ids)
                .chain(&source.bundle_volume_ids)
                .chain(&source.series_bundle_ids)
            {
                if !is_bundle_id(bundle_id) {
                    return Err(refused(format!(
                        "{context} publishes {bundle_id:?}, which is neither a variable nor a bundle"
                    )));
                }
            }
        }
        Ok(())
    }

    // -- lookups --------------------------------------------------------------

    pub fn variable(&self, variable_id: &str) -> Result<&VariableSpec> {
        self.variables
            .get(variable_id)
            .ok_or_else(|| EncodeError::conversion(format!("unsupported variable: {variable_id}")))
    }

    pub fn source(&self, model: &str) -> Result<&SourceSpec> {
        self.sources
            .get(model)
            .ok_or_else(|| EncodeError::conversion(format!("unsupported model: {model}")))
    }

    /// The source whose manifest `model` string is `manifest_model`.
    pub fn source_by_manifest_model(&self, manifest_model: &str) -> Option<&SourceSpec> {
        self.sources().find(|source| source.manifest_model == manifest_model)
    }

    pub fn bundle(&self, bundle_id: &str) -> Option<&BundleSpec> {
        self.bundles.get(bundle_id)
    }

    /// Every variable, in document order.
    pub fn variables(&self) -> impl Iterator<Item = &VariableSpec> {
        self.variable_order.iter().map(|id| &self.variables[id])
    }

    /// Every source, in document order.
    pub fn sources(&self) -> impl Iterator<Item = &SourceSpec> {
        self.source_order.iter().map(|id| &self.sources[id])
    }

    /// One profile's codebook for one variable.
    pub fn codebook(&self, profile: &str, variable_id: &str) -> Result<Codebook> {
        self.variable(variable_id)?
            .codebooks
            .get(profile)
            .cloned()
            .ok_or_else(|| EncodeError::conversion(format!("no {profile} codebook for {variable_id}")))
    }

    /// Whether any published variable has a codebook under `profile`.
    pub fn knows_profile(&self, profile: &str) -> bool {
        self.variables().any(|spec| spec.codebooks.contains_key(profile))
    }

    /// Whether a variable is time-invariant; an unregistered id is not.
    pub fn is_static(&self, variable_id: &str) -> bool {
        self.variables.get(variable_id).is_some_and(|spec| spec.static_)
    }

    /// Whether a variable's chunks stack RAW rather than chaining.
    pub fn is_raw(&self, variable_id: &str) -> Result<bool> {
        Ok(self.variable(variable_id)?.predictor == Predictor::Raw)
    }

    // -- bundles ---------------------------------------------------------------

    /// The variables one bundle carries, in bundle order: a scalar's own, a
    /// vector's pair, a composite's components, a volume's levels.
    pub fn bundle_variable_ids(&self, bundle_id: &str) -> Vec<String> {
        match self.bundles.get(bundle_id) {
            Some(bundle) if !bundle.members.is_empty() => bundle.members.clone(),
            _ => vec![bundle_id.to_string()],
        }
    }

    /// The two component variables of a vector bundle, or `None`.
    pub fn vector_components(&self, bundle_id: &str) -> Option<(&str, &str)> {
        let bundle = self.bundles.get(bundle_id)?;
        (bundle.kind == BundleKind::Vector).then(|| (bundle.members[0].as_str(), bundle.members[1].as_str()))
    }

    /// A composite's components, or `None` for any other bundle.
    pub fn composite_components(&self, bundle_id: &str) -> Option<&[String]> {
        let bundle = self.bundles.get(bundle_id)?;
        (bundle.kind == BundleKind::Composite).then_some(bundle.members.as_slice())
    }

    /// A volume's members, or `None` for any other bundle.
    pub fn volume_components(&self, bundle_id: &str) -> Option<&[String]> {
        let bundle = self.bundles.get(bundle_id)?;
        (bundle.kind == BundleKind::Volume).then_some(bundle.members.as_slice())
    }

    /// The derivation of a bundle the converter computes, or `None`.
    pub fn derivation(&self, bundle_id: &str) -> Option<&Derivation> {
        self.bundles.get(bundle_id)?.derivation.as_ref()
    }

    /// The derivation of a scalar the per-frame stage computes from other
    /// planes (the equivalent potential temperature, the precipitation
    /// type, the turbulence index); the precipitation rate is the per-file
    /// stage's and is not one.
    pub fn scalar_derivation(&self, bundle_id: &str) -> Option<&Derivation> {
        let bundle = self.bundles.get(bundle_id)?;
        let derivation = bundle.derivation.as_ref()?;
        (bundle.kind == BundleKind::Scalar && derivation.kind != DerivationKind::PrecipitationRate)
            .then_some(derivation)
    }

    /// Whether a vector bundle is derived rather than read.
    pub fn is_derived_vector(&self, bundle_id: &str) -> bool {
        self.bundles
            .get(bundle_id)
            .is_some_and(|bundle| bundle.kind == BundleKind::Vector && bundle.derivation.is_some())
    }

    /// The source inputs one vector bundle is built from: a wind pair is
    /// its own two components, a derived pair its derivation's inputs.
    pub fn vector_input_ids(&self, bundle_id: &str) -> Vec<String> {
        match self.bundles.get(bundle_id) {
            Some(bundle) if bundle.kind == BundleKind::Vector => bundle
                .derivation
                .as_ref()
                .map_or_else(|| bundle.members.clone(), |derivation| derivation.inputs.clone()),
            _ => vec![bundle_id.to_string()],
        }
    }

    /// The source input variables one published bundle is built from. A
    /// composite reads its producer's inputs less the optional ones the
    /// source does not fetch; the precipitation rate reads the first of its
    /// derivation's inputs the source lists.
    pub fn bundle_input_ids(&self, source: &SourceSpec, bundle_id: &str) -> Vec<String> {
        let Some(bundle) = self.bundles.get(bundle_id) else {
            return vec![bundle_id.to_string()];
        };
        if let Some(derivation) = &bundle.derivation {
            if derivation.kind == DerivationKind::PrecipitationRate {
                return derivation
                    .inputs
                    .iter()
                    .filter(|id| source.input_variable_ids.contains(id))
                    .take(1)
                    .cloned()
                    .collect();
            }
            return derivation.inputs.clone();
        }
        match bundle.kind {
            BundleKind::Composite => bundle
                .inputs
                .iter()
                .filter(|id| !bundle.optional_inputs.contains(id) || source.input_variable_ids.contains(id))
                .cloned()
                .collect(),
            BundleKind::Vector | BundleKind::Volume => bundle.members.clone(),
            BundleKind::Scalar => vec![bundle_id.to_string()],
        }
    }

    /// The precipitation-rate bundle a source derives in its per-file
    /// stage, as `(bundle id, raw input id)`: the listed scalar whose
    /// derivation is the rate, on a source whose precipitation arrives
    /// accumulated, averaged or as an interval total. `None` on a source
    /// that reads the rate as it is.
    pub fn derived_precipitation(&self, source: &SourceSpec) -> Result<Option<(String, String)>> {
        if !(source.accumulated_precipitation || source.averaged_precipitation || source.interval_precipitation) {
            return Ok(None);
        }
        let mut found: Option<(String, String)> = None;
        for bundle_id in &source.bundle_scalar_ids {
            let is_rate = self
                .derivation(bundle_id)
                .is_some_and(|derivation| derivation.kind == DerivationKind::PrecipitationRate);
            if !is_rate {
                continue;
            }
            let Some(raw_id) = self.bundle_input_ids(source, bundle_id).into_iter().next() else {
                continue;
            };
            if found.is_some() {
                return Err(EncodeError::conversion(format!(
                    "{} derives more than one precipitation rate",
                    source.manifest_model
                )));
            }
            found = Some((bundle_id.clone(), raw_id));
        }
        Ok(found)
    }

    /// Every bundle a source can publish, in manifest order: its scalars,
    /// then each listed vector whose inputs it fetches, then each listed
    /// composite whose producer's channels it fetches, then each listed
    /// volume whose members it fetches. A derived scalar counts the same
    /// way — listed, it ships only when its inputs are.
    pub fn published_bundle_ids<'a>(&self, source: &'a SourceSpec) -> Vec<&'a str> {
        let fetched = |bundle_id: &str| {
            self.bundle_input_ids(source, bundle_id)
                .iter()
                .all(|id| source.input_variable_ids.contains(id))
        };
        source
            .bundle_scalar_ids
            .iter()
            .chain(&source.bundle_vector_ids)
            .chain(&source.bundle_composite_ids)
            .chain(&source.bundle_volume_ids)
            .map(String::as_str)
            .filter(|bundle_id| fetched(bundle_id))
            .collect()
    }

    /// The published scalars of `scalar_ids` whose series has no analysis
    /// frame: the rate a source derives by de-accumulating or de-averaging,
    /// and any scalar read from a record the source lists under
    /// `optional_at_analysis`.
    pub fn analysis_optional_ids<'a>(&self, source: &SourceSpec, scalar_ids: &[&'a str]) -> Vec<&'a str> {
        scalar_ids
            .iter()
            .copied()
            .filter(|bundle_id| {
                let derived_rate = self
                    .derivation(bundle_id)
                    .is_some_and(|derivation| derivation.kind == DerivationKind::PrecipitationRate)
                    && (source.accumulated_precipitation || source.averaged_precipitation);
                derived_rate
                    || self
                        .bundle_input_ids(source, bundle_id)
                        .iter()
                        .any(|input_id| source.optional_at_analysis.contains(input_id))
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::{BundleKind, Companion, Conversion, Decimation, DerivationKind, GdalRule, Predictor, Registry};
    use serde_json::{json, Value};

    #[test]
    fn the_snapshot_parses_into_its_representative_entries() {
        let registry = Registry::embedded();
        assert_eq!(registry.schema, 1);

        // A linear codebook and a log1p one, profile for profile.
        let tmp2m = registry.variable("tmp2m").unwrap();
        assert_eq!(tmp2m.label, "2 meter temperature");
        assert_eq!(tmp2m.output_unit, "°C");
        assert_eq!(
            (tmp2m.grib2_discipline, tmp2m.grib2_category, tmp2m.grib2_number, tmp2m.grib2_level_type),
            (0, 0, 0, 103)
        );
        assert_eq!(tmp2m.grib2_level_value, Some(2.0));
        assert_eq!(tmp2m.conversion, Conversion::Celsius);
        assert_eq!(tmp2m.predictor, Predictor::Delta);
        assert_eq!(tmp2m.companions, [Companion::Poster, Companion::Video]);
        let quality = registry.codebook("quality", "tmp2m").unwrap();
        let linear = quality.as_linear().unwrap();
        assert_eq!((linear.minimum, linear.step, linear.maximum_code()), (-60.0, 0.5, 220));
        assert_eq!(quality.metadata()["type"], json!("linear"));
        assert_eq!(registry.codebook("compact", "tmp2m").unwrap().as_linear().unwrap().step, 1.0);
        let prate = registry.codebook("balanced", "prate").unwrap();
        assert!(prate.as_linear().is_none());
        assert_eq!(prate.metadata()["maximumCode"], json!(125));
        assert_eq!(prate.metadata()["type"], json!("log1p"));
        assert!(registry.codebook("quality", "tp").is_err(), "input-only");
        assert!(registry.codebook("lossless", "tmp2m").is_err());
        assert!(registry.knows_profile("balanced") && !registry.knows_profile("lossless"));

        // The surface value of a layer is `scaledValue × 10^−scaleFactor`.
        assert_eq!(registry.variable("tsoil2").unwrap().grib2_level_value, Some(0.1));
        assert_eq!(registry.variable("tcdc").unwrap().grib2_level_value, None);
        let aod = registry.variable("aoddust").unwrap();
        assert_eq!(aod.grib2_aerosol.unwrap().aerosol_type, 62001);
        assert_eq!(aod.grib2_aerosol.unwrap().wavelength_second, Some((9, 565)));
        assert_eq!(registry.variable("pm25").unwrap().grib2_aerosol.unwrap().wavelength_first, None);
        assert_eq!(registry.variable("dustr").unwrap().producer_id.as_deref(), Some("shachen"));
        assert_eq!(registry.variable("ir039").unwrap().decimation, Decimation::Peak);
        assert_eq!(registry.variable("prmsl").unwrap().companions, []);
        assert!(registry.variable("orog").unwrap().static_ && registry.is_static("orog"));
        assert!(registry.is_raw("prate").unwrap() && !registry.is_raw("tmp2m").unwrap());
        assert!(registry.variable("thetae850").unwrap().matching.gdal.is_empty());
        assert_eq!(
            registry.variable("rh850").unwrap().matching.gdal,
            [GdalRule::Isobaric { element: "RH".into(), hpa: 850 }]
        );
        assert!(registry.variable("nothing").is_err());

        // A vector bundle and a derived one; a scalar is any id not listed.
        assert_eq!(registry.vector_components("wind10m"), Some(("ugrd10m", "vgrd10m")));
        assert_eq!(registry.bundle_variable_ids("wind10m"), ["ugrd10m", "vgrd10m"]);
        assert_eq!(registry.vector_input_ids("wind10m"), ["ugrd10m", "vgrd10m"]);
        assert!(!registry.is_derived_vector("wind10m"));
        assert!(registry.is_derived_vector("qflux850"));
        assert_eq!(registry.vector_input_ids("qflux850"), ["spfh850", "ugrd850", "vgrd850"]);
        assert_eq!(registry.bundle_variable_ids("qflux850"), ["uqflx850", "vqflx850"]);
        let thetae = registry.scalar_derivation("thetae850").unwrap();
        assert_eq!((thetae.kind, thetae.level_hpa), (DerivationKind::ThetaE, Some(850)));
        assert!(registry.scalar_derivation("prate").is_none(), "the rate is the per-file stage's");
        assert_eq!(registry.derivation("prate").unwrap().kind, DerivationKind::PrecipitationRate);
        assert_eq!(registry.bundle_variable_ids("tmp2m"), ["tmp2m"]);
        assert_eq!(registry.bundle("refl3d").unwrap().kind, BundleKind::Volume);
        assert_eq!(registry.composite_components("dustrgb").unwrap(), ["dustr", "dustg", "dustb"]);
        assert_eq!(registry.volume_components("cloud3d").unwrap().len(), 24);

        // A source with a companion family, and the inputs resolved through it.
        let gfs = registry.source("gfs").unwrap();
        assert_eq!(gfs.manifest_model, "GFS");
        assert_eq!(gfs.companion_files.len(), 1);
        assert_eq!(gfs.companion_files[0].id, "wave");
        assert!(gfs.companion_files[0].variable_ids.contains(&"htsgw".to_string()));
        assert_eq!(gfs.companion_of("htsgw").map(|companion| companion.id.as_str()), Some("wave"));
        assert_eq!(gfs.steps, [(120, 1), (240, 3)]);
        assert_eq!(gfs.production_grid, (1440, 721));
        assert_eq!(gfs.cat_calibration[0].0, 300);
        assert_eq!(registry.bundle_input_ids(gfs, "prate"), ["prate"]);
        assert_eq!(registry.derived_precipitation(gfs).unwrap(), None);
        let ecmwf = registry.source("ecmwf").unwrap();
        assert_eq!(
            registry.derived_precipitation(ecmwf).unwrap(),
            Some(("prate".to_string(), "tp".to_string()))
        );
        assert_eq!(registry.analysis_optional_ids(ecmwf, &["tmp2m", "prate", "gust"]), ["prate", "gust"]);
        let cfs = registry.source("cfs").unwrap();
        assert_eq!(cfs.companion_files[0].production_grid, Some((360, 181)));
        assert!(cfs.companion_files[0].own_grid());
        assert_eq!(registry.source("mrms").unwrap().downsample.map(|downsample| downsample.factor), Some(2));
        assert_eq!(registry.source("hrrr").unwrap().regrid.map(|regrid| regrid.step), Some(0.03));
        assert_eq!(registry.source("efas").unwrap().latest_filename, None);
        assert_eq!(registry.source_by_manifest_model("HRRR").unwrap().id, "hrrr");
        assert!(registry.source("nothing").is_err());
        assert_eq!(registry.sources().count(), 26);
        assert_eq!(registry.variables().count(), 229);
    }

    fn snapshot() -> Value {
        serde_json::from_str(super::EMBEDDED_DOCUMENT).unwrap()
    }

    fn refusal(document: &Value) -> String {
        Registry::parse(&document.to_string()).expect_err("refused").to_string()
    }

    #[test]
    fn a_document_outside_the_closed_sets_is_refused_whole() {
        let mut document = snapshot();
        document["registrySchema"] = json!(2);
        assert!(refusal(&document).contains("registrySchema 2"));

        let mut document = snapshot();
        document["variables"]["tmp2m"]["conversion"]["rule"] = json!("fahrenheit");
        let message = refusal(&document);
        assert!(message.contains("variables.tmp2m") && message.contains("fahrenheit"), "{message}");

        let mut document = snapshot();
        document["variables"]["tmp2m"]["match"]["gdal"][0]["rule"] = json!("altitude");
        assert!(refusal(&document).contains("altitude"));

        let mut document = snapshot();
        document["variables"]["ir039"]["decimation"] = json!("mean");
        assert!(refusal(&document).contains("mean"));

        let mut document = snapshot();
        document["variables"]["tmp2m"]["quantization"]["quality"]["type"] = json!("cubic");
        assert!(refusal(&document).contains("cubic"));

        let mut document = snapshot();
        document["bundles"]["wind10m"]["members"] = json!(["ugrd10m", "vgrd10m", "gust"]);
        assert!(refusal(&document).contains("not two"));

        let mut document = snapshot();
        document["bundles"]["wind10m"]["members"][1] = json!("vgrd9m");
        let message = refusal(&document);
        assert!(message.contains("bundles.wind10m") && message.contains("vgrd9m"), "{message}");

        let mut document = snapshot();
        document["bundles"]["thetae850"]["derivation"]["kind"] = json!("theta-w");
        assert!(refusal(&document).contains("theta-w"));

        let mut document = snapshot();
        document["sources"]["gfs"]["bundleScalarIds"][0] = json!("tmp3m");
        let message = refusal(&document);
        assert!(message.contains("sources.gfs") && message.contains("tmp3m"), "{message}");

        let mut document = snapshot();
        document["sources"]["gfs"]["inputVariableIds"][0] = json!("tmp3m");
        assert!(refusal(&document).contains("tmp3m"));

        // A key a reader does not know is ignored, not refused.
        let mut document = snapshot();
        document["variables"]["tmp2m"]["colour"] = json!("red");
        document["sources"]["gfs"]["mascot"] = json!("goose");
        document["bundles"]["wind10m"]["note"] = json!("gusty");
        assert!(Registry::parse(&document.to_string()).is_ok());
    }
}
