/**
 * One open bundle of a store: residency, decoding and assembly, with no
 * worker in it.
 *
 * The session is what the Zarr worker drives and what the unit tests and
 * the measurement harness drive directly. A store is opened as one bundle's:
 * a profile-1 store is one bundle's anyway, and a shared (profile-2) store
 * holds every bundle of a tier, of which the session reads only the one it
 * was opened for — that bundle's metadata out of the group and that bundle's
 * variable arrays, by id. It holds the bundle's metadata, one array reader
 * per variable, and the compressed inner chunks it has fetched, under a
 * budget. This is the side that waits: requests, range merging, what is
 * local and what is evicted.
 *
 * The format itself is read by the WASM module (`ZarrCodec`), injected so
 * the session can be opened where the module is loaded differently (a
 * worker's `?url` import, a test's file read). Each array's reader there
 * holds the layout, the verified shard indices and the decoded tiles of the
 * time chunk being read — dropped when the read moves on, which keeps
 * scrubbing inside six frames free and memory at one time chunk's tiles —
 * and assembles a frame from them, or a cell's series straight from the
 * chunks. A frame is a new plane every time: with
 * tile rectangles only their tiles are written and the cells outside are
 * zero, which a caller must not read, the coverage contract the `.xue`
 * worker's tiled decode has.
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
  readArray,
  readGroup,
  type ZarrArrayLayout,
  type ZarrArrayReader,
  type ZarrCodec,
  type ZarrGroup,
} from "./shard";
import type { ZarrStore } from "./store";

/** One inner chunk's address: a variable, a time chunk, a tile. */
export interface ChunkKey {
  variableId: number;
  timeChunk: number;
  tile: number;
}

interface VariableArray {
  id: string;
  layout: ZarrArrayLayout;
  /** The array in the WASM module: the layout's source, and the holder of
   * the shard indices fetched so far and of the decoded tiles of the time
   * chunk being read. */
  reader: ZarrArrayReader;
}

/** What `feedChunk` takes for a chunk the shard never held. A chunk that
 * was written is never empty: the index refuses a zero-length entry. */
const NEVER_HELD = new Uint8Array(0);

/** Tile rectangles as the WASM reader takes them, `[firstColumn, firstRow,
 * lastColumn, lastRow, ...]`, or null for the whole plane. */
function flatRects(rects: readonly TileRect[] | null): Uint32Array | null {
  if (!rects) return null;
  const flat = new Uint32Array(rects.length * 4);
  rects.forEach((rect, index) => flat.set([rect.firstColumn, rect.firstRow, rect.lastColumn, rect.lastRow], index * 4));
  return flat;
}

/** One least-recently-used payload budget shared by several sessions: the
 * segments of a rolling window (`SegmentedZarrSession`). Playback stays in
 * one segment for minutes, and a budget split evenly would leave it a
 * fraction of the memory the device allows, with the eviction-bound
 * re-fetching that follows; one pool lets the segment being played use the
 * whole budget and evicts whichever segment's chunks were used longest ago.
 * A session that is given no pool keeps its own budget and order. */
export class PayloadPool {
  /** Compressed chunk bytes held right now, over every member. */
  bytes = 0;
  private nextMember = 0;
  /** Every held chunk of every member, oldest use first: a `Map` iterates
   * in insertion order, so re-inserting is the whole LRU. */
  private readonly order = new Map<string, { member: PoolMember; name: string; length: number }>();

  constructor(readonly budgetBytes: number) {}

  /** A member's prefix for its chunk names in the shared order. */
  join(): string {
    this.nextMember += 1;
    return `${this.nextMember}|`;
  }

  add(member: PoolMember, name: string, length: number): void {
    this.order.set(member.poolKey + name, { member, name, length });
    this.bytes += length;
  }

  touch(member: PoolMember, name: string): void {
    const key = member.poolKey + name;
    const held = this.order.get(key);
    if (!held) return;
    this.order.delete(key);
    this.order.set(key, held);
  }

