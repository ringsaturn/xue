//! Xue v1 bundle parser and frame decoder.
//!
//! The binary contract is defined in `docs/format.md` and mirrored by the
//! Python reference implementation in `xue/binformat.py`. Every
//! integer computation on untrusted input uses checked arithmetic, and no
//! allocation is sized from a file value before it is validated against the
//! metadata grid and the file length.
//!
//! Two readers share the same structural validation and decode logic:
//! [`Bundle`] opens a complete file, while [`StreamingBundle`] opens only the
//! structural prefix (header + metadata + index) and accepts payload bytes
//! incrementally as HTTP range responses arrive.

use std::collections::HashMap;
use std::fmt;
use std::io::Read;

pub const MAGIC: &[u8; 8] = b"XUE\0\0\0\0\0";
pub const VERSION: u16 = 1;
pub const HEADER_SIZE: usize = 80;
pub const INDEX_MAGIC: &[u8; 4] = b"IDX1";
pub const INDEX_HEADER_SIZE: usize = 16;
pub const ENTRY_SIZE: usize = 40;
pub const NO_DEPENDENCY: u16 = 0xFFFF;
pub const FLAG_ZSTD_CHECKSUM: u8 = 0x01;
/// Safety limit for one decoded plane: 64M points.
pub const MAX_PLANE_LENGTH: u64 = 64 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

impl fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for DecodeError {}

