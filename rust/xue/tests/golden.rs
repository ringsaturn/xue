//! Cross-language golden tests: Python encodes the cropped GRIB fixture,
//! Rust must decode it byte-for-byte identically to the Python reference.
//!
//! Generate the fixture first:
//!     .venv/bin/python tests/prepare_bin_fixture.py

use xue::{Bundle, FrameRequest, StreamingBundle};
use std::path::PathBuf;

fn fixture_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/generated")
}

fn variable_fixture_bytes(name: &str) -> Vec<u8> {
    let path = fixture_dir().join(format!("{name}.xue"));
    std::fs::read(&path).unwrap_or_else(|_| {
        panic!(
            "missing golden fixture {path:?}; run `.venv/bin/python tests/prepare_bin_fixture.py` first"
        )
    })
}

fn fixture_bytes() -> Vec<u8> {
    variable_fixture_bytes("tmp2m")
}

#[test]
fn golden_decode_matches_python_reference() {
    for (variable_id, name) in [(1u8, "tmp2m"), (2u8, "prate")] {
        let mut bundle =
            Bundle::open(&variable_fixture_bytes(name)).expect("fixture must parse");
        let metadata: serde_json::Value =
            serde_json::from_str(bundle.metadata_json()).expect("metadata JSON");
        assert_eq!(metadata["schemaVersion"], 1);
        assert_eq!(metadata["model"], "GFS");
        assert_eq!(metadata["variables"].as_array().map(Vec::len), Some(1));
        assert_eq!(metadata["variables"][0]["id"], name);

        let expected_path = fixture_dir().join(format!("expected.{name}.f000.bin"));
        let expected = std::fs::read(&expected_path).unwrap_or_else(|_| {
            panic!("missing expected plane {expected_path:?}; regenerate the fixture")
        });
        let plane = bundle
            .decode_frame(FrameRequest { variable_id, forecast_hour: 0 })
            .expect("decode");
        assert_eq!(plane.len(), expected.len(), "plane length for {name}");
        assert_eq!(plane, expected.as_slice(), "plane bytes for {name}");
    }
}

/// The synthetic mixed-step axis of the schemaVersion 2 fixture
/// (tests/prepare_bin_fixture.py MIXED_HOURS): hourly, then three-hourly.
fn mixed_hours() -> Vec<u16> {
    (0..=12).chain((15..=36).step_by(3)).collect()
}

#[test]
fn golden_mixed_axis_decode_matches_python_reference() {
    let bytes = variable_fixture_bytes("mixed");
    let mut bundle = Bundle::open(&bytes).expect("mixed-axis fixture must parse");
    let metadata: serde_json::Value =
        serde_json::from_str(bundle.metadata_json()).expect("metadata JSON");
    assert_eq!(metadata["schemaVersion"], 2);
    assert!(metadata["time"].get("stepHours").is_none());
    assert_eq!(
        metadata["time"]["hours"].as_array().map(Vec::len),
        Some(mixed_hours().len())
    );

    for hour in mixed_hours() {
        let expected_path = fixture_dir().join(format!("expected.mixed.f{hour:03}.bin"));
        let expected = std::fs::read(&expected_path).unwrap_or_else(|_| {
            panic!("missing expected plane {expected_path:?}; regenerate the fixture")
        });
        let plane = bundle
            .decode_frame(FrameRequest { variable_id: 1, forecast_hour: hour })
            .expect("decode");
        assert_eq!(plane, expected.as_slice(), "mixed f{hour:03}");
    }
}

