//! A run's column store for the browser (`docs/zarr-profile.md`, "Column
//! store"), read by the reader the native side uses (`xue::column`).
//!
//! The Worker keeps the network and asks here what the format says. A cell's
//! column of one array is `shardOf` → `setShardIndex` once per shard →
//! `chunkRange` → `column` over that one chunk, and the cells of one block
//! (`blockShape`) are read out of it together with `cells`; every call is
//! synchronous over bytes already local.

use wasm_bindgen::prelude::*;
use xue::column::{ColumnArray, ColumnGroup, ColumnReader};
use xue::zarr::IndexLocation;

fn js_error(error: xue::DecodeError) -> JsError {
    JsError::new(&error.0)
}

/// A column store: its validated group and the shard indices handed over so
/// far. An array is named by its id in the `xue_columns` block.
#[wasm_bindgen]
pub struct WasmColumnStore {
    inner: ColumnReader,
}

impl WasmColumnStore {
    fn array(&self, id: &str) -> Result<&ColumnArray, JsError> {
        self.inner.group().array(id).map_err(js_error)
    }
}

#[wasm_bindgen]
impl WasmColumnStore {
    /// Read the store's root `zarr.json`: the `xue_columns` block and every
    /// array document it consolidates, which is the only document a reader
    /// fetches.
    #[wasm_bindgen(js_name = fromGroup)]
    pub fn from_group(text: &str) -> Result<WasmColumnStore, JsError> {
        ColumnGroup::parse(text)
            .map(|group| WasmColumnStore {
                inner: ColumnReader::new(group),
            })
            .map_err(js_error)
    }

    /// The `xue_columns` block as compact JSON. Its keys come out sorted.
    #[wasm_bindgen(getter, js_name = blockJson)]
    pub fn block_json(&self) -> String {
        self.inner.group().block_json.clone()
    }

    /// The arrays' ids, in the block's order.
    #[wasm_bindgen(js_name = arrayIds)]
    pub fn array_ids(&self) -> Vec<String> {
        let arrays = &self.inner.group().arrays;
        arrays.iter().map(|array| array.id.clone()).collect()
    }

    #[wasm_bindgen(js_name = frameCount)]
    pub fn frame_count(&self, id: &str) -> Result<u32, JsError> {
        Ok(self.array(id)?.layout.frame_count)
    }

    /// 1 on a surface array.
    #[wasm_bindgen(js_name = levelCount)]
    pub fn level_count(&self, id: &str) -> Result<u32, JsError> {
        Ok(self.array(id)?.layout.level_count)
    }

    /// The array's block, `[height, width]` cells: those of one block are one
    /// chunk.
    #[wasm_bindgen(js_name = blockShape)]
    pub fn block_shape(&self, id: &str) -> Result<Vec<u32>, JsError> {
        let layout = &self.array(id)?.layout;
        Ok(vec![layout.block_height, layout.block_width])
    }

    /// The shard a cell lies in.
    #[wasm_bindgen(js_name = shardOf)]
    pub fn shard_of(&self, id: &str, row: u32, column: u32) -> Result<u32, JsError> {
        self.array(id)?
            .layout
            .shard_of(row, column)
            .map_err(js_error)
    }

    /// The object holding one shard, relative to the store.
    #[wasm_bindgen(js_name = shardPath)]
    pub fn shard_path(&self, id: &str, shard: u32) -> Result<String, JsError> {
        Ok(self.array(id)?.shard_path(shard))
    }

    /// The byte length of one shard's index, at the end `indexAtEnd` names.
    #[wasm_bindgen(js_name = shardIndexLength)]
    pub fn shard_index_length(&self, id: &str) -> Result<u32, JsError> {
        // The layout bounds it well under 2^32.
        Ok(self.array(id)?.layout.index_length() as u32)
    }

    /// The index is a suffix of the shard; otherwise a prefix.
    #[wasm_bindgen(js_name = indexAtEnd)]
    pub fn index_at_end(&self, id: &str) -> Result<bool, JsError> {
        Ok(self.array(id)?.layout.index_location == IndexLocation::End)
    }

    #[wasm_bindgen(js_name = hasShardIndex)]
    pub fn has_shard_index(&self, id: &str, shard: u32) -> bool {
        self.inner.has_shard_index(id, shard)
    }

    /// Verify one shard's index and hold it.
    #[wasm_bindgen(js_name = setShardIndex)]
    pub fn set_shard_index(&mut self, id: &str, shard: u32, bytes: &[u8]) -> Result<(), JsError> {
        self.inner
            .set_shard_index(id, shard, bytes)
            .map_err(js_error)
    }

    /// `[offset, length]` of the chunk a cell's column is in, within the
    /// cell's shard, or `undefined` for one the shard never held, which reads
    /// as the fill value. Throws until the shard's index has been handed over.
    #[wasm_bindgen(js_name = chunkRange)]
    pub fn chunk_range(
        &self,
        id: &str,
        row: u32,
        column: u32,
    ) -> Result<Option<Box<[f64]>>, JsError> {
        let entry = self.inner.chunk_range(id, row, column).map_err(js_error)?;
        // Offsets cross as f64: the index parser holds them to 2^53.
        Ok(entry.map(|entry| vec![entry.offset as f64, entry.length as f64].into_boxed_slice()))
    }

    /// A cell's codes, `frames × levels`, frame by frame. `payload` is the
    /// bytes `chunkRange` names, empty for a chunk the shard never held;
    /// anything else throws.
    #[wasm_bindgen]
    pub fn column(
        &self,
        id: &str,
        row: u32,
        column: u32,
        payload: &[u8],
    ) -> Result<Vec<u8>, JsError> {
        self.inner
            .column(id, row, column, payload)
            .map_err(js_error)
    }

    /// The codes of cells of one block, `[row, column, row, column, ...]`:
    /// cell by cell in that order, each `frames × levels`, frame by frame.
    /// `payload` is the block's chunk, as `column` takes it.
    #[wasm_bindgen]
    pub fn cells(&self, id: &str, cells: &[u32], payload: &[u8]) -> Result<Vec<u8>, JsError> {
        if cells.len() % 2 != 0 {
            return Err(JsError::new("cells must be row and column pairs"));
        }
        let cells: Vec<(u32, u32)> = cells.chunks_exact(2).map(|at| (at[0], at[1])).collect();
        self.inner.cells(id, &cells, payload).map_err(js_error)
    }
}