fn err(message: impl Into<String>) -> DecodeError {
    DecodeError(message.into())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Predictor {
    Raw = 0,
    Anchor = 1,
    Previous = 2,
    Zero = 3,
}

impl Predictor {
    fn parse(value: u8) -> Result<Self, DecodeError> {
        match value {
            0 => Ok(Self::Raw),
            1 => Ok(Self::Anchor),
            2 => Ok(Self::Previous),
            3 => Ok(Self::Zero),
            other => Err(err(format!("unknown predictor {other}"))),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Compression {
    None = 0,
    Zstd = 1,
    ZstdDict = 2,
}

impl Compression {
    fn parse(value: u8) -> Result<Self, DecodeError> {
        match value {
            0 => Ok(Self::None),
            1 => Ok(Self::Zstd),
            2 => Ok(Self::ZstdDict),
            other => Err(err(format!("unknown compression {other}"))),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct PlaneEntry {
    pub variable_id: u8,
    pub predictor: Predictor,
    pub compression: Compression,
    pub flags: u8,
    pub forecast_hour: u16,
    pub dependency_hour: u16,
    pub group_id: u16,
    pub compressed_length: u32,
    pub data_offset: u64,
    pub decoded_length: u32,
    pub crc32: u32,
    pub minimum_code: u8,
    pub maximum_code: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FrameRequest {
    pub variable_id: u8,
    pub forecast_hour: u16,
}

fn read_u16(data: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes([data[offset], data[offset + 1]])
}

fn read_u32(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([data[offset], data[offset + 1], data[offset + 2], data[offset + 3]])
}

fn read_u64(data: &[u8], offset: usize) -> u64 {
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&data[offset..offset + 8]);
    u64::from_le_bytes(bytes)
}

fn align8(value: u64) -> Result<u64, DecodeError> {
    value
        .checked_add(7)
        .map(|sum| sum / 8 * 8)
        .ok_or_else(|| err("offset arithmetic overflow"))
}

fn checked_end(offset: u64, length: u64, file_size: u64, label: &str) -> Result<u64, DecodeError> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| err(format!("{label} range overflows")))?;
    if end > file_size {
        return Err(err(format!("{label} range exceeds file size")));
    }
    Ok(end)
}

fn require_zero(data: &[u8], start: u64, end: u64, label: &str) -> Result<(), DecodeError> {
    let slice = &data[start as usize..end as usize];
    if slice.iter().any(|&byte| byte != 0) {
        return Err(err(format!("{label} padding bytes must be zero")));
    }
    Ok(())
}

struct Metadata {
    json: String,
    plane_length: u32,
    frame_count: u32,
    first_hour: u16,
    step_hours: u16,
    variable_ids: Vec<u8>,
}

fn parse_metadata(raw: &[u8]) -> Result<Metadata, DecodeError> {
    let text = std::str::from_utf8(raw).map_err(|_| err("metadata is not UTF-8"))?;
    let value: serde_json::Value =
        serde_json::from_str(text).map_err(|_| err("metadata is not valid JSON"))?;
    let object = value.as_object().ok_or_else(|| err("metadata must be a JSON object"))?;
    if object.get("schemaVersion").and_then(|v| v.as_u64()) != Some(1) {
        return Err(err("metadata schemaVersion must be 1"));
    }
    let grid = object
        .get("grid")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("metadata grid missing"))?;
    let width = grid.get("width").and_then(|v| v.as_u64()).ok_or_else(|| err("grid width missing"))?;
    let height = grid.get("height").and_then(|v| v.as_u64()).ok_or_else(|| err("grid height missing"))?;
    if width == 0 || height == 0 {
        return Err(err("grid dimensions must be positive"));
    }
    let plane_length = width
        .checked_mul(height)
        .filter(|&points| points <= MAX_PLANE_LENGTH)
        .ok_or_else(|| err("grid exceeds the plane safety limit"))?;
    let time = object
        .get("time")
        .and_then(|v| v.as_object())
        .ok_or_else(|| err("metadata time missing"))?;
    let frame_count = time
        .get("frameCount")
        .and_then(|v| v.as_u64())
        .filter(|&count| count > 0 && count <= u16::MAX as u64)
        .ok_or_else(|| err("metadata frameCount is invalid"))?;
    let first_hour = time
        .get("firstForecastHour")
        .and_then(|v| v.as_u64())
        .filter(|&hour| hour <= u16::MAX as u64)
        .ok_or_else(|| err("metadata firstForecastHour is invalid"))?;
    let step_hours = time
        .get("stepHours")
        .and_then(|v| v.as_u64())
        .filter(|&step| step > 0 && step <= u16::MAX as u64)
        .ok_or_else(|| err("metadata stepHours is invalid"))?;
    let variables = object
        .get("variables")
        .and_then(|v| v.as_array())
        .ok_or_else(|| err("metadata variables missing"))?;
    let mut variable_ids = Vec::new();
    for variable in variables {
        let numeric = variable
            .get("numericId")
            .and_then(|v| v.as_u64())
            .filter(|&id| (1..=255).contains(&id))
            .ok_or_else(|| err("variable numericId is invalid"))?;
        if variable.get("id").and_then(|v| v.as_str()).is_none() {
            return Err(err("variable id is invalid"));
        }
        if variable_ids.contains(&(numeric as u8)) {
            return Err(err("duplicate variable numericId"));
        }
        variable_ids.push(numeric as u8);
    }
    if variable_ids.is_empty() {
        return Err(err("metadata must declare at least one variable"));
    }
    let last_hour = first_hour
        .checked_add((frame_count - 1).checked_mul(step_hours).ok_or_else(|| err("hour overflow"))?)
        .ok_or_else(|| err("hour overflow"))?;
    if last_hour > u16::MAX as u64 - 1 {
        return Err(err("forecast hours exceed the u16 range"));
    }
    Ok(Metadata {
        json: text.to_owned(),
        plane_length: plane_length as u32,
        frame_count: frame_count as u32,
        first_hour: first_hour as u16,
        step_hours: step_hours as u16,
        variable_ids,
    })
}

/// How much of the file the parser was given.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ParseMode {
    /// The complete file: payload adjacency and trailing padding are verified
    /// against the actual bytes.
    FullFile,
    /// Only the structural prefix (everything before `dataOffset`): payload
    /// geometry is verified arithmetically against the declared fileSize.
    Prefix,
}

/// Everything the index describes: parsed header geometry plus all entries.
struct Structure {
    metadata: Metadata,
    entries: Vec<PlaneEntry>,
    entry_map: HashMap<FrameRequest, usize>,
    data_offset: u64,
    file_size: u64,
}

fn parse_structure(data: &[u8], mode: ParseMode) -> Result<Structure, DecodeError> {
    if data.len() < HEADER_SIZE {
        return Err(err("file is smaller than the fixed header"));
    }
    if &data[0..8] != MAGIC {
        return Err(err("invalid magic, not a Xue file"));
    }
    if read_u16(data, 8) != VERSION {
        return Err(err("unsupported Xue version"));
    }
    if read_u16(data, 10) as usize != HEADER_SIZE {
        return Err(err("headerSize must be 80 for v1"));
    }
    if read_u32(data, 12) != 0 {
        return Err(err("header flags must be 0 for v1"));
    }
    let file_size = read_u64(data, 16);
    if mode == ParseMode::FullFile && file_size != data.len() as u64 {
        return Err(err("header fileSize does not match actual length"));
    }
    if mode == ParseMode::Prefix && (data.len() as u64) > file_size {
        return Err(err("prefix is longer than the declared fileSize"));
    }
    let metadata_offset = read_u64(data, 24);
    let metadata_length = read_u64(data, 32);
    let index_offset = read_u64(data, 40);
    let index_length = read_u64(data, 48);
    let data_offset = read_u64(data, 56);
    let dictionary_offset = read_u64(data, 64);
    let dictionary_length = read_u64(data, 72);

    // Section geometry: pure arithmetic against the declared fileSize, so it
    // is identical for full files and prefixes.
    if metadata_offset != HEADER_SIZE as u64 {
        return Err(err("metadataOffset must be 80 for v1"));
    }
    let metadata_end = checked_end(metadata_offset, metadata_length, file_size, "metadata")?;
    if index_offset != align8(metadata_end)? {
        return Err(err("indexOffset must immediately follow aligned metadata"));
    }
    let index_end = checked_end(index_offset, index_length, file_size, "index")?;
    let expected_data = if dictionary_length == 0 {
        if dictionary_offset != 0 {
            return Err(err("dictionaryOffset must be 0 when no dictionary is embedded"));
        }
        align8(index_end)?
    } else {
        if dictionary_offset != align8(index_end)? {
            return Err(err("dictionaryOffset must immediately follow the aligned index"));
        }
        let dictionary_end = checked_end(dictionary_offset, dictionary_length, file_size, "dictionary")?;
        align8(dictionary_end)?
    };
    if data_offset != expected_data {
        return Err(err("dataOffset must immediately follow the previous aligned section"));
    }
    if data_offset > file_size {
        return Err(err("dataOffset exceeds file size"));
    }
    // From here on the parser reads bytes below dataOffset, so the prefix
    // must actually contain them.
    if (data.len() as u64) < data_offset {
        return Err(err("prefix must include the complete metadata and index"));
    }

    require_zero(data, metadata_end, index_offset, "metadata")?;
    if dictionary_length == 0 {
        require_zero(data, index_end, data_offset, "index")?;
    } else {
        require_zero(data, index_end, dictionary_offset, "index")?;
        require_zero(data, dictionary_offset + dictionary_length, data_offset, "dictionary")?;
    }

    let metadata = parse_metadata(&data[metadata_offset as usize..metadata_end as usize])?;

    // Index header.
    if index_length < INDEX_HEADER_SIZE as u64 {
        return Err(err("index is smaller than its header"));
    }
    let index_start = index_offset as usize;
    if &data[index_start..index_start + 4] != INDEX_MAGIC {
        return Err(err("invalid index magic"));
    }
    if read_u16(data, index_start + 4) as usize != ENTRY_SIZE {
        return Err(err("index entrySize must be 40 for v1"));
    }
    if read_u16(data, index_start + 6) != 1 {
        return Err(err("index version must be 1"));
    }
    let entry_count = read_u32(data, index_start + 8) as u64;
    if read_u32(data, index_start + 12) != 0 {
        return Err(err("index reserved must be 0"));
    }
    let expected_entries = metadata.frame_count as u64 * metadata.variable_ids.len() as u64;
    if entry_count != expected_entries {
        return Err(err("entryCount does not match metadata"));
    }
    let entries_bytes = entry_count
        .checked_mul(ENTRY_SIZE as u64)
        .and_then(|bytes| bytes.checked_add(INDEX_HEADER_SIZE as u64))
        .ok_or_else(|| err("index size overflow"))?;
    if index_length != entries_bytes {
        return Err(err("indexLength does not match entryCount"));
    }

    let mut entries = Vec::with_capacity(entry_count as usize);
    for position in 0..entry_count as usize {
        let start = index_start + INDEX_HEADER_SIZE + position * ENTRY_SIZE;
        let raw = &data[start..start + ENTRY_SIZE];
        if read_u16(raw, 10) != 0 || raw[34..40].iter().any(|&byte| byte != 0) {
            return Err(err("index entry reserved fields must be 0"));
        }
        let entry = PlaneEntry {
            variable_id: raw[0],
            predictor: Predictor::parse(raw[1])?,
            compression: Compression::parse(raw[2])?,
            flags: raw[3],
            forecast_hour: read_u16(raw, 4),
            dependency_hour: read_u16(raw, 6),
            group_id: read_u16(raw, 8),
            compressed_length: read_u32(raw, 12),
            data_offset: read_u64(raw, 16),
            decoded_length: read_u32(raw, 24),
            crc32: read_u32(raw, 28),
            minimum_code: raw[32],
            maximum_code: raw[33],
        };
        entries.push(entry);
    }

    let mut entry_map = HashMap::with_capacity(entries.len());
    let mut previous_key: Option<(u8, u16)> = None;
    let mut occupied: Vec<(u64, u64)> = Vec::new();
    for (position, entry) in entries.iter().enumerate() {
        let key = (entry.variable_id, entry.forecast_hour);
        if let Some(previous) = previous_key {
            if key <= previous {
                return Err(err("index entries must be sorted and unique by (variableId, forecastHour)"));
            }
        }
        previous_key = Some(key);
        if !metadata.variable_ids.contains(&entry.variable_id) {
            return Err(err("entry references an unknown variableId"));
        }
        if entry.flags & !FLAG_ZSTD_CHECKSUM != 0 {
            return Err(err("unknown entry flags"));
        }
        if entry.compression == Compression::ZstdDict && dictionary_length == 0 {
            return Err(err("ZSTD_DICT entry requires an embedded dictionary"));
        }
        if entry.decoded_length != metadata.plane_length {
            return Err(err("entry decodedLength does not match the metadata grid"));
        }
        if entry.minimum_code > entry.maximum_code {
            return Err(err("entry minimumCode exceeds maximumCode"));
        }
        if entry.predictor == Predictor::Zero {
            if entry.compressed_length != 0 {
                return Err(err("ZERO entries must have no payload"));
            }
        } else {
            if entry.compressed_length == 0 {
                return Err(err("non-ZERO entries must have a payload"));
            }
            if entry.data_offset < data_offset {
                return Err(err("payload overlaps a structural section"));
            }
            checked_end(entry.data_offset, entry.compressed_length as u64, file_size, "payload")?;
            occupied.push((entry.data_offset, entry.compressed_length as u64));
        }
        entry_map.insert(
            FrameRequest { variable_id: entry.variable_id, forecast_hour: entry.forecast_hour },
            position,
        );
    }

    // Hour coverage per variable.
    for &variable_id in &metadata.variable_ids {
        for frame in 0..metadata.frame_count {
            let hour = metadata.first_hour as u64 + frame as u64 * metadata.step_hours as u64;
            let request = FrameRequest { variable_id, forecast_hour: hour as u16 };
            if !entry_map.contains_key(&request) {
                return Err(err("a variable does not cover every forecast hour"));
            }
        }
    }

    // Payload adjacency: contiguous, no gaps, aligned tail padding. This is
    // arithmetic over index entries, so a prefix can verify it too.
    occupied.sort_unstable();
    let mut cursor = data_offset;
    for (start, length) in &occupied {
        if *start != cursor {
            return Err(err("payloads must be strictly adjacent with no unindexed gaps"));
        }
        cursor += length;
    }
    if align8(cursor)? != file_size {
        return Err(err("fileSize must equal the aligned end of the last payload"));
    }
    if mode == ParseMode::FullFile {
        require_zero(data, cursor, file_size, "trailing")?;
    }

    // Dependency validation: same variable, same group, acyclic.
    for entry in &entries {
        match entry.predictor {
            Predictor::Raw | Predictor::Zero => {
                if entry.dependency_hour != NO_DEPENDENCY {
                    return Err(err("RAW and ZERO entries must have dependencyHour 65535"));
                }
            }
            Predictor::Anchor | Predictor::Previous => {
                let dependency_hour = if entry.predictor == Predictor::Anchor {
                    entry.dependency_hour
                } else {
                    if entry.dependency_hour != NO_DEPENDENCY
                        && entry.dependency_hour + 1 != entry.forecast_hour
                    {
                        return Err(err("PREVIOUS entry dependencyHour must reference the previous forecast time"));
                    }
                    entry
                        .forecast_hour
                        .checked_sub(1)
                        .ok_or_else(|| err("PREVIOUS entry has no previous forecast time"))?
                };
                let request = FrameRequest {
                    variable_id: entry.variable_id,
                    forecast_hour: dependency_hour,
                };
                let dependency = entry_map
                    .get(&request)
                    .map(|&position| &entries[position])
                    .ok_or_else(|| err("entry depends on a plane that does not exist"))?;
                if dependency.group_id != entry.group_id {
                    return Err(err("dependencies must stay inside the same temporal group"));
                }
            }
        }
    }

    let structure = Structure { metadata, entries, entry_map, data_offset, file_size };
    // Depth and cycle check for every chain.
    for entry in &structure.entries {
        structure.dependency_chain(FrameRequest {
            variable_id: entry.variable_id,
            forecast_hour: entry.forecast_hour,
        })?;
    }
    Ok(structure)
}

impl Structure {
    fn entry_position(&self, request: FrameRequest) -> Result<usize, DecodeError> {
        self.entry_map
            .get(&request)
            .copied()
            .ok_or_else(|| err("no plane for the requested variable and forecast hour"))
    }

    fn dependency_of(entry: &PlaneEntry) -> Option<u16> {
        match entry.predictor {
            Predictor::Anchor => Some(entry.dependency_hour),
            Predictor::Previous => entry.forecast_hour.checked_sub(1),
            _ => None,
        }
    }

    /// The chain from the requested frame down to its RAW/ZERO base, base last.
    fn dependency_chain(&self, request: FrameRequest) -> Result<Vec<FrameRequest>, DecodeError> {
        let mut chain = vec![request];
        let mut current = request;
        loop {
            let entry = &self.entries[self.entry_position(current)?];
            match Self::dependency_of(entry) {
                None => return Ok(chain),
                Some(hour) => {
                    let next = FrameRequest { variable_id: current.variable_id, forecast_hour: hour };
                    if chain.contains(&next) || chain.len() > self.metadata.frame_count as usize {
                        return Err(err("cyclic or too-deep dependency chain"));
                    }
                    chain.push(next);
                    current = next;
                }
            }
        }
    }
}

/// Where payload bytes live: the whole file, or per-entry sparse buffers
/// filled in by [`StreamingBundle::insert_range`].
enum PayloadStore {
    Full(Vec<u8>),
    Sparse { payloads: Vec<Option<Vec<u8>>>, resident_bytes: u64 },
}

impl PayloadStore {
    fn payload(&self, position: usize, entry: &PlaneEntry) -> Result<&[u8], DecodeError> {
        match self {
            PayloadStore::Full(data) => {
                let start = entry.data_offset as usize;
                Ok(&data[start..start + entry.compressed_length as usize])
            }
            PayloadStore::Sparse { payloads, .. } => payloads[position]
                .as_deref()
                .ok_or_else(|| err("payload is not resident yet")),
        }
    }

    fn is_resident(&self, position: usize, entry: &PlaneEntry) -> bool {
        match self {
            PayloadStore::Full(_) => true,
            PayloadStore::Sparse { payloads, .. } => {
                entry.compressed_length == 0 || payloads[position].is_some()
            }
        }
    }
}

/// Shared decode engine over a [`Structure`] and a [`PayloadStore`].
struct Core {
    structure: Structure,
    store: PayloadStore,
    anchor_cache: HashMap<FrameRequest, Vec<u8>>,
    output: Vec<u8>,
}

impl Core {
    fn entry(&self, request: FrameRequest) -> Result<(usize, PlaneEntry), DecodeError> {
        let position = self.structure.entry_position(request)?;
        Ok((position, self.structure.entries[position]))
    }

    fn decompress_payload(&self, position: usize, entry: &PlaneEntry) -> Result<Vec<u8>, DecodeError> {
        let raw = self.store.payload(position, entry)?;
        let expected = entry.decoded_length as usize;
        match entry.compression {
            Compression::None => {
                if raw.len() != expected {
                    return Err(err("uncompressed payload length mismatch"));
                }
                Ok(raw.to_vec())
            }
            Compression::ZstdDict => Err(err("ZSTD_DICT payloads require an embedded dictionary decoder")),
            Compression::Zstd => {
                let mut decoder = ruzstd::decoding::StreamingDecoder::new(raw)
                    .map_err(|error| err(format!("zstd frame error: {error}")))?;
                let mut output = Vec::with_capacity(expected);
                let mut limited = decoder.by_ref().take(expected as u64 + 1);
                limited
                    .read_to_end(&mut output)
                    .map_err(|error| err(format!("zstd decode error: {error}")))?;
                if output.len() != expected {
                    return Err(err("decompressed payload length mismatch"));
                }
                Ok(output)
            }
        }
    }

    fn checked_plane(&self, entry: &PlaneEntry, plane: Vec<u8>) -> Result<Vec<u8>, DecodeError> {
        if crc32fast::hash(&plane) != entry.crc32 {
            return Err(err(format!(
                "plane CRC32 mismatch for variable {} hour {}",
                entry.variable_id, entry.forecast_hour
            )));
        }
        let (minimum, maximum) = plane
            .iter()
            .fold((u8::MAX, u8::MIN), |(low, high), &value| (low.min(value), high.max(value)));
        if minimum != entry.minimum_code || maximum != entry.maximum_code {
            return Err(err("plane code range mismatch"));
        }
        Ok(plane)
    }

    fn decode_base(&mut self, request: FrameRequest) -> Result<Vec<u8>, DecodeError> {
        let (position, entry) = self.entry(request)?;
        let plane = match entry.predictor {
            Predictor::Zero => vec![0u8; entry.decoded_length as usize],
            Predictor::Raw => self.decompress_payload(position, &entry)?,
            _ => return Err(err("dependency chain base must be RAW or ZERO")),
        };
        self.checked_plane(&entry, plane)
    }

    /// Decode one frame into an internal buffer and return it as a slice.
    ///
    /// RAW base planes along the dependency chain are cached so scrubbing
    /// inside a temporal group re-decodes only the target residual.
    fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        let chain = self.structure.dependency_chain(request)?;
        let base_request = *chain.last().expect("chain is never empty");
        if chain.len() == 1 {
            // Independent plane: decode directly without growing the cache.
            self.output = self.decode_base(base_request)?;
            return Ok(&self.output);
        }
        if !self.anchor_cache.contains_key(&base_request) {
            let base = self.decode_base(base_request)?;
            self.anchor_cache.insert(base_request, base);
        }
        let mut plane = self.anchor_cache[&base_request].clone();
        for link in chain.iter().rev().skip(1) {
            let (position, entry) = self.entry(*link)?;
            let residual = self.decompress_payload(position, &entry)?;
            if residual.len() != plane.len() {
                return Err(err("residual length mismatch"));
            }
            for (target, delta) in plane.iter_mut().zip(residual.iter()) {
                *target = target.wrapping_add(*delta);
            }
            plane = self.checked_plane(&entry, plane)?;
        }
        self.output = plane;
        Ok(&self.output)
    }
}

pub struct Bundle {
    core: Core,
}

impl Bundle {
    /// Parse and fully validate a bundle, copying the bytes into owned memory.
    pub fn open(bytes: &[u8]) -> Result<Self, DecodeError> {
        let structure = parse_structure(bytes, ParseMode::FullFile)?;
        Ok(Bundle {
            core: Core {
                structure,
                store: PayloadStore::Full(bytes.to_vec()),
                anchor_cache: HashMap::new(),
                output: Vec::new(),
            },
        })
    }

    pub fn metadata_json(&self) -> &str {
        &self.core.structure.metadata.json
    }

    pub fn plane_length(&self) -> usize {
        self.core.structure.metadata.plane_length as usize
    }

    pub fn frame_count(&self) -> u32 {
        self.core.structure.metadata.frame_count
    }

    pub fn variable_ids(&self) -> &[u8] {
        &self.core.structure.metadata.variable_ids
    }

    pub fn clear_cache(&mut self) {
        self.core.anchor_cache.clear();
    }

    pub fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        self.core.decode_frame(request)
    }
}

/// A bundle opened from just its structural prefix, with payload bytes
/// arriving incrementally as HTTP range responses.
///
/// The prefix must span at least `[0, dataOffset)` — header, metadata, index,
/// and dictionary. Callers then ask [`missing_group_span`] which byte range a
/// frame's temporal group still needs, fetch it, and hand it to
/// [`insert_range`]; the writer keeps each group's payloads contiguous, so
/// one range request per group suffices.
///
/// [`missing_group_span`]: StreamingBundle::missing_group_span
/// [`insert_range`]: StreamingBundle::insert_range
pub struct StreamingBundle {
    core: Core,
}

impl StreamingBundle {
    pub fn open_prefix(prefix: &[u8]) -> Result<Self, DecodeError> {
        let structure = parse_structure(prefix, ParseMode::Prefix)?;
        let payloads = vec![None; structure.entries.len()];
        Ok(StreamingBundle {
            core: Core {
                structure,
                store: PayloadStore::Sparse { payloads, resident_bytes: 0 },
                anchor_cache: HashMap::new(),
                output: Vec::new(),
            },
        })
    }

    pub fn metadata_json(&self) -> &str {
        &self.core.structure.metadata.json
    }

    pub fn plane_length(&self) -> usize {
        self.core.structure.metadata.plane_length as usize
    }

    pub fn frame_count(&self) -> u32 {
        self.core.structure.metadata.frame_count
    }

    pub fn variable_ids(&self) -> &[u8] {
        &self.core.structure.metadata.variable_ids
    }

    pub fn clear_cache(&mut self) {
        self.core.anchor_cache.clear();
    }

    /// End of the structural prefix: the minimum bytes `open_prefix` needs.
    pub fn data_offset(&self) -> u64 {
        self.core.structure.data_offset
    }

    pub fn file_size(&self) -> u64 {
        self.core.structure.file_size
    }

    /// Sum of every payload's compressed length.
    pub fn total_payload_bytes(&self) -> u64 {
        self.core
            .structure
            .entries
            .iter()
            .map(|entry| entry.compressed_length as u64)
            .sum()
    }

    /// Compressed bytes inserted so far.
    pub fn resident_payload_bytes(&self) -> u64 {
        match &self.core.store {
            PayloadStore::Sparse { resident_bytes, .. } => *resident_bytes,
            PayloadStore::Full(_) => unreachable!("streaming bundles use a sparse store"),
        }
    }

    /// The contiguous byte span `[start, end)` still needed to decode any
    /// frame of the temporal group containing `request`, or `None` when the
    /// whole group is already resident.
    pub fn missing_group_span(&self, request: FrameRequest) -> Result<Option<(u64, u64)>, DecodeError> {
        let position = self.core.structure.entry_position(request)?;
        let target = self.core.structure.entries[position];
        let mut span: Option<(u64, u64)> = None;
        for (member_position, member) in self.core.structure.entries.iter().enumerate() {
            if member.variable_id != target.variable_id || member.group_id != target.group_id {
                continue;
            }
            if self.core.store.is_resident(member_position, member) {
                continue;
            }
            let start = member.data_offset;
            let end = start + member.compressed_length as u64;
            span = Some(match span {
                None => (start, end),
                Some((low, high)) => (low.min(start), high.max(end)),
            });
        }
        Ok(span)
    }

    /// Store payload bytes covering `[offset, offset + bytes.len())` of the
    /// file. Every index entry whose payload lies fully inside the range
    /// becomes resident; partial overlaps are ignored.
    pub fn insert_range(&mut self, offset: u64, bytes: &[u8]) -> Result<(), DecodeError> {
        let end = checked_end(offset, bytes.len() as u64, self.core.structure.file_size, "inserted")?;
        let PayloadStore::Sparse { payloads, resident_bytes } = &mut self.core.store else {
            unreachable!("streaming bundles use a sparse store");
        };
        for (position, entry) in self.core.structure.entries.iter().enumerate() {
            if entry.compressed_length == 0 || payloads[position].is_some() {
                continue;
            }
            let start = entry.data_offset;
            let stop = start + entry.compressed_length as u64;
            if start < offset || stop > end {
                continue;
            }
            let from = (start - offset) as usize;
            payloads[position] = Some(bytes[from..from + entry.compressed_length as usize].to_vec());
            *resident_bytes += entry.compressed_length as u64;
        }
        Ok(())
    }

    pub fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        self.core.decode_frame(request)
    }
}
