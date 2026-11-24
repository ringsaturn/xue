//! Reading the Zarr profile's column store.
//!
//! `docs/zarr-profile.md`, "Column store", is normative. A column store holds
//! the codes of a run's published map stores cut the other way: one `uint8`
//! array per quantity, `[time, level, latitude, longitude]` (a surface array
//! has no level axis), whose inner chunk is the whole axis and every level of
//! one small block of cells. One cell's column over the run is then one chunk
//! per quantity.
//!
//! Like [`crate::zarr`], this module is IO-free: it takes the group document,
//! the shard indices and the chunks' bytes, and returns codes and values. The
//! group's consolidated metadata holds every array document and is
//! authoritative, so the group is the only document read. The metadata rules,
//! the codec chain, the shard index and the chunk payload are the grid
//! store's own; only the block, the dimensions and the cut are this branch's.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Map, Value};

use crate::format::{err, DecodeError, Predictor, MAX_PLANE_LENGTH};
use crate::zarr::{
    self, decode_value, parse_iso, Grid, IndexLocation, Quantization, ShardIndexEntry, StoreKind,
    VariableMetadata, INDEX_ENTRY_BYTES,
};

/// The `xue_columns` block's version this reader implements.
pub const COLUMN_VERSION: u64 = 1;
/// The cells along each side of a writer's block. A reader takes the block
/// from the array document and never assumes this one.
pub const COLUMN_BLOCK_CELLS: u32 = 8;
/// The rows of a writer's shard, whole blocks; a reader takes it likewise.
pub const COLUMN_SHARD_ROWS: u32 = 96;
/// The group attribute that carries the block, and the array attribute that
/// names the array.
pub(crate) const COLUMN_ATTRIBUTE: &str = "xue_columns";
/// The grid branch's version, which goes with its block: beside this one it
/// would leave a group that reads as two branches.
const GRID_PROFILE_ATTRIBUTE: &str = "xue_profile";
/// The coordinate arrays' names, which a data array must not take.
pub(crate) const COORDINATES: [&str; 4] = ["time", "level", "latitude", "longitude"];
/// The regular grid's numbers. A bundle's metadata reads defaults for missing
/// ones because old metadata lacks them; nothing this old is a column store.
const GRID_NUMBERS: [&str; 4] = [
    "firstLongitude",
    "firstLatitude",
    "longitudeStep",
    "latitudeStep",
];

// -- group ---------------------------------------------------------------------

/// One level of a levelled array, or a surface array's one field: the bundle
/// its codes come from and that bundle's variable.
#[derive(Debug, Clone)]
pub struct ColumnMember {
    pub bundle: String,
    /// The codebook is this member's own: two levels of one array may differ.
    /// `numeric_id` is meaningless here.
    pub variable: VariableMetadata,
}

/// One array of the store, its document held to the block.
#[derive(Debug, Clone)]
pub struct ColumnArray {
    pub id: String,
    /// Pressure levels in hPa, strictly descending, index for index with
    /// `members`; `None` on a surface array, which has one member.
    pub levels: Option<Vec<u32>>,
    pub members: Vec<ColumnMember>,
    /// The array's axis: the store's, unless it declares its own.
    pub unit_seconds: u32,
    pub offsets: Vec<u16>,
    pub layout: ColumnLayout,
}

impl ColumnArray {
    /// The object holding one shard, relative to the store. A shard spans the
    /// whole axis, every level and every column, so only its row band counts.
    pub fn shard_path(&self, shard: u32) -> String {
        match self.levels {
            Some(_) => format!("{}/c/0/0/{shard}/0", self.id),
            None => format!("{}/c/0/{shard}/0", self.id),
        }
    }

    /// One cell's codes ([`decode_column`], `frames × levels`) as physical
    /// values through each level's own codebook; `NaN` for no value.
    pub fn values(&self, codes: &[u8]) -> Result<Vec<f64>, DecodeError> {
        if codes.len() != self.layout.column_length() {
            return Err(err("column does not fit the array's frames and levels"));
        }
        Ok(codes
            .chunks_exact(self.members.len())
            .flat_map(|frame| frame.iter().zip(&self.members))
            .map(|(&code, member)| {
                decode_value(&member.variable.quantization, code).unwrap_or(f64::NAN)
            })
            .collect())
    }
}

/// A column store's group document: the `xue_columns` block and every array
/// document it consolidates, validated against one another.
#[derive(Debug, Clone)]
pub struct ColumnGroup {
    pub version: u32,
    pub model: String,
    pub product: String,
    pub run_time: String,
    /// `run_time` in Unix seconds.
    pub run_time_seconds: i64,
    pub profile: String,
    pub grid: Grid,
    /// The store's axis, in `unit_seconds`, strictly ascending.
    pub unit_seconds: u32,
    pub offsets: Vec<u16>,
    pub arrays: Vec<ColumnArray>,
    /// The `xue_columns` block as compact JSON.
    pub block_json: String,
}

impl ColumnGroup {
    /// Parse a store root's `zarr.json`: a Zarr v3 group whose attributes
    /// carry `xue_columns` and no other branch's block, at a version this
    /// reader implements, with the consolidated document of every array the
    /// block lists.
    pub fn parse(text: &str) -> Result<ColumnGroup, DecodeError> {
        let mut document: Value =
            serde_json::from_str(text).map_err(|_| err("group metadata is not valid JSON"))?;
        let (kind, _) = zarr::document_branch(&document)?;
        if kind != StoreKind::Column {
            return Err(err("group is not a column store"));
        }
        if document["attributes"].get(GRID_PROFILE_ATTRIBUTE).is_some() {
            return Err(err("a column store group carries no xue_profile"));
        }
        let mut documents = consolidated_documents(&mut document)?;
        let block = document["attributes"][COLUMN_ATTRIBUTE]
            .as_object()
            .ok_or_else(|| err("xue_columns must be an object"))?;
        let version = block
            .get("version")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| err("xue_columns version missing"))?;
        if version != COLUMN_VERSION {
            return Err(err(format!(
                "column store version {version} is not implemented"
            )));
        }
        let model = nonempty(block, "model")?;
        let product = nonempty(block, "product")?;
        let profile = nonempty(block, "profile")?;
        let run_time = nonempty(block, "runTime")?;
        let run_time_ms = parse_iso(&run_time)
            .ok_or_else(|| err("xue_columns runTime is not an ISO 8601 instant"))?;
        if run_time_ms % 1000.0 != 0.0 {
            return Err(err("xue_columns runTime must fall on a whole second"));
        }
        let grid = block
            .get("grid")
            .ok_or_else(|| err("xue_columns grid missing"))?;
        check_grid(grid)?;
        let time = block
            .get("time")
            .ok_or_else(|| err("xue_columns time missing"))?;

