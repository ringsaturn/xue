//! Xue bundles as Zarr v3 stores, written straight from their codes.
//!
//! Two layouts, both normative in `docs/zarr-profile.md`, and both mirroring
//! `xuebuild/zarrstore.py`, the reference, byte for byte. A **shared store**
//! (profile 2, `bundles[.<tier>].zarr`) holds every bundle of one resolution
//! tier: [`write_bundle_arrays`] writes one bundle's arrays and the
//! coordinates its dimension names name into it, [`write_part`] the partial
//! group document over the bundles one build wrote. The store's `zarr.json`
//! itself is never written here: the reference's `finalize_store` merges the
//! parts into it, so the group document has one implementation. A
//! **one-bundle store** (profile 1, [`write_store`]) is what a series store
//! is.
//!
//! Documents are serialized the way Python's `json.dumps(..., indent=2,
//! ensure_ascii=False)` does, plus a trailing newline; inner chunks are
//! padded and compressed by the same one-shot zstd call, with the same shard
//! index and CRC-32C. A bundle's arrays are a function of its metadata, its
//! codes, its tile, each variable's predictor and its dimension names, so
//! nothing here reads a container. Every chunk is decompressed again right
//! after it is compressed and compared with what went in, and every document
//! written is parsed back by the decoder's own store validators
//! (`crate::zarr`), the store's counterpart of the container's full
//! read-back.

use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rayon::prelude::*;
use serde_json::{json, Map, Value};

use crate::encode::binformat::crc32_plane;
use crate::encode::errors::{EncodeError, Result};
use crate::zarr;

/// Frames per time chunk of a map store: the bundle's own group length, on a
/// regular grid.
pub const TIME_CHUNK: usize = 6;
/// The spatial block of a series store, `(width, height)`.
pub const SERIES_BLOCK: (usize, usize) = (8, 8);
/// The inner chunks' zstd level, which the array documents declare.
pub const ZSTD_LEVEL: i32 = 15;
const PROFILE_VERSION: u64 = 1;
/// The `xue_profile` of a shared store (`bundles[.<tier>].zarr`).
pub const SHARED_PROFILE_VERSION: u64 = 2;
/// The reserved stem of a shared store; no bundle may be named it.
pub const SHARED_STORE_STEM: &str = "bundles";
/// File name prefix of a partial group document inside a shared store
/// (`zarr.part.<first bundle id>.json`).
pub const PART_PREFIX: &str = "zarr.part.";
const DELTA_CODEC: &str = "xue.delta";
/// The primary dimension names, which are also the store's primary
/// coordinate arrays.
pub const DIMENSIONS: [&str; 3] = ["time", "latitude", "longitude"];

/// One variable's codes: its planes in axis order, each `width * height`.
pub struct StoreVariable<'a> {
    pub id: String,
    /// Stored as residuals against the previous frame in the container
    /// (`previous`), or not (`raw`).
    pub previous: bool,
    pub planes: Vec<&'a [u8]>,
}

/// A bundle's codes as the writer reads them.
pub struct StoreCodes<'a> {
    /// The bundle's metadata document, exactly as a container embeds it.
    pub metadata: &'a Value,
    /// In the metadata's variable order, which is the store's array order.
    pub variables: Vec<StoreVariable<'a>>,
    /// `(width, height)` of the tile the bundle is cut with.
    pub tile: (usize, usize),
}

/// What one store write produced: the manifest descriptor's three values
/// plus the report fields `ExportReport.to_dict` carries.
#[derive(Debug, Clone)]
pub struct StoreReport {
    pub path: PathBuf,
    pub byte_length: u64,
    pub crc32: String,
    pub delta: bool,
    pub arrays: Vec<(String, u64)>,
    pub chunk_count: u64,
}

impl StoreReport {
    /// The report block, keyed as the reference's `ExportReport.to_dict`.
    pub fn to_value(&self) -> Value {
        let mut arrays = Map::new();
        for (name, written) in &self.arrays {
            arrays.insert(name.clone(), json!(written));
        }
        json!({
            "path": self.path.display().to_string(),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "delta": self.delta,
            "indexLocation": "end",
            "arrays": Value::Object(arrays),
            "chunkCount": self.chunk_count,
            "comparableChunks": 0,
            "identicalChunks": 0,
        })
    }
}

/// How one array is cut (`ArrayGeometry` in the reference): inner chunk
/// `[time_chunk, tile_height, tile_width]`, one shard of the whole axis
/// rounded up to whole time chunks over the whole grid of whole tiles.
#[derive(Debug, Clone, Copy)]
struct Geometry {
    frame_count: usize,
    height: usize,
    width: usize,
    time_chunk: usize,
    tile_height: usize,
    tile_width: usize,
}

impl Geometry {
    fn tile_rows(&self) -> usize {
        self.height.div_ceil(self.tile_height)
    }
    fn tile_columns(&self) -> usize {
        self.width.div_ceil(self.tile_width)
    }
    fn tile_count(&self) -> usize {
        self.tile_rows() * self.tile_columns()
    }
    fn time_chunks(&self) -> usize {
        self.frame_count.div_ceil(self.time_chunk)
    }
    fn shard_frames(&self) -> usize {
        self.time_chunks() * self.time_chunk
    }
    fn chunks_per_shard(&self) -> usize {
        self.time_chunks() * self.tile_count()
    }
    fn inner_length(&self) -> usize {
        self.time_chunk * self.tile_height * self.tile_width
    }
    fn tile_origin(&self, tile: usize) -> (usize, usize) {
        (
            (tile / self.tile_columns()) * self.tile_height,
            (tile % self.tile_columns()) * self.tile_width,
        )
    }
    fn tile_shape(&self, tile: usize) -> (usize, usize) {
        let (row, column) = self.tile_origin(tile);
        (
            self.tile_height.min(self.height - row),
            self.tile_width.min(self.width - column),
        )
    }
}

