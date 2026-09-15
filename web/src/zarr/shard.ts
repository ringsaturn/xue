/**
 * Reading the Xue Zarr profile: the two metadata documents, the shard index,
 * and the arithmetic that maps a frame and a tile to a byte span.
 *
 * A store is one Zarr v3 group per bundle whose `attributes.xue` is the
 * bundle's metadata JSON verbatim — so the same `parseBundleMetadata` the
 * `.xue` path uses reads it — and one `uint8` array per variable, sharded:
 * the inner chunk is one six-frame time chunk of one of the bundle's tiles,
 * each one Zstandard frame, and the outer chunk — the shard, one object —
 * is a whole number of time chunks of the whole grid, which the exporter
 * makes the whole axis, so an array is one object read by range, as the
 * container was. A shard's index is a `(offset, nbytes)` table of
 * little-endian `uint64` pairs plus a CRC-32C at one end of it, one pair per
 * inner chunk in row-major order over the shard's chunk grid: time chunk by
 * time chunk, the bundle's tiles in row-major order within each
 * (docs/zarr-profile.md is normative). Everything here validates before it
 * trusts: an array that is not `uint8`, not sharded, or chained through
 * codecs this profile does not name is refused at open, and an index whose
 * checksum fails is refused before any of its offsets are used.
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

/** Predictor codes as `decodeChunk` takes them — the container's own
 * numbering (docs/format.md). */
export const PREDICTOR_RAW = 0;
export const PREDICTOR_PREVIOUS = 2;

export const DELTA_CODEC = "xue.delta";
export const INDEX_ENTRY_BYTES = 16;
export const INDEX_CHECKSUM_BYTES = 4;

export type IndexLocation = "start" | "end";

export interface ZarrGroup {
  /** The bundle metadata, as the group's `attributes.xue` carries it. */
  metadata: BundleMetadata;
  /** That block re-serialized, for the `ready` message's `metadataJson`. */
  metadataJson: string;
  profile: number;
}

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
  indexLocation: IndexLocation;
  fillValue: number;
}

export interface ShardIndexEntry {
  offset: number;
  length: number;
}

// -- CRC-32C -------------------------------------------------------------------

/** Castagnoli, the `crc32c` codec's checksum over a shard index — a
 * different polynomial from the CRC-32/IEEE the container uses. */
const CRC32C_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let byte = 0; byte < 256; byte += 1) {
    let value = byte;
    for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? (value >>> 1) ^ 0x82f63b78 : value >>> 1;
    table[byte] = value >>> 0;
  }
  return table;
})();

export function crc32c(bytes: Uint8Array): number {
  let value = 0xffffffff;
  for (let index = 0; index < bytes.byteLength; index += 1) {
    value = CRC32C_TABLE[(value ^ bytes[index]!) & 0xff]! ^ (value >>> 8);
  }
  return (value ^ 0xffffffff) >>> 0;
}

// -- metadata ------------------------------------------------------------------

function object(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error(`${what} is not an object`);
  return value as Record<string, unknown>;
}

function positiveInts(value: unknown, count: number, what: string): number[] {
  if (!Array.isArray(value) || value.length !== count) throw new Error(`${what} must list ${count} entries`);
  for (const item of value) {
    if (typeof item !== "number" || !Number.isInteger(item) || item <= 0) throw new Error(`${what} must be positive integers`);
  }
  return value as number[];
}

function codecNames(value: unknown, what: string): { names: string[]; codecs: Record<string, unknown>[] } {
  if (!Array.isArray(value)) throw new Error(`${what} must be a list`);
  const codecs = value.map((item) => object(item, `${what} entry`));
  return { names: codecs.map((codec) => String(codec.name)), codecs };
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index]);
}

/** The group's `zarr.json`. */
export function parseGroupMetadata(text: string): ZarrGroup {
  const group = object(JSON.parse(text), "group metadata");
  if (group.zarr_format !== 3 || group.node_type !== "group") throw new Error("not a Zarr v3 group");
  const attributes = object(group.attributes, "group attributes");
  const xue = attributes.xue;
  if (typeof xue !== "object" || xue === null) throw new Error("group attributes carry no xue metadata block");
  const metadataJson = JSON.stringify(xue);
  const profile = attributes.xue_profile;
  if (typeof profile !== "number" || !Number.isInteger(profile) || profile < 1) {
    throw new Error("group attributes carry no xue_profile version");
  }
  return { metadata: parseBundleMetadata(metadataJson), metadataJson, profile };
}

