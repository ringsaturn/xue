//! One variable array, read without IO.
//!
//! The caller owns the requests: it asks where a shard's index and an inner
//! chunk sit, fetches those bytes however it likes, and hands them over. What
//! is held here is what the format says about the bytes — the array's layout,
//! each shard's verified index, and the decoded tiles of the time chunk being
//! read — so the browser, the API and a native caller read a store through
//! one implementation of it.
//!
//! A frame is read in three steps: [`ArrayReader::tiles_for`] names the tiles
//! it needs, the caller hands over the payload of each one not yet decoded
//! ([`ArrayReader::undecoded_tiles`], [`ArrayReader::feed_chunk`]), and
//! [`ArrayReader::assemble_frame`] returns the plane. Every plane is a new
//! buffer, so two frames being read at once never write into each other.

use std::collections::{BTreeMap, BTreeSet};

use super::{
    decode_inner_chunk, parse_shard_index, place_tile, shard_of, ArrayLayout, ShardIndexEntry,
};
use crate::format::{err, DecodeError, TileRect};

/// A variable array being read: its layout, the shard indices handed over so
/// far and the decoded tiles of one time chunk.
#[derive(Debug, Clone)]
pub struct ArrayReader {
    layout: ArrayLayout,
    /// Verified indices by shard. A map, not a list of `shard_count` slots:
    /// a store cut one shard per time chunk has as many shards as time chunks
    /// and a read touches few of them.
    indices: BTreeMap<u32, Vec<Option<ShardIndexEntry>>>,
    /// The time chunk whose tiles `decoded` holds.
    decoded_time_chunk: Option<u32>,
    /// Decoded inner chunks of that time chunk by tile, each at the full inner
    /// shape; `None` for a chunk the shard never held. One time chunk only:
    /// scrubbing inside it decodes nothing twice, and memory stays at one
    /// time chunk's tiles.
    decoded: BTreeMap<u32, Option<Vec<u8>>>,
}

impl ArrayReader {
    pub fn new(layout: ArrayLayout) -> ArrayReader {
        ArrayReader {
            layout,
            indices: BTreeMap::new(),
            decoded_time_chunk: None,
            decoded: BTreeMap::new(),
        }
    }

    pub fn layout(&self) -> &ArrayLayout {
        &self.layout
    }

    /// The shard a time chunk lives in.
    pub fn shard_of(&self, time_chunk: u32) -> Result<u32, DecodeError> {
        if time_chunk >= self.layout.time_chunks {
            return Err(err("time chunk is outside the array"));
        }
        Ok(shard_of(&self.layout, time_chunk).0)
    }

    pub fn has_shard_index(&self, shard: u32) -> bool {
        self.indices.contains_key(&shard)
    }

    /// Verify one shard's index — its length, its CRC-32C and every entry —
    /// and hold it. `bytes` is exactly the index, read from whichever end of
    /// the shard the layout names.
    pub fn set_shard_index(&mut self, shard: u32, bytes: &[u8]) -> Result<(), DecodeError> {
        if shard >= self.layout.shard_count {
            return Err(err("shard is outside the array"));
        }
        let index = parse_shard_index(bytes, self.layout.chunks_per_shard as usize)?;
        self.indices.insert(shard, index);
        Ok(())
    }

    /// Where one inner chunk sits in its shard, or `None` for a chunk the
    /// shard never held, which reads as the fill value. An error until the
    /// shard's index has been handed over.
    pub fn chunk_range(
        &self,
        time_chunk: u32,
        tile: u32,
    ) -> Result<Option<ShardIndexEntry>, DecodeError> {
        if tile >= self.layout.tile_count {
            return Err(err("tile is outside the grid"));
        }
        let shard = self.shard_of(time_chunk)?;
        let (_, position) = shard_of(&self.layout, time_chunk);
        let index = self
            .indices
            .get(&shard)
            .ok_or_else(|| err("shard index is not resident yet"))?;
        index
            .get((position + tile) as usize)
            .copied()
            .ok_or_else(|| err("tile is outside the shard"))
    }

