/**
 * One open store: residency, decoding and assembly, with no worker in it.
 *
 * The session is what the Zarr worker drives and what the unit tests and
 * the measurement harness drive directly. It holds the group's metadata,
 * one array layout per variable, the shard indices and compressed inner
 * chunks it has fetched (kept for the session's life, as the streaming
 * `.xue` reader keeps its payloads, so a chunk is fetched once), and one
 * plane buffer per variable that a tile decode writes its rectangles into —
 * cells outside them keep what the buffer held, which is the coverage
 * contract the `.xue` worker's tiled decode already has.
 *
 * Decoding itself is the container's chunk path, `decodeChunk` from the
 * WASM module, injected so the session can be opened where the module is
 * loaded differently (a worker's `?url` import, a test's file read). A
 * decoded chunk is cached for the time chunk being read, per variable, and
 * dropped when the read moves on, which keeps scrubbing inside six frames
 * free and memory at one time chunk's tiles.
 *
 * A shard's index is read once, on the first chunk wanted from it — a
 * suffix range, no object length needed — and held. On a store the
 * exporter writes an array is one shard, so that one read is the whole
 * index, the way the container's structural prefix carries its whole
 * index: a series then costs one request per time chunk and a frame one
 * per tile row, never a dependent index read in between.
 */

import { frameOffsets } from "../manifest";
import type { TileRect } from "../tiles";
import {
  framesInTimeChunk,
  parseArrayMetadata,
  parseGroupMetadata,
  parseShardIndex,
  placeTile,
  PREDICTOR_PREVIOUS,
  PREDICTOR_RAW,
  shardIndexLength,
  shardOf,
  shardPath,
  tileOf,
  tileOrigin,
  type ShardIndexEntry,
  type ZarrArrayLayout,
  type ZarrGroup,
} from "./shard";
import type { ZarrStore } from "./store";

export type DecodeChunkFn = (bytes: Uint8Array, frames: number, height: number, width: number, predictor: number) => Uint8Array;

/** One inner chunk's address: a variable, a time chunk, a tile. */
export interface ChunkKey {
  variableId: number;
  timeChunk: number;
  tile: number;
}

interface VariableArray {
  id: string;
  layout: ZarrArrayLayout;
  plane: Uint8Array;
  /** The time chunk whose decoded tiles `decoded` holds. */
  decodedTimeChunk: number;
  decoded: Map<number, Uint8Array | null>;
}

function chunkKey(key: ChunkKey): string {
  return `${key.variableId}:${key.timeChunk}:${key.tile}`;
}

function indexKey(variableId: number, shard: number): string {
  return `${variableId}:${shard}`;
}

export class ZarrSession {
  /** Bytes of shard indices and inner chunks held, for `progress`; an
   * index counts here the way the container's prefix does. */
  residentBytes = 0;
  /** The most compressed chunk bytes the session keeps. A full satellite
   * disk's Dust RGB window is some 400 MB of chunks, and a session that
   * held every chunk it ever fetched would hold all of it after one pass
   * of the axis; instead the chunks are a least-recently-used cache under
   * this budget (`payloadBytes` counts them), so the worker's memory is
   * bounded by the window ahead of the playhead, not by the axis. A chunk
   * a decode is assembling is never in danger: `decodeFrame` re-fetches
   * what an eviction took between its fetch and its assembly. Infinity
   * keeps everything, as the session always did. */
  payloadBudgetBytes = Number.POSITIVE_INFINITY;
  /** Compressed chunk bytes held right now, the part of `residentBytes`
   * the budget governs. */
  payloadBytes = 0;

  private readonly arrays = new Map<number, VariableArray>();
  private readonly indices = new Map<string, (ShardIndexEntry | null)[]>();
  private readonly indexFetches = new Map<string, Promise<(ShardIndexEntry | null)[]>>();
  /** Compressed inner chunks, `null` for one the shard never held. */
  private readonly payloads = new Map<string, Uint8Array | null>();
  private readonly chunkFetches = new Map<string, Promise<void>>();
  private readonly offsetIndex: Map<number, number>;