        let entries = parse_entries(block)?;
        let shared: Vec<&Value> = entries
            .iter()
            .filter(|entry| entry.time.is_none())
            .flat_map(|entry| entry.variables.iter().copied())
            .collect();
        if shared.is_empty() {
            return Err(err("xue_columns arrays must hold one on the store's axis"));
        }
        let axis = read_axis(grid, time, &shared)?;
        let grid_shape = (u64::from(axis.grid.height()), u64::from(axis.grid.width()));
        let mut shared_variables = axis.variables.into_iter();
        let mut variable_ids = BTreeSet::new();
        // The first levelled array's levels are the store's `level` coordinate.
        let store_levels = entries.iter().find_map(|entry| entry.levels.clone());
        let mut arrays = Vec::with_capacity(entries.len());
        for entry in entries {
            let (unit_seconds, offsets, variables) = match entry.time {
                None => (
                    axis.unit_seconds,
                    axis.offsets.clone(),
                    shared_variables
                        .by_ref()
                        .take(entry.variables.len())
                        .collect::<Vec<_>>(),
                ),
                Some(own) => {
                    let own = read_axis(grid, own, &entry.variables)?;
                    if (own.unit_seconds, &own.offsets) == (axis.unit_seconds, &axis.offsets) {
                        return Err(err(format!(
                            "array {} repeats the store's time block",
                            entry.id
                        )));
                    }
                    (own.unit_seconds, own.offsets, own.variables)
                }
            };
            for variable in &variables {
                if !variable_ids.insert(variable.id.clone()) {
                    return Err(err(format!("variable {} occurs twice", variable.id)));
                }
            }
            let array_document = documents
                .remove(entry.id)
                .ok_or_else(|| err(format!("group consolidates no array {}", entry.id)))?;
            let mut dimensions = vec![dimension("time", entry.time.is_some(), entry.id)];
            if entry.levels.is_some() {
                dimensions.push(dimension("level", entry.levels != store_levels, entry.id));
            }
            dimensions.extend(["latitude".to_owned(), "longitude".to_owned()]);
            let layout = read_layout(
                array_document,
                entry.id,
                &dimensions,
                offsets.len() as u64,
                entry.levels.as_ref().map(|levels| levels.len() as u64),
                grid_shape,
            )
            .map_err(|error| err(format!("array {}: {}", entry.id, error.0)))?;
            if variables
                .iter()
                .any(|variable| nodata_code(&variable.quantization) != f64::from(layout.fill_value))
            {
                return Err(err(format!(
                    "array {}: fill_value must be every member's nodataCode",
                    entry.id
                )));
            }
            if entry.levels.is_none() && variables[0].id != entry.id {
                return Err(err(format!(
                    "surface array {} must be named for its variable",
                    entry.id
                )));
            }
            arrays.push(ColumnArray {
                id: entry.id.to_owned(),
                levels: entry.levels,
                members: entry
                    .bundles
                    .into_iter()
                    .zip(variables)
                    .map(|(bundle, variable)| ColumnMember {
                        bundle: bundle.to_owned(),
                        variable,
                    })
                    .collect(),
                unit_seconds,
                offsets,
                layout,
            });
        }
        let block_json = serde_json::to_string(block)
            .map_err(|_| err("xue_columns block is not serializable"))?;
        Ok(ColumnGroup {
            version: version as u32,
            model,
            product,
            run_time,
            run_time_seconds: (run_time_ms / 1000.0) as i64,
            profile,
            grid: axis.grid,
            unit_seconds: axis.unit_seconds,
            offsets: axis.offsets,
            arrays,
            block_json,
        })
    }

    pub fn array(&self, id: &str) -> Result<&ColumnArray, DecodeError> {
        self.arrays
            .iter()
            .find(|array| array.id == id)
            .ok_or_else(|| err(format!("column store holds no array {id:?}")))
    }
}

fn nonempty(block: &Map<String, Value>, key: &str) -> Result<String, DecodeError> {
    block
        .get(key)
        .and_then(|v| v.as_str())
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| err(format!("xue_columns {key} must be a non-empty string")))
}

/// The inline consolidated array documents, taken out of the group rather than
/// copied: they are most of it.
fn consolidated_documents(document: &mut Value) -> Result<Map<String, Value>, DecodeError> {
    let consolidated = document
        .get_mut("consolidated_metadata")
        .ok_or_else(|| err("column store group carries no consolidated metadata"))?;
    if consolidated.get("kind").and_then(|v| v.as_str()) != Some("inline") {
        return Err(err("consolidated metadata must be inline"));
    }
    match consolidated.get_mut("metadata").map(Value::take) {
        Some(Value::Object(documents)) => Ok(documents),
        _ => Err(err("consolidated metadata holds no documents")),
    }
}

/// A regular or Gaussian grid with every field its schema requires. The rest
/// of the rules are bundle metadata's, checked with the axis.
fn check_grid(grid: &Value) -> Result<(), DecodeError> {
    if grid.get("projection").is_some() {
        return Err(err("a column store's grid must not be projected"));
    }
    if GRID_NUMBERS
        .iter()
        .any(|key| grid.get(key).and_then(|v| v.as_f64()).is_none())
        || grid
            .get("wrapLongitude")
            .and_then(|v| v.as_bool())
            .is_none()
    {
        return Err(err("xue_columns grid is incomplete"));
    }
    Ok(())
}

/// One entry of the block's `arrays`, its structure checked.
struct Entry<'a> {
    id: &'a str,
    time: Option<&'a Value>,
    levels: Option<Vec<u32>>,
    bundles: Vec<&'a str>,
    variables: Vec<&'a Value>,
}

/// `^[a-z][a-z0-9]*$`: an array id, and a bundle id.
pub(crate) fn is_id(text: &str) -> bool {
    text.bytes().next().is_some_and(|first| first.is_ascii_lowercase())
        && text.bytes().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
}

fn parse_entries(block: &Map<String, Value>) -> Result<Vec<Entry<'_>>, DecodeError> {
    let list = block
        .get("arrays")
        .and_then(|v| v.as_array())
        .filter(|list| !list.is_empty())
        .ok_or_else(|| err("xue_columns arrays must list at least one array"))?;
    let mut ids = BTreeSet::new();
    let mut entries = Vec::with_capacity(list.len());
    for item in list {
        let id = item
            .get("id")
            .and_then(|v| v.as_str())
            .filter(|id| is_id(id) && !COORDINATES.contains(id))
            .ok_or_else(|| err("xue_columns array id is invalid"))?;
        if !ids.insert(id) {
            return Err(err(format!("xue_columns array {id} occurs twice")));
        }
        let levels = item.get("levels").map(parse_levels).transpose()?;
        let members = item
            .get("members")
            .and_then(|v| v.as_array())
            .filter(|members| members.len() == levels.as_ref().map_or(1, Vec::len))
            .ok_or_else(|| err(format!("array {id} must have one member per level")))?;
        let mut bundles = Vec::with_capacity(members.len());
        let mut variables = Vec::with_capacity(members.len());
        for member in members {
            bundles.push(
                member
                    .get("bundle")
                    .and_then(|v| v.as_str())
                    .filter(|bundle| is_id(bundle))
                    .ok_or_else(|| err(format!("array {id}: a member names no bundle id")))?,
            );
            variables.push(
                member
                    .get("variable")
                    .filter(|variable| variable.is_object())
                    .ok_or_else(|| err(format!("array {id}: a member carries no variable")))?,
            );
        }
        entries.push(Entry {
            id,
            time: item.get("time"),
            levels,
            bundles,
            variables,
        });
    }
    Ok(entries)
}

