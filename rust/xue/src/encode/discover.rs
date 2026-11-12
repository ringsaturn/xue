//! Frame discovery: the input files a conversion reads and the frames each
//! one holds, for a record-per-file source and for a series-file source.

use std::path::{Path, PathBuf};

use time::OffsetDateTime;

use crate::encode::binformat::HOUR_SECONDS;
use crate::encode::convert::{grid_family_of, ConvertOptions, PlaneSources};
use crate::encode::errors::{EncodeError, Result};
use crate::encode::gribindex::inspect_grib_fast;
use crate::encode::inspect::{inspect_grib_multi, raster_expression, SUPPORTED_EXTENSIONS};
use crate::encode::model::{PlaneSource, SourceFrame};
use crate::encode::observation::inspect_observation;
use crate::encode::registry::Registry;
use crate::encode::sources::{family_frame_path, SourceSpec};

/// One file's frames, keyed by variable id, in the source's own order.
pub(super) type FileFrames = Vec<(String, SourceFrame)>;

pub(super) fn frame_of<'a>(frames: &'a FileFrames, variable_id: &str) -> Option<&'a SourceFrame> {
    frames
        .iter()
        .find(|(id, _)| id == variable_id)
        .map(|(_, frame)| frame)
}

// -- input discovery ---------------------------------------------------------

/// The GRIB files a conversion reads: one file, every file in a directory, or
/// exactly the files given.
pub fn discover_inputs(inputs: &[PathBuf]) -> Result<Vec<PathBuf>> {
    if inputs.is_empty() {
        return Err(EncodeError::conversion("no GRIB files given"));
    }
    if inputs.len() == 1 {
        let input = &inputs[0];
        if input.is_file() {
            return Ok(vec![input.clone()]);
        }
        if !input.is_dir() {
            return Err(EncodeError::conversion(format!(
                "input does not exist: {}",
                input.display()
            )));
        }
        let mut files: Vec<PathBuf> = std::fs::read_dir(input)
            .map_err(|error| {
                EncodeError::conversion(format!("cannot list {}: {error}", input.display()))
            })?
            .filter_map(std::result::Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.is_file()
                    && path.extension().is_some_and(|extension| {
                        SUPPORTED_EXTENSIONS
                            .contains(&extension.to_string_lossy().to_lowercase().as_str())
                    })
            })
            .collect();
        files.sort();
        if files.is_empty() {
            return Err(EncodeError::conversion(format!(
                "no GRIB files found in {}",
                input.display()
            )));
        }
        return Ok(files);
    }
    let mut files = inputs.to_vec();
    files.sort();
    if let Some(missing) = files.iter().find(|path| !path.is_file()) {
        return Err(EncodeError::conversion(format!(
            "input does not exist: {}",
            missing.display()
        )));
    }
    Ok(files)
}

/// The frames a conversion reads, named by the primary file of each.
///
/// A frame of a source with a grid family is more than one file under one run
/// directory: the primary file, and a sibling of the same name under each
/// family's own directory (`sources::family_frame_path`). Listing the run
/// directory lists the primary files, as it always did. A build narrowed to
/// one family's bundles fetches no primary file at all, so its frames are
/// listed from that family's directory instead and named by the primary path
/// they would have had; nothing reads that path unless a variable of the
/// primary file is read. Mirrors `discover_frames` in
/// `xuebuild/discover.py`.
pub(super) fn discover_frames(inputs: &[PathBuf], source: &SourceSpec) -> Result<Vec<PathBuf>> {
    match discover_inputs(inputs) {
        Ok(paths) => Ok(paths),
        Err(error) => {
            if inputs.len() != 1 || !inputs[0].is_dir() {
                return Err(error);
            }
            for companion in source.grid_families() {
                let directory = inputs[0].join(&companion.id);
                if directory.is_dir() {
                    return Ok(discover_inputs(&[directory])?
                        .into_iter()
                        .map(|path| {
                            inputs[0].join(path.file_name().unwrap_or(path.as_os_str()))
                        })
                        .collect());
                }
            }
            Err(error)
        }
    }
}

