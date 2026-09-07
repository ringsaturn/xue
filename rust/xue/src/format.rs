//! The Xue v1 container's byte layout, shared by the decoder and the encoder.
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
pub const VERSION: u16 = 1;
pub const HEADER_SIZE: usize = 80;
pub const INDEX_MAGIC: &[u8; 4] = b"IDX1";
pub const INDEX_HEADER_SIZE: usize = 16;
pub const INDEX_VERSION: u16 = 1;
pub const ENTRY_SIZE: usize = 40;
pub const NO_DEPENDENCY: u16 = 0xFFFF;
pub const FLAG_ZSTD_CHECKSUM: u8 = 0x01;
/// Safety limit for one decoded plane: 64M points.
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

/// The 80-byte fixed header's section geometry.
///
/// `magic`, `version`, `headerSize` and the reserved flags word are checked by
/// [`FixedHeader::unpack`] and written by [`FixedHeader::pack`], so they are
/// not fields. Whether the offsets are *consistent with each other* is
/// [`crate::decode`]'s question, not this module's.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FixedHeader {
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
        if read_u16(data, 8) != VERSION {
            return Err(err("unsupported Xue version"));
        }
        if read_u16(data, 10) as usize != HEADER_SIZE {
            return Err(err("headerSize must be 80 for v1"));
        }
        if read_u32(data, 12) != 0 {
            return Err(err("header flags must be 0 for v1"));
        }
        Ok(FixedHeader {
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
        buffer[8..10].copy_from_slice(&VERSION.to_le_bytes());
        buffer[10..12].copy_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
        // flags stay zero for v1.
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