/// Positive whole hPa, strictly descending, in the `int32` range of the
/// `level` coordinate.
fn parse_levels(value: &Value) -> Result<Vec<u32>, DecodeError> {
    let list = value
        .as_array()
        .filter(|list| !list.is_empty())
        .ok_or_else(|| err("levels must list at least one level"))?;
    let mut levels: Vec<u32> = Vec::with_capacity(list.len());
    for item in list {
        let level = item
            .as_u64()
            .filter(|&level| level > 0 && level <= i32::MAX as u64)
            .ok_or_else(|| err("levels must be positive integers"))? as u32;
        if levels.last().is_some_and(|&previous| level >= previous) {
            return Err(err("levels must be strictly descending"));
        }
        levels.push(level);
    }
    Ok(levels)
}

struct Axis {
    unit_seconds: u32,
    offsets: Vec<u16>,
    grid: Grid,
    variables: Vec<VariableMetadata>,
}

/// The grid, a time block and the variables on it, validated as the bundle
/// metadata they were copied from: the rules live in one place. `numericId`
/// is local to each variable's own bundle, so they are renumbered for a
/// document that holds variables of several, at most 255 at a time.
fn read_axis(grid: &Value, time: &Value, variables: &[&Value]) -> Result<Axis, DecodeError> {
    let mut read: Option<Axis> = None;
    for group in variables.chunks(usize::from(u8::MAX)) {
        let numbered: Vec<Value> = group
            .iter()
            .zip(1..=u8::MAX)
            .map(|(&variable, number)| {
                let mut variable = variable.clone();
                variable["numericId"] = Value::from(number);
                variable
            })
            .collect();
        let document =
            json!({"schemaVersion": 3, "grid": grid, "time": time, "variables": numbered});
        let metadata = zarr::parse_bundle_metadata(&document.to_string())?;
        match &mut read {
            None => {
                read = Some(Axis {
                    unit_seconds: metadata.unit_seconds,
                    offsets: metadata.offsets,
                    grid: metadata.grid,
                    variables: metadata.variables,
                })
            }
            Some(axis) => axis.variables.extend(metadata.variables),
        }
    }
    read.ok_or_else(|| err("xue_columns axis serves no array"))
}

fn dimension(name: &str, own: bool, array: &str) -> String {
    if own {
        format!("{name}_{array}")
    } else {
        name.to_owned()
    }
}

fn nodata_code(quantization: &Quantization) -> f64 {
    match *quantization {
        Quantization::Linear { nodata_code, .. } | Quantization::Log1p { nodata_code, .. } => {
            nodata_code
        }
    }
}

// -- array ---------------------------------------------------------------------

/// How an array is cut: an inner chunk of the whole axis and every level of
/// one block of cells, a shard of whole blocks across the grid's width.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ColumnLayout {
    pub frame_count: u32,
    /// 1 on a surface array.
    pub level_count: u32,
    pub height: u32,
    pub width: u32,
    pub block_height: u32,
    pub block_width: u32,
    /// Rows per shard, whole blocks. The last shard's may run past the grid.
    pub shard_height: u32,
    pub shard_width: u32,
    pub shard_count: u32,
    /// Blocks in a shard, and pairs in its index.
    pub chunks_per_shard: u32,
    pub delta: bool,
    pub index_location: IndexLocation,
    pub fill_value: u8,
}

impl ColumnLayout {
    /// Codes of one cell's column: `frames × levels`.
    pub fn column_length(&self) -> usize {
        self.frame_count as usize * self.level_count as usize
    }

    /// The shard index's byte length: one pair per block plus the CRC-32C.
    pub fn index_length(&self) -> usize {
        zarr::shard_index_length(self.chunks_per_shard as usize)
    }

    /// The shard a cell lies in.
    pub fn shard_of(&self, row: u32, column: u32) -> Result<u32, DecodeError> {
        if row >= self.height || column >= self.width {
            return Err(err("cell is outside the grid"));
        }
        Ok(row / self.shard_height)
    }

    /// The index position, within its shard, of the block a cell lies in.
    pub fn position(&self, row: u32, column: u32) -> Result<usize, DecodeError> {
        self.shard_of(row, column)?;
        let block_row = (row % self.shard_height) / self.block_height;
        let blocks_across = self.shard_width / self.block_width;
        Ok(block_row as usize * blocks_across as usize + (column / self.block_width) as usize)
    }

    /// The index's span within a shard of `shard_length` bytes, `[start,
    /// end)`.
    pub fn shard_index_range(&self, shard_length: u64) -> Result<(u64, u64), DecodeError> {
        let length = self.index_length() as u64;
        if shard_length < length {
            return Err(err("shard is shorter than its index"));
        }
        Ok(match self.index_location {
            IndexLocation::Start => (0, length),
            IndexLocation::End => (shard_length - length, shard_length),
        })
    }
}

/// One array's document held to the profile's codec rules and to the block.
fn read_layout(
    document: Value,
    id: &str,
    dimensions: &[String],
    frames: u64,
    levels: Option<u64>,
    (height, width): (u64, u64),
) -> Result<ColumnLayout, DecodeError> {
    let array = zarr::sharded_array(document, dimensions.len())?;
    let names = array
        .value
        .get("dimension_names")
        .and_then(|v| v.as_array())
        .ok_or_else(|| err("dimension_names missing"))?;
    if !names
        .iter()
        .map(|name| name.as_str())
        .eq(dimensions.iter().map(|name| Some(name.as_str())))
    {
        return Err(err(format!(
            "dimension_names must be {}",
            dimensions.join(", ")
        )));
    }
    let named = array
        .value
        .get("attributes")
        .and_then(|v| v.get(COLUMN_ATTRIBUTE))
        .and_then(|v| v.get("array"))
        .and_then(|v| v.as_str());
    if named != Some(id) {
        return Err(err("attributes must name the array in xue_columns"));
    }
    // Every shape is the axis, the levels where there are any, then cells.
    let axes: Vec<u64> = [Some(frames), levels].into_iter().flatten().collect();
    let cells = |shape: &[u64], what: &str| -> Result<(u64, u64), DecodeError> {
        match shape.strip_prefix(axes.as_slice()) {
            Some(&[rows, columns]) => Ok((rows, columns)),
            _ => Err(err(format!(
                "{what} must span the whole axis and every level"
            ))),
        }
    };
    if cells(&array.shape, "shape")? != (height, width) {
        return Err(err("shape must be the store's grid"));
    }
    let (block_height, block_width) = cells(&array.inner_shape, "inner chunk")?;
    let (shard_height, shard_width) = cells(&array.shard_shape, "shard")?;
    if shard_height % block_height != 0 || shard_width % block_width != 0 {
        return Err(err("shard must be whole blocks"));
    }
    if shard_width < width {
        return Err(err("shard must cover the grid's width"));
    }
    let level_count = levels.unwrap_or(1);
    block_height
        .checked_mul(block_width)
        .and_then(|cells| cells.checked_mul(frames))
        .and_then(|cells| cells.checked_mul(level_count))
        .filter(|&cells| cells <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("inner chunk exceeds the decoded size limit"))?;
    let chunks_per_shard = (shard_height / block_height)
        .checked_mul(shard_width / block_width)
        .filter(|&count| count.saturating_mul(INDEX_ENTRY_BYTES as u64) <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("shard index exceeds the decoded size limit"))?;
    // A block is bounded by the chunk and the blocks of a shard by its index,
    // but their product is not: hold it to the type the layout keeps.
    let dimension =
        |cells: u64| u32::try_from(cells).map_err(|_| err("array exceeds the decoded size limit"));
    Ok(ColumnLayout {
        frame_count: dimension(frames)?,
        level_count: dimension(level_count)?,
        height: dimension(height)?,
        width: dimension(width)?,
        block_height: dimension(block_height)?,
        block_width: dimension(block_width)?,
        shard_height: dimension(shard_height)?,
        shard_width: dimension(shard_width)?,
        shard_count: dimension(height.div_ceil(shard_height))?,
        chunks_per_shard: dimension(chunks_per_shard)?,
        delta: array.delta,
        index_location: array.index_location,
        fill_value: array.fill_value,
    })
}

