//! Reading the Zarr profile's network store.
//!
//! `docs/zarr-profile.md`, "Network store", is normative. A network store
//! holds one track of a river model — an analysis window or one forecast
//! cycle — as one `uint8` array `streamflow` of shape `[time, feature]`,
//! where feature `f` is row `f` of the GeoParquet beside the store, with an
//! inner chunk of six frames of one GeoParquet row group. Beside it sit the
//! reach ids, the frame times and each reach's baseflow.
//!
//! Like [`crate::zarr`], this module is IO-free: it takes the documents and
//! the shard's bytes (whole, or one fetched range of it with that range's
//! offset) and returns codes and values. The shard, its index and the inner
//! chunk path are the grid store's own; only the dimensions, the chunking and
//! the attributes are this branch's. Every count that sizes an allocation is
//! validated first, and every offset is checked against the bytes given.

use serde_json::{Map, Value};

use crate::format::{err, gcd, DecodeError, Predictor, HOUR_SECONDS, MAX_PLANE_LENGTH};
use crate::zarr::{
    self, decode_value, parse_iso, IndexLocation, Quantization, ShardIndexEntry, StoreKind,
    VariableMetadata,
};

/// The `xue_network` block's version this reader implements.
pub const NETWORK_VERSION: u64 = 1;
/// The data array's name, and the one variable's `id`.
pub const NETWORK_ARRAY: &str = "streamflow";
/// The data array's one shard object, relative to the store root.
pub const NETWORK_SHARD_PATH: &str = "streamflow/c/0/0";
/// Frames per inner chunk, as in a grid store.
pub const NETWORK_TIME_CHUNK: u32 = 6;
/// The side arrays' one chunk each, relative to the store root.
pub const FEATURE_ID_PATH: &str = "feature_id/c/0";
pub const TIME_PATH: &str = "time/c/0";
pub const BASEFLOW_PATH: &str = "baseflow/c/0";

const GEOMETRY_KEYS: [&str; 5] = [
    "path",
    "byteLength",
    "crc32",
    "featureCount",
    "rowGroupRows",
];
const TIME_KEYS: [&str; 5] = [
    "unitSeconds",
    "firstFrameOffset",
    "frameCount",
    "frameStep",
    "frameOffsets",
];

// -- group ---------------------------------------------------------------------

/// The GeoParquet the store's features are the rows of.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NetworkGeometry {
    /// Relative to the directory holding the store.
    pub path: String,
    pub byte_length: u64,
    /// The CRC-32 of the file's bytes, eight lowercase hex digits.
    pub crc32: String,
    pub feature_count: u32,
    pub row_group_rows: u32,
}

/// A network store's group document: its `xue_network` block, validated.
#[derive(Debug, Clone)]
pub struct NetworkGroup {
    pub version: u32,
    pub model: String,
    pub product: String,
    /// The cycle of a forecast, the first frame of an analysis window.
    pub run_time: String,
    /// `run_time` in Unix seconds.
    pub run_time_seconds: i64,
    pub unit_seconds: u32,
    /// The materialized frame-offset axis, in `unit_seconds`, strictly
    /// ascending.
    pub offsets: Vec<u16>,
    pub geometry: NetworkGeometry,
    pub variable: VariableMetadata,
    /// The variable entry verbatim, which the data array's attributes repeat.
    variable_json: Value,
}

impl NetworkGroup {
    pub fn frame_count(&self) -> u32 {
        self.offsets.len() as u32
    }

    /// Every frame's valid time, Unix seconds UTC.
    pub fn frame_times(&self) -> Vec<i64> {
        self.offsets
            .iter()
            .map(|&offset| self.run_time_seconds + i64::from(offset) * i64::from(self.unit_seconds))
            .collect()
    }
}

/// Parse a store root's `zarr.json` as a network store: a Zarr v3 group whose
/// attributes carry `xue_network` and neither of the other branches' blocks,
/// at a version this reader implements.
pub fn parse_network_group(text: &str) -> Result<NetworkGroup, DecodeError> {
    let (kind, attributes) = zarr::group_branch(text)?;
    if kind != StoreKind::Network {
        return Err(err("group is not a network store"));
    }
    let block = attributes
        .get("xue_network")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("xue_network must be an object"))?;
    let version = block
        .get("version")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| err("xue_network version missing"))?;
    if version != NETWORK_VERSION {
        return Err(err(format!(
            "network store version {version} is not implemented"
        )));
    }
    let model = nonempty(block, "model")?;
    let product = nonempty(block, "product")?;
    let run_time = nonempty(block, "runTime")?;
    let run_time_ms = parse_iso(&run_time)
        .ok_or_else(|| err("xue_network runTime is not an ISO 8601 instant"))?;
    if run_time_ms % 1000.0 != 0.0 {
        return Err(err("xue_network runTime must fall on a whole second"));
    }
    let time = block
        .get("time")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("xue_network time missing"))?;
    let (unit_seconds, offsets) = parse_time_block(time)?;
    let geometry = parse_geometry(block.get("geometry"))?;
    let variables = block
        .get("variables")
        .and_then(|v| v.as_array())
        .filter(|list| list.len() == 1)
        .ok_or_else(|| err("xue_network variables must list exactly one variable"))?;
    let variable_json = variables[0].clone();
    let variable = parse_variable(&variable_json)?;
    Ok(NetworkGroup {
        version: version as u32,
        model,
        product,
        run_time,
        run_time_seconds: (run_time_ms / 1000.0) as i64,
        unit_seconds,
        offsets,
        geometry,
        variable,
        variable_json,
    })
}

