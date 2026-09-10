//! The decode engine: payload residency, decompression and residual replay.
//!
//! One [`Core`] serves both readers and both container versions. What differs
//! between a whole file and a streamed one is only where a payload's bytes
//! come from, which is what [`PayloadStore`] abstracts; what differs between
//! v1 and v2 is what a payload *is*, which is what [`Layout`] abstracts. The
//! integrity checks above both are the same code either way.

use std::collections::HashMap;
use std::io::Read;

use crate::decode::structure::{Layout, Structure, TiledLayout};
use crate::format::{
    crc32, err, Compression, DecodeError, FrameRequest, PlaneEntry, Predictor, TileGeometry,
    TileRect,
};

/// Where payload bytes live: the whole file, or per-payload sparse buffers
/// filled in by [`crate::StreamingBundle::insert_range`].
pub(crate) enum PayloadStore {
    Full(Vec<u8>),
    Sparse { payloads: Vec<Option<Vec<u8>>>, resident_bytes: u64 },
}

impl PayloadStore {
    /// One payload's compressed bytes, addressed by its position in the index
    /// and its byte span in the file — the one thing v1 planes and v2 chunks
    /// have in common.
    pub(crate) fn payload(&self, position: usize, span: (u64, u64)) -> Result<&[u8], DecodeError> {
        match self {
            PayloadStore::Full(data) => Ok(&data[span.0 as usize..span.1 as usize]),
            PayloadStore::Sparse { payloads, .. } => payloads[position]
                .as_deref()
                .ok_or_else(|| err("payload is not resident yet")),
        }
    }

    pub(crate) fn is_resident(&self, position: usize, span: (u64, u64)) -> bool {
        match self {
            PayloadStore::Full(_) => true,
            PayloadStore::Sparse { payloads, .. } => span.0 == span.1 || payloads[position].is_some(),
        }
    }
}

/// Shared decode engine over a [`Structure`] and a [`PayloadStore`].
pub(crate) struct Core {
    pub(crate) structure: Structure,
    pub(crate) store: PayloadStore,
    /// v1: RAW base planes along a dependency chain.
    pub(crate) anchor_cache: HashMap<FrameRequest, Vec<u8>>,
    /// v2: reconstructed chunks of the group currently being read. Scrubbing
    /// inside a group is then free; moving to another group drops the lot,
    /// which keeps residency at one group's tiles rather than the file's.
    pub(crate) chunk_cache: HashMap<usize, Vec<u8>>,
    cached_group: Option<u32>,
    pub(crate) output: Vec<u8>,
}

impl Core {
    pub(crate) fn new(structure: Structure, store: PayloadStore) -> Self {
        Core {
            structure,
            store,
            anchor_cache: HashMap::new(),
            chunk_cache: HashMap::new(),
            cached_group: None,
            output: Vec::new(),
        }
    }

    pub(crate) fn clear_cache(&mut self) {
        self.anchor_cache.clear();
        self.chunk_cache.clear();
        self.cached_group = None;
    }

