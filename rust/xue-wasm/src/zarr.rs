//! The Zarr store's read side for the browser (`docs/zarr-profile.md`).
//!
//! The Worker keeps the network — requests, range merging, the compressed
//! chunks it holds and their budget — and asks here what the format says:
//! which block of the group is a bundle's metadata, how an array is cut,
//! where a shard's index and an inner chunk sit. Nothing in this module
//! fetches or awaits; every call is synchronous over bytes already local.

use wasm_bindgen::prelude::*;
use xue::zarr::{self, ArrayReader, IndexLocation};

fn js_error(error: xue::DecodeError) -> JsError {
    JsError::new(&error.0)
}

/// A store's group document read as one bundle's.
#[wasm_bindgen]
pub struct WasmZarrGroup {
    profile: u32,
    metadata_json: String,
}

#[wasm_bindgen]
impl WasmZarrGroup {
    /// The store's `xue_profile`: 1 for one bundle per store, 2 for a store
    /// shared by every bundle of a tier.
    #[wasm_bindgen(getter)]
    pub fn profile(&self) -> u32 {
        self.profile
    }

    /// The bundle's metadata block as compact JSON, for the shell's own
    /// metadata validator. Its keys come out sorted.
    #[wasm_bindgen(getter, js_name = metadataJson)]
    pub fn metadata_json(&self) -> String {
        self.metadata_json.clone()
    }
}

/// Read a grid store's root `zarr.json` as `bundle`'s: `attributes.xue` on a
/// one-bundle store, whatever `bundle` says, and
/// `attributes.xue_bundles[bundle]` on a shared one, which is refused without
/// a bundle named. The block itself is left to the caller's validator.
#[wasm_bindgen(js_name = parseZarrGroup)]
pub fn parse_zarr_group(text: &str, bundle: Option<String>) -> Result<WasmZarrGroup, JsError> {
    let (profile, metadata_json) =
        zarr::group_metadata_json(text, bundle.as_deref()).map_err(js_error)?;
    Ok(WasmZarrGroup {
        profile,
        metadata_json,
    })
}

/// The byte length of a shard index naming `chunkCount` inner chunks.
#[wasm_bindgen(js_name = shardIndexLength)]
pub fn shard_index_length(chunk_count: u32) -> f64 {
    // In f64, since the product can pass a 32-bit `usize`.
    16.0 * f64::from(chunk_count) + 4.0
}

/// One shard's index, verified (length, CRC-32C, every entry), flattened as
/// `[offset, length, ...]` with one pair per inner chunk. A chunk the shard
/// never held is the pair `[-1, -1]`. For a store whose array document is
/// read elsewhere; a variable array is read through [`WasmZarrArray`].
#[wasm_bindgen(js_name = parseShardIndex)]
pub fn parse_shard_index(bytes: &[u8], chunk_count: u32) -> Result<Vec<f64>, JsError> {
    let index = zarr::parse_shard_index(bytes, chunk_count as usize).map_err(js_error)?;
    Ok(index
        .into_iter()
        .flat_map(|entry| match entry {
            // Offsets cross as f64: the parser holds them to 2^53.
            Some(entry) => [entry.offset as f64, entry.length as f64],
            None => [-1.0, -1.0],
        })
        .collect())
}

/// One variable array of a store: its layout and the shard indices handed
/// over so far.
#[wasm_bindgen]
pub struct WasmZarrArray {
    inner: ArrayReader,
    bundle: Option<String>,
}

#[wasm_bindgen]
impl WasmZarrArray {
    /// Read the array's `zarr.json`, held to the profile's codec chain and
    /// chunk rules.
    #[wasm_bindgen(js_name = fromMetadata)]
    pub fn from_metadata(text: &str) -> Result<WasmZarrArray, JsError> {
        let document = zarr::parse_array_document(text).map_err(js_error)?;
        Ok(WasmZarrArray {
            inner: ArrayReader::new(document.layout),
            bundle: document.bundle,
        })
    }

    #[wasm_bindgen(getter, js_name = frameCount)]
    pub fn frame_count(&self) -> u32 {
        self.inner.layout().frame_count
    }

    #[wasm_bindgen(getter)]
    pub fn height(&self) -> u32 {
        self.inner.layout().height
    }

    #[wasm_bindgen(getter)]
    pub fn width(&self) -> u32 {
        self.inner.layout().width
    }

    /// Frames per inner chunk.
    #[wasm_bindgen(getter, js_name = timeChunk)]
    pub fn time_chunk(&self) -> u32 {
        self.inner.layout().time_chunk
    }

