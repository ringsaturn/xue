//! `zarr::ArrayReader` against the container.
//!
//! The web fixtures (`tests/prepare_web_fixture.py`) hold each bundle as a
//! `.xue` and as the stores exported from it. A frame the reader assembles
//! from a store's shards must be, byte for byte, the frame `Bundle` decodes
//! from the container: one shard with its index at the end (the shared
//! stores), the delta chain with its index at the start, a RAW variable and a
//! two-variable bundle.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Once;

use xue::zarr::{self, ArrayLayout, ArrayReader, IndexLocation};
use xue::{Bundle, FrameRequest, TileRect};

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture_root() -> PathBuf {
    repository_root().join("tests/fixtures/generated/web")
}

/// The fixtures are generated and gitignored; build them once on a fresh
/// checkout, the way the web unit tests do.
fn ensure_fixtures() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        if fixture_root().join("tmp2m.delta.zarr/zarr.json").exists()
            && fixture_root().join("bundles.half.zarr/zarr.json").exists()
        {
            return;
        }
        let python = std::env::var("PYTHON").unwrap_or_else(|_| ".venv/bin/python".to_owned());
        let status = Command::new(&python)
            .arg("tests/prepare_web_fixture.py")
            .current_dir(repository_root())
            .status()
            .unwrap_or_else(|error| panic!("cannot run {python}: {error}"));
        assert!(status.success(), "tests/prepare_web_fixture.py failed");
    });
}

fn read(path: PathBuf) -> Vec<u8> {
    std::fs::read(&path).unwrap_or_else(|error| panic!("cannot read {path:?}: {error}"))
}

fn text(path: PathBuf) -> String {
    String::from_utf8(read(path)).expect("a metadata document is UTF-8")
}

/// One variable array of a fixture store with every shard in memory, the
/// way a caller that owns the requests would hold them.
struct Array {
    reader: ArrayReader,
    shards: Vec<Vec<u8>>,
}

impl Array {
    fn open(store: &Path, id: &str) -> Array {
        let document = zarr::parse_array_document(&text(store.join(id).join("zarr.json")))
            .expect("the fixture's array reads");
        let layout = document.layout;
        let mut reader = ArrayReader::new(layout);
        let length = zarr::shard_index_length(layout.chunks_per_shard as usize);
        let shards: Vec<Vec<u8>> = (0..layout.shard_count)
            .map(|shard| read(store.join(zarr::shard_path(id, shard))))
            .collect();
        for (shard, bytes) in shards.iter().enumerate() {
            let index = match layout.index_location {
                IndexLocation::End => &bytes[bytes.len() - length..],
                IndexLocation::Start => &bytes[..length],
            };
            reader
                .set_shard_index(shard as u32, index)
                .expect("the fixture's shard index verifies");
        }
        Array { reader, shards }
    }

    fn layout(&self) -> ArrayLayout {
        *self.reader.layout()
    }

    /// Hand the reader one inner chunk out of the shard that holds it.
    fn feed(&mut self, time_chunk: u32, tile: u32) {
        let shard = self.reader.shard_of(time_chunk).unwrap();
        let payload = self.reader.chunk_range(time_chunk, tile).unwrap().map(|entry| {
            let start = entry.offset as usize;
            &self.shards[shard as usize][start..start + entry.length as usize]
        });
        self.reader.feed_chunk(time_chunk, tile, payload).unwrap();
    }

    /// One frame over `rects`, feeding whatever is not decoded yet.
    fn frame(&mut self, frame: u32, rects: Option<&[TileRect]>) -> Vec<u8> {
        let time_chunk = frame / self.layout().time_chunk;
        let tiles = self.reader.tiles_for(rects).unwrap();
        for tile in self.reader.undecoded_tiles(time_chunk, &tiles) {
            self.feed(time_chunk, tile);
        }
        assert!(self.reader.undecoded_tiles(time_chunk, &tiles).is_empty());
        self.reader.assemble_frame(frame, rects).unwrap()
    }
}

/// Every frame of every variable of `bundle`, read from `store`, is the
/// container's.
fn frames_match_the_container(store: &str, bundle: &str, container: &str) {
    ensure_fixtures();
    let store = fixture_root().join(store);
    let group = zarr::parse_group_metadata_for(&text(store.join("zarr.json")), bundle)
        .expect("the fixture's group reads");
    let mut reference =
        Bundle::open(&read(fixture_root().join(container))).expect("the fixture's container opens");
    for variable in &group.metadata.variables {
        let mut array = Array::open(&store, &variable.id);
        assert_eq!(array.layout().frame_count as usize, group.metadata.offsets.len());
        for (frame, &frame_offset) in group.metadata.offsets.iter().enumerate() {
            let plane = array.frame(frame as u32, None);
            let expected = reference
                .decode_frame(FrameRequest {
                    variable_id: variable.numeric_id,
                    frame_offset,
                })
                .expect("the container decodes the frame");
            assert!(
                plane == expected,
                "{} frame {frame} of {store:?} differs from {container}",
                variable.id
            );
        }
    }
}

#[test]
fn a_shared_store_reads_as_the_container_does() {
    frames_match_the_container("bundles.zarr", "tmp2m", "tmp2m.xue");
    // A RAW variable, and a bundle of two arrays.
    frames_match_the_container("bundles.zarr", "prate", "prate.xue");
    frames_match_the_container("bundles.zarr", "wind10m", "wind10m.xue");
}

