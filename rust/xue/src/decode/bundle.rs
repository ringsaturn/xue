//! The two public readers.
//!
//! [`Bundle`] owns a complete file. [`StreamingBundle`] owns only the
//! structural prefix and accepts payload bytes as range responses arrive.
//! Both are thin shells over the shared [`Core`]: the accessors below read
//! validated metadata, and `decode_frame` delegates outright.

use std::collections::HashMap;

use crate::decode::core::{Core, PayloadStore};
use crate::decode::structure::{checked_end, parse_structure, ParseMode};
use crate::format::{DecodeError, FrameRequest};

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

    /// Seconds per axis unit: a frame's valid time is
    /// `runTime + frameOffset * unitSeconds`.
    pub fn unit_seconds(&self) -> u32 {
        self.core.structure.metadata.unit_seconds
    }

    /// The materialized frame-offset axis, ascending — the keys every plane
    /// of every variable is addressed by.
    pub fn frame_offsets(&self) -> &[u16] {
        &self.core.structure.metadata.offsets
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

    /// Seconds per axis unit: a frame's valid time is
    /// `runTime + frameOffset * unitSeconds`.
    pub fn unit_seconds(&self) -> u32 {
        self.core.structure.metadata.unit_seconds
    }

    /// The materialized frame-offset axis, ascending — the keys every plane
    /// of every variable is addressed by.
    pub fn frame_offsets(&self) -> &[u16] {
        &self.core.structure.metadata.offsets
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
