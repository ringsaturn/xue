//! One point's series, read straight from the published Zarr stores.
//!
//! Resolution is the published chain — Collection (`xue:pointer`) → pointer →
//! manifest — and decoding reuses the container's `decode_chunk` plus the Zarr
//! read geometry in `xue::zarr`. No new format, no new artifact. The response
//! carries the source and run, the request, the probed cell and the ISO 8601
//! axis. `variables` is a flat map keyed by variable id — a vector bundle's
//! components are separate entries carrying their `bundle` — so a client types
//! it as one `map<string, Variable>` whatever it asked for; `null` marks a
//! missing code.

use futures_util::future::join_all;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use url::Url;

use xue::zarr::{self, ArrayLayout, ProbeCell, VariableMetadata};
use xue::{decode_chunk, DecodeError, Predictor};

use crate::bucket::Data;
use crate::error::HttpError;
use crate::{products, timezone};

#[derive(Debug, Deserialize)]
struct Collection {
    #[serde(rename = "xue:pointer")]
    pointer: Option<String>,
}

/// A live pointer. A run's names its manifest; a point product's
/// (`latest-sounding.json`, …) names an index instead and carries `product`,
/// which is how `/v1/point` tells a client it asked the wrong route.
#[derive(Debug, Deserialize)]
struct Pointer {
    product: Option<String>,
    #[serde(rename = "runTime")]
    run_time: Option<String>,
    #[serde(rename = "manifestPath")]
    manifest_path: Option<String>,
    #[serde(rename = "manifestCrc32")]
    manifest_crc32: Option<String>,
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
    let (manifest_path, manifest_crc32) = match (pointer.manifest_path, pointer.manifest_crc32) {
        (Some(path), Some(crc32)) => (path, crc32),
        _ => {
            let product = pointer.product.as_deref().unwrap_or(source);
            let endpoint = products::product(product).and_then(|product| product.endpoint);
            return Err(HttpError::new(
                400,
                "not_a_grid",
                match endpoint {
                    Some(endpoint) => {
                        format!("{source} is a point product, not a grid; use {endpoint}")
                    }
                    None => format!("{source} is a point product, not a grid"),
                },
            )
            .with_detail(json!({ "product": product, "endpoint": endpoint })));
        }
    };
    // The live manifest is immutable for this pointer's crc, so an isolate
    // that already read it answers the next request without R2.
    let manifest: Manifest = data.json_cached(&manifest_path, &manifest_crc32).await?;
    let dir = manifest_path
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
    // Both spellings: `?variables=a,b` (the OpenAPI `explode: false` form) and
    // `?variables=a&variables=b` (a repeated array).
    let mut variables: Vec<String> = Vec::new();
    for (name, value) in url.query_pairs() {
        if name == "variables" {
            for part in value.split(',') {
                let part = part.trim();
                if !part.is_empty() {
                    variables.push(part.to_owned());
                }
            }
        }
    }
    PointQuery {
        source: param(url, "source"),
        lat: param(url, "lat").and_then(|value| value.parse().ok()),
        lon: param(url, "lon").and_then(|value| value.parse().ok()),
        variables: (!variables.is_empty()).then_some(variables),
        time: param(url, "time"),
        run: param(url, "run"),
    }
}

fn storage_error(error: DecodeError) -> HttpError {
    HttpError::new(502, "upstream_failed", error.to_string())
}

