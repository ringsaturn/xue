//! Xue v1 container serialization — the port of `xuebuild/binformat.py`'s writer.
//!
//! `docs/format.md` is the normative spec; this module produces complete
//! files. The byte layout itself lives in [`crate::format`], which the
//! decoder unpacks through, so this module only decides *where* each section
//! goes and hands the structures over to be packed. Reading back is the
//! decoder's job (`crate::decode`), which is exactly how the Python encoder
//! verifies what it writes.

use std::fs;
use std::io::Write;
use std::path::Path;

use crate::encode::errors::{EncodeError, Result};
use crate::format::{
    self, FixedHeader, IndexHeader, PlaneEntry, ENTRY_SIZE, HEADER_SIZE, INDEX_HEADER_SIZE,
};

/// [`crate::format::HOUR_SECONDS`] in the signed arithmetic the encoder's time
/// axes are carried in.
pub const HOUR_SECONDS: i64 = format::HOUR_SECONDS as i64;

pub fn align8(value: u64) -> u64 {
    format::align8(value).expect("section offsets are far below u64::MAX")
}

pub fn crc32_plane(plane: &[u8]) -> u32 {
    format::crc32(plane)
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
    let metadata_offset = HEADER_SIZE as u64;
    let index_offset = align8(metadata_offset + metadata_bytes.len() as u64);
    let index_length = (INDEX_HEADER_SIZE + ENTRY_SIZE * planes.len()) as u64;
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

    let mut sorted = entries;
    sorted.sort_by_key(|entry| (entry.variable_id, entry.frame_offset));
    if sorted
        .windows(2)
        .any(|pair| (pair[0].variable_id, pair[0].frame_offset) == (pair[1].variable_id, pair[1].frame_offset))
    {
        return Err(EncodeError::bundle(
            "duplicate (variableId, frameOffset) entries",
        ));
    }

    let header = FixedHeader {
        file_size,
        metadata_offset,
        metadata_length: metadata_bytes.len() as u64,
        index_offset,
        index_length,
        data_offset,
        // No production build embeds a dictionary; the section stays reserved.
        dictionary_offset: 0,
        dictionary_length: 0,
    };

    let mut output = vec![0u8; file_size as usize];
    output[0..HEADER_SIZE].copy_from_slice(&header.pack());

    let metadata_start = metadata_offset as usize;
    output[metadata_start..metadata_start + metadata_bytes.len()].copy_from_slice(metadata_bytes);

    let index_start = index_offset as usize;
    output[index_start..index_start + INDEX_HEADER_SIZE]
        .copy_from_slice(&IndexHeader { entry_count: sorted.len() as u32 }.pack());
    for (position, entry) in sorted.iter().enumerate() {
        let start = index_start + INDEX_HEADER_SIZE + position * ENTRY_SIZE;
        output[start..start + ENTRY_SIZE].copy_from_slice(&entry.pack());
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