/// Inspect every file once for all variables, in parallel across files.
///
/// Variables in `optional_at_analysis` may be absent from the f000 file only,
/// and one with a first hour of its own (`first_hours`) from every file
/// before it, where it is not read even when present.
/// Inspection uses the GRIB2 header index; the first file is cross-checked
/// against `reference_frames` (a real GDAL pass) and a run whose files the
/// header index cannot parse falls back to GDAL inspection.
///
/// `families` names, per variable, the grid family it is read from where a
/// source has one: those variables are inspected in the frame's sibling file
/// (`sources::family_frame_path`) instead of in the frame itself, and one
/// frame's answers are merged into the one record set the rest of the
/// converter reads. A frame whose family file is missing is refused before
/// anything is inspected, since it is a fetch that did not finish rather
/// than a file to fall back over. Mirrors `prepare_frames_all` in
/// `xuebuild/discover.py`.
pub(super) fn prepare_frames_all(
    paths: &[PathBuf],
    variable_ids: &[&str],
    optional_at_analysis: &[&str],
    first_hours: &[(String, i64)],
    reference_frames: &FileFrames,
    cadence_seconds: Option<i64>,
    families: &[(String, Option<&str>)],
    static_ids: &[&str],
    options: &ConvertOptions,
    registry: &Registry,
) -> Result<Vec<FileFrames>> {
    use rayon::prelude::*;

    // The variables of each family, in first-seen order.
    let mut by_family: Vec<(Option<&str>, Vec<&str>)> = Vec::new();
    for variable_id in variable_ids {
        let family = families
            .iter()
            .find(|(id, _)| id == variable_id)
            .and_then(|(_, family)| *family);
        match by_family.iter_mut().find(|(held, _)| *held == family) {
            Some((_, ids)) => ids.push(variable_id),
            None => by_family.push((family, vec![variable_id])),
        }
    }
    for (family, _) in &by_family {
        // The primary file's absence is the inspector's own error.
        let Some(family) = family else { continue };
        for path in paths {
            let family_path = family_frame_path(path, Some(family));
            if !family_path.is_file() {
                return Err(EncodeError::conversion(format!(
                    "the {family} records of {} are missing: {} does not exist",
                    path.file_name().unwrap_or(path.as_os_str()).to_string_lossy(),
                    family_path.display()
                )));
            }
        }
    }
    // A static record is expected only at the first frame, so it is optional
    // everywhere the inspector looks; its presence at the first frame is
    // required below.
    let mut inspection_optional: Vec<&str> = optional_at_analysis.to_vec();
    inspection_optional.extend_from_slice(static_ids);
    // A variable with a first hour of its own is missing before it; its
    // presence from then on is required below.
    let first_hour_of = |variable_id: &str| {
        first_hours
            .iter()
            .find(|(id, _)| id == variable_id)
            .map(|(_, hour)| *hour)
    };
    inspection_optional.extend(variable_ids.iter().copied().filter(|id| first_hour_of(id).is_some()));
    let inspect_all = |inspect: &(dyn Fn(&Path, &[&str]) -> Result<FileFrames> + Sync)| -> Result<Vec<FileFrames>> {
        paths
            .par_iter()
            .map(|path| {
                let mut frames: FileFrames = Vec::new();
                for (family, family_ids) in &by_family {
                    let family_path = family_frame_path(path, *family);
                    for entry in inspect(&family_path, family_ids)? {
                        frames.retain(|(id, _)| *id != entry.0);
                        frames.push(entry);
                    }
                }
                Ok(frames)
            })
            .collect::<Result<Vec<_>>>()
    };

    let fast: Result<Vec<FileFrames>> = inspect_all(&|path, ids| {
        inspect_grib_fast(path, ids, &inspection_optional, registry)
    })
    .and_then(|per_file| {
        check_reference_frames(&per_file[0], reference_frames, variable_ids, registry)?;
        Ok(per_file)
    });
    let mut per_file = match fast {
        Ok(per_file) => per_file,
        Err(error) => {
            eprintln!(
                "WARNING GRIB2 header index unavailable ({error}); falling back to GDAL inspection"
            );
            inspect_all(&|path, ids| inspect_grib_multi(path, ids, &inspection_optional, registry))?
        }
    };
    log!(options, "indexed {} files", per_file.len());
    // Before its first hour a variable is not read, whatever the file holds.
    for frames in &mut per_file {
        frames.retain(|(variable_id, frame)| {
            first_hour_of(variable_id).is_none_or(|hour| frame.lead_seconds >= hour * HOUR_SECONDS)
        });
    }
    if let Some(cadence_seconds) = cadence_seconds {
        per_file = snap_observation_frames(per_file, cadence_seconds)?;
    }

    for frames in &per_file {
        let mut leads: Vec<i64> = frames.iter().map(|(_, frame)| frame.lead_seconds).collect();
        leads.dedup();
        if leads.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "variables disagree on the lead time in {}",
                frames[0].1.path.display()
            )));
        }
    }
    per_file.sort_by_key(|frames| {
        frame_of(frames, variable_ids[0])
            .map(|frame| frame.lead_seconds)
            .unwrap_or(i64::MAX)
    });
    let first_lead = per_file
        .iter()
        .filter_map(|frames| frames.first())
        .map(|(_, frame)| frame.lead_seconds)
        .min()
        .unwrap_or(0);
    for variable_id in variable_ids {
        if static_ids.contains(variable_id) {
            // A static record is fetched at the first frame alone; its
            // absence anywhere is expected, its absence there is a fetch
            // that did not finish.
            let present: Vec<i64> = per_file
                .iter()
                .filter_map(|frames| frame_of(frames, variable_id))
                .map(|frame| frame.lead_seconds)
                .collect();
            if present.is_empty() || present.iter().min() != Some(&first_lead) {
                return Err(EncodeError::conversion(format!(
                    "missing static {variable_id} record at the first frame"
                )));
            }
        } else {
            for frames in &per_file {
                if frame_of(frames, variable_id).is_some() {
                    continue;
                }
                let lead = frames[0].1.lead_seconds;
                let before_first_hour = first_hour_of(variable_id).is_some_and(|hour| lead < hour * HOUR_SECONDS);
                if before_first_hour {
                    continue;
                }
                if !optional_at_analysis.contains(variable_id) || lead != 0 {
                    return Err(EncodeError::conversion(format!(
                        "missing {variable_id} record at forecast hour {}",
                        lead / HOUR_SECONDS
                    )));
                }
            }
        }
        let mut leads: Vec<i64> = per_file
            .iter()
            .filter_map(|frames| frame_of(frames, variable_id))
            .map(|frame| frame.lead_seconds)
            .collect();
        let count = leads.len();
        leads.sort_unstable();
        leads.dedup();
        if leads.len() != count {
            return Err(EncodeError::conversion(format!(
                "duplicate lead times for {variable_id}"
            )));
        }
        let mut runs: Vec<i64> = per_file
            .iter()
            .filter_map(|frames| frame_of(frames, variable_id))
            .map(|frame| frame.run_time.unix_timestamp())
            .collect();
        runs.sort_unstable();
        runs.dedup();
        if runs.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "input files contain multiple run times for {variable_id}"
            )));
        }
    }
    Ok(per_file)
}

