//! The two public readers.
//!
//! [`Bundle`] owns a complete file. [`StreamingBundle`] owns only the
//! structural prefix and accepts payload bytes as range responses arrive.
//! Both are thin shells over the shared [`Core`]: the accessors below read
//! validated metadata, and the decode calls delegate outright.
//!
//! Both read either container version. The tiled calls — a frame restricted
//! to a tile rectangle, a cell's series, the spans either of them still needs
//! — are the ones v2 adds; on a v1 file they report that the file has no
//! tiles rather than pretending otherwise.


use crate::decode::core::{Core, PayloadStore};
use crate::decode::structure::{checked_end, parse_structure, Layout, ParseMode};
use crate::format::{err, DecodeError, FrameRequest, TileGeometry, TileRect};

pub struct Bundle {
    core: Core,
}

impl Bundle {
    /// Parse and fully validate a bundle, copying the bytes into owned memory.
    pub fn open(bytes: &[u8]) -> Result<Self, DecodeError> {
        let structure = parse_structure(bytes, ParseMode::FullFile)?;
        Ok(Bundle { core: Core::new(structure, PayloadStore::Full(bytes.to_vec())) })
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
        self.core.clear_cache();
    }

    /// The file's tiling, or `None` for a plane-major v1 file.
    pub fn tile_geometry(&self) -> Option<TileGeometry> {
        self.core.tile_geometry()
    }