    /// The tiles a list of tile rectangles names, ascending, or every tile
    /// when there is no list. Rectangles may overlap; a tile is named once.
    pub fn tiles_for(&self, rects: Option<&[TileRect]>) -> Result<Vec<u32>, DecodeError> {
        let Some(rects) = rects else {
            return Ok((0..self.layout.tile_count).collect());
        };
        let mut tiles = BTreeSet::new();
        for rect in rects {
            if rect.last_row >= self.layout.tile_rows || rect.last_column >= self.layout.tile_columns {
                return Err(err("tile rectangle is outside the grid"));
            }
            for row in rect.first_row..=rect.last_row {
                for column in rect.first_column..=rect.last_column {
                    tiles.insert(row * self.layout.tile_columns + column);
                }
            }
        }
        Ok(tiles.into_iter().collect())
    }

    /// Which of `tiles` are not decoded for `time_chunk`: the ones
    /// [`ArrayReader::feed_chunk`] still has to be given before a frame of
    /// that time chunk assembles over them.
    pub fn undecoded_tiles(&self, time_chunk: u32, tiles: &[u32]) -> Vec<u32> {
        if self.decoded_time_chunk != Some(time_chunk) {
            return tiles.to_vec();
        }
        tiles
            .iter()
            .copied()
            .filter(|tile| !self.decoded.contains_key(tile))
            .collect()
    }

    /// Decode one inner chunk and hold it. `payload` is the chunk's bytes as
    /// the shard holds them, or `None` for a chunk the shard never held.
    /// Moving to another time chunk drops the tiles of the one held.
    pub fn feed_chunk(
        &mut self,
        time_chunk: u32,
        tile: u32,
        payload: Option<&[u8]>,
    ) -> Result<(), DecodeError> {
        if time_chunk >= self.layout.time_chunks {
            return Err(err("time chunk is outside the array"));
        }
        if tile >= self.layout.tile_count {
            return Err(err("tile is outside the grid"));
        }
        let chunk = payload
            .map(|payload| decode_inner_chunk(&self.layout, payload))
            .transpose()?;
        if self.decoded_time_chunk != Some(time_chunk) {
            self.decoded.clear();
            self.decoded_time_chunk = Some(time_chunk);
        }
        self.decoded.insert(tile, chunk);
        Ok(())
    }

    /// One frame as a new plane of `height × width` codes. With `rects` only
    /// those tiles are written and every cell outside them is zero, which
    /// says nothing about the data there: a caller draws only what it asked
    /// for. An error unless every tile named has been fed for the frame's
    /// time chunk.
    pub fn assemble_frame(
        &self,
        frame_index: u32,
        rects: Option<&[TileRect]>,
    ) -> Result<Vec<u8>, DecodeError> {
        if frame_index >= self.layout.frame_count {
            return Err(err("frame is outside the array"));
        }
        let time_chunk = frame_index / self.layout.time_chunk;
        let tiles = self.tiles_for(rects)?;
        let held = (self.decoded_time_chunk == Some(time_chunk)).then_some(&self.decoded);
        // Sized by the layout, which holds a plane to the decoded size limit.
        let mut plane = vec![0u8; self.layout.height as usize * self.layout.width as usize];
        let frame_in_chunk = frame_index % self.layout.time_chunk;
        for tile in tiles {
            let chunk = held
                .and_then(|decoded| decoded.get(&tile))
                .ok_or_else(|| err("chunk is not resident yet"))?;
            place_tile(&mut plane, &self.layout, tile, chunk.as_deref(), frame_in_chunk)?;
        }
        Ok(plane)
    }

    /// Drop the decoded tiles; the shard indices stay.
    pub fn clear_cache(&mut self) {
        self.decoded.clear();
        self.decoded_time_chunk = None;
    }
}
