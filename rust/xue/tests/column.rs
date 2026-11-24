//! Column store golden tests (`docs/zarr-profile.md`, "Column store").
//!
//! `tests/fixtures/column/` is committed: a map store, the column store cut
//! from it and the codes of some cells, all regenerated with
//!     .venv/bin/python tests/prepare_column_golden.py

use std::path::{Path, PathBuf};

use serde_json::Value;
use xue::column::{parse_column_index, ColumnGroup, ColumnReader};
use xue::zarr::{self, StoreKind};

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/column")
}

fn bytes(path: &Path) -> Vec<u8> {
    std::fs::read(path).unwrap_or_else(|error| panic!("cannot read {path:?}: {error}"))
}

fn text(path: &Path) -> String {
    String::from_utf8(bytes(path)).expect("a metadata document is UTF-8")
}

/// The committed store with every shard in memory, its indices handed over as
/// a reader fetching each by range would.
struct Store {
    reader: ColumnReader,
    /// By array, in the group's order, then by shard.
    shards: Vec<Vec<Vec<u8>>>,
}

impl Store {
    fn open() -> Store {
        let root = fixture().join("columns.zarr");
        let group_text = text(&root.join("zarr.json"));
        assert_eq!(zarr::store_kind(&group_text).unwrap(), StoreKind::Column);
        let group = ColumnGroup::parse(&group_text).expect("the fixture's group reads");
        let mut reader = ColumnReader::new(group.clone());
        let mut shards = Vec::new();
        for array in &group.arrays {
            let mut objects = Vec::new();
            for shard in 0..array.layout.shard_count {
                let object = bytes(&root.join(array.shard_path(shard)));
                let (start, end) = array.layout.shard_index_range(object.len() as u64).unwrap();
                reader
                    .set_shard_index(&array.id, shard, &object[start as usize..end as usize])
                    .expect("the fixture's shard index verifies");
                objects.push(object);
            }
            assert!(
                !root
                    .join(array.shard_path(array.layout.shard_count))
                    .exists(),
                "{} has more shards than its layout counts",
                array.id
            );
            shards.push(objects);
        }
        Store { reader, shards }
    }

    fn group(&self) -> &ColumnGroup {
        self.reader.group()
    }

    fn column(&self, array: usize, row: u32, column: u32) -> Vec<u8> {
        let entry = &self.group().arrays[array];
        let shard = entry.layout.shard_of(row, column).unwrap();
        let object = &self.shards[array][shard as usize];
        let payload = match self.reader.chunk_range(&entry.id, row, column).unwrap() {
            Some(span) => &object[span.offset as usize..][..span.length as usize],
            None => &[][..],
        };
        self.reader.column(&entry.id, row, column, payload).unwrap()
    }
}

#[test]
fn every_listed_cell_reads_its_codes() {
    let store = Store::open();
    let expected: Value = serde_json::from_str(&text(&fixture().join("expected.json"))).unwrap();
    let cells = expected["cells"].as_array().expect("cells");
    assert!(!cells.is_empty());
    for cell in cells {
        let row = cell["row"].as_u64().unwrap() as u32;
        let column = cell["column"].as_u64().unwrap() as u32;
        let arrays = cell["arrays"].as_object().unwrap();
        assert_eq!(arrays.len(), store.group().arrays.len());
        for (position, array) in store.group().arrays.iter().enumerate() {
            let frames = arrays[&array.id].as_array().unwrap();
            assert_eq!(frames.len(), array.offsets.len(), "{}", array.id);
            let want: Vec<u8> = frames
                .iter()
                .flat_map(|levels| levels.as_array().unwrap())
                .map(|code| code.as_u64().unwrap() as u8)
                .collect();
            assert_eq!(
                store.column(position, row, column),
                want,
                "{} at {row}, {column}",
                array.id
            );
        }
    }
}

/// The layouts the fixture is meant to hold: a grid that is no multiple of
/// the block or the shard, a chain of each kind, two level lists, an axis of
/// one frame and codebooks that differ by level.
#[test]
fn the_store_holds_every_shape_of_array() {
    let store = Store::open();
    let arrays = &store.group().arrays;
    assert!(arrays.iter().any(|array| array.layout.delta));
    assert!(arrays
        .iter()
        .any(|array| !array.layout.delta && array.layout.frame_count > 1));
    let mut levels: Vec<&[u32]> = arrays
        .iter()
        .filter_map(|array| array.levels.as_deref())
        .collect();
    levels.dedup();
    assert!(levels.len() > 1, "two level lists");
    assert!(arrays
        .iter()
        .any(|array| array.levels.is_none() && array.layout.frame_count == 1));
    assert!(arrays.iter().any(|array| {
        let first = array.members[0].variable.quantization;
        array
            .members
            .iter()
            .any(|member| member.variable.quantization != first)
    }));
    for array in arrays {
        let layout = &array.layout;
        assert_ne!(layout.height % layout.block_height, 0);
        assert_ne!(layout.width % layout.block_width, 0);
        assert!(layout.shard_count > 1);
        let codes = store.column(
            arrays
                .iter()
                .position(|other| other.id == array.id)
                .unwrap(),
            layout.height - 1,
            layout.width - 1,
        );
        assert_eq!(array.values(&codes).unwrap().len(), layout.column_length());
    }
}

/// Blocks wholly past the grid, which only the last band has, are never
/// written and every block on it is.
#[test]
fn only_blocks_past_the_grid_are_missing() {
    let store = Store::open();
    for (array, shards) in store.group().arrays.iter().zip(&store.shards) {
        let layout = &array.layout;
        for (shard, object) in shards.iter().enumerate() {
            let (start, end) = layout.shard_index_range(object.len() as u64).unwrap();
            let index = parse_column_index(layout, &object[start as usize..end as usize]).unwrap();
            let across = (layout.shard_width / layout.block_width) as usize;
            let mut missing = 0;
            for (position, entry) in index.iter().enumerate() {
                let row = shard as u32 * layout.shard_height
                    + (position / across) as u32 * layout.block_height;
                let column = (position % across) as u32 * layout.block_width;
                let on_grid = row < layout.height && column < layout.width;
                assert_eq!(entry.is_some(), on_grid, "{} {shard} {position}", array.id);
                missing += usize::from(!on_grid);
                if let Some(entry) = entry {
                    assert!(entry.offset + entry.length <= object.len() as u64);
                }
            }
            assert_eq!(
                missing > 0,
                shard + 1 == shards.len(),
                "{} {shard}",
                array.id
            );
        }
    }
}

/// A byte changed anywhere in a chunk is noticed by its frame's checksum.
#[test]
fn a_damaged_chunk_is_refused() {
    let store = Store::open();
    let array = &store.group().arrays[0];
    let span = store.reader.chunk_range(&array.id, 0, 0).unwrap().unwrap();
    let chunk = &store.shards[0][0][span.offset as usize..][..span.length as usize];
    for at in [chunk.len() / 2, chunk.len() - 1] {
        let mut damaged = chunk.to_vec();
        damaged[at] ^= 0x55;
        assert!(store.reader.column(&array.id, 0, 0, &damaged).is_err());
    }
}
