//! The Xue container's byte layout, shared by the decoder and the encoder.
//!
//! `docs/format.md` is the normative spec. This module is the one place the
//! spec's offsets and widths are written down: the decoder unpacks structures
//! through it and the native encoder packs them through it, so a field can
//! never drift between the two halves of the crate.
//!
//! Everything here is pure byte conversion. It validates only what a field's
//! own encoding demands — a magic number, a reserved word, an enum's code —
//! and never allocates from a file value. Cross-section geometry, the
//! metadata grid, dependency chains and every other semantic rule stay in
//! [`crate::decode`], which is what actually reads untrusted files.

use std::fmt;

pub const MAGIC: &[u8; 8] = b"XUE\0\0\0\0\0";
/// The plane-major container. Still read; no longer written.
pub const VERSION: u16 = 1;
/// The tiled container: a payload is a chunk — one tile of one temporal group
/// for one variable — not a whole plane.
pub const VERSION_V2: u16 = 2;
pub const HEADER_SIZE: usize = 80;
pub const INDEX_MAGIC: &[u8; 4] = b"IDX1";
pub const INDEX_HEADER_SIZE: usize = 16;
pub const INDEX_VERSION: u16 = 1;
pub const ENTRY_SIZE: usize = 40;
pub const NO_DEPENDENCY: u16 = 0xFFFF;
pub const FLAG_ZSTD_CHECKSUM: u8 = 0x01;

pub const INDEX_MAGIC_V2: &[u8; 4] = b"IDX2";
pub const INDEX_HEADER_SIZE_V2: usize = 32;
pub const INDEX_VERSION_V2: u16 = 2;
pub const VARIABLE_ENTRY_SIZE: usize = 4;
pub const GROUP_ENTRY_SIZE: usize = 4;
pub const CHUNK_ENTRY_SIZE: usize = 8;

/// Safety limit for one decoded plane: 64M points. A chunk is one tile of one
/// group, so it is bounded by the same limit times the group length, and a
/// group holds at most 255 frames.
pub const MAX_PLANE_LENGTH: u64 = 64 * 1024 * 1024;
/// The coarsest time-axis unit, and the only one schema versions 1 and 2 can
/// describe. A schemaVersion 3 axis names its own unit, which must divide it.
pub const HOUR_SECONDS: u32 = 3600;

/// A file that cannot be read as Xue v1, at any layer: a malformed byte
/// layout here, or a violated structural rule in [`crate::decode`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodeError(pub String);

impl fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for DecodeError {}

pub(crate) fn err(message: impl Into<String>) -> DecodeError {
    DecodeError(message.into())
}

// -- primitives --------------------------------------------------------------

pub(crate) fn read_u16(data: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes([data[offset], data[offset + 1]])
}

pub(crate) fn read_u32(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([data[offset], data[offset + 1], data[offset + 2], data[offset + 3]])
}

pub(crate) fn read_u64(data: &[u8], offset: usize) -> u64 {
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&data[offset..offset + 8]);
    u64::from_le_bytes(bytes)
}

/// The next multiple of 8 at or above `value`, or `None` on overflow.
///
/// Every section boundary in the container is 8-byte aligned, so both halves
/// round the same way: the decoder turns `None` into a rejected file, the
/// encoder into a bundle error.
pub fn align8(value: u64) -> Option<u64> {
    value.checked_add(7).map(|sum| sum / 8 * 8)
}

/// Greatest common divisor, used on both sides to reduce a time axis to the
/// coarsest unit that expresses every offset exactly.
pub fn gcd(left: u64, right: u64) -> u64 {
    let (mut left, mut right) = (left, right);
    while right != 0 {
        (left, right) = (right, left % right);
    }
    left
}

pub fn crc32(bytes: &[u8]) -> u32 {
    crc32fast::hash(bytes)
}

// -- enums -------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Predictor {
    Raw = 0,
    Anchor = 1,
    Previous = 2,
    Zero = 3,
}

impl Predictor {
    pub fn parse(value: u8) -> Result<Self, DecodeError> {
        match value {
            0 => Ok(Self::Raw),
            1 => Ok(Self::Anchor),
            2 => Ok(Self::Previous),
            3 => Ok(Self::Zero),
            other => Err(err(format!("unknown predictor {other}"))),
        }
    }

