/**
 * One open column store (docs/zarr-profile.md, "Column store"): a cell's
 * whole axis, every level at once, in one chunk per array.
 *
 * Only a pinned point reads one, on a hub worker (`columnendpoint.ts`); no
 * worker is in here, so the unit tests drive it directly. The group document
 * is the only metadata read, since it consolidates every array's. A read is
 * then the cell's shard index, held for good once fetched, and the one chunk
 * the cell's block is in. Chunks are held compressed, a few pins' worth: the
 * bilinear reads of one pin are mostly one block, and a pin moved by a cell
 * or two often stays in it.
 *
 * The WASM module reads the format; this is the side that waits.
 */

import { checkColumnGroup } from "../schema/generated/column-group";
import type { WasmColumnStore } from "../wasm/xue";
import type { ZarrStore } from "./store";

/** What a session needs of the WASM module, injected as `ZarrCodec` is. */
export interface ColumnCodec {
  WasmColumnStore: Pick<typeof WasmColumnStore, "fromGroup">;
}

/** The compressed chunks held: a chunk is some hundred kilobytes on a
 * levelled array, so this is a pin's every array several times over. */
const HELD_BYTES = 16 * 1024 * 1024;

/** A chunk the shard never held: the reader answers it with the fill. */
const NEVER_HELD = new Uint8Array(0);

export class ColumnSession {
  private readonly indexFetches = new Map<string, Promise<void>>();
  private readonly chunkFetches = new Map<string, Promise<Uint8Array>>();
  /** Compressed chunks by shard and offset, oldest use first. */
  private readonly held = new Map<string, Uint8Array>();
  private heldBytes = 0;
  /** Moves with the pin; a read carries the one it began under. */
  private epoch = 0;
  private reading = 0;
  private closed = false;

  private constructor(
    private readonly store: ZarrStore,
    private readonly reader: WasmColumnStore,
    private readonly heldLimit: number,
  ) {}

  static async open(store: ZarrStore, codec: ColumnCodec, heldLimit = HELD_BYTES): Promise<ColumnSession> {
    const text = new TextDecoder().decode(await store.get("zarr.json"));
    checkColumnGroup(JSON.parse(text), "column store group");
    return new ColumnSession(store, codec.WasmColumnStore.fromGroup(text), heldLimit);
  }

  /** The group's `xue_columns` block, as JSON. */
  get blockJson(): string {
    return this.reader.blockJson;
  }

  /**
   * One array's column at a cell: the code of every level of every frame,
   * frame by frame. Null for a read let go because the pin moved on
   * (`supersede`) or the store was closed under it.
   */
  async read(array: string, row: number, column: number): Promise<Uint8Array | null> {
    if (this.closed) return null;
    const epoch = this.epoch;
    this.reading += 1;
    try {
      const shard = this.reader.shardOf(array, row, column);
      const path = this.reader.shardPath(array, shard);
      await this.ensureIndex(array, shard, path);
      // The index is kept whatever the pin did meanwhile; the chunk is only
      // this pin's.
      if (this.closed || epoch !== this.epoch) return null;
      const range = this.reader.chunkRange(array, row, column);
      const payload = range === undefined ? NEVER_HELD : await this.chunk(path, range[0]!, range[1]!);
      if (this.closed) return null;
      return this.reader.column(array, row, column, payload);
    } finally {
      this.reading -= 1;
      this.releaseWhenIdle();
    }
  }

  /**
   * Let go of the reads under way for the previous pin. Nothing on the wire
   * is cancelled: a cancelled response is a reset HTTP/2 stream, which a run
   * of can cost the page its connection (`sessions.ts`), and what is in
   * flight is small and held when it lands. A read that has not asked for
   * its chunk yet does not ask.
   */
  supersede(): void {
    this.epoch += 1;
  }

  /** Give back the reader's WASM memory, once no read is going through it. */
  dispose(): void {
    if (this.closed) return;
    this.closed = true;
    this.held.clear();
    this.heldBytes = 0;
    this.releaseWhenIdle();
  }

  private releaseWhenIdle(): void {
    if (this.closed && this.reading === 0) this.reader.free();
  }

  private ensureIndex(array: string, shard: number, path: string): Promise<void> {
    if (this.reader.hasShardIndex(array, shard)) return Promise.resolve();
    const running = this.indexFetches.get(path);
    if (running) return running;
    const task = (async () => {
      try {
        const length = this.reader.shardIndexLength(array);
        // At the start, where the writer puts it, a plain range: a suffix
        // range is not safelisted and would cost a preflight per object.
        const bytes = await this.store.getRange(
          path,
          this.reader.indexAtEnd(array) ? { suffixLength: length } : { offset: 0, length },
        );
        if (!this.closed) this.reader.setShardIndex(array, shard, bytes);
      } finally {
        this.indexFetches.delete(path);
      }
    })();
    this.indexFetches.set(path, task);
    return task;
  }

  private chunk(path: string, offset: number, length: number): Promise<Uint8Array> {
    const key = `${path}@${offset}`;
    const held = this.held.get(key);
    if (held) {
      this.held.delete(key);
      this.held.set(key, held);
      return Promise.resolve(held);
    }
    const running = this.chunkFetches.get(key);
    if (running) return running;
    const task = (async () => {
      try {
        const read = await this.store.getRange(path, { offset, length });
        // A view of a merged response would keep the whole response.
        const bytes = read.byteLength === read.buffer.byteLength ? read : read.slice();
        if (!this.closed) this.hold(key, bytes);
        return bytes;
      } finally {
        this.chunkFetches.delete(key);
      }
    })();
    this.chunkFetches.set(key, task);
    return task;
  }

  private hold(key: string, bytes: Uint8Array): void {
    this.held.set(key, bytes);
    this.heldBytes += bytes.byteLength;
    for (const [oldest, dropped] of this.held) {
      if (this.heldBytes <= this.heldLimit || oldest === key) break;
      this.held.delete(oldest);
      this.heldBytes -= dropped.byteLength;
    }
  }
}
