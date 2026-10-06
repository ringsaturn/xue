//! The Level 3 reader against the Python reference.
//!
//! `tests/fixtures/nexrad/nexrad/` holds seven real Level 3 files, and
//! `expected/level3.json` is what `xuebuild/nexrad/level3.py::read_sweep`
//! makes of each — written by `tests/prepare_nexrad_golden.py` and held to
//! the Python reader by `tests/test_nexrad.py`. The port must agree on every
//! header fact and on every code, through a CRC-32 of the beam grid.

use std::path::PathBuf;

use serde_json::Value;
use xue::{read_level3, LEVEL3_BEAMS};

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/nexrad")
}

fn level3_bytes(name: &str) -> Vec<u8> {
    std::fs::read(fixtures().join("nexrad").join(&name[..3]).join(name)).unwrap()
}

#[test]
fn every_fixture_file_matches_the_reference_digest() {
    let digest: Value =
        serde_json::from_slice(&std::fs::read(fixtures().join("expected/level3.json")).unwrap()).unwrap();
    let entries = digest.as_array().unwrap();
    assert_eq!(entries.len(), 7);
    for entry in entries {
        let name = entry["name"].as_str().unwrap();
        let sweep = read_level3(&level3_bytes(name)).unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(u64::from(sweep.product_code), entry["productCode"].as_u64().unwrap(), "{name}");
        assert_eq!(u64::from(sweep.gates), entry["gates"].as_u64().unwrap(), "{name}");
        assert_eq!(sweep.scan_time, entry["scanTime"].as_i64().unwrap(), "{name}");
        assert_eq!(sweep.elevation, entry["elevation"].as_f64().unwrap() as f32, "{name}");
        assert_eq!(sweep.latitude, entry["latitude"].as_f64().unwrap(), "{name}");
        assert_eq!(sweep.longitude, entry["longitude"].as_f64().unwrap(), "{name}");
        assert_eq!(sweep.height_m, entry["heightM"].as_f64().unwrap(), "{name}");
        assert_eq!(i64::from(sweep.vcp), entry["vcp"].as_i64().unwrap(), "{name}");
        assert_eq!(i64::from(sweep.volume_number), entry["volumeNumber"].as_i64().unwrap(), "{name}");
        assert_eq!(i64::from(sweep.elevation_number), entry["elevationNumber"].as_i64().unwrap(), "{name}");
        assert_eq!(sweep.codes.len(), LEVEL3_BEAMS * sweep.gates as usize, "{name}");
        assert_eq!(u64::from(crc32fast::hash(&sweep.codes)), entry["codesCrc32"].as_u64().unwrap(), "{name}");
    }
}

/// Offset of description-block halfword `number` in a fixture file, past its
/// WMO heading and message header.
fn halfword_offset(data: &[u8], number: usize) -> usize {
    let line = |from: usize| from + data[from..].windows(3).position(|w| w == b"\r\r\n").unwrap() + 3;
    line(line(0)) + 18 + (number - 10) * 2
}

#[test]
fn a_foreign_codebook_is_refused() {
    let mut data = level3_bytes("DGX_N0B_2023_03_25_01_37_39");
    let offset = halfword_offset(&data, 32);
    data[offset..offset + 2].copy_from_slice(&10i16.to_be_bytes());
    let error = read_level3(&data).unwrap_err();
    assert!(error.0.contains("thresholds"), "{error}");
}

#[test]
fn a_product_this_one_does_not_carry_is_refused() {
    let mut data = level3_bytes("DGX_N0B_2023_03_25_01_37_39");
    let offset = halfword_offset(&data, 16);
    data[offset..offset + 2].copy_from_slice(&94i16.to_be_bytes());
    let error = read_level3(&data).unwrap_err();
    assert!(error.0.contains("product 94"), "{error}");
}

#[test]
fn an_unknown_compression_method_is_refused() {
    let mut data = level3_bytes("DGX_N0G_2023_03_25_01_37_39");
    let offset = halfword_offset(&data, 51);
    data[offset..offset + 2].copy_from_slice(&2i16.to_be_bytes());
    assert!(read_level3(&data).unwrap_err().0.contains("compression method 2"));
}

#[test]
fn a_truncated_file_is_refused_without_panicking() {
    let data = level3_bytes("GWX_N0G_2023_03_25_01_41_22");
    for length in [0, 3, 30, 100, 140, data.len() / 2, data.len() - 1] {
        assert!(read_level3(&data[..length]).is_err(), "{length} bytes");
    }
}