/// A schemaVersion-1-only decoder must reject a mixed-cadence bundle; ours
/// must reject the same bundle re-labelled as schemaVersion 1 (an `hours`
/// axis requires schemaVersion 2), keeping one valid encoding per axis.
#[test]
fn mixed_axis_relabelled_as_v1_rejected() {
    let bytes = variable_fixture_bytes("mixed");
    let metadata_offset = u64::from_le_bytes(bytes[24..32].try_into().unwrap()) as usize;
    let metadata_length = u64::from_le_bytes(bytes[32..40].try_into().unwrap()) as usize;
    let json = std::str::from_utf8(&bytes[metadata_offset..metadata_offset + metadata_length]).unwrap();
    let needle = "\"schemaVersion\":2";
    let position = metadata_offset + json.find(needle).expect("schemaVersion in metadata");
    let mut mutated = bytes.clone();
    mutated[position + needle.len() - 1] = b'1';
    assert!(Bundle::open(&mutated).is_err());
}

#[test]
fn streaming_mixed_axis_matches_full_decode() {
    let bytes = variable_fixture_bytes("mixed");
    let data_offset = data_offset_of(&bytes);
    let mut full = Bundle::open(&bytes).expect("full bundle parses");
    let mut streaming = StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
    for hour in mixed_hours() {
        let request = FrameRequest { variable_id: 1, forecast_hour: hour };
        if let Some((start, end)) = streaming.missing_group_span(request).expect("span") {
            streaming
                .insert_range(start, &bytes[start as usize..end as usize])
                .expect("insert");
        }
        let expected = full.decode_frame(request).expect("full decode").to_vec();
        let plane = streaming.decode_frame(request).expect("streaming decode");
        assert_eq!(plane, expected.as_slice(), "mixed f{hour:03}");
    }
}

#[test]
fn truncated_file_rejected() {
    let bytes = fixture_bytes();
    assert!(Bundle::open(&bytes[..bytes.len() - 4]).is_err());
    assert!(Bundle::open(&bytes[..40]).is_err());
    assert!(Bundle::open(&[]).is_err());
}

#[test]
fn bad_magic_rejected() {
    let mut bytes = fixture_bytes();
    bytes[0] = b'Q';
    assert!(Bundle::open(&bytes).is_err());
}

#[test]
fn foreign_magic_rejected() {
    let mut bytes = fixture_bytes();
    bytes[0..8].copy_from_slice(b"NOTXUE\0\0");
    assert!(Bundle::open(&bytes).is_err());
}

#[test]
fn oversized_index_offset_rejected() {
    let mut bytes = fixture_bytes();
    let huge = (bytes.len() as u64 * 2).to_le_bytes();
    bytes[40..48].copy_from_slice(&huge);
    assert!(Bundle::open(&bytes).is_err());
}

#[test]
fn wrong_file_size_rejected() {
    let mut bytes = fixture_bytes();
    let wrong = (bytes.len() as u64 + 8).to_le_bytes();
    bytes[16..24].copy_from_slice(&wrong);
    assert!(Bundle::open(&bytes).is_err());
}

#[test]
fn corrupt_crc_fails_decode() {
    let bytes = fixture_bytes();
    let index_offset = u64::from_le_bytes(bytes[40..48].try_into().unwrap()) as usize;
    let entry_offset = index_offset + 16; // first entry
    let mut mutated = bytes.clone();
    for byte in &mut mutated[entry_offset + 28..entry_offset + 32] {
        *byte ^= 0xFF;
    }
    let mut bundle = Bundle::open(&mutated).expect("structure still parses");
    assert!(bundle
        .decode_frame(FrameRequest { variable_id: 1, forecast_hour: 0 })
        .is_err());
}

#[test]
fn corrupt_payload_fails_decode() {
    let bytes = fixture_bytes();
    let data_offset = u64::from_le_bytes(bytes[56..64].try_into().unwrap()) as usize;
    let mut mutated = bytes.clone();
    mutated[data_offset + 12] ^= 0xFF;
    if let Ok(mut bundle) = Bundle::open(&mutated) {
        assert!(bundle
            .decode_frame(FrameRequest { variable_id: 1, forecast_hour: 0 })
            .is_err());
    }
}

fn data_offset_of(bytes: &[u8]) -> usize {
    u64::from_le_bytes(bytes[56..64].try_into().unwrap()) as usize
}