  private constructor(
    readonly store: ZarrStore,
    private readonly decodeChunk: DecodeChunkFn,
    readonly group: ZarrGroup,
    arrays: { numericId: number; id: string; layout: ZarrArrayLayout }[],
  ) {
    this.offsetIndex = new Map(frameOffsets(group.metadata.time).map((offset, index) => [offset, index]));
    for (const array of arrays) {
      this.arrays.set(array.numericId, {
        id: array.id,
        layout: array.layout,
        plane: new Uint8Array(array.layout.height * array.layout.width),
        decodedTimeChunk: -1,
        decoded: new Map(),
      });
    }
  }

  /** Read the group and every variable array's metadata. Every array must
   * describe the grid and axis the group's metadata declares, cut the same
   * way — one tiling per bundle, as the container has. */
  static async open(
    store: ZarrStore,
    decodeChunk: DecodeChunkFn,
    options: { payloadBudgetBytes?: number } = {},
  ): Promise<ZarrSession> {
    const group = parseGroupMetadata(new TextDecoder().decode(await store.get("zarr.json")));
    const { metadata } = group;
    const arrays = await Promise.all(
      metadata.variables.map(async (variable) => {
        const layout = parseArrayMetadata(new TextDecoder().decode(await store.get(`${variable.id}/zarr.json`)));
        if (layout.height !== metadata.grid.height || layout.width !== metadata.grid.width) {
          throw new Error(`array ${variable.id} does not match the group's grid`);
        }
        if (layout.frameCount !== metadata.time.frameCount) {
          throw new Error(`array ${variable.id} does not match the group's time axis`);
        }
        return { numericId: variable.numericId, id: variable.id, layout };
      }),
    );
    const first = arrays[0]!.layout;
    for (const array of arrays) {
      const { layout } = array;
      if (
        layout.tileHeight !== first.tileHeight ||
        layout.tileWidth !== first.tileWidth ||
        layout.timeChunk !== first.timeChunk ||
        layout.shardFrames !== first.shardFrames
      ) {
        throw new Error("every array of a store must be cut the same way");
      }
    }
    const session = new ZarrSession(store, decodeChunk, group, arrays);
    if (options.payloadBudgetBytes !== undefined && options.payloadBudgetBytes > 0) {
      session.payloadBudgetBytes = options.payloadBudgetBytes;
    }
    return session;
  }

  get metadataJson(): string {
    return this.group.metadataJson;
  }

  get planeLength(): number {
    const { layout } = this.firstArray();
    return layout.height * layout.width;
  }

  /** The bundle's variables, in file order — the ids a decode names. */
  get numericIds(): number[] {
    return [...this.arrays.keys()];
  }

  /** `[tileWidth, tileHeight, columns, rows]`, the shape `tileGeometry()`
   * reports on a v2 bundle. */
  tileGeometry(): Uint32Array {
    const { layout } = this.firstArray();
    return Uint32Array.from([layout.tileWidth, layout.tileHeight, layout.tileColumns, layout.tileRows]);
  }

  /** The frame offsets on the axis, ascending. */
  get frameOffsets(): number[] {
    return [...this.offsetIndex.keys()];
  }

  clearCache(): void {
    for (const array of this.arrays.values()) {
      array.decoded.clear();
      array.decodedTimeChunk = -1;
    }
  }

  private firstArray(): VariableArray {
    const first = this.arrays.values().next().value;
    if (!first) throw new Error("store has no variables");
    return first;
  }

  private array(variableId: number): VariableArray {
    const array = this.arrays.get(variableId);
    if (!array) throw new Error(`no array for variable ${variableId}`);
    return array;
  }

  private frameIndex(frameOffset: number): number {
    const index = this.offsetIndex.get(frameOffset);
    if (index === undefined) throw new Error("no plane for the requested variable and forecast hour");
    return index;
  }

  /** The tiles a rectangle list names, in row-major order, or every tile. */
  private tilesFor(layout: ZarrArrayLayout, rects: readonly TileRect[] | null): number[] {
    if (!rects) return Array.from({ length: layout.tileCount }, (_, tile) => tile);
    const tiles = new Set<number>();
    for (const rect of rects) {
      for (let row = rect.firstRow; row <= rect.lastRow; row += 1) {
        for (let column = rect.firstColumn; column <= rect.lastColumn; column += 1) {
          if (row < 0 || column < 0 || row >= layout.tileRows || column >= layout.tileColumns) {
            throw new Error("tile rectangle is outside the grid");
          }
          tiles.add(row * layout.tileColumns + column);
        }
      }
    }
    return [...tiles].sort((a, b) => a - b);
  }

