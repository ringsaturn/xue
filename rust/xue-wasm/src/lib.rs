//! Browser bindings for the Xue decoder.
//!
//! The Worker copies the downloaded bundle into WASM linear memory exactly
//! once through `WasmBundle::new`, then drops its JavaScript ArrayBuffer.
//! `decode_frame` returns a fresh `Vec<u8>` which wasm-bindgen copies into a
//! JavaScript `Uint8Array`, so no slice into linear memory outlives a
//! potential memory growth.
//!
//! Both readers expose the container's two shapes: the whole-plane calls a v1
//! file has always answered, and the tiled calls v2 adds — a frame restricted
//! to the tiles a viewport covers, a cell's series across the whole axis, and
//! the byte spans either still needs. `tileGeometry` returning `undefined` is
//! how the Worker tells the two versions apart without an error path.

use xue::{Bundle, FrameRequest, StreamingBundle, TileGeometry, TileRect};
use wasm_bindgen::prelude::*;

/// A viewport's tile rectangles, flattened as
/// `[firstColumn, firstRow, lastColumn, lastRow, ...]`.
///
/// A list rather than one rectangle because a viewport straddling the
/// antimeridian covers two disjoint column ranges of a wrapping grid; the
/// caller splits it, and the decoder unions them into the same plane buffer.
fn parse_rects(flat: &[u32]) -> Result<Vec<TileRect>, JsError> {
    if flat.is_empty() || flat.len() % 4 != 0 {
        return Err(JsError::new("tile rectangles come in groups of four"));
    }
    Ok(flat
        .chunks_exact(4)
        .map(|rect| TileRect {
            first_column: rect[0],
            first_row: rect[1],
            last_column: rect[2],
            last_row: rect[3],
        })
        .collect())
}

/// `[tileWidth, tileHeight, columns, rows]`, or `None` on a v1 file.
fn geometry_array(geometry: Option<TileGeometry>) -> Option<Box<[u32]>> {
    geometry.map(|geometry| {
        vec![geometry.tile_width, geometry.tile_height, geometry.columns(), geometry.rows()]
            .into_boxed_slice()
    })
}

/// Spans flattened as `[start, end, ...]`. Offsets cross as f64; file sizes
/// stay far below 2^53.
fn span_array(spans: Vec<(u64, u64)>) -> Box<[f64]> {
    spans
        .into_iter()
        .flat_map(|(start, end)| [start as f64, end as f64])
        .collect::<Vec<f64>>()
        .into_boxed_slice()
}

