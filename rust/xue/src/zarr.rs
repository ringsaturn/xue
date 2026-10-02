//! Reading the Xue Zarr profile.
//!
//! `docs/zarr-profile.md` is normative. A bundle is one Zarr v3 group whose
//! `attributes.xue` is the bundle metadata verbatim, with one sharded `uint8`
//! array per variable. This module is the format's read side in Rust: the two
//! metadata documents, the shard index, the arithmetic that maps a frame and a
//! tile to a byte span, the geographic cell a point probes, and the codebook
//! that turns a stored code into a physical value.
//!
//! It is deliberately IO-free — it takes `&str` / `&[u8]` and returns values —
//! so the same code serves the native worker and, through wasm-bindgen, the
//! browser. The container decoder in [`crate::decode`] shares its metadata
//! validation: [`parse_bundle_metadata`] calls the container's own
//! `parse_metadata`, so a group the container would refuse is refused here too.
//! The chunk payload itself is decoded by [`crate::decode_chunk`], the same
//! Zstandard + `PREVIOUS` residual path the `.xue` reader uses.

use serde_json::Value;

use crate::format::{err, DecodeError};

/// Bytes of one `(offset, nbytes)` pair in a shard index.
pub const INDEX_ENTRY_BYTES: usize = 16;
/// The CRC-32C trailer of a shard index.
pub const INDEX_CHECKSUM_BYTES: usize = 4;
/// The array-to-array delta codec's name in an inner codec chain.
pub const DELTA_CODEC: &str = "xue.delta";
/// Padding frames and cells are this many bytes per value.
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

// -- grid ----------------------------------------------------------------------

/// The bundle's regular grid, in the container's row-major, north-to-south,
/// west-to-east order.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Grid {
    pub width: u32,
    pub height: u32,
    pub first_longitude: f64,
    pub first_latitude: f64,
    pub longitude_step: f64,
    pub latitude_step: f64,
    /// Whether a longitude wraps around the antimeridian (a global or
    /// antimeridian-crossing grid).
    pub wraps: bool,
}

impl Grid {
    /// The defaults `web/src/probe.ts::geoGrid` applies to a missing field.
    pub fn from_metadata(value: &Value) -> Grid {
        let grid = value.get("grid").unwrap_or(&Value::Null);
        let width = number(grid, "width").unwrap_or(0.0);
        let longitude_step = number(grid, "longitudeStep").unwrap_or(0.25);
        let wraps = grid
            .get("wrapLongitude")
            .and_then(|v| v.as_bool())
            .unwrap_or_else(|| (width * longitude_step - 360.0).abs() < 1e-6);
        Grid {
            width: width as u32,
            height: number(grid, "height").unwrap_or(0.0) as u32,
            first_longitude: number(grid, "firstLongitude").unwrap_or(-180.0),
            first_latitude: number(grid, "firstLatitude").unwrap_or(90.0),
            longitude_step,
            latitude_step: number(grid, "latitudeStep").unwrap_or(-0.25),
            wraps,
        }
    }
}

/// The cell a point probes: the grid index and the cell's own coordinates.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ProbeCell {
    pub column: u32,
    pub row: u32,
    pub longitude: f64,
    pub latitude: f64,
}

fn number(value: &Value, key: &str) -> Option<f64> {
    value.get(key).and_then(|v| v.as_f64())
}

fn wrap(value: f64, modulus: f64) -> f64 {
    ((value % modulus) + modulus) % modulus
}

fn normalize_longitude(longitude: f64) -> f64 {
    wrap(longitude + 180.0, 360.0) - 180.0
}

/// The cell a point reads, or `None` when it is off the grid (a regional
/// model's rectangle, a satellite disk). The rules are the shell probe's and
/// the fragment shader's (`web/src/probe.ts::probeCell`).
pub fn probe_cell(grid: &Grid, longitude: f64, latitude: f64) -> Option<ProbeCell> {
    if grid.width == 0
        || grid.height == 0
        || grid.longitude_step == 0.0
        || grid.latitude_step == 0.0
        || !longitude.is_finite()
        || !latitude.is_finite()
    {
        return None;
    }
    let delta = wrap(longitude - grid.first_longitude, 360.0);
    // `Math.round` rounds half toward +Infinity, not away from zero.
    let column = (delta / grid.longitude_step + 0.5).floor();
    let column = if grid.wraps {
        wrap(column, f64::from(grid.width))
    } else if column < 0.0 || column >= f64::from(grid.width) {
        return None;
    } else {
        column
    };
    let row = ((latitude - grid.first_latitude) / grid.latitude_step + 0.5).floor();
    if row < 0.0 || row >= f64::from(grid.height) {
        return None;
    }
    Some(ProbeCell {
        column: column as u32,
        row: row as u32,
        longitude: normalize_longitude(grid.first_longitude + column * grid.longitude_step),
        latitude: grid.first_latitude + row * grid.latitude_step,
    })
}

// -- quantisation --------------------------------------------------------------

/// A codebook: how a stored `uint8` code maps back to a physical value
/// (`docs/format.md`, "Codebooks").
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Quantization {
    Linear {
        offset: f64,
        scale: f64,
        minimum_code: f64,
        maximum_code: f64,
        nodata_code: f64,
    },
    Log1p {
        trace: f64,
        scale: f64,
        maximum: f64,
        minimum_code: f64,
        maximum_code: f64,
        zero_code: f64,
        overflow_code: f64,
        nodata_code: f64,
    },
}