// -- shard ---------------------------------------------------------------------

/// Parse one shard's index, CRC-32C verified, no chunk lying inside an index
/// at the shard's start. `bytes` is exactly the index.
pub fn parse_column_index(
    layout: &ColumnLayout,
    bytes: &[u8],
) -> Result<Vec<Option<ShardIndexEntry>>, DecodeError> {
    let index = zarr::parse_shard_index(bytes, layout.chunks_per_shard as usize)?;
    if layout.index_location == IndexLocation::Start
        && index
            .iter()
            .flatten()
            .any(|entry| entry.offset < layout.index_length() as u64)
    {
        return Err(err("shard index names bytes inside the index"));
    }
    Ok(index)
}

/// Where the block a cell lies in sits within its shard, or `None` for a
/// block the shard never held, which reads as the fill value. `index` is that
/// of the cell's shard ([`ColumnLayout::shard_of`]).
pub fn chunk_range(
    layout: &ColumnLayout,
    index: &[Option<ShardIndexEntry>],
    row: u32,
    column: u32,
) -> Result<Option<ShardIndexEntry>, DecodeError> {
    if index.len() != layout.chunks_per_shard as usize {
        return Err(err("shard index does not match the array's chunk grid"));
    }
    index
        .get(layout.position(row, column)?)
        .copied()
        .ok_or_else(|| err("cell is outside the shard"))
}

/// One cell's codes, `frames × levels`, frame by frame. `payload` is the
/// chunk of the block the cell lies in, `None` for one the shard never held.
pub fn decode_column(
    layout: &ColumnLayout,
    row: u32,
    column: u32,
    payload: Option<&[u8]>,
) -> Result<Vec<u8>, DecodeError> {
    layout.shard_of(row, column)?;
    let Some(payload) = payload else {
        return Ok(vec![layout.fill_value; layout.column_length()]);
    };
    // The delta runs along time only, so a frame's stride is every level of
    // the block: the grid store's chunk with the levels stacked as rows. The
    // layout holds the product to the decoded size limit.
    let chunk = crate::decode_chunk(
        payload,
        layout.frame_count,
        layout.level_count * layout.block_height,
        layout.block_width,
        if layout.delta {
            Predictor::Previous
        } else {
            Predictor::Raw
        },
    )?;
    let cell = (row % layout.block_height) as usize * layout.block_width as usize
        + (column % layout.block_width) as usize;
    let block = layout.block_height as usize * layout.block_width as usize;
    Ok(chunk.chunks_exact(block).map(|codes| codes[cell]).collect())
}

// -- reader --------------------------------------------------------------------

/// A column store being read: the group and the shard indices handed over so
/// far. The caller owns the requests and the chunks' bytes.
#[derive(Debug, Clone)]
pub struct ColumnReader {
    group: ColumnGroup,
    /// Verified indices by array id and shard.
    indices: BTreeMap<(String, u32), Vec<Option<ShardIndexEntry>>>,
}

impl ColumnReader {
    pub fn new(group: ColumnGroup) -> ColumnReader {
        ColumnReader {
            group,
            indices: BTreeMap::new(),
        }
    }

    pub fn group(&self) -> &ColumnGroup {
        &self.group
    }

    pub fn has_shard_index(&self, id: &str, shard: u32) -> bool {
        self.indices.contains_key(&(id.to_owned(), shard))
    }

    /// Verify one shard's index and hold it.
    pub fn set_shard_index(
        &mut self,
        id: &str,
        shard: u32,
        bytes: &[u8],
    ) -> Result<(), DecodeError> {
        let layout = &self.group.array(id)?.layout;
        if shard >= layout.shard_count {
            return Err(err("shard is outside the array"));
        }
        let index = parse_column_index(layout, bytes)?;
        self.indices.insert((id.to_owned(), shard), index);
        Ok(())
    }

    /// [`chunk_range`] through the index held for the cell's shard; an error
    /// until it has been handed over.
    pub fn chunk_range(
        &self,
        id: &str,
        row: u32,
        column: u32,
    ) -> Result<Option<ShardIndexEntry>, DecodeError> {
        let layout = &self.group.array(id)?.layout;
        let shard = layout.shard_of(row, column)?;
        let index = self
            .indices
            .get(&(id.to_owned(), shard))
            .ok_or_else(|| err("shard index is not resident yet"))?;
        chunk_range(layout, index, row, column)
    }