#[test]
fn a_half_resolution_store_reads_as_its_container_does() {
    frames_match_the_container("bundles.half.zarr", "tmp2m", "tmp2m.half.xue");
}

#[test]
fn the_delta_chain_with_its_index_at_the_start_reads_as_the_container_does() {
    ensure_fixtures();
    let store = fixture_root().join("tmp2m.delta.zarr");
    let layout = Array::open(&store, "tmp2m").layout();
    assert!(layout.delta);
    assert_eq!(layout.index_location, IndexLocation::Start);
    frames_match_the_container("tmp2m.delta.zarr", "tmp2m", "tmp2m.xue");
}

#[test]
fn a_frame_over_rectangles_holds_their_tiles_and_zero_elsewhere() {
    ensure_fixtures();
    let store = fixture_root().join("bundles.zarr");
    let mut whole = Array::open(&store, "tmp2m");
    let layout = whole.layout();
    assert!(layout.tile_columns >= 3 && layout.tile_rows >= 3 && layout.time_chunks >= 2);
    // The second time chunk, so the frame is not the one a fresh reader
    // would hold by accident.
    let frame = layout.time_chunk + 1;
    let full = whole.frame(frame, None);

    // Two rectangles that overlap in one tile, and the clipped last tile.
    let rects = [
        TileRect { first_column: 1, first_row: 1, last_column: 2, last_row: 2 },
        TileRect { first_column: 2, first_row: 2, last_column: 2, last_row: 2 },
        TileRect {
            first_column: layout.tile_columns - 1,
            first_row: layout.tile_rows - 1,
            last_column: layout.tile_columns - 1,
            last_row: layout.tile_rows - 1,
        },
    ];
    let mut partial = Array::open(&store, "tmp2m");
    let tiles = partial.reader.tiles_for(Some(&rects)).unwrap();
    let columns = layout.tile_columns;
    assert_eq!(
        tiles,
        [columns + 1, columns + 2, 2 * columns + 1, 2 * columns + 2, layout.tile_count - 1]
    );
    let plane = partial.frame(frame, Some(&rects));
    assert_eq!(plane.len(), full.len());
    for row in 0..layout.height {
        for column in 0..layout.width {
            let tile = zarr::tile_of(&layout, row, column).unwrap();
            let cell = (row * layout.width + column) as usize;
            let expected = if tiles.contains(&tile) { full[cell] } else { 0 };
            assert_eq!(plane[cell], expected, "cell ({row}, {column})");
        }
    }

    // No rectangle names no tile, which needs nothing fed, on any frame.
    let empty = partial.reader.assemble_frame(0, Some(&[])).unwrap();
    assert!(partial.reader.tiles_for(Some(&[])).unwrap().is_empty());
    assert!(empty.len() == full.len() && empty.iter().all(|&code| code == 0));

    // Only those tiles were fed: the whole plane, and another time chunk's
    // frame over the same tiles, are refused rather than assembled from
    // what is not there.
    assert!(partial.reader.assemble_frame(frame, None).is_err());
    assert!(partial.reader.assemble_frame(0, Some(&rects)).is_err());
    assert_eq!(partial.reader.undecoded_tiles(0, &tiles), tiles);
    // Reading the other time chunk drops this one's tiles.
    partial.frame(0, Some(&rects));
    assert_eq!(partial.reader.undecoded_tiles(frame / layout.time_chunk, &tiles), tiles);
    partial.reader.clear_cache();
    assert_eq!(partial.reader.undecoded_tiles(0, &tiles), tiles);
}

#[test]
fn a_chunk_the_shard_never_held_reads_as_the_fill_value() {
    ensure_fixtures();
    let mut array = Array::open(&fixture_root().join("bundles.zarr"), "tmp2m");
    let layout = array.layout();
    let rect = [TileRect { first_column: 0, first_row: 0, last_column: 1, last_row: 0 }];
    array.feed(0, 0);
    array.reader.feed_chunk(0, 1, None).unwrap();
    let plane = array.reader.assemble_frame(0, Some(&rect)).unwrap();
    let (rows, columns) = zarr::tile_shape(&layout, 1);
    let (origin_row, origin_column) = zarr::tile_origin(&layout, 1);
    for row in origin_row..origin_row + rows {
        for column in origin_column..origin_column + columns {
            assert_eq!(plane[(row * layout.width + column) as usize], layout.fill_value);
        }
    }
}

#[test]
fn a_reader_refuses_what_is_outside_the_array_or_not_a_chunk() {
    ensure_fixtures();
    let mut array = Array::open(&fixture_root().join("bundles.zarr"), "tmp2m");
    let layout = array.layout();
    let outside = [TileRect {
        first_column: 0,
        first_row: 0,
        last_column: layout.tile_columns,
        last_row: 0,
    }];
    assert!(array.reader.tiles_for(Some(&outside)).is_err());
    assert!(array.reader.feed_chunk(layout.time_chunks, 0, None).is_err());
    assert!(array.reader.feed_chunk(0, layout.tile_count, None).is_err());

    // A payload that is not the chunk is refused, and leaves what is held.
    array.frame(0, None);
    let tiles = array.reader.tiles_for(None).unwrap();
    assert!(array.reader.feed_chunk(1, 0, Some(b"not a zstandard frame")).is_err());
    assert!(array.reader.undecoded_tiles(0, &tiles).is_empty());
    assert!(array.reader.assemble_frame(0, None).is_ok());
    assert!(array.reader.assemble_frame(layout.frame_count, None).is_err());
}
