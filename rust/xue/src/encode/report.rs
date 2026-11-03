//! The build report and the manifest a conversion writes last.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{json, Map, Value};
use time::OffsetDateTime;

use crate::encode::convert::{
    bundle_grid_family, zstd_version, ConvertOptions, PlaneStats,
};
use crate::encode::errors::{EncodeError, Result};
use crate::encode::grid::GridInfo;
use crate::encode::manifest::{build_bin_manifest, write_json};
use crate::encode::metadata::lead_hours;
use crate::encode::registry::Registry;
use crate::encode::sources::SourceSpec;

/// The bundles the build report's named statistics describe
/// (`temperatureMaxAbsError`, `precipitationOverflowPoints`,
/// `windMaxAbsError`): the reference report names the same three, so a
/// report compares key for key whatever the source published. Names in a
/// report, not a rule the pipeline dispatches on.
const REPORT_TEMPERATURE_ID: &str = "tmp2m";
const REPORT_PRECIPITATION_ID: &str = "prate";
const REPORT_WIND_BUNDLE_ID: &str = "wind10m";

/// What a bundle or tier report weighs as delivered: its container where one
/// was written, its store otherwise.
pub(super) fn delivered_byte_length(report: &Value) -> Option<u64> {
    report["byteLength"]
        .as_u64()
        .or_else(|| report["zarr"]["byteLength"].as_u64())
}

pub(super) fn bundle_manifest_entry(
    bundle: &Value,
    manifest_dir: &Path,
    poster: Option<&Value>,
    variants: Option<&Vec<Value>>,
    grid: &GridInfo,
) -> Result<Value> {
    let relative = |path: &str| -> Result<String> {
        let path = Path::new(path);
        Ok(path
            .strip_prefix(manifest_dir)
            .map_err(|_| {
                EncodeError::manifest(format!(
                    "{} is not inside the manifest directory {}",
                    path.display(),
                    manifest_dir.display()
                ))
            })?
            .to_string_lossy()
            .replace('\\', "/"))
    };
    // The `zarr` / `series` descriptor from a report's store block: the
    // store's root relative to the manifest, its size and the CRC-32 of its
    // root document. Mirrors `_zarr_descriptor`.
    let descriptor = |store: &Value| -> Result<Value> {
        Ok(json!({
            "path": relative(store["path"].as_str().unwrap_or_default())?,
            "byteLength": store["byteLength"],
            "crc32": store["crc32"],
        }))
    };
    // A report whose container was not written carries no `output`; its
    // entry names the store alone (`_container_fields`).
    let has_container = |report: &Value| report.get("output").is_some();
    let mut entry = Map::new();
    entry.insert("variable".into(), bundle["variable"].clone());
    // The bundle's full grid, the block a store's `attributes.xue.grid`
    // carries too, so the STAC Item states the authoritative grid.
    entry.insert("grid".into(), Value::Object(grid.metadata()));
    if has_container(bundle) {
        entry.insert(
            "path".into(),
            json!(relative(bundle["output"].as_str().unwrap_or_default())?),
        );
        entry.insert("byteLength".into(), bundle["byteLength"].clone());
        entry.insert("crc32".into(), bundle["crc32"].clone());
    }
    if let Some(variants) = variants.filter(|variants| !variants.is_empty()) {
        // Resolution ladder: STREAM-INF style alternate renditions of the same
        // variable; the top-level entry stays the canonical full-res tier.
        // Keys in the order `_variant_manifest_entry` writes them.
        let tiers: Vec<Value> = variants
            .iter()
            .map(|variant| {
                let mut tier = Map::new();
                if has_container(variant) {
                    tier.insert(
                        "path".into(),
                        json!(relative(variant["output"].as_str().unwrap_or_default())?),
                    );
                }
                tier.insert("width".into(), variant["width"].clone());
                tier.insert("height".into(), variant["height"].clone());
                if has_container(variant) {
                    tier.insert("byteLength".into(), variant["byteLength"].clone());
                    tier.insert("crc32".into(), variant["crc32"].clone());
                }
                tier.insert("bandwidth".into(), variant["bandwidth"].clone());
                if variant.get("zarr").is_some() {
                    tier.insert("zarr".into(), descriptor(&variant["zarr"])?);
                }
                Ok(Value::Object(tier))
            })
            .collect::<Result<_>>()?;
        entry.insert("variants".into(), Value::Array(tiers));
    }
    if let Some(poster) = poster {
        entry.insert(
            "poster".into(),
            json!({
                "path": relative(poster["path"].as_str().unwrap_or_default())?,
                "width": poster["width"],
                "height": poster["height"],
                "byteLength": poster["byteLength"],
                "crc32": poster["crc32"],
                "metadataJson": poster["metadataJson"],
            }),
        );
    }
    if bundle.get("zarr").is_some() {
        entry.insert("zarr".into(), descriptor(&bundle["zarr"])?);
    }
    if bundle.get("series").is_some() {
        entry.insert("series".into(), descriptor(&bundle["series"])?);
    }
    Ok(Value::Object(entry))
}