fn nonempty(block: &Map<String, Value>, key: &str) -> Result<String, DecodeError> {
    block
        .get(key)
        .and_then(|v| v.as_str())
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| err(format!("xue_network {key} must be a non-empty string")))
}

/// The container's time block (`docs/format.md`, "Time Axis"): `unitSeconds`
/// a whole divisor of an hour and the coarsest that fits, exactly one of
/// `frameStep` (uniform) and `frameOffsets` (listed, strictly increasing,
/// starting at `firstFrameOffset`, never uniform), offsets below 65535.
fn parse_time_block(time: &Map<String, Value>) -> Result<(u32, Vec<u16>), DecodeError> {
    if time.keys().any(|key| !TIME_KEYS.contains(&key.as_str())) {
        return Err(err("time block has an unknown field"));
    }
    let frame_count = time
        .get("frameCount")
        .and_then(|v| v.as_u64())
        .filter(|&count| count > 0 && count <= u64::from(u16::MAX))
        .ok_or_else(|| err("time frameCount is invalid"))?;
    let unit_seconds =
        time.get("unitSeconds")
            .and_then(|v| v.as_u64())
            .filter(|&unit| {
                (1..=u64::from(HOUR_SECONDS)).contains(&unit) && u64::from(HOUR_SECONDS) % unit == 0
            })
            .ok_or_else(|| err("time unitSeconds must be a whole divisor of 3600"))? as u32;
    let first = time
        .get("firstFrameOffset")
        .and_then(|v| v.as_u64())
        .filter(|&offset| offset < u64::from(u16::MAX))
        .ok_or_else(|| err("time firstFrameOffset is invalid"))?;
    let offsets = match (time.get("frameStep"), time.get("frameOffsets")) {
        (Some(step), None) => {
            let step = step
                .as_u64()
                .filter(|&step| step > 0 && step <= u64::from(u16::MAX))
                .ok_or_else(|| err("time frameStep is invalid"))?;
            (frame_count - 1)
                .checked_mul(step)
                .and_then(|span| span.checked_add(first))
                .filter(|&last| last < u64::from(u16::MAX))
                .ok_or_else(|| err("time offsets exceed the u16 range"))?;
            (0..frame_count)
                .map(|frame| (first + frame * step) as u16)
                .collect::<Vec<_>>()
        }
        (None, Some(listed)) => {
            let listed = listed
                .as_array()
                .filter(|list| list.len() as u64 == frame_count)
                .ok_or_else(|| err("time frameOffsets must hold frameCount offsets"))?;
            let mut offsets: Vec<u16> = Vec::with_capacity(listed.len());
            for value in listed {
                let offset = value
                    .as_u64()
                    .filter(|&offset| offset < u64::from(u16::MAX))
                    .ok_or_else(|| err("time frameOffsets entry is invalid"))?
                    as u16;
                if offsets.last().is_some_and(|&previous| offset <= previous) {
                    return Err(err("time frameOffsets must be strictly increasing"));
                }
                offsets.push(offset);
            }
            if u64::from(offsets[0]) != first {
                return Err(err("time frameOffsets must begin at firstFrameOffset"));
            }
            let mut steps = offsets.windows(2).map(|pair| pair[1] - pair[0]);
            let first_step = steps.next();
            if steps.all(|step| Some(step) == first_step) {
                return Err(err("a uniform time axis must be encoded as a step"));
            }
            offsets
        }
        _ => {
            return Err(err(
                "time must declare exactly one of frameStep and frameOffsets",
            ))
        }
    };
    let mut divisor = u64::from(HOUR_SECONDS / unit_seconds);
    for &offset in &offsets {
        divisor = gcd(divisor, u64::from(offset));
    }
    if divisor != 1 {
        return Err(err("time unitSeconds is finer than the axis needs"));
    }
    Ok((unit_seconds, offsets))
}

