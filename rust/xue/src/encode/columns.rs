//! A run's column store, cut from its finished map stores.
//!
//! `docs/zarr-profile.md`, "Column store", is normative and
//! `xuebuild/columns.py` the reference, mirrored byte for byte. The input is
//! the plan both writers take: which array holds which variable of which map
//! store, its predictor and its levels. Everything else, metadata, grid, axis,
//! codebooks, codes and tiling, is read from the stores the plan names, so
//! nothing here knows a source or opens the registry document.
//!
//! The unit of work is one shard of one array: a band of rows, every level
//! over the whole axis. Its codes are held uncompressed, each map shard is
//! read by seeking to the tiles the band overlaps, and payloads go to disk as
//! they are compressed, so memory is one band per worker whatever the run's
//! size. `zarr.json` is written last: until then no reader takes the directory
//! for a store, and a failed cut removes it.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::ops::Range;
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use serde_json::{json, Map, Value};

use crate::column::{
    is_id, ColumnGroup, COLUMN_ATTRIBUTE, COLUMN_BLOCK_CELLS, COLUMN_SHARD_ROWS, COLUMN_VERSION, COORDINATES,
};
use crate::encode::binformat::crc32_plane;
use crate::encode::errors::{EncodeError, Result};
use crate::encode::pyjson::dumps_indented;
use crate::encode::zarrstore::{
    compress_chunk, compression_pool, coordinate_metadata, write_coordinates, write_file,
    write_file_atomic,
};
use crate::format::MAX_PLANE_LENGTH;
use crate::zarr::{self, ArrayLayout, IndexLocation, ShardIndexEntry, DELTA_CODEC};

/// The `columnsPlan` this writer reads.
pub const PLAN_VERSION: u64 = 1;

/// The inner chunks' zstd level, which the array documents declare; the grid
/// stores' is 15. A run's whole store is cut on one machine after the run is
/// live, and 15 costs several times the time for a few percent fewer bytes.
const ZSTD_LEVEL: i32 = 6;
const BLOCK: usize = COLUMN_BLOCK_CELLS as usize;
const SHARD_ROWS: usize = COLUMN_SHARD_ROWS as usize;
/// The index pair of a block wholly past the grid.
const NEVER_WRITTEN: [u8; 16] = [0xff; 16];

fn invalid(message: impl Into<String>) -> EncodeError {
    EncodeError::bundle(message)
}

fn io_error(verb: &str, path: &Path, error: &std::io::Error) -> EncodeError {
    EncodeError::conversion(format!("cannot {verb} {}: {error}", path.display()))
}

// -- the plan ------------------------------------------------------------------

struct PlanMember {
    bundle: String,
    variable: String,
    /// The map store, relative to the run directory.
    store: String,
}

struct PlanArray {
    id: String,
    previous: bool,
    levels: Option<Vec<i32>>,
    members: Vec<PlanMember>,
}

/// An object of every `required` key and no key beyond `optional`.
fn closed<'a>(node: &'a Value, required: &[&str], optional: &[&str], what: &str) -> Result<&'a Map<String, Value>> {
    node.as_object()
        .filter(|object| {
            required.iter().all(|key| object.contains_key(*key))
                && object.keys().all(|key| required.contains(&key.as_str()) || optional.contains(&key.as_str()))
        })
        .ok_or_else(|| {
            invalid(format!("{what} must be an object of {}", [required, optional].concat().join(", ")))
        })
}

fn parse_plan(text: &str) -> Result<Vec<PlanArray>> {
    let plan: Value =
        serde_json::from_str(text).map_err(|error| invalid(format!("the column plan is not JSON: {error}")))?;
    let document = closed(&plan, &["columnsPlan", "arrays"], &[], "the column plan")?;
    if document["columnsPlan"].as_u64() != Some(PLAN_VERSION) {
        return Err(invalid(format!("column plan version {} is not {PLAN_VERSION}", document["columnsPlan"])));
    }
    let entries = document["arrays"]
        .as_array()
        .filter(|entries| !entries.is_empty())
        .ok_or_else(|| invalid("a column store holds at least one array"))?;
    let mut array_ids = BTreeSet::new();
    let mut variable_ids = BTreeSet::new();
    let mut arrays = Vec::with_capacity(entries.len());
    for (number, entry) in entries.iter().enumerate() {
        let what = format!("column plan array {number}");
        let entry = closed(entry, &["id", "predictor", "members"], &["levels"], &what)?;
        let id = entry["id"]
            .as_str()
            .filter(|id| is_id(id) && !COORDINATES.contains(id))
            .ok_or_else(|| invalid(format!("{} cannot name a column array", entry["id"])))?;
        if !array_ids.insert(id) {
            return Err(invalid(format!("column array {id} occurs twice")));
        }
        let previous = match entry["predictor"].as_str() {
            Some("previous") => true,
            Some("raw") => false,
            _ => return Err(invalid(format!("column array {id}: unknown predictor {}", entry["predictor"]))),
        };
        let members = parse_members(&entry["members"], id, &mut variable_ids)?;
        let levels = match entry.get("levels") {
            Some(levels) => Some(parse_levels(levels, members.len(), id)?),
            None if members.len() == 1 && members[0].variable == id => None,
            None => {
                return Err(invalid(format!(
                    "column array {id}: a surface array has one member and is named by it"
                )))
            }
        };
        arrays.push(PlanArray { id: id.to_owned(), previous, levels, members });
    }
    Ok(arrays)
}

fn parse_members(node: &Value, id: &str, seen: &mut BTreeSet<String>) -> Result<Vec<PlanMember>> {
    let entries = node
        .as_array()
        .filter(|entries| !entries.is_empty())
        .ok_or_else(|| invalid(format!("column array {id} has no members")))?;
    let mut members = Vec::with_capacity(entries.len());
    for entry in entries {
        let what = format!("column array {id}: a member");
        let entry = closed(entry, &["bundle", "variable", "store"], &[], &what)?;
        let text = |key: &str| -> Result<String> {
            entry[key].as_str().filter(|text| !text.is_empty()).map(str::to_owned).ok_or_else(|| {
                invalid(format!("column array {id}: a member names its bundle, variable and store"))
            })
        };
        let member = PlanMember { bundle: text("bundle")?, variable: text("variable")?, store: text("store")? };
        if !seen.insert(member.variable.clone()) {
            return Err(invalid(format!("variable {} occurs twice in one column store", member.variable)));
        }
        members.push(member);
    }
    Ok(members)
}

/// Strictly descending positive hPa, one per member, in the `int32` range of
/// the `level` coordinate.
fn parse_levels(node: &Value, count: usize, id: &str) -> Result<Vec<i32>> {
    node.as_array()
        .and_then(|levels| {
            levels
                .iter()
                .map(|level| level.as_u64().and_then(|level| i32::try_from(level).ok()).filter(|&level| level > 0))
                .collect::<Option<Vec<i32>>>()
        })
        .filter(|levels| levels.len() == count && levels.windows(2).all(|pair| pair[1] < pair[0]))
        .ok_or_else(|| {
            invalid(format!("column array {id}: levels must be strictly descending hPa, one per member"))
        })
}

// -- what the map stores hold --------------------------------------------------

/// One variable's array in a map store, its shard's index read and verified.
struct MapArray {
    path: PathBuf,
    layout: ArrayLayout,
    index: Vec<Option<ShardIndexEntry>>,
}

