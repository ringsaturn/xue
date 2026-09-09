//! Cross-language golden tests: Python encodes the cropped GRIB fixture,
//! Rust must decode it byte-for-byte identically to the Python reference.
//!
//! Generate the fixture first:
//!     .venv/bin/python tests/prepare_bin_fixture.py

use xue::{Bundle, FrameRequest, StreamingBundle, TileRect};
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
        assert_eq!(metadata["schemaVersion"], 3);
        assert_eq!(metadata["model"], "GFS");
        assert_eq!(metadata["variables"].as_array().map(Vec::len), Some(1));
        assert_eq!(metadata["variables"][0]["id"], name);
        // Schema v3: the variable says what it is in GRIB2's own terms.
        assert_eq!(metadata["variables"][0]["parameter"]["discipline"], 0);
        assert!(metadata["variables"][0]["parameter"]["parameterNumber"].is_number());

        let expected_path = fixture_dir().join(format!("expected.{name}.f000.bin"));
        let expected = std::fs::read(&expected_path).unwrap_or_else(|_| {
            panic!("missing expected plane {expected_path:?}; regenerate the fixture")
        });
        let plane = bundle
            .decode_frame(FrameRequest { variable_id, frame_offset: 0 })
            .expect("decode");
        assert_eq!(plane.len(), expected.len(), "plane length for {name}");
        assert_eq!(plane, expected.as_slice(), "plane bytes for {name}");
    }
}

/// The synthetic mixed-step axis of the listed-hours fixture
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
    assert_eq!(metadata["schemaVersion"], 3);
    assert_eq!(metadata["time"]["unitSeconds"], 3600);
    assert!(metadata["time"].get("frameStep").is_none());
    assert_eq!(
        metadata["time"]["frameOffsets"].as_array().map(Vec::len),
        Some(mixed_hours().len())
    );

    for hour in mixed_hours() {
        let expected_path = fixture_dir().join(format!("expected.mixed.f{hour:03}.bin"));
        let expected = std::fs::read(&expected_path).unwrap_or_else(|_| {
            panic!("missing expected plane {expected_path:?}; regenerate the fixture")
        });
        let plane = bundle
            .decode_frame(FrameRequest { variable_id: 1, frame_offset: hour })
            .expect("decode");
        assert_eq!(plane, expected.as_slice(), "mixed f{hour:03}");
    }
}

/// A decoder that implements fewer schema versions must reject what it
/// cannot read; ours must reject a bundle re-labelled to a version its own
/// metadata does not need, keeping exactly one valid encoding per file.
#[test]
fn relabelled_schema_version_rejected() {
    let bytes = variable_fixture_bytes("mixed");
    let metadata_offset = u64::from_le_bytes(bytes[24..32].try_into().unwrap()) as usize;
    let metadata_length = u64::from_le_bytes(bytes[32..40].try_into().unwrap()) as usize;
    let json = std::str::from_utf8(&bytes[metadata_offset..metadata_offset + metadata_length]).unwrap();
    let needle = "\"schemaVersion\":3";
    let position = metadata_offset + json.find(needle).expect("schemaVersion in metadata");
    for version in [b'1', b'2', b'4'] {
        let mut mutated = bytes.clone();
        mutated[position + needle.len() - 1] = version;
        assert!(Bundle::open(&mutated).is_err(), "schemaVersion {}", version as char);
    }
}

