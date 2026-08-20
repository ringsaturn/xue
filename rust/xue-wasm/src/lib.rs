//! Browser bindings for the Xue decoder.
//!
//! The Worker copies the downloaded bundle into WASM linear memory exactly
//! once through `WasmBundle::new`, then drops its JavaScript ArrayBuffer.
//! `decode_frame` returns a fresh `Vec<u8>` which wasm-bindgen copies into a
//! JavaScript `Uint8Array`, so no slice into linear memory outlives a
//! potential memory growth.

use xue::{Bundle, FrameRequest, StreamingBundle};
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct WasmBundle {
    inner: Bundle,
}

#[wasm_bindgen]
impl WasmBundle {
    #[wasm_bindgen(constructor)]
    pub fn new(bytes: &[u8]) -> Result<WasmBundle, JsError> {
        Bundle::open(bytes)
            .map(|inner| WasmBundle { inner })
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = metadataJson)]
    pub fn metadata_json(&self) -> String {
        self.inner.metadata_json().to_owned()
    }

    #[wasm_bindgen(js_name = planeLength)]
    pub fn plane_length(&self) -> u32 {
        self.inner.plane_length() as u32
    }

    #[wasm_bindgen(js_name = decodeFrame)]
    pub fn decode_frame(&mut self, variable_id: u8, forecast_hour: u16) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_frame(FrameRequest { variable_id, forecast_hour })
            .map(|plane| plane.to_vec())
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = clearCache)]
    pub fn clear_cache(&mut self) {
        self.inner.clear_cache();
    }
}

/// Streaming variant: opened from the structural prefix (header + metadata +
/// index), payload bytes inserted incrementally as HTTP range responses.
/// Offsets cross the JS boundary as f64 — file sizes stay far below 2^53.
#[wasm_bindgen]
pub struct WasmStreamingBundle {
    inner: StreamingBundle,
}

#[wasm_bindgen]
impl WasmStreamingBundle {
    #[wasm_bindgen(constructor)]
    pub fn new(prefix: &[u8]) -> Result<WasmStreamingBundle, JsError> {
        StreamingBundle::open_prefix(prefix)
            .map(|inner| WasmStreamingBundle { inner })
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = metadataJson)]
    pub fn metadata_json(&self) -> String {
        self.inner.metadata_json().to_owned()
    }

    #[wasm_bindgen(js_name = planeLength)]
    pub fn plane_length(&self) -> u32 {
        self.inner.plane_length() as u32
    }

    #[wasm_bindgen(js_name = dataOffset)]
    pub fn data_offset(&self) -> f64 {
        self.inner.data_offset() as f64
    }

    #[wasm_bindgen(js_name = fileSize)]
    pub fn file_size(&self) -> f64 {
        self.inner.file_size() as f64
    }

    #[wasm_bindgen(js_name = totalPayloadBytes)]
    pub fn total_payload_bytes(&self) -> f64 {
        self.inner.total_payload_bytes() as f64
    }

    #[wasm_bindgen(js_name = residentPayloadBytes)]
    pub fn resident_payload_bytes(&self) -> f64 {
        self.inner.resident_payload_bytes() as f64
    }

    /// `[start, end)` still needed for the frame's temporal group, or `None`
    /// when the group is fully resident and the frame is decodable.
    #[wasm_bindgen(js_name = missingGroupSpan)]
    pub fn missing_group_span(
        &self,
        variable_id: u8,
        forecast_hour: u16,
    ) -> Result<Option<Box<[f64]>>, JsError> {
        self.inner
            .missing_group_span(FrameRequest { variable_id, forecast_hour })
            .map(|span| span.map(|(start, end)| vec![start as f64, end as f64].into_boxed_slice()))
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = insertRange)]
    pub fn insert_range(&mut self, offset: f64, bytes: &[u8]) -> Result<(), JsError> {
        self.inner
            .insert_range(offset as u64, bytes)
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = decodeFrame)]
    pub fn decode_frame(&mut self, variable_id: u8, forecast_hour: u16) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_frame(FrameRequest { variable_id, forecast_hour })
            .map(|plane| plane.to_vec())
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = clearCache)]
    pub fn clear_cache(&mut self) {
        self.inner.clear_cache();
    }
}