impl MapArray {
    fn open(path: PathBuf, layout: ArrayLayout) -> Result<MapArray> {
        let fail = |error: &std::io::Error| io_error("read", &path, error);
        let mut file = fs::File::open(&path).map_err(|error| fail(&error))?;
        let length = file.metadata().map_err(|error| fail(&error))?.len();
        // The layout holds the index to the decoded size limit.
        let index_length = zarr::shard_index_length(layout.chunks_per_shard as usize);
        if length < index_length as u64 {
            return Err(invalid(format!("{} is smaller than its index", path.display())));
        }
        if layout.index_location == IndexLocation::End {
            file.seek(SeekFrom::Start(length - index_length as u64)).map_err(|error| fail(&error))?;
        }
        let mut bytes = vec![0u8; index_length];
        file.read_exact(&mut bytes).map_err(|error| fail(&error))?;
        let index = zarr::parse_shard_index(&bytes, layout.chunks_per_shard as usize)
            .map_err(|error| invalid(format!("{}: {}", path.display(), error.0)))?;
        if index
            .iter()
            .flatten()
            .any(|entry| entry.offset.checked_add(entry.length).is_none_or(|end| end > length))
        {
            return Err(invalid(format!("{}: an index entry exceeds the shard", path.display())));
        }
        Ok(MapArray { path, layout, index })
    }