    pub fn code(self) -> u8 {
        self as u8
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Compression {
    None = 0,
    Zstd = 1,
    ZstdDict = 2,
}

impl Compression {
    pub fn parse(value: u8) -> Result<Self, DecodeError> {
        match value {
            0 => Ok(Self::None),
            1 => Ok(Self::Zstd),
            2 => Ok(Self::ZstdDict),
            other => Err(err(format!("unknown compression {other}"))),
        }
    }

    pub fn code(self) -> u8 {
        self as u8
    }
}

// -- FixedHeader -------------------------------------------------------------

/// The 80-byte fixed header's section geometry, plus the container version
/// that says how to read the index below it.
///
/// `magic`, `headerSize` and the reserved flags word are checked by
/// [`FixedHeader::unpack`] and written by [`FixedHeader::pack`], so they are
/// not fields. Whether the offsets are *consistent with each other* is
/// [`crate::decode`]'s question, not this module's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FixedHeader {
    /// [`VERSION`] or [`VERSION_V2`]; nothing else parses.
    pub version: u16,
    pub file_size: u64,
    pub metadata_offset: u64,
    pub metadata_length: u64,
    pub index_offset: u64,
    pub index_length: u64,
    pub data_offset: u64,
    pub dictionary_offset: u64,
    pub dictionary_length: u64,
}

impl FixedHeader {
    pub fn unpack(data: &[u8]) -> Result<Self, DecodeError> {
        if data.len() < HEADER_SIZE {
            return Err(err("file is smaller than the fixed header"));
        }
        if &data[0..8] != MAGIC {
            return Err(err("invalid magic, not a Xue file"));
        }
        let version = read_u16(data, 8);
        if version != VERSION && version != VERSION_V2 {
            return Err(err("unsupported Xue version"));
        }
        if read_u16(data, 10) as usize != HEADER_SIZE {
            return Err(err("headerSize must be 80"));
        }
        if read_u32(data, 12) != 0 {
            return Err(err("header flags must be 0"));
        }
        Ok(FixedHeader {
            version,
            file_size: read_u64(data, 16),
            metadata_offset: read_u64(data, 24),
            metadata_length: read_u64(data, 32),
            index_offset: read_u64(data, 40),
            index_length: read_u64(data, 48),
            data_offset: read_u64(data, 56),
            dictionary_offset: read_u64(data, 64),
            dictionary_length: read_u64(data, 72),
        })
    }

    pub fn pack(&self) -> [u8; HEADER_SIZE] {
        let mut buffer = [0u8; HEADER_SIZE];
        buffer[0..8].copy_from_slice(MAGIC);
        buffer[8..10].copy_from_slice(&self.version.to_le_bytes());
        buffer[10..12].copy_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
        // flags stay zero in both versions.
        buffer[16..24].copy_from_slice(&self.file_size.to_le_bytes());
        buffer[24..32].copy_from_slice(&self.metadata_offset.to_le_bytes());
        buffer[32..40].copy_from_slice(&self.metadata_length.to_le_bytes());
        buffer[40..48].copy_from_slice(&self.index_offset.to_le_bytes());
        buffer[48..56].copy_from_slice(&self.index_length.to_le_bytes());
        buffer[56..64].copy_from_slice(&self.data_offset.to_le_bytes());
        buffer[64..72].copy_from_slice(&self.dictionary_offset.to_le_bytes());
        buffer[72..80].copy_from_slice(&self.dictionary_length.to_le_bytes());
        buffer
    }
}

// -- IndexHeader -------------------------------------------------------------

/// The 16-byte index header. Its magic, entry size, version and reserved word
/// are fixed for v1, so `entry_count` is the only field it carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexHeader {
    pub entry_count: u32,
}