// -- JSON in Python's spelling -------------------------------------------------

/// `json.dumps(value, indent=2, ensure_ascii=False) + "\n"`: two-space
/// indentation, `": "` between a key and its value, raw UTF-8, and `{}` /
/// `[]` for empty containers, which is serde_json's pretty printer with a
/// two-space indent. Numbers are serde_json's, the same spelling the bundle
/// metadata already shares with the reference.
fn dump_json(value: &Value) -> Vec<u8> {
    let mut output = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b"  ");
    let mut serializer = serde_json::Serializer::with_formatter(&mut output, formatter);
    serde::Serialize::serialize(value, &mut serializer).expect("serializable JSON");
    output.push(b'\n');
    output
}

fn write_file(path: &Path, data: &[u8]) -> Result<u64> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| io_error(parent, &error))?;
    }
    fs::write(path, data).map_err(|error| io_error(path, &error))?;
    Ok(data.len() as u64)
}

fn io_error(path: &Path, error: &std::io::Error) -> EncodeError {
    EncodeError::conversion(format!("cannot write {}: {error}", path.display()))
}

// -- documents -----------------------------------------------------------------

/// One variable array's document. `bundle` is set in a shared store only,
/// where it leads the `xue` block so an array opened alone knows which
/// bundle's metadata describes it.
fn array_metadata(
    geometry: &Geometry,
    variable: &Value,
    predictor: &str,
    delta: bool,
    bundle: Option<&str>,
    dimensions: &[String; 3],
) -> Value {
    let quantization = &variable["quantization"];
    let mut xue = Map::new();
    if let Some(bundle) = bundle {
        xue.insert("bundle".into(), json!(bundle));
    }
    xue.insert("variable".into(), variable.clone());
    xue.insert("predictor".into(), json!(predictor));
    let mut attributes = Map::new();
    attributes.insert("xue".into(), Value::Object(xue));
    if quantization.get("type").and_then(Value::as_str) == Some("linear") {
        attributes.insert("scale_factor".into(), quantization["scale"].clone());
        attributes.insert("add_offset".into(), quantization["offset"].clone());
    }
    attributes.insert("_FillValue".into(), quantization["nodataCode"].clone());
    let mut inner = Vec::new();
    if delta {
        inner.push(json!({"name": DELTA_CODEC, "configuration": {"axis": 0}}));
    }
    inner.push(json!({"name": "bytes"}));
    inner.push(json!({"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": true}}));
    json!({
        "zarr_format": 3,
        "node_type": "array",
        "shape": [geometry.frame_count, geometry.height, geometry.width],
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [
            geometry.shard_frames(),
            geometry.tile_rows() * geometry.tile_height,
            geometry.tile_columns() * geometry.tile_width,
        ]}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": quantization["nodataCode"].clone(),
        "codecs": [{
            "name": "sharding_indexed",
            "configuration": {
                "chunk_shape": [geometry.time_chunk, geometry.tile_height, geometry.tile_width],
                "codecs": inner,
                "index_codecs": [
                    {"name": "bytes", "configuration": {"endian": "little"}},
                    {"name": "crc32c"},
                ],
                "index_location": "end",
            },
        }],
        "attributes": Value::Object(attributes),
        "dimension_names": dimensions,
    })
}

fn coordinate_metadata(length: usize, data_type: &str, fill_value: Value, attributes: Value, name: &str) -> Value {
    json!({
        "zarr_format": 3,
        "node_type": "array",
        "shape": [length],
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [length]}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": fill_value,
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
        "attributes": attributes,
        "dimension_names": [name],
    })
}

fn number(value: &Value, what: &str) -> Result<f64> {
    value
        .as_f64()
        .ok_or_else(|| EncodeError::conversion(format!("metadata grid {what} is not a number")))
}