  /** Drop the least recently used chunks, whoever holds them, until the rest
   * fit the budget, never the one just inserted. */
  evict(keep: PoolMember, keepName: string): void {
    if (this.bytes <= this.budgetBytes) return;
    const kept = keep.poolKey + keepName;
    for (const [key, held] of this.order) {
      if (this.bytes <= this.budgetBytes) break;
      if (key === kept) continue;
      this.order.delete(key);
      this.bytes -= held.length;
      held.member.dropPayload(held.name);
    }
  }
}

/** What a pool needs of a session that holds chunks in it. */
interface PoolMember {
  readonly poolKey: string;
  dropPayload(name: string): void;
}

/** What the worker drives: one store (`ZarrSession`) or a rolling window's
 * segments (`SegmentedZarrSession`), answering the same questions in the
 * bundle's own frame offsets. A key is only ever handed back to the reader
 * that produced it. */
export interface ZarrReader {
  readonly metadataJson: string;
  readonly planeLength: number;
  readonly numericIds: number[];
  readonly frameOffsets: number[];
  /** Bytes received over the network, coalesced over-reads included. */
  readonly fetchedBytes: number;
  tileGeometry(): Uint32Array;
  clearCache(): void;
  chunksFor(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): ChunkKey[];
  isResident(key: ChunkKey): boolean;
  isFetching(key: ChunkKey): boolean;
  ensureChunks(keys: readonly ChunkKey[]): Promise<void>;
  frameResident(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): boolean;
  /** One frame as a plane of its own. With `rects`, only their tiles are
   * valid. */
  decodeFrame(variableId: number, frameOffset: number, rects?: readonly TileRect[] | null): Promise<Uint8Array>;
  decodeSeries(variableId: number, column: number, row: number): Promise<Uint8Array>;
}

export interface ZarrSessionOptions {
  /** The bundle to read out of the store: the manifest entry's `variable`.
   * Required on a shared store, which holds every bundle of its tier. */
  bundle?: string;
  payloadBudgetBytes?: number;
  payloadPool?: PayloadPool;
}

function chunkKey(key: ChunkKey): string {
  return `${key.variableId}:${key.timeChunk}:${key.tile}`;
}

function indexKey(variableId: number, shard: number): string {
  return `${variableId}:${shard}`;
}

export class ZarrSession implements ZarrReader, PoolMember {
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
  /** The shared budget this session's chunks count against, in place of
   * its own `payloadBudgetBytes`, and its prefix in the pool's order. */
  private pool: PayloadPool | null = null;
  poolKey = "";
  /** Bytes of `store.stats` that `fetchedBytes` does not report. */
  private uncountedBytes = 0;

  private readonly arrays = new Map<number, VariableArray>();
  private readonly indexFetches = new Map<string, Promise<void>>();
  /** Compressed inner chunks, `null` for one the shard never held. */
  private readonly payloads = new Map<string, Uint8Array | null>();
  private readonly chunkFetches = new Map<string, Promise<void>>();
  private readonly offsetIndex: Map<number, number>;

  private constructor(
    readonly store: ZarrStore,
    readonly group: ZarrGroup,
    arrays: { numericId: number; id: string; layout: ZarrArrayLayout; reader: ZarrArrayReader }[],
  ) {
    this.offsetIndex = new Map(frameOffsets(group.metadata.time).map((offset, index) => [offset, index]));
    for (const array of arrays) {
      this.arrays.set(array.numericId, {
        id: array.id,
        layout: array.layout,
        reader: array.reader,
      });
    }
  }