impl Quantization {
    /// The step a linear codebook quantises against, for a response's
    /// informational `quantization.step`.
    pub fn step(&self) -> Option<f64> {
        match self {
            Quantization::Linear { scale, .. } => Some(*scale),
            Quantization::Log1p { .. } => None,
        }
    }
}

/// Code → physical value, or `None` for a missing/overflow code
/// (`web/src/palettes.ts::decodeValue`, and the schema v3 block beside it).
pub fn decode_value(quantization: &Quantization, code: u8) -> Option<f64> {
    let code = f64::from(code);
    match *quantization {
        Quantization::Linear {
            offset,
            scale,
            maximum_code,
            nodata_code,
            ..
        } => {
            if code == nodata_code || code > maximum_code {
                None
            } else {
                Some(offset + code * scale)
            }
        }
        Quantization::Log1p {
            trace,
            scale,
            maximum,
            minimum_code,
            maximum_code,
            zero_code,
            overflow_code,
            nodata_code,
        } => {
            if code == nodata_code {
                return None;
            }
            if code == zero_code {
                return Some(0.0);
            }
            if code > overflow_code {
                return None;
            }
            let lo = (trace / scale).ln_1p();
            let hi = (maximum / scale).ln_1p();
            let unit = (code - minimum_code) / (maximum_code - minimum_code);
            Some(scale * (lo + unit * (hi - lo)).exp_m1())
        }
    }
}

fn parse_quantization(value: &Value) -> Result<Quantization, DecodeError> {
    let kind = value.get("type").and_then(|v| v.as_str());
    let code = |key: &str| -> Result<f64, DecodeError> {
        value
            .get(key)
            .and_then(|v| v.as_f64())
            .ok_or_else(|| err(format!("quantization {key} is invalid")))
    };
    match kind {
        Some("linear") => Ok(Quantization::Linear {
            offset: code("offset")?,
            scale: code("scale")?,
            minimum_code: code("minimumCode")?,
            maximum_code: code("maximumCode")?,
            nodata_code: code("nodataCode")?,
        }),
        Some("log1p") => Ok(Quantization::Log1p {
            trace: code("trace")?,
            scale: code("scale")?,
            maximum: code("maximum")?,
            minimum_code: code("minimumCode")?,
            maximum_code: code("maximumCode")?,
            zero_code: code("zeroCode")?,
            overflow_code: code("overflowCode")?,
            nodata_code: code("nodataCode")?,
        }),
        _ => Err(err("unsupported bundle quantization")),
    }
}

// -- bundle metadata -----------------------------------------------------------

/// One variable in the group's metadata.
#[derive(Debug, Clone)]
pub struct VariableMetadata {
    pub numeric_id: u8,
    pub id: String,
    pub label: Option<String>,
    pub unit: Option<String>,
    pub quantization: Quantization,
}

/// The bundle metadata a store's group carries in `attributes.xue`, reduced to
/// what a reader needs: the grid, the materialized time axis and the variables
/// with their codebooks.
#[derive(Debug, Clone)]
pub struct BundleMetadata {
    pub schema_version: u8,
    pub run_time: Option<String>,
    pub unit_seconds: u32,
    pub offsets: Vec<u16>,
    pub grid: Grid,
    pub variables: Vec<VariableMetadata>,
}

impl BundleMetadata {
    /// Seconds since the run time for each frame offset.
    pub fn axis_seconds(&self, run_time: &str) -> Result<Vec<f64>, DecodeError> {
        let run_ms = parse_iso(run_time)
            .ok_or_else(|| err("metadata runTime is not an ISO 8601 instant"))?;
        Ok(self
            .offsets
            .iter()
            .map(|&offset| run_ms + f64::from(offset) * f64::from(self.unit_seconds) * 1000.0)
            .collect())
    }
}