/// Write by rename: in a shared store several bundles write the same
/// coordinate array concurrently, always the same bytes, and a rename never
/// leaves a reader (or the last writer) a torn file.
fn write_file_atomic(path: &Path, data: &[u8]) -> Result<u64> {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let parent = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(|error| io_error(parent, &error))?;
    let name = path.file_name().map(|name| name.to_string_lossy().into_owned()).unwrap_or_default();
    let temporary = parent.join(format!(
        ".{name}.{}.{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    if let Err(error) = fs::write(&temporary, data) {
        let _ = fs::remove_file(&temporary);
        return Err(io_error(&temporary, &error));
    }
    if let Err(error) = fs::rename(&temporary, path) {
        let _ = fs::remove_file(&temporary);
        return Err(io_error(path, &error));
    }
    Ok(data.len() as u64)
}

/// The three one-dimensional coordinate arrays, each one chunk: for xarray
/// and its kind, never read by the viewer. `names` are the bundle's
/// dimension names (`time` or `time_<bundle>`, and so on): a coordinate array
/// is named by the dimension it labels, and its encoding does not depend on
/// the name. `atomic` writes by rename, for a shared store.
fn write_coordinates(
    metadata: &Value,
    geometry: &Geometry,
    store: &Path,
    names: &[String; 3],
    atomic: bool,
    documents: &mut Vec<(String, Value)>,
) -> Result<Vec<(String, u64)>> {
    let text = serde_json::to_string(metadata).expect("serializable metadata");
    let parsed = zarr::parse_bundle_metadata(&text)
        .map_err(|error| EncodeError::bundle(format!("store metadata is invalid: {}", error.0)))?;
    let grid = &metadata["grid"];
    let run_time = metadata["runTime"].as_str().unwrap_or_default();
    let unit_seconds = i64::from(parsed.unit_seconds);
    let mut time = Vec::with_capacity(parsed.offsets.len() * 4);
    for &offset in &parsed.offsets {
        let seconds = i32::try_from(i64::from(offset) * unit_seconds)
            .map_err(|_| EncodeError::conversion("a frame offset overflows the int32 time coordinate"))?;
        time.extend_from_slice(&seconds.to_le_bytes());
    }
    let axis = |first: f64, step: f64, count: usize| -> Vec<u8> {
        let mut bytes = Vec::with_capacity(count * 8);
        for index in 0..count {
            // Two roundings, as NumPy's `first + arange(n) * step` makes them.
            let value = first + (index as f64) * step;
            bytes.extend_from_slice(&value.to_le_bytes());
        }
        bytes
    };
    let latitude = axis(
        number(&grid["firstLatitude"], "firstLatitude")?,
        number(&grid["latitudeStep"], "latitudeStep")?,
        geometry.height,
    );
    let longitude = axis(
        number(&grid["firstLongitude"], "firstLongitude")?,
        number(&grid["longitudeStep"], "longitudeStep")?,
        geometry.width,
    );
    let coordinates = [
        (
            names[0].as_str(),
            time,
            parsed.offsets.len(),
            "int32",
            json!(0),
            json!({
                "units": format!("seconds since {run_time}"),
                "calendar": "proleptic_gregorian",
                "standard_name": "time",
            }),
        ),
        (
            names[1].as_str(),
            latitude,
            geometry.height,
            "float64",
            json!("NaN"),
            json!({"units": "degrees_north", "standard_name": "latitude"}),
        ),
        (
            names[2].as_str(),
            longitude,
            geometry.width,
            "float64",
            json!("NaN"),
            json!({"units": "degrees_east", "standard_name": "longitude"}),
        ),
    ];
    let write = if atomic { write_file_atomic } else { write_file };
    let mut sizes = Vec::new();
    for (name, values, length, data_type, fill_value, attributes) in coordinates {
        let document = coordinate_metadata(length, data_type, fill_value, attributes, name);
        let directory = store.join(name);
        let written = write(&directory.join("zarr.json"), &dump_json(&document))?
            + write(&directory.join("c").join("0"), &values)?;
        documents.push((name.to_owned(), document));
        sizes.push((name.to_owned(), written));
    }
    Ok(sizes)
}

// -- chunks --------------------------------------------------------------------

/// One inner chunk at its full shape: the clipped tile's codes for the frames
/// the time chunk holds, `fill` beyond the grid and beyond the axis, then the
/// `xue.delta` encode over the padded block when asked (`_pad_block` and
/// `_delta_encode` in the reference).
fn inner_chunk(
    planes: &[&[u8]],
    geometry: &Geometry,
    time_chunk: usize,
    tile: usize,
    fill: u8,
    delta: bool,
) -> Vec<u8> {
    let mut block = vec![fill; geometry.inner_length()];
    let (row, column) = geometry.tile_origin(tile);
    let (height, width) = geometry.tile_shape(tile);
    let first = time_chunk * geometry.time_chunk;
    let last = (first + geometry.time_chunk).min(geometry.frame_count);
    let frame_cells = geometry.tile_height * geometry.tile_width;
    for (slot, frame) in (first..last).enumerate() {
        let plane = planes[frame];
        for line in 0..height {
            let source = (row + line) * geometry.width + column;
            let target = slot * frame_cells + line * geometry.tile_width;
            block[target..target + width].copy_from_slice(&plane[source..source + width]);
        }
    }
    if delta {
        for index in (frame_cells..block.len()).rev() {
            block[index] = block[index].wrapping_sub(block[index - frame_cells]);
        }
    }
    block
}

/// Compress one inner chunk and prove the frame decodes back to it.
fn compress_chunk(block: &[u8]) -> Result<Vec<u8>> {
    let payload = crate::encode::convert::zstd_compress(block, ZSTD_LEVEL)?;
    let round_trip = zstd::bulk::decompress(&payload, block.len())
        .map_err(|error| EncodeError::bundle(format!("store chunk does not decompress: {error}")))?;
    if round_trip != block {
        return Err(EncodeError::bundle("store chunk does not decompress to its codes"));
    }
    Ok(payload)
}

/// One variable's array: its document and its one shard, the inner chunks in
/// row-major order (time chunk, then tile) followed by the index. `bundle`
/// and `dimensions` are a shared store's ([`write_bundle_arrays`]).
#[allow(clippy::too_many_arguments)]
fn write_array(
    variable: &StoreVariable<'_>,
    document_variable: &Value,
    geometry: &Geometry,
    directory: &Path,
    delta: bool,
    pool: &rayon::ThreadPool,
    bundle: Option<&str>,
    dimensions: &[String; 3],
) -> Result<(u64, u64, Value)> {
    let predictor = if variable.previous { "previous" } else { "raw" };
    let use_delta = delta && variable.previous;
    let fill = document_variable["quantization"]["nodataCode"]
        .as_u64()
        .and_then(|code| u8::try_from(code).ok())
        .ok_or_else(|| EncodeError::conversion(format!("{}: nodataCode is not a byte", variable.id)))?;
    if variable.planes.len() != geometry.frame_count {
        return Err(EncodeError::conversion(format!(
            "{}: {} planes for an axis of {} frames",
            variable.id,
            variable.planes.len(),
            geometry.frame_count
        )));
    }
    if let Some(plane) = variable.planes.iter().find(|plane| plane.len() != geometry.width * geometry.height) {
        return Err(EncodeError::conversion(format!(
            "{}: a plane of {} codes on a {}x{} grid",
            variable.id,
            plane.len(),
            geometry.width,
            geometry.height
        )));
    }
    let document = array_metadata(geometry, document_variable, predictor, use_delta, bundle, dimensions);
    let text = dump_json(&document);
    zarr::parse_array_metadata(std::str::from_utf8(&text).expect("UTF-8 JSON"))
        .map_err(|error| EncodeError::bundle(format!("{}: array document is invalid: {}", variable.id, error.0)))?;
    let mut written = write_file(&directory.join("zarr.json"), &text)?;

    let shard_path = directory.join("c").join("0").join("0").join("0");
    fs::create_dir_all(shard_path.parent().expect("shard directory"))
        .map_err(|error| io_error(&shard_path, &error))?;
    let file = fs::File::create(&shard_path).map_err(|error| io_error(&shard_path, &error))?;
    let mut shard = BufWriter::new(file);
    let mut entries: Vec<u8> = Vec::with_capacity(16 * geometry.chunks_per_shard());
    let mut cursor = 0u64;
    let mut count = 0usize;
    let mut append = |payloads: Vec<Vec<u8>>, shard: &mut BufWriter<fs::File>| -> Result<()> {
        for payload in payloads {
            entries.extend_from_slice(&cursor.to_le_bytes());
            entries.extend_from_slice(&(payload.len() as u64).to_le_bytes());
            cursor += payload.len() as u64;
            count += 1;
            shard.write_all(&payload).map_err(|error| io_error(&shard_path, &error))?;
        }
        Ok(())
    };
    let mut chunks = 0u64;
    for time_chunk in 0..geometry.time_chunks() {
        let payloads = pool.install(|| {
            (0..geometry.tile_count())
                .into_par_iter()
                .map(|tile| {
                    compress_chunk(&inner_chunk(&variable.planes, geometry, time_chunk, tile, fill, use_delta))
                })
                .collect::<Result<Vec<_>>>()
        })?;
        chunks += payloads.len() as u64;
        append(payloads, &mut shard)?;
    }
    // Every inner chunk the shard's shape names is stored; a time chunk past
    // the axis (none, with the shard cut to the axis) would be fill.
    let remaining = geometry.chunks_per_shard().saturating_sub(chunks as usize);
    if remaining > 0 {
        let fill_chunk = compress_chunk(&vec![fill; geometry.inner_length()])?;
        append(vec![fill_chunk; remaining], &mut shard)?;
    }
    drop(append);
    if count != geometry.chunks_per_shard() {
        return Err(EncodeError::bundle("shard payload count disagrees with the geometry"));
    }
    let checksum = zarr::crc32c(&entries);
    let mut index = entries;
    index.extend_from_slice(&checksum.to_le_bytes());
    zarr::parse_shard_index(&index, geometry.chunks_per_shard())
        .map_err(|error| EncodeError::bundle(format!("{}: shard index is invalid: {}", variable.id, error.0)))?;
    shard.write_all(&index).map_err(|error| io_error(&shard_path, &error))?;
    shard.flush().map_err(|error| io_error(&shard_path, &error))?;
    drop(shard);
    let shard_length = cursor + index.len() as u64;
    let on_disk = fs::metadata(&shard_path).map_err(|error| io_error(&shard_path, &error))?.len();
    if on_disk != shard_length {
        return Err(EncodeError::bundle(format!("{}: shard is {on_disk} bytes, not {shard_length}", variable.id)));
    }
    written += shard_length;
    Ok((written, chunks, document))
}

fn map_geometry(codes: &StoreCodes<'_>, frame_count: usize, width: usize, height: usize) -> Geometry {
    Geometry {
        frame_count,
        height,
        width,
        time_chunk: TIME_CHUNK,
        tile_height: codes.tile.1.min(height),
        tile_width: codes.tile.0.min(width),
    }
}

fn series_geometry(block: (usize, usize), frame_count: usize, width: usize, height: usize) -> Geometry {
    Geometry {
        frame_count,
        height,
        width,
        time_chunk: frame_count,
        tile_height: block.1,
        tile_width: block.0,
    }
}

/// The compression pool a caller outside the crate hands to [`write_store`].
pub fn compression_pool(workers: usize) -> Result<rayon::ThreadPool> {
    rayon::ThreadPoolBuilder::new()
        .num_threads(workers.max(1))
        .build()
        .map_err(|error| EncodeError::conversion(format!("cannot start zstd pool: {error}")))
}

fn primary_dimensions() -> [String; 3] {
    DIMENSIONS.map(str::to_owned)
}

/// The geometry a bundle's codes are cut with, and the metadata's variable
/// documents, which must name the codes' variables in order.
fn store_geometry<'m>(
    codes: &StoreCodes<'m>,
    series: Option<(usize, usize)>,
) -> Result<(Geometry, &'m Vec<Value>)> {
    let metadata = codes.metadata;
    let grid = &metadata["grid"];
    let dimension = |key: &str| -> Result<usize> {
        grid[key]
            .as_u64()
            .filter(|&value| value > 0)
            .map(|value| value as usize)
            .ok_or_else(|| EncodeError::conversion(format!("metadata grid {key} is invalid")))
    };
    let width = dimension("width")?;
    let height = dimension("height")?;
    let frame_count = metadata["time"]["frameCount"]
        .as_u64()
        .filter(|&value| value > 0)
        .map(|value| value as usize)
        .ok_or_else(|| EncodeError::conversion("metadata frameCount is invalid"))?;
    let geometry = match series {
        Some(block) => {
            if block.0 == 0 || block.1 == 0 {
                return Err(EncodeError::conversion("a series block must be two positive cell counts"));
            }
            series_geometry(block, frame_count, width, height)
        }
        None => map_geometry(codes, frame_count, width, height),
    };
    let document_variables = metadata["variables"]
        .as_array()
        .ok_or_else(|| EncodeError::conversion("metadata variables missing"))?;
    let names: Vec<&str> = document_variables
        .iter()
        .map(|variable| variable["id"].as_str().unwrap_or_default())
        .collect();
    let given: Vec<&str> = codes.variables.iter().map(|variable| variable.id.as_str()).collect();
    if names != given {
        return Err(EncodeError::bundle("the codes do not name the metadata's variables in order"));
    }
    Ok((geometry, document_variables))
}