    pub fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        self.core.decode_frame(request)
    }

    /// Decode only the tiles a viewport covers, into the same whole-plane
    /// buffer. Cells outside the rectangle keep whatever the buffer held, so
    /// a renderer must draw only the rectangle it asked for.
    pub fn decode_frame_tiles(
        &mut self,
        request: FrameRequest,
        tiles: TileRect,
    ) -> Result<&[u8], DecodeError> {
        self.core.decode_frame_tiles(request, Some(tiles))
    }

    /// One cell's code on every frame of the axis, in axis order.
    pub fn decode_series(&mut self, variable_id: u8, column: u32, row: u32) -> Result<Vec<u8>, DecodeError> {
        self.core.decode_series(variable_id, column, row)
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
        let payloads = vec![None; structure.payload_count()];
        Ok(StreamingBundle {
            core: Core::new(structure, PayloadStore::Sparse { payloads, resident_bytes: 0 }),
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
        self.core.clear_cache();
    }

    /// The file's tiling, or `None` for a plane-major v1 file.
    pub fn tile_geometry(&self) -> Option<TileGeometry> {
        self.core.tile_geometry()
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
        (0..self.core.structure.payload_count())
            .map(|position| {
                let (start, end) = self.core.structure.payload_span(position);
                end - start
            })
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
    ///
    /// This is the global-view request in both versions, and it stays one
    /// range in both: v1 keeps a variable's group adjacent, v2 keeps a whole
    /// group — every tile, every variable — adjacent.
    pub fn missing_group_span(&self, request: FrameRequest) -> Result<Option<(u64, u64)>, DecodeError> {
        Ok(coalesce(self.missing_positions(request, None)?.into_iter().map(
            |position| self.core.structure.payload_span(position),
        )))
    }

    /// The byte spans a frame still needs, restricted to a tile rectangle and
    /// coalesced where the tiles are adjacent in the file — one span per tile
    /// row per group, since a row of tiles is contiguous.
    pub fn missing_spans(
        &self,
        request: FrameRequest,
        tiles: Option<TileRect>,
    ) -> Result<Vec<(u64, u64)>, DecodeError> {
        Ok(merge_adjacent(
            self.missing_positions(request, tiles)?
                .into_iter()
                .map(|position| self.core.structure.payload_span(position)),
        ))
    }

    /// The spans one cell's series still needs: one chunk per group, so at
    /// most one span per group and nothing proportional to the frame count.
    pub fn missing_series_spans(
        &self,
        variable_id: u8,
        column: u32,
        row: u32,
    ) -> Result<Vec<(u64, u64)>, DecodeError> {
        let tiles = self.core.tiled()?;
        let variable_position = tiles.variable_position(variable_id)?;
        let tile = tiles.geometry.tile_of(row, column)?;
        let spans = (0..tiles.groups.len())
            .map(|group| tiles.chunk_position(group as u32, tile, variable_position))
            .filter(|&position| {
                !self
                    .core
                    .store
                    .is_resident(position, self.core.structure.payload_span(position))
            })
            .map(|position| self.core.structure.payload_span(position));
        Ok(merge_adjacent(spans))
    }

    /// The index positions a frame needs and does not have yet, ascending.
    fn missing_positions(
        &self,
        request: FrameRequest,
        tiles: Option<TileRect>,
    ) -> Result<Vec<usize>, DecodeError> {
        let positions = match &self.core.structure.layout {
            Layout::Planes(planes) => {
                if tiles.is_some() {
                    return Err(err("a plane-major file has no tiles"));
                }
                let target = planes.entries[planes.entry_position(request)?];
                planes
                    .entries
                    .iter()
                    .enumerate()
                    .filter(|(_, member)| {
                        member.variable_id == target.variable_id && member.group_id == target.group_id
                    })
                    .map(|(position, _)| position)
                    .collect::<Vec<_>>()
            }
            Layout::Tiles(layout) => {
                let variable_position = layout.variable_position(request.variable_id)?;
                let (group, _) = layout.frame_group[self.core.structure.frame_index(request.frame_offset)?];
                (0..layout.geometry.count())
                    .filter(|&tile| tiles.is_none_or(|rect| rect.contains(&layout.geometry, tile)))
                    .map(|tile| layout.chunk_position(group, tile, variable_position))
                    .collect::<Vec<_>>()
            }
        };
        Ok(positions
            .into_iter()
            .filter(|&position| {
                !self
                    .core
                    .store
                    .is_resident(position, self.core.structure.payload_span(position))
            })
            .collect())
    }

    /// Store payload bytes covering `[offset, offset + bytes.len())` of the
    /// file. Every index entry whose payload lies fully inside the range
    /// becomes resident; partial overlaps are ignored.
    pub fn insert_range(&mut self, offset: u64, bytes: &[u8]) -> Result<(), DecodeError> {
        let end = checked_end(offset, bytes.len() as u64, self.core.structure.file_size, "inserted")?;
        let PayloadStore::Sparse { payloads, resident_bytes } = &mut self.core.store else {
            unreachable!("streaming bundles use a sparse store");
        };
        for position in 0..self.core.structure.payload_count() {
            if payloads[position].is_some() {
                continue;
            }
            let (start, stop) = self.core.structure.payload_span(position);
            if start == stop || start < offset || stop > end {
                continue;
            }
            let from = (start - offset) as usize;
            payloads[position] = Some(bytes[from..from + (stop - start) as usize].to_vec());
            *resident_bytes += stop - start;
        }
        Ok(())
    }

    pub fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        self.core.decode_frame(request)
    }

    /// Decode only the tiles a viewport covers; see [`Bundle::decode_frame_tiles`].
    pub fn decode_frame_tiles(
        &mut self,
        request: FrameRequest,
        tiles: TileRect,
    ) -> Result<&[u8], DecodeError> {
        self.core.decode_frame_tiles(request, Some(tiles))
    }

    /// One cell's code on every frame of the axis, in axis order.
    pub fn decode_series(&mut self, variable_id: u8, column: u32, row: u32) -> Result<Vec<u8>, DecodeError> {
        self.core.decode_series(variable_id, column, row)
    }
}

/// The one span covering every span given, or `None` when there are none.
fn coalesce(spans: impl Iterator<Item = (u64, u64)>) -> Option<(u64, u64)> {
    spans.reduce(|(low, high), (start, end)| (low.min(start), high.max(end)))
}

/// The given spans in ascending order, with touching ones joined. Chunks are
/// strictly adjacent in the file, so a run of neighbouring tiles collapses to
/// a single range request.
fn merge_adjacent(spans: impl Iterator<Item = (u64, u64)>) -> Vec<(u64, u64)> {
    let mut spans: Vec<(u64, u64)> = spans.collect();
    spans.sort_unstable();
    let mut merged: Vec<(u64, u64)> = Vec::with_capacity(spans.len());
    for (start, end) in spans {
        match merged.last_mut() {
            Some(last) if start <= last.1 => last.1 = last.1.max(end),
            _ => merged.push((start, end)),
        }
    }
    merged
}