/// Parse and validate a bundle metadata JSON document. Validation is the
/// container decoder's own (`crate::decode::metadata`), so the two readers
/// agree on what is a valid document; the geo and codebook fields the API
/// needs are read off the same object afterwards.
pub fn parse_bundle_metadata(text: &str) -> Result<BundleMetadata, DecodeError> {
    let validated = crate::decode::metadata::parse_metadata(text.as_bytes())?;
    let value: Value = serde_json::from_str(text).map_err(|_| err("metadata is not valid JSON"))?;
    let object = value
        .as_object()
        .ok_or_else(|| err("metadata must be a JSON object"))?;
    let variables = object
        .get("variables")
        .and_then(|v| v.as_array())
        .ok_or_else(|| err("metadata variables missing"))?;
    let mut parsed = Vec::with_capacity(variables.len());
    for variable in variables {
        let numeric_id = variable
            .get("numericId")
            .and_then(|v| v.as_u64())
            .filter(|&id| (1..=255).contains(&id))
            .ok_or_else(|| err("variable numericId is invalid"))?;
        let id = variable
            .get("id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| err("variable id is invalid"))?;
        let quantization = parse_quantization(
            variable
                .get("quantization")
                .ok_or_else(|| err("variable quantization missing"))?,
        )?;
        parsed.push(VariableMetadata {
            numeric_id: numeric_id as u8,
            id: id.to_owned(),
            label: variable
                .get("label")
                .and_then(|v| v.as_str())
                .map(str::to_owned),
            unit: variable
                .get("unit")
                .and_then(|v| v.as_str())
                .map(str::to_owned),
            quantization,
        });
    }
    Ok(BundleMetadata {
        schema_version: validated.schema_version,
        run_time: object
            .get("runTime")
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        unit_seconds: validated.unit_seconds,
        offsets: validated.offsets.clone(),
        grid: Grid::from_metadata(&value),
        variables: parsed,
    })
}

/// A parsed group document: the metadata, its compact re-serialization and the
/// profile version.
#[derive(Debug, Clone)]
pub struct ZarrGroup {
    pub metadata: BundleMetadata,
    pub metadata_json: String,
    pub profile: u32,
}

/// The store root's `zarr.json` (`docs/zarr-profile.md`, "Attributes").
pub fn parse_group_metadata(text: &str) -> Result<ZarrGroup, DecodeError> {
    let value: Value =
        serde_json::from_str(text).map_err(|_| err("group metadata is not valid JSON"))?;
    if value.get("zarr_format").and_then(|v| v.as_u64()) != Some(3)
        || value.get("node_type").and_then(|v| v.as_str()) != Some("group")
    {
        return Err(err("not a Zarr v3 group"));
    }
    let attributes = value
        .get("attributes")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("group attributes missing"))?;
    let xue = attributes
        .get("xue")
        .filter(|v| v.is_object())
        .ok_or_else(|| err("group attributes carry no xue metadata block"))?;
    let profile = attributes
        .get("xue_profile")
        .and_then(|v| v.as_u64())
        .filter(|&profile| profile >= 1)
        .ok_or_else(|| err("group attributes carry no xue_profile version"))?;
    let metadata_json =
        serde_json::to_string(xue).map_err(|_| err("xue metadata is not serializable"))?;
    let metadata = parse_bundle_metadata(&metadata_json)?;
    Ok(ZarrGroup {
        metadata,
        metadata_json,
        profile: profile as u32,
    })
}

// -- array metadata ------------------------------------------------------------

/// Where a shard's index sits within the object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IndexLocation {
    Start,
    End,
}

/// How one variable's array is cut (`docs/zarr-profile.md`, "Array
/// metadata"). `time_chunk`, `tile_height` and `tile_width` are the inner
/// chunk; `shard_frames` is the outer chunk's time extent, a positive multiple
/// of the time chunk.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ArrayLayout {
    pub frame_count: u32,
    pub height: u32,
    pub width: u32,
    pub time_chunk: u32,
    pub tile_height: u32,
    pub tile_width: u32,
    pub tile_rows: u32,
    pub tile_columns: u32,
    pub tile_count: u32,
    pub time_chunks: u32,
    pub shard_frames: u32,
    pub shard_count: u32,
    pub chunks_per_shard: u32,
    pub delta: bool,
    pub index_location: IndexLocation,
    pub fill_value: u8,
}

fn positive_ints(value: &Value, count: usize, what: &str) -> Result<Vec<u64>, DecodeError> {
    let list = value
        .as_array()
        .filter(|list| list.len() == count)
        .ok_or_else(|| err(format!("{what} must list {count} entries")))?;
    let mut out = Vec::with_capacity(count);
    for item in list {
        let number = item
            .as_u64()
            .filter(|&number| number > 0)
            .ok_or_else(|| err(format!("{what} must be positive integers")))?;
        out.push(number);
    }
    Ok(out)
}

fn codec_names(value: &Value, what: &str) -> Result<Vec<String>, DecodeError> {
    let list = value
        .as_array()
        .ok_or_else(|| err(format!("{what} must be a list")))?;
    let mut names = Vec::with_capacity(list.len());
    for item in list {
        let name = item
            .as_object()
            .and_then(|codec| codec.get("name"))
            .and_then(|name| name.as_str())
            .ok_or_else(|| err(format!("{what} entry is not an object")))?;
        names.push(name.to_owned());
    }
    Ok(names)
}

