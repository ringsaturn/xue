//! Network store golden tests (`docs/zarr-profile.md`, "Network store").
//!
//! The synthetic stores in `tests/fixtures/river/network/` are committed;
//! their codes are listed in that directory's README and regenerated with
//!     .venv/bin/python tests/fixtures/river/network/prepare.py
//! The Guadalupe 2025 showcase stores are read too where the case's data is
//! present in the checkout, and skipped where it is not.

use std::path::{Path, PathBuf};

use xue::network::{
    self, decode_network_frame, decode_network_series, decode_time_chunk, dequantize, frame_range,
    parse_network_array, parse_network_group, parse_side_array, read_baseflow, read_feature_ids,
    read_frame_times, read_network_index, NetworkGroup, NetworkLayout,
};
use xue::zarr::{self, crc32c, IndexLocation, ShardIndexEntry, StoreKind};

/// The codes of `tests/fixtures/river/network/*.zarr`, frame by frame (its
/// README): a flood wave down 20 reaches, reach 13 a lake (255 throughout),
/// reach 4 dry at frame 2 (0) and reach 9 past the codebook at frame 5 (254).
const EXPECTED: [[u8; 20]; 8] = [
    [
        150, 149, 148, 146, 143, 138, 133, 128, 121, 113, 104, 95, 85, 255, 62, 51, 39, 29, 21, 16,
    ],
    [
        143, 146, 148, 149, 150, 149, 148, 146, 143, 138, 133, 128, 121, 255, 104, 95, 85, 74, 62,
        51,
    ],
    [
        121, 128, 133, 138, 0, 146, 148, 149, 150, 149, 148, 146, 143, 255, 133, 128, 121, 113,
        104, 95,
    ],
    [
        85, 95, 104, 113, 121, 128, 133, 138, 143, 146, 148, 149, 150, 255, 148, 146, 143, 138,
        133, 128,
    ],
    [
        39, 51, 62, 74, 85, 95, 104, 113, 121, 128, 133, 138, 143, 255, 148, 149, 150, 149, 148,
        146,
    ],
    [
        13, 16, 21, 29, 39, 51, 62, 74, 85, 254, 104, 113, 121, 255, 133, 138, 143, 146, 148, 149,
    ],
    [
        10, 10, 10, 11, 13, 16, 21, 29, 39, 51, 62, 74, 85, 255, 104, 113, 121, 128, 133, 138,
    ],
    [
        10, 10, 10, 10, 10, 10, 10, 11, 13, 16, 21, 29, 39, 255, 62, 74, 85, 95, 104, 113,
    ],
];
/// 2025-07-04T01:00:00Z, the first frame (runTime 00Z, offset 1 h).
const FIRST_FRAME: i64 = 1_751_590_800;

fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/river/network")
        .join(name)
}

fn text(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|_| panic!("missing {path:?}"))
}

fn bytes(path: &Path) -> Vec<u8> {
    std::fs::read(path).unwrap_or_else(|_| panic!("missing {path:?}"))
}

struct Store {
    group: NetworkGroup,
    layout: NetworkLayout,
    shard: Vec<u8>,
    index: Vec<Option<ShardIndexEntry>>,
}

fn open(root: &Path) -> Store {
    let group_text = text(&root.join("zarr.json"));
    assert_eq!(zarr::store_kind(&group_text).unwrap(), StoreKind::Network);
    let group = parse_network_group(&group_text).expect("group parses");
    let layout = parse_network_array(&text(&root.join("streamflow/zarr.json")), &group)
        .expect("array parses");
    let shard = bytes(&root.join(network::NETWORK_SHARD_PATH));
    let index = read_network_index(&layout, &shard).expect("index parses");
    Store {
        group,
        layout,
        shard,
        index,
    }
}

/// Every frame, read twice: from the whole shard, and from the one range
/// [`frame_range`] names, as a reader fetching by range would.
fn frames(store: &Store) -> Vec<Vec<u8>> {
    (0..store.layout.frame_count)
        .map(|frame| {
            let whole =
                decode_network_frame(&store.layout, &store.index, frame, &store.shard, 0).unwrap();
            let (start, end) = frame_range(&store.layout, &store.index, frame)
                .unwrap()
                .expect("every time chunk is written");
            let ranged = decode_network_frame(
                &store.layout,
                &store.index,
                frame,
                &store.shard[start as usize..end as usize],
                start,
            )
            .unwrap();
            assert_eq!(whole, ranged, "frame {frame} differs read by range");
            whole
        })
        .collect()
}