/** One variable array's `zarr.json`, held to the profile: a `uint8` array of
 * three dimensions on a regular chunk grid, exactly one `sharding_indexed`
 * codec whose inner chain is `[bytes, zstd]` or `[xue.delta{axis: 0}, bytes,
 * zstd]`, whose index chain is `[bytes, crc32c]`, and whose outer chunk is
 * whole time chunks of whole tiles. */
export function parseArrayMetadata(text: string): ZarrArrayLayout {
  const array = object(JSON.parse(text), "array metadata");
  if (array.zarr_format !== 3 || array.node_type !== "array") throw new Error("not a Zarr v3 array");
  if (array.data_type !== "uint8") throw new Error(`array data type must be uint8, not ${String(array.data_type)}`);
  const [frameCount, height, width] = positiveInts(array.shape, 3, "array shape") as [number, number, number];

  const chunkGrid = object(array.chunk_grid, "chunk grid");
  if (chunkGrid.name !== "regular") throw new Error("chunk grid must be regular");
  const shardShape = positiveInts(object(chunkGrid.configuration, "chunk grid configuration").chunk_shape, 3, "chunk shape");

  const keyEncoding = object(array.chunk_key_encoding, "chunk key encoding");
  if (keyEncoding.name !== "default") throw new Error("chunk key encoding must be default");
  const separator = keyEncoding.configuration === undefined ? "/" : object(keyEncoding.configuration, "chunk key encoding configuration").separator;
  if (separator !== undefined && separator !== "/") throw new Error("chunk key separator must be /");

  const fillValue = array.fill_value;
  if (typeof fillValue !== "number" || !Number.isInteger(fillValue) || fillValue < 0 || fillValue > 255) {
    throw new Error("fill value must be a uint8 code");
  }

  const outer = codecNames(array.codecs, "codecs");
  if (!sameList(outer.names, ["sharding_indexed"])) throw new Error("array must carry exactly one sharding_indexed codec");
  const sharding = object(outer.codecs[0]!.configuration, "sharding configuration");
  const [timeChunk, tileHeight, tileWidth] = positiveInts(sharding.chunk_shape, 3, "inner chunk shape") as [number, number, number];

  const inner = codecNames(sharding.codecs, "inner codecs");
  let delta = false;
  if (sameList(inner.names, [DELTA_CODEC, "bytes", "zstd"])) {
    const axis = object(inner.codecs[0]!.configuration ?? {}, "delta configuration").axis;
    if (axis !== 0) throw new Error("xue.delta must run along axis 0");
    delta = true;
  } else if (!sameList(inner.names, ["bytes", "zstd"])) {
    throw new Error(`inner codec chain [${inner.names.join(", ")}] is not one this profile reads`);
  }

  const index = codecNames(sharding.index_codecs, "index codecs");
  if (!sameList(index.names, ["bytes", "crc32c"])) throw new Error("index codecs must be [bytes, crc32c]");
  const endian = index.codecs[0]!.configuration === undefined ? "little" : object(index.codecs[0]!.configuration, "index bytes configuration").endian;
  if (endian !== "little") throw new Error("shard index must be little-endian");
  const indexLocation = sharding.index_location ?? "end";
  if (indexLocation !== "start" && indexLocation !== "end") throw new Error(`unknown index location ${String(indexLocation)}`);

  const tileRows = Math.ceil(height / tileHeight);
  const tileColumns = Math.ceil(width / tileWidth);
  const shardFrames = shardShape[0]!;
  if (shardFrames % timeChunk !== 0 || !sameList(shardShape.slice(1).map(String), [tileRows * tileHeight, tileColumns * tileWidth].map(String))) {
    throw new Error("outer chunk must be whole time chunks of whole tiles");
  }
  const tileCount = tileRows * tileColumns;
  return {
    frameCount,
    height,
    width,
    timeChunk,
    tileHeight,
    tileWidth,
    tileRows,
    tileColumns,
    tileCount,
    timeChunks: Math.ceil(frameCount / timeChunk),
    shardFrames,
    shardCount: Math.ceil(frameCount / shardFrames),
    chunksPerShard: (shardFrames / timeChunk) * tileCount,
    delta,
    indexLocation,
    fillValue,
  };
}