fn parse_geometry(value: Option<&Value>) -> Result<NetworkGeometry, DecodeError> {
    let geometry = value
        .and_then(|v| v.as_object())
        .filter(|object| {
            object.len() == GEOMETRY_KEYS.len()
                && GEOMETRY_KEYS.iter().all(|key| object.contains_key(*key))
        })
        .ok_or_else(|| {
            err("geometry must be {path, byteLength, crc32, featureCount, rowGroupRows}")
        })?;
    let path = geometry
        .get("path")
        .and_then(|v| v.as_str())
        .filter(|path| {
            !path.is_empty()
                && !path.starts_with('/')
                && !path.contains('\\')
                && path
                    .split('/')
                    .all(|part| !part.is_empty() && part != "." && part != "..")
        })
        .ok_or_else(|| err("geometry path must be a relative path"))?;
    let byte_length = geometry
        .get("byteLength")
        .and_then(|v| v.as_u64())
        .filter(|&length| length > 0)
        .ok_or_else(|| err("geometry byteLength must be a positive integer"))?;
    let crc32 = geometry
        .get("crc32")
        .and_then(|v| v.as_str())
        .filter(|crc| crc.len() == 8 && crc.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
        .ok_or_else(|| err("geometry crc32 must be eight lowercase hex digits"))?;
    let feature_count = geometry
        .get("featureCount")
        .and_then(|v| v.as_u64())
        .filter(|&count| count > 0 && count <= u64::from(u32::MAX))
        .ok_or_else(|| err("geometry featureCount must be a positive integer"))?;
    let row_group_rows = geometry
        .get("rowGroupRows")
        .and_then(|v| v.as_u64())
        .filter(|&rows| rows > 0 && rows <= u64::from(u32::MAX))
        .ok_or_else(|| err("geometry rowGroupRows must be a positive integer"))?;
    // A time chunk across every reach is what a frame read decodes, so it is
    // held to the decoder's plane limit before anything is sized from it.
    feature_count
        .div_ceil(row_group_rows)
        .checked_mul(row_group_rows)
        .and_then(|features| features.checked_mul(u64::from(NETWORK_TIME_CHUNK)))
        .filter(|&cells| cells <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("network exceeds the decoded size limit"))?;
    Ok(NetworkGeometry {
        path: path.to_owned(),
        byte_length,
        crc32: crc32.to_owned(),
        feature_count: feature_count as u32,
        row_group_rows: row_group_rows as u32,
    })
}

/// The one variable: `streamflow`, `numericId` 1, a `log1p` codebook whose
/// reserved codes are distinct and whose value codes run between them.
fn parse_variable(value: &Value) -> Result<VariableMetadata, DecodeError> {
    let id = value
        .get("id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| err("variable id is invalid"))?;
    if id != NETWORK_ARRAY {
        return Err(err(format!("the network variable must be {NETWORK_ARRAY}")));
    }
    if value.get("numericId").and_then(|v| v.as_u64()) != Some(1) {
        return Err(err("the network variable's numericId must be 1"));
    }
    let quantization = value
        .get("quantization")
        .ok_or_else(|| err("variable quantization missing"))?;
    if quantization.get("type").and_then(|v| v.as_str()) != Some("log1p") {
        return Err(err("the network variable must carry a log1p codebook"));
    }
    let quantization = zarr::parse_quantization(quantization)?;
    let Quantization::Log1p {
        trace,
        scale,
        maximum,
        minimum_code,
        maximum_code,
        zero_code,
        overflow_code,
        nodata_code,
    } = quantization
    else {
        return Err(err("the network variable must carry a log1p codebook"));
    };
    let byte = |code: f64| code.fract() == 0.0 && (0.0..=255.0).contains(&code);
    let value_codes = minimum_code..=maximum_code;
    let reserved = [zero_code, overflow_code, nodata_code];
    let valid = trace > 0.0
        && scale > 0.0
        && maximum > trace
        && [minimum_code, maximum_code]
            .into_iter()
            .chain(reserved)
            .all(byte)
        && minimum_code < maximum_code
        && reserved.iter().all(|code| !value_codes.contains(code))
        && zero_code != overflow_code
        && zero_code != nodata_code
        && overflow_code != nodata_code;
    if !valid {
        return Err(err("the network variable's log1p codebook is malformed"));
    }
    Ok(VariableMetadata {
        numeric_id: 1,
        id: id.to_owned(),
        label: value
            .get("label")
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        unit: value
            .get("unit")
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        quantization,
    })
}

// -- data array ----------------------------------------------------------------

/// How the `streamflow` array is cut, held to the group: `[frameCount,
/// featureCount]` in inner chunks of `[6, rowGroupRows]`, one shard of the
/// whole array rounded up to whole inner chunks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NetworkLayout {
    pub frame_count: u32,
    pub feature_count: u32,
    pub time_chunk: u32,
    pub row_group_rows: u32,
    pub time_chunks: u32,
    pub feature_chunks: u32,
    /// Inner chunks in the shard, and pairs in its index.
    pub chunk_count: u32,
    pub delta: bool,
    pub index_location: IndexLocation,
    pub fill_value: u8,
}

impl NetworkLayout {
    /// The shard index's byte length: one pair per inner chunk plus the
    /// CRC-32C.
    pub fn index_length(&self) -> usize {
        zarr::shard_index_length(self.chunk_count as usize)
    }

    /// Bytes of one decoded inner chunk.
    pub fn chunk_cells(&self) -> usize {
        self.time_chunk as usize * self.row_group_rows as usize
    }

    /// The frames of time chunk `t` that lie on the axis.
    pub fn frames_in_chunk(&self, time_chunk: u32) -> u32 {
        self.time_chunk.min(
            self.frame_count
                .saturating_sub(time_chunk * self.time_chunk),
        )
    }

    /// The reaches of feature chunk `g` that lie inside the network.
    pub fn features_in_chunk(&self, feature_chunk: u32) -> u32 {
        self.row_group_rows.min(
            self.feature_count
                .saturating_sub(feature_chunk * self.row_group_rows),
        )
    }

    /// The index position of inner chunk `[time_chunk, feature_chunk]`.
    pub fn position(&self, time_chunk: u32, feature_chunk: u32) -> usize {
        time_chunk as usize * self.feature_chunks as usize + feature_chunk as usize
    }

    fn predictor(&self) -> Predictor {
        if self.delta {
            Predictor::Previous
        } else {
            Predictor::Raw
        }
    }
}

/// Parse and validate `streamflow/zarr.json` against its group.
pub fn parse_network_array(text: &str, group: &NetworkGroup) -> Result<NetworkLayout, DecodeError> {
    let array = zarr::parse_sharded_array(text, 2)?;
    let frame_count = group.frame_count();
    let NetworkGeometry {
        feature_count,
        row_group_rows,
        ..
    } = group.geometry;
    if array.shape != [u64::from(frame_count), u64::from(feature_count)] {
        return Err(err("streamflow shape must be [frameCount, featureCount]"));
    }
    let dimensions = array
        .value
        .get("dimension_names")
        .and_then(|v| v.as_array());
    if dimensions.map(|names| names.iter().map(|n| n.as_str()).collect::<Vec<_>>())
        != Some(vec![Some("time"), Some("feature")])
    {
        return Err(err("streamflow dimension_names must be [time, feature]"));
    }
    if array.inner_shape != [u64::from(NETWORK_TIME_CHUNK), u64::from(row_group_rows)] {
        return Err(err("streamflow inner chunk must be [6, rowGroupRows]"));
    }
    let time_chunks = frame_count.div_ceil(NETWORK_TIME_CHUNK);
    let feature_chunks = feature_count.div_ceil(row_group_rows);
    let outer = [
        u64::from(time_chunks) * u64::from(NETWORK_TIME_CHUNK),
        u64::from(feature_chunks) * u64::from(row_group_rows),
    ];
    if array.shard_shape != outer {
        return Err(err(
            "streamflow shard must be the whole array in whole inner chunks",
        ));
    }
    let Quantization::Log1p { nodata_code, .. } = group.variable.quantization else {
        return Err(err("the network variable must carry a log1p codebook"));
    };
    if f64::from(array.fill_value) != nodata_code {
        return Err(err(
            "streamflow fill_value must be the codebook's nodataCode",
        ));
    }
    let repeated = array
        .value
        .get("attributes")
        .and_then(|v| v.get("xue_network"))
        .and_then(|v| v.get("variable"));
    if repeated != Some(&group.variable_json) {
        return Err(err(
            "streamflow attributes must repeat the group's variable",
        ));
    }
    let chunk_count = u64::from(time_chunks)
        .checked_mul(u64::from(feature_chunks))
        .filter(|&count| count.saturating_mul(16) <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("streamflow shard index exceeds the size limit"))?;
    Ok(NetworkLayout {
        frame_count,
        feature_count,
        time_chunk: NETWORK_TIME_CHUNK,
        row_group_rows,
        time_chunks,
        feature_chunks,
        chunk_count: chunk_count as u32,
        delta: array.delta,
        index_location: array.index_location,
        fill_value: array.fill_value,
    })
}

// -- shard ---------------------------------------------------------------------

/// The index's span within a shard of `shard_length` bytes, `[start, end)`.
pub fn index_span(layout: &NetworkLayout, shard_length: u64) -> Result<(u64, u64), DecodeError> {
    let length = layout.index_length() as u64;
    if shard_length < length {
        return Err(err("shard is shorter than its index"));
    }
    Ok(match layout.index_location {
        IndexLocation::Start => (0, length),
        IndexLocation::End => (shard_length - length, shard_length),
    })
}

/// Read the shard index out of the whole shard, CRC-32C verified, every
/// written chunk lying inside the shard and outside the index.
pub fn read_network_index(
    layout: &NetworkLayout,
    shard: &[u8],
) -> Result<Vec<Option<ShardIndexEntry>>, DecodeError> {
    let (start, end) = index_span(layout, shard.len() as u64)?;
    let index = parse_network_index(layout, &shard[start as usize..end as usize])?;
    for entry in index.iter().flatten() {
        let chunk_end = entry
            .offset
            .checked_add(entry.length)
            .ok_or_else(|| err("shard index entry overflows"))?;
        if chunk_end > shard.len() as u64 || (entry.offset < end && chunk_end > start) {
            return Err(err("shard index names bytes outside the shard's data"));
        }
    }
    Ok(index)
}

/// Parse the index bytes alone (a suffix range, or the shard's head), CRC-32C
/// verified. Spans are checked against the bytes when chunks are read.
pub fn parse_network_index(
    layout: &NetworkLayout,
    bytes: &[u8],
) -> Result<Vec<Option<ShardIndexEntry>>, DecodeError> {
    zarr::parse_shard_index(bytes, layout.chunk_count as usize)
}

/// The byte span `[start, end)` of the shard holding every written chunk of
/// one frame's time chunk — one range per frame — or `None` when none of them
/// was written.
pub fn frame_range(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
    frame: u32,
) -> Result<Option<(u64, u64)>, DecodeError> {
    check_index(layout, index)?;
    if frame >= layout.frame_count {
        return Err(err("frame is outside the axis"));
    }
    let time_chunk = frame / layout.time_chunk;
    let mut span: Option<(u64, u64)> = None;
    for group in 0..layout.feature_chunks {
        if let Some(entry) = index[layout.position(time_chunk, group)] {
            let end = entry
                .offset
                .checked_add(entry.length)
                .ok_or_else(|| err("shard index entry overflows"))?;
            span = Some(match span {
                None => (entry.offset, end),
                Some((lo, hi)) => (lo.min(entry.offset), hi.max(end)),
            });
        }
    }
    Ok(span)
}

/// The chunk spans one reach's series reads: one per time chunk, `None` for a
/// chunk the shard never held.
pub fn series_ranges(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
    feature: u32,
) -> Result<Vec<Option<ShardIndexEntry>>, DecodeError> {
    check_index(layout, index)?;
    if feature >= layout.feature_count {
        return Err(err("feature is outside the network"));
    }
    let group = feature / layout.row_group_rows;
    Ok((0..layout.time_chunks)
        .map(|chunk| index[layout.position(chunk, group)])
        .collect())
}

fn check_index(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
) -> Result<(), DecodeError> {
    if index.len() != layout.chunk_count as usize {
        return Err(err("shard index does not match the array's chunk grid"));
    }
    Ok(())
}

/// One inner chunk's compressed bytes out of `data`, which holds the shard's
/// bytes from `data_offset` on (the whole shard at 0, or one fetched range).
fn chunk_bytes(
    data: &[u8],
    data_offset: u64,
    entry: ShardIndexEntry,
) -> Result<&[u8], DecodeError> {
    let start = entry
        .offset
        .checked_sub(data_offset)
        .ok_or_else(|| err("chunk lies before the bytes given"))?;
    let end = start
        .checked_add(entry.length)
        .filter(|&end| end <= data.len() as u64)
        .ok_or_else(|| err("chunk lies past the bytes given"))?;
    Ok(&data[start as usize..end as usize])
}

/// Decode one inner chunk to its `6 × rowGroupRows` codes, frame-major, the
/// delta replayed along the frame axis. A never-written chunk is all fill.
pub fn decode_network_chunk(
    layout: &NetworkLayout,
    entry: Option<ShardIndexEntry>,
    data: &[u8],
    data_offset: u64,
) -> Result<Vec<u8>, DecodeError> {
    match entry {
        None => Ok(vec![layout.fill_value; layout.chunk_cells()]),
        Some(entry) => crate::decode_chunk(
            chunk_bytes(data, data_offset, entry)?,
            layout.time_chunk,
            1,
            layout.row_group_rows,
            layout.predictor(),
        ),
    }
}

/// Every frame of one time chunk that lies on the axis, `frames × featureCount`
/// codes, frame-major, padding trimmed: every row group of the chunk decoded
/// once.
pub fn decode_time_chunk(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
    time_chunk: u32,
    data: &[u8],
    data_offset: u64,
) -> Result<Vec<u8>, DecodeError> {
    check_index(layout, index)?;
    if time_chunk >= layout.time_chunks {
        return Err(err("time chunk is outside the axis"));
    }
    let frames = layout.frames_in_chunk(time_chunk) as usize;
    let features = layout.feature_count as usize;
    let rows = layout.row_group_rows as usize;
    let mut out = vec![layout.fill_value; frames * features];
    for group in 0..layout.feature_chunks {
        let chunk = decode_network_chunk(
            layout,
            index[layout.position(time_chunk, group)],
            data,
            data_offset,
        )?;
        let first = group as usize * rows;
        let width = layout.features_in_chunk(group) as usize;
        for frame in 0..frames {
            out[frame * features + first..][..width]
                .copy_from_slice(&chunk[frame * rows..][..width]);
        }
    }
    Ok(out)
}

/// One frame's codes, one per reach in GeoParquet row order.
pub fn decode_network_frame(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
    frame: u32,
    data: &[u8],
    data_offset: u64,
) -> Result<Vec<u8>, DecodeError> {
    if frame >= layout.frame_count {
        return Err(err("frame is outside the axis"));
    }
    let time_chunk = frame / layout.time_chunk;
    let within = (frame % layout.time_chunk) as usize;
    let features = layout.feature_count as usize;
    let codes = decode_time_chunk(layout, index, time_chunk, data, data_offset)?;
    Ok(codes[within * features..][..features].to_vec())
}

/// One reach's codes over the whole axis: one inner chunk per time chunk.
pub fn decode_network_series(
    layout: &NetworkLayout,
    index: &[Option<ShardIndexEntry>],
    feature: u32,
    data: &[u8],
    data_offset: u64,
) -> Result<Vec<u8>, DecodeError> {
    let entries = series_ranges(layout, index, feature)?;
    let column = (feature % layout.row_group_rows) as usize;
    let rows = layout.row_group_rows as usize;
    let mut out = Vec::with_capacity(layout.frame_count as usize);
    for (chunk, entry) in entries.into_iter().enumerate() {
        let codes = decode_network_chunk(layout, entry, data, data_offset)?;
        for frame in 0..layout.frames_in_chunk(chunk as u32) as usize {
            out.push(codes[frame * rows + column]);
        }
    }
    Ok(out)
}

/// Codes → m³/s through the variable's codebook; `NaN` for no value. Code 0
/// is no flow, and the overflow code reads as a flow above the codebook's
/// maximum.
pub fn dequantize(quantization: &Quantization, codes: &[u8]) -> Vec<f64> {
    codes
        .iter()
        .map(|&code| decode_value(quantization, code).unwrap_or(f64::NAN))
        .collect()
}

// -- side arrays ---------------------------------------------------------------

/// A side array's `zarr.json`: one dimension of `length`, one chunk, the
/// `bytes` codec little-endian, the named dtype and dimension.
pub fn parse_side_array(
    text: &str,
    data_type: &str,
    dimension: &str,
    length: u32,
) -> Result<(), DecodeError> {
    let value: Value =
        serde_json::from_str(text).map_err(|_| err("side array metadata is not valid JSON"))?;
    let length = Value::from(vec![length]);
    let ok = value.get("zarr_format").and_then(|v| v.as_u64()) == Some(3)
        && value.get("node_type").and_then(|v| v.as_str()) == Some("array")
        && value.get("data_type").and_then(|v| v.as_str()) == Some(data_type)
        && value.get("shape") == Some(&length)
        && value
            .get("chunk_grid")
            .and_then(|v| v.get("name"))
            .and_then(|v| v.as_str())
            == Some("regular")
        && value
            .get("chunk_grid")
            .and_then(|v| v.get("configuration"))
            .and_then(|v| v.get("chunk_shape"))
            == Some(&length)
        && value.get("dimension_names") == Some(&Value::from(vec![dimension]))
        && value
            .get("codecs")
            .and_then(|v| v.as_array())
            .is_some_and(|codecs| {
                codecs.len() == 1
                    && codecs[0].get("name").and_then(|v| v.as_str()) == Some("bytes")
                    && codecs[0]
                        .get("configuration")
                        .and_then(|v| v.get("endian"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("little")
                        == "little"
            });
    if ok {
        Ok(())
    } else {
        Err(err(format!(
            "side array must be one {data_type} chunk of [{dimension}] under [bytes]"
        )))
    }
}

fn exact_length(bytes: &[u8], count: u32, width: usize, what: &str) -> Result<(), DecodeError> {
    if bytes.len() as u64 != u64::from(count) * width as u64 {
        return Err(err(format!(
            "{what} holds {} bytes, not {count} values",
            bytes.len()
        )));
    }
    Ok(())
}

/// `feature_id/c/0`: each reach's identifier in its source, in row order.
pub fn read_feature_ids(layout: &NetworkLayout, bytes: &[u8]) -> Result<Vec<i64>, DecodeError> {
    exact_length(bytes, layout.feature_count, 8, "feature_id")?;
    Ok(bytes
        .chunks_exact(8)
        .map(|value| i64::from_le_bytes(value.try_into().unwrap()))
        .collect())
}

/// `time/c/0`: each frame's valid time in Unix seconds, which must be the
/// time the group's block gives it.
pub fn read_frame_times(group: &NetworkGroup, bytes: &[u8]) -> Result<Vec<i64>, DecodeError> {
    exact_length(bytes, group.frame_count(), 8, "time")?;
    let times: Vec<i64> = bytes
        .chunks_exact(8)
        .map(|value| i64::from_le_bytes(value.try_into().unwrap()))
        .collect();
    if times != group.frame_times() {
        return Err(err("time array disagrees with the group's time block"));
    }
    Ok(times)
}

/// `baseflow/c/0`: each reach's usual flow before the event, m³/s; `NaN`
/// where unknown.
pub fn read_baseflow(layout: &NetworkLayout, bytes: &[u8]) -> Result<Vec<f32>, DecodeError> {
    exact_length(bytes, layout.feature_count, 4, "baseflow")?;
    Ok(bytes
        .chunks_exact(4)
        .map(|value| f32::from_le_bytes(value.try_into().unwrap()))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn group_document(edit: impl FnOnce(&mut Value)) -> String {
        let mut document = json!({
            "zarr_format": 3, "node_type": "group",
            "attributes": {"xue_network": {
                "version": 1, "model": "NWM v3.0", "product": "short_range",
                "runTime": "2025-07-04T00:00:00Z",
                "time": {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 8, "frameStep": 1},
                "geometry": {"path": "reaches.parquet", "byteLength": 35, "crc32": "0123abcd",
                             "featureCount": 20, "rowGroupRows": 8},
                "variables": [{"numericId": 1, "id": "streamflow", "label": "Streamflow", "unit": "m3/s",
                               "quantization": {"type": "log1p", "trace": 0.01, "scale": 1.0, "maximum": 100000.0,
                                                "minimumCode": 1, "maximumCode": 253, "zeroCode": 0,
                                                "overflowCode": 254, "nodataCode": 255}}]
            }}
        });
        edit(&mut document);
        document.to_string()
    }

    fn array_document(edit: impl FnOnce(&mut Value)) -> String {
        let group: Value = serde_json::from_str(&group_document(|_| {})).unwrap();
        let variable = group["attributes"]["xue_network"]["variables"][0].clone();
        let mut document = json!({
            "zarr_format": 3, "node_type": "array", "shape": [8, 20], "data_type": "uint8",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [12, 24]}},
            "chunk_key_encoding": {"name": "default"}, "fill_value": 255,
            "codecs": [{"name": "sharding_indexed", "configuration": {
                "chunk_shape": [6, 8],
                "codecs": [{"name": "xue.delta", "configuration": {"axis": 0}}, {"name": "bytes"},
                           {"name": "zstd", "configuration": {"level": 15, "checksum": true}}],
                "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}],
                "index_location": "end"}}],
            "attributes": {"xue_network": {"variable": variable}, "_FillValue": 255},
            "dimension_names": ["time", "feature"]
        });
        edit(&mut document);
        document.to_string()
    }

    #[test]
    fn reads_the_block_and_the_layout() {
        let group = parse_network_group(&group_document(|_| {})).unwrap();
        assert_eq!(group.model, "NWM v3.0");
        assert_eq!(group.offsets, (1..=8).collect::<Vec<u16>>());
        assert_eq!(group.frame_times()[0], 1_751_590_800);
        assert_eq!(group.geometry.row_group_rows, 8);
        let layout = parse_network_array(&array_document(|_| {}), &group).unwrap();
        assert_eq!(layout.time_chunks, 2);
        assert_eq!(layout.feature_chunks, 3);
        assert_eq!(layout.chunk_count, 6);
        assert_eq!(layout.frames_in_chunk(1), 2);
        assert_eq!(layout.features_in_chunk(2), 4);
        assert!(layout.delta);
        assert_eq!(layout.index_length(), 100);
    }

    #[test]
    fn tells_the_branches_apart() {
        assert_eq!(
            zarr::store_kind(&group_document(|_| {})).unwrap(),
            StoreKind::Network
        );
        assert!(zarr::parse_group_metadata(&group_document(|_| {})).is_err());
        for other in ["xue", "xue_polar"] {
            let text = group_document(|d| d["attributes"][other] = json!({"version": 1}));
            assert!(zarr::store_kind(&text).is_err());
            assert!(parse_network_group(&text).is_err());
        }
        let polar = json!({"zarr_format": 3, "node_type": "group",
                           "attributes": {"xue_polar": {"version": 1}}});
        assert_eq!(
            zarr::store_kind(&polar.to_string()).unwrap(),
            StoreKind::Polar
        );
        assert!(parse_network_group(&polar.to_string()).is_err());
    }

    #[test]
    fn rejects_malformed_groups() {
        let edits: Vec<fn(&mut Value)> = vec![
            |d| d["attributes"]["xue_network"]["version"] = json!(2),
            |d| d["attributes"]["xue_network"]["model"] = json!(""),
            |d| d["attributes"]["xue_network"]["runTime"] = json!("yesterday"),
            |d| {
                d["attributes"]["xue_network"]["time"]["frameOffsets"] =
                    json!([1, 2, 3, 4, 5, 6, 7, 8]);
            },
            |d| d["attributes"]["xue_network"]["time"]["unitSeconds"] = json!(7),
            |d| {
                // Offsets 2, 4, … in half hours are whole hours: the unit is finer than it needs.
                let time = &mut d["attributes"]["xue_network"]["time"];
                time["unitSeconds"] = json!(1800);
                time["firstFrameOffset"] = json!(2);
                time["frameStep"] = json!(2);
            },
            |d| d["attributes"]["xue_network"]["time"]["frameCount"] = json!(0),
            |d| d["attributes"]["xue_network"]["time"]["hours"] = json!([1]),
            |d| {
                let time = &mut d["attributes"]["xue_network"]["time"];
                time.as_object_mut().unwrap().remove("frameStep");
                time["frameOffsets"] = json!([1, 2, 3, 4, 5, 6, 7, 8]);
            },
            |d| {
                let time = &mut d["attributes"]["xue_network"]["time"];
                time.as_object_mut().unwrap().remove("frameStep");
                time["frameOffsets"] = json!([1, 2, 2, 4, 5, 6, 7, 9]);
            },
            |d| d["attributes"]["xue_network"]["geometry"]["extra"] = json!(1),
            |d| d["attributes"]["xue_network"]["geometry"]["path"] = json!("../reaches.parquet"),
            |d| d["attributes"]["xue_network"]["geometry"]["crc32"] = json!("XYZ"),
            |d| d["attributes"]["xue_network"]["geometry"]["featureCount"] = json!(0),
            |d| d["attributes"]["xue_network"]["geometry"]["rowGroupRows"] = json!(0),
            |d| d["attributes"]["xue_network"]["geometry"]["featureCount"] = json!(1u64 << 40),
            |d| d["attributes"]["xue_network"]["variables"] = json!([]),
            |d| d["attributes"]["xue_network"]["variables"][0]["id"] = json!("discharge"),
            |d| {
                d["attributes"]["xue_network"]["variables"][0]["quantization"]["type"] =
                    json!("linear")
            },
            |d| {
                d["attributes"]["xue_network"]["variables"][0]["quantization"]["nodataCode"] =
                    json!(100)
            },
        ];
        for (position, edit) in edits.into_iter().enumerate() {
            assert!(
                parse_network_group(&group_document(edit)).is_err(),
                "edit {position} was accepted"
            );
        }
        let listed = group_document(|d| {
            let time = &mut d["attributes"]["xue_network"]["time"];
            time.as_object_mut().unwrap().remove("frameStep");
            time["frameOffsets"] = json!([1, 2, 3, 4, 5, 6, 7, 9]);
        });
        assert_eq!(parse_network_group(&listed).unwrap().offsets[7], 9);
    }

    #[test]
    fn rejects_malformed_arrays() {
        let group = parse_network_group(&group_document(|_| {})).unwrap();
        let edits: Vec<fn(&mut Value)> = vec![
            |d| d["data_type"] = json!("int16"),
            |d| d["shape"] = json!([8, 21]),
            |d| d["shape"] = json!([8, 20, 1]),
            |d| d["dimension_names"] = json!(["feature", "time"]),
            |d| {
                d.as_object_mut().unwrap().remove("dimension_names");
            },
            |d| d["codecs"] = json!([{"name": "bytes"}, {"name": "zstd"}]),
            |d| d["codecs"][0]["configuration"]["chunk_shape"] = json!([6, 16]),
            |d| d["codecs"][0]["configuration"]["chunk_shape"] = json!([3, 8]),
            |d| d["chunk_grid"]["configuration"]["chunk_shape"] = json!([8, 20]),
            |d| d["chunk_grid"]["configuration"]["chunk_shape"] = json!([6, 24]),
            |d| d["codecs"][0]["configuration"]["index_codecs"] = json!([{"name": "bytes"}]),
            |d| {
                d["codecs"][0]["configuration"]["index_codecs"][0]["configuration"]["endian"] =
                    json!("big");
            },
            |d| d["codecs"][0]["configuration"]["index_location"] = json!("middle"),
            |d| d["fill_value"] = json!(0),
            |d| {
                d["codecs"][0]["configuration"]["codecs"] = json!([{"name": "xue.delta", "configuration": {"axis": 1}}, {"name": "bytes"}, {"name": "zstd"}]);
            },
            |d| {
                d["codecs"][0]["configuration"]["codecs"] =
                    json!([{"name": "bytes"}, {"name": "gzip"}])
            },
            |d| d["attributes"] = json!({}),
        ];
        for (position, edit) in edits.into_iter().enumerate() {
            assert!(
                parse_network_array(&array_document(edit), &group).is_err(),
                "edit {position} was accepted"
            );
        }
        let plain = parse_network_array(
            &array_document(|d| {
                d["codecs"][0]["configuration"]["codecs"] =
                    json!([{"name": "bytes"}, {"name": "zstd"}]);
                d["codecs"][0]["configuration"]["index_location"] = json!("start");
            }),
            &group,
        )
        .unwrap();
        assert!(!plain.delta);
        assert_eq!(plain.index_location, IndexLocation::Start);
    }

    #[test]
    fn side_arrays_are_held_to_their_length() {
        let group = parse_network_group(&group_document(|_| {})).unwrap();
        let layout = parse_network_array(&array_document(|_| {}), &group).unwrap();
        assert!(read_feature_ids(&layout, &[0u8; 159]).is_err());
        assert!(read_baseflow(&layout, &[0u8; 80]).is_ok());
        let mut times = Vec::new();
        for time in group.frame_times() {
            times.extend_from_slice(&time.to_le_bytes());
        }
        assert_eq!(read_frame_times(&group, &times).unwrap().len(), 8);
        times[0] ^= 1;
        assert!(read_frame_times(&group, &times).is_err());
        let side = json!({"zarr_format": 3, "node_type": "array", "shape": [20], "data_type": "int64",
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [20]}},
            "chunk_key_encoding": {"name": "default"}, "fill_value": 0,
            "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
            "attributes": {}, "dimension_names": ["feature"]});
        assert!(parse_side_array(&side.to_string(), "int64", "feature", 20).is_ok());
        assert!(parse_side_array(&side.to_string(), "int64", "feature", 21).is_err());
        assert!(parse_side_array(&side.to_string(), "float32", "feature", 20).is_err());
    }
}