impl IndexHeader {
    pub fn unpack(data: &[u8]) -> Result<Self, DecodeError> {
        if data.len() < INDEX_HEADER_SIZE {
            return Err(err("index is smaller than its header"));
        }
        if &data[0..4] != INDEX_MAGIC {
            return Err(err("invalid index magic"));
        }
        if read_u16(data, 4) as usize != ENTRY_SIZE {
            return Err(err("index entrySize must be 40 for v1"));
        }
        if read_u16(data, 6) != INDEX_VERSION {
            return Err(err("index version must be 1"));
        }
        let entry_count = read_u32(data, 8);
        if read_u32(data, 12) != 0 {
            return Err(err("index reserved must be 0"));
        }
        Ok(IndexHeader { entry_count })
    }

    pub fn pack(&self) -> [u8; INDEX_HEADER_SIZE] {
        let mut buffer = [0u8; INDEX_HEADER_SIZE];
        buffer[0..4].copy_from_slice(INDEX_MAGIC);
        buffer[4..6].copy_from_slice(&(ENTRY_SIZE as u16).to_le_bytes());
        buffer[6..8].copy_from_slice(&INDEX_VERSION.to_le_bytes());
        buffer[8..12].copy_from_slice(&self.entry_count.to_le_bytes());
        // reserved stays zero.
        buffer
    }
}

// -- v2 index ----------------------------------------------------------------

/// The 32-byte v2 index header: the tiling, the partition sizes and the one
/// compression every chunk uses.
///
/// `tileColumns`, `tileRows` and `tileCount` are derived from the metadata
/// grid and never stored, so the geometry has exactly one encoding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexHeaderV2 {
    pub tile_width: u16,
    pub tile_height: u16,
    pub group_count: u16,
    pub variable_count: u8,
    pub compression: Compression,
    pub chunk_count: u32,
}

impl IndexHeaderV2 {
    pub fn unpack(data: &[u8]) -> Result<Self, DecodeError> {
        if data.len() < INDEX_HEADER_SIZE_V2 {
            return Err(err("index is smaller than its header"));
        }
        if &data[0..4] != INDEX_MAGIC_V2 {
            return Err(err("invalid index magic"));
        }
        if read_u16(data, 4) != INDEX_VERSION_V2 {
            return Err(err("index version must be 2"));
        }
        if read_u16(data, 6) as usize != INDEX_HEADER_SIZE_V2 {
            return Err(err("index headerSize must be 32 for v2"));
        }
        if read_u32(data, 20) != 0 || read_u64(data, 24) != 0 {
            return Err(err("index reserved words must be 0"));
        }
        Ok(IndexHeaderV2 {
            tile_width: read_u16(data, 8),
            tile_height: read_u16(data, 10),
            group_count: read_u16(data, 12),
            variable_count: data[14],
            compression: Compression::parse(data[15])?,
            chunk_count: read_u32(data, 16),
        })
    }

    pub fn pack(&self) -> [u8; INDEX_HEADER_SIZE_V2] {
        let mut buffer = [0u8; INDEX_HEADER_SIZE_V2];
        buffer[0..4].copy_from_slice(INDEX_MAGIC_V2);
        buffer[4..6].copy_from_slice(&INDEX_VERSION_V2.to_le_bytes());
        buffer[6..8].copy_from_slice(&(INDEX_HEADER_SIZE_V2 as u16).to_le_bytes());
        buffer[8..10].copy_from_slice(&self.tile_width.to_le_bytes());
        buffer[10..12].copy_from_slice(&self.tile_height.to_le_bytes());
        buffer[12..14].copy_from_slice(&self.group_count.to_le_bytes());
        buffer[14] = self.variable_count;
        buffer[15] = self.compression.code();
        buffer[16..20].copy_from_slice(&self.chunk_count.to_le_bytes());
        // reserved words stay zero.
        buffer
    }
}

/// One 4-byte variable entry: an id and the predictor every chunk of that
/// variable is coded with.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VariableEntry {
    pub variable_id: u8,
    pub predictor: Predictor,
}

impl VariableEntry {
    pub fn unpack(raw: &[u8]) -> Result<Self, DecodeError> {
        if raw.len() < VARIABLE_ENTRY_SIZE {
            return Err(err("variable entry is truncated"));
        }
        if read_u16(raw, 2) != 0 {
            return Err(err("variable entry reserved must be 0"));
        }
        let predictor = Predictor::parse(raw[1])?;
        // A v2 chunk is decompressed whole, so an anchor buys no random
        // access: one chunk has exactly one valid encoding.
        if !matches!(predictor, Predictor::Raw | Predictor::Previous) {
            return Err(err("v2 predictors must be RAW or PREVIOUS"));
        }
        Ok(VariableEntry { variable_id: raw[0], predictor })
    }

