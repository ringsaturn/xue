//! Structural validation: header geometry, the index, and dependency chains.
//!
//! This is the layer that decides whether a file is well-formed, over the
//! byte layout [`crate::format`] unpacks. Every integer computation on
//! untrusted input is checked, and nothing is allocated from a file value
//! before it has been validated against the metadata grid and the declared
//! file length.
//!
//! Both readers come through here, and both container versions do. The
//! section geometry above the index is shared; below it the file is either a
//! plane-major v1 index ([`PlaneLayout`]) or a tiled v2 one ([`TiledLayout`]),
//! and [`Layout`] is what the decode engine dispatches on.
//!
//! [`ParseMode`] is the only difference between the readers: a complete file
//! has its payload adjacency and trailing padding verified against the actual
//! bytes, while a prefix verifies the same geometry arithmetically against the
//! header's `fileSize`.

use std::collections::HashMap;

use crate::decode::metadata::{parse_metadata, Metadata};
use crate::format::{
    align8, err, ChunkEntry, Compression, DecodeError, FixedHeader, FrameRequest, GroupEntry,
    IndexHeader, IndexHeaderV2, PlaneEntry, Predictor, TileGeometry, VariableEntry,
    CHUNK_ENTRY_SIZE, ENTRY_SIZE, FLAG_ZSTD_CHECKSUM, GROUP_ENTRY_SIZE, HEADER_SIZE,
    INDEX_HEADER_SIZE, INDEX_HEADER_SIZE_V2, NO_DEPENDENCY, VARIABLE_ENTRY_SIZE, VERSION_V2,
};

/// The next 8-byte boundary, as a decode failure rather than an Option.
fn aligned(value: u64) -> Result<u64, DecodeError> {
    align8(value).ok_or_else(|| err("offset arithmetic overflow"))
}

pub(crate) fn checked_end(offset: u64, length: u64, file_size: u64, label: &str) -> Result<u64, DecodeError> {
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
/// How much of the file the parser was given.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ParseMode {
    /// The complete file: payload adjacency and trailing padding are verified
    /// against the actual bytes.
    FullFile,
    /// Only the structural prefix (everything before `dataOffset`): payload
    /// geometry is verified arithmetically against the declared fileSize.
    Prefix,
}

/// Everything the index describes: parsed header geometry plus the layout.
pub(crate) struct Structure {
    pub(crate) metadata: Metadata,
    pub(crate) layout: Layout,
    pub(crate) data_offset: u64,
    pub(crate) file_size: u64,
}

/// What a payload is in this file: a whole plane, or a tile of a group.
pub(crate) enum Layout {
    Planes(PlaneLayout),
    Tiles(TiledLayout),
}

/// The v1 index: one entry per (variable, frame), with dependency chains.
pub(crate) struct PlaneLayout {
    pub(crate) entries: Vec<PlaneEntry>,
    entry_map: HashMap<FrameRequest, usize>,
}

