//! Model-independent writing from quantized planes on disk.
//!
//! Callers own ingestion, codebooks and metadata. Xue owns temporal coding,
//! compression and the wire format. Only an anchor and one frame's encode
//! buffers are resident; the output itself is a temporary file.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use super::binformat::align8;
use super::convert::zstd_compress;
use super::errors::{EncodeError, Result};
use super::temporal::{anchor_hour, encode_residual, group_forecast_hours};
use crate::decode::metadata::parse_metadata;
use crate::format::{
    crc32, Compression, FixedHeader, IndexHeader, PlaneEntry, Predictor, ENTRY_SIZE,
    FLAG_ZSTD_CHECKSUM, HEADER_SIZE, INDEX_HEADER_SIZE, NO_DEPENDENCY,
};

fn io_error(error: std::io::Error) -> EncodeError {
    EncodeError::bundle(format!("quantized bundle I/O failed: {error}"))
}

fn read_plane(path: &Path, length: usize) -> Result<Vec<u8>> {
    let mut file = File::open(path).map_err(io_error)?;
    if file.metadata().map_err(io_error)?.len() != length as u64 {
        return Err(EncodeError::bundle(format!(
            "quantized plane length differs from grid: {path:?}"
        )));
    }
    let mut plane = vec![0; length];
    file.read_exact(&mut plane).map_err(io_error)?;
    if file.read(&mut [0]).map_err(io_error)? != 0 {
        return Err(EncodeError::bundle(format!(
            "quantized plane grew while reading: {path:?}"
        )));
    }
    Ok(plane)
}

