/**
 * Reading the Xue Zarr profile: the two metadata documents and the shape of
 * one variable's array.
 *
 * A store is one Zarr v3 group carrying a bundle's metadata JSON verbatim —
 * so the same `parseBundleMetadata` the `.xue` path uses reads it — and one
 * `uint8` array per variable. Under profile 1 the group is one bundle's and
 * `attributes.xue` is its metadata; under profile 2 the group is shared by
 * every bundle of one resolution tier and `attributes.xue_bundles` holds
 * each bundle's metadata by bundle id, so a store is opened by (path,
 * bundle id). Either way an array is sharded:
 * the inner chunk is one six-frame time chunk of one of the bundle's tiles,
 * each one Zstandard frame, and the outer chunk — the shard, one object —
 * is a whole number of time chunks of the whole grid, which the exporter
 * makes the whole axis, so an array is one object read by range, as the
 * container was. A shard's index is a `(offset, nbytes)` table of
 * little-endian `uint64` pairs plus a CRC-32C at one end of it, one pair per
 * inner chunk in row-major order over the shard's chunk grid: time chunk by
 * time chunk, the bundle's tiles in row-major order within each
 * (docs/zarr-profile.md is normative).
 *
 * The format is read by the Rust reader behind the WASM module (`xue::zarr`,
 * the one the data API reads through): which block of a group is a bundle's
 * metadata, how an array is cut, where a shard's index and an inner chunk
 * sit, with the index's CRC-32C verified before any of its offsets are used.
 * What stays here is what the shell adds: both documents are first held to
 * the validators generated from docs/schemas/, and the bundle's block is
 * read by the same `parseBundleMetadata` the `.xue` path uses. An array that
 * is not `uint8`, not sharded, or chained through codecs this profile does
 * not name is refused at open.
 *
 * The reader takes the shard's frame count from the array's own chunk shape
 * and handles any multiple of the time chunk, so a store cut one shard per
 * time chunk — the profile's earlier form, still on the bucket — reads
 * too; a `xue_index` attribute such a store carries is ignored.
 *
 * The one thing a reader must not assume is that a time chunk is a
 * container group. The store cuts its time axis every six frames regardless
 * of the axis's step; the container restarts its groups where the step
 * changes; so the two coincide only up to the first change of step, and a
 * frame is located by dividing its axis index, never by consulting a group.
 */

import { parseBundleMetadata, type BundleMetadata } from "../manifest";
import { checkStoreArray } from "../schema/generated/store-array";
import { checkStoreGroup } from "../schema/generated/store-group";
import type * as Wasm from "../wasm/xue";

/** What a session needs of the WASM module, injected so it can be opened
 * where the module is loaded differently (a worker's `?url` import, a
 * test's file read). */
export type ZarrCodec = Pick<typeof Wasm, "decodeChunk" | "parseZarrGroup" | "WasmZarrArray">;

/** One variable array as the WASM reader holds it: its layout and the shard
 * indices handed over so far. */
export type ZarrArrayReader = Wasm.WasmZarrArray;

/** Predictor codes as `decodeChunk` takes them — the container's own
 * numbering (docs/format.md). */
export const PREDICTOR_RAW = 0;
export const PREDICTOR_PREVIOUS = 2;

type IndexLocation = "start" | "end";

export interface ZarrGroup {
  /** The bundle metadata, as the group's `attributes.xue` carries it, or
   * the requested bundle's entry of `attributes.xue_bundles`. */
  metadata: BundleMetadata;
  /** That block as compact JSON, for the `ready` message's `metadataJson`. */
  metadataJson: string;
  profile: number;
}

const PROFILE_SINGLE = 1;

/** How one variable's array is cut. `frameCount`, `height` and `width` are
 * the array's shape; the tile and the time chunk are the inner chunk; the
 * shard is `shardFrames` frames — a multiple of the time chunk, the whole
 * axis rounded up on a store the exporter wrote — of the whole grid. Tile
 * rows and columns round the grid up, so the last row and column of tiles
 * may be clipped, and the last time chunk may hold fewer frames than the
 * chunk shape — both are padding the exporter fills with `fillValue` and a
 * reader trims. */
export interface ZarrArrayLayout {
  frameCount: number;
  height: number;
  width: number;
  timeChunk: number;
  tileHeight: number;
  tileWidth: number;
  tileRows: number;
  tileColumns: number;
  tileCount: number;
  timeChunks: number;
  /** Frames per shard: the outer chunk's time extent. */
  shardFrames: number;
  /** Shards along the axis: one on a store the exporter wrote. */
  shardCount: number;
  /** Inner chunks a shard's index names: its time chunks times the tiles. */
  chunksPerShard: number;
  /** The inner chain carries `xue.delta` in front of `bytes`: decode as
   * the PREVIOUS predictor rather than RAW. */
  delta: boolean;
  /** The bundle the array declares it belongs to (`attributes.xue.bundle`,
   * written on a shared store), or null where it declares none. */
  bundle: string | null;
  indexLocation: IndexLocation;
  fillValue: number;
}