/// Write one bundle's store at `store` (replaced whole). `series` cuts the
/// series layout over that block instead of the map layout; `delta` puts
/// `xue.delta` in front of `bytes` on every previous-predicted variable.
pub fn write_store(
    codes: &StoreCodes<'_>,
    store: &Path,
    delta: bool,
    series: Option<(usize, usize)>,
    pool: &rayon::ThreadPool,
) -> Result<StoreReport> {
    let metadata = codes.metadata;
    let (geometry, document_variables) = store_geometry(codes, series)?;
    let dimensions = primary_dimensions();

    if store.exists() {
        if !store.is_dir() {
            return Err(EncodeError::conversion(format!("{} exists and is not a directory", store.display())));
        }
        fs::remove_dir_all(store).map_err(|error| io_error(store, &error))?;
    }
    fs::create_dir_all(store).map_err(|error| io_error(store, &error))?;

    let mut report = StoreReport {
        path: store.to_path_buf(),
        byte_length: 0,
        crc32: String::new(),
        delta,
        arrays: Vec::new(),
        chunk_count: 0,
    };
    let mut documents: Vec<(String, Value)> = Vec::new();
    for (variable, document_variable) in codes.variables.iter().zip(document_variables) {
        let (written, chunks, document) = write_array(
            variable,
            document_variable,
            &geometry,
            &store.join(&variable.id),
            delta,
            pool,
            None,
            &dimensions,
        )?;
        report.arrays.push((variable.id.clone(), written));
        report.byte_length += written;
        report.chunk_count += chunks;
        documents.push((variable.id.clone(), document));
    }
    for (name, written) in write_coordinates(metadata, &geometry, store, &dimensions, false, &mut documents)? {
        report.arrays.push((name, written));
        report.byte_length += written;
    }
    // The root group last, its consolidated metadata every array document
    // just written; its CRC-32 is the store's `?v=`.
    let mut consolidated = Map::new();
    for (name, document) in documents {
        consolidated.insert(name, document);
    }
    let root = json!({
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"xue": metadata.clone(), "xue_profile": PROFILE_VERSION},
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": false,
            "metadata": Value::Object(consolidated),
        },
    });
    let text = dump_json(&root);
    zarr::parse_group_metadata(std::str::from_utf8(&text).expect("UTF-8 JSON"))
        .map_err(|error| EncodeError::bundle(format!("group document is invalid: {}", error.0)))?;
    report.byte_length += write_file(&store.join("zarr.json"), &text)?;
    report.crc32 = format!("{:08x}", crc32_plane(&text));
    Ok(report)
}

