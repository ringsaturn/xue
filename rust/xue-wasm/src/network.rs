//! A river case's network store for the browser (`docs/zarr-profile.md`,
//! "Network store"): how its one data array is cut, read by the reader the
//! native side uses (`xue::network`). The shell keeps the requests and the
//! decoded chunks; the shard index and the chunks go through the same calls
//! as a grid store's.

use wasm_bindgen::prelude::*;
use xue::network::{self, NetworkLayout};
use xue::zarr::IndexLocation;

fn js_error(error: xue::DecodeError) -> JsError {
    JsError::new(&error.0)
}

/// How a network store's `streamflow` array is cut.
#[wasm_bindgen]
pub struct WasmNetworkLayout {
    inner: NetworkLayout,
}

#[wasm_bindgen]
impl WasmNetworkLayout {
    #[wasm_bindgen(getter, js_name = frameCount)]
    pub fn frame_count(&self) -> u32 {
        self.inner.frame_count
    }

    #[wasm_bindgen(getter, js_name = featureCount)]
    pub fn feature_count(&self) -> u32 {
        self.inner.feature_count
    }

    /// Frames per inner chunk.
    #[wasm_bindgen(getter, js_name = timeChunk)]
    pub fn time_chunk(&self) -> u32 {
        self.inner.time_chunk
    }

    /// Reaches per inner chunk: one GeoParquet row group.
    #[wasm_bindgen(getter, js_name = rowGroupRows)]
    pub fn row_group_rows(&self) -> u32 {
        self.inner.row_group_rows
    }

    #[wasm_bindgen(getter, js_name = rowGroups)]
    pub fn row_groups(&self) -> u32 {
        self.inner.feature_chunks
    }

    #[wasm_bindgen(getter, js_name = timeChunks)]
    pub fn time_chunks(&self) -> u32 {
        self.inner.time_chunks
    }

    /// Inner chunks in the one shard, and pairs in its index.
    #[wasm_bindgen(getter, js_name = chunksPerShard)]
    pub fn chunks_per_shard(&self) -> u32 {
        self.inner.chunk_count
    }

    /// The inner chain carries `xue.delta`: chunks are PREVIOUS residuals.
    #[wasm_bindgen(getter)]
    pub fn delta(&self) -> bool {
        self.inner.delta
    }

    /// The index is a suffix of the shard; otherwise a prefix.
    #[wasm_bindgen(getter, js_name = indexAtEnd)]
    pub fn index_at_end(&self) -> bool {
        self.inner.index_location == IndexLocation::End
    }

    #[wasm_bindgen(getter, js_name = fillValue)]
    pub fn fill_value(&self) -> u8 {
        self.inner.fill_value
    }
}

/// Read a network store's data array against its group: `groupText` is the
/// root `zarr.json`, `arrayText` the store's `streamflow/zarr.json`. Without
/// `arrayText` the document is taken from the group's consolidated metadata,
/// which must then carry it. The group's block is held to the profile here
/// too, and the array to the group: its shape, its inner chunk of six frames
/// of one row group, its one shard, its fill value and the variable it
/// repeats.
#[wasm_bindgen(js_name = parseNetworkArray)]
pub fn parse_network_array(group_text: &str, array_text: Option<String>) -> Result<WasmNetworkLayout, JsError> {
    let group = network::parse_network_group(group_text).map_err(js_error)?;
    let array_text = match array_text {
        Some(text) => text,
        None => network::consolidated_network_array(group_text)
            .map_err(js_error)?
            .ok_or_else(|| JsError::new("group carries no consolidated streamflow document"))?,
    };
    network::parse_network_array(&array_text, &group)
        .map(|inner| WasmNetworkLayout { inner })
        .map_err(js_error)
}