// -- metadata ------------------------------------------------------------------

/** One bundle's block of a group, held to docs/schemas/store-group.schema.json
 * and read as bundle metadata. The envelope is rebuilt around the one bundle
 * a session reads: the other bundles of a shared store are each their own
 * session's to refuse, and the consolidated block is not read at all. */
export function groupOf(profile: number, metadataJson: string, bundle?: string): ZarrGroup {
  const block: unknown = JSON.parse(metadataJson);
  const attributes =
    profile === PROFILE_SINGLE ? { xue_profile: profile, xue: block } : { xue_profile: profile, xue_bundles: { [bundle ?? ""]: block } };
  checkStoreGroup({ zarr_format: 3, node_type: "group", attributes }, "group metadata");
  return { metadata: parseBundleMetadata(metadataJson), metadataJson, profile };
}

/** The group's `zarr.json`, read as `bundle`'s. A profile-1 group is one
 * bundle's whatever `bundle` says; a profile-2 group must name `bundle` in
 * its `xue_bundles`, and is refused without one asked for, since nothing
 * else in it says which bundle's metadata a session would be reading. */
export function readGroup(codec: ZarrCodec, text: string, bundle?: string): ZarrGroup {
  const group = codec.parseZarrGroup(text, bundle);
  try {
    return groupOf(group.profile, group.metadataJson, bundle);
  } finally {
    group.free();
  }
}

/** One variable array's `zarr.json`, held to docs/schemas/store-array.schema.json
 * and then to the profile: a `uint8` array of three dimensions on a regular
 * chunk grid, exactly one `sharding_indexed` codec whose inner chain is
 * `[bytes, zstd]` or `[xue.delta{axis: 0}, bytes, zstd]`, whose index chain
 * is `[bytes, crc32c]`, and whose outer chunk is whole time chunks of whole
 * tiles. The layout is the reader's, copied out once: it never changes. */
export function readArray(codec: ZarrCodec, text: string): { reader: ZarrArrayReader; layout: ZarrArrayLayout } {
  checkStoreArray(JSON.parse(text), "array metadata");
  const reader = codec.WasmZarrArray.fromMetadata(text);
  return {
    reader,
    layout: {
      bundle: reader.bundle ?? null,
      frameCount: reader.frameCount,
      height: reader.height,
      width: reader.width,
      timeChunk: reader.timeChunk,
      tileHeight: reader.tileHeight,
      tileWidth: reader.tileWidth,
      tileRows: reader.tileRows,
      tileColumns: reader.tileColumns,
      tileCount: reader.tileCount,
      timeChunks: reader.timeChunks,
      shardFrames: reader.shardFrames,
      shardCount: reader.shardCount,
      chunksPerShard: reader.chunksPerShard,
      delta: reader.delta,
      indexLocation: reader.indexAtEnd ? "end" : "start",
      fillValue: reader.fillValue,
    },
  };
}

// -- geometry ------------------------------------------------------------------

export function tileOrigin(layout: ZarrArrayLayout, tile: number): { row: number; column: number } {
  return {
    row: Math.floor(tile / layout.tileColumns) * layout.tileHeight,
    column: (tile % layout.tileColumns) * layout.tileWidth,
  };
}

/** A tile's clipped shape: what of it lies inside the grid. */
export function tileShape(layout: ZarrArrayLayout, tile: number): { height: number; width: number } {
  const origin = tileOrigin(layout, tile);
  return {
    height: Math.min(layout.tileHeight, layout.height - origin.row),
    width: Math.min(layout.tileWidth, layout.width - origin.column),
  };
}

export function tileOf(layout: ZarrArrayLayout, row: number, column: number): number {
  if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || column < 0 || row >= layout.height || column >= layout.width) {
    throw new Error("cell is outside the grid");
  }
  return Math.floor(row / layout.tileHeight) * layout.tileColumns + Math.floor(column / layout.tileWidth);
}

/** How many of a time chunk's frames lie on the axis; fewer than the chunk
 * shape on the last one. */
export function framesInTimeChunk(layout: ZarrArrayLayout, timeChunk: number): number {
  return Math.min(layout.timeChunk, layout.frameCount - timeChunk * layout.timeChunk);
}
