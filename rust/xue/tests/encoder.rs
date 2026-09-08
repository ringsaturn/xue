//! Unit tests for the pure stages, plus a golden test that encodes the shared
//! GRIB fixture and demands the same bytes the Python encoder wrote.
//!
//! The whole file compiles away without the feature: a decode-only build has
//! neither the encoder nor its dependencies.
#![cfg(feature = "encoder")]

use std::path::PathBuf;

use xue::encode::convert::{
    average_window_start, convert_bin, deaccumulate_precipitation, deaverage_precipitation,
    ConvertOptions,
};
use xue::encode::grid::{crop_grid, GridInfo};
use xue::encode::poster::{decode_poster, encode_poster};
use xue::encode::quantize::codebook;
use xue::encode::temporal::{anchor_hour, encode_residual, group_forecast_hours, split_segments};

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

// -- quantization ------------------------------------------------------------

#[test]
fn linear_codebook_rounds_half_up() {
    let book = codebook("quality", "tmp2m").expect("temperature codebook");
    // -60 + 0.5 * 1 = -59.5 is exactly on a step; -59.75 sits exactly half way
    // and must round up, which round-half-even would not do.
    let values = [-60.0, -59.75, -59.5, 0.0, 50.0, 99.0, -99.0];
    let mut codes = [0u8; 7];
    book.quantize(&values, &mut codes).expect("quantizes");
    assert_eq!(codes, [0, 1, 1, 120, 220, 220, 0]);
}

#[test]
fn linear_codebook_declares_its_maximum_code() {
    for (variable, expected) in [("tmp2m", 220), ("ugrd10m", 254), ("dswrf", 254), ("cref", 160)] {
        let book = codebook("quality", variable).expect("codebook");
        assert_eq!(
            book.as_linear().expect("linear").maximum_code(),
            expected,
            "{variable}"
        );
    }
}

#[test]
fn precipitation_codebook_marks_dry_and_overflow() {
    let book = codebook("quality", "prate").expect("precipitation codebook");
    let values = [0.0, 0.009, 0.01, 1.0, 128.0, 200.0];
    let mut codes = [0u8; 6];
    book.quantize(&values, &mut codes).expect("quantizes");
    assert_eq!(codes[0], 0, "dry");
    assert_eq!(codes[1], 0, "below trace is dry");
    assert_eq!(codes[2], 1, "trace is the first positive code");
    assert_eq!(codes[4], 253, "the maximum decodes to exactly the maximum");
    assert_eq!(codes[5], 254, "above the maximum is the overflow code");
    assert!(codes[3] > 1 && codes[3] < 253);
}

#[test]
fn compact_and_balanced_profiles_differ_only_where_documented() {
    let quality = codebook("quality", "tmp2m").expect("codebook");
    let balanced = codebook("balanced", "tmp2m").expect("codebook");
    let compact = codebook("compact", "tmp2m").expect("codebook");
    assert_eq!(quality.metadata(), balanced.metadata());
    assert_ne!(quality.metadata(), compact.metadata());
    // Balanced keeps temperature at quality and drops precipitation to the
    // 128-level codebook.
    assert_eq!(
        codebook("balanced", "prate").expect("codebook").metadata(),
        codebook("compact", "prate").expect("codebook").metadata()
    );
}

// -- temporal ----------------------------------------------------------------

#[test]
fn segments_split_where_the_step_changes() {
    let axis: Vec<i64> = (0..=12).chain((15..=24).step_by(3)).collect();
    let segments = split_segments(&axis).expect("splits");
    assert_eq!(segments.len(), 2);
    assert_eq!(*segments[0].last().expect("last"), 12);
    assert_eq!(segments[1][0], 15);
}

#[test]
fn groups_never_straddle_a_cadence_change() {
    let axis: Vec<i64> = (0..=12).chain((15..=24).step_by(3)).collect();
    for group in group_forecast_hours(&axis).expect("groups") {
        let steps: Vec<i64> = group.windows(2).map(|pair| pair[1] - pair[0]).collect();
        assert!(steps.windows(2).all(|pair| pair[0] == pair[1]), "{group:?}");
        assert!(group.len() <= 6);
    }
}

#[test]
fn the_anchor_is_the_middle_frame() {
    assert_eq!(anchor_hour(&[0, 1, 2, 3, 4, 5]).expect("anchor"), 3);
    assert_eq!(anchor_hour(&[0]).expect("anchor"), 0);
    assert_eq!(anchor_hour(&[0, 1, 2]).expect("anchor"), 1);
}

#[test]
fn residuals_wrap_losslessly() {
    let current = [0u8, 255, 7, 128];
    let base = [1u8, 0, 250, 128];
    let residual = encode_residual(&current, &base).expect("residual");
    let restored: Vec<u8> = residual
        .iter()
        .zip(&base)
        .map(|(value, anchor)| value.wrapping_add(*anchor))
        .collect();
    assert_eq!(restored, current);
}

#[test]
fn ascending_unique_offsets_are_required() {
    assert!(split_segments(&[0, 1, 1]).is_err());
    assert!(split_segments(&[2, 1]).is_err());
}

// -- the grid ----------------------------------------------------------------

fn global_grid() -> GridInfo {
    GridInfo::new(1440, 721, -180.0, 90.0, 0.25, -0.25)
}

#[test]
fn a_global_grid_wraps() {
    assert!(global_grid().wraps());
    assert!(!GridInfo::new(35, 29, 105.0, 42.0, 0.5, -0.5).wraps());
}