/// The array's `zarr.json`, held to the profile: `uint8`, three dimensions, a
/// regular chunk grid, exactly one `sharding_indexed` codec, an inner chain of
/// `[bytes, zstd]` or `[xue.delta{axis: 0}, bytes, zstd]`, index codecs
/// `[bytes, crc32c]` little-endian, and an outer chunk of whole time chunks of
/// whole tiles.
pub fn parse_array_metadata(text: &str) -> Result<ArrayLayout, DecodeError> {
    let value: Value =
        serde_json::from_str(text).map_err(|_| err("array metadata is not valid JSON"))?;
    if value.get("zarr_format").and_then(|v| v.as_u64()) != Some(3)
        || value.get("node_type").and_then(|v| v.as_str()) != Some("array")
    {
        return Err(err("not a Zarr v3 array"));
    }
    if value.get("data_type").and_then(|v| v.as_str()) != Some("uint8") {
        return Err(err("array data type must be uint8"));
    }
    let shape = positive_ints(
        value
            .get("shape")
            .ok_or_else(|| err("array shape missing"))?,
        3,
        "array shape",
    )?;
    let chunk_grid = value
        .get("chunk_grid")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("chunk grid missing"))?;
    if chunk_grid.get("name").and_then(|v| v.as_str()) != Some("regular") {
        return Err(err("chunk grid must be regular"));
    }
    let shard_shape = positive_ints(
        chunk_grid
            .get("configuration")
            .and_then(|v| v.get("chunk_shape"))
            .ok_or_else(|| err("chunk shape missing"))?,
        3,
        "chunk shape",
    )?;
    let key_encoding = value
        .get("chunk_key_encoding")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("chunk key encoding missing"))?;
    if key_encoding.get("name").and_then(|v| v.as_str()) != Some("default") {
        return Err(err("chunk key encoding must be default"));
    }
    if let Some(separator) = key_encoding
        .get("configuration")
        .and_then(|v| v.get("separator"))
        .and_then(|v| v.as_str())
    {
        if separator != "/" {
            return Err(err("chunk key separator must be /"));
        }
    }
    let fill_value = value
        .get("fill_value")
        .and_then(|v| v.as_u64())
        .filter(|&fill| fill <= 255)
        .ok_or_else(|| err("fill value must be a uint8 code"))?;

    let outer = codec_names(
        value.get("codecs").ok_or_else(|| err("codecs missing"))?,
        "codecs",
    )?;
    if outer != ["sharding_indexed"] {
        return Err(err("array must carry exactly one sharding_indexed codec"));
    }
    let sharding = value
        .get("codecs")
        .and_then(|v| v.as_array())
        .and_then(|list| list.first())
        .and_then(|codec| codec.get("configuration"))
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("sharding configuration missing"))?;
    let inner_shape = positive_ints(
        sharding
            .get("chunk_shape")
            .ok_or_else(|| err("inner chunk shape missing"))?,
        3,
        "inner chunk shape",
    )?;
    let inner = codec_names(
        sharding
            .get("codecs")
            .ok_or_else(|| err("inner codecs missing"))?,
        "inner codecs",
    )?;
    let inner_names: Vec<&str> = inner.iter().map(String::as_str).collect();
    let delta = if inner_names == [DELTA_CODEC, "bytes", "zstd"] {
        let axis = sharding
            .get("codecs")
            .and_then(|v| v.as_array())
            .and_then(|list| list.first())
            .and_then(|codec| codec.get("configuration"))
            .and_then(|v| v.get("axis"))
            .and_then(|v| v.as_u64());
        if axis != Some(0) {
            return Err(err("xue.delta must run along axis 0"));
        }
        true
    } else if inner_names == ["bytes", "zstd"] {
        false
    } else {
        return Err(err("inner codec chain is not one this profile reads"));
    };
    let index = codec_names(
        sharding
            .get("index_codecs")
            .ok_or_else(|| err("index codecs missing"))?,
        "index codecs",
    )?;
    if index != ["bytes", "crc32c"] {
        return Err(err("index codecs must be [bytes, crc32c]"));
    }
    let endian = sharding
        .get("index_codecs")
        .and_then(|v| v.as_array())
        .and_then(|list| list.first())
        .and_then(|codec| codec.get("configuration"))
        .and_then(|v| v.get("endian"))
        .and_then(|v| v.as_str())
        .unwrap_or("little");
    if endian != "little" {
        return Err(err("shard index must be little-endian"));
    }
    let index_location = match sharding
        .get("index_location")
        .and_then(|v| v.as_str())
        .unwrap_or("end")
    {
        "start" => IndexLocation::Start,
        "end" => IndexLocation::End,
        other => return Err(err(format!("unknown index location {other}"))),
    };

    let frame_count = shape[0] as u32;
    let height = shape[1] as u32;
    let width = shape[2] as u32;
    let time_chunk = inner_shape[0] as u32;
    let tile_height = inner_shape[1] as u32;
    let tile_width = inner_shape[2] as u32;
    let tile_rows = height.div_ceil(tile_height);
    let tile_columns = width.div_ceil(tile_width);
    let shard_frames = shard_shape[0] as u32;
    if shard_frames % time_chunk != 0
        || shard_shape[1] as u32 != tile_rows * tile_height
        || shard_shape[2] as u32 != tile_columns * tile_width
    {
        return Err(err("outer chunk must be whole time chunks of whole tiles"));
    }
    let tile_count = tile_rows * tile_columns;
    Ok(ArrayLayout {
        frame_count,
        height,
        width,
        time_chunk,
        tile_height,
        tile_width,
        tile_rows,
        tile_columns,
        tile_count,
        time_chunks: frame_count.div_ceil(time_chunk),
        shard_frames,
        shard_count: frame_count.div_ceil(shard_frames),
        chunks_per_shard: (shard_frames / time_chunk) * tile_count,
        delta,
        index_location,
        fill_value: fill_value as u8,
    })
}

// -- shard index ---------------------------------------------------------------

/// The CRC-32C (Castagnoli) checksum the `crc32c` codec puts over a shard
/// index — a different polynomial from the container's CRC-32/IEEE.
pub fn crc32c(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &byte in bytes {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0x82f6_3b78 & mask);
        }
    }
    !crc
}

/// The byte length of a shard's index: one pair per inner chunk plus the
/// checksum.
pub fn shard_index_length(chunk_count: usize) -> usize {
    INDEX_ENTRY_BYTES * chunk_count + INDEX_CHECKSUM_BYTES
}

/// One inner chunk's byte span within its shard.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ShardIndexEntry {
    pub offset: u64,
    pub length: u64,
}

