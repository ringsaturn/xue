/// <reference lib="webworker" />
/**
 * The Zarr decode worker: the third implementation of the decode protocol.
 *
 * It answers exactly what worker.ts answers — `booted` → `init-stream` →
 * `ready`, `decode` → `frame` with the same coverage semantics, `series`,
 * `prefetch-window`, `recycle`, `clear-cache`, `progress` and `resident` —
 * over a Zarr v3 store (docs/zarr-profile.md) instead of a `.xue` file, so
 * `main.ts` holds it in the same field without branching. The store,
 * the shard indices and the assembly live in `session.ts`; this file is the
 * message loop and the windowed prefetch pump, which mirrors the `.xue`
 * worker's: the main thread names the frame offsets ahead of the playhead
 * plus a concurrency cap, the worker keeps at most that many fetches in
 * flight inside the window, decode-triggered fetches always go first, and
 * `resident` fires once every frame of the axis is local within the tiles
 * the window names.
 *
 * Decoding is the container's own chunk path, `decodeChunk` from the WASM
 * module: the only thing this channel changes is where the bytes come from
 * and how they are indexed, which is what the comparison with the `.xue`
 * channel is meant to isolate.
 */

import { sameTileRects, type TileRect } from "../tiles";
import wasmInit, { decodeChunk } from "../wasm/xue";
import wasmUrl from "../wasm/xue_bg.wasm?url";
import type { ZarrDecodeMessage, ZarrInitStreamMessage, ZarrSeriesMessage, ZarrWorkerRequest } from "./protocol";
import { ZarrSession, type ChunkKey } from "./session";
import { ZarrStore } from "./store";

const PREFETCH_RETRIES = 3;

let session: ZarrSession | null = null;
let variableKey = "";
let totalBytes = 0;
let decodeFetchCount = 0;
/** Fetch tasks the prefetch pump has in flight, one per (variable, time
 * chunk) it filled — the unit the concurrency cap counts, as the `.xue`
 * worker counts one span per group. */
let prefetchTasks = 0;
const recycled: ArrayBuffer[] = [];

let windowOffsets: number[] = [];
let windowConcurrency = 0;
let windowTiles: TileRect[] | null = null;
let residentAnnounced = false;
let prefetchFailures = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function post(message: unknown, transfer: Transferable[] = []): void {
  (self as unknown as DedicatedWorkerGlobalScope).postMessage(message, transfer);
}

function postProgress(): void {
  if (!session) return;
  post({ type: "progress", variableKey, bytes: session.store.stats.bytes, totalBytes });
}

/** The chunks a frame still needs within the window's tiles, neither local
 * nor being fetched. */
function missingChunks(active: ZarrSession, variableId: number, frameOffset: number): ChunkKey[] {
  return active.chunksFor(variableId, frameOffset, windowTiles).filter((key) => !active.isResident(key) && !active.isFetching(key));
}

/** Fill the prefetch window (see the module notes); decode fetches always
 * win, and a fetch that fails backs off up to three times before waiting
 * for the next window update. */
function pumpPrefetch(): void {
  const active = session;
  if (!active || windowConcurrency <= 0) return;
  if (decodeFetchCount > 0) return;
  if (prefetchFailures >= PREFETCH_RETRIES) return;
  let missing = false;
  for (const offset of windowOffsets) {
    for (const variableId of active.numericIds) {
      const keys = missingChunks(active, variableId, offset);
      if (keys.length === 0) continue;
      if (prefetchTasks >= windowConcurrency) return;
      missing = true;
      prefetchTasks += 1;
      void active
        .ensureChunks(keys)
        .then(() => {
          prefetchFailures = 0;
          postProgress();
        })
        .catch(() => {
          prefetchFailures += 1;
          if (prefetchFailures < PREFETCH_RETRIES && !retryTimer) {
            retryTimer = setTimeout(() => {
              retryTimer = null;
              pumpPrefetch();
            }, 1000 * prefetchFailures);
          }
        })
        .finally(() => {
          prefetchTasks -= 1;
          // A freed slot may unblock the window.
          pumpPrefetch();
        });
    }
  }
  if (!missing && prefetchTasks === 0 && !residentAnnounced) {
    const complete = active.frameOffsets.every((offset) =>
      active.numericIds.every((variableId) => active.frameResident(variableId, offset, windowTiles)),
    );
    if (complete) {
      residentAnnounced = true;
      post({ type: "resident", variableKey, scope: windowTiles ? "viewport" : "bundle" });
    }
  }
}