#[test]
fn synthetic_stores_decode_to_the_readme_codes() {
    for (name, delta) in [("delta.zarr", true), ("plain.zarr", false)] {
        let store = open(&fixture(name));
        assert_eq!(store.layout.delta, delta, "{name}");
        assert_eq!(store.layout.frame_count, 8);
        assert_eq!(store.layout.feature_count, 20);
        assert_eq!(store.layout.row_group_rows, 8);
        assert_eq!(store.layout.chunk_count, 6);
        assert_eq!(store.layout.index_location, IndexLocation::End);
        assert_eq!(store.group.model, "NWM v3.0");
        assert_eq!(store.group.product, "short_range");
        assert_eq!(store.group.run_time, "2025-07-04T00:00:00Z");
        assert_eq!(store.group.geometry.path, "reaches.parquet");
        assert_eq!(store.group.geometry.feature_count, 20);

        let decoded = frames(&store);
        for (frame, codes) in decoded.iter().enumerate() {
            assert_eq!(
                codes.as_slice(),
                EXPECTED[frame].as_slice(),
                "{name} frame {frame}"
            );
        }
        let chunk = decode_time_chunk(&store.layout, &store.index, 1, &store.shard, 0).unwrap();
        assert_eq!(
            chunk.len(),
            2 * 20,
            "the last time chunk is trimmed to the axis"
        );

        for feature in [0u32, 4, 9, 13, 19] {
            let series =
                decode_network_series(&store.layout, &store.index, feature, &store.shard, 0)
                    .unwrap();
            let want: Vec<u8> = EXPECTED.iter().map(|row| row[feature as usize]).collect();
            assert_eq!(series, want, "{name} series of reach {feature}");
        }
    }
}

#[test]
fn synthetic_values_dequantize_within_a_code() {
    let store = open(&fixture("delta.zarr"));
    let quantization = store.group.variable.quantization;
    for feature in 0..20u32 {
        let codes =
            decode_network_series(&store.layout, &store.index, feature, &store.shard, 0).unwrap();
        let values = dequantize(&quantization, &codes);
        for (frame, (&code, &value)) in codes.iter().zip(&values).enumerate() {
            let t = frame as f64;
            let f = f64::from(feature);
            let source = 0.5 + 900.0 * (-(t - f / 4.0).powi(2) / 3.0).exp();
            match code {
                255 => assert!(value.is_nan()),
                0 => assert_eq!(value, 0.0),
                254 => assert!(value > 100_000.0),
                // A round trip lands within half a code: the grid is
                // (ln(1 + 100000) - ln(1 + 0.01)) / 252 = 0.0457 in log1p
                // space, which is 4.7 % relative far above 1 m3/s but
                // wider below (the (1 + v) / v factor), so the check is in
                // the codebook's own space.
                _ => assert!(
                    ((value + 1.0).ln() - (source + 1.0).ln()).abs() < 0.0235,
                    "reach {feature} frame {frame}: {value} against {source}"
                ),
            }
        }
    }
    // The fixture's own spot values: the overflow, the dry cell and the lake.
    assert_eq!(dequantize(&quantization, &[0])[0], 0.0);
    assert!((dequantize(&quantization, &[150])[0] - 907.067_327_6).abs() < 1e-3);
    assert!(dequantize(&quantization, &[255])[0].is_nan());
}

#[test]
fn synthetic_side_arrays() {
    let root = fixture("delta.zarr");
    let store = open(&root);
    parse_side_array(
        &text(&root.join("feature_id/zarr.json")),
        "int64",
        "feature",
        20,
    )
    .unwrap();
    parse_side_array(&text(&root.join("time/zarr.json")), "int64", "time", 8).unwrap();
    parse_side_array(
        &text(&root.join("baseflow/zarr.json")),
        "float32",
        "feature",
        20,
    )
    .unwrap();
    let ids =
        read_feature_ids(&store.layout, &bytes(&root.join(network::FEATURE_ID_PATH))).unwrap();
    assert_eq!(ids, (0..20).map(|i| 1000 + 3 * i).collect::<Vec<i64>>());
    let times = read_frame_times(&store.group, &bytes(&root.join(network::TIME_PATH))).unwrap();
    assert_eq!(
        times,
        (0..8).map(|i| FIRST_FRAME + 3600 * i).collect::<Vec<i64>>()
    );
    let baseflow =
        read_baseflow(&store.layout, &bytes(&root.join(network::BASEFLOW_PATH))).unwrap();
    assert_eq!(baseflow.len(), 20);
    assert!((baseflow[0] - 0.1).abs() < 1e-6 && (baseflow[19] - 2.0).abs() < 1e-6);
    assert!(baseflow[13].is_nan());
}