    pub fn pack(&self) -> [u8; VARIABLE_ENTRY_SIZE] {
        let mut buffer = [0u8; VARIABLE_ENTRY_SIZE];
        buffer[0] = self.variable_id;
        buffer[1] = self.predictor.code();
        buffer
    }
}

/// One 4-byte group entry: a run of consecutive frames on the shared axis.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GroupEntry {
    pub first_frame: u16,
    pub frame_count: u8,
}

impl GroupEntry {
    pub fn unpack(raw: &[u8]) -> Result<Self, DecodeError> {
        if raw.len() < GROUP_ENTRY_SIZE {
            return Err(err("group entry is truncated"));
        }
        if raw[3] != 0 {
            return Err(err("group entry reserved must be 0"));
        }
        Ok(GroupEntry { first_frame: read_u16(raw, 0), frame_count: raw[2] })
    }

    pub fn pack(&self) -> [u8; GROUP_ENTRY_SIZE] {
        let mut buffer = [0u8; GROUP_ENTRY_SIZE];
        buffer[0..2].copy_from_slice(&self.first_frame.to_le_bytes());
        buffer[2] = self.frame_count;
        buffer
    }
}

/// One 8-byte chunk entry. A chunk's *offset* is not stored: the physical
/// order is the spec's and chunks are strictly adjacent, so an offset is the
/// prefix sum of the lengths before it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChunkEntry {
    pub compressed_length: u32,
    pub crc32: u32,
}

impl ChunkEntry {
    pub fn unpack(raw: &[u8]) -> Result<Self, DecodeError> {
        if raw.len() < CHUNK_ENTRY_SIZE {
            return Err(err("chunk entry is truncated"));
        }
        Ok(ChunkEntry { compressed_length: read_u32(raw, 0), crc32: read_u32(raw, 4) })
    }

    pub fn pack(&self) -> [u8; CHUNK_ENTRY_SIZE] {
        let mut buffer = [0u8; CHUNK_ENTRY_SIZE];
        buffer[0..4].copy_from_slice(&self.compressed_length.to_le_bytes());
        buffer[4..8].copy_from_slice(&self.crc32.to_le_bytes());
        buffer
    }
}

/// How a grid is cut into tiles: derived arithmetic over the metadata grid
/// and the index header, stored nowhere.
///
/// Tiles start at the grid's first cell and are laid out row-major; the last
/// column and the last row are clipped to the grid, so a tile size need not
/// divide the grid. A tile is cells, never degrees — its geographic footprint
/// follows from the grid block, and the horizontal wrap of a global grid is a
/// property of the grid, not of any tile.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TileGeometry {
    pub width: u32,
    pub height: u32,
    pub tile_width: u32,
    pub tile_height: u32,
}

impl TileGeometry {
    pub fn new(width: u32, height: u32, tile_width: u32, tile_height: u32) -> Result<Self, DecodeError> {
        if tile_width == 0 || tile_height == 0 || tile_width > width || tile_height > height {
            return Err(err("tile size must be between 1 and the grid dimensions"));
        }
        Ok(TileGeometry { width, height, tile_width, tile_height })
    }

    pub fn columns(&self) -> u32 {
        self.width.div_ceil(self.tile_width)
    }

    pub fn rows(&self) -> u32 {
        self.height.div_ceil(self.tile_height)
    }

    pub fn count(&self) -> u32 {
        self.columns() * self.rows()
    }

    /// The (row, column) of a tile's north-west cell in the grid.
    pub fn origin(&self, tile: u32) -> (u32, u32) {
        ((tile / self.columns()) * self.tile_height, (tile % self.columns()) * self.tile_width)
    }

    /// The clipped (height, width) of a tile in cells.
    pub fn shape(&self, tile: u32) -> (u32, u32) {
        let (row, column) = self.origin(tile);
        (
            self.tile_height.min(self.height - row),
            self.tile_width.min(self.width - column),
        )
    }