    /// Decompress one payload to exactly `expected` bytes.
    ///
    /// The output length is checked against what the structure says it must
    /// be, and the decoder is capped one byte above it, so a hostile frame
    /// can neither over-allocate nor run long.
    fn decompress(
        &self,
        position: usize,
        span: (u64, u64),
        compression: Compression,
        expected: usize,
    ) -> Result<Vec<u8>, DecodeError> {
        let raw = self.store.payload(position, span)?;
        match compression {
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

    // -- v1: plane-major -----------------------------------------------------

    fn plane_entry(&self, request: FrameRequest) -> Result<(usize, PlaneEntry), DecodeError> {
        let Layout::Planes(planes) = &self.structure.layout else {
            return Err(err("not a plane-major file"));
        };
        let position = planes.entry_position(request)?;
        Ok((position, planes.entries[position]))
    }

    fn decompress_plane(&self, position: usize, entry: &PlaneEntry) -> Result<Vec<u8>, DecodeError> {
        let span = (entry.data_offset, entry.data_offset + u64::from(entry.compressed_length));
        self.decompress(position, span, entry.compression, entry.decoded_length as usize)
    }

    fn checked_plane(&self, entry: &PlaneEntry, plane: Vec<u8>) -> Result<Vec<u8>, DecodeError> {
        if crc32(&plane) != entry.crc32 {
            return Err(err(format!(
                "plane CRC32 mismatch for variable {} hour {}",
                entry.variable_id, entry.frame_offset
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
        let (position, entry) = self.plane_entry(request)?;
        let plane = match entry.predictor {
            Predictor::Zero => vec![0u8; entry.decoded_length as usize],
            Predictor::Raw => self.decompress_plane(position, &entry)?,
            _ => return Err(err("dependency chain base must be RAW or ZERO")),
        };
        self.checked_plane(&entry, plane)
    }

    fn decode_frame_v1(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        let Layout::Planes(planes) = &self.structure.layout else {
            return Err(err("not a plane-major file"));
        };
        let chain = planes.dependency_chain(request, self.structure.metadata.frame_count)?;
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
            let (position, entry) = self.plane_entry(*link)?;
            let residual = self.decompress_plane(position, &entry)?;
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

    // -- v2: tiled -----------------------------------------------------------

    pub(crate) fn tiled(&self) -> Result<&TiledLayout, DecodeError> {
        match &self.structure.layout {
            Layout::Tiles(tiles) => Ok(tiles),
            Layout::Planes(_) => Err(err("not a tiled file")),
        }
    }

    /// The file's tiling, or `None` for a plane-major v1 file.
    pub(crate) fn tile_geometry(&self) -> Option<TileGeometry> {
        match &self.structure.layout {
            Layout::Tiles(tiles) => Some(tiles.geometry),
            Layout::Planes(_) => None,
        }
    }

    fn tiles(&self) -> Result<&TiledLayout, DecodeError> {
        self.tiled()
    }

    /// The frame's index on the axis, and the group holding it.
    fn frame_position(&self, frame_offset: u16) -> Result<(u32, u32), DecodeError> {
        let index = self
            .structure
            .metadata
            .offsets
            .binary_search(&frame_offset)
            .map_err(|_| err("no plane for the requested variable and forecast hour"))?;
        Ok(self.tiles()?.frame_group[index])
    }

    /// One chunk, decompressed and its residual chain replayed.
    fn build_chunk(&self, position: usize) -> Result<Vec<u8>, DecodeError> {
        let layout = self.tiles()?;
        let variable_count = layout.variables.len();
        let group = (position / (layout.geometry.count() as usize * variable_count)) as u32;
        let tile = (position / variable_count % layout.geometry.count() as usize) as u32;
        let variable = layout.variables[position % variable_count];
        let (height, width) = layout.geometry.shape(tile);
        let frames = layout.groups[group as usize].frame_count as usize;
        let expected = layout.decoded_length(group, tile);
        let mut chunk = self.decompress(
            position,
            layout.chunk_span(position),
            layout.compression,
            expected,
        )?;
        if variable.predictor == Predictor::Previous {
            // A group's frames are contiguous on the axis, so the chain never
            // leaves the chunk: each frame is a modulo-256 residual against
            // the frame reconstructed just before it.
            let stride = height as usize * width as usize;
            for frame in 1..frames {
                let (previous, current) = chunk.split_at_mut(frame * stride);
                let previous = &previous[(frame - 1) * stride..];
                for (target, base) in current[..stride].iter_mut().zip(previous.iter()) {
                    *target = target.wrapping_add(*base);
                }
            }
        }
        if crc32(&chunk) != layout.chunks[position].crc32 {
            return Err(err(format!("chunk CRC32 mismatch at position {position}")));
        }
        Ok(chunk)
    }

    /// Reconstruct a chunk into the group-local cache if it is not there yet.
    fn ensure_chunk(&mut self, group: u32, position: usize) -> Result<(), DecodeError> {
        if self.cached_group != Some(group) {
            self.chunk_cache.clear();
            self.cached_group = Some(group);
        }
        if self.chunk_cache.contains_key(&position) {
            return Ok(());
        }
        let chunk = self.build_chunk(position)?;
        self.chunk_cache.insert(position, chunk);
        Ok(())
    }

    /// Assemble a frame from the chunks of its group.
    ///
    /// `tiles` restricts the work to the tiles a viewport covers; cells no
    /// tile covered are left as they were, so a caller drawing a partial
    /// plane must know which rectangle it asked for.
    fn decode_frame_v2(
        &mut self,
        request: FrameRequest,
        tiles: Option<TileRect>,
    ) -> Result<&[u8], DecodeError> {
        let (group, frame_in_group) = self.frame_position(request.frame_offset)?;
        let variable_position = self.tiles()?.variable_position(request.variable_id)?;
        let plane_length = self.structure.metadata.plane_length as usize;
        if self.output.len() != plane_length {
            self.output = vec![0u8; plane_length];
        }
        let grid_width = self.structure.metadata.width as usize;
        let tile_count = self.tiles()?.geometry.count();
        for tile in 0..tile_count {
            let geometry = self.tiles()?.geometry;
            if tiles.is_some_and(|rect| !rect.contains(&geometry, tile)) {
                continue;
            }
            let position = self.tiles()?.chunk_position(group, tile, variable_position);
            self.ensure_chunk(group, position)?;
            let (height, width) = geometry.shape(tile);
            let (origin_row, origin_column) = geometry.origin(tile);
            let stride = height as usize * width as usize;
            let chunk = &self.chunk_cache[&position];
            let block = &chunk[frame_in_group as usize * stride..][..stride];
            for row in 0..height as usize {
                let target = (origin_row as usize + row) * grid_width + origin_column as usize;
                self.output[target..target + width as usize]
                    .copy_from_slice(&block[row * width as usize..][..width as usize]);
            }
        }
        Ok(&self.output)
    }

    /// One cell's code on every frame of the axis.
    ///
    /// The cost is one chunk per group of the single tile holding the cell,
    /// independent of how many frames the axis has — which is the whole point
    /// of the tiled container. Chunks are read straight through rather than
    /// cached: each is touched once.
    pub(crate) fn decode_series(&mut self, variable_id: u8, column: u32, row: u32) -> Result<Vec<u8>, DecodeError> {
        let layout = self.tiles()?;
        let variable_position = layout.variable_position(variable_id)?;
        let tile = layout.geometry.tile_of(row, column)?;
        let (origin_row, origin_column) = layout.geometry.origin(tile);
        let (_, width) = layout.geometry.shape(tile);
        let cell = (row - origin_row) as usize * width as usize + (column - origin_column) as usize;
        let mut series = Vec::with_capacity(self.structure.metadata.frame_count as usize);
        for group in 0..layout.groups.len() {
            let layout = self.tiles()?;
            let frames = layout.groups[group].frame_count as usize;
            let stride = layout.decoded_length(group as u32, tile) / frames;
            let position = layout.chunk_position(group as u32, tile, variable_position);
            let chunk = self.build_chunk(position)?;
            series.extend((0..frames).map(|frame| chunk[frame * stride + cell]));
        }
        Ok(series)
    }

    // -- dispatch ------------------------------------------------------------

    /// Decode one whole frame into an internal buffer and return it.
    pub(crate) fn decode_frame(&mut self, request: FrameRequest) -> Result<&[u8], DecodeError> {
        self.decode_frame_tiles(request, None)
    }

    pub(crate) fn decode_frame_tiles(
        &mut self,
        request: FrameRequest,
        tiles: Option<TileRect>,
    ) -> Result<&[u8], DecodeError> {
        match self.structure.layout {
            Layout::Planes(_) => {
                if tiles.is_some() {
                    return Err(err("a plane-major file has no tiles"));
                }
                self.decode_frame_v1(request)
            }
            Layout::Tiles(_) => self.decode_frame_v2(request, tiles),
        }
    }
}