/// Decode every rectangle into the reader's one plane buffer, then copy the
/// buffer out.
///
/// The rectangles accumulate because the decoder writes only the tiles it was
/// asked for and leaves the rest of the buffer untouched, so the last call
/// returns the union of them all. A macro rather than a function because the
/// two readers are distinct types carrying the same inherent method.
macro_rules! decode_rects {
    ($inner:expr, $request:expr, $rects:expr) => {{
        let rects: Vec<TileRect> = $rects;
        let (last, leading) = rects.split_last().expect("parse_rects rejects an empty list");
        for rect in leading {
            $inner.decode_frame_tiles($request, *rect).map_err(|error| JsError::new(&error.0))?;
        }
        $inner
            .decode_frame_tiles($request, *last)
            .map(|plane| plane.to_vec())
            .map_err(|error| JsError::new(&error.0))
    }};
}

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

    /// `[tileWidth, tileHeight, columns, rows]`, or `undefined` on a v1 file.
    #[wasm_bindgen(js_name = tileGeometry)]
    pub fn tile_geometry(&self) -> Option<Box<[u32]>> {
        geometry_array(self.inner.tile_geometry())
    }

    #[wasm_bindgen(js_name = decodeFrame)]
    pub fn decode_frame(&mut self, variable_id: u8, frame_offset: u16) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_frame(FrameRequest { variable_id, frame_offset })
            .map(|plane| plane.to_vec())
            .map_err(|error| JsError::new(&error.0))
    }

    /// Decode only the tiles the given rectangles cover. Cells outside them
    /// keep whatever the decoder's plane buffer already held, so the caller
    /// must draw only the rectangles it asked for.
    #[wasm_bindgen(js_name = decodeFrameTiles)]
    pub fn decode_frame_tiles(
        &mut self,
        variable_id: u8,
        frame_offset: u16,
        rects: &[u32],
    ) -> Result<Vec<u8>, JsError> {
        let request = FrameRequest { variable_id, frame_offset };
        decode_rects!(self.inner, request, parse_rects(rects)?)
    }

    /// One cell's code on every frame of the axis, in axis order.
    #[wasm_bindgen(js_name = decodeSeries)]
    pub fn decode_series(&mut self, variable_id: u8, column: u32, row: u32) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_series(variable_id, column, row)
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

    /// `[tileWidth, tileHeight, columns, rows]`, or `undefined` on a v1 file.
    #[wasm_bindgen(js_name = tileGeometry)]
    pub fn tile_geometry(&self) -> Option<Box<[u32]>> {
        geometry_array(self.inner.tile_geometry())
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
        frame_offset: u16,
    ) -> Result<Option<Box<[f64]>>, JsError> {
        self.inner
            .missing_group_span(FrameRequest { variable_id, frame_offset })
            .map(|span| span.map(|(start, end)| vec![start as f64, end as f64].into_boxed_slice()))
            .map_err(|error| JsError::new(&error.0))
    }

    /// The byte spans a frame still needs within the given tile rectangles,
    /// flattened as `[start, end, ...]`. A row of tiles is contiguous in the
    /// file, so a viewport costs one range per tile row per group rather than
    /// one per tile.
    #[wasm_bindgen(js_name = missingTileSpans)]
    pub fn missing_tile_spans(
        &self,
        variable_id: u8,
        frame_offset: u16,
        rects: &[u32],
    ) -> Result<Box<[f64]>, JsError> {
        let request = FrameRequest { variable_id, frame_offset };
        let mut spans = Vec::new();
        for rect in parse_rects(rects)? {
            spans.extend(
                self.inner
                    .missing_spans(request, Some(rect))
                    .map_err(|error| JsError::new(&error.0))?,
            );
        }
        Ok(span_array(spans))
    }

    /// The spans one cell's series still needs: one chunk per temporal group,
    /// nothing proportional to the frame count.
    #[wasm_bindgen(js_name = missingSeriesSpans)]
    pub fn missing_series_spans(
        &self,
        variable_id: u8,
        column: u32,
        row: u32,
    ) -> Result<Box<[f64]>, JsError> {
        self.inner
            .missing_series_spans(variable_id, column, row)
            .map(span_array)
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = insertRange)]
    pub fn insert_range(&mut self, offset: f64, bytes: &[u8]) -> Result<(), JsError> {
        self.inner
            .insert_range(offset as u64, bytes)
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = decodeFrame)]
    pub fn decode_frame(&mut self, variable_id: u8, frame_offset: u16) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_frame(FrameRequest { variable_id, frame_offset })
            .map(|plane| plane.to_vec())
            .map_err(|error| JsError::new(&error.0))
    }

    /// See [`WasmBundle::decode_frame_tiles`].
    #[wasm_bindgen(js_name = decodeFrameTiles)]
    pub fn decode_frame_tiles(
        &mut self,
        variable_id: u8,
        frame_offset: u16,
        rects: &[u32],
    ) -> Result<Vec<u8>, JsError> {
        let request = FrameRequest { variable_id, frame_offset };
        decode_rects!(self.inner, request, parse_rects(rects)?)
    }

    /// One cell's code on every frame of the axis, in axis order.
    #[wasm_bindgen(js_name = decodeSeries)]
    pub fn decode_series(&mut self, variable_id: u8, column: u32, row: u32) -> Result<Vec<u8>, JsError> {
        self.inner
            .decode_series(variable_id, column, row)
            .map_err(|error| JsError::new(&error.0))
    }

    #[wasm_bindgen(js_name = clearCache)]
    pub fn clear_cache(&mut self) {
        self.inner.clear_cache();
    }
}