/// Streaming decode over range-fetched payload groups must be byte-identical
/// to decoding the complete file.
#[test]
fn streaming_matches_full_decode() {
    for (variable_id, name) in [(1u8, "tmp2m"), (2u8, "prate")] {
        let bytes = variable_fixture_bytes(name);
        let data_offset = data_offset_of(&bytes);
        let mut full = Bundle::open(&bytes).expect("full bundle parses");
        let mut streaming =
            StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
        assert_eq!(streaming.data_offset() as usize, data_offset);
        assert_eq!(streaming.file_size() as usize, bytes.len());
        assert_eq!(streaming.resident_payload_bytes(), 0);

        for hour in 0..streaming.frame_count() as u16 {
            let request = FrameRequest { variable_id, forecast_hour: hour };
            // Not resident yet: decode must fail, span must be reported.
            if streaming.missing_group_span(request).expect("span").is_some() {
                assert!(streaming.decode_frame(request).is_err());
            }
            if let Some((start, end)) = streaming.missing_group_span(request).expect("span") {
                streaming
                    .insert_range(start, &bytes[start as usize..end as usize])
                    .expect("insert");
            }
            assert!(streaming.missing_group_span(request).expect("span").is_none());
            let expected = full.decode_frame(request).expect("full decode").to_vec();
            let plane = streaming.decode_frame(request).expect("streaming decode");
            assert_eq!(plane, expected.as_slice(), "{name} f{hour:03}");
        }
        assert_eq!(streaming.resident_payload_bytes(), streaming.total_payload_bytes());
    }
}

#[test]
fn streaming_prefix_too_short_rejected() {
    let bytes = fixture_bytes();
    let data_offset = data_offset_of(&bytes);
    assert!(StreamingBundle::open_prefix(&bytes[..data_offset - 8]).is_err());
    assert!(StreamingBundle::open_prefix(&bytes[..40]).is_err());
    // The complete file is also a valid prefix.
    assert!(StreamingBundle::open_prefix(&bytes).is_ok());
}

#[test]
fn streaming_insert_out_of_bounds_rejected() {
    let bytes = fixture_bytes();
    let data_offset = data_offset_of(&bytes);
    let mut streaming =
        StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
    let overflow = bytes.len() as u64 - 16;
    assert!(streaming.insert_range(overflow, &bytes[..64]).is_err());
}

#[test]
fn streaming_corrupt_payload_fails_decode() {
    let bytes = fixture_bytes();
    let data_offset = data_offset_of(&bytes);
    let mut streaming =
        StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
    let request = FrameRequest { variable_id: 1, forecast_hour: 0 };
    let (start, end) = streaming
        .missing_group_span(request)
        .expect("span")
        .expect("group not resident");
    let mut payload = bytes[start as usize..end as usize].to_vec();
    payload[12] ^= 0xFF;
    streaming.insert_range(start, &payload).expect("insert");
    assert!(streaming.decode_frame(request).is_err());
}

/// Deterministic mutation fuzz over the header and index: parsing must never
/// panic, only return errors or succeed.
#[test]
fn mutated_structures_do_not_panic() {
    let bytes = fixture_bytes();
    let index_offset = u64::from_le_bytes(bytes[40..48].try_into().unwrap()) as usize;
    let structure_end = (index_offset + 16 + 2 * 40).min(bytes.len());
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    for _ in 0..4000 {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let position = (state >> 33) as usize % structure_end;
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let value = (state >> 56) as u8;
        let mut mutated = bytes.clone();
        mutated[position] = value;
        let _ = Bundle::open(&mutated);
    }
}

#[test]
fn truncations_do_not_panic() {
    let bytes = fixture_bytes();
    let mut length = bytes.len();
    while length > 0 {
        let _ = Bundle::open(&bytes[..length]);
        let _ = StreamingBundle::open_prefix(&bytes[..length]);
        length = length.saturating_sub(7);
    }
}