/// The same shard with its index moved to the front, offsets shifted past it
/// and the CRC-32C recomputed: what `index_location: "start"` writes.
#[test]
fn an_index_at_the_start_reads_the_same() {
    let root = fixture("delta.zarr");
    let store = open(&root);
    let length = store.layout.index_length();
    let data = &store.shard[..store.shard.len() - length];
    let mut index = Vec::with_capacity(length);
    for entry in &store.index {
        let entry = entry.expect("the writer writes every chunk");
        index.extend_from_slice(&(entry.offset + length as u64).to_le_bytes());
        index.extend_from_slice(&entry.length.to_le_bytes());
    }
    let checksum = crc32c(&index);
    index.extend_from_slice(&checksum.to_le_bytes());
    let shard = [index.as_slice(), data].concat();

    let array = text(&root.join("streamflow/zarr.json"))
        .replace(r#""index_location": "end""#, r#""index_location": "start""#);
    let layout = parse_network_array(&array, &store.group).unwrap();
    assert_eq!(layout.index_location, IndexLocation::Start);
    let moved = read_network_index(&layout, &shard).unwrap();
    for frame in 0..8u32 {
        let codes = decode_network_frame(&layout, &moved, frame, &shard, 0).unwrap();
        assert_eq!(codes.as_slice(), EXPECTED[frame as usize].as_slice());
    }
}

#[test]
fn a_damaged_shard_is_refused() {
    let store = open(&fixture("delta.zarr"));
    let length = store.layout.index_length();

    let mut index_flip = store.shard.clone();
    let at = index_flip.len() - length + 3;
    index_flip[at] ^= 1;
    assert!(read_network_index(&store.layout, &index_flip).is_err());

    let mut chunk_flip = store.shard.clone();
    let first = store.index[0].unwrap();
    chunk_flip[(first.offset + first.length / 2) as usize] ^= 0x55;
    assert!(decode_network_frame(&store.layout, &store.index, 0, &chunk_flip, 0).is_err());

    // A range that does not cover the chunk it is asked for.
    let (start, end) = frame_range(&store.layout, &store.index, 0)
        .unwrap()
        .unwrap();
    let short = &store.shard[start as usize..(end - 1) as usize];
    assert!(decode_network_frame(&store.layout, &store.index, 0, short, start).is_err());
    assert!(decode_network_frame(&store.layout, &store.index, 8, &store.shard, 0).is_err());
    assert!(decode_network_series(&store.layout, &store.index, 20, &store.shard, 0).is_err());
    assert!(read_network_index(&store.layout, &store.shard[..length - 1]).is_err());
}

#[test]
fn a_group_of_another_branch_or_version_is_refused() {
    let root = fixture("delta.zarr");
    let group: serde_json::Value = serde_json::from_str(&text(&root.join("zarr.json"))).unwrap();
    for (key, value) in [
        ("xue", serde_json::json!({"schemaVersion": 3})),
        ("xue_polar", serde_json::json!({"version": 1})),
    ] {
        let mut both = group.clone();
        both["attributes"][key] = value;
        assert!(
            parse_network_group(&both.to_string()).is_err(),
            "{key} beside xue_network"
        );
        assert!(zarr::store_kind(&both.to_string()).is_err());
    }
    let mut future = group.clone();
    future["attributes"]["xue_network"]["version"] = serde_json::json!(2);
    assert!(parse_network_group(&future.to_string()).is_err());
    assert!(zarr::parse_group_metadata(&group.to_string()).is_err());
}

// -- the Guadalupe 2025 showcase case ------------------------------------------

fn guadalupe() -> Option<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../web/public/data/showcase/guadalupe-2025/river");
    if root.join("index.json").is_file() {
        Some(root)
    } else {
        eprintln!("skipping: the Guadalupe 2025 case's river data is not in this checkout");
        None
    }
}