/// The `(offset, nbytes)` pairs of one shard, CRC-32C verified. `None` marks an
/// inner chunk the shard never held — the all-ones sentinel — which reads as
/// the fill value. `bytes` is exactly the index.
pub fn parse_shard_index(
    bytes: &[u8],
    chunk_count: usize,
) -> Result<Vec<Option<ShardIndexEntry>>, DecodeError> {
    let expected = shard_index_length(chunk_count);
    if bytes.len() != expected {
        return Err(err(format!(
            "shard index is {} bytes, not {expected}",
            bytes.len()
        )));
    }
    let entries = &bytes[..INDEX_ENTRY_BYTES * chunk_count];
    let checksum = u32::from_le_bytes(
        bytes[INDEX_ENTRY_BYTES * chunk_count..]
            .try_into()
            .map_err(|_| err("shard index checksum is malformed"))?,
    );
    if checksum != crc32c(entries) {
        return Err(err("shard index CRC-32C mismatch"));
    }
    let mut index = Vec::with_capacity(chunk_count);
    for position in 0..chunk_count {
        let pair = &entries[position * INDEX_ENTRY_BYTES..][..INDEX_ENTRY_BYTES];
        let offset = u64::from_le_bytes(pair[..8].try_into().unwrap());
        let length = u64::from_le_bytes(pair[8..].try_into().unwrap());
        if offset == u64::MAX && length == u64::MAX {
            index.push(None);
            continue;
        }
        if offset > MAX_SAFE_INTEGER || length > MAX_SAFE_INTEGER {
            return Err(err("shard index entry exceeds the safe integer range"));
        }
        if length == 0 {
            return Err(err("shard index names an empty chunk"));
        }
        index.push(Some(ShardIndexEntry { offset, length }));
    }
    Ok(index)
}

// -- geometry ------------------------------------------------------------------

/// The object holding shard `s` of a variable's array: the default chunk key
/// encoding, with the two grid dimensions at chunk 0 because a shard spans the
/// whole grid.
pub fn shard_path(variable_id: &str, shard: u32) -> String {
    format!("{variable_id}/c/{shard}/0/0")
}

/// Which shard a time chunk lives in, and the position of its first tile in
/// that shard's index (row-major over the inner chunk grid).
pub fn shard_of(layout: &ArrayLayout, time_chunk: u32) -> (u32, u32) {
    let per_shard = layout.shard_frames / layout.time_chunk;
    (
        time_chunk / per_shard,
        (time_chunk % per_shard) * layout.tile_count,
    )
}

/// The row and column a tile starts at.
pub fn tile_origin(layout: &ArrayLayout, tile: u32) -> (u32, u32) {
    (
        (tile / layout.tile_columns) * layout.tile_height,
        (tile % layout.tile_columns) * layout.tile_width,
    )
}

/// A tile's clipped shape: what of it lies inside the grid.
pub fn tile_shape(layout: &ArrayLayout, tile: u32) -> (u32, u32) {
    let (row, column) = tile_origin(layout, tile);
    (
        layout.tile_height.min(layout.height.saturating_sub(row)),
        layout.tile_width.min(layout.width.saturating_sub(column)),
    )
}

/// The tile a cell falls in.
pub fn tile_of(layout: &ArrayLayout, row: u32, column: u32) -> Result<u32, DecodeError> {
    if row >= layout.height || column >= layout.width {
        return Err(err("cell is outside the grid"));
    }
    Ok((row / layout.tile_height) * layout.tile_columns + column / layout.tile_width)
}

/// How many of a time chunk's frames lie on the axis; fewer than the chunk
/// shape on the last one.
pub fn frames_in_time_chunk(layout: &ArrayLayout, time_chunk: u32) -> u32 {
    layout.time_chunk.min(
        layout
            .frame_count
            .saturating_sub(time_chunk * layout.time_chunk),
    )
}

/// Copy frame `frame_in_chunk` of one decoded inner chunk — stored whole at
/// the inner shape — into a plane, trimming the padding past the grid's edge.
/// `chunk` is `None` for a chunk the shard never held, which is the fill value
/// throughout.
pub fn place_tile(
    plane: &mut [u8],
    layout: &ArrayLayout,
    tile: u32,
    chunk: Option<&[u8]>,
    frame_in_chunk: u32,
) -> Result<(), DecodeError> {
    let (origin_row, origin_column) = tile_origin(layout, tile);
    let (shape_height, shape_width) = tile_shape(layout, tile);
    let stride = (layout.tile_height as usize) * (layout.tile_width as usize);
    for row in 0..shape_height {
        let target =
            ((origin_row + row) as usize) * (layout.width as usize) + origin_column as usize;
        let width = shape_width as usize;
        if target + width > plane.len() {
            return Err(err("plane buffer is smaller than the grid"));
        }
        match chunk {
            None => plane[target..target + width].fill(layout.fill_value),
            Some(chunk) => {
                let source = (frame_in_chunk as usize) * stride
                    + (row as usize) * (layout.tile_width as usize);
                let end = source + width;
                if end > chunk.len() {
                    return Err(err("inner chunk is smaller than its declared shape"));
                }
                plane[target..target + width].copy_from_slice(&chunk[source..end]);
            }
        }
    }
    Ok(())
}

// -- time ----------------------------------------------------------------------