    #[wasm_bindgen(getter, js_name = timeChunks)]
    pub fn time_chunks(&self) -> u32 {
        self.inner.layout().time_chunks
    }

    #[wasm_bindgen(getter, js_name = tileHeight)]
    pub fn tile_height(&self) -> u32 {
        self.inner.layout().tile_height
    }

    #[wasm_bindgen(getter, js_name = tileWidth)]
    pub fn tile_width(&self) -> u32 {
        self.inner.layout().tile_width
    }

    #[wasm_bindgen(getter, js_name = tileRows)]
    pub fn tile_rows(&self) -> u32 {
        self.inner.layout().tile_rows
    }

    #[wasm_bindgen(getter, js_name = tileColumns)]
    pub fn tile_columns(&self) -> u32 {
        self.inner.layout().tile_columns
    }

    #[wasm_bindgen(getter, js_name = tileCount)]
    pub fn tile_count(&self) -> u32 {
        self.inner.layout().tile_count
    }

    /// Frames per shard: the outer chunk's time extent.
    #[wasm_bindgen(getter, js_name = shardFrames)]
    pub fn shard_frames(&self) -> u32 {
        self.inner.layout().shard_frames
    }

    #[wasm_bindgen(getter, js_name = shardCount)]
    pub fn shard_count(&self) -> u32 {
        self.inner.layout().shard_count
    }

    /// Inner chunks a shard's index names.
    #[wasm_bindgen(getter, js_name = chunksPerShard)]
    pub fn chunks_per_shard(&self) -> u32 {
        self.inner.layout().chunks_per_shard
    }

    /// The inner chain carries `xue.delta`: chunks are PREVIOUS residuals.
    #[wasm_bindgen(getter)]
    pub fn delta(&self) -> bool {
        self.inner.layout().delta
    }

    /// The index is a suffix of the shard; otherwise a prefix.
    #[wasm_bindgen(getter, js_name = indexAtEnd)]
    pub fn index_at_end(&self) -> bool {
        self.inner.layout().index_location == IndexLocation::End
    }

    #[wasm_bindgen(getter, js_name = fillValue)]
    pub fn fill_value(&self) -> u8 {
        self.inner.layout().fill_value
    }

    /// The bundle the array says it belongs to (`attributes.xue.bundle`,
    /// written on a shared store), or `undefined` where it declares none.
    #[wasm_bindgen(getter)]
    pub fn bundle(&self) -> Option<String> {
        self.bundle.clone()
    }

    /// The shard a time chunk lives in.
    #[wasm_bindgen(js_name = shardOf)]
    pub fn shard_of(&self, time_chunk: u32) -> Result<u32, JsError> {
        self.inner.shard_of(time_chunk).map_err(js_error)
    }

    /// The object holding one shard, relative to the store: `arrayId` is the
    /// variable's id, which names its array.
    #[wasm_bindgen(js_name = shardPath)]
    pub fn shard_path(&self, array_id: &str, shard: u32) -> String {
        zarr::shard_path(array_id, shard)
    }

    /// The byte length of one shard's index, at the end `indexAtEnd` names.
    #[wasm_bindgen(getter, js_name = shardIndexLength)]
    pub fn shard_index_length(&self) -> u32 {
        // The layout bounds it well under 2^32.
        zarr::shard_index_length(self.inner.layout().chunks_per_shard as usize) as u32
    }

    #[wasm_bindgen(js_name = hasShardIndex)]
    pub fn has_shard_index(&self, shard: u32) -> bool {
        self.inner.has_shard_index(shard)
    }

    /// Verify one shard's index and hold it.
    #[wasm_bindgen(js_name = setShardIndex)]
    pub fn set_shard_index(&mut self, shard: u32, bytes: &[u8]) -> Result<(), JsError> {
        self.inner.set_shard_index(shard, bytes).map_err(js_error)
    }

    /// `[offset, length]` of one inner chunk within its shard, or `undefined`
    /// for a chunk the shard never held, which reads as the fill value.
    /// Throws until the shard's index has been handed over.
    #[wasm_bindgen(js_name = chunkRange)]
    pub fn chunk_range(&self, time_chunk: u32, tile: u32) -> Result<Option<Box<[f64]>>, JsError> {
        let entry = self.inner.chunk_range(time_chunk, tile).map_err(js_error)?;
        Ok(entry.map(|entry| vec![entry.offset as f64, entry.length as f64].into_boxed_slice()))
    }
}