#[test]
fn guadalupe_tracks_read_whole() {
    let Some(root) = guadalupe() else { return };
    let index: serde_json::Value = serde_json::from_str(&text(&root.join("index.json"))).unwrap();
    // (track, frames, first valid time, nodata codes, sum of codes, Hunt series head)
    let tracks = [
        (
            "analysis",
            73u32,
            1_751_500_800i64,
            18_615usize,
            10_476_300u64,
            [4u8, 4, 4, 4, 4, 4, 4, 4],
        ),
        (
            "short_range-2025070409",
            18,
            1_751_623_200,
            4_590,
            2_330_274,
            [158, 162, 161, 155, 148, 143, 138, 134],
        ),
    ];
    for (id, frame_count, first_time, nodata, sum, head) in tracks {
        let store_root = root.join(format!("{id}.zarr"));
        let group_bytes = bytes(&store_root.join("zarr.json"));
        let track = index["tracks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|track| track["id"] == id)
            .expect("track in the index");
        assert_eq!(
            format!("{:08x}", crc32fast::hash(&group_bytes)),
            track["store"]["crc32"].as_str().unwrap(),
            "{id}: the store's crc32 is its group document's"
        );
        let store = open(&store_root);
        assert_eq!(store.layout.frame_count, frame_count);
        assert_eq!(store.layout.feature_count, 10_940);
        assert_eq!(store.layout.row_group_rows, 4096);
        assert_eq!(store.layout.feature_chunks, 3);
        assert!(store.layout.delta);
        assert_eq!(store.group.geometry, {
            let geometry = &index["geometry"];
            network::NetworkGeometry {
                path: geometry["path"].as_str().unwrap().to_owned(),
                byte_length: geometry["byteLength"].as_u64().unwrap(),
                crc32: geometry["crc32"].as_str().unwrap().to_owned(),
                feature_count: 10_940,
                row_group_rows: 4096,
            }
        });

        let decoded = frames(&store);
        let codes: Vec<u8> = decoded.concat();
        assert_eq!(
            codes.iter().filter(|&&code| code == 255).count(),
            nodata,
            "{id}"
        );
        assert_eq!(
            codes.iter().map(|&code| u64::from(code)).sum::<u64>(),
            sum,
            "{id}"
        );

        // The Guadalupe at Hunt's reach: its series is its column of the frames.
        let hunt = 5366u32;
        let series =
            decode_network_series(&store.layout, &store.index, hunt, &store.shard, 0).unwrap();
        assert_eq!(&series[..8], &head, "{id}");
        let column: Vec<u8> = decoded.iter().map(|frame| frame[hunt as usize]).collect();
        assert_eq!(series, column, "{id}");

        let times =
            read_frame_times(&store.group, &bytes(&store_root.join(network::TIME_PATH))).unwrap();
        assert_eq!(times[0], first_time);
        let ids = read_feature_ids(
            &store.layout,
            &bytes(&store_root.join(network::FEATURE_ID_PATH)),
        )
        .unwrap();
        let mut unique = ids.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(unique.len(), ids.len(), "{id}: feature ids are unique");
        let baseflow = read_baseflow(
            &store.layout,
            &bytes(&store_root.join(network::BASEFLOW_PATH)),
        )
        .unwrap();
        assert_eq!(baseflow.len(), 10_940);
    }
}

#[test]
fn guadalupe_analysis_peaks_at_hunt() {
    let Some(root) = guadalupe() else { return };
    let store = open(&root.join("analysis.zarr"));
    let codes = decode_network_series(&store.layout, &store.index, 5366, &store.shard, 0).unwrap();
    let values = dequantize(&store.group.variable.quantization, &codes);
    let (peak_frame, peak) = values
        .iter()
        .copied()
        .enumerate()
        .filter(|(_, value)| value.is_finite())
        .max_by(|a, b| a.1.total_cmp(&b.1))
        .unwrap();
    assert_eq!(peak_frame, 34);
    assert!((peak - 2841.64).abs() < 0.01, "peak {peak}");
}