  /** Read the group as `options.bundle`'s and every one of that bundle's
   * variable arrays. Every array must describe the grid and axis the
   * bundle's metadata declares — not the store's other bundles', which may
   * have their own — and be cut the same way: one tiling per bundle, as the
   * container has. `bundle` is required on a shared store and checked
   * against what an array declares wherever both are known. */
  static async open(
    store: ZarrStore,
    codec: ZarrCodec,
    options: ZarrSessionOptions = {},
  ): Promise<ZarrSession> {
    const { bundle } = options;
    const groupBytes = await store.get("zarr.json");
    const group = readGroup(codec, new TextDecoder().decode(groupBytes), bundle);
    const { metadata } = group;
    const arrays = await Promise.all(
      metadata.variables.map(async (variable) => {
        const { reader, layout } = readArray(codec, new TextDecoder().decode(await store.get(`${variable.id}/zarr.json`)));
        if (bundle !== undefined && layout.bundle !== null && layout.bundle !== bundle) {
          throw new Error(`array ${variable.id} belongs to bundle ${layout.bundle}, not ${bundle}`);
        }
        if (layout.height !== metadata.grid.height || layout.width !== metadata.grid.width) {
          throw new Error(`array ${variable.id} does not match the bundle's grid`);
        }
        if (layout.frameCount !== metadata.time.frameCount) {
          throw new Error(`array ${variable.id} does not match the bundle's time axis`);
        }
        return { numericId: variable.numericId, id: variable.id, layout, reader };
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
        throw new Error("every array of a bundle must be cut the same way");
      }
    }
    const session = new ZarrSession(store, group, arrays);
    // A shared store's descriptor sizes this bundle's arrays and coordinates
    // but not the group, which carries every bundle of the tier; progress
    // is reported against that size, so the group is left out of it too.
    if (group.profile !== 1) session.uncountedBytes = groupBytes.byteLength;
    if (options.payloadBudgetBytes !== undefined && options.payloadBudgetBytes > 0) {
      session.payloadBudgetBytes = options.payloadBudgetBytes;
    }
    if (options.payloadPool) {
      session.pool = options.payloadPool;
      session.poolKey = options.payloadPool.join();
    }
    return session;
  }

  get metadataJson(): string {
    return this.group.metadataJson;
  }

  get fetchedBytes(): number {
    return this.store.stats.bytes - this.uncountedBytes;
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
    for (const array of this.arrays.values()) array.reader.clearCache();
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

  /** The chunks a frame needs within the given tiles, as chunk keys. */
  chunksFor(variableId: number, frameOffset: number, rects: readonly TileRect[] | null): ChunkKey[] {
    const { layout, reader } = this.array(variableId);
    const timeChunk = Math.floor(this.frameIndex(frameOffset) / layout.timeChunk);
    return Array.from(reader.tilesFor(flatRects(rects)), (tile) => ({ variableId, timeChunk, tile }));
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

  /** Make one shard's index local: read once, verified and held by the
   * array's reader. */
  private ensureIndex(variableId: number, shard: number): Promise<void> {
    const { id, layout, reader } = this.array(variableId);
    if (reader.hasShardIndex(shard)) return Promise.resolve();
    const key = indexKey(variableId, shard);
    const running = this.indexFetches.get(key);
    if (running) return running;
    if (shard < 0 || shard >= layout.shardCount) throw new Error("shard is outside the array");
    const length = reader.shardIndexLength;
    const path = reader.shardPath(id, shard);
    // The index sits at whichever end the array declares; at the end it is
    // a suffix range, which needs no object length, and at the start a
    // prefix of known length.
    const task = (async () => {
      try {
        const bytes = await this.store.getRange(
          path,
          layout.indexLocation === "end" ? { suffixLength: length } : { offset: 0, length },
        );
        reader.setShardIndex(shard, bytes);
        this.residentBytes += bytes.byteLength;
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
    const { id, layout, reader } = this.array(key.variableId);
    if (key.timeChunk < 0 || key.timeChunk >= layout.timeChunks) throw new Error("time chunk is outside the array");
    const task = (async () => {
      try {
        const shard = reader.shardOf(key.timeChunk);
        await this.ensureIndex(key.variableId, shard);
        const range = reader.chunkRange(key.timeChunk, key.tile);
        if (range === undefined) {
          this.payloads.set(name, null);
          return;
        }
        const bytes = await this.store.getRange(reader.shardPath(id, shard), { offset: range[0]!, length: range[1]! });
        this.payloads.set(name, bytes);
        this.residentBytes += bytes.byteLength;
        this.payloadBytes += bytes.byteLength;
        if (this.pool) {
          this.pool.add(this, name, bytes.byteLength);
          this.pool.evict(this, name);
        } else {
          this.evictPayloads(name);
        }
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
  /** Let go of one held chunk, at the pool's request. */
  dropPayload(name: string): void {
    const bytes = this.payloads.get(name);
    if (!bytes) return;
    this.payloads.delete(name);
    this.payloadBytes -= bytes.byteLength;
    this.residentBytes -= bytes.byteLength;
  }

  private touchPayload(name: string): void {
    this.pool?.touch(this, name);
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

  /** Assemble one frame and return it. With `rects` only those tiles are
   * fetched and written; the cells outside them are zero and mean nothing.
   *
   * Decodes run concurrently, and those whose chunks are already local all
   * resume in one burst of microtasks. Each gets a plane of its own: the
   * reader is handed the chunks and assembles the frame with no `await` in
   * between, and what it returns is a new array. */
  async decodeFrame(variableId: number, frameOffset: number, rects: readonly TileRect[] | null = null): Promise<Uint8Array> {
    const { layout, reader } = this.array(variableId);
    const index = this.frameIndex(frameOffset);
    const keys = this.chunksFor(variableId, frameOffset, rects);
    // A concurrent insert may have evicted one of these between its fetch
    // and now, when the budget is tighter than what is being asked for at
    // once; those are fetched again, a bounded number of times.
    for (let attempt = 0; ; attempt += 1) {
      await this.ensureChunks(keys);
      const missing = keys.filter((key) => !this.payloads.has(chunkKey(key)));
      if (missing.length === 0) break;
      if (attempt >= 3) throw new Error("chunks evicted faster than a frame assembles; raise the payload budget");
    }
    // Synchronous from here: another decode of this variable, for another
    // time chunk, would otherwise drop the tiles being fed.
    const timeChunk = Math.floor(index / layout.timeChunk);
    const tiles = Uint32Array.from(keys, (key) => key.tile);
    for (const tile of reader.undecodedTiles(timeChunk, tiles)) {
      const payload = this.payloads.get(chunkKey({ variableId, timeChunk, tile }));
      if (payload === undefined) throw new Error("chunk is not resident yet");
      reader.feedChunk(timeChunk, tile, payload ?? NEVER_HELD);
    }
    return reader.assembleFrame(index, flatRects(rects));
  }

  /** The chunks one cell's series needs: one per time chunk of its tile. */
  seriesChunks(variableId: number, column: number, row: number): ChunkKey[] {
    const { layout, reader } = this.array(variableId);
    if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || column < 0) throw new Error("cell is outside the grid");
    const tile = reader.tileOf(row, column);
    return Array.from({ length: layout.timeChunks }, (_, timeChunk) => ({ variableId, timeChunk, tile }));
  }

  /** One held chunk's bytes, `null` for one the shard never held. A series
   * touches every time chunk of one tile, more than a tight budget may hold
   * at once, so a chunk evicted since it was fetched is fetched again here;
   * the caller keeps the bytes it is given, whatever the cache then does. */
  private async heldPayload(key: ChunkKey): Promise<Uint8Array | null> {
    const name = chunkKey(key);
    for (let attempt = 0; ; attempt += 1) {
      const payload = this.payloads.get(name);
      if (payload !== undefined) return payload;
      if (attempt >= 3) throw new Error("chunks evicted faster than a series reads them; raise the payload budget");
      await this.ensureChunk(key);
    }
  }

  /** One cell's code on every frame of the axis. The chunks are handed to
   * the reader in one call, joined end to end with the boundaries between
   * them, and decoded there one after another: each is touched once, so
   * none is kept. */
  async decodeSeries(variableId: number, column: number, row: number): Promise<Uint8Array> {
    const { reader } = this.array(variableId);
    const keys = this.seriesChunks(variableId, column, row);
    await this.ensureChunks(keys);
    const payloads: (Uint8Array | null)[] = [];
    for (const key of keys) payloads.push(await this.heldPayload(key));
    const offsets = new Uint32Array(payloads.length + 1);
    payloads.forEach((payload, index) => {
      offsets[index + 1] = offsets[index]! + (payload?.byteLength ?? 0);
    });
    const joined = new Uint8Array(offsets[payloads.length]!);
    payloads.forEach((payload, index) => {
      if (payload) joined.set(payload, offsets[index]!);
    });
    return reader.series(row, column, joined, offsets);
  }
}