/// Parse an ISO 8601 instant to milliseconds since the Unix epoch, without a
/// date library: the same arithmetic `Date.parse` does for the forms the
/// pipeline emits.
pub fn parse_iso(text: &str) -> Option<f64> {
    let bytes = text.as_bytes();
    if bytes.len() < 20 {
        return None;
    }
    let digit = |start: usize, len: usize| -> Option<i64> {
        let part = text.get(start..start + len)?;
        part.bytes()
            .all(|b| b.is_ascii_digit())
            .then(|| part.parse().ok())?
    };
    let year = digit(0, 4)?;
    if bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    let month = digit(5, 2)?;
    let day = digit(8, 2)?;
    let separator = bytes[10];
    if separator != b'T' && separator != b't' && separator != b' ' {
        return None;
    }
    let hour = digit(11, 2)?;
    if bytes[13] != b':' {
        return None;
    }
    let minute = digit(14, 2)?;
    let mut index = 16;
    let mut second = 0i64;
    if bytes.get(16) == Some(&b':') {
        second = digit(17, 2)?;
        index = 19;
    }
    let mut millis = 0i64;
    if bytes.get(index) == Some(&b'.') {
        let start = index + 1;
        let mut end = start;
        while end < bytes.len() && bytes[end].is_ascii_digit() {
            end += 1;
        }
        let digits = &text[start..end];
        let scale = 10i64.pow((3 - digits.len().min(3)) as u32);
        let value: i64 = digits.get(..digits.len().min(3))?.parse().ok()?;
        millis = value * scale;
        index = end;
    }
    // The numeric spelling (`2026-09-25T06:00:00Z`) is what the pipeline
    // publishes; the offset forms are accepted too.
    let offset_minutes = match bytes.get(index) {
        None | Some(b'Z') | Some(b'z') => 0i64,
        Some(b'+') | Some(b'-') => {
            let sign = if bytes[index] == b'+' { 1 } else { -1 };
            let oh = digit(index + 1, 2)?;
            if bytes.get(index + 3) != Some(&b':') {
                return None;
            }
            let om = digit(index + 4, 2)?;
            sign * (oh * 60 + om)
        }
        _ => return None,
    };
    let days = days_from_civil(year, month, day)?;
    let seconds = days * 86_400 + hour * 3600 + minute * 60 + second - offset_minutes * 60;
    Some((seconds * 1000 + millis) as f64)
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
/// `days_from_civil`).
fn days_from_civil(year: i64, month: i64, day: i64) -> Option<i64> {
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let day_of_year = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    Some(era * 146_097 + day_of_era - 719_468)
}

/// A run directory under the data root. Accepts the STAC Item id form with a
/// 4-digit round (`mrms.2026092509.1455` → `mrms.2026092509/1455`), the run
/// directory itself (`mrms.2026092509/1455`), or a plain run id
/// (`gfs.2026092506`). `None` is a spelling no run directory may take.
pub fn run_directory(run: &str) -> Option<String> {
    let valid = !run.is_empty()
        && run
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'/' | b'-'));
    if !valid {
        return None;
    }
    // No empty or all-dot path segment: `run=../x` is not a run directory, and
    // a run never has a `//`.
    if run
        .split('/')
        .any(|segment| segment.is_empty() || segment.bytes().all(|byte| byte == b'.'))
    {
        return None;
    }
    if run.contains('/') {
        return Some(run.to_owned());
    }
    let parts: Vec<&str> = run.split('.').collect();
    let last = parts.last().copied().unwrap_or_default();
    if parts.len() >= 2 && last.len() == 4 && last.bytes().all(|byte| byte.is_ascii_digit()) {
        Some(format!("{}/{}", parts[..parts.len() - 1].join("."), last))
    } else {
        Some(run.to_owned())
    }
}

/// Frame indices to read: all of them, or the one nearest the requested valid
/// time. `run_time` settles where offset 0 sits; without it the axis starts at
/// the epoch, as the shell's probe does.
pub fn frame_indices(
    offsets: &[u16],
    unit_seconds: u32,
    run_time: Option<&str>,
    time: Option<&str>,
) -> Result<Vec<u32>, DecodeError> {
    let Some(time) = time else {
        return Ok((0..offsets.len() as u32).collect());
    };
    let target =
        parse_iso(time).ok_or_else(|| err(format!("time must be ISO 8601, got {time}")))?;
    let run_ms = run_time.and_then(parse_iso).unwrap_or(0.0);
    let unit_ms = f64::from(unit_seconds) * 1000.0;
    let mut best = 0usize;
    let mut best_distance = f64::INFINITY;
    for (index, &offset) in offsets.iter().enumerate() {
        let distance = (run_ms + f64::from(offset) * unit_ms - target).abs();
        if distance < best_distance {
            best_distance = distance;
            best = index;
        }
    }
    Ok(vec![best as u32])
}

/// The ISO 8601 instants of the selected frames.
pub fn axis_times(
    offsets: &[u16],
    unit_seconds: u32,
    run_time: Option<&str>,
    indices: &[u32],
) -> Vec<String> {
    let run_ms = run_time.and_then(parse_iso).unwrap_or(0.0);
    let unit_ms = f64::from(unit_seconds) * 1000.0;
    indices
        .iter()
        .map(|&index| {
            let offset = offsets.get(index as usize).copied().unwrap_or_default();
            format_iso_millis(run_ms + f64::from(offset) * unit_ms)
        })
        .collect()
}