    /// The tile containing a grid cell.
    pub fn tile_of(&self, row: u32, column: u32) -> Result<u32, DecodeError> {
        if row >= self.height || column >= self.width {
            return Err(err("cell is outside the grid"));
        }
        Ok((row / self.tile_height) * self.columns() + column / self.tile_width)
    }
}

/// A rectangle of tiles, the unit a viewport asks for. Inclusive of both ends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TileRect {
    pub first_column: u32,
    pub first_row: u32,
    pub last_column: u32,
    pub last_row: u32,
}

impl TileRect {
    /// The tiles a grid rectangle touches, clamped to the geometry.
    pub fn covering(geometry: &TileGeometry, row: u32, column: u32, height: u32, width: u32) -> Self {
        let last_row = (row + height.saturating_sub(1)).min(geometry.height - 1);
        let last_column = (column + width.saturating_sub(1)).min(geometry.width - 1);
        TileRect {
            first_column: column.min(geometry.width - 1) / geometry.tile_width,
            first_row: row.min(geometry.height - 1) / geometry.tile_height,
            last_column: last_column / geometry.tile_width,
            last_row: last_row / geometry.tile_height,
        }
    }

    pub fn contains(&self, geometry: &TileGeometry, tile: u32) -> bool {
        let (row, column) = (tile / geometry.columns(), tile % geometry.columns());
        (self.first_row..=self.last_row).contains(&row)
            && (self.first_column..=self.last_column).contains(&column)
    }
}

// -- PlaneEntry --------------------------------------------------------------

/// One 40-byte index entry: where a plane's payload is, how to undo its
/// coding, and what the decoded bytes must check out as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PlaneEntry {
    pub variable_id: u8,
    pub predictor: Predictor,
    pub compression: Compression,
    pub flags: u8,
    pub frame_offset: u16,
    pub dependency_offset: u16,
    pub group_id: u16,
    pub compressed_length: u32,
    pub data_offset: u64,
    pub decoded_length: u32,
    pub crc32: u32,
    pub minimum_code: u8,
    pub maximum_code: u8,
}

impl PlaneEntry {
    /// The key a plane is addressed by.
    pub fn request(&self) -> FrameRequest {
        FrameRequest { variable_id: self.variable_id, frame_offset: self.frame_offset }
    }

    pub fn unpack(raw: &[u8]) -> Result<Self, DecodeError> {
        if raw.len() < ENTRY_SIZE {
            return Err(err("index entry is truncated"));
        }
        if read_u16(raw, 10) != 0 || raw[34..40].iter().any(|&byte| byte != 0) {
            return Err(err("index entry reserved fields must be 0"));
        }
        Ok(PlaneEntry {
            variable_id: raw[0],
            predictor: Predictor::parse(raw[1])?,
            compression: Compression::parse(raw[2])?,
            flags: raw[3],
            frame_offset: read_u16(raw, 4),
            dependency_offset: read_u16(raw, 6),
            group_id: read_u16(raw, 8),
            compressed_length: read_u32(raw, 12),
            data_offset: read_u64(raw, 16),
            decoded_length: read_u32(raw, 24),
            crc32: read_u32(raw, 28),
            minimum_code: raw[32],
            maximum_code: raw[33],
        })
    }

    pub fn pack(&self) -> [u8; ENTRY_SIZE] {
        let mut buffer = [0u8; ENTRY_SIZE];
        buffer[0] = self.variable_id;
        buffer[1] = self.predictor.code();
        buffer[2] = self.compression.code();
        buffer[3] = self.flags;
        buffer[4..6].copy_from_slice(&self.frame_offset.to_le_bytes());
        buffer[6..8].copy_from_slice(&self.dependency_offset.to_le_bytes());
        buffer[8..10].copy_from_slice(&self.group_id.to_le_bytes());
        // reserved0 stays zero.
        buffer[12..16].copy_from_slice(&self.compressed_length.to_le_bytes());
        buffer[16..24].copy_from_slice(&self.data_offset.to_le_bytes());
        buffer[24..28].copy_from_slice(&self.decoded_length.to_le_bytes());
        buffer[28..32].copy_from_slice(&self.crc32.to_le_bytes());
        buffer[32] = self.minimum_code;
        buffer[33] = self.maximum_code;
        // reserved1 stays zero.
        buffer
    }
}