  /** The chunks a frame needs within the given tiles, as chunk keys. */
  chunksFor(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): ChunkKey[] {
    const { layout } = this.array(variableId);
    const timeChunk = Math.floor(this.frameIndex(frameOffset) / layout.timeChunk);
    return this.tilesFor(layout, rects).map((tile) => ({ variableId, timeChunk, tile }));
  }

  isResident(key: ChunkKey): boolean {
    return this.payloads.has(chunkKey(key));
  }

  isFetching(key: ChunkKey): boolean {
    return this.chunkFetches.has(chunkKey(key));
  }

  /** Whether every chunk a frame needs within the tiles is local. */
  frameResident(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): boolean {
    return this.chunksFor(variableId, frameOffset, rects).every((key) => this.isResident(key));
  }

  private ensureIndex(variableId: number, shard: number): Promise<(ShardIndexEntry | null)[]> {
    const key = indexKey(variableId, shard);
    const held = this.indices.get(key);
    if (held) return Promise.resolve(held);
    const running = this.indexFetches.get(key);
    if (running) return running;
    const { id, layout } = this.array(variableId);
    if (shard < 0 || shard >= layout.shardCount) throw new Error("shard is outside the array");
    const length = shardIndexLength(layout.chunksPerShard);
    const path = shardPath(id, shard);
    // The index sits at whichever end the array declares; at the end it is
    // a suffix range, which needs no object length, and at the start a
    // prefix of known length.
    const task = (async () => {
      try {
        const bytes = await this.store.getRange(
          path,
          layout.indexLocation === "end" ? { suffixLength: length } : { offset: 0, length },
        );
        const index = parseShardIndex(bytes, layout.chunksPerShard);
        this.indices.set(key, index);
        this.residentBytes += bytes.byteLength;
        return index;
      } finally {
        this.indexFetches.delete(key);
      }
    })();
    this.indexFetches.set(key, task);
    return task;
  }

  /** Make one inner chunk's bytes local, deduplicating concurrent requests.
   * Ranges for the chunks of one shard issued in the same turn are merged
   * by the store, so a caller asking for a tile row asks in a loop. */
  ensureChunk(key: ChunkKey): Promise<void> {
    const name = chunkKey(key);
    if (this.payloads.has(name)) {
      this.touchPayload(name);
      return Promise.resolve();
    }
    const running = this.chunkFetches.get(name);
    if (running) return running;
    const { id, layout } = this.array(key.variableId);
    if (key.timeChunk < 0 || key.timeChunk >= layout.timeChunks) throw new Error("time chunk is outside the array");
    const task = (async () => {
      try {
        const { shard, position } = shardOf(layout, key.timeChunk);
        const index = await this.ensureIndex(key.variableId, shard);
        const entry = index[position + key.tile];
        if (entry === undefined) throw new Error("tile is outside the shard");
        if (entry === null) {
          this.payloads.set(name, null);
          return;
        }
        const bytes = await this.store.getRange(shardPath(id, shard), entry);
        this.payloads.set(name, bytes);
        this.residentBytes += bytes.byteLength;
        this.payloadBytes += bytes.byteLength;
        this.evictPayloads(name);
      } finally {
        this.chunkFetches.delete(name);
      }
    })();
    this.chunkFetches.set(name, task);
    return task;
  }

  ensureChunks(keys: readonly ChunkKey[]): Promise<void> {
    return Promise.all(keys.map((key) => this.ensureChunk(key))).then(() => undefined);
  }

  /** Move a held chunk to the newest end of the cache's order. A `Map`
   * iterates in insertion order, so re-inserting is the whole LRU. */
  private touchPayload(name: string): void {
    const held = this.payloads.get(name);
    if (held === undefined) return;
    this.payloads.delete(name);
    this.payloads.set(name, held);
  }

  /** Drop the least recently used chunks until the rest fit the budget,
   * never the one just inserted (`keep`). A chunk the shard never held is
   * a `null` entry that costs nothing and is left alone: it is what says
   * "no data here" without a request. */
  private evictPayloads(keep: string): void {
    if (this.payloadBytes <= this.payloadBudgetBytes) return;
    for (const [name, bytes] of this.payloads) {
      if (this.payloadBytes <= this.payloadBudgetBytes) break;
      if (name === keep || bytes === null) continue;
      this.payloads.delete(name);
      this.payloadBytes -= bytes.byteLength;
      this.residentBytes -= bytes.byteLength;
    }
  }