    /// [`decode_column`], the payload held to what the shard's index says of
    /// it: empty exactly for a block the shard never held, else the length
    /// the index gives. Without that a failed or cut read would pass for
    /// missing data.
    pub fn column(
        &self,
        id: &str,
        row: u32,
        column: u32,
        payload: &[u8],
    ) -> Result<Vec<u8>, DecodeError> {
        let entry = self.chunk_range(id, row, column)?;
        if payload.len() as u64 != entry.map_or(0, |entry| entry.length) {
            return Err(err("chunk payload is not the length the shard index gives"));
        }
        let layout = &self.group.array(id)?.layout;
        decode_column(layout, row, column, entry.map(|_| payload))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::zarr::crc32c;
    use ruzstd::encoding::{compress_to_vec, CompressionLevel};

    const FRAMES: usize = 3;
    const HEIGHT: usize = 10;
    const WIDTH: usize = 10;
    const BLOCK: usize = 4;
    const SHARD_HEIGHT: usize = 8;
    const SHARD_WIDTH: usize = 12;
    const FILL: u8 = 255;

    fn variable(id: &str, level: Option<u32>, offset: f64, scale: f64) -> Value {
        json!({
            "numericId": 1, "id": id, "label": id, "unit": "K",
            "quantization": {"type": "linear", "offset": offset, "scale": scale,
                             "minimumCode": 0, "maximumCode": 254, "nodataCode": 255},
            "parameter": {
                "discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                "typeOfFirstFixedSurface": if level.is_some() { 100 } else { 101 },
                "scaleFactorOfFirstFixedSurface": level.map(|_| 0),
                "scaledValueOfFirstFixedSurface": level.map(|level| level * 100),
            }
        })
    }

    fn array(
        id: &str,
        frames: usize,
        level: Option<(&str, usize)>,
        time: &str,
        delta: bool,
    ) -> Value {
        let over = |rows: usize, columns: usize| match level {
            Some((_, levels)) => json!([frames, levels, rows, columns]),
            None => json!([frames, rows, columns]),
        };
        let mut dimensions = vec![time];
        dimensions.extend(level.map(|(name, _)| name));
        dimensions.extend(["latitude", "longitude"]);
        let mut codecs = vec![
            json!({"name": "bytes"}),
            json!({"name": "zstd", "configuration": {"level": 6, "checksum": true}}),
        ];
        if delta {
            codecs.insert(
                0,
                json!({"name": "xue.delta", "configuration": {"axis": 0}}),
            );
        }
        json!({
            "zarr_format": 3, "node_type": "array", "data_type": "uint8",
            "shape": over(HEIGHT, WIDTH),
            "chunk_grid": {"name": "regular",
                           "configuration": {"chunk_shape": over(SHARD_HEIGHT, SHARD_WIDTH)}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": FILL,
            "codecs": [{"name": "sharding_indexed", "configuration": {
                "chunk_shape": over(BLOCK, BLOCK), "codecs": codecs,
                "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}},
                                 {"name": "crc32c"}],
                "index_location": "start"}}],
            "attributes": {"_FillValue": FILL, "xue_columns": {"array": id}},
            "dimension_names": dimensions,
        })
    }

    /// Two levelled arrays on different levels, `tmp` with a codebook per
    /// level and `rh` RAW, and a one-frame surface array.
    fn group_document(edit: impl FnOnce(&mut Value)) -> String {
        let mut document = json!({
            "zarr_format": 3, "node_type": "group",
            "attributes": {"xue_columns": {
                "version": 1, "model": "GFS", "product": "pgrb2.0p25",
                "runTime": "2026-11-24T00:00:00Z", "profile": "balanced",
                "time": {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": FRAMES,
                         "frameOffsets": [0, 1, 3]},
                "grid": {"width": WIDTH, "height": HEIGHT, "firstLongitude": 0.0,
                         "firstLatitude": 10.0, "longitudeStep": 0.25, "latitudeStep": -0.25,
                         "wrapLongitude": false},
                "arrays": [
                    {"id": "tmp", "levels": [1000, 850], "members": [
                        {"bundle": "tmp1000", "variable": variable("tmp1000", Some(1000), -50.0, 0.5)},
                        {"bundle": "tmp850", "variable": variable("tmp850", Some(850), -80.0, 1.0)}]},
                    {"id": "rh", "levels": [1000], "members": [
                        {"bundle": "rh1000", "variable": variable("rh1000", Some(1000), 0.0, 1.0)}]},
                    {"id": "orog",
                     "time": {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 1, "frameStep": 1},
                     "members": [{"bundle": "orog", "variable": variable("orog", None, -500.0, 30.0)}]}
                ]
            }},
            "consolidated_metadata": {"kind": "inline", "must_understand": false, "metadata": {
                "tmp": array("tmp", FRAMES, Some(("level", 2)), "time", true),
                "rh": array("rh", FRAMES, Some(("level_rh", 1)), "time", false),
                "orog": array("orog", 1, None, "time_orog", false),
            }}
        });
        edit(&mut document);
        document.to_string()
    }

    fn code(frame: usize, level: usize, row: usize, column: usize) -> u8 {
        if row == 5 && column == 6 {
            return FILL;
        }
        ((frame * 91 + level * 37 + row * 13 + column * 5) % 255) as u8
    }

    /// One shard of `layout` under [`code`], its blocks in row-major order
    /// after the index, `skip` the index position left out.
    fn shard(layout: &ColumnLayout, shard: usize, skip: Option<usize>) -> Vec<u8> {
        let (frames, levels) = (layout.frame_count as usize, layout.level_count as usize);
        let mut index = Vec::new();
        let mut data = Vec::new();
        for position in 0..layout.chunks_per_shard as usize {
            let across = SHARD_WIDTH / BLOCK;
            let row = shard * SHARD_HEIGHT + position / across * BLOCK;
            let column = position % across * BLOCK;
            if row >= HEIGHT || column >= WIDTH || skip == Some(position) {
                index.extend_from_slice(&[0xff; 16]);
                continue;
            }
            let mut chunk = Vec::new();
            for frame in 0..frames {
                for level in 0..levels {
                    for cell in 0..BLOCK * BLOCK {
                        let (r, c) = (row + cell / BLOCK, column + cell % BLOCK);
                        chunk.push(if r < HEIGHT && c < WIDTH {
                            code(frame, level, r, c)
                        } else {
                            FILL
                        });
                    }
                }
            }
            if layout.delta {
                let stride = levels * BLOCK * BLOCK;
                for at in (stride..chunk.len()).rev() {
                    chunk[at] = chunk[at].wrapping_sub(chunk[at - stride]);
                }
            }
            let payload = compress_to_vec(chunk.as_slice(), CompressionLevel::Fastest);
            index.extend_from_slice(&((layout.index_length() + data.len()) as u64).to_le_bytes());
            index.extend_from_slice(&(payload.len() as u64).to_le_bytes());
            data.extend_from_slice(&payload);
        }
        let checksum = crc32c(&index);
        index.extend_from_slice(&checksum.to_le_bytes());
        [index, data].concat()
    }

    fn payload(shard: &[u8], entry: Option<ShardIndexEntry>) -> &[u8] {
        entry.map_or(&[], |entry| {
            &shard[entry.offset as usize..][..entry.length as usize]
        })
    }

    #[test]
    fn reads_the_block_and_every_layout() {
        let text = group_document(|_| {});
        assert_eq!(zarr::store_kind(&text).unwrap(), StoreKind::Column);
        let group = ColumnGroup::parse(&text).unwrap();
        assert_eq!(group.model, "GFS");
        assert_eq!(group.profile, "balanced");
        assert_eq!(group.run_time_seconds, 1_795_478_400);
        assert_eq!(group.offsets, [0, 1, 3]);
        let ids: Vec<&str> = group.arrays.iter().map(|array| array.id.as_str()).collect();
        assert_eq!(ids, ["tmp", "rh", "orog"]);

        let tmp = group.array("tmp").unwrap();
        assert_eq!(tmp.levels.as_deref(), Some(&[1000, 850][..]));
        assert_eq!(tmp.members[1].bundle, "tmp850");
        assert_eq!(tmp.members[1].variable.id, "tmp850");
        assert!(tmp.layout.delta);
        assert_eq!(tmp.layout.shard_count, 2);
        assert_eq!(tmp.layout.chunks_per_shard, 6);
        assert_eq!(tmp.layout.index_length(), 100);
        assert_eq!(tmp.layout.shard_index_range(500).unwrap(), (0, 100));
        assert!(tmp.layout.shard_index_range(99).is_err());
        assert_eq!(tmp.shard_path(1), "tmp/c/0/0/1/0");
        assert_eq!(tmp.layout.shard_of(7, 9).unwrap(), 0);
        assert_eq!(tmp.layout.shard_of(8, 0).unwrap(), 1);
        assert_eq!(tmp.layout.position(8, 0).unwrap(), 0);
        assert_eq!(tmp.layout.position(7, 9).unwrap(), 5);
        assert!(tmp.layout.shard_of(10, 0).is_err());
        assert!(tmp.layout.position(0, 10).is_err());

        assert!(!group.array("rh").unwrap().layout.delta);
        let orog = group.array("orog").unwrap();
        assert_eq!(orog.offsets, [0]);
        assert_eq!(orog.layout.level_count, 1);
        assert_eq!(orog.shard_path(1), "orog/c/0/1/0");
        assert!(group.array("time").is_err());

        let block: Value = serde_json::from_str(&group.block_json).unwrap();
        let whole: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(block, whole["attributes"]["xue_columns"]);
    }

    #[test]
    fn a_column_is_one_chunk() {
        for (id, skip) in [("tmp", None), ("rh", Some(1)), ("orog", None)] {
            let group = ColumnGroup::parse(&group_document(|_| {})).unwrap();
            let layout = group.array(id).unwrap().layout;
            let shards = [shard(&layout, 0, skip), shard(&layout, 1, None)];
            let mut reader = ColumnReader::new(group);
            assert!(reader.chunk_range(id, 0, 0).is_err(), "no index yet");
            for (number, bytes) in shards.iter().enumerate() {
                let index = &bytes[..layout.index_length()];
                reader.set_shard_index(id, number as u32, index).unwrap();
                assert!(reader.has_shard_index(id, number as u32));
            }
            assert!(!reader.has_shard_index(id, 2));
            for row in 0..HEIGHT {
                for column in 0..WIDTH {
                    let (r, c) = (row as u32, column as u32);
                    let entry = reader.chunk_range(id, r, c).unwrap();
                    let written = skip.is_none() || row >= 4 || !(4..8).contains(&column);
                    assert_eq!(entry.is_some(), written, "{id} {row} {column}");
                    let bytes = payload(&shards[row / SHARD_HEIGHT], entry);
                    let codes = reader.column(id, r, c, bytes).unwrap();
                    let want: Vec<u8> = (0..layout.frame_count as usize)
                        .flat_map(|frame| {
                            (0..layout.level_count as usize).map(move |level| (frame, level))
                        })
                        .map(|(frame, level)| {
                            if written {
                                code(frame, level, row, column)
                            } else {
                                FILL
                            }
                        })
                        .collect();
                    assert_eq!(codes, want, "{id} {row} {column}");
                }
            }
        }
    }

    #[test]
    fn each_level_reads_through_its_own_codebook() {
        let group = ColumnGroup::parse(&group_document(|_| {})).unwrap();
        let tmp = group.array("tmp").unwrap();
        let values = tmp.values(&[100, 100, 0, 254, 255, 1]).unwrap();
        assert_eq!(&values[..4], [0.0, 20.0, -50.0, 174.0]);
        assert!(values[4].is_nan());
        assert_eq!(values[5], -79.0);
        assert!(tmp.values(&[0; 5]).is_err());
    }

    #[test]
    fn a_payload_is_held_to_the_index() {
        let group = ColumnGroup::parse(&group_document(|_| {})).unwrap();
        let layout = group.array("tmp").unwrap().layout;
        let bytes = shard(&layout, 0, Some(1));
        let index = bytes[..layout.index_length()].to_vec();
        let mut reader = ColumnReader::new(group);
        assert!(reader.column("tmp", 0, 0, &[]).is_err(), "no index yet");

        assert!(reader.set_shard_index("tmp", 2, &index).is_err());
        assert!(reader.set_shard_index("hgt", 0, &index).is_err());
        assert!(reader.set_shard_index("tmp", 0, &index[1..]).is_err());
        let mut flipped = index.clone();
        flipped[3] ^= 1;
        assert!(reader.set_shard_index("tmp", 0, &flipped).is_err());
        // A pair whose chunk starts inside the index, its checksum right.
        let mut inside = index.clone();
        inside[..8].copy_from_slice(&99u64.to_le_bytes());
        let end = inside.len() - 4;
        let checksum = crc32c(&inside[..end]);
        inside[end..].copy_from_slice(&checksum.to_le_bytes());
        assert!(reader.set_shard_index("tmp", 0, &inside).is_err());
        assert!(!reader.has_shard_index("tmp", 0));
        reader.set_shard_index("tmp", 0, &index).unwrap();

        let chunk = payload(&bytes, reader.chunk_range("tmp", 0, 0).unwrap()).to_vec();
        assert!(reader.column("tmp", 0, 0, &chunk).is_ok());
        assert!(
            reader.column("tmp", 0, 0, &[]).is_err(),
            "a written chunk is not empty"
        );
        assert!(reader.column("tmp", 0, 0, &chunk[1..]).is_err());
        let mut damaged = chunk.clone();
        *damaged.last_mut().unwrap() ^= 1;
        assert!(reader.column("tmp", 0, 0, &damaged).is_err());
        assert!(
            reader.column("tmp", 0, 4, &chunk).is_err(),
            "a hole has no payload"
        );
        assert_eq!(reader.column("tmp", 0, 4, &[]).unwrap(), [FILL; 6]);
        assert!(reader.column("tmp", 10, 0, &[]).is_err());
        assert!(reader.column("tmp", 0, 10, &[]).is_err());
        // A whole frame of the wrong shape.
        let short = compress_to_vec(&[0u8; 95][..], CompressionLevel::Fastest);
        assert!(decode_column(&layout, 0, 0, Some(&short)).is_err());
    }

    #[test]
    fn an_index_at_the_end_reads_too() {
        let group = ColumnGroup::parse(&group_document(|d| {
            d["consolidated_metadata"]["metadata"]["tmp"]["codecs"][0]["configuration"]
                ["index_location"] = json!("end");
        }))
        .unwrap();
        let layout = group.array("tmp").unwrap().layout;
        assert_eq!(layout.index_location, IndexLocation::End);
        assert_eq!(layout.shard_index_range(500).unwrap(), (400, 500));
        let mut index = Vec::new();
        for position in 0..6u64 {
            index.extend_from_slice(&(position * 7).to_le_bytes());
            index.extend_from_slice(&7u64.to_le_bytes());
        }
        let checksum = crc32c(&index);
        index.extend_from_slice(&checksum.to_le_bytes());
        let mut reader = ColumnReader::new(group);
        reader.set_shard_index("tmp", 0, &index).unwrap();
        let entry = reader.chunk_range("tmp", 0, 9).unwrap().unwrap();
        assert_eq!((entry.offset, entry.length), (14, 7));
    }

    #[test]
    fn more_variables_than_a_bundle_numbers_still_read() {
        let levels: Vec<u32> = (1..=300).rev().collect();
        let text = group_document(|d| {
            let members: Vec<Value> = levels
                .iter()
                .map(|&level| {
                    let id = format!("tmp{level}");
                    json!({"bundle": id, "variable": variable(&id, Some(level), 0.0, 1.0)})
                })
                .collect();
            let block = &mut d["attributes"]["xue_columns"]["arrays"][0];
            block["levels"] = json!(levels);
            block["members"] = json!(members);
            d["consolidated_metadata"]["metadata"]["tmp"] =
                array("tmp", FRAMES, Some(("level", 300)), "time", true);
        });
        let group = ColumnGroup::parse(&text).unwrap();
        assert_eq!(group.arrays[0].members[299].variable.id, "tmp1");
        assert_eq!(group.arrays[1].members[0].variable.id, "rh1000");
    }

    #[test]
    fn tells_the_branches_apart() {
        for other in ["xue", "xue_bundles", "xue_polar", "xue_network"] {
            let both = group_document(|d| d["attributes"][other] = json!({"version": 1}));
            assert!(zarr::store_kind(&both).is_err(), "{other}");
            assert!(ColumnGroup::parse(&both).is_err(), "{other}");
            let alone = group_document(|d| d["attributes"] = json!({other: {"version": 1}}));
            assert!(ColumnGroup::parse(&alone).is_err(), "{other}");
        }
        for profile in [1, 2] {
            let both = group_document(|d| d["attributes"]["xue_profile"] = json!(profile));
            assert!(ColumnGroup::parse(&both).is_err(), "xue_profile {profile}");
        }
        // The level is the writer's: a reader follows the chain whatever it is.
        for level in [1, 15] {
            let text = group_document(|d| {
                let tmp = &mut d["consolidated_metadata"]["metadata"]["tmp"];
                tmp["codecs"][0]["configuration"]["codecs"][2]["configuration"]["level"] = json!(level);
            });
            assert!(ColumnGroup::parse(&text).is_ok(), "level {level}");
        }
        let text = group_document(|_| {});
        assert!(zarr::parse_group_metadata(&text).is_err());
        assert!(zarr::parse_group_metadata_for(&text, "tmp1000").is_err());
        assert!(crate::network::parse_network_group(&text).is_err());
    }

    #[test]
    fn rejects_malformed_blocks() {
        fn block(d: &mut Value) -> &mut Value {
            &mut d["attributes"]["xue_columns"]
        }
        fn remove(value: &mut Value, key: &str) {
            value.as_object_mut().unwrap().remove(key);
        }
        let edits: Vec<fn(&mut Value)> = vec![
            |d| block(d)["version"] = json!(2),
            |d| remove(block(d), "version"),
            |d| d["attributes"]["xue_columns"] = json!([]),
            |d| block(d)["model"] = json!(""),
            |d| remove(block(d), "product"),
            |d| remove(block(d), "profile"),
            |d| block(d)["runTime"] = json!("yesterday"),
            |d| block(d)["runTime"] = json!("2026-11-24T00:00:00.5Z"),
            |d| remove(block(d), "time"),
            |d| block(d)["time"]["frameOffsets"] = json!([0, 1, 2]),
            |d| block(d)["time"]["frameCount"] = json!(0),
            |d| block(d)["time"]["hours"] = json!([0]),
            |d| remove(block(d), "grid"),
            |d| remove(&mut block(d)["grid"], "longitudeStep"),
            |d| remove(&mut block(d)["grid"], "wrapLongitude"),
            |d| block(d)["grid"]["width"] = json!(1u64 << 40),
            |d| block(d)["grid"]["width"] = json!(11),
            |d| block(d)["grid"]["projection"] = json!({"kind": "lambertConformal"}),
            |d| block(d)["grid"]["gaussian"] = json!({"n": 2, "firstRow": 0, "rowStride": 1}),
            |d| block(d)["arrays"] = json!([]),
            |d| block(d)["arrays"][0]["id"] = json!("Tmp"),
            |d| block(d)["arrays"][0]["id"] = json!("level"),
            |d| block(d)["arrays"][0]["id"] = json!("tmp_2"),
            |d| block(d)["arrays"][0]["id"] = json!("hgt"),
            |d| block(d)["arrays"][1]["id"] = json!("tmp"),
            |d| block(d)["arrays"][0]["levels"] = json!([850, 1000]),
            |d| block(d)["arrays"][0]["levels"] = json!([1000, 1000]),
            |d| block(d)["arrays"][0]["levels"] = json!([1000, 0]),
            |d| block(d)["arrays"][0]["levels"] = json!([1000.5, 850]),
            |d| block(d)["arrays"][0]["levels"] = json!([1u64 << 32, 850]),
            |d| block(d)["arrays"][0]["levels"] = json!([]),
            |d| block(d)["arrays"][0]["levels"] = json!([1000]),
            |d| block(d)["arrays"][0]["levels"] = json!(null),
            |d| remove(&mut block(d)["arrays"][0], "levels"),
            |d| block(d)["arrays"][2]["members"] = json!([]),
            |d| block(d)["arrays"][0]["members"][0]["bundle"] = json!(""),
            |d| block(d)["arrays"][0]["members"][0]["bundle"] = json!("Tmp1000"),
            |d| block(d)["arrays"][0]["members"][0]["bundle"] = json!("1000tmp"),
            |d| block(d)["arrays"][0]["members"][0]["bundle"] = json!("tmp/1000"),
            |d| block(d)["arrays"][0]["members"][0]["bundle"] = json!(1000),
            |d| remove(&mut block(d)["arrays"][0]["members"][0], "variable"),
            |d| block(d)["arrays"][0]["members"][0]["variable"] = json!(1),
            |d| {
                remove(
                    &mut block(d)["arrays"][0]["members"][0]["variable"],
                    "parameter",
                )
            },
            |d| {
                remove(
                    &mut block(d)["arrays"][0]["members"][0]["variable"],
                    "quantization",
                )
            },
            |d| block(d)["arrays"][0]["members"][1]["variable"]["id"] = json!("tmp1000"),
            |d| block(d)["arrays"][1]["members"][0]["variable"]["id"] = json!("tmp1000"),
            // One member's codebook reserves another code than the array's fill.
            |d| {
                block(d)["arrays"][0]["members"][1]["variable"]["quantization"]["nodataCode"] =
                    json!(254)
            },
            |d| block(d)["arrays"][2]["members"][0]["variable"]["id"] = json!("orography"),
            // Its own axis, but the store's.
            |d| {
                block(d)["arrays"][1]["time"] = block(d)["time"].clone();
                d["consolidated_metadata"]["metadata"]["rh"]["dimension_names"][0] =
                    json!("time_rh");
            },
            |d| remove(d, "consolidated_metadata"),
            |d| d["consolidated_metadata"]["kind"] = json!("other"),
            |d| d["consolidated_metadata"]["metadata"] = json!([]),
            |d| remove(&mut d["consolidated_metadata"]["metadata"], "rh"),
            |d| d["node_type"] = json!("array"),
        ];
        for (position, edit) in edits.into_iter().enumerate() {
            assert!(
                ColumnGroup::parse(&group_document(edit)).is_err(),
                "edit {position} was accepted"
            );
        }
        assert!(ColumnGroup::parse("{").is_err());
        // Nothing left on the store's axis.
        let own = group_document(|d| {
            let arrays = block(d)["arrays"].as_array_mut().unwrap();
            arrays.drain(..2);
        });
        assert!(ColumnGroup::parse(&own).is_err());
    }

    #[test]
    fn rejects_malformed_arrays() {
        fn shard_shape(a: &mut Value) -> &mut Value {
            &mut a["chunk_grid"]["configuration"]["chunk_shape"]
        }
        fn sharding(a: &mut Value) -> &mut Value {
            &mut a["codecs"][0]["configuration"]
        }
        let edits: Vec<fn(&mut Value)> = vec![
            |a| a["node_type"] = json!("group"),
            |a| a["data_type"] = json!("int16"),
            |a| a["shape"] = json!([3, 2, 10]),
            |a| a["shape"] = json!([4, 2, 10, 10]),
            |a| a["shape"] = json!([3, 1, 10, 10]),
            |a| a["shape"] = json!([3, 2, 10, 9]),
            |a| a["dimension_names"] = json!(["time", "level", "longitude", "latitude"]),
            |a| a["dimension_names"] = json!(["time", "level_tmp", "latitude", "longitude"]),
            |a| a["dimension_names"] = json!(["time", "level", "latitude"]),
            |a| a["dimension_names"] = json!(null),
            |a| a["attributes"] = json!({}),
            |a| a["attributes"]["xue_columns"]["array"] = json!("rh"),
            |a| a["fill_value"] = json!(0),
            |a| a["chunk_key_encoding"]["name"] = json!("v2"),
            |a| *shard_shape(a) = json!([6, 2, 8, 12]),
            |a| *shard_shape(a) = json!([3, 4, 8, 12]),
            |a| *shard_shape(a) = json!([3, 2, 9, 12]),
            |a| *shard_shape(a) = json!([3, 2, 8, 10]),
            |a| *shard_shape(a) = json!([3, 2, 8, 8]),
            |a| *shard_shape(a) = json!([3, 2, 1u64 << 60, 1u64 << 60]),
            |a| *shard_shape(a) = json!([3, 2, 4u64 << 32, 12]),
            |a| sharding(a)["chunk_shape"] = json!([1, 2, 4, 4]),
            |a| sharding(a)["chunk_shape"] = json!([3, 1, 4, 4]),
            |a| sharding(a)["chunk_shape"] = json!([3, 2, 0, 4]),
            |a| {
                sharding(a)["chunk_shape"] = json!([3, 2, 1u64 << 24, 12]);
                *shard_shape(a) = json!([3, 2, 1u64 << 24, 12]);
            },
            |a| {
                sharding(a)["chunk_shape"] = json!([3, 2, 1u64 << 62, 1u64 << 62]);
                *shard_shape(a) = json!([3, 2, 1u64 << 62, 1u64 << 62]);
            },
            |a| sharding(a)["index_location"] = json!("middle"),
            |a| sharding(a)["index_codecs"] = json!([{"name": "bytes"}]),
            |a| sharding(a)["codecs"][0]["configuration"]["axis"] = json!(1),
            |a| sharding(a)["codecs"] = json!([{"name": "bytes"}, {"name": "gzip"}]),
            |a| a["codecs"] = json!([{"name": "bytes"}, {"name": "zstd"}]),
        ];
        for (position, edit) in edits.into_iter().enumerate() {
            let text = group_document(|d| edit(&mut d["consolidated_metadata"]["metadata"]["tmp"]));
            assert!(
                ColumnGroup::parse(&text).is_err(),
                "edit {position} was accepted"
            );
        }
        // A level list of its own is named for its array, and so is an axis.
        for (id, at, name) in [("rh", 1, "level"), ("orog", 0, "time")] {
            let text = group_document(|d| {
                d["consolidated_metadata"]["metadata"][id]["dimension_names"][at] = json!(name);
            });
            assert!(ColumnGroup::parse(&text).is_err(), "{id}");
        }
        // A block larger than the grid, and a shard wider than it, are legal.
        let wide = group_document(|d| {
            let tmp = &mut d["consolidated_metadata"]["metadata"]["tmp"];
            *shard_shape(tmp) = json!([3, 2, 16, 32]);
            sharding(tmp)["chunk_shape"] = json!([3, 2, 16, 16]);
        });
        let layout = ColumnGroup::parse(&wide).unwrap().arrays[0].layout;
        assert_eq!((layout.shard_count, layout.chunks_per_shard), (1, 2));
    }
}

#[cfg(test)]
mod schema_tie_tests {
    //! The group and array fields this reader reads are the ones
    //! `docs/schemas/` names. The array's shape, codecs and fill value are
    //! `crate::zarr`'s to read, and tied there.
    use super::{COLUMN_ATTRIBUTE, GRID_NUMBERS, GRID_PROFILE_ATTRIBUTE};
    use crate::schematie::{assert_named, schema};

