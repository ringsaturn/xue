//! Browser-target smoke test: run with
//!     wasm-pack test --headless --chrome rust/xue-wasm

#![cfg(target_arch = "wasm32")]

use xue::{Bundle, FrameRequest};
use wasm_bindgen_test::*;

wasm_bindgen_test_configure!(run_in_browser);

const FIXTURE: &[u8] = include_bytes!("../../../tests/fixtures/generated/tmp2m.xue");
const EXPECTED_TMP2M: &[u8] = include_bytes!("../../../tests/fixtures/generated/expected.tmp2m.f000.bin");

#[wasm_bindgen_test]
fn decode_fixture_in_browser() {
    let mut bundle = Bundle::open(FIXTURE).expect("fixture must parse");
    let plane = bundle
        .decode_frame(FrameRequest { variable_id: 1, forecast_hour: 0 })
        .expect("decode");
    assert_eq!(plane, EXPECTED_TMP2M);
}

#[wasm_bindgen_test]
fn corrupted_fixture_rejected() {
    let mut bytes = FIXTURE.to_vec();
    bytes[0] = b'Q';
    assert!(Bundle::open(&bytes).is_err());
}
