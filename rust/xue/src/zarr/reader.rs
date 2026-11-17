//! One variable array, read without IO.
//!
//! The caller owns the requests: it asks where a shard's index and an inner
//! chunk sit, fetches those bytes however it likes, and hands them over. What
//! is held here is what the format says about the bytes — the array's layout
//! and each shard's verified index — so the browser, the API and a native
//! caller read a store through one implementation of it.

use std::collections::BTreeMap;

use super::{parse_shard_index, shard_of, ArrayLayout, ShardIndexEntry};
use crate::format::{err, DecodeError};

/// A variable array being read: its layout and the shard indices handed over
/// so far.
#[derive(Debug, Clone)]
pub struct ArrayReader {
    layout: ArrayLayout,
    /// Verified indices by shard. A map, not a list of `shard_count` slots:
    /// a store cut one shard per time chunk has as many shards as time chunks
    /// and a read touches few of them.
    indices: BTreeMap<u32, Vec<Option<ShardIndexEntry>>>,
}

impl ArrayReader {
    pub fn new(layout: ArrayLayout) -> ArrayReader {
        ArrayReader {
            layout,
            indices: BTreeMap::new(),
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
}
