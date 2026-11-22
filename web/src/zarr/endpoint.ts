/**
 * One Zarr decode channel's state and message loop, apart from the worker
 * that hosts it.
 *
 * A layer's session has a worker to itself (`worker.ts`): it decodes whole
 * planes several times a second and wants a thread for it. A pinned point's
 * sessions are another matter — a chart with a model column opens dozens of
 * them, each to read one cell's series and then sit idle — and a worker
 * apiece is dozens of threads and dozens of WASM instances for work that
 * fits in a few. So the loop lives here, with nothing of the worker's in
 * it but the function it posts through, and `hub.worker.ts` runs many of
 * them side by side.
 *
 * What the loop does is described where it always was, in `worker.ts`.
 */

import { sameTileRects, type TileRect } from "../tiles";
import wasmInit, { parseZarrGroup, WasmZarrArray } from "../wasm/xue";
import wasmUrl from "../wasm/xue_bg.wasm?url";
import type { ZarrDecodeMessage, ZarrInitStreamMessage, ZarrSeriesMessage, ZarrWorkerRequest } from "./protocol";
import { SegmentedZarrSession } from "./segmented";
import { ZarrSession, type ChunkKey, type ZarrReader } from "./session";
import type { ZarrCodec } from "./documents";
import { ZarrStore } from "./store";

const PREFETCH_RETRIES = 3;
const CODEC: ZarrCodec = { parseZarrGroup, WasmZarrArray };

/** The WASM module, instantiated once per worker however many endpoints
 * the worker hosts: a second instantiation started before the first has
 * finished would replace the instance under the readers the first one's
 * endpoint is already holding. */
let wasmReady: Promise<unknown> | null = null;

function loadWasm(): Promise<unknown> {
  wasmReady ??= wasmInit(wasmUrl).catch((error: unknown) => {
    // A load that failed is not the module: the next channel tries again,
    // rather than every channel of the worker failing with this one.
    wasmReady = null;
    throw error;
  });
  return wasmReady;
}

/** How an endpoint answers: the hosting worker's `postMessage`, or the
 * hub's, which tags the message with the channel it belongs to. */
export type ZarrPost = (message: unknown, transfer?: Transferable[]) => void;

export interface ZarrEndpoint {
  /** Answer one request of the decode protocol. Never rejects: a failure
   * is posted as an `error` message, as the protocol has it. */
  handle(message: ZarrWorkerRequest): Promise<void>;
  /** The channel is closed: stop the prefetch pump, take no more requests,
   * and free the reader's WASM memory once what is in flight has come back
   * — the worker lives on, so nothing else would. */
  close(): void;
}

export function createZarrEndpoint(post: ZarrPost): ZarrEndpoint {
  let session: ZarrReader | null = null;
  let variableKey = "";
  let totalBytes = 0;
  let decodeFetchCount = 0;
  /** Fetch tasks the prefetch pump has in flight, one per (variable, time
   * chunk) it filled — the unit the concurrency cap counts, as the `.xue`
   * worker counts one span per group. */
  let prefetchTasks = 0;

  let windowOffsets: number[] = [];
  let windowConcurrency = 0;
  let windowTiles: TileRect[] | null = null;
  let residentAnnounced = false;
  let prefetchFailures = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  /** Free a closed channel's reader, once no read is still going through
   * it: a fetch that returns to a freed reader would have nothing to hand
   * its bytes to. */
  function releaseWhenIdle(): void {
    if (!closed || !session || decodeFetchCount > 0 || prefetchTasks > 0) return;
    session.dispose();
    session = null;
  }

  function postProgress(): void {
    if (!session) return;
    post({ type: "progress", variableKey, bytes: session.fetchedBytes, totalBytes });
  }

  /** The chunks a frame still needs within the window's tiles, neither local
   * nor being fetched. */
  function missingChunks(active: ZarrReader, variableId: number, frameOffset: number): ChunkKey[] {
    return active.chunksFor(variableId, frameOffset, windowTiles).filter((key) => !active.isResident(key) && !active.isFetching(key));
  }

  /** Fill the prefetch window (see the module notes); decode fetches always
   * win, and a fetch that fails backs off up to three times before waiting
   * for the next window update. */
  function pumpPrefetch(): void {
    const active = session;
    if (!active || closed || windowConcurrency <= 0) return;
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
            releaseWhenIdle();
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
    await loadWasm();
    variableKey = message.variableKey;
    totalBytes = message.byteLength;
    const started = performance.now();
    const ranges = message.ranges ?? true;
    const options = { bundle: message.bundle, payloadBudgetBytes: message.payloadBudgetBytes };
    const opened: ZarrReader = message.segments
      ? await SegmentedZarrSession.open(
          message.segments.map((segment) => new ZarrStore(segment.url, segment.crc32, { ranges })),
          CODEC,
          options,
        )
      : await ZarrSession.open(new ZarrStore(message.url, message.crc32, { ranges }), CODEC, options);
    if (closed) {
      // Closed while the store was opening: nobody is left to read it.
      opened.dispose();
      return;
    }
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
      releaseWhenIdle();
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
      releaseWhenIdle();
    }
  }

  async function handle(message: ZarrWorkerRequest): Promise<void> {
    if (closed) return;
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
        // The plane is the decode's own array, whole: its buffer is posted.
        const buffer = (await decodeFrame(message)).buffer as ArrayBuffer;
        post(
          {
            type: "frame",
            requestId: message.requestId,
            generation: message.generation,
            variableId: message.variableId,
            frameOffset: message.frameOffset,
            decodeMs: performance.now() - started,
            // The plane outside these rectangles is not data; absent means
            // the whole plane is valid.
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
  }

  return {
    handle,
    close() {
      closed = true;
      windowOffsets = [];
      windowConcurrency = 0;
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      releaseWhenIdle();
    },
  };
}