// -- the shared store (profile 2) ----------------------------------------------

/// `bundles.zarr` for the full tier, `bundles.<tier>.zarr` for a rung of the
/// resolution ladder.
pub fn shared_store_name(tier: Option<&str>) -> String {
    match tier {
        Some(tier) => format!("{SHARED_STORE_STEM}.{tier}.zarr"),
        None => format!("{SHARED_STORE_STEM}.zarr"),
    }
}

/// `<store>/zarr.part.<first bundle id>.json`: a build's bundles are disjoint
/// from every other build's of the same run, so the first one names the part
/// uniquely.
pub fn part_path(store: &Path, first_bundle_id: &str) -> PathBuf {
    store.join(format!("{PART_PREFIX}{first_bundle_id}.json"))
}

/// `^[a-z][a-z0-9]*$`: a bundle id, and the suffix of a coordinate named
/// for a grid family.
pub fn is_coordinate_suffix(text: &str) -> bool {
    let mut bytes = text.bytes();
    bytes.next().is_some_and(|first| first.is_ascii_lowercase())
        && bytes.all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
}

/// `Some("")` for the primary name, `Some(family)` for `<primary>_<family>`,
/// `None` for anything else.
fn grid_suffix<'a>(name: &'a str, primary: &str) -> Option<&'a str> {
    if name == primary {
        return Some("");
    }
    name.strip_prefix(primary)
        .and_then(|rest| rest.strip_prefix('_'))
        .filter(|family| is_coordinate_suffix(family))
}

/// A bundle's dimension names (`docs/zarr-profile.md`, "Coordinates"): `time`
/// or `time_<bundle id>`, and `latitude` / `longitude` or `latitude_<family>`
/// / `longitude_<family>` of one family id.
fn check_dimensions(bundle_id: &str, dimensions: &[String]) -> Result<()> {
    let valid = dimensions.len() == 3
        && (dimensions[0] == "time" || dimensions[0] == format!("time_{bundle_id}"))
        && grid_suffix(&dimensions[1], "latitude")
            .is_some_and(|latitude| grid_suffix(&dimensions[2], "longitude") == Some(latitude));
    if valid {
        Ok(())
    } else {
        Err(EncodeError::bundle(format!(
            "{bundle_id}: dimension names {dimensions:?} are not the profile's"
        )))
    }
}

/// One bundle written into a shared store (`BundleWrite` in the reference).
///
/// `documents` holds every array document the bundle uses, its variable
/// arrays in the metadata's order and then the coordinate arrays its
/// dimensions name; `byte_length` is those arrays' objects (each `zarr.json`
/// plus shard or chunk), which is what a viewer downloads for the bundle
/// alone and what its manifest entry reports. `crc32` is the CRC-32 of the
/// group document naming the bundle, stamped once that is written: here the
/// part's.
#[derive(Debug, Clone)]
pub struct BundleWrite {
    pub bundle_id: String,
    pub path: PathBuf,
    pub metadata: Value,
    pub dimensions: [String; 3],
    pub documents: Vec<(String, Value)>,
    pub byte_length: u64,
    pub arrays: Vec<(String, u64)>,
    pub chunk_count: u64,
    pub crc32: String,
}