async function initStream(message: ZarrInitStreamMessage): Promise<void> {
  await wasmInit(wasmUrl);
  variableKey = message.variableKey;
  totalBytes = message.byteLength;
  const started = performance.now();
  const opened = await ZarrSession.open(
    new ZarrStore(message.url, message.crc32, { ranges: message.ranges ?? true }),
    decodeChunk,
    { payloadBudgetBytes: message.payloadBudgetBytes },
  );
  session = opened;
  post({
    type: "ready",
    metadataJson: opened.metadataJson,
    planeLength: opened.planeLength,
    tileGeometry: opened.tileGeometry(),
    openMs: performance.now() - started,
  });
  postProgress();
  residentAnnounced = false;
  // A prefetch-window message may have arrived before init finished.
  pumpPrefetch();
}

async function decodeFrame(message: ZarrDecodeMessage): Promise<Uint8Array> {
  const active = session;
  if (!active) throw new Error("store is not initialized");
  decodeFetchCount += 1;
  try {
    return await active.decodeFrame(message.variableId, message.frameOffset, message.tiles ?? null);
  } finally {
    decodeFetchCount -= 1;
    postProgress();
    pumpPrefetch();
  }
}

async function decodeSeries(message: ZarrSeriesMessage): Promise<Uint8Array> {
  const active = session;
  if (!active) throw new Error("store is not initialized");
  decodeFetchCount += 1;
  try {
    return await active.decodeSeries(message.variableId, message.column, message.row);
  } finally {
    decodeFetchCount -= 1;
    postProgress();
    pumpPrefetch();
  }
}

self.onmessage = async (event: MessageEvent<ZarrWorkerRequest>) => {
  const message = event.data;
  try {
    if (message.type === "init") {
      throw new Error("a Zarr store is streamed, never handed over as one buffer");
    }
    if (message.type === "init-stream") {
      await initStream(message);
      return;
    }
    if (message.type === "decode") {
      const started = performance.now();
      const plane = await decodeFrame(message);
      let buffer = recycled.pop();
      if (!buffer || buffer.byteLength !== plane.byteLength) buffer = new ArrayBuffer(plane.byteLength);
      new Uint8Array(buffer).set(plane);
      post(
        {
          type: "frame",
          requestId: message.requestId,
          generation: message.generation,
          variableId: message.variableId,
          frameOffset: message.frameOffset,
          decodeMs: performance.now() - started,
          // The plane outside these rectangles is whatever the buffer held;
          // absent means the whole plane is valid.
          tiles: message.tiles,
          buffer,
        },
        [buffer],
      );
      return;
    }
    if (message.type === "series") {
      const codes = await decodeSeries(message);
      const buffer = new ArrayBuffer(codes.byteLength);
      new Uint8Array(buffer).set(codes);
      post(
        {
          type: "series",
          requestId: message.requestId,
          generation: message.generation,
          variableId: message.variableId,
          column: message.column,
          row: message.row,
          buffer,
        },
        [buffer],
      );
      return;
    }
    if (message.type === "recycle") {
      if (recycled.length < 4) recycled.push(message.buffer);
      return;
    }
    if (message.type === "prefetch-window") {
      windowOffsets = message.hours;
      windowConcurrency = message.concurrency;
      if (!sameTileRects(message.tiles ?? null, windowTiles)) {
        windowTiles = message.tiles ?? null;
        // A wider view has more to fetch, so residency has to be earned again.
        residentAnnounced = false;
      }
      prefetchFailures = 0;
      pumpPrefetch();
      return;
    }
    if (message.type === "clear-cache") {
      session?.clearCache();
    }
  } catch (error) {
    post({
      type: "error",
      requestId: message.type === "decode" || message.type === "series" ? message.requestId : undefined,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};

post({ type: "booted" });