    #[test]
    fn the_column_block_read_is_the_schemas() {
        assert_named("column-group", "", &["consolidated_metadata"]);
        assert_named(
            "store-group",
            "/$defs/ConsolidatedMetadata",
            &["kind", "metadata"],
        );
        assert_named(
            "column-group",
            "/properties/attributes",
            &[COLUMN_ATTRIBUTE, GRID_PROFILE_ATTRIBUTE],
        );
        assert_named(
            "column-group",
            "/$defs/Columns",
            &[
                "version", "model", "product", "runTime", "profile", "time", "grid", "arrays",
            ],
        );
        assert_named(
            "column-group",
            "/$defs/Array",
            &["id", "time", "levels", "members"],
        );
        assert_named("column-group", "/$defs/Member", &["bundle", "variable"]);
        assert_named("bundle-metadata", "/$defs/Grid", &GRID_NUMBERS);
        assert_named(
            "bundle-metadata",
            "/$defs/Grid",
            &["projection", "wrapLongitude"],
        );
        assert_named(
            "bundle-metadata",
            "/$defs/Variable",
            &["numericId", "id", "quantization"],
        );
        assert_named("store-array", "", &["dimension_names", "attributes"]);
        assert_named("store-array", "/$defs/Attributes", &[COLUMN_ATTRIBUTE]);
        assert_named("store-array", "/$defs/ColumnArrayBlock", &["array"]);
    }

    /// The fields the block must carry are the ones the reader refuses a
    /// block without.
    #[test]
    fn the_version_and_the_required_fields_agree() {
        let schema = schema("column-group");
        let columns = &schema["$defs"]["Columns"];
        assert_eq!(
            columns["properties"]["version"]["const"],
            super::COLUMN_VERSION
        );
        for field in [
            "version", "model", "product", "runTime", "profile", "time", "grid", "arrays",
        ] {
            let required = columns["required"].as_array().unwrap();
            assert!(required.iter().any(|name| name == field), "{field}");
        }
    }
}