/// One plane, addressed the way both the index and the callers address it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FrameRequest {
    pub variable_id: u8,
    pub frame_offset: u16,
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Packing and unpacking are the same table read in two directions, so a
    /// field that drifts on one side fails to round-trip.
    #[test]
    fn headers_round_trip() {
        let header = FixedHeader {
            version: VERSION,
            file_size: 4096,
            metadata_offset: 80,
            metadata_length: 301,
            index_offset: 384,
            index_length: 96,
            data_offset: 480,
            dictionary_offset: 0,
            dictionary_length: 0,
        };
        assert_eq!(FixedHeader::unpack(&header.pack()).expect("valid header"), header);
        let index = IndexHeader { entry_count: 12 };
        assert_eq!(IndexHeader::unpack(&index.pack()).expect("valid index header"), index);
    }

    #[test]
    fn plane_entries_round_trip() {
        for (predictor, compression) in [
            (Predictor::Raw, Compression::Zstd),
            (Predictor::Anchor, Compression::None),
            (Predictor::Previous, Compression::ZstdDict),
            (Predictor::Zero, Compression::None),
        ] {
            let entry = PlaneEntry {
                variable_id: 3,
                predictor,
                compression,
                flags: FLAG_ZSTD_CHECKSUM,
                frame_offset: 258,
                dependency_offset: NO_DEPENDENCY,
                group_id: 7,
                compressed_length: 1234,
                data_offset: 8192,
                decoded_length: 65_160,
                crc32: 0xDEAD_BEEF,
                minimum_code: 4,
                maximum_code: 251,
            };
            assert_eq!(PlaneEntry::unpack(&entry.pack()).expect("valid entry"), entry);
        }
    }

    /// Every reserved byte a writer must leave zero is a byte a reader must
    /// refuse to see set — otherwise a later version's field is silently
    /// ignored by an old decoder.
    #[test]
    fn reserved_bytes_are_rejected_when_set() {
        let entry = PlaneEntry {
            variable_id: 1,
            predictor: Predictor::Raw,
            compression: Compression::Zstd,
            flags: 0,
            frame_offset: 0,
            dependency_offset: NO_DEPENDENCY,
            group_id: 0,
            compressed_length: 1,
            data_offset: 480,
            decoded_length: 1,
            crc32: 0,
            minimum_code: 0,
            maximum_code: 0,
        };
        for byte in [10, 11, 34, 35, 36, 37, 38, 39] {
            let mut raw = entry.pack();
            raw[byte] = 1;
            assert!(PlaneEntry::unpack(&raw).is_err(), "reserved byte {byte}");
        }
        let mut header = FixedHeader {
            version: VERSION_V2,
            file_size: 480,
            metadata_offset: 80,
            metadata_length: 8,
            index_offset: 88,
            index_length: 16,
            data_offset: 104,
            dictionary_offset: 0,
            dictionary_length: 0,
        }
        .pack();
        header[12] = 1;
        assert!(FixedHeader::unpack(&header).is_err(), "header flags");
        let mut index = IndexHeader { entry_count: 0 }.pack();
        index[12] = 1;
        assert!(IndexHeader::unpack(&index).is_err(), "index reserved");
    }


    #[test]
    fn v2_index_structures_round_trip() {
        let header = IndexHeaderV2 {
            tile_width: 48,
            tile_height: 52,
            group_count: 28,
            variable_count: 2,
            compression: Compression::Zstd,
            chunk_count: 23_520,
        };
        assert_eq!(IndexHeaderV2::unpack(&header.pack()).expect("valid v2 index header"), header);
        for predictor in [Predictor::Raw, Predictor::Previous] {
            let entry = VariableEntry { variable_id: 3, predictor };
            assert_eq!(VariableEntry::unpack(&entry.pack()).expect("valid variable entry"), entry);
        }
        let group = GroupEntry { first_frame: 120, frame_count: 6 };
        assert_eq!(GroupEntry::unpack(&group.pack()).expect("valid group entry"), group);
        let chunk = ChunkEntry { compressed_length: 5_120, crc32: 0xDEAD_BEEF };
        assert_eq!(ChunkEntry::unpack(&chunk.pack()).expect("valid chunk entry"), chunk);
    }

    /// ANCHOR made sense when a reader decoded two planes to reach a frame.
    /// A v2 chunk is decompressed whole, so allowing it would give one chunk
    /// two encodings.
    #[test]
    fn v2_rejects_anchor_and_zero_predictors() {
        for code in [Predictor::Anchor.code(), Predictor::Zero.code(), 9] {
            let raw = [7u8, code, 0, 0];
            assert!(VariableEntry::unpack(&raw).is_err(), "predictor {code}");
        }
        let mut raw = VariableEntry { variable_id: 1, predictor: Predictor::Raw }.pack();
        raw[2] = 1;
        assert!(VariableEntry::unpack(&raw).is_err(), "variable reserved");
        let mut raw = GroupEntry { first_frame: 0, frame_count: 1 }.pack();
        raw[3] = 1;
        assert!(GroupEntry::unpack(&raw).is_err(), "group reserved");
        for byte in [20, 21, 22, 23, 24, 31] {
            let mut raw = IndexHeaderV2 {
                tile_width: 1,
                tile_height: 1,
                group_count: 1,
                variable_count: 1,
                compression: Compression::Zstd,
                chunk_count: 1,
            }
            .pack();
            raw[byte] = 1;
            assert!(IndexHeaderV2::unpack(&raw).is_err(), "index reserved byte {byte}");
        }
    }

    /// 721 rows have no tidy power of two, so clipped tiles are the normal
    /// case rather than an edge: the last row and column must still describe
    /// exactly the cells that are left.
    #[test]
    fn tile_geometry_clips_the_last_row_and_column() {
        let geometry = TileGeometry::new(1440, 721, 48, 52).expect("valid geometry");
        assert_eq!((geometry.columns(), geometry.rows()), (30, 14));
        assert_eq!(geometry.count(), 420);
        assert_eq!(geometry.shape(0), (52, 48));
        // The last tile row covers 721 - 13 * 52 = 45 rows.
        assert_eq!(geometry.shape(geometry.count() - 1), (45, 48));
        assert_eq!(geometry.origin(geometry.count() - 1), (676, 1392));
        // Every cell lands in exactly one tile, and the areas sum to the grid.
        let area: u32 = (0..geometry.count())
            .map(|tile| {
                let (height, width) = geometry.shape(tile);
                height * width
            })
            .sum();
        assert_eq!(area, 1440 * 721);
        assert_eq!(geometry.tile_of(0, 0), Ok(0));
        assert_eq!(geometry.tile_of(720, 1439), Ok(419));
        assert!(geometry.tile_of(721, 0).is_err());
        assert!(TileGeometry::new(16, 8, 0, 4).is_err());
        assert!(TileGeometry::new(16, 8, 17, 4).is_err());
    }

    #[test]
    fn tile_rects_cover_the_cells_asked_for() {
        let geometry = TileGeometry::new(16, 8, 5, 4).expect("valid geometry");
        let rect = TileRect::covering(&geometry, 3, 4, 2, 7);
        assert_eq!(rect, TileRect { first_column: 0, first_row: 0, last_column: 2, last_row: 1 });
        for tile in 0..geometry.count() {
            let (row, column) = geometry.origin(tile);
            let (height, width) = geometry.shape(tile);
            let touched = row < 5 && row + height > 3 && column < 11 && column + width > 4;
            assert_eq!(rect.contains(&geometry, tile), touched, "tile {tile}");
        }
    }

    #[test]
    fn align8_rounds_up_and_saturates() {
        assert_eq!(align8(0), Some(0));
        assert_eq!(align8(1), Some(8));
        assert_eq!(align8(8), Some(8));
        assert_eq!(align8(81), Some(88));
        assert_eq!(align8(u64::MAX), None);
    }

    #[test]
    fn gcd_reduces_axis_units() {
        assert_eq!(gcd(3600, 0), 3600);
        assert_eq!(gcd(3600, 900), 900);
        assert_eq!(gcd(10, 4), 2);
    }
}