    /// Every frame of a band of grid rows, read tile by tile: `place(frame,
    /// row of the band, first column, codes)` for each run of cells. A tile
    /// the shard never held is the array's fill value.
    fn read_rows(&self, rows: &Range<usize>, mut place: impl FnMut(usize, usize, usize, &[u8])) -> Result<()> {
        let layout = &self.layout;
        let fail = |error: &std::io::Error| io_error("read", &self.path, error);
        let mut file = fs::File::open(&self.path).map_err(|error| fail(&error))?;
        let (tile_height, tile_width) = (layout.tile_height as usize, layout.tile_width as usize);
        let tile_cells = tile_height * tile_width;
        let tile_rows = rows.start / tile_height..rows.end.div_ceil(tile_height);
        let mut payload = Vec::new();
        for time_chunk in 0..layout.time_chunks {
            let first_frame = time_chunk as usize * layout.time_chunk as usize;
            let frames = zarr::frames_in_time_chunk(layout, time_chunk) as usize;
            let (_, first_entry) = zarr::shard_of(layout, time_chunk);
            for tile_row in tile_rows.clone() {
                let top = tile_row * tile_height;
                let (low, high) = (rows.start.max(top), rows.end.min(top + tile_height));
                for tile_column in 0..layout.tile_columns as usize {
                    let entry = first_entry as usize + tile_row * layout.tile_columns as usize + tile_column;
                    let chunk = match self.index.get(entry).copied().flatten() {
                        None => vec![layout.fill_value; tile_cells * layout.time_chunk as usize],
                        Some(span) => {
                            // Held to the file's length when the index was read.
                            payload.resize(span.length as usize, 0);
                            file.seek(SeekFrom::Start(span.offset)).map_err(|error| fail(&error))?;
                            file.read_exact(&mut payload).map_err(|error| fail(&error))?;
                            zarr::decode_inner_chunk(layout, &payload)
                                .map_err(|error| invalid(format!("{}: {}", self.path.display(), error.0)))?
                        }
                    };
                    let left = tile_column * tile_width;
                    let cells = tile_width.min(layout.width as usize - left);
                    for frame in 0..frames {
                        for row in low..high {
                            let source = frame * tile_cells + (row - top) * tile_width;
                            place(first_frame + frame, row - rows.start, left, &chunk[source..source + cells]);
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

struct Member {
    bundle: String,
    /// The bundle metadata's variable object, verbatim.
    variable: Value,
    array: MapArray,
}

/// One array of the store about to be cut.
struct ColumnArray {
    id: String,
    levels: Option<Vec<i32>>,
    /// The metadata of its first member's bundle, whose `time` is the array's.
    metadata: Value,
    members: Vec<Member>,
    delta: bool,
    fill_value: u8,
    geometry: Geometry,
}

impl ColumnArray {
    fn time(&self) -> &Value {
        &self.metadata["time"]
    }
}

/// A map store's group document, parsed once for all its members.
struct Group {
    document: Value,
}

impl Group {
    fn read(store: &Path) -> Result<Group> {
        let path = store.join("zarr.json");
        let text = fs::read_to_string(&path).map_err(|error| io_error("read", &path, &error))?;
        // Holds the document to the grid branch and to a profile that is read.
        zarr::group_bundle_ids(&text).map_err(|error| invalid(format!("{}: {}", path.display(), error.0)))?;
        let document =
            serde_json::from_str(&text).map_err(|error| invalid(format!("{}: {error}", path.display())))?;
        Ok(Group { document })
    }

    /// One bundle's metadata block, whichever profile the store is of.
    fn metadata(&self, bundle: &str) -> Option<&Value> {
        let attributes = &self.document["attributes"];
        attributes.get("xue").or_else(|| attributes["xue_bundles"].get(bundle)).filter(|block| block.is_object())
    }

    /// The consolidated block holds every array's document, so a cut needs no
    /// object of the store but its group document and the shards.
    fn array(&self, variable: &str) -> Option<&Value> {
        self.document["consolidated_metadata"]["metadata"].get(variable).filter(|document| document.is_object())
    }
}

/// One plan member opened: its bundle's metadata beside it.
fn open_member(run_directory: &Path, plan: &PlanMember, groups: &mut BTreeMap<String, Group>) -> Result<(Value, Member)> {
    let store = run_directory.join(&plan.store);
    if !groups.contains_key(&plan.store) {
        groups.insert(plan.store.clone(), Group::read(&store)?);
    }
    let group = &groups[&plan.store];
    let metadata = group
        .metadata(&plan.bundle)
        .ok_or_else(|| invalid(format!("{} carries no bundle {}", plan.store, plan.bundle)))?;
    let parsed = zarr::parse_bundle_metadata(&metadata.to_string())
        .map_err(|error| invalid(format!("{}: {}", plan.bundle, error.0)))?;
    let variable = metadata["variables"]
        .as_array()
        .and_then(|variables| variables.iter().find(|variable| variable["id"] == plan.variable.as_str()));
    let (Some(variable), Some(document)) = (variable, group.array(&plan.variable)) else {
        return Err(invalid(format!("bundle {} of {} has no array {}", plan.bundle, plan.store, plan.variable)));
    };
    let what = format!("{}/{}", plan.store, plan.variable);
    let layout = zarr::parse_array_metadata(&document.to_string())
        .map_err(|error| invalid(format!("{what}: {}", error.0)))?;
    if (layout.frame_count as usize, layout.height, layout.width)
        != (parsed.offsets.len(), parsed.grid.height(), parsed.grid.width())
    {
        return Err(invalid(format!("{what}: the array's shape is not its bundle's")));
    }
    if layout.shard_count != 1 {
        return Err(invalid(format!("{what}: a column store is cut from arrays of one shard")));
    }
    let array = MapArray::open(store.join(zarr::shard_path(&plan.variable, 0)), layout)?;
    Ok((metadata.clone(), Member { bundle: plan.bundle.clone(), variable: variable.clone(), array }))
}

/// Read the stores a plan names and settle the arrays, refusing what a column
/// store cannot hold.
fn resolve_plan(run_directory: &Path, plan: Vec<PlanArray>) -> Result<Vec<ColumnArray>> {
    let mut groups = BTreeMap::new();
    let mut arrays: Vec<ColumnArray> = Vec::with_capacity(plan.len());
    for entry in plan {
        let PlanArray { id, previous, levels, members: planned } = entry;
        let mut first: Option<Value> = None;
        let mut members: Vec<Member> = Vec::with_capacity(planned.len());
        for member in &planned {
            let (metadata, opened) = open_member(run_directory, member, &mut groups)?;
            if metadata["grid"].get("projection").is_some() {
                return Err(invalid(format!("{}: a column store is for regular and Gaussian grids", member.bundle)));
            }
            let reference = arrays.first().map(|array| &array.metadata).or(first.as_ref()).unwrap_or(&metadata);
            if IDENTITY.iter().any(|key| metadata.get(*key) != reference.get(*key)) {
                return Err(invalid(format!(
                    "{}: its metadata is of another run or profile than the store's",
                    member.bundle
                )));
            }
            if metadata["grid"] != reference["grid"] {
                return Err(invalid(format!("{}: its grid is not the column store's", member.bundle)));
            }
            if first.as_ref().is_some_and(|held| held["time"] != metadata["time"]) {
                return Err(invalid(format!("column array {id}: its members do not share one time axis")));
            }
            members.push(opened);
            first.get_or_insert(metadata);
        }
        let metadata = first.ok_or_else(|| invalid(format!("column array {id} has no members")))?;
        let mut fills = Vec::with_capacity(members.len());
        for member in &members {
            fills.push(
                member.variable["quantization"]["nodataCode"]
                    .as_u64()
                    .and_then(|code| u8::try_from(code).ok())
                    .ok_or_else(|| invalid(format!("column array {id}: nodataCode is not a byte")))?,
            );
        }
        if fills.iter().any(|&fill| fill != fills[0]) {
            return Err(invalid(format!("column array {id}: its members do not share one nodataCode")));
        }
        let layout = &members[0].array.layout;
        let geometry = Geometry {
            frame_count: layout.frame_count as usize,
            levels: levels.as_ref().map(Vec::len),
            height: layout.height as usize,
            width: layout.width as usize,
        };
        geometry.check(&id)?;
        arrays.push(ColumnArray {
            id,
            levels,
            metadata,
            // One frame has nothing to be predicted from.
            delta: geometry.frame_count > 1 && previous,
            fill_value: fills[0],
            geometry,
            members,
        });
    }
    Ok(arrays)
}

/// What every member's metadata shares, in the block's order.
const IDENTITY: [&str; 4] = ["model", "product", "runTime", "profile"];

// -- geometry ------------------------------------------------------------------

/// How one array is cut: an inner chunk of the whole axis and every level of
/// one block of cells, a shard of [`SHARD_ROWS`] rows across the grid's width
/// rounded up to whole blocks. `levels` is `None` on a surface array.
#[derive(Debug, Clone, Copy)]
struct Geometry {
    frame_count: usize,
    levels: Option<usize>,
    height: usize,
    width: usize,
}

impl Geometry {
    /// What a reader refuses, refused before anything is cut.
    fn check(&self, id: &str) -> Result<()> {
        let within = |size: Option<usize>| size.is_some_and(|size| size as u64 <= MAX_PLANE_LENGTH);
        if !within(self.inner_length()) {
            return Err(invalid(format!("column array {id}: its inner chunk exceeds the decoded size limit")));
        }
        if !within(Some(zarr::shard_index_length(self.chunks_per_shard()))) {
            return Err(invalid(format!("column array {id}: its shard index exceeds the decoded size limit")));
        }
        Ok(())
    }
    fn level_count(&self) -> usize {
        self.levels.unwrap_or(1)
    }
    /// A shape of the array: the axis, the levels where there are any, then
    /// the rows and columns given.
    fn shape(&self, rows: usize, columns: usize) -> Vec<usize> {
        [Some(self.frame_count), self.levels, Some(rows), Some(columns)].into_iter().flatten().collect()
    }
    fn inner_length(&self) -> Option<usize> {
        self.frame_count.checked_mul(self.level_count())?.checked_mul(BLOCK * BLOCK)
    }
    fn block_rows(&self) -> usize {
        SHARD_ROWS / BLOCK
    }
    fn block_columns(&self) -> usize {
        self.width.div_ceil(BLOCK)
    }
    fn shard_width(&self) -> usize {
        self.block_columns() * BLOCK
    }
    fn chunks_per_shard(&self) -> usize {
        self.block_rows() * self.block_columns()
    }
    fn shard_count(&self) -> usize {
        self.height.div_ceil(SHARD_ROWS)
    }
    /// The grid rows of a shard, fewer than its height on the last.
    fn shard_rows(&self, shard: usize) -> Range<usize> {
        shard * SHARD_ROWS..((shard + 1) * SHARD_ROWS).min(self.height)
    }
    fn shard_path(&self, shard: usize) -> PathBuf {
        // The axis, every level and every column are one shard wide.
        let mut path = PathBuf::from(if self.levels.is_some() { "c/0/0" } else { "c/0" });
        path.push(shard.to_string());
        path.push("0");
        path
    }
}

// -- shards --------------------------------------------------------------------

/// A shard's codes, `[frames, levels, rows, shardWidth]`, the rows padded to
/// whole blocks and every cell past the grid at the fill value.
struct Band {
    geometry: Geometry,
    rows: usize,
    codes: Vec<u8>,
}

impl Band {
    fn new(geometry: Geometry, rows: &Range<usize>, fill: u8) -> Result<Band> {
        let padded = rows.len().next_multiple_of(BLOCK);
        let length = geometry
            .frame_count
            .checked_mul(geometry.level_count())
            .and_then(|planes| planes.checked_mul(padded))
            .and_then(|lines| lines.checked_mul(geometry.shard_width()))
            .ok_or_else(|| invalid("a column shard exceeds what can be addressed"))?;
        Ok(Band { geometry, rows: padded, codes: vec![fill; length] })
    }

    fn offset(&self, frame: usize, level: usize, row: usize, column: usize) -> usize {
        ((frame * self.geometry.level_count() + level) * self.rows + row) * self.geometry.shard_width() + column
    }

    /// One block as the inner chunk stores it: cut whole out of the padded
    /// band, then the `xue.delta` encode along time when asked.
    fn cut(&self, block_row: usize, block_column: usize, delta: bool, chunk: &mut Vec<u8>) {
        let geometry = &self.geometry;
        chunk.clear();
        for frame in 0..geometry.frame_count {
            for level in 0..geometry.level_count() {
                for line in 0..BLOCK {
                    let start = self.offset(frame, level, block_row * BLOCK + line, block_column * BLOCK);
                    chunk.extend_from_slice(&self.codes[start..start + BLOCK]);
                }
            }
        }
        if delta {
            let frame_cells = geometry.level_count() * BLOCK * BLOCK;
            for index in (frame_cells..chunk.len()).rev() {
                chunk[index] = chunk[index].wrapping_sub(chunk[index - frame_cells]);
            }
        }
    }
}

/// One shard: its length and its CRC-32.
struct Shard {
    length: u64,
    crc32: String,
}

/// Cut one shard of one array into `store`. The payloads are written as they
/// are compressed and the index, whose length is known, over the gap left for
/// it. Block rows the grid does not reach are never written.
fn write_shard(array: &ColumnArray, shard: usize, store: &Path) -> Result<Shard> {
    let geometry = array.geometry;
    let rows = geometry.shard_rows(shard);
    let mut band = Band::new(geometry, &rows, array.fill_value)?;
    for (level, member) in array.members.iter().enumerate() {
        member.array.read_rows(&rows, |frame, row, column, cells| {
            let start = band.offset(frame, level, row, column);
            band.codes[start..start + cells.len()].copy_from_slice(cells);
        })?;
    }

    let path = store.join(&array.id).join(geometry.shard_path(shard));
    let fail = |error: std::io::Error| io_error("write", &path, &error);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(fail)?;
    }
    let mut file = BufWriter::new(fs::File::create(&path).map_err(fail)?);
    let index_length = zarr::shard_index_length(geometry.chunks_per_shard());
    let mut index = Vec::with_capacity(index_length);
    let mut cursor = index_length as u64;
    file.seek(SeekFrom::Start(cursor)).map_err(fail)?;
    let mut body = crc32fast::Hasher::new();
    let mut chunk = Vec::new();
    for block_row in 0..band.rows / BLOCK {
        for block_column in 0..geometry.block_columns() {
            band.cut(block_row, block_column, array.delta, &mut chunk);
            let payload = compress_chunk(&chunk, ZSTD_LEVEL)?;
            index.extend_from_slice(&cursor.to_le_bytes());
            index.extend_from_slice(&(payload.len() as u64).to_le_bytes());
            cursor += payload.len() as u64;
            body.update(&payload);
            file.write_all(&payload).map_err(fail)?;
        }
    }
    while index.len() < geometry.chunks_per_shard() * NEVER_WRITTEN.len() {
        index.extend_from_slice(&NEVER_WRITTEN);
    }
    let checksum = zarr::crc32c(&index);
    index.extend_from_slice(&checksum.to_le_bytes());
    zarr::parse_shard_index(&index, geometry.chunks_per_shard())
        .map_err(|error| invalid(format!("{}: shard index is invalid: {}", array.id, error.0)))?;
    file.seek(SeekFrom::Start(0)).map_err(fail)?;
    file.write_all(&index).map_err(fail)?;
    file.flush().map_err(fail)?;
    drop(file);
    let on_disk = fs::metadata(&path).map_err(fail)?.len();
    if on_disk != cursor {
        return Err(invalid(format!("{}: shard is {on_disk} bytes, not {cursor}", array.id)));
    }
    let mut whole = crc32fast::Hasher::new();
    whole.update(&index);
    whole.combine(&body);
    Ok(Shard { length: cursor, crc32: format!("{:08x}", whole.finalize()) })
}

// -- documents -----------------------------------------------------------------

/// The value under `key` where every member's variable has the same one.
fn uniform<'a>(members: &'a [Member], key: &str) -> Option<&'a Value> {
    let first = members.first()?.variable.get(key)?;
    members.iter().all(|member| member.variable.get(key) == Some(first)).then_some(first)
}

fn array_document(array: &ColumnArray, dimensions: &[String]) -> Value {
    let geometry = &array.geometry;
    let mut attributes = Map::new();
    attributes.insert(COLUMN_ATTRIBUTE.into(), json!({"array": array.id}));
    for (name, key) in [("long_name", "label"), ("units", "unit")] {
        if let Some(value) = uniform(&array.members, key) {
            attributes.insert(name.into(), value.clone());
        }
    }
    // CF packing, only where one spelling holds for every level.
    if let Some(codebook) = uniform(&array.members, "quantization").filter(|codebook| codebook["type"] == "linear") {
        attributes.insert("scale_factor".into(), codebook["scale"].clone());
        attributes.insert("add_offset".into(), codebook["offset"].clone());
    }
    attributes.insert("_FillValue".into(), json!(array.fill_value));
    let mut inner = Vec::new();
    if array.delta {
        inner.push(json!({"name": DELTA_CODEC, "configuration": {"axis": 0}}));
    }
    inner.push(json!({"name": "bytes"}));
    inner.push(json!({"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": true}}));
    json!({
        "zarr_format": 3,
        "node_type": "array",
        "shape": geometry.shape(geometry.height, geometry.width),
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {
            "chunk_shape": geometry.shape(SHARD_ROWS, geometry.shard_width()),
        }},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": array.fill_value,
        "codecs": [{
            "name": "sharding_indexed",
            "configuration": {
                "chunk_shape": geometry.shape(BLOCK, BLOCK),
                "codecs": inner,
                "index_codecs": [
                    {"name": "bytes", "configuration": {"endian": "little"}},
                    {"name": "crc32c"},
                ],
                "index_location": "start",
            },
        }],
        "attributes": Value::Object(attributes),
        "dimension_names": dimensions,
    })
}

/// Each array's dimension names: `time` or `time_<array>` by its axis, then on
/// a levelled array `level` or `level_<array>` by its levels.
fn dimensions(arrays: &[ColumnArray]) -> Vec<Vec<String>> {
    let store_time = arrays.first().map(ColumnArray::time);
    let store_levels = arrays.iter().find_map(|array| array.levels.as_ref());
    let named = |name: &str, shared: bool, id: &str| if shared { name.to_owned() } else { format!("{name}_{id}") };
    arrays
        .iter()
        .map(|array| {
            let mut names = vec![named("time", Some(array.time()) == store_time, &array.id)];
            if array.levels.is_some() {
                names.push(named("level", array.levels.as_ref() == store_levels, &array.id));
            }
            names.extend(["latitude".to_owned(), "longitude".to_owned()]);
            names
        })
        .collect()
}

/// An array's document under its name; the bytes written.
fn write_document(store: &Path, name: &str, document: &Value) -> Result<u64> {
    write_file(&store.join(name).join("zarr.json"), dumps_indented(document).as_bytes())
}

/// Every coordinate the arrays name, their documents in the consolidated
/// block's order, and the bytes written.
fn write_store_coordinates(
    arrays: &[ColumnArray],
    names: &[Vec<String>],
    store: &Path,
) -> Result<(Vec<(String, Value)>, u64)> {
    let mut sizes: BTreeMap<String, u64> = BTreeMap::new();
    let mut primary: Vec<(String, Value)> = Vec::new();
    let mut level: Option<(String, Value)> = None;
    let mut own: Vec<(String, Value)> = Vec::new();
    for (position, (array, dimensions)) in arrays.iter().zip(names).enumerate() {
        // The first array's axis is the store's.
        if position == 0 || dimensions[0] != "time" {
            // The grid store's own encoding, which takes the time and the
            // grid's two together; the grid's are the same bytes every time.
            let mut documents = Vec::new();
            let coordinates = [dimensions[0].clone(), "latitude".to_owned(), "longitude".to_owned()];
            let shape = (array.geometry.height, array.geometry.width);
            sizes.extend(write_coordinates(&array.metadata, shape, store, &coordinates, false, &mut documents)?);
            if position == 0 {
                primary = documents;
            } else {
                own.extend(documents.into_iter().take(1));
            }
        }
        let Some(levels) = &array.levels else { continue };
        let name = &dimensions[1];
        if name == "level" && level.is_some() {
            continue;
        }
        let document = coordinate_metadata(
            levels.len(),
            "int32",
            json!(0),
            json!({"standard_name": "air_pressure", "units": "hPa", "positive": "down"}),
            name,
        );
        let values: Vec<u8> = levels.iter().flat_map(|level| level.to_le_bytes()).collect();
        let written = write_document(store, name, &document)?
            + write_file(&store.join(name).join("c").join("0"), &values)?;
        sizes.insert(name.clone(), written);
        if name == "level" {
            level = Some((name.clone(), document));
        } else {
            own.push((name.clone(), document));
        }
    }
    if let Some(level) = level {
        primary.insert(1, level);
    }
    primary.extend(own);
    Ok((primary, sizes.values().sum()))
}

fn group_document(arrays: &[ColumnArray], shards: &[Vec<String>], documents: &[(String, Value)]) -> Value {
    let first = &arrays[0];
    let mut block = Map::new();
    block.insert("version".into(), json!(COLUMN_VERSION));
    for key in IDENTITY {
        block.insert(key.into(), first.metadata[key].clone());
    }
    block.insert("time".into(), first.time().clone());
    block.insert("grid".into(), first.metadata["grid"].clone());
    let entries = arrays
        .iter()
        .zip(shards)
        .map(|(array, shards)| {
            let mut entry = Map::new();
            entry.insert("id".into(), json!(array.id));
            if array.time() != first.time() {
                entry.insert("time".into(), array.time().clone());
            }
            if let Some(levels) = &array.levels {
                entry.insert("levels".into(), json!(levels));
            }
            let members: Vec<Value> = array
                .members
                .iter()
                .map(|member| json!({"bundle": member.bundle, "variable": member.variable}))
                .collect();
            entry.insert("members".into(), Value::Array(members));
            // The group document's CRC-32 is the store's `?v=`, and the codes
            // are nowhere else in it.
            entry.insert("shards".into(), json!(shards));
            Value::Object(entry)
        })
        .collect();
    block.insert("arrays".into(), Value::Array(entries));
    let consolidated: Map<String, Value> = documents.iter().cloned().collect();
    json!({
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {COLUMN_ATTRIBUTE: Value::Object(block)},
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": false,
            "metadata": Value::Object(consolidated),
        },
    })
}

/// A group document as it is written, read back by the decoder's own reader.
fn checked_group(document: &Value) -> Result<String> {
    let text = dumps_indented(document);
    ColumnGroup::parse(&text).map_err(|error| invalid(format!("column store group is invalid: {}", error.0)))?;
    Ok(text)
}

// -- the store -----------------------------------------------------------------

/// What one cut wrote: the manifest descriptor's three values and each
/// array's shard bytes.
#[derive(Debug, Clone)]
pub struct ColumnReport {
    pub path: PathBuf,
    pub byte_length: u64,
    pub crc32: String,
    pub arrays: Vec<(String, u64)>,
}

impl ColumnReport {
    /// The report, keyed as the reference's `write_column_store`.
    pub fn to_value(&self) -> Value {
        let arrays: Map<String, Value> = self.arrays.iter().map(|(id, length)| (id.clone(), json!(length))).collect();
        json!({
            "path": self.path.display().to_string(),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "arrays": Value::Object(arrays),
        })
    }
}

/// Cut the column store a plan describes from the map stores under
/// `run_directory` into `output`, which must not exist, with `jobs` workers.
/// The bytes do not depend on `jobs`.
pub fn write_columns(run_directory: &Path, plan: &str, output: &Path, jobs: usize) -> Result<ColumnReport> {
    if output.exists() {
        return Err(invalid(format!("{} exists", output.display())));
    }
    let arrays = resolve_plan(run_directory, parse_plan(plan)?)?;
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent).map_err(|error| io_error("write", parent, &error))?;
    }
    fs::create_dir(output).map_err(|error| io_error("write", output, &error))?;
    let report = write_store(&arrays, output, jobs);
    if report.is_err() {
        let _ = fs::remove_dir_all(output);
    }
    report
}