/// Re-key the frames of a fetched observation onto the window's axis — the
/// port of `snap_observation_frames` in `xuebuild/discover.py`.
///
/// A GRIB observation names only its own time: each MRMS composite is its
/// own reference time, some forty seconds past a two-minute mark, with a
/// zero forecast time. Every observation time is snapped *down* to its
/// `cadence_seconds` slot (the products of one frame are stamped apart and
/// agree on the slot), the run time is the whole hour the first slot falls
/// in, and a frame's lead is its slot's distance from it.
fn snap_observation_frames(per_file: Vec<FileFrames>, cadence_seconds: i64) -> Result<Vec<FileFrames>> {
    let mut slots = Vec::with_capacity(per_file.len());
    for frames in &per_file {
        let mut file_slots: Vec<i64> = frames
            .iter()
            .map(|(_, frame)| frame.valid_time.unix_timestamp().div_euclid(cadence_seconds) * cadence_seconds)
            .collect();
        file_slots.sort_unstable();
        file_slots.dedup();
        if file_slots.len() != 1 {
            return Err(EncodeError::conversion(format!(
                "variables disagree on the observation slot in {}",
                frames[0].1.path.display()
            )));
        }
        slots.push(file_slots[0]);
    }
    let window_start = slots.iter().copied().min().unwrap_or(0).div_euclid(HOUR_SECONDS) * HOUR_SECONDS;
    let run_time = OffsetDateTime::from_unix_timestamp(window_start)
        .map_err(|error| EncodeError::conversion(format!("invalid observation time: {error}")))?;
    let mut snapped = Vec::with_capacity(per_file.len());
    for (frames, slot) in per_file.into_iter().zip(slots) {
        let valid_time = OffsetDateTime::from_unix_timestamp(slot)
            .map_err(|error| EncodeError::conversion(format!("invalid observation time: {error}")))?;
        snapped.push(
            frames
                .into_iter()
                .map(|(variable_id, frame)| {
                    (
                        variable_id,
                        SourceFrame {
                            run_time,
                            valid_time,
                            lead_seconds: slot - window_start,
                            ..frame
                        },
                    )
                })
                .collect(),
        );
    }
    Ok(snapped)
}