#[test]
fn decimation_keeps_the_south_pole() {
    let half = global_grid().decimated();
    assert_eq!((half.width, half.height), (720, 361));
    assert_eq!(half.first_latitude + 360.0 * half.latitude_step, -90.0);
}

#[test]
fn cropping_covers_the_requested_box() {
    let grid = crop_grid(global_grid(), (105.0, 28.0, 122.0, 42.0)).expect("crops");
    assert!(!grid.wraps());
    assert!(grid.first_longitude <= 105.0);
    assert!(grid.first_latitude >= 42.0);
    let east = grid.first_longitude + (grid.width - 1) as f64 * grid.longitude_step;
    let south = grid.first_latitude + (grid.height - 1) as f64 * grid.latitude_step;
    assert!(east >= 122.0);
    assert!(south <= 28.0);
}

#[test]
fn cropping_across_the_antimeridian_stays_contiguous() {
    let grid = crop_grid(global_grid(), (170.0, -10.0, -170.0, 10.0)).expect("crops");
    let window = grid.crop.expect("window");
    // The window continues past the last column and wraps to column 0.
    assert!(window.column_start + window.width > window.source_width);
    assert!(grid.first_longitude >= 169.0 && grid.first_longitude <= 171.0);
}

#[test]
fn a_grid_can_only_be_cropped_once() {
    let grid = crop_grid(global_grid(), (105.0, 28.0, 122.0, 42.0)).expect("crops");
    assert!(crop_grid(grid, (105.0, 28.0, 122.0, 42.0)).is_err());
}

// -- derived precipitation ---------------------------------------------------

#[test]
fn deaccumulation_clamps_backward_steps() {
    let current = [5.0, 1.0, 3.0];
    let previous = [2.0, 4.0, 3.0];
    let rate = deaccumulate_precipitation(&current, Some(&previous), 3);
    assert_eq!(rate, vec![1.0, 0.0, 0.0]);
    // The first frame has no preceding interval.
    assert_eq!(
        deaccumulate_precipitation(&current, None, 3),
        vec![0.0, 0.0, 0.0]
    );
}

#[test]
fn averaging_windows_reset_every_six_hours() {
    assert_eq!(average_window_start(1, 6).expect("start"), 0);
    assert_eq!(average_window_start(6, 6).expect("start"), 0);
    assert_eq!(average_window_start(7, 6).expect("start"), 6);
    assert!(average_window_start(0, 6).is_err());
}

#[test]
fn deaveraging_differences_within_the_window() {
    // f001 averages one hour from the window start, f002 two hours; a constant
    // average rate therefore yields the same per-step rate at both.
    let one_mm_per_hour = 1.0 / 3600.0;
    let first =
        deaverage_precipitation(&[one_mm_per_hour], 1, None, None, 6).expect("first frame");
    let second = deaverage_precipitation(
        &[one_mm_per_hour],
        2,
        Some(&[one_mm_per_hour]),
        Some(1),
        6,
    )
    .expect("second frame");
    assert!((first[0] - 1.0).abs() < 1e-9);
    assert!((second[0] - 1.0).abs() < 1e-9);
    // A frame from the previous window is not a valid predecessor.
    assert!(deaverage_precipitation(&[0.0], 7, Some(&[0.0]), Some(6), 6).is_err());
}

// -- posters ----------------------------------------------------------------

#[test]
fn posters_round_trip() {
    let grid = GridInfo::new(8, 6, -180.0, 90.0, 45.0, -30.0);
    let codes: Vec<u8> = (0..48).map(|index| (index * 7 % 251) as u8).collect();
    let (payload, poster_grid) = encode_poster(&codes, &grid).expect("encodes");
    let decoded =
        decode_poster(&payload, poster_grid.width, poster_grid.height).expect("decodes");
    let mut expected: Vec<u8> = Vec::new();
    for row in (0..grid.height).step_by(2) {
        for column in (0..grid.width).step_by(2) {
            expected.push(codes[row * grid.width + column]);
        }
    }
    assert_eq!(decoded, expected);
}

// -- the golden encode -------------------------------------------------------

/// Encode the shared cropped GRIB fixture and demand the exact bytes the
/// Python encoder wrote for it.
///
/// Skipped (rather than failed) when the Python fixture is absent, since it is
/// generated: `.venv/bin/python tests/prepare_bin_fixture.py`.
#[test]
fn golden_encode_matches_the_python_reference() {
    let root = repository_root();
    let fixture = root.join("tests/fixtures/gfs.2026081406.f000.crop.grib2");
    let expected_dir = root.join("tests/fixtures/generated");
    if !fixture.is_file() || !expected_dir.join("tmp2m.xue").is_file() {
        eprintln!(
            "skipping: run `.venv/bin/python tests/prepare_bin_fixture.py` to generate the fixture"
        );
        return;
    }
    let output = std::env::temp_dir().join("xue-encode-golden");
    let _ = std::fs::remove_dir_all(&output);
    convert_bin(&[fixture], &output, &ConvertOptions::default()).expect("conversion");
    for name in [
        "tmp2m.xue",
        "tmp2m.half.xue",
        "tmp2m.poster.bin",
        "prate.xue",
        "prate.half.xue",
        "prate.poster.bin",
    ] {
        let expected = std::fs::read(expected_dir.join(name)).expect("reference artifact");
        let actual = std::fs::read(output.join(name)).expect("encoded artifact");
        assert_eq!(
            actual.len(),
            expected.len(),
            "{name} differs in length from the Python encoder's output"
        );
        assert!(
            actual == expected,
            "{name} differs from the Python encoder's output"
        );
    }
}
