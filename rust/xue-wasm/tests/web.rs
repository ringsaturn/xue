//! Browser-target smoke test: run with
//!     wasm-pack test --headless --chrome rust/xue-wasm

#![cfg(target_arch = "wasm32")]

use xue::{Bundle, FrameRequest, Predictor};
use wasm_bindgen_test::*;

wasm_bindgen_test_configure!(run_in_browser);

const FIXTURE: &[u8] = include_bytes!("../../../tests/fixtures/generated/tmp2m.xue");
const EXPECTED_TMP2M: &[u8] = include_bytes!("../../../tests/fixtures/generated/expected.tmp2m.f000.bin");

#[wasm_bindgen_test]
fn decode_fixture_in_browser() {
    let mut bundle = Bundle::open(FIXTURE).expect("fixture must parse");
    let plane = bundle
        .decode_frame(FrameRequest { variable_id: 1, frame_offset: 0 })
        .expect("decode");
    assert_eq!(plane, EXPECTED_TMP2M);
}

#[wasm_bindgen_test]
fn corrupted_fixture_rejected() {
    let mut bytes = FIXTURE.to_vec();
    bytes[0] = b'Q';
    assert!(Bundle::open(&bytes).is_err());
}

/// The bytes 1..=12 as one Zstandard frame with a content checksum (see the
/// unit tests in `xue::decode::core`), decoded the way the Zarr channel
/// decodes an inner chunk.
#[wasm_bindgen_test]
fn decode_chunk_in_browser() {
    const FRAME: [u8; 25] = [
        0x28, 0xB5, 0x2F, 0xFD, 0x24, 0x0C, 0x61, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06,
        0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0xA5, 0xE9, 0x82, 0xA5,
    ];
    let chunk = xue::decode_chunk(&FRAME, 3, 2, 2, Predictor::Previous).expect("decode");
    assert_eq!(chunk, vec![1, 2, 3, 4, 6, 8, 10, 12, 15, 18, 21, 24]);
    assert!(xue::decode_chunk(&FRAME, 2, 2, 2, Predictor::Raw).is_err());
}