/// Format milliseconds since the Unix epoch as `YYYY-MM-DDTHH:MM:SS.sssZ`, the
/// spelling `Date#toISOString` produces.
pub fn format_iso_millis(millis: f64) -> String {
    let total = millis.floor() as i64;
    let seconds = total.div_euclid(1000);
    let millis_part = total.rem_euclid(1000);
    let days = seconds.div_euclid(86_400);
    let time = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let (hour, minute, second) = (time / 3600, (time % 3600) / 60, time % 60);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis_part:03}Z")
}

/// The inverse of [`days_from_civil`].
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let mp = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (if month <= 2 { year + 1 } else { year }, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn array_document(overrides: impl FnOnce(&mut Value)) -> String {
        let mut document = json!({
            "zarr_format": 3,
            "node_type": "array",
            "shape": [121, 73, 144],
            "data_type": "uint8",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [126, 80, 144]}},
            "chunk_key_encoding": {"name": "default"},
            "fill_value": 255,
            "codecs": [{
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": [6, 16, 16],
                    "codecs": [{"name": "bytes"}, {"name": "zstd", "configuration": {"level": 15, "checksum": true}}],
                    "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}],
                    "index_location": "end"
                }
            }],
            "attributes": {},
            "dimension_names": ["time", "latitude", "longitude"]
        });
        overrides(&mut document);
        serde_json::to_string(&document).unwrap()
    }

    fn build_index(entries: &[Option<(u64, u64)>]) -> Vec<u8> {
        let mut bytes = vec![0u8; shard_index_length(entries.len())];
        for (position, entry) in entries.iter().enumerate() {
            let (offset, length) = entry.unwrap_or((u64::MAX, u64::MAX));
            bytes[position * 16..position * 16 + 8].copy_from_slice(&offset.to_le_bytes());
            bytes[position * 16 + 8..position * 16 + 16].copy_from_slice(&length.to_le_bytes());
        }
        let count = entries.len() * 16;
        let checksum = crc32c(&bytes[..count]);
        bytes[count..count + 4].copy_from_slice(&checksum.to_le_bytes());
        bytes
    }

    #[test]
    fn crc32c_is_castagnoli() {
        assert_eq!(crc32c(b"123456789"), 0xe306_9283);
        assert_eq!(crc32c(&[]), 0);
    }

    #[test]
    fn reads_the_profile_layout() {
        let layout = parse_array_metadata(&array_document(|_| {})).unwrap();
        assert_eq!(layout.frame_count, 121);
        assert_eq!(layout.height, 73);
        assert_eq!(layout.width, 144);
        assert_eq!(layout.time_chunk, 6);
        assert_eq!(layout.tile_height, 16);
        assert_eq!(layout.tile_width, 16);
        assert_eq!(layout.tile_rows, 5);
        assert_eq!(layout.tile_columns, 9);
        assert_eq!(layout.tile_count, 45);
        assert_eq!(layout.time_chunks, 21);
        assert_eq!(layout.shard_frames, 126);
        assert_eq!(layout.shard_count, 1);
        assert_eq!(layout.chunks_per_shard, 945);
        assert!(!layout.delta);
        assert_eq!(layout.index_location, IndexLocation::End);
        assert_eq!(layout.fill_value, 255);
        assert_eq!(tile_shape(&layout, 44), (9, 16));
        assert_eq!(tile_of(&layout, 72, 143).unwrap(), 44);
        assert_eq!(shard_of(&layout, 0), (0, 0));
        assert_eq!(shard_of(&layout, 20), (0, 900));
    }

    #[test]
    fn reads_a_legacy_shard_per_time_chunk_and_refuses_a_partial_one() {
        let legacy = parse_array_metadata(&array_document(|document| {
            document["chunk_grid"]["configuration"]["chunk_shape"] = json!([6, 80, 144]);
        }))
        .unwrap();
        assert_eq!(legacy.shard_frames, 6);
        assert_eq!(legacy.shard_count, 21);
        assert_eq!(legacy.chunks_per_shard, 45);
        assert_eq!(shard_of(&legacy, 20), (20, 0));
        let two = parse_array_metadata(&array_document(|document| {
            document["chunk_grid"]["configuration"]["chunk_shape"] = json!([12, 80, 144]);
        }))
        .unwrap();
        assert_eq!(shard_of(&two, 7), (3, 45));
        assert!(parse_array_metadata(&array_document(|document| {
            document["chunk_grid"]["configuration"]["chunk_shape"] = json!([9, 80, 144]);
        }))
        .is_err());
    }

    #[test]
    fn accepts_delta_and_an_index_at_the_start() {
        let layout = parse_array_metadata(&array_document(|document| {
            document["codecs"][0]["configuration"]["codecs"] =
                json!([{"name": "xue.delta", "configuration": {"axis": 0}}, {"name": "bytes"}, {"name": "zstd"}]);
            document["codecs"][0]["configuration"]["index_location"] = json!("start");
        }))
        .unwrap();
        assert!(layout.delta);
        assert_eq!(layout.index_location, IndexLocation::Start);
    }

    #[test]
    fn rejects_documents_off_the_profile() {
        for edit in [
            |d: &mut Value| d["data_type"] = json!("float32"),
            |d: &mut Value| d["codecs"] = json!([{"name": "bytes"}, {"name": "zstd"}]),
            |d: &mut Value| {
                d["codecs"][0]["configuration"]["index_codecs"] = json!([{"name": "bytes"}]);
            },
            |d: &mut Value| d["codecs"][0]["configuration"]["index_location"] = json!("middle"),
            |d: &mut Value| d["chunk_grid"]["configuration"]["chunk_shape"] = json!([126, 73, 144]),
        ] {
            assert!(parse_array_metadata(&array_document(edit)).is_err());
        }
    }

    #[test]
    fn reads_the_shard_index_and_verifies_its_checksum() {
        let entries = [Some((0, 40)), None, Some((40, 7))];
        let parsed = parse_shard_index(&build_index(&entries), 3).unwrap();
        assert_eq!(
            parsed[0],
            Some(ShardIndexEntry {
                offset: 0,
                length: 40
            })
        );
        assert_eq!(parsed[1], None);
        assert_eq!(
            parsed[2],
            Some(ShardIndexEntry {
                offset: 40,
                length: 7
            })
        );

        let mut corrupt = build_index(&entries);
        corrupt[3] ^= 1;
        assert!(parse_shard_index(&corrupt, 3).is_err());
        assert!(parse_shard_index(&build_index(&entries)[1..], 3).is_err());
        assert!(parse_shard_index(&build_index(&[Some((0, 0))]), 1).is_err());
    }

    #[test]
    fn probes_cells_as_the_shell_does() {
        let grid = Grid {
            width: 1440,
            height: 721,
            first_longitude: -180.0,
            first_latitude: 90.0,
            longitude_step: 0.25,
            latitude_step: -0.25,
            wraps: true,
        };
        let cell = probe_cell(&grid, 139.7, 35.7).unwrap();
        assert_eq!(cell.column, 1279);
        assert_eq!(cell.row, 217);
        assert!((cell.longitude - 139.75).abs() < 1e-9);
        assert!((cell.latitude - 35.75).abs() < 1e-9);
        assert!(probe_cell(&grid, -180.1, 0.0).is_some());
    }

    #[test]
    fn decodes_linear_and_log_codebooks() {
        let linear = Quantization::Linear {
            offset: -60.0,
            scale: 0.5,
            minimum_code: 0.0,
            maximum_code: 220.0,
            nodata_code: 255.0,
        };
        assert_eq!(decode_value(&linear, 163), Some(21.5));
        assert_eq!(decode_value(&linear, 255), None);
        assert_eq!(decode_value(&linear, 221), None);

        let log = Quantization::Log1p {
            trace: 0.01,
            scale: 0.05,
            maximum: 128.0,
            minimum_code: 1.0,
            maximum_code: 253.0,
            zero_code: 0.0,
            overflow_code: 254.0,
            nodata_code: 255.0,
        };
        assert_eq!(decode_value(&log, 0), Some(0.0));
        assert_eq!(decode_value(&log, 255), None);
        // 254 is the overflow code the profile defines, not a missing one.
        assert!(decode_value(&log, 254).is_some());
        assert!(decode_value(&log, 1).unwrap() < decode_value(&log, 253).unwrap());
    }

    #[test]
    fn run_directories_take_every_spelling() {
        assert_eq!(
            run_directory("gfs.2026092506").as_deref(),
            Some("gfs.2026092506")
        );
        assert_eq!(
            run_directory("mrms.2026092509.1455").as_deref(),
            Some("mrms.2026092509/1455")
        );
        assert_eq!(
            run_directory("mrms.2026092509/1455").as_deref(),
            Some("mrms.2026092509/1455")
        );
        assert_eq!(run_directory("../etc/passwd"), None);
        assert_eq!(run_directory(""), None);
    }

    #[test]
    fn frame_selection_picks_the_nearest_instant() {
        let offsets = [0u16, 6, 12, 18];
        assert_eq!(
            frame_indices(&offsets, 3600, Some("2026-09-25T00:00:00Z"), None).unwrap(),
            vec![0, 1, 2, 3]
        );
        assert_eq!(
            frame_indices(
                &offsets,
                3600,
                Some("2026-09-25T00:00:00Z"),
                Some("2026-09-25T07:00:00Z")
            )
            .unwrap(),
            vec![1]
        );
        assert!(frame_indices(&offsets, 3600, None, Some("not-a-time")).is_err());
    }

    #[test]
    fn axis_times_are_iso_instants() {
        assert_eq!(
            axis_times(&[0, 6], 3600, Some("2026-09-25T00:00:00Z"), &[0, 1]),
            vec!["2026-09-25T00:00:00.000Z", "2026-09-25T06:00:00.000Z"]
        );
    }

    #[test]
    fn formats_and_parses_iso_instants() {
        assert_eq!(parse_iso("2026-09-25T06:00:00Z"), Some(1_790_316_000_000.0));
        assert_eq!(
            parse_iso("2026-09-25T06:00:00.000Z"),
            Some(1_790_316_000_000.0)
        );
        assert_eq!(
            format_iso_millis(1_790_316_000_000.0),
            "2026-09-25T06:00:00.000Z"
        );
        assert_eq!(format_iso_millis(0.0), "1970-01-01T00:00:00.000Z");
    }
}