  /** One inner chunk decoded to codes at the full inner shape, or `null`
   * for a chunk the shard never held. */
  private decodedChunk(array: VariableArray, key: ChunkKey): Uint8Array | null {
    if (array.decodedTimeChunk !== key.timeChunk) {
      array.decoded.clear();
      array.decodedTimeChunk = key.timeChunk;
    }
    const held = array.decoded.get(key.tile);
    if (held !== undefined) return held;
    const chunk = this.buildChunk(array, key);
    array.decoded.set(key.tile, chunk);
    return chunk;
  }

  private buildChunk(array: VariableArray, key: ChunkKey): Uint8Array | null {
    const payload = this.payloads.get(chunkKey(key));
    if (payload === undefined) throw new Error("chunk is not resident yet");
    if (payload === null) return null;
    const { layout } = array;
    return this.decodeChunk(
      payload,
      layout.timeChunk,
      layout.tileHeight,
      layout.tileWidth,
      layout.delta ? PREDICTOR_PREVIOUS : PREDICTOR_RAW,
    );
  }

  /** Assemble one frame into the variable's plane buffer and return the
   * buffer. With `rects` only those tiles are fetched and written; the rest
   * of the buffer is whatever earlier decodes left there. */
  async decodeFrame(variableId: number, frameOffset: number, rects: readonly TileRect[] | null = null): Promise<Uint8Array> {
    const array = this.array(variableId);
    const { layout } = array;
    const index = this.frameIndex(frameOffset);
    const keys = this.chunksFor(variableId, frameOffset, rects);
    // Every chunk local, then the assembly in one synchronous pass, so no
    // other decode's writes to the shared plane interleave with this one.
    // A concurrent insert may have evicted one of these between its fetch
    // and now, when the budget is tighter than what is being asked for at
    // once; those are fetched again, a bounded number of times.
    for (let attempt = 0; ; attempt += 1) {
      await this.ensureChunks(keys);
      const missing = keys.filter((key) => !this.payloads.has(chunkKey(key)));
      if (missing.length === 0) break;
      if (attempt >= 3) throw new Error("chunks evicted faster than a frame assembles; raise the payload budget");
    }
    const frameInChunk = index % layout.timeChunk;
    for (const key of keys) placeTile(array.plane, layout, key.tile, this.decodedChunk(array, key), frameInChunk);
    return array.plane;
  }

  /** The chunks one cell's series needs: one per time chunk of its tile. */
  seriesChunks(variableId: number, column: number, row: number): ChunkKey[] {
    const { layout } = this.array(variableId);
    const tile = tileOf(layout, row, column);
    return Array.from({ length: layout.timeChunks }, (_, timeChunk) => ({ variableId, timeChunk, tile }));
  }

  /** One cell's code on every frame of the axis. Chunks are read straight
   * through rather than cached: each is touched once. */
  async decodeSeries(variableId: number, column: number, row: number): Promise<Uint8Array> {
    const array = this.array(variableId);
    const { layout } = array;
    const keys = this.seriesChunks(variableId, column, row);
    await this.ensureChunks(keys);
    const origin = tileOrigin(layout, keys[0]!.tile);
    const cell = (row - origin.row) * layout.tileWidth + (column - origin.column);
    const stride = layout.tileHeight * layout.tileWidth;
    const series = new Uint8Array(layout.frameCount);
    for (const key of keys) {
      const frames = framesInTimeChunk(layout, key.timeChunk);
      // A series touches every time chunk of one tile, more than a tight
      // budget may hold at once; a chunk evicted since the fetch above is
      // fetched again here, one at a time.
      if (!this.payloads.has(chunkKey(key))) await this.ensureChunk(key);
      const chunk = array.decodedTimeChunk === key.timeChunk ? this.decodedChunk(array, key) : this.buildChunk(array, key);
      for (let frame = 0; frame < frames; frame += 1) {
        series[key.timeChunk * layout.timeChunk + frame] = chunk === null ? layout.fillValue : chunk[frame * stride + cell]!;
      }
    }
    return series;
  }
}