/// Raise if the GRIB2 header index disagrees with GDAL on the per-run
/// reference file. The header index locates the bands every extraction reads,
/// so a mismatch must never pass silently; the error drops the whole run into
/// the GDAL fallback path.
fn check_reference_frames(
    fast_frames: &FileFrames,
    reference_frames: &FileFrames,
    variable_ids: &[&str],
    registry: &Registry,
) -> Result<()> {
    for variable_id in variable_ids {
        let spec = registry.variable(variable_id)?;
        let fast = frame_of(fast_frames, variable_id);
        let reference = frame_of(reference_frames, variable_id);
        match (fast, reference) {
            (None, None) => continue,
            (None, Some(_)) | (Some(_), None) => {
                return Err(EncodeError::conversion(format!(
                    "GRIB2 header index and GDAL disagree on the presence of {variable_id}"
                )))
            }
            (Some(fast), Some(reference)) => {
                if fast.band != reference.band
                    || fast.run_time != reference.run_time
                    || fast.valid_time != reference.valid_time
                    || fast.lead_seconds != reference.lead_seconds
                    || raster_expression(spec, &fast.unit)?
                        != raster_expression(spec, &reference.unit)?
                {
                    return Err(EncodeError::conversion(format!(
                        "GRIB2 header index disagrees with GDAL for {variable_id} in {}",
                        fast.path.display()
                    )));
                }
            }
        }
    }
    Ok(())
}

/// What frame discovery found: the frames of every file, the variables
/// read and in what order, the bundles the inputs can build beyond the
/// scalars, and where the planes and the run's grid are read from.
pub(super) struct Discovered<'a> {
    pub per_file: Vec<FileFrames>,
    pub variable_ids: Vec<String>,
    pub available_vector_ids: Vec<&'a str>,
    pub available_derived_ids: Vec<&'a str>,
    pub available_composite_ids: Vec<&'a str>,
    pub available_volume_ids: Vec<&'a str>,
    /// The inputs that only serve a derivation, released once it is done.
    pub drop_ids: Vec<String>,
    pub grid_path: PathBuf,
    pub plane_source: PlaneSources,
    pub producer_versions: Vec<(String, (String, String))>,
    /// Which grid family each variable is read on: empty for every source but
    /// CFSv2, whose pressure-level inputs sit in a frame file of their own on
    /// a grid of their own (`sources::CompanionFile`).
    pub variable_families: Vec<(String, Option<&'a str>)>,
}