fn write_store(arrays: &[ColumnArray], output: &Path, jobs: usize) -> Result<ColumnReport> {
    let names = dimensions(arrays);
    let mut documents = Vec::with_capacity(arrays.len());
    let mut byte_length = 0;
    for (array, names) in arrays.iter().zip(&names) {
        let document = array_document(array, names);
        byte_length += write_document(output, &array.id, &document)?;
        documents.push((array.id.clone(), document));
    }
    let (coordinates, written) = write_store_coordinates(arrays, &names, output)?;
    documents.extend(coordinates);
    byte_length += written;
    // What a reader would refuse is refused here, not after a long cut: only
    // the shards' CRCs are still to come.
    let pending: Vec<Vec<String>> =
        arrays.iter().map(|array| vec!["00000000".to_owned(); array.geometry.shard_count()]).collect();
    checked_group(&group_document(arrays, &pending, &documents))?;

    let tasks: Vec<(usize, usize)> = arrays
        .iter()
        .enumerate()
        .flat_map(|(array, held)| (0..held.geometry.shard_count()).map(move |shard| (array, shard)))
        .collect();
    let written = compression_pool(jobs)?.install(|| {
        tasks
            .par_iter()
            // A shard is the unit: without this a worker would be handed runs of them.
            .with_max_len(1)
            .map(|&(array, shard)| write_shard(&arrays[array], shard, output))
            .collect::<Result<Vec<_>>>()
    })?;
    let mut shards: Vec<Vec<String>> = vec![Vec::new(); arrays.len()];
    let mut sizes = vec![0u64; arrays.len()];
    for (&(array, _), shard) in tasks.iter().zip(written) {
        sizes[array] += shard.length;
        shards[array].push(shard.crc32);
    }
    byte_length += sizes.iter().sum::<u64>();

    let text = checked_group(&group_document(arrays, &shards, &documents))?;
    byte_length += write_file_atomic(&output.join("zarr.json"), text.as_bytes())?;
    Ok(ColumnReport {
        path: output.to_path_buf(),
        byte_length,
        crc32: format!("{:08x}", crc32_plane(text.as_bytes())),
        arrays: arrays.iter().map(|array| array.id.clone()).zip(sizes).collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::column::{parse_column_index, ColumnReader};
    use crate::encode::zarrstore::{self, StoreCodes, StoreVariable};
    use crate::format::Predictor;

    const HEIGHT: usize = 97;
    const WIDTH: usize = 10;
    const FRAMES: usize = 7;
    const NODATA: u8 = 255;

    /// A scratch directory of the test's own, removed when it ends.
    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Scratch {
            let root = std::env::temp_dir().join(format!("xue-columns-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).expect("scratch directory");
            Scratch(root)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn metadata(id: &str, frames: usize, offset: f64) -> Value {
        json!({
            "schemaVersion": 3,
            "model": "TEST",
            "product": "test",
            "runTime": "2026-08-14T06:00:00Z",
            "profile": "quality",
            "time": {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": frames, "frameStep": 1},
            "grid": {"width": WIDTH, "height": HEIGHT, "firstLongitude": 100.0, "firstLatitude": 48.0,
                     "longitudeStep": 0.5, "latitudeStep": -0.5, "wrapLongitude": false},
            "variables": [{
                "numericId": 1, "id": id, "label": "T", "unit": "°C",
                "parameter": {"discipline": 0, "parameterCategory": 0, "parameterNumber": 0,
                              "typeOfFirstFixedSurface": 103, "scaleFactorOfFirstFixedSurface": 0,
                              "scaledValueOfFirstFixedSurface": 2},
                "quantization": {"type": "linear", "offset": offset, "scale": 0.5,
                                 "minimumCode": 0, "maximumCode": 220, "nodataCode": NODATA},
            }],
        })
    }

    fn code(salt: usize, frame: usize, row: usize, column: usize) -> u8 {
        ((salt * 31 + frame * 7 + row * 3 + column * 5) % 220) as u8
    }

    /// A one-bundle map store `<id>.zarr` of `code` over its grid.
    fn write_map(root: &Path, metadata: &Value, salt: usize, delta: bool) {
        let variable = &metadata["variables"][0]["id"];
        let id = variable.as_str().expect("id");
        let count = |node: &Value| node.as_u64().expect("count") as usize;
        let grid = &metadata["grid"];
        let (height, width) = (count(&grid["height"]), count(&grid["width"]));
        let planes: Vec<Vec<u8>> = (0..count(&metadata["time"]["frameCount"]))
            .map(|frame| {
                (0..height * width).map(|cell| code(salt, frame, cell / width, cell % width)).collect()
            })
            .collect();
        let codes = StoreCodes {
            metadata,
            variables: vec![StoreVariable {
                id: id.to_owned(),
                previous: true,
                planes: planes.iter().map(Vec::as_slice).collect(),
            }],
            // 36 rows: the first band ends inside a tile.
            tile: (4, 36),
        };
        let pool = compression_pool(2).expect("pool");
        zarrstore::write_store(&codes, &root.join(format!("{id}.zarr")), delta, None, &pool).expect("map store");
    }

    fn member(id: &str) -> Value {
        json!({"bundle": id, "variable": id, "store": format!("{id}.zarr")})
    }

    fn surface(id: &str, predictor: &str) -> Value {
        json!({"id": id, "predictor": predictor, "members": [member(id)]})
    }

    fn levelled(levels: &[u32], ids: &[&str]) -> Value {
        let members: Vec<Value> = ids.iter().copied().map(member).collect();
        json!({"id": "tmp", "predictor": "previous", "levels": levels, "members": members})
    }

    fn plan(arrays: &[Value]) -> Value {
        json!({"columnsPlan": 1, "arrays": arrays})
    }

    fn files(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
        fn walk(root: &Path, directory: &Path, held: &mut BTreeMap<PathBuf, Vec<u8>>) {
            for entry in fs::read_dir(directory).expect("directory") {
                let path = entry.expect("entry").path();
                if path.is_dir() {
                    walk(root, &path, held);
                } else {
                    let name = path.strip_prefix(root).expect("below the root").to_owned();
                    held.insert(name, fs::read(&path).expect("file"));
                }
            }
        }
        let mut held = BTreeMap::new();
        walk(root, root, &mut held);
        held
    }

    #[test]
    fn a_plan_is_parsed() {
        let arrays = parse_plan(
            &plan(&[levelled(&[850, 500], &["tmp850", "tmp500"]), surface("prate", "raw")]).to_string(),
        )
        .expect("a valid plan");
        assert_eq!(arrays[0].levels.as_deref(), Some([850, 500].as_slice()));
        assert!(arrays[0].previous && !arrays[1].previous);
        assert_eq!(arrays[0].members[1].store, "tmp500.zarr");
        assert_eq!((arrays[1].id.as_str(), &arrays[1].levels), ("prate", &None));
    }

    #[test]
    fn a_malformed_plan_is_refused() {
        let array = || levelled(&[850, 500], &["tmp850", "tmp500"]);
        let with = |key: &str, value: Value| {
            let mut array = array();
            array[key] = value;
            plan(&[array])
        };
        let mut unnamed_store = array();
        unnamed_store["members"][0]["store"] = json!("");
        let mut extra_member_key = array();
        extra_member_key["members"][0]["level"] = json!(850);
        let mut no_predictor = array();
        no_predictor.as_object_mut().unwrap().remove("predictor");
        for (what, document) in [
            ("no version", json!({"arrays": [array()]})),
            ("another version", json!({"columnsPlan": 2, "arrays": [array()]})),
            ("a fractional version", json!({"columnsPlan": 1.0, "arrays": [array()]})),
            ("a boolean version", json!({"columnsPlan": true, "arrays": [array()]})),
            ("an unknown key", json!({"columnsPlan": 1, "arrays": [array()], "block": 8})),
            ("no arrays", plan(&[])),
            ("an unknown array key", with("block", json!(8))),
            ("no predictor", plan(&[no_predictor])),
            ("an unknown predictor", with("predictor", json!("anchor"))),
            ("no members", with("members", json!([]))),
            ("a bad id", with("id", json!("Tmp"))),
            ("a coordinate's id", with("id", json!("level"))),
            ("a repeated array", plan(&[array(), array()])),
            ("a repeated variable", plan(&[array(), surface("tmp850", "raw")])),
            ("ascending levels", with("levels", json!([500, 850]))),
            ("repeated levels", with("levels", json!([850, 850]))),
            ("a level short", with("levels", json!([850]))),
            ("a level of zero", with("levels", json!([850, 0]))),
            ("a fractional level", with("levels", json!([850, 0.5]))),
            ("a level past the coordinate", with("levels", json!([3_000_000_000u64, 500]))),
            ("null levels", with("levels", Value::Null)),
            ("an empty store", plan(&[unnamed_store])),
            ("an unknown member key", plan(&[extra_member_key])),
            ("a surface array under another name", plan(&[json!({
                "id": "t2m", "predictor": "raw", "members": [member("tmp2m")],
            })])),
            ("a surface array of two", plan(&[json!({
                "id": "tmp850", "predictor": "raw", "members": [member("tmp850"), member("tmp500")],
            })])),
        ] {
            assert!(parse_plan(&document.to_string()).is_err(), "{what} was accepted");
        }
        assert!(parse_plan("{").is_err());
        assert!(parse_plan("[1, []]").is_err());
    }

    #[test]
    fn a_band_pads_its_blocks_and_differences_along_time_only() {
        let geometry = Geometry { frame_count: 2, levels: Some(2), height: 3, width: 9 };
        assert_eq!(geometry.shape(SHARD_ROWS, geometry.shard_width()), [2, 2, 96, 16]);
        assert_eq!(geometry.shard_path(0), Path::new("c/0/0/0/0"));
        let mut band = Band::new(geometry, &(0..3), NODATA).expect("band");
        assert_eq!(band.rows, BLOCK);
        for frame in 0..2 {
            for level in 0..2 {
                let start = band.offset(frame, level, 2, 8);
                band.codes[start] = (10 * frame + level) as u8;
            }
        }
        let mut chunk = Vec::new();
        band.cut(0, 1, false, &mut chunk);
        assert_eq!(chunk.len(), 2 * 2 * 64);
        // The block's one column in the grid is its first; everything past
        // it and past the third row is fill.
        let cells: Vec<(usize, u8)> =
            chunk.iter().copied().enumerate().filter(|&(_, code)| code != NODATA).collect();
        assert_eq!(cells, [(16, 0), (64 + 16, 1), (128 + 16, 10), (192 + 16, 11)]);
        band.cut(0, 1, true, &mut chunk);
        // Each level against itself a frame earlier, padding included.
        assert_eq!((chunk[16], chunk[64 + 16]), (0, 1));
        assert_eq!((chunk[128 + 16], chunk[192 + 16]), (10, 10));
        assert!(chunk[..16].iter().all(|&code| code == NODATA));
        assert!(chunk[128..128 + 16].iter().all(|&code| code == 0));
    }

    /// Every cell of every array reads back as the codes the map stores hold,
    /// whatever the map store's predictor, and the bytes are the same at any
    /// number of workers.
    #[test]
    fn a_store_holds_the_map_stores_codes() {
        let scratch = Scratch::new("codes");
        let root = scratch.0.as_path();
        write_map(root, &metadata("tmp850", FRAMES, -70.0), 1, true);
        write_map(root, &metadata("tmp500", FRAMES, -90.0), 2, false);
        write_map(root, &metadata("prate", FRAMES, 0.0), 3, false);
        write_map(root, &metadata("orog", 1, 0.0), 4, false);
        let document = plan(&[
            levelled(&[850, 500], &["tmp850", "tmp500"]),
            surface("prate", "raw"),
            surface("orog", "previous"),
        ])
        .to_string();
        let output = root.join("columns.zarr");
        let report = write_columns(root, &document, &output, 1).expect("a column store");
        let held = files(&output);
        assert_eq!(report.byte_length, held.values().map(|bytes| bytes.len() as u64).sum::<u64>());
        assert_eq!(report.crc32, format!("{:08x}", crc32_plane(&held[Path::new("zarr.json")])));
        let parallel = root.join("parallel.zarr");
        write_columns(root, &document, &parallel, 3).expect("a column store");
        assert!(held == files(&parallel), "the bytes depend on the number of workers");

        let text = String::from_utf8(held[Path::new("zarr.json")].clone()).expect("UTF-8");
        let group: Value = serde_json::from_str(&text).expect("JSON");
        let consolidated = group["consolidated_metadata"]["metadata"].as_object().expect("object");
        assert_eq!(
            consolidated.keys().map(String::as_str).collect::<Vec<_>>(),
            ["tmp", "prate", "orog", "time", "level", "latitude", "longitude", "time_orog"]
        );
        // The two levels' codebooks differ, so the array has no CF packing.
        assert!(consolidated["tmp"]["attributes"].get("scale_factor").is_none());
        assert_eq!(consolidated["prate"]["attributes"]["scale_factor"], 0.5);
        let inner = |id: &str| consolidated[id]["codecs"][0]["configuration"]["codecs"][0]["name"].clone();
        // A RAW array and a one-frame array are stored without the delta.
        assert_eq!([inner("tmp"), inner("prate"), inner("orog")], ["xue.delta", "bytes", "bytes"]);
        assert_eq!(held[Path::new("level/c/0")], [850i32.to_le_bytes(), 500i32.to_le_bytes()].concat());

        let mut reader = ColumnReader::new(ColumnGroup::parse(&text).expect("the group reads"));
        for (id, frames, salts) in
            [("tmp", FRAMES, vec![1, 2]), ("prate", FRAMES, vec![3]), ("orog", 1, vec![4])]
        {
            let array = reader.group().array(id).expect("array").clone();
            assert_eq!(array.layout.shard_count, 2);
            let mut shards = Vec::new();
            for shard in 0..2 {
                let bytes = &held[Path::new(&array.shard_path(shard))];
                let index = &bytes[..array.layout.index_length()];
                reader.set_shard_index(id, shard, index).expect("index");
                let crc32 = format!("{:08x}", crc32_plane(bytes));
                let entries = group["attributes"]["xue_columns"]["arrays"].as_array().expect("arrays");
                let entry = entries.iter().find(|entry| entry["id"] == id).expect("entry");
                assert_eq!(entry["shards"][shard as usize], crc32.as_str());
                // The second band is one row of the grid: one block row
                // written, eleven past the grid never.
                let written = parse_column_index(&array.layout, index).expect("index").iter().flatten().count();
                assert_eq!(written, if shard == 0 { 24 } else { 2 });
                shards.push(bytes);
            }
            for row in 0..HEIGHT {
                for column in 0..WIDTH {
                    let span = reader.chunk_range(id, row as u32, column as u32).expect("range").expect("written");
                    let shard = shards[row / SHARD_ROWS];
                    let payload = &shard[span.offset as usize..(span.offset + span.length) as usize];
                    let expected: Vec<u8> = (0..frames)
                        .flat_map(|frame| salts.iter().map(move |&salt| code(salt, frame, row, column)))
                        .collect();
                    let codes = reader.column(id, row as u32, column as u32, payload).expect("column");
                    assert_eq!(codes, expected, "{id} at {row}, {column}");
                }
            }
        }
    }

    #[test]
    fn a_tile_the_map_store_never_held_is_its_fill_value() {
        let scratch = Scratch::new("absent");
        let root = scratch.0.as_path();
        write_map(root, &metadata("tmp2m", FRAMES, 0.0), 1, false);
        let path = root.join("tmp2m.zarr/tmp2m/c/0/0/0");
        let mut shard = fs::read(&path).expect("shard");
        // 3 tile rows of 3 tiles, two time chunks: drop the first tile.
        let start = shard.len() - zarr::shard_index_length(18);
        shard[start..start + 16].copy_from_slice(&NEVER_WRITTEN);
        let end = shard.len() - 4;
        let checksum = zarr::crc32c(&shard[start..end]);
        shard[end..].copy_from_slice(&checksum.to_le_bytes());
        fs::write(&path, &shard).expect("shard");
        let text = fs::read_to_string(root.join("tmp2m.zarr/zarr.json")).expect("group");
        let group: Value = serde_json::from_str(&text).expect("JSON");
        let document = group["consolidated_metadata"]["metadata"]["tmp2m"].to_string();
        let layout = zarr::parse_array_metadata(&document).expect("layout");
        let array = MapArray::open(path, layout).expect("map array");
        let mut band = vec![0u8; FRAMES * WIDTH];
        array
            .read_rows(&(35..36), |frame, row, column, cells| {
                assert_eq!(row, 0);
                let start = frame * WIDTH + column;
                band[start..start + cells.len()].copy_from_slice(cells);
            })
            .expect("rows");
        for frame in 0..FRAMES {
            for column in 0..WIDTH {
                let expected = if frame < 6 && column < 4 { NODATA } else { code(1, frame, 35, column) };
                assert_eq!(band[frame * WIDTH + column], expected, "frame {frame}, column {column}");
            }
        }
    }

    /// What a store cannot hold is refused, and leaves no directory behind.
    #[test]
    fn what_a_column_store_cannot_hold_is_refused() {
        let scratch = Scratch::new("refused");
        let root = scratch.0.as_path();
        let base = metadata("tmp850", FRAMES, 0.0);
        write_map(root, &base, 1, false);
        write_map(root, &metadata("tmp500", FRAMES, 0.0), 2, false);
        write_map(root, &metadata("tmp300", 3, 0.0), 3, false);
        let mut nodata = metadata("tmp250", FRAMES, 0.0);
        nodata["variables"][0]["quantization"]["nodataCode"] = json!(254);
        write_map(root, &nodata, 4, false);
        let mut grid = metadata("tmp200", FRAMES, 0.0);
        grid["grid"]["firstLongitude"] = json!(101.0);
        write_map(root, &grid, 5, false);
        // The same place in another spelling is another document.
        let mut spelled = metadata("tmp225", FRAMES, 0.0);
        spelled["grid"]["firstLongitude"] = json!(100);
        write_map(root, &spelled, 9, false);
        let mut run = metadata("tmp150", FRAMES, 0.0);
        run["runTime"] = json!("2026-08-14T12:00:00Z");
        write_map(root, &run, 6, false);
        let mut projected = metadata("tmp100", FRAMES, 0.0);
        projected["schemaVersion"] = json!(4);
        projected["grid"] = json!({
            "width": WIDTH, "height": HEIGHT, "wrapLongitude": false,
            "projection": {"kind": "lambert-conformal", "radius": 6371229.0, "standardParallel1": 38.5,
                           "standardParallel2": 38.5, "latitudeOfOrigin": 38.5, "centralMeridian": -97.5,
                           "x0": -1500.0, "y0": 1500.0, "dx": 3000.0, "dy": -3000.0},
        });
        write_map(root, &projected, 7, false);
        write_map(root, &metadata("tmp50", FRAMES, 0.0), 8, false);
        fs::remove_file(root.join("tmp50.zarr/tmp50/c/0/0/0")).expect("shard");

        let output = root.join("columns.zarr");
        let pair = |other: &str| plan(&[levelled(&[850, 500], &["tmp850", other])]);
        let mut elsewhere = pair("tmp500");
        elsewhere["arrays"][0]["members"][1]["store"] = json!("tmp850.zarr");
        for (what, document) in [
            ("two axes in one array", pair("tmp300")),
            ("two nodata codes", pair("tmp250")),
            ("another grid", pair("tmp200")),
            ("a grid spelled otherwise", pair("tmp225")),
            ("another run", pair("tmp150")),
            ("another run in another array", plan(&[surface("tmp850", "raw"), surface("tmp150", "raw")])),
            ("a projected grid", plan(&[surface("tmp100", "raw")])),
            ("a missing shard", pair("tmp50")),
            ("a missing store", pair("tmp70")),
            ("a store without the array", elsewhere),
        ] {
            assert!(write_columns(root, &document.to_string(), &output, 2).is_err(), "{what} was accepted");
            assert!(!output.exists(), "{what} left a directory behind");
        }
        // A payload that does not decode fails in the cut itself.
        let path = root.join("tmp500.zarr/tmp500/c/0/0/0");
        let mut shard = fs::read(&path).expect("shard");
        shard[..8].fill(0);
        fs::write(&path, &shard).expect("shard");
        assert!(write_columns(root, &pair("tmp500").to_string(), &output, 2).is_err());
        assert!(!output.exists(), "a failed cut left its directory");

        let document = plan(&[surface("tmp850", "raw")]).to_string();
        write_columns(root, &document, &output, 1).expect("a column store");
        let held = files(&output);
        assert!(write_columns(root, &document, &output, 1).is_err(), "an existing store was written over");
        assert!(held == files(&output));
    }

    /// Every block of every array, decoded through the shard indices; `None`
    /// for one never written.
    fn decoded(held: &BTreeMap<PathBuf, Vec<u8>>) -> Vec<(String, Vec<Option<Vec<u8>>>)> {
        let text = std::str::from_utf8(&held[Path::new("zarr.json")]).expect("UTF-8");
        let group = ColumnGroup::parse(text).expect("the group reads");
        group
            .arrays
            .iter()
            .map(|array| {
                let layout = &array.layout;
                let predictor = if layout.delta { Predictor::Previous } else { Predictor::Raw };
                let blocks = (0..layout.shard_count)
                    .flat_map(|shard| {
                        let bytes = &held[Path::new(&array.shard_path(shard))];
                        let index = parse_column_index(layout, &bytes[..layout.index_length()]).expect("index");
                        index.into_iter().map(move |entry| {
                            entry.map(|entry| {
                                crate::decode_chunk(
                                    &bytes[entry.offset as usize..][..entry.length as usize],
                                    layout.frame_count,
                                    layout.level_count * layout.block_height,
                                    layout.block_width,
                                    predictor,
                                )
                                .expect("block")
                            })
                        })
                    })
                    .collect();
                (array.id.clone(), blocks)
            })
            .collect()
    }

    /// The group document but for the shards' CRCs, the one thing in it that
    /// the compressed bytes move.
    fn group_without_shards(held: &BTreeMap<PathBuf, Vec<u8>>) -> String {
        let mut group: Value = serde_json::from_slice(&held[Path::new("zarr.json")]).expect("JSON");
        for entry in group["attributes"]["xue_columns"]["arrays"].as_array_mut().expect("arrays") {
            entry["shards"] = Value::Null;
        }
        dumps_indented(&group)
    }

    /// The committed store the reference cut: byte for byte where this libzstd
    /// is the one it was compressed with, and everything but the compressed
    /// payloads, their lengths and their CRCs where it is not.
    #[test]
    fn the_committed_store_is_reproduced() {
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/column");
        let read = |name: &str| fs::read_to_string(fixture.join(name)).expect("fixture");
        let expected: Value = serde_json::from_str(&read("expected.json")).expect("JSON");
        let same_zstd = expected["zstdVersion"] == crate::encode::convert::zstd_version().as_str();
        let reference = files(&fixture.join("columns.zarr"));
        let blocks = decoded(&reference);
        // A data array's chunks; a coordinate's are not compressed.
        let is_shard =
            |name: &Path| blocks.iter().any(|(id, _)| name.starts_with(Path::new(id).join("c")));
        let scratch = Scratch::new("golden");
        for jobs in [1, 4] {
            let output = scratch.0.join(format!("columns.{jobs}.zarr"));
            write_columns(&fixture, &read("plan.json"), &output, jobs).expect("a column store");
            let written = files(&output);
            assert!(written.keys().eq(reference.keys()));
            for (name, bytes) in &reference {
                if same_zstd || !(is_shard(name) || name == Path::new("zarr.json")) {
                    assert!(&written[name] == bytes, "{} differs from the reference's", name.display());
                }
            }
            assert_eq!(group_without_shards(&written), group_without_shards(&reference));
            for ((id, written), (_, reference)) in decoded(&written).iter().zip(&blocks) {
                assert!(written == reference, "{id} holds other codes than the reference's");
            }
        }
    }
}

#[cfg(test)]
mod schema_tie_tests {
    //! The block this writer writes is the one
    //! `docs/schemas/column-group.schema.json` names.
    use super::IDENTITY;
    use crate::schematie::assert_named;

    const SCHEMA: &str = "column-group";

    #[test]
    fn the_block_written_is_the_schemas() {
        assert_named(SCHEMA, "/$defs/Columns", &["version", "time", "grid", "arrays"]);
        assert_named(SCHEMA, "/$defs/Columns", &IDENTITY);
        assert_named(SCHEMA, "/$defs/Array", &["id", "time", "levels", "members", "shards"]);
        assert_named(SCHEMA, "/$defs/Member", &["bundle", "variable"]);
    }
}
