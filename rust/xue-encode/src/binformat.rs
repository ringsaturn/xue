//! Xue v1 container serialization — the port of `xue/binformat.py`'s writer.
//!
//! `docs/format.md` is the normative spec; this module produces complete
//! files. Reading back is the decoder crate's job (`xue`), which is exactly
//! how the Python encoder verifies what it writes.

use std::fs;
use std::io::Write;
use std::path::Path;

use crate::errors::{EncodeError, Result};

pub const MAGIC: &[u8; 8] = b"XUE\0\0\0\0\0";
pub const VERSION: u16 = 1;
pub const HEADER_SIZE: u64 = 80;
pub const INDEX_MAGIC: &[u8; 4] = b"IDX1";
pub const INDEX_HEADER_SIZE: u64 = 16;
pub const ENTRY_SIZE: u64 = 40;
pub const INDEX_VERSION: u16 = 1;
pub const NO_DEPENDENCY: u16 = 0xFFFF;

pub const PREDICTOR_RAW: u8 = 0;
pub const PREDICTOR_ANCHOR: u8 = 1;

pub const COMPRESSION_ZSTD: u8 = 1;

pub const FLAG_ZSTD_CHECKSUM: u8 = 0x01;

/// The coarsest time-axis unit, and the only one schema versions 1 and 2 can
/// describe. A schemaVersion 3 axis names its own unit, which must divide it.
pub const HOUR_SECONDS: i64 = 3600;

pub fn align8(value: u64) -> u64 {
    value.div_ceil(8) * 8
}

pub fn crc32_plane(plane: &[u8]) -> u32 {
    let mut hasher = crc32fast::Hasher::new();
    hasher.update(plane);
    hasher.finalize()
}

#[derive(Debug, Clone, Copy)]
pub struct PlaneEntry {
    pub variable_id: u8,
    pub predictor: u8,
    pub compression: u8,
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
    fn pack(&self) -> [u8; ENTRY_SIZE as usize] {
        let mut buffer = [0u8; ENTRY_SIZE as usize];
        buffer[0] = self.variable_id;
        buffer[1] = self.predictor;
        buffer[2] = self.compression;
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

/// One payload in physical file order, before offsets are assigned.
pub struct PlanePayload {
    pub entry: PlaneEntry,
    pub payload: Vec<u8>,
}

/// Assemble a complete Xue v1 file and publish it atomically.
///
/// `metadata_json` is the already-serialized metadata block: the encoder emits
/// it with `serde_json`'s compact separators so it matches the Python writer
/// byte for byte.
pub fn write_bundle(path: &Path, metadata_json: &str, planes: &[PlanePayload]) -> Result<Vec<u8>> {
    let metadata_bytes = metadata_json.as_bytes();
    let metadata_offset = HEADER_SIZE;
    let index_offset = align8(metadata_offset + metadata_bytes.len() as u64);
    let index_length = INDEX_HEADER_SIZE + ENTRY_SIZE * planes.len() as u64;
    // No production build embeds a dictionary; the section stays reserved.
    let dictionary_offset = 0u64;
    let dictionary_length = 0u64;
    let data_offset = align8(index_offset + index_length);

    let mut entries: Vec<PlaneEntry> = Vec::with_capacity(planes.len());
    let mut cursor = data_offset;
    for plane in planes {
        if plane.entry.compressed_length as usize != plane.payload.len() {
            return Err(EncodeError::bundle(
                "entry compressedLength does not match payload",
            ));
        }
        let mut entry = plane.entry;
        entry.data_offset = cursor;
        entries.push(entry);
        cursor += plane.payload.len() as u64;
    }
    let file_size = align8(cursor);

    let mut sorted = entries.clone();
    sorted.sort_by_key(|entry| (entry.variable_id, entry.frame_offset));
    if sorted
        .windows(2)
        .any(|pair| (pair[0].variable_id, pair[0].frame_offset) == (pair[1].variable_id, pair[1].frame_offset))
    {
        return Err(EncodeError::bundle(
            "duplicate (variableId, frameOffset) entries",
        ));
    }

    let mut output = vec![0u8; file_size as usize];
    output[0..8].copy_from_slice(MAGIC);
    output[8..10].copy_from_slice(&VERSION.to_le_bytes());
    output[10..12].copy_from_slice(&(HEADER_SIZE as u16).to_le_bytes());
    // flags stay zero for v1.
    output[16..24].copy_from_slice(&file_size.to_le_bytes());
    output[24..32].copy_from_slice(&metadata_offset.to_le_bytes());
    output[32..40].copy_from_slice(&(metadata_bytes.len() as u64).to_le_bytes());
    output[40..48].copy_from_slice(&index_offset.to_le_bytes());
    output[48..56].copy_from_slice(&index_length.to_le_bytes());
    output[56..64].copy_from_slice(&data_offset.to_le_bytes());
    output[64..72].copy_from_slice(&dictionary_offset.to_le_bytes());
    output[72..80].copy_from_slice(&dictionary_length.to_le_bytes());

    let metadata_start = metadata_offset as usize;
    output[metadata_start..metadata_start + metadata_bytes.len()].copy_from_slice(metadata_bytes);

    let index_start = index_offset as usize;
    output[index_start..index_start + 4].copy_from_slice(INDEX_MAGIC);
    output[index_start + 4..index_start + 6].copy_from_slice(&(ENTRY_SIZE as u16).to_le_bytes());
    output[index_start + 6..index_start + 8].copy_from_slice(&INDEX_VERSION.to_le_bytes());
    output[index_start + 8..index_start + 12]
        .copy_from_slice(&(sorted.len() as u32).to_le_bytes());
    // reserved stays zero.
    for (position, entry) in sorted.iter().enumerate() {
        let start = index_start + INDEX_HEADER_SIZE as usize + position * ENTRY_SIZE as usize;
        output[start..start + ENTRY_SIZE as usize].copy_from_slice(&entry.pack());
    }

    let mut cursor = data_offset as usize;
    for plane in planes {
        output[cursor..cursor + plane.payload.len()].copy_from_slice(&plane.payload);
        cursor += plane.payload.len();
    }

    write_atomic(path, &output)?;
    Ok(output)
}

/// Write `bytes` to `path` through a sibling temporary file, fsynced before
/// the rename, exactly as the Python writer publishes a bundle.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| EncodeError::bundle(format!("cannot create {parent:?}: {error}")))?;
    }
    let mut temporary = path.as_os_str().to_owned();
    temporary.push(".tmp");
    let temporary = Path::new(&temporary);
    let mut handle = fs::File::create(temporary)
        .map_err(|error| EncodeError::bundle(format!("cannot create {temporary:?}: {error}")))?;
    handle
        .write_all(bytes)
        .and_then(|()| handle.sync_all())
        .map_err(|error| EncodeError::bundle(format!("cannot write {temporary:?}: {error}")))?;
    drop(handle);
    fs::rename(temporary, path)
        .map_err(|error| EncodeError::bundle(format!("cannot publish {path:?}: {error}")))
}