/// Discovery for a series-file source.
pub(super) fn discover_series<'a>(
    inputs: &[PathBuf],
    options: &ConvertOptions,
    source: &'a SourceSpec,
    published: &[&'a str],
    registry: &'a Registry,
) -> Result<Discovered<'a>> {
    let mut per_file: Vec<FileFrames>;
    let variable_ids: Vec<String>;
    let available_vector_ids: Vec<&str>;
    let available_derived_ids: Vec<&str>;
    let available_composite_ids: Vec<&str>;
    let available_volume_ids: Vec<&str>;
    let drop_ids: Vec<String>;
    let grid_path: PathBuf;
    let plane_source: PlaneSources;
    let producer_versions: Vec<(String, (String, String))>;
    let variable_families: Vec<(String, Option<&str>)>;

    // A series-file source is one NetCDF file holding a whole variable's
    // series, one band per time: the CMA mosaic's local archive file, the
    // window the JMA fetch wrote, a satellite window's channels, or the
    // forecast run om2nc resampled off the Open-Meteo bucket. There are
    // no records to match. An observation's own times are the axis, gaps
    // included, with no published cadence to validate against; a
    // forecast's are its lead times and are validated below like any
    // cycle's. (The MRMS observation is one GRIB per frame and takes the
    // record path below, re-keyed onto its window's axis.) A run
    // directory holds one such file per variable — or one file for its
    // one variable (`observation::series_files`). Only the variables the
    // requested bundles carry are read: a satellite window's unpublished
    // channels feed its composite in the fetch stage and are never
    // quantized here.
    if inputs.len() != 1 {
        return Err(EncodeError::conversion(format!(
            "a {} build takes one NetCDF series, or the directory holding them",
            source.manifest_model
        )));
    }
    let input = inputs[0].clone();
    if options.require_complete && !source.fetched() {
        return Err(EncodeError::conversion(format!(
            "{} has no complete run to require",
            source.manifest_model
        )));
    }
    let requested_bundle_ids: Vec<&str> = published
        .iter()
        .copied()
        .filter(|bundle_id| {
            options
                .bundle_ids
                .as_ref()
                .is_none_or(|wanted| wanted.iter().any(|id| id == bundle_id))
        })
        .collect();
    // What the series must carry: a composite's components, which the
    // fetch stage produced and wrote as series of their own, and every
    // other bundle's *inputs* — the question the GRIB path asks too, and
    // the one that matters on a forecast series, where `prate` is derived
    // from an `apcp` series (`interval_precipitation`). Then the
    // analysis-optional inputs sort to the back, so the first variable is
    // one no frame of the run can lack and can key the axis; the GRIB
    // path sorts its own the same way.
    let mut series_variable_ids: Vec<String> = Vec::new();
    for bundle_id in &requested_bundle_ids {
        let carried = if registry.composite_components(bundle_id).is_some() {
            registry.bundle_variable_ids(bundle_id)
        } else {
            registry.bundle_input_ids(source, bundle_id)
        };
        for variable_id in carried {
            if !series_variable_ids.contains(&variable_id) {
                series_variable_ids.push(variable_id);
            }
        }
    }
    series_variable_ids.sort_by_key(|id| source.is_optional_at_analysis(id));
    let series_variable_refs: Vec<&str> = series_variable_ids.iter().map(String::as_str).collect();
    let series = inspect_observation(&input, source, Some(&series_variable_refs), registry)?;
    grid_path = series.dataset().to_path_buf();
    per_file = series.frames;
    // `last_hour` trims the series to a leading window of the file, and
    // the frame it stops on must exist — a case's declared range is never
    // silently shortened.
    if let Some(last_hour) = options.last_hour {
        let cutoff = last_hour * HOUR_SECONDS;
        let ends_at = per_file
            .last()
            .map_or(0, |frames| frames[0].1.lead_seconds);
        per_file.retain(|frames| frames[0].1.lead_seconds <= cutoff);
        if per_file.last().is_none_or(|frames| frames[0].1.lead_seconds != cutoff) {
            return Err(EncodeError::conversion(format!(
                "{} has no frame exactly at hour {last_hour}; its series ends at hour {}",
                inputs[0].display(),
                ends_at as f64 / HOUR_SECONDS as f64
            )));
        }
    }
    variable_ids = series_variable_ids;
    // A vector bundle ships when the series carries every input it is
    // built from — the same rule the GRIB path applies to a run's first
    // file, asked of the files the fetch wrote.
    available_vector_ids = requested_bundle_ids
        .iter()
        .copied()
        .filter(|bundle_id| registry.vector_components(bundle_id).is_some())
        .filter(|bundle_id| {
            registry
                .vector_input_ids(bundle_id)
                .iter()
                .all(|id| series.datasets.iter().any(|(name, _)| name == id))
        })
        .collect();
    available_derived_ids = Vec::new();
    available_composite_ids = requested_bundle_ids
        .iter()
        .copied()
        .filter(|bundle_id| registry.composite_components(bundle_id).is_some())
        .collect();
    available_volume_ids = requested_bundle_ids
        .iter()
        .copied()
        .filter(|bundle_id| registry.volume_components(bundle_id).is_some())
        .collect();
    drop_ids = Vec::new();
    plane_source = PlaneSources::PerVariable(series.plane_sources);
    producer_versions = series.producers;
    // No series-file source has a grid family: one run, one grid.
    variable_families = Vec::new();
    Ok(Discovered {
        per_file,
        variable_ids,
        available_vector_ids,
        available_derived_ids,
        available_composite_ids,
        available_volume_ids,
        drop_ids,
        grid_path,
        plane_source,
        producer_versions,
        variable_families,
    })
}