impl BundleWrite {
    /// The report block, keyed as the reference's `BundleWrite.to_dict`.
    pub fn to_value(&self) -> Value {
        let mut arrays = Map::new();
        for (name, written) in &self.arrays {
            arrays.insert(name.clone(), json!(written));
        }
        json!({
            "path": self.path.display().to_string(),
            "bundle": self.bundle_id,
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "delta": false,
            "indexLocation": "end",
            "dimensions": self.dimensions,
            "arrays": Value::Object(arrays),
            "chunkCount": self.chunk_count,
            "comparableChunks": 0,
            "identicalChunks": 0,
        })
    }
}

/// Write one bundle's variable arrays and the coordinate arrays its
/// `dimensions` name into the shared store `store`, which is created if
/// absent and otherwise left as it is: other bundles' arrays, written before
/// or concurrently, stay. The group document is not touched.
///
/// `dimensions` is what the encoder decided for the bundle
/// (`convert::store_dimensions`); a coordinate shared with another bundle is
/// the same bytes from either, and is written by rename so two concurrent
/// writers never tear it. Arrays, codecs and chunking are exactly a
/// one-bundle store's, with `attributes.xue.bundle` added.
pub fn write_bundle_arrays(
    codes: &StoreCodes<'_>,
    store: &Path,
    bundle_id: &str,
    dimensions: &[String; 3],
    pool: &rayon::ThreadPool,
) -> Result<BundleWrite> {
    if !is_coordinate_suffix(bundle_id) || bundle_id == SHARED_STORE_STEM {
        return Err(EncodeError::bundle(format!(
            "{bundle_id:?} cannot name a bundle of a shared store"
        )));
    }
    check_dimensions(bundle_id, dimensions)?;
    let metadata = codes.metadata;
    let (geometry, document_variables) = store_geometry(codes, None)?;
    if store.exists() && !store.is_dir() {
        return Err(EncodeError::conversion(format!("{} exists and is not a directory", store.display())));
    }
    fs::create_dir_all(store).map_err(|error| io_error(store, &error))?;

    let mut report = BundleWrite {
        bundle_id: bundle_id.to_owned(),
        path: store.to_path_buf(),
        metadata: metadata.clone(),
        dimensions: dimensions.clone(),
        documents: Vec::new(),
        byte_length: 0,
        arrays: Vec::new(),
        chunk_count: 0,
        crc32: String::new(),
    };
    for (variable, document_variable) in codes.variables.iter().zip(document_variables) {
        if dimensions.contains(&variable.id) {
            return Err(EncodeError::bundle(format!(
                "{bundle_id}: variable {} is named like a coordinate",
                variable.id
            )));
        }
        let (written, chunks, document) = write_array(
            variable,
            document_variable,
            &geometry,
            &store.join(&variable.id),
            false,
            pool,
            Some(bundle_id),
            dimensions,
        )?;
        report.arrays.push((variable.id.clone(), written));
        report.chunk_count += chunks;
        report.documents.push((variable.id.clone(), document));
    }
    let sizes = write_coordinates(metadata, &geometry, store, dimensions, true, &mut report.documents)?;
    report.arrays.extend(sizes);
    report.byte_length = report.arrays.iter().map(|(_, written)| written).sum();
    Ok(report)
}

/// A shared store's group document over `bundles`, in manifest order
/// (`group_document` in the reference).
///
/// Key order is part of the bytes (`docs/zarr-profile.md`, "Group
/// document"): `attributes` holds `xue_profile` then `xue_bundles`, the
/// bundles in the given order; `consolidated_metadata.metadata` holds every
/// variable array, bundle by bundle and in each bundle's `variables` order,
/// then the primary coordinates `time`, `latitude`, `longitude` that some
/// bundle uses, then every other coordinate in the order its first bundle
/// names it. Two bundles naming one coordinate must carry the same document
/// for it.
pub fn group_document(bundles: &[&BundleWrite]) -> Result<Value> {
    if bundles.is_empty() {
        return Err(EncodeError::bundle("a shared store holds at least one bundle"));
    }
    let mut metadata_by_bundle = Map::new();
    let mut variable_arrays = Map::new();
    let mut coordinates = Map::new();
    for bundle in bundles {
        let bundle_id = bundle.bundle_id.as_str();
        if metadata_by_bundle.contains_key(bundle_id) {
            return Err(EncodeError::bundle(format!("bundle {bundle_id} appears twice in one shared store")));
        }
        metadata_by_bundle.insert(bundle_id.to_owned(), bundle.metadata.clone());
        let document_of = |name: &str| bundle.documents.iter().find(|(held, _)| held == name).map(|(_, document)| document);
        let variables = bundle.metadata["variables"]
            .as_array()
            .ok_or_else(|| EncodeError::bundle(format!("{bundle_id}: metadata variables missing")))?;
        let mut dimensions: Option<Vec<String>> = None;
        for variable in variables {
            let name = variable["id"].as_str().unwrap_or_default();
            let document = document_of(name)
                .ok_or_else(|| EncodeError::bundle(format!("{bundle_id}: no array document for {name}")))?;
            if variable_arrays.contains_key(name) || coordinates.contains_key(name) {
                return Err(EncodeError::bundle(format!("array {name} appears twice in one shared store")));
            }
            let names: Vec<String> = document["dimension_names"]
                .as_array()
                .map(|names| names.iter().map(|name| name.as_str().unwrap_or_default().to_owned()).collect())
                .unwrap_or_default();
            match &dimensions {
                None => dimensions = Some(names),
                Some(held) if *held != names => {
                    return Err(EncodeError::bundle(format!(
                        "{bundle_id}: its arrays do not share one set of dimension names"
                    )))
                }
                Some(_) => {}
            }
            variable_arrays.insert(name.to_owned(), document.clone());
        }
        let dimensions = dimensions.unwrap_or_default();
        check_dimensions(bundle_id, &dimensions)?;
        for name in &dimensions {
            let document = document_of(name).ok_or_else(|| {
                EncodeError::bundle(format!("{bundle_id}: no array document for its coordinate {name}"))
            })?;
            if variable_arrays.contains_key(name) {
                return Err(EncodeError::bundle(format!("coordinate {name} is named like a variable")));
            }
            match coordinates.get(name) {
                Some(held) if held != document => {
                    return Err(EncodeError::bundle(format!(
                        "coordinate {name} differs between the bundles that use it"
                    )))
                }
                Some(_) => {}
                None => {
                    coordinates.insert(name.clone(), document.clone());
                }
            }
        }
    }
    let mut ordered = variable_arrays;
    for name in DIMENSIONS {
        if let Some(document) = coordinates.get(name) {
            ordered.insert(name.to_owned(), document.clone());
        }
    }
    for (name, document) in &coordinates {
        if !DIMENSIONS.contains(&name.as_str()) {
            ordered.insert(name.clone(), document.clone());
        }
    }
    Ok(json!({
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {
            "xue_profile": SHARED_PROFILE_VERSION,
            "xue_bundles": Value::Object(metadata_by_bundle),
        },
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": false,
            "metadata": Value::Object(ordered),
        },
    }))
}