/// Write one variable from `(frame_offset, raw_u8_path)` pairs.
///
/// The metadata is preserved verbatim, validated by the reader and must
/// describe exactly these frames and one variable. `grouped` selects the
/// usual middle-anchor predictor; false writes independently decodable RAW
/// frames. Input files are borrowed and never removed. Success atomically
/// replaces `path` and returns its byte length; failure preserves it.
pub fn write_quantized_bundle(
    path: &Path,
    metadata_json: &str,
    frames: &[(u16, PathBuf)],
    grouped: bool,
    zstd_level: i32,
) -> Result<u64> {
    let metadata =
        parse_metadata(metadata_json.as_bytes()).map_err(|error| EncodeError::bundle(error.0))?;
    if metadata.variable_ids.len() != 1 {
        return Err(EncodeError::bundle(
            "quantized writer requires exactly one variable",
        ));
    }
    let paths: BTreeMap<u16, &Path> = frames
        .iter()
        .map(|(offset, path)| (*offset, path.as_path()))
        .collect();
    if paths.len() != frames.len() || paths.keys().copied().collect::<Vec<_>>() != metadata.offsets
    {
        return Err(EncodeError::bundle(
            "quantized frames must match the metadata axis exactly, without duplicates",
        ));
    }
    let offsets: Vec<i64> = metadata
        .offsets
        .iter()
        .map(|&offset| i64::from(offset))
        .collect();
    let groups = if grouped {
        group_forecast_hours(&offsets)?
    } else {
        offsets.iter().map(|&offset| vec![offset]).collect()
    };

    let index_offset = align8(HEADER_SIZE as u64 + metadata_json.len() as u64);
    let index_length = (INDEX_HEADER_SIZE + ENTRY_SIZE * frames.len()) as u64;
    let data_offset = align8(index_offset + index_length);
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent).map_err(io_error)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(io_error)?;
    temporary
        .seek(SeekFrom::Start(data_offset))
        .map_err(io_error)?;
    let mut cursor = data_offset;
    let mut entries = Vec::with_capacity(frames.len());
    for (group_id, group) in groups.iter().enumerate() {
        let anchor_offset = anchor_hour(group)? as u16;
        let anchor = read_plane(paths[&anchor_offset], metadata.plane_length as usize)?;
        let order = std::iter::once(anchor_offset).chain(
            group
                .iter()
                .map(|&offset| offset as u16)
                .filter(|&offset| offset != anchor_offset),
        );
        for offset in order {
            let current;
            let plane = if offset == anchor_offset {
                &anchor
            } else {
                current = read_plane(paths[&offset], metadata.plane_length as usize)?;
                &current
            };
            let residual;
            let payload = if offset == anchor_offset {
                plane
            } else {
                residual = encode_residual(plane, &anchor)?;
                &residual
            };
            let compressed = zstd_compress(payload, zstd_level)?;
            let compressed_length = u32::try_from(compressed.len())
                .map_err(|_| EncodeError::bundle("compressed plane exceeds u32"))?;
            entries.push(PlaneEntry {
                variable_id: metadata.variable_ids[0],
                predictor: if offset == anchor_offset {
                    Predictor::Raw
                } else {
                    Predictor::Anchor
                },
                compression: Compression::Zstd,
                flags: FLAG_ZSTD_CHECKSUM,
                frame_offset: offset,
                dependency_offset: if offset == anchor_offset {
                    NO_DEPENDENCY
                } else {
                    anchor_offset
                },
                group_id: if grouped { group_id as u16 } else { offset },
                compressed_length,
                data_offset: cursor,
                decoded_length: metadata.plane_length,
                crc32: crc32(plane),
                minimum_code: *plane.iter().min().expect("positive grid size"),
                maximum_code: *plane.iter().max().expect("positive grid size"),
            });
            temporary.write_all(&compressed).map_err(io_error)?;
            cursor += u64::from(compressed_length);
        }
    }

    let file_size = align8(cursor);
    let header = FixedHeader {
        file_size,
        metadata_offset: HEADER_SIZE as u64,
        metadata_length: metadata_json.len() as u64,
        index_offset,
        index_length,
        data_offset,
        dictionary_offset: 0,
        dictionary_length: 0,
    };
    let mut prefix = vec![0; data_offset as usize];
    prefix[..HEADER_SIZE].copy_from_slice(&header.pack());
    prefix[HEADER_SIZE..HEADER_SIZE + metadata_json.len()]
        .copy_from_slice(metadata_json.as_bytes());
    let index_start = index_offset as usize;
    prefix[index_start..index_start + INDEX_HEADER_SIZE].copy_from_slice(
        &IndexHeader {
            entry_count: entries.len() as u32,
        }
        .pack(),
    );
    entries.sort_by_key(|entry| entry.frame_offset);
    for (position, entry) in entries.iter().enumerate() {
        let start = index_start + INDEX_HEADER_SIZE + position * ENTRY_SIZE;
        prefix[start..start + ENTRY_SIZE].copy_from_slice(&entry.pack());
    }
    crate::StreamingBundle::open_prefix(&prefix).map_err(|error| EncodeError::bundle(error.0))?;
    temporary.seek(SeekFrom::Start(0)).map_err(io_error)?;
    temporary.write_all(&prefix).map_err(io_error)?;
    temporary.as_file().set_len(file_size).map_err(io_error)?;
    temporary.as_file().sync_all().map_err(io_error)?;
    temporary
        .persist(path)
        .map_err(|error| io_error(error.error))?;
    Ok(file_size)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Bundle, FrameRequest, StreamingBundle};

    fn metadata() -> String {
        serde_json::json!({
            "schemaVersion": 1,
            "grid": {"width": 256, "height": 1},
            "time": {"firstForecastHour": 0, "stepHours": 1, "frameCount": 8},
            "variables": [{"numericId": 48, "id": "custom", "quantization": {"nodataCode": 255}}]
        })
        .to_string()
    }

    fn frames(root: &Path) -> Vec<(u16, PathBuf)> {
        (0..8)
            .map(|offset| {
                let path = root.join(format!("{offset}.u8"));
                let bytes: Vec<u8> = (0u8..=255)
                    .map(|code| code.wrapping_add(offset as u8))
                    .collect();
                fs::write(&path, bytes).unwrap();
                (offset, path)
            })
            .collect()
    }

    #[test]
    fn raw_and_grouped_round_trip_through_full_and_range_readers() {
        let root = tempfile::tempdir().unwrap();
        let frames = frames(root.path());
        let output = root.path().join("nested/custom.xue");
        for grouped in [false, true] {
            let size = write_quantized_bundle(&output, &metadata(), &frames, grouped, 3).unwrap();
            let bytes = fs::read(&output).unwrap();
            assert_eq!(size, bytes.len() as u64);
            let header = FixedHeader::unpack(&bytes).unwrap();
            let mut full = Bundle::open(&bytes).unwrap();
            let mut range =
                StreamingBundle::open_prefix(&bytes[..header.data_offset as usize]).unwrap();
            assert_eq!(full.metadata_json(), metadata());
            for &(offset, ref path) in frames.iter().rev() {
                let request = FrameRequest {
                    variable_id: 48,
                    frame_offset: offset,
                };
                if let Some((start, end)) = range.missing_group_span(request).unwrap() {
                    range
                        .insert_range(start, &bytes[start as usize..end as usize])
                        .unwrap();
                }
                let expected = fs::read(path).unwrap();
                assert_eq!(full.decode_frame(request).unwrap(), expected);
                assert_eq!(range.decode_frame(request).unwrap(), expected);
                full.clear_cache();
                range.clear_cache();
                let start =
                    header.index_offset as usize + INDEX_HEADER_SIZE + ENTRY_SIZE * offset as usize;
                let entry = PlaneEntry::unpack(&bytes[start..start + ENTRY_SIZE]).unwrap();
                assert_eq!(entry.flags, FLAG_ZSTD_CHECKSUM);
                assert_eq!(
                    entry.predictor == Predictor::Anchor,
                    grouped && offset != 3 && offset != 7
                );
            }
        }
    }

    #[test]
    fn late_input_failure_preserves_output_and_removes_temporary_file() {
        let root = tempfile::tempdir().unwrap();
        let frames = frames(root.path());
        let output = root.path().join("custom.xue");
        fs::write(&output, b"previous published bundle").unwrap();
        fs::write(&frames[6].1, [0u8]).unwrap();
        let error = write_quantized_bundle(&output, &metadata(), &frames, true, 3).unwrap_err();
        assert!(error.to_string().contains("plane length"));
        assert_eq!(fs::read(&output).unwrap(), b"previous published bundle");
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 9);
    }

    #[test]
    fn rejects_missing_duplicate_and_mismatched_frame_offsets() {
        let root = tempfile::tempdir().unwrap();
        let frames = frames(root.path());
        let output = root.path().join("custom.xue");
        let mut duplicate = frames.clone();
        duplicate[7] = duplicate[6].clone();
        let mut wrong_offset = frames.clone();
        wrong_offset[7].0 = 9;
        for invalid in [&frames[..7], &duplicate, &wrong_offset] {
            let error = write_quantized_bundle(&output, &metadata(), invalid, true, 3).unwrap_err();
            assert!(error.to_string().contains("match the metadata axis"));
            assert!(!output.exists());
        }
        let invalid_metadata = metadata().replace("\"schemaVersion\":1", "\"schemaVersion\":99");
        assert!(write_quantized_bundle(&output, &invalid_metadata, &frames, true, 3).is_err());
        assert!(!output.exists());
    }
}