/// The build report, key for key the reference report
/// (`binconvert.convert_bin`), after the quantization acceptance check.
#[allow(clippy::too_many_arguments)]
pub(super) fn build_report(
    output_dir: &Path,
    source: &SourceSpec,
    grid: &GridInfo,
    options: &ConvertOptions,
    bundle_reports: &[Value],
    variant_reports: &BTreeMap<&str, Vec<Value>>,
    poster_reports: &BTreeMap<&str, Value>,
    stats: &[PlaneStats],
    available_vector_ids: &[&str],
    encoded_variable_ids: &[String],
    registry: &Registry,
) -> Result<Map<String, Value>> {
    let stat_max = |variables: &[&str]| {
        stats
            .iter()
            .filter(|item| variables.contains(&item.variable_id.as_str()))
            .map(|item| item.max_abs_error)
            .fold(0.0f64, f64::max)
    };
    let stat_clamped = |variables: &[&str]| -> u64 {
        stats
            .iter()
            .filter(|item| variables.contains(&item.variable_id.as_str()))
            .map(|item| item.clamped_points)
            .sum()
    };
    let mut report = Map::new();
    report.insert("outputDir".into(), json!(output_dir.display().to_string()));
    report.insert("model".into(), json!(&source.manifest_model));
    report.insert("grid".into(), Value::Object(grid.metadata()));
    report.insert("profile".into(), json!(options.profile));
    report.insert("zstdLevel".into(), json!(options.zstd_level));
    report.insert("zstdVersion".into(), json!(zstd_version()));
    report.insert("bundles".into(), json!(bundle_reports));
    report.insert(
        "variants".into(),
        json!(variant_reports.values().flatten().collect::<Vec<_>>()),
    );
    report.insert(
        "posters".into(),
        json!(poster_reports.values().collect::<Vec<_>>()),
    );
    report.insert("videos".into(), json!([]));
    report.insert(
        "byteLength".into(),
        json!(bundle_reports
            .iter()
            .filter_map(|bundle| delivered_byte_length(bundle))
            .sum::<u64>()),
    );
    // The report's named statistics are the reference report's keys
    // (`binconvert.convert_bin`): the 2 m temperature, the rate and the
    // 10 m wind pair by name, whatever the source published.
    report.insert(
        "temperatureMaxAbsError".into(),
        json!(stat_max(&[REPORT_TEMPERATURE_ID])),
    );
    report.insert(
        "temperatureClampedPoints".into(),
        json!(stat_clamped(&[REPORT_TEMPERATURE_ID])),
    );
    report.insert(
        "precipitationOverflowPoints".into(),
        json!(stats
            .iter()
            .filter(|item| item.variable_id == REPORT_PRECIPITATION_ID)
            .map(|item| item.overflow_points)
            .sum::<u64>()),
    );
    if available_vector_ids.contains(&REPORT_WIND_BUNDLE_ID) {
        let (u, v) = registry
            .vector_components(REPORT_WIND_BUNDLE_ID)
            .expect("a built vector bundle is a vector");
        report.insert("windMaxAbsError".into(), json!(stat_max(&[u, v])));
        report.insert("windClampedPoints".into(), json!(stat_clamped(&[u, v])));
    }

    // Quantization acceptance runs over the *encoded* variables (prate is the
    // derived output on ECMWF/sflux; the raw input has no codebook).
    for variable_id in encoded_variable_ids {
        let book = registry.codebook(&options.profile, variable_id)?;
        let Some(linear) = book.as_linear() else {
            continue;
        };
        let worst = stat_max(&[variable_id]);
        if worst > 0.5001 * linear.step {
            return Err(EncodeError::conversion(format!(
                "{variable_id} quantization error exceeds half a step"
            )));
        }
    }
    Ok(report)
}

/// The run's manifest, over the bundles this build wrote, at `manifest_path`.
/// No live pointer: it carries the CRC32 of the finished manifest, which
/// `xuebuild/native.py` completes with the video descriptors first, so the
/// pointer is always written on the Python side.
#[allow(clippy::too_many_arguments)]
pub(super) fn write_manifest<'g>(
    manifest_path: &Path,
    options: &ConvertOptions,
    source: &SourceSpec,
    bundle_reports: &[Value],
    variant_reports: &BTreeMap<&str, Vec<Value>>,
    poster_reports: &BTreeMap<&str, Value>,
    grid_for: impl Fn(Option<&str>) -> &'g GridInfo,
    offsets: &[i64],
    unit_seconds: i64,
    run_time: OffsetDateTime,
    registry: &Registry,
) -> Result<()> {
    // The source's core bundles (`core_bundle_ids`: the tmp2m/prate pair
    // on a forecast, the reflectivity on a radar mosaic) are what a
    // complete run must publish. A restricted build ships only the
    // bundles it was asked for.
    let require_core = options.bundle_ids.is_none();
    let manifest_dir = manifest_path.parent().unwrap_or(Path::new("."));
    let entries: Vec<Value> = bundle_reports
        .iter()
        .map(|bundle| {
            let variable = bundle["variable"].as_str().unwrap_or_default();
            // The bundle's grid is its family's; `family_of` is keyed by
            // variable id, and a vector bundle's id is not one.
            let family = bundle_grid_family(registry, source, variable)?;
            bundle_manifest_entry(
                bundle,
                manifest_dir,
                poster_reports.get(variable),
                variant_reports.get(variable),
                grid_for(family),
            )
        })
        .collect::<Result<_>>()?;
    let expected_hours = if offsets.len() > 1 {
        lead_hours(*offsets.last().expect("non-empty axis"), unit_seconds)
    } else {
        options.expected_hours
    };
    let payload = build_bin_manifest(
        run_time,
        entries,
        expected_hours,
        &source.manifest_model,
        &source.product,
        require_core,
        registry,
    )?;
    write_json(manifest_path, &payload, options.force)?;
    log!(options, "wrote manifest {}", manifest_path.display());
    Ok(())
}