/// The partial group document over the bundles one build wrote into `store`,
/// in manifest order: [`group_document`] over them alone, at
/// [`part_path`] of the first. Returns its path and its CRC-32, which the
/// build's entries carry until the reference's `finalize_store` writes the
/// store's group document from the parts.
///
/// The part is parsed back as every bundle's view of a profile-2 group, the
/// decoder's own check (`zarr::parse_group_metadata_for`), so a part the
/// readers would refuse once merged never leaves the encoder.
pub fn write_part(store: &Path, bundles: &[&BundleWrite]) -> Result<(PathBuf, String)> {
    let text = dump_json(&group_document(bundles)?);
    let utf8 = std::str::from_utf8(&text).expect("UTF-8 JSON");
    for bundle in bundles {
        zarr::parse_group_metadata_for(utf8, &bundle.bundle_id).map_err(|error| {
            EncodeError::bundle(format!("{}: partial group document is invalid: {}", bundle.bundle_id, error.0))
        })?;
    }
    let path = part_path(store, &bundles[0].bundle_id);
    write_file(&path, &text)?;
    Ok((path, format!("{:08x}", crc32_plane(&text))))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pool() -> rayon::ThreadPool {
        rayon::ThreadPoolBuilder::new().num_threads(2).build().expect("pool")
    }

    #[test]
    fn json_is_spelled_the_way_python_indents_it() {
        let value = json!({"a": [1, 2.5, "°C"], "b": {}, "c": [], "d": {"e": true}});
        let text = String::from_utf8(dump_json(&value)).expect("UTF-8");
        assert_eq!(
            text,
            "{\n  \"a\": [\n    1,\n    2.5,\n    \"°C\"\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": {\n    \"e\": true\n  }\n}\n"
        );
    }

    #[test]
    fn a_padded_chunk_differences_every_frame_against_the_one_before() {
        let geometry = Geometry { frame_count: 2, height: 3, width: 3, time_chunk: 6, tile_height: 2, tile_width: 2 };
        let first: Vec<u8> = (0..9).collect();
        let second: Vec<u8> = (10..19).collect();
        let planes: Vec<&[u8]> = vec![&first, &second];
        let raw = inner_chunk(&planes, &geometry, 0, 3, 255, false);
        // The south-east tile is one cell; the rest is fill, and so are the
        // four frames past the axis.
        assert_eq!(&raw[..4], &[8, 255, 255, 255]);
        assert_eq!(&raw[4..8], &[18, 255, 255, 255]);
        assert!(raw[8..].iter().all(|&code| code == 255));
        let stored = inner_chunk(&planes, &geometry, 0, 3, 255, true);
        assert_eq!(&stored[..4], &[8, 255, 255, 255]);
        assert_eq!(&stored[4..8], &[10, 0, 0, 0]);
        assert_eq!(&stored[8..12], &[255u8.wrapping_sub(18), 0, 0, 0]);
    }

    #[test]
    fn a_store_reads_back_through_the_decoder_validators() {
        let metadata = json!({
            "schemaVersion": 1,
            "model": "TEST",
            "product": "test",
            "runTime": "2026-08-14T06:00:00Z",
            "profile": "quality",
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 7},
            "grid": {"width": 5, "height": 3, "firstLongitude": -180.0, "firstLatitude": 90.0,
                     "longitudeStep": 72.0, "latitudeStep": -90.0, "wrapLongitude": true},
            "variables": [{"numericId": 1, "id": "tmp2m", "label": "T", "unit": "°C",
                           "quantization": {"type": "linear", "offset": -60.0, "scale": 0.5,
                                            "minimumCode": 0, "maximumCode": 220, "nodataCode": 255}}],
        });
        let planes: Vec<Vec<u8>> = (0..7u8).map(|frame| (0..15u8).map(|cell| cell * 3 + frame).collect()).collect();
        let codes = StoreCodes {
            metadata: &metadata,
            variables: vec![StoreVariable {
                id: "tmp2m".into(),
                previous: true,
                planes: planes.iter().map(Vec::as_slice).collect(),
            }],
            tile: (2, 2),
        };
        let root = std::env::temp_dir().join(format!("xue-zarrstore-{}", std::process::id()));
        let report = write_store(&codes, &root.join("tmp2m.zarr"), false, None, &pool()).expect("map store");
        // Seven frames make two time chunks, of six tiles each.
        assert_eq!(report.chunk_count, 12);
        let series = write_store(&codes, &root.join("tmp2m.series.zarr"), true, Some(SERIES_BLOCK), &pool())
            .expect("series store");
        assert_eq!(series.chunk_count, 1);
        let on_disk: u64 = ["tmp2m.zarr"]
            .iter()
            .map(|store| walk(&root.join(store)))
            .sum();
        assert_eq!(on_disk, report.byte_length);
        let _ = fs::remove_dir_all(&root);
    }

    fn bundle_metadata(variables: &[&str], frame_count: u64) -> Value {
        json!({
            "schemaVersion": 1,
            "model": "TEST",
            "product": "test",
            "runTime": "2026-08-14T06:00:00Z",
            "profile": "quality",
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": frame_count},
            "grid": {"width": 5, "height": 3, "firstLongitude": -180.0, "firstLatitude": 90.0,
                     "longitudeStep": 72.0, "latitudeStep": -90.0, "wrapLongitude": true},
            "variables": variables.iter().enumerate().map(|(index, id)| json!({
                "numericId": index + 1, "id": id, "label": id, "unit": "1",
                "quantization": {"type": "linear", "offset": 0.0, "scale": 1.0,
                                 "minimumCode": 0, "maximumCode": 250, "nodataCode": 255},
            })).collect::<Vec<_>>(),
        })
    }

    fn dimensions(names: [&str; 3]) -> [String; 3] {
        names.map(str::to_owned)
    }

    /// The part's key order is the reference's: `xue_profile` before
    /// `xue_bundles`, bundles in the order given, then every variable array
    /// bundle by bundle, the primary coordinates, and the other coordinates
    /// in first-appearance order; arrays lead their `xue` block with the
    /// bundle and carry the bundle's dimension names.
    #[test]
    fn a_part_orders_bundles_arrays_and_coordinates_like_the_reference() {
        let root = std::env::temp_dir().join(format!("xue-shared-store-{}", std::process::id()));
        let store = root.join(shared_store_name(None));
        let wind = bundle_metadata(&["ugrd10m", "vgrd10m"], 3);
        let rate = bundle_metadata(&["prate"], 2);
        let height = bundle_metadata(&["hgt500"], 3);
        let planes: Vec<Vec<u8>> = (0..3u8).map(|frame| vec![frame; 15]).collect();
        let variable = |id: &str, count: usize| StoreVariable {
            id: id.into(),
            previous: true,
            planes: planes[..count].iter().map(Vec::as_slice).collect(),
        };
        let write = |metadata: &Value, variables: Vec<StoreVariable<'_>>, bundle_id: &str, names: [&str; 3]| {
            let codes = StoreCodes { metadata, variables, tile: (2, 2) };
            write_bundle_arrays(&codes, &store, bundle_id, &dimensions(names), &pool()).expect("bundle")
        };
        let prate = write(&rate, vec![variable("prate", 2)], "prate", ["time_prate", "latitude", "longitude"]);
        let wind10m = write(
            &wind,
            vec![variable("ugrd10m", 3), variable("vgrd10m", 3)],
            "wind10m",
            ["time", "latitude", "longitude"],
        );
        let hgt500 = write(&height, vec![variable("hgt500", 3)], "hgt500", ["time", "latitude_pgb", "longitude_pgb"]);

        let (path, crc32) = write_part(&store, &[&prate, &wind10m, &hgt500]).expect("part");
        assert_eq!(path, store.join("zarr.part.prate.json"));
        assert!(!store.join("zarr.json").exists(), "the group document is the reference's to write");
        let text = fs::read(&path).expect("part");
        assert_eq!(crc32, format!("{:08x}", crc32_plane(&text)));
        let part: Value = serde_json::from_slice(&text).expect("JSON");
        let keys = |value: &Value| value.as_object().expect("object").keys().cloned().collect::<Vec<_>>();
        assert_eq!(keys(&part), ["zarr_format", "node_type", "attributes", "consolidated_metadata"]);
        assert_eq!(keys(&part["attributes"]), ["xue_profile", "xue_bundles"]);
        assert_eq!(keys(&part["attributes"]["xue_bundles"]), ["prate", "wind10m", "hgt500"]);
        assert_eq!(
            keys(&part["consolidated_metadata"]["metadata"]),
            [
                "prate", "ugrd10m", "vgrd10m", "hgt500", "time", "latitude", "longitude", "time_prate",
                "latitude_pgb", "longitude_pgb",
            ]
        );
        let array = &part["consolidated_metadata"]["metadata"]["hgt500"];
        assert_eq!(keys(&array["attributes"]["xue"]), ["bundle", "variable", "predictor"]);
        assert_eq!(array["attributes"]["xue"]["bundle"], "hgt500");
        assert_eq!(array["dimension_names"], json!(["time", "latitude_pgb", "longitude_pgb"]));
        assert_eq!(keys(&wind10m.to_value())[..3], ["path", "bundle", "byteLength"]);
        // A bundle's size is its arrays and the coordinates it uses.
        let used: u64 = ["ugrd10m", "vgrd10m", "time", "latitude", "longitude"]
            .iter()
            .map(|name| walk(&store.join(name)))
            .sum();
        assert_eq!(wind10m.byte_length, used);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn names_outside_the_profile_are_refused() {
        assert!(check_dimensions("prate", &dimensions(["time_prate", "latitude", "longitude"])).is_ok());
        assert!(check_dimensions("prate", &dimensions(["time_gust", "latitude", "longitude"])).is_err());
        assert!(check_dimensions("hgt500", &dimensions(["time", "latitude_pgb", "longitude_pgb"])).is_ok());
        assert!(check_dimensions("hgt500", &dimensions(["time", "latitude_pgb", "longitude"])).is_err());
        assert!(check_dimensions("hgt500", &dimensions(["time", "latitude_", "longitude_"])).is_err());
        assert!(check_dimensions("hgt500", &dimensions(["time", "latitude_Pgb", "longitude_Pgb"])).is_err());
        assert_eq!(shared_store_name(Some("half")), "bundles.half.zarr");
    }

    fn walk(path: &Path) -> u64 {
        fs::read_dir(path)
            .expect("store directory")
            .map(|entry| {
                let path = entry.expect("entry").path();
                if path.is_dir() {
                    walk(&path)
                } else {
                    fs::metadata(&path).expect("file").len()
                }
            })
            .sum()
    }
}
