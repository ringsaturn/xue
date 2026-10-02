//! One point's series, read straight from the published Zarr stores.
//!
//! Resolution is the published chain — Collection (`xue:pointer`) → pointer →
//! manifest — and decoding reuses the container's `decode_chunk` plus the Zarr
//! read geometry in `xue::zarr`. No new format, no new artifact. The response
//! carries the source and run, the request, the probed cell, the ISO 8601 axis,
//! and per requested bundle either one variable's `{label, unit, values}` or a
//! `{components: {…}}` map, with `null` for a missing code.

use futures_util::future::join_all;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use worker::Url;

use xue::zarr::{self, ArrayLayout, ProbeCell, VariableMetadata};
use xue::{decode_chunk, DecodeError, Predictor};

use crate::bucket::Data;
use crate::error::HttpError;

#[derive(Debug, Deserialize)]
struct Collection {
    #[serde(rename = "xue:pointer")]
    pointer: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Pointer {
    #[serde(rename = "runTime")]
    run_time: Option<String>,
    #[serde(rename = "manifestPath")]
    manifest_path: String,
    #[serde(rename = "manifestCrc32")]
    manifest_crc32: String,
}

#[derive(Debug, Deserialize)]
pub(crate) struct Manifest {
    #[serde(rename = "runTime")]
    pub run_time: Option<String>,
    pub bundles: Vec<ManifestBundle>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct ManifestBundle {
    pub variable: String,
    pub zarr: Option<Store>,
    /// The series companion (`docs/zarr-profile.md`, "Series store"): one
    /// inner chunk is a cell's whole series, preferred over `zarr` when
    /// present because a point read then costs one chunk rather than one per
    /// time chunk.
    pub series: Option<Store>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct Store {
    pub path: String,
    /// The store's CRC-32, its `?v=`; the identity an isolate cache keys the
    /// immutable group/array/index reads by.
    pub crc32: String,
}

pub(crate) struct Run {
    pub manifest: Manifest,
    pub dir: String,
    pub run_time: Option<String>,
}

/// Resolve a run to its manifest. With `run` the manifest is read directly and
/// the live pointer is skipped; without it the published chain Collection →
/// pointer → manifest is followed.
pub(crate) async fn resolve_run(
    data: &Data,
    source: &str,
    run: Option<&str>,
) -> Result<Run, HttpError> {
    if let Some(run) = run {
        let dir = zarr::run_directory(run).ok_or_else(|| {
            HttpError::new(400, "invalid_parameter", format!("invalid run {run}"))
        })?;
        let manifest: Manifest =
            data.json(&format!("{dir}/manifest.json"))
                .await
                .map_err(|error| {
                    if error.status == 404 {
                        HttpError::new(
                            404,
                            "unknown_run",
                            format!("no manifest at {dir}/manifest.json"),
                        )
                    } else {
                        error
                    }
                })?;
        let run_time = manifest.run_time.clone();
        return Ok(Run {
            manifest,
            dir,
            run_time,
        });
    }
    let collection: Collection = data
        .json(&format!("{source}/collection.json"))
        .await
        .map_err(|error| {
            if error.status == 404 {
                HttpError::new(404, "unknown_source", format!("unknown source {source}"))
            } else {
                error
            }
        })?;
    let pointer_name = collection.pointer.ok_or_else(|| {
        HttpError::new(
            404,
            "no_live_run",
            format!("source {source} has no live pointer"),
        )
    })?;
    let pointer: Pointer = data.json(&pointer_name).await?;
    // The live manifest is immutable for this pointer's crc, so an isolate
    // that already read it answers the next request without R2.
    let manifest: Manifest = data
        .json_cached(&pointer.manifest_path, &pointer.manifest_crc32)
        .await?;
    let dir = pointer
        .manifest_path
        .rsplit_once('/')
        .map(|(dir, _)| dir.to_owned())
        .unwrap_or_default();
    let run_time = pointer.run_time.or_else(|| manifest.run_time.clone());
    Ok(Run {
        manifest,
        dir,
        run_time,
    })
}

/// The parsed `/v1/point` query.
pub struct PointQuery {
    pub source: Option<String>,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
    pub variables: Option<Vec<String>>,
    pub time: Option<String>,
    pub run: Option<String>,
}

fn param(url: &Url, key: &str) -> Option<String> {
    url.query_pairs()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value.into_owned())
}

pub fn parse_query(url: &Url) -> PointQuery {
    let variables = param(url, "variables").map(|value| {
        value
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .collect::<Vec<_>>()
    });
    PointQuery {
        source: param(url, "source"),
        lat: param(url, "lat").and_then(|value| value.parse().ok()),
        lon: param(url, "lon").and_then(|value| value.parse().ok()),
        variables: variables.filter(|list| !list.is_empty()),
        time: param(url, "time"),
        run: param(url, "run"),
    }
}

fn storage_error(error: DecodeError) -> HttpError {
    HttpError::new(502, "upstream_failed", error.to_string())
}

fn variable_json(variable: &VariableMetadata, values: &[Option<f64>]) -> Value {
    let mut entry = Map::new();
    if let Some(label) = &variable.label {
        entry.insert("label".into(), json!(label));
    }
    if let Some(unit) = &variable.unit {
        entry.insert("unit".into(), json!(unit));
    }
    entry.insert("values".into(), json!(values));
    Value::Object(entry)
}

/// The values of one variable at one cell across the requested frames.
async fn read_bundle_series(
    data: &Data,
    store_base: &str,
    version: &str,
    variable: &VariableMetadata,
    cell: ProbeCell,
    indices: &[u32],
) -> Result<Vec<Option<f64>>, HttpError> {
    let layout: ArrayLayout = zarr::parse_array_metadata(
        &data
            .text_cached(&format!("{store_base}/{}/zarr.json", variable.id), version)
            .await?,
    )
    .map_err(storage_error)?;
    let tile = zarr::tile_of(&layout, cell.row, cell.column).map_err(storage_error)?;
    let (origin_row, origin_column) = zarr::tile_origin(&layout, tile);
    let stride = layout.tile_height as usize * layout.tile_width as usize;
    let cell_offset = (cell.row - origin_row) as usize * layout.tile_width as usize
        + (cell.column - origin_column) as usize;

    // One chunk holds a time chunk's frames, so group the frames by the chunk
    // they fall in and read each chunk once.
    let mut by_chunk: Vec<(u32, Vec<usize>)> = Vec::new();
    for (position, &frame_index) in indices.iter().enumerate() {
        let time_chunk = frame_index / layout.time_chunk;
        match by_chunk.iter_mut().find(|(chunk, _)| *chunk == time_chunk) {
            Some((_, positions)) => positions.push(position),
            None => by_chunk.push((time_chunk, vec![position])),
        }
    }

    // The index per shard the requested frames touch (one shard on a store the
    // exporter writes; kept general for the earlier shape).
    let mut shard_indices: Vec<(u32, Vec<Option<zarr::ShardIndexEntry>>)> = Vec::new();
    for (time_chunk, _) in &by_chunk {
        let (shard, _) = zarr::shard_of(&layout, *time_chunk);
        if shard_indices.iter().any(|(held, _)| *held == shard) {
            continue;
        }
        let index = zarr::parse_shard_index(
            &data
                .suffix_cached(
                    &format!("{store_base}/{}", zarr::shard_path(&variable.id, shard)),
                    zarr::shard_index_length(layout.chunks_per_shard as usize) as u64,
                    version,
                )
                .await?,
            layout.chunks_per_shard as usize,
        )
        .map_err(storage_error)?;
        shard_indices.push((shard, index));
    }

    let predictor = if layout.delta {
        Predictor::Previous
    } else {
        Predictor::Raw
    };
    // The chunks a series touches are independent, so read them together
    // rather than one after another: on a map store the whole axis is one
    // round trip per time chunk, and reading them sequentially turned a point
    // query into twenty-seven R2 round trips. Batched so a very long axis (a
    // seasonal run) does not open hundreds of R2 operations at once.
    let mut values: Vec<Option<f64>> = vec![None; indices.len()];
    for batch in by_chunk.chunks(12) {
        let chunk_results = join_all(batch.iter().map(|(time_chunk, positions)| {
            let (shard, base) = zarr::shard_of(&layout, *time_chunk);
            let index = &shard_indices
                .iter()
                .find(|(held, _)| *held == shard)
                .expect("the shard's index was fetched above")
                .1;
            // A chunk the shard never held reads as the fill value, which the
            // response spells `null`.
            let entry = index.get((base + tile) as usize).copied().flatten();
            let path = format!("{store_base}/{}", zarr::shard_path(&variable.id, shard));
            let positions = positions.clone();
            async move {
                let count = positions.len();
                let Some(entry) = entry else {
                    return Ok::<_, HttpError>((positions, vec![None; count]));
                };
                let payload = data.range(&path, entry.offset, entry.length).await?;
                let codes = decode_chunk(
                    &payload,
                    layout.time_chunk,
                    layout.tile_height,
                    layout.tile_width,
                    predictor,
                )
                .map_err(storage_error)?;
                let mut chunk_values = Vec::with_capacity(count);
                for &position in &positions {
                    let frame_in_chunk = indices[position] - *time_chunk * layout.time_chunk;
                    let code = codes[frame_in_chunk as usize * stride + cell_offset];
                    chunk_values.push(zarr::decode_value(&variable.quantization, code));
                }
                Ok((positions, chunk_values))
            }
        }))
        .await;
        for result in chunk_results {
            let (positions, chunk_values) = result?;
            for (position, value) in positions.into_iter().zip(chunk_values) {
                values[position] = value;
            }
        }
    }
    Ok(values)
}

/// One point's series across the requested bundles. Returns the response body
/// and the run directory it resolved to (the `x-xue-run` header).
pub async fn read_point(data: &Data, query: &PointQuery) -> Result<(Value, String), HttpError> {
    let source = query
        .source
        .clone()
        .ok_or_else(|| HttpError::new(400, "invalid_parameter", "source is required"))?;
    let lat = query
        .lat
        .filter(|value| value.is_finite() && (-90.0..=90.0).contains(value))
        .ok_or_else(|| HttpError::new(400, "invalid_parameter", "lat must be within [-90, 90]"))?;
    let lon = query
        .lon
        .filter(|value| value.is_finite() && (-180.0..=360.0).contains(value))
        .ok_or_else(|| {
            HttpError::new(400, "invalid_parameter", "lon must be within [-180, 360]")
        })?;

    let Run {
        manifest,
        dir,
        run_time,
    } = resolve_run(data, &source, query.run.as_deref()).await?;
    let bundle_ids: Vec<String> = match &query.variables {
        Some(variables) if !variables.is_empty() => variables.clone(),
        _ => manifest
            .bundles
            .first()
            .map(|bundle| vec![bundle.variable.clone()])
            .unwrap_or_default(),
    };
    if bundle_ids.is_empty() {
        return Err(HttpError::new(
            404,
            "no_live_run",
            format!("source {source} publishes no bundles"),
        ));
    }

    let mut variables = Map::new();
    let mut cell: Option<ProbeCell> = None;
    let mut times: Option<Vec<String>> = None;
    let mut unit_seconds: Option<u32> = None;

    for bundle_id in &bundle_ids {
        let bundle = manifest
            .bundles
            .iter()
            .find(|bundle| &bundle.variable == bundle_id)
            .ok_or_else(|| {
                let available: Vec<&str> = manifest
                    .bundles
                    .iter()
                    .map(|bundle| bundle.variable.as_str())
                    .collect();
                HttpError::new(
                    404,
                    "unknown_variable",
                    format!("source {source} has no bundle {bundle_id}"),
                )
                .with_detail(json!({ "available": available }))
            })?;
        let store = bundle
            .series
            .as_ref()
            .or(bundle.zarr.as_ref())
            .ok_or_else(|| {
                HttpError::new(
                    404,
                    "unknown_variable",
                    format!("bundle {bundle_id} ships no store"),
                )
            })?;
        let store_base = format!("{dir}/{}", store.path);
        let group = zarr::parse_group_metadata(
            &data
                .text_cached(&format!("{store_base}/zarr.json"), &store.crc32)
                .await?,
        )
        .map_err(storage_error)?;
        let metadata = group.metadata;
        let bundle_cell = zarr::probe_cell(&metadata.grid, lon, lat).ok_or_else(|| {
            HttpError::new(
                422,
                "point_off_grid",
                format!("point is outside the {source}/{bundle_id} grid"),
            )
        })?;
        cell.get_or_insert(bundle_cell);
        let run_time_ref = run_time.as_deref().or(metadata.run_time.as_deref());
        let indices = zarr::frame_indices(
            &metadata.offsets,
            metadata.unit_seconds,
            run_time_ref,
            query.time.as_deref(),
        )
        .map_err(|error| HttpError::new(400, "invalid_parameter", error.to_string()))?;
        if times.is_none() {
            times = Some(zarr::axis_times(
                &metadata.offsets,
                metadata.unit_seconds,
                run_time_ref,
                &indices,
            ));
        }
        unit_seconds.get_or_insert(metadata.unit_seconds);

        let mut decoded = Map::new();
        for variable in &metadata.variables {
            let values = read_bundle_series(
                data,
                &store_base,
                &store.crc32,
                variable,
                bundle_cell,
                &indices,
            )
            .await?;
            decoded.insert(variable.id.clone(), variable_json(variable, &values));
        }
        let entry = if metadata.variables.len() == 1 && metadata.variables[0].id == *bundle_id {
            decoded.get(bundle_id).cloned().unwrap_or(Value::Null)
        } else if metadata.variables.len() == 1 {
            decoded
                .get(&metadata.variables[0].id)
                .cloned()
                .unwrap_or(Value::Null)
        } else {
            json!({ "components": decoded })
        };
        variables.insert(bundle_id.clone(), entry);
    }

    let body = json!({
        "source": source,
        "run": dir,
        "runTime": run_time,
        "request": { "lat": lat, "lon": lon, "variables": bundle_ids },
        "cell": cell.map(|cell| json!({
            "column": cell.column,
            "row": cell.row,
            "longitude": cell.longitude,
            "latitude": cell.latitude,
        })),
        "time": { "unitSeconds": unit_seconds, "times": times },
        "variables": variables,
    });
    Ok((body, dir))
}
