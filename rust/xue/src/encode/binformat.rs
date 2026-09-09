//! Xue container v2 serialization — the port of `xuebuild/binformat.py`'s
//! writer.
//!
//! `docs/format.md` is the normative spec; this module produces complete
//! files. The byte layout itself lives in [`crate::format`], which the
//! decoder unpacks through, so this module only decides *where* each section
//! goes and hands the structures over to be packed. Reading back is the
//! decoder's job (`crate::decode`), which is exactly how the Python encoder
//! verifies what it writes.
//!
//! Only v2 is written. The decoder still reads v1 — published runs carry
//! those bytes — but nothing produces it, and the frozen corpus in
//! `tests/fixtures/v1/` is committed rather than regenerated, so a v1 writer
//! here would be untested code with no caller.

use std::fs;
use std::io::Write;
use std::path::Path;

use crate::encode::errors::{EncodeError, Result};
use crate::format::{
    self, ChunkEntry, Compression, FixedHeader, GroupEntry, IndexHeaderV2, TileGeometry,
    VariableEntry, CHUNK_ENTRY_SIZE, GROUP_ENTRY_SIZE, HEADER_SIZE, INDEX_HEADER_SIZE_V2,
    VARIABLE_ENTRY_SIZE,
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

/// One chunk in physical file order, before offsets are assigned.
pub struct ChunkPayload {
    pub entry: ChunkEntry,
    pub payload: Vec<u8>,
}

/// The v2 index tables a bundle is written from.
pub struct ChunkTables {
    pub variables: Vec<VariableEntry>,
    pub groups: Vec<GroupEntry>,
    pub chunks: Vec<ChunkPayload>,
}

/// Assemble a complete Xue **v2** file and publish it atomically.
///
/// `chunks` is already in the physical order the spec fixes — group, then
/// tile row-major, then variable — because that order is what makes a group
/// one contiguous range. The writer only checks the caller produced the right
/// number of them, and the section geometry mirrors `write_bundle_v2` in
/// `xuebuild/binformat.py` byte for byte.
pub fn write_bundle_v2(
    path: &Path,
    metadata_json: &str,
    geometry: &TileGeometry,
    tables: &ChunkTables,
    frame_count: u32,
) -> Result<Vec<u8>> {
    let ChunkTables { variables, groups, chunks } = tables;
    if variables.windows(2).any(|pair| pair[0].variable_id >= pair[1].variable_id) {
        return Err(EncodeError::bundle(
            "variable table must be sorted and unique by variableId",
        ));
    }
    let mut cursor = 0u32;
    for group in groups {
        if u32::from(group.first_frame) != cursor || group.frame_count == 0 {
            return Err(EncodeError::bundle(
                "temporal groups must partition the axis in order",
            ));
        }
        cursor += u32::from(group.frame_count);
    }
    if cursor != frame_count {
        return Err(EncodeError::bundle(
            "temporal groups must cover the whole axis",
        ));
    }
    let expected = groups.len() * geometry.count() as usize * variables.len();
    if chunks.len() != expected {
        return Err(EncodeError::bundle(format!(
            "expected {expected} chunks, got {}",
            chunks.len()
        )));
    }

    let metadata_bytes = metadata_json.as_bytes();
    let metadata_offset = HEADER_SIZE as u64;
    let index_offset = align8(metadata_offset + metadata_bytes.len() as u64);
    let index_length = (INDEX_HEADER_SIZE_V2
        + VARIABLE_ENTRY_SIZE * variables.len()
        + GROUP_ENTRY_SIZE * groups.len()
        + CHUNK_ENTRY_SIZE * chunks.len()) as u64;
    let data_offset = align8(index_offset + index_length);

    let mut cursor = data_offset;
    for chunk in chunks {
        if chunk.payload.is_empty() {
            return Err(EncodeError::bundle("a chunk must have a payload"));
        }
        if chunk.entry.compressed_length as usize != chunk.payload.len() {
            return Err(EncodeError::bundle(
                "chunk compressedLength does not match payload",
            ));
        }
        cursor += chunk.payload.len() as u64;
    }
    let file_size = align8(cursor);

    let header = FixedHeader {
        version: format::VERSION_V2,
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
    output[index_start..index_start + INDEX_HEADER_SIZE_V2].copy_from_slice(
        &IndexHeaderV2 {
            tile_width: geometry.tile_width as u16,
            tile_height: geometry.tile_height as u16,
            group_count: groups.len() as u16,
            variable_count: variables.len() as u8,
            compression: Compression::Zstd,
            chunk_count: chunks.len() as u32,
        }
        .pack(),
    );
    let mut cursor = index_start + INDEX_HEADER_SIZE_V2;
    for variable in variables {
        output[cursor..cursor + VARIABLE_ENTRY_SIZE].copy_from_slice(&variable.pack());
        cursor += VARIABLE_ENTRY_SIZE;
    }
    for group in groups {
        output[cursor..cursor + GROUP_ENTRY_SIZE].copy_from_slice(&group.pack());
        cursor += GROUP_ENTRY_SIZE;
    }
    for chunk in chunks {
        output[cursor..cursor + CHUNK_ENTRY_SIZE].copy_from_slice(&chunk.entry.pack());
        cursor += CHUNK_ENTRY_SIZE;
    }

    let mut cursor = data_offset as usize;
    for chunk in chunks {
        output[cursor..cursor + chunk.payload.len()].copy_from_slice(&chunk.payload);
        cursor += chunk.payload.len();
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

#[cfg(test)]
mod tests {
    use super::align8;

    #[test]
    fn alignment_rounds_up_to_eight() {
        assert_eq!(align8(0), 0);
        assert_eq!(align8(1), 8);
        assert_eq!(align8(8), 8);
        assert_eq!(align8(81), 88);
    }
}