/// The v2 index: the tiling, the axis partition, and one entry per chunk.
///
/// Chunk offsets are the prefix sums of the lengths — the physical order is
/// the spec's and chunks are strictly adjacent — so they are computed once
/// here rather than read from the file.
pub(crate) struct TiledLayout {
    pub(crate) geometry: TileGeometry,
    pub(crate) compression: Compression,
    pub(crate) variables: Vec<VariableEntry>,
    pub(crate) groups: Vec<GroupEntry>,
    pub(crate) chunks: Vec<ChunkEntry>,
    pub(crate) chunk_offsets: Vec<u64>,
    /// Frame index on the axis to `(group, index within the group)`.
    pub(crate) frame_group: Vec<(u32, u32)>,
}
pub(crate) fn parse_structure(data: &[u8], mode: ParseMode) -> Result<Structure, DecodeError> {
    let FixedHeader {
        version,
        file_size,
        metadata_offset,
        metadata_length,
        index_offset,
        index_length,
        data_offset,
        dictionary_offset,
        dictionary_length,
    } = FixedHeader::unpack(data)?;
    if mode == ParseMode::FullFile && file_size != data.len() as u64 {
        return Err(err("header fileSize does not match actual length"));
    }
    if mode == ParseMode::Prefix && (data.len() as u64) > file_size {
        return Err(err("prefix is longer than the declared fileSize"));
    }
    // Section geometry: pure arithmetic against the declared fileSize, so it
    // is identical for full files and prefixes.
    if metadata_offset != HEADER_SIZE as u64 {
        return Err(err("metadataOffset must be 80"));
    }
    let metadata_end = checked_end(metadata_offset, metadata_length, file_size, "metadata")?;
    if index_offset != aligned(metadata_end)? {
        return Err(err("indexOffset must immediately follow aligned metadata"));
    }
    let index_end = checked_end(index_offset, index_length, file_size, "index")?;
    let expected_data = if dictionary_length == 0 {
        if dictionary_offset != 0 {
            return Err(err("dictionaryOffset must be 0 when no dictionary is embedded"));
        }
        aligned(index_end)?
    } else {
        if dictionary_offset != aligned(index_end)? {
            return Err(err("dictionaryOffset must immediately follow the aligned index"));
        }
        let dictionary_end = checked_end(dictionary_offset, dictionary_length, file_size, "dictionary")?;
        aligned(dictionary_end)?
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

    let layout = if version == VERSION_V2 {
        Layout::Tiles(parse_tiled_index(
            data,
            &metadata,
            index_offset,
            index_length,
            data_offset,
            file_size,
            dictionary_length,
            mode,
        )?)
    } else {
        Layout::Planes(parse_plane_index(
            data,
            &metadata,
            index_offset,
            index_length,
            data_offset,
            file_size,
            dictionary_length,
            mode,
        )?)
    };
    Ok(Structure { metadata, layout, data_offset, file_size })
}

/// The plane-major v1 index: entries sorted by (variableId, frameOffset),
/// covering every frame of every variable, with acyclic dependency chains
/// that never leave a temporal group.
#[allow(clippy::too_many_arguments)]
fn parse_plane_index(
    data: &[u8],
    metadata: &Metadata,
    index_offset: u64,
    index_length: u64,
    data_offset: u64,
    file_size: u64,
    dictionary_length: u64,
    mode: ParseMode,
) -> Result<PlaneLayout, DecodeError> {
    // Index header.
    if index_length < INDEX_HEADER_SIZE as u64 {
        return Err(err("index is smaller than its header"));
    }
    let index_start = index_offset as usize;
    let entry_count = u64::from(IndexHeader::unpack(&data[index_start..])?.entry_count);
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
        entries.push(PlaneEntry::unpack(&data[start..start + ENTRY_SIZE])?);
    }

    let mut entry_map = HashMap::with_capacity(entries.len());
    let mut previous_key: Option<(u8, u16)> = None;
    let mut occupied: Vec<(u64, u64)> = Vec::new();
    for (position, entry) in entries.iter().enumerate() {
        let key = (entry.variable_id, entry.frame_offset);
        if let Some(previous) = previous_key {
            if key <= previous {
                return Err(err("index entries must be sorted and unique by (variableId, frameOffset)"));
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
        entry_map.insert(entry.request(), position);
    }

    // Frame coverage per variable, against the materialized axis.
    for &variable_id in &metadata.variable_ids {
        for &offset in &metadata.offsets {
            let request = FrameRequest { variable_id, frame_offset: offset };
            if !entry_map.contains_key(&request) {
                return Err(err("a variable does not cover every frame of the axis"));
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
    if aligned(cursor)? != file_size {
        return Err(err("fileSize must equal the aligned end of the last payload"));
    }
    if mode == ParseMode::FullFile {
        require_zero(data, cursor, file_size, "trailing")?;
    }

    // Dependency validation: same variable, same group, acyclic.
    for entry in &entries {
        match entry.predictor {
            Predictor::Raw | Predictor::Zero => {
                if entry.dependency_offset != NO_DEPENDENCY {
                    return Err(err("RAW and ZERO entries must have dependencyOffset 65535"));
                }
            }
            Predictor::Anchor | Predictor::Previous => {
                let dependency_offset = if entry.predictor == Predictor::Anchor {
                    entry.dependency_offset
                } else {
                    // PREVIOUS references the preceding frame on the time
                    // axis, carried explicitly in dependencyOffset (never the
                    // sentinel, never frameOffset - 1 by arithmetic).
                    let position = metadata
                        .offsets
                        .binary_search(&entry.frame_offset)
                        .map_err(|_| err("PREVIOUS entry is not on the time axis"))?;
                    if position == 0 {
                        return Err(err("PREVIOUS entry has no preceding frame on the time axis"));
                    }
                    let preceding = metadata.offsets[position - 1];
                    if entry.dependency_offset != preceding {
                        return Err(err(
                            "PREVIOUS entry dependencyOffset must reference the preceding frame on the time axis",
                        ));
                    }
                    preceding
                };
                let request = FrameRequest {
                    variable_id: entry.variable_id,
                    frame_offset: dependency_offset,
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

    let layout = PlaneLayout { entries, entry_map };
    // Depth and cycle check for every chain.
    for entry in &layout.entries {
        layout.dependency_chain(entry.request(), metadata.frame_count)?;
    }
    Ok(layout)
}

/// The tiled v2 index: three tables over a physical order the spec fixes.
///
/// Nothing here says where a chunk is — the order is `group, tile, variable`
/// and chunks are strictly adjacent, so an offset is the prefix sum of the
/// lengths before it. What is validated is that the geometry, the axis
/// partition and those prefix sums all agree with the metadata and the
/// declared file length.
#[allow(clippy::too_many_arguments)]
fn parse_tiled_index(
    data: &[u8],
    metadata: &Metadata,
    index_offset: u64,
    index_length: u64,
    data_offset: u64,
    file_size: u64,
    dictionary_length: u64,
    mode: ParseMode,
) -> Result<TiledLayout, DecodeError> {
    if index_length < INDEX_HEADER_SIZE_V2 as u64 {
        return Err(err("index is smaller than its header"));
    }
    let index_start = index_offset as usize;
    let header = IndexHeaderV2::unpack(&data[index_start..])?;
    if header.compression == Compression::None {
        return Err(err("v2 chunks must be Zstandard frames"));
    }
    if header.compression == Compression::ZstdDict && dictionary_length == 0 {
        return Err(err("ZSTD_DICT requires an embedded dictionary"));
    }
    if header.group_count == 0 || header.variable_count == 0 {
        return Err(err("a v2 file must declare at least one group and one variable"));
    }
    if header.variable_count as usize != metadata.variable_ids.len() {
        return Err(err("index variableCount does not match the metadata variables"));
    }
    let geometry = TileGeometry::new(
        metadata.width,
        metadata.height,
        u32::from(header.tile_width),
        u32::from(header.tile_height),
    )?;
    let expected_chunks = u64::from(header.group_count)
        .checked_mul(u64::from(geometry.count()))
        .and_then(|chunks| chunks.checked_mul(u64::from(header.variable_count)))
        .ok_or_else(|| err("chunk count overflow"))?;
    if u64::from(header.chunk_count) != expected_chunks {
        return Err(err("chunkCount does not match groupCount x tileCount x variableCount"));
    }
    let tables_bytes = (INDEX_HEADER_SIZE_V2 as u64)
        + u64::from(header.variable_count) * VARIABLE_ENTRY_SIZE as u64
        + u64::from(header.group_count) * GROUP_ENTRY_SIZE as u64
        + u64::from(header.chunk_count) * CHUNK_ENTRY_SIZE as u64;
    if index_length != tables_bytes {
        return Err(err("indexLength does not match the index tables"));
    }

    let mut cursor = index_start + INDEX_HEADER_SIZE_V2;
    let mut variables = Vec::with_capacity(header.variable_count as usize);
    for _ in 0..header.variable_count {
        variables.push(VariableEntry::unpack(&data[cursor..cursor + VARIABLE_ENTRY_SIZE])?);
        cursor += VARIABLE_ENTRY_SIZE;
    }
    if variables
        .windows(2)
        .any(|pair| pair[0].variable_id >= pair[1].variable_id)
    {
        return Err(err("variable entries must be sorted and unique by variableId"));
    }
    if variables
        .iter()
        .any(|variable| !metadata.variable_ids.contains(&variable.variable_id))
    {
        return Err(err("variable entries do not match the metadata variables"));
    }

    let mut groups = Vec::with_capacity(header.group_count as usize);
    let mut frame_group = Vec::with_capacity(metadata.frame_count as usize);
    let mut frame_cursor: u32 = 0;
    for index in 0..header.group_count {
        let group = GroupEntry::unpack(&data[cursor..cursor + GROUP_ENTRY_SIZE])?;
        cursor += GROUP_ENTRY_SIZE;
        if group.frame_count == 0 {
            return Err(err("a temporal group must hold at least one frame"));
        }
        if u32::from(group.first_frame) != frame_cursor {
            return Err(err("temporal groups must partition the axis in order"));
        }
        for position in 0..u32::from(group.frame_count) {
            frame_group.push((u32::from(index), position));
        }
        frame_cursor += u32::from(group.frame_count);
        if frame_cursor > metadata.frame_count {
            return Err(err("temporal groups overrun the axis"));
        }
        groups.push(group);
    }
    if frame_cursor != metadata.frame_count {
        return Err(err("temporal groups must end exactly at the axis frameCount"));
    }

    let mut chunks = Vec::with_capacity(header.chunk_count as usize);
    let mut chunk_offsets = Vec::with_capacity(header.chunk_count as usize);
    let mut position = data_offset;
    for _ in 0..header.chunk_count {
        let chunk = ChunkEntry::unpack(&data[cursor..cursor + CHUNK_ENTRY_SIZE])?;
        cursor += CHUNK_ENTRY_SIZE;
        if chunk.compressed_length == 0 {
            return Err(err("a chunk must have a payload"));
        }
        chunk_offsets.push(position);
        position = checked_end(position, u64::from(chunk.compressed_length), file_size, "chunk")?;
        chunks.push(chunk);
    }
    if aligned(position)? != file_size {
        return Err(err("fileSize must equal the aligned end of the last chunk"));
    }
    if mode == ParseMode::FullFile {
        require_zero(data, position, file_size, "trailing")?;
    }

    Ok(TiledLayout {
        geometry,
        compression: header.compression,
        variables,
        groups,
        chunks,
        chunk_offsets,
        frame_group,
    })
}

impl Structure {
    /// How many payloads the index describes — planes in v1, chunks in v2.
    /// Residency and range requests are tracked per payload, whatever a
    /// payload happens to be in this file.
    pub(crate) fn payload_count(&self) -> usize {
        match &self.layout {
            Layout::Planes(planes) => planes.entries.len(),
            Layout::Tiles(tiles) => tiles.chunks.len(),
        }
    }

    /// One payload's byte span `[start, end)`. A v1 ZERO plane has no bytes
    /// at all and reports an empty span.
    pub(crate) fn payload_span(&self, position: usize) -> (u64, u64) {
        match &self.layout {
            Layout::Planes(planes) => {
                let entry = planes.entries[position];
                if entry.compressed_length == 0 {
                    (0, 0)
                } else {
                    (entry.data_offset, entry.data_offset + u64::from(entry.compressed_length))
                }
            }
            Layout::Tiles(tiles) => tiles.chunk_span(position),
        }
    }

    /// The frame's index on the axis.
    pub(crate) fn frame_index(&self, frame_offset: u16) -> Result<usize, DecodeError> {
        self.metadata
            .offsets
            .binary_search(&frame_offset)
            .map_err(|_| err("no plane for the requested variable and forecast hour"))
    }
}

impl PlaneLayout {
    pub(crate) fn entry_position(&self, request: FrameRequest) -> Result<usize, DecodeError> {
        self.entry_map
            .get(&request)
            .copied()
            .ok_or_else(|| err("no plane for the requested variable and forecast hour"))
    }

    fn dependency_of(entry: &PlaneEntry) -> Option<u16> {
        // ANCHOR and PREVIOUS both carry their dependency explicitly;
        // parse_plane_index has pinned PREVIOUS to the preceding axis frame.
        match entry.predictor {
            Predictor::Anchor | Predictor::Previous => Some(entry.dependency_offset),
            _ => None,
        }
    }

    /// The chain from the requested frame down to its RAW/ZERO base, base last.
    pub(crate) fn dependency_chain(
        &self,
        request: FrameRequest,
        frame_count: u32,
    ) -> Result<Vec<FrameRequest>, DecodeError> {
        let mut chain = vec![request];
        let mut current = request;
        loop {
            let entry = &self.entries[self.entry_position(current)?];
            match Self::dependency_of(entry) {
                None => return Ok(chain),
                Some(hour) => {
                    let next = FrameRequest { variable_id: current.variable_id, frame_offset: hour };
                    if chain.contains(&next) || chain.len() > frame_count as usize {
                        return Err(err("cyclic or too-deep dependency chain"));
                    }
                    chain.push(next);
                    current = next;
                }
            }
        }
    }
}

impl TiledLayout {
    pub(crate) fn variable_position(&self, variable_id: u8) -> Result<usize, DecodeError> {
        self.variables
            .iter()
            .position(|variable| variable.variable_id == variable_id)
            .ok_or_else(|| err("no chunks for the requested variable"))
    }

    /// A chunk's position in the file's fixed physical order:
    /// group, then tile (row-major), then variable (ascending id).
    pub(crate) fn chunk_position(&self, group: u32, tile: u32, variable_position: usize) -> usize {
        ((group as usize * self.geometry.count() as usize) + tile as usize) * self.variables.len()
            + variable_position
    }

    /// The byte range `[start, end)` of a chunk's compressed payload.
    pub(crate) fn chunk_span(&self, position: usize) -> (u64, u64) {
        let start = self.chunk_offsets[position];
        (start, start + u64::from(self.chunks[position].compressed_length))
    }

    /// How many bytes a chunk reconstructs to: the group's frames of the
    /// tile's clipped rectangle.
    pub(crate) fn decoded_length(&self, group: u32, tile: u32) -> usize {
        let (height, width) = self.geometry.shape(tile);
        self.groups[group as usize].frame_count as usize * height as usize * width as usize
    }
}