/// One entry of the flat `variables` map: the variable's own id keys it, and
/// `bundle` says which requested bundle it belongs to, so a vector bundle's
/// components are separate entries rather than a nested `components` object.
/// Every field is always present — `label`/`unit` are `null` when the metadata
/// has none — so a client types one struct whatever it asked for.
fn variable_json(variable: &VariableMetadata, bundle: &str, values: &[Option<f64>]) -> Value {
    json!({
        "bundle": bundle,
        "label": variable.label,
        "unit": variable.unit,
        "values": values,
    })
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

/// Where a read lands on each bundle's grid.
#[derive(Clone, Copy)]
enum Target {
    /// A coordinate, probed on each bundle's own grid.
    Point { lon: f64, lat: f64 },
    /// A cell by index, which only means one place when every requested
    /// bundle shares a grid.
    Cell { column: u32, row: u32 },
}

/// The series of the requested bundles at a target.
struct Series {
    variables: Map<String, Value>,
    cell: Option<ProbeCell>,
    times: Option<Vec<String>>,
    unit_seconds: Option<u32>,
}

/// The bundles to read: those asked for, or the run's first.
fn bundle_ids(
    source: &str,
    manifest: &Manifest,
    asked: &Option<Vec<String>>,
) -> Result<Vec<String>, HttpError> {
    let bundle_ids: Vec<String> = match asked {
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
    Ok(bundle_ids)
}

async fn read_series(
    data: &Data,
    source: &str,
    run: &Run,
    bundle_ids: &[String],
    time: Option<&str>,
    target: Target,
) -> Result<Series, HttpError> {
    let Run {
        manifest,
        dir,
        run_time,
    } = run;
    let mut series = Series {
        variables: Map::new(),
        cell: None,
        times: None,
        unit_seconds: None,
    };
    let mut first_grid: Option<(&str, zarr::Grid)> = None;

    for bundle_id in bundle_ids {
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
        // A shared store holds every bundle of its tier, so the group is
        // opened for this entry's bundle; a single-bundle store ignores it.
        let group = zarr::parse_group_metadata_for(
            &data
                .text_cached(&format!("{store_base}/zarr.json"), &store.crc32)
                .await?,
            bundle_id,
        )
        .map_err(storage_error)?;
        let metadata = group.metadata;
        let bundle_cell = match target {
            Target::Point { lon, lat } => {
                zarr::probe_cell(&metadata.grid, lon, lat).ok_or_else(|| {
                    HttpError::new(
                        422,
                        "point_off_grid",
                        format!("point is outside the {source}/{bundle_id} grid"),
                    )
                })?
            }
            Target::Cell { column, row } => {
                let grid = metadata.grid;
                match first_grid {
                    None => first_grid = Some((bundle_id, grid)),
                    Some((first, held)) if held != grid => {
                        return Err(HttpError::new(
                            400,
                            "grid_mismatch",
                            format!(
                                "{first} and {bundle_id} are on different grids; read them in separate requests"
                            ),
                        )
                        .with_detail(json!({ "bundles": [first, bundle_id] })));
                    }
                    Some(_) => {}
                }
                zarr::grid_cell(&grid, column, row).ok_or_else(|| {
                    HttpError::new(
                        400,
                        "invalid_parameter",
                        format!(
                            "cell ({row}, {column}) is outside the {source}/{bundle_id} grid of {} rows × {} columns",
                            grid.height, grid.width
                        ),
                    )
                    .with_detail(json!({ "rows": grid.height, "columns": grid.width }))
                })?
            }
        };
        series.cell.get_or_insert(bundle_cell);
        let run_time_ref = run_time.as_deref().or(metadata.run_time.as_deref());
        let indices =
            zarr::frame_indices(&metadata.offsets, metadata.unit_seconds, run_time_ref, time)
                .map_err(|error| HttpError::new(400, "invalid_parameter", error.to_string()))?;
        if series.times.is_none() {
            series.times = Some(zarr::axis_times(
                &metadata.offsets,
                metadata.unit_seconds,
                run_time_ref,
                &indices,
            ));
        }
        series.unit_seconds.get_or_insert(metadata.unit_seconds);

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
            series.variables.insert(
                variable.id.clone(),
                variable_json(variable, bundle_id, &values),
            );
        }
    }
    Ok(series)
}

fn cell_json(cell: Option<ProbeCell>) -> Value {
    cell.map(|cell| {
        json!({
            "column": cell.column,
            "row": cell.row,
            "longitude": cell.longitude,
            "latitude": cell.latitude,
        })
    })
    .unwrap_or(Value::Null)
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

    let run = resolve_run(data, &source, query.run.as_deref()).await?;
    let bundle_ids = bundle_ids(&source, &run.manifest, &query.variables)?;
    let series = read_series(
        data,
        &source,
        &run,
        &bundle_ids,
        query.time.as_deref(),
        Target::Point { lon, lat },
    )
    .await?;

    let body = json!({
        "source": source,
        "run": run.dir,
        "runTime": run.run_time,
        "request": { "lat": lat, "lon": lon, "variables": bundle_ids },
        // The requested point's zone, not the cell's: on a coarse grid the
        // cell centre can sit across a border from the point.
        "timezone": timezone::name(lon, lat),
        "cell": cell_json(series.cell),
        "time": { "unitSeconds": series.unit_seconds, "times": series.times },
        "variables": series.variables,
    });
    Ok((body, run.dir))
}

/// The parsed `/v1/cell` query.
pub struct CellQuery {
    pub source: Option<String>,
    pub run: Option<String>,
    pub row: Option<String>,
    pub column: Option<String>,
    pub variables: Option<Vec<String>>,
    pub time: Option<String>,
}

pub fn parse_cell_query(url: &Url) -> CellQuery {
    let point = parse_query(url);
    CellQuery {
        source: point.source,
        run: point.run,
        row: param(url, "row"),
        column: param(url, "column"),
        variables: point.variables,
        time: point.time,
    }
}

fn index(name: &str, value: Option<&str>) -> Result<u32, HttpError> {
    value
        .ok_or_else(|| HttpError::new(400, "invalid_parameter", format!("{name} is required")))?
        .parse()
        .map_err(|_| {
            HttpError::new(
                400,
                "invalid_parameter",
                format!("{name} must be a non-negative integer"),
            )
        })
}

/// One grid cell's series in a named run: the cacheable form of a point read.
/// `run` is required, so the answer never changes for its URL; every
/// requested bundle must be on one grid, since `row`/`column` index it.
pub async fn read_cell(data: &Data, query: &CellQuery) -> Result<(Value, String), HttpError> {
    let source = query
        .source
        .clone()
        .ok_or_else(|| HttpError::new(400, "invalid_parameter", "source is required"))?;
    let run_name = query.run.as_deref().ok_or_else(|| {
        HttpError::new(
            400,
            "invalid_parameter",
            format!("run is required; /v1/sources/{source} names the live run"),
        )
    })?;
    let row = index("row", query.row.as_deref())?;
    let column = index("column", query.column.as_deref())?;

    let run = resolve_run(data, &source, Some(run_name)).await?;
    let mut bundle_ids = bundle_ids(&source, &run.manifest, &query.variables)?;
    // Asking twice reads once; the order a client lists them in is echoed.
    let mut seen = std::collections::HashSet::new();
    bundle_ids.retain(|id| seen.insert(id.clone()));
    let series = read_series(
        data,
        &source,
        &run,
        &bundle_ids,
        query.time.as_deref(),
        Target::Cell { column, row },
    )
    .await?;
    // The cell's own zone: there is no requested point to prefer.
    let timezone = series
        .cell
        .and_then(|cell| timezone::name(cell.longitude, cell.latitude));

    let body = json!({
        "source": source,
        "run": run.dir,
        "runTime": run.run_time,
        "request": { "row": row, "column": column, "variables": bundle_ids },
        "timezone": timezone,
        "cell": cell_json(series.cell),
        "time": { "unitSeconds": series.unit_seconds, "times": series.times },
        "variables": series.variables,
    });
    Ok((body, run.dir))
}