#[test]
fn streaming_mixed_axis_matches_full_decode() {
    let bytes = variable_fixture_bytes("mixed");
    let data_offset = data_offset_of(&bytes);
    let mut full = Bundle::open(&bytes).expect("full bundle parses");
    let mut streaming = StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
    for hour in mixed_hours() {
        let request = FrameRequest { variable_id: 1, frame_offset: hour };
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
        .decode_frame(FrameRequest { variable_id: 1, frame_offset: 0 })
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
            .decode_frame(FrameRequest { variable_id: 1, frame_offset: 0 })
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
            let request = FrameRequest { variable_id, frame_offset: hour };
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
    let request = FrameRequest { variable_id: 1, frame_offset: 0 };
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
/// panic, only return errors or succeed. Both container versions: v2 derives
/// far more from file values than v1 does (tile geometry, chunk offsets as
/// prefix sums, a group partition), so it has more arithmetic to get wrong.
#[test]
fn mutated_structures_do_not_panic() {
    for bytes in [fixture_bytes(), tiled_bytes()] {
        let data_offset = data_offset_of(&bytes);
        let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
        for _ in 0..4000 {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            let position = (state >> 33) as usize % data_offset;
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            let value = (state >> 56) as u8;
            let mut mutated = bytes.clone();
            mutated[position] = value;
            if let Ok(mut bundle) = Bundle::open(&mutated) {
                // Parsing is only half of it: a structure that survived
                // validation must not panic when it is actually read.
                let _ = bundle.decode_frame(FrameRequest { variable_id: 1, frame_offset: 0 });
                let _ = bundle.decode_series(1, 0, 0);
            }
            let _ = StreamingBundle::open_prefix(&mutated[..data_offset]);
        }
    }
}

#[test]
fn truncations_do_not_panic() {
    for bytes in [fixture_bytes(), tiled_bytes()] {
        let mut length = bytes.len();
        while length > 0 {
            let _ = Bundle::open(&bytes[..length]);
            let _ = StreamingBundle::open_prefix(&bytes[..length]);
            length = length.saturating_sub(7);
        }
    }
}

// -- container v2 -------------------------------------------------------------

/// The synthetic tiled fixture (tests/prepare_bin_fixture.py
/// prepare_tiled_fixture): a 17 x 9 grid cut into 5 x 4 tiles, so the last
/// tile column is two cells wide and the last tile row one cell tall.
const TILED_WIDTH: u32 = 17;
const TILED_HEIGHT: u32 = 9;

fn tiled_bytes() -> Vec<u8> {
    variable_fixture_bytes("tiled")
}

fn tiled_expected(variable_id: u8, hour: u16) -> Vec<u8> {
    let path = fixture_dir().join(format!("expected.tiled.v{variable_id}.f{hour:03}.bin"));
    std::fs::read(&path).unwrap_or_else(|_| panic!("missing {path:?}; regenerate the fixture"))
}

#[test]
fn tiled_decode_matches_python_reference() {
    let mut bundle = Bundle::open(&tiled_bytes()).expect("tiled fixture must parse");
    let geometry = bundle.tile_geometry().expect("a v2 file declares tiles");
    assert_eq!((geometry.width, geometry.height), (TILED_WIDTH, TILED_HEIGHT));
    assert_eq!((geometry.tile_width, geometry.tile_height), (5, 4));
    assert_eq!((geometry.columns(), geometry.rows()), (4, 3));
    // The last tile is clipped on both axes: 17 - 3 * 5 = 2 wide, 9 - 2 * 4 = 1 tall.
    assert_eq!(geometry.shape(geometry.count() - 1), (1, 2));
    assert_eq!(bundle.variable_ids().len(), 2);

    for variable_id in [1u8, 2u8] {
        for &hour in &mixed_hours() {
            let expected = tiled_expected(variable_id, hour);
            let plane = bundle
                .decode_frame(FrameRequest { variable_id, frame_offset: hour })
                .expect("decode");
            assert_eq!(plane, expected.as_slice(), "variable {variable_id} f{hour:03}");
        }
    }
}

/// A cell's series must be the same bytes as reading that cell out of every
/// decoded plane — at one chunk per group instead of one plane per frame.
#[test]
fn tiled_series_matches_the_planes() {
    let mut bundle = Bundle::open(&tiled_bytes()).expect("tiled fixture must parse");
    let hours = mixed_hours();
    for variable_id in [1u8, 2u8] {
        // Every corner, plus a cell inside each clipped edge and one interior.
        for (column, row) in [
            (0, 0),
            (TILED_WIDTH - 1, 0),
            (0, TILED_HEIGHT - 1),
            (TILED_WIDTH - 1, TILED_HEIGHT - 1),
            (16, 4),
            (7, 8),
            (9, 5),
        ] {
            let series = bundle.decode_series(variable_id, column, row).expect("series");
            assert_eq!(series.len(), hours.len());
            let expected: Vec<u8> = hours
                .iter()
                .map(|&hour| tiled_expected(variable_id, hour)[(row * TILED_WIDTH + column) as usize])
                .collect();
            assert_eq!(series, expected, "variable {variable_id} at ({column}, {row})");
        }
        // The golden series the Python reference dumped for the south-east
        // corner, which lives in the doubly clipped last tile.
        let path = fixture_dir().join(format!("expected.tiled.v{variable_id}.series.bin"));
        let golden = std::fs::read(&path).unwrap_or_else(|_| panic!("missing {path:?}"));
        let series = bundle
            .decode_series(variable_id, TILED_WIDTH - 1, TILED_HEIGHT - 1)
            .expect("series");
        assert_eq!(series, golden, "golden series for variable {variable_id}");
    }
    assert!(bundle.decode_series(1, TILED_WIDTH, 0).is_err());
    assert!(bundle.decode_series(1, 0, TILED_HEIGHT).is_err());
    assert!(bundle.decode_series(9, 0, 0).is_err());
}

/// Decoding a tile rectangle must produce exactly the bytes a whole-plane
/// decode produces, inside the rectangle — that equality is the whole promise
/// of fetching only what a viewport covers.
#[test]
fn tiled_partial_decode_matches_the_whole_plane() {
    let mut bundle = Bundle::open(&tiled_bytes()).expect("tiled fixture must parse");
    let geometry = bundle.tile_geometry().expect("tiles");
    let request = FrameRequest { variable_id: 1, frame_offset: 9 };
    let expected = tiled_expected(1, 9);
    for rect in [
        TileRect { first_column: 0, first_row: 0, last_column: 0, last_row: 0 },
        TileRect { first_column: 1, first_row: 1, last_column: 2, last_row: 2 },
        TileRect { first_column: 3, first_row: 2, last_column: 3, last_row: 2 },
        TileRect { first_column: 0, first_row: 0, last_column: 3, last_row: 2 },
    ] {
        bundle.clear_cache();
        let plane = bundle.decode_frame_tiles(request, rect).expect("partial decode").to_vec();
        for tile in 0..geometry.count() {
            if !rect.contains(&geometry, tile) {
                continue;
            }
            let (row, column) = geometry.origin(tile);
            let (height, width) = geometry.shape(tile);
            for line in 0..height {
                let start = ((row + line) * TILED_WIDTH + column) as usize;
                let end = start + width as usize;
                assert_eq!(plane[start..end], expected[start..end], "tile {tile}");
            }
        }
    }
}

/// A v2 file streams the way a v1 one does — a whole group is still one
/// contiguous range — and a viewport or a series asks for strictly less.
#[test]
fn tiled_streaming_matches_full_decode() {
    let bytes = tiled_bytes();
    let data_offset = data_offset_of(&bytes);
    let mut full = Bundle::open(&bytes).expect("full bundle parses");
    let mut streaming = StreamingBundle::open_prefix(&bytes[..data_offset]).expect("prefix parses");
    assert_eq!(streaming.file_size() as usize, bytes.len());
    let geometry = streaming.tile_geometry().expect("tiles");

    // One tile's series costs one chunk per group, and nothing else.
    let series_spans = streaming.missing_series_spans(1, 16, 8).expect("series spans");
    let series_bytes: u64 = series_spans.iter().map(|(start, end)| end - start).sum();
    assert!(series_bytes < streaming.total_payload_bytes() / 4, "{series_bytes} bytes for a series");

    // A viewport asks for less than the whole group.
    let request = FrameRequest { variable_id: 1, frame_offset: 6 };
    let rect = TileRect { first_column: 0, first_row: 0, last_column: 1, last_row: 0 };
    let partial: u64 = streaming
        .missing_spans(request, Some(rect))
        .expect("spans")
        .iter()
        .map(|(start, end)| end - start)
        .sum();
    let group = streaming.missing_group_span(request).expect("group span").expect("not resident");
    assert!(partial < group.1 - group.0, "{partial} vs {}", group.1 - group.0);

    for &hour in &mixed_hours() {
        for variable_id in [1u8, 2u8] {
            let request = FrameRequest { variable_id, frame_offset: hour };
            if let Some((start, end)) = streaming.missing_group_span(request).expect("span") {
                assert!(streaming.decode_frame(request).is_err());
                streaming
                    .insert_range(start, &bytes[start as usize..end as usize])
                    .expect("insert");
            }
            assert!(streaming.missing_group_span(request).expect("span").is_none());
            let expected = full.decode_frame(request).expect("full decode").to_vec();
            let plane = streaming.decode_frame(request).expect("streaming decode");
            assert_eq!(plane, expected.as_slice(), "variable {variable_id} f{hour:03}");
        }
    }
    assert_eq!(streaming.resident_payload_bytes(), streaming.total_payload_bytes());
    let _ = geometry;
}

/// A corrupt chunk must fail on its own CRC32, not silently paint wrong cells.
#[test]
fn tiled_corrupt_chunk_fails_decode() {
    let mut bytes = tiled_bytes();
    let last = bytes.len() - 1;
    // Flip a byte inside the final chunk's payload (before the tail padding).
    let data_offset = data_offset_of(&bytes);
    let target = (data_offset + last) / 2;
    bytes[target] ^= 0xFF;
    match Bundle::open(&bytes) {
        Err(_) => {}
        Ok(mut bundle) => {
            let failed = mixed_hours().iter().any(|&hour| {
                bundle.clear_cache();
                bundle
                    .decode_frame(FrameRequest { variable_id: 1, frame_offset: hour })
                    .is_err()
                    || bundle
                        .decode_frame(FrameRequest { variable_id: 2, frame_offset: hour })
                        .is_err()
            });
            assert!(failed, "a flipped payload byte must be caught");
        }
    }
}