/// Discovery for a source with one record file per frame.
pub(super) fn discover_records<'a>(
    inputs: &[PathBuf],
    options: &ConvertOptions,
    source: &'a SourceSpec,
    published: &[&'a str],
    registry: &'a Registry,
) -> Result<Discovered<'a>> {
    let per_file: Vec<FileFrames>;
    let variable_ids: Vec<String>;
    let available_vector_ids: Vec<&str>;
    let available_derived_ids: Vec<&str>;
    let available_composite_ids: Vec<&str>;
    let available_volume_ids: Vec<&str>;
    let drop_ids: Vec<String>;
    let grid_path: PathBuf;
    let plane_source: PlaneSources;
    let producer_versions: Vec<(String, (String, String))>;
    let variable_families: Vec<(String, Option<&str>)>;

    let paths = discover_frames(inputs, source)?;
    // One real GDAL inspection pass over the first file: it probes which
    // vector bundles can be built (their inputs are optional, so runs
    // fetched before the wind components joined the download set still
    // build cleanly) and serves as the per-run cross-check reference for
    // the GRIB2 header index used on every file.
    let mut inspect_ids: Vec<&str> = source.input_variable_ids.iter().map(String::as_str).collect();
    if let Some(bundle_ids) = &options.bundle_ids {
        let needed: Vec<String> = bundle_ids
            .iter()
            .flat_map(|bundle_id| registry.bundle_input_ids(source, bundle_id))
            .collect();
        inspect_ids.retain(|id| needed.iter().any(|needed| needed == id));
    }
    let requested_vector_ids: Vec<&str> = published
        .iter()
        .copied()
        .filter(|id| registry.vector_components(id).is_some())
        .filter(|id| {
            options
                .bundle_ids
                .as_ref()
                .is_none_or(|ids| ids.iter().any(|wanted| wanted == id))
        })
        .collect();
    let requested_derived_ids: Vec<&str> = published
        .iter()
        .copied()
        .filter(|id| registry.scalar_derivation(id).is_some())
        .filter(|id| {
            options
                .bundle_ids
                .as_ref()
                .is_none_or(|ids| ids.iter().any(|wanted| wanted == id))
        })
        .collect();
    // An input that only feeds a derivation — a vector bundle, a derived
    // scalar — may be absent; one that is also a published scalar may not.
    let mut vector_only_ids: Vec<String> = Vec::new();
    for bundle_id in requested_vector_ids.iter().chain(&requested_derived_ids) {
        for id in registry.bundle_input_ids(source, bundle_id) {
            if !source.bundle_scalar_ids.contains(&id) && !vector_only_ids.contains(&id) {
                vector_only_ids.push(id);
            }
        }
    }
    let mut optional: Vec<&str> = source.optional_at_analysis.iter().map(String::as_str).collect();
    optional.extend(vector_only_ids.iter().map(String::as_str));
    // The reference file is the run's first, before any variable's own
    // first hour.
    optional.extend(source.variable_first_hours.iter().map(|(id, _)| id.as_str()));
    // Which grid family each input is read from — `None` for every input
    // of every source but CFSv2. The reference pass runs once per family
    // in play, each over that family's own file.
    let input_families: Vec<(String, Option<&str>)> = source
        .input_variable_ids
        .iter()
        .map(|variable_id| (variable_id.clone(), grid_family_of(source, variable_id)))
        .collect();
    let family_of = |variable_id: &str| -> Option<&str> {
        input_families
            .iter()
            .find(|(id, _)| id == variable_id)
            .and_then(|(_, family)| *family)
    };
    let mut inspect_families: Vec<Option<&str>> = Vec::new();
    for variable_id in &inspect_ids {
        let family = family_of(variable_id);
        if !inspect_families.contains(&family) {
            inspect_families.push(family);
        }
    }
    let mut reference_frames: FileFrames = Vec::new();
    for family in inspect_families {
        let family_ids: Vec<&str> = inspect_ids
            .iter()
            .copied()
            .filter(|id| family_of(id) == family)
            .collect();
        for entry in inspect_grib_multi(
            &family_frame_path(&paths[0], family),
            &family_ids,
            &optional,
            registry,
        )? {
            reference_frames.retain(|(id, _)| *id != entry.0);
            reference_frames.push(entry);
        }
    }

    available_vector_ids = requested_vector_ids
        .iter()
        .copied()
        .filter(|bundle_id| {
            registry
                .vector_input_ids(bundle_id)
                .iter()
                .all(|id| reference_frames.iter().any(|(name, _)| name == id))
        })
        .collect();
    available_derived_ids = requested_derived_ids
        .iter()
        .copied()
        .filter(|bundle_id| {
            registry
                .bundle_input_ids(source, bundle_id)
                .iter()
                .all(|id| reference_frames.iter().any(|(name, _)| name == id))
        })
        .collect();
    for bundle_id in requested_vector_ids.iter().chain(&requested_derived_ids) {
        if !available_vector_ids.contains(bundle_id) && !available_derived_ids.contains(bundle_id) {
            eprintln!(
                "WARNING building without the {bundle_id} bundle, {} are not all in {}",
                registry.bundle_input_ids(source, bundle_id).join(", "),
                paths[0].display()
            );
        }
    }

    // The variables read from the GRIB inputs: every scalar input (ECMWF
    // carries the accumulated tp instead of a rate and sflux the
    // window-averaged prate_ave, which the per-file stage derives into
    // prate), plus the inputs of each vector bundle that can be built.
    let mut ordered: Vec<String> = inspect_ids
        .iter()
        .filter(|id| !vector_only_ids.iter().any(|only| only == *id))
        .map(|id| (*id).to_string())
        .collect();
    let scalar_count = ordered.len();
    for bundle_id in available_vector_ids.iter().chain(&available_derived_ids) {
        for id in registry.bundle_input_ids(source, bundle_id) {
            if !ordered.contains(&id) {
                ordered.push(id);
            }
        }
    }
    // The inputs that only serve a derivation (spfh850 under the vapour
    // flux) are released once it is done, rather than quantized and held
    // for the whole run.
    let components: Vec<String> = available_vector_ids
        .iter()
        .filter_map(|bundle_id| registry.vector_components(bundle_id))
        .flat_map(|(u, v)| [u.to_string(), v.to_string()])
        .collect();
    drop_ids = ordered[scalar_count..]
        .iter()
        .filter(|id| !components.contains(id))
        .cloned()
        .collect();
    // The first variable is the run's reference: every file is keyed by
    // its forecast hour, so it must be one no file can lack. Stable-sorting
    // the analysis-optional inputs to the back is enough unless nothing
    // else was asked for.
    ordered.sort_by_key(|id| {
        (
            registry.is_static(id),
            source.first_hour_of(id).is_some(),
            source.is_optional_at_analysis(id),
        )
    });
    if ordered.is_empty()
        || source.is_optional_at_analysis(&ordered[0])
        || source.first_hour_of(&ordered[0]).is_some()
        || registry.is_static(&ordered[0])
    {
        return Err(EncodeError::conversion(format!(
            "a {} build needs at least one variable present in every file, including the \
             analysis; {ordered:?} is not enough",
            source.manifest_model
        )));
    }
    variable_ids = ordered;
    let ordered_refs: Vec<&str> = variable_ids.iter().map(String::as_str).collect();
    let static_refs: Vec<&str> = ordered_refs
        .iter()
        .copied()
        .filter(|id| registry.is_static(id))
        .collect();
    let optional_at_analysis: Vec<&str> = source.optional_at_analysis.iter().map(String::as_str).collect();
    per_file = prepare_frames_all(
        &paths,
        &ordered_refs,
        &optional_at_analysis,
        &source.variable_first_hours,
        &reference_frames,
        source.cadence_seconds,
        &input_families,
        &static_refs,
        options,
        registry,
    )?;
    available_composite_ids = Vec::new();
    // A volume's levels are every one of them an input the run must
    // carry (none is optional), so a listed volume is built from what the
    // first file was found to hold.
    available_volume_ids = published
        .iter()
        .copied()
        .filter(|bundle_id| {
            options
                .bundle_ids
                .as_ref()
                .is_none_or(|ids| ids.iter().any(|wanted| wanted == bundle_id))
        })
        .filter(|bundle_id| {
            registry.volume_components(bundle_id).is_some_and(|members| {
                members
                    .iter()
                    .all(|id| reference_frames.iter().any(|(name, _)| name == id))
            })
        })
        .collect();
    grid_path = paths[0].clone();
    plane_source = PlaneSources::Uniform(PlaneSource::grib());
    producer_versions = Vec::new();
    variable_families = input_families;
    Ok(Discovered {
        per_file,
        variable_ids,
        available_vector_ids,
        available_derived_ids,
        available_composite_ids,
        available_volume_ids,
        drop_ids,
        grid_path,
        plane_source,
        producer_versions,
        variable_families,
    })
}