// -- shard index ---------------------------------------------------------------

export function shardIndexLength(chunkCount: number): number {
  return INDEX_ENTRY_BYTES * chunkCount + INDEX_CHECKSUM_BYTES;
}

const EMPTY_ENTRY = 0xffffffffffffffffn;

/** The `(offset, nbytes)` pairs of one shard, CRC-32C verified. `null`
 * marks an inner chunk the shard never held — the sentinel pair of all
 * ones — which reads as the fill value. `bytes` is exactly the index: the
 * caller fetched it as a suffix or a prefix of the shard. */
export function parseShardIndex(bytes: Uint8Array, chunkCount: number): (ShardIndexEntry | null)[] {
  if (bytes.byteLength !== shardIndexLength(chunkCount)) {
    throw new Error(`shard index is ${bytes.byteLength} bytes, not ${shardIndexLength(chunkCount)}`);
  }
  const entries = bytes.subarray(0, INDEX_ENTRY_BYTES * chunkCount);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(entries.byteLength, true) !== crc32c(entries)) throw new Error("shard index CRC-32C mismatch");
  const index: (ShardIndexEntry | null)[] = [];
  for (let position = 0; position < chunkCount; position += 1) {
    const offset = view.getBigUint64(position * INDEX_ENTRY_BYTES, true);
    const length = view.getBigUint64(position * INDEX_ENTRY_BYTES + 8, true);
    if (offset === EMPTY_ENTRY && length === EMPTY_ENTRY) {
      index.push(null);
      continue;
    }
    if (offset > BigInt(Number.MAX_SAFE_INTEGER) || length > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new Error("shard index entry exceeds the safe integer range");
    }
    if (length === 0n) throw new Error("shard index names an empty chunk");
    index.push({ offset: Number(offset), length: Number(length) });
  }
  return index;
}

// -- geometry ------------------------------------------------------------------

/** The object holding shard `s` of a variable's array: the default chunk
 * key encoding with the `/` separator, and the two grid dimensions always
 * at chunk 0 because a shard spans the whole grid. */
export function shardPath(variableId: string, shard: number): string {
  return `${variableId}/c/${shard}/0/0`;
}

/** Where a time chunk lives: which shard, and the position of its first
 * tile in that shard's index (row-major over the inner chunk grid, so a
 * time chunk's tiles are `tileCount` consecutive entries). */
export function shardOf(layout: ZarrArrayLayout, timeChunk: number): { shard: number; position: number } {
  const perShard = layout.shardFrames / layout.timeChunk;
  return { shard: Math.floor(timeChunk / perShard), position: (timeChunk % perShard) * layout.tileCount };
}

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

/** Copy frame `frameInChunk` of one decoded inner chunk — stored whole at
 * the inner shape — into a plane, trimming the padding past the grid's
 * edge. `chunk` is `null` for a chunk the shard never held, which is the
 * fill value throughout. */
export function placeTile(
  plane: Uint8Array,
  layout: ZarrArrayLayout,
  tile: number,
  chunk: Uint8Array | null,
  frameInChunk: number,
): void {
  const origin = tileOrigin(layout, tile);
  const shape = tileShape(layout, tile);
  const stride = layout.tileHeight * layout.tileWidth;
  for (let row = 0; row < shape.height; row += 1) {
    const target = (origin.row + row) * layout.width + origin.column;
    if (chunk === null) {
      plane.fill(layout.fillValue, target, target + shape.width);
      continue;
    }
    const source = frameInChunk * stride + row * layout.tileWidth;
    plane.set(chunk.subarray(source, source + shape.width), target);
  }
}
