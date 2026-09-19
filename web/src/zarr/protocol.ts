/**
 * The Zarr channel's side of the decode-worker protocol.
 *
 * `main.ts` never holds a file, only a `DecodeChannel` that speaks
 * `booted` → `init-stream` → `ready`, then `decode` → `frame`, `series` →
 * `series`, `prefetch-window`, `recycle` and `clear-cache` (worker.ts is the
 * `.xue` implementation, webcodecs.ts the H.264 one). The Zarr worker is the
 * third implementation and answers the same messages with the same replies;
 * what differs is only how it is opened, which is the one message declared
 * here. The request shapes below mirror worker.ts's exactly and exist so the
 * two workers and their tests share one spelling without importing a worker
 * entry module (whose top level posts `booted` on load).
 */

import type { TileRect } from "../tiles";

/** Open a store. `url` is the store's root — the directory holding the
 * group's `zarr.json` — resolved against the manifest like a bundle path,
 * without a query; `crc32` is the manifest descriptor's, appended as `?v=`
 * to every object the worker fetches, since every object under a run is
 * immutable and this is the one value that names the store's version. */
export interface ZarrInitStreamMessage {
  type: "init-stream";
  kind: "zarr";
  url: string;
  crc32: string;
  /** The sum of every object in the store, which `progress` reports against. */
  byteLength: number;
  /** Echoed back in progress/resident messages so the main thread can
   * attribute them to the right variable session. */
  variableKey: string;
  /** False when the main thread's probe found the origin serves no ranges:
   * the store then reads whole objects — one GET per shard, which is one
   * temporal group of one variable — instead of ranges. True by default. */
  ranges?: boolean;
  /** The most compressed chunk bytes the worker keeps
   * (`ZarrSession.payloadBudgetBytes`): a least-recently-used cache bounded
   * by the device rather than by the axis. Absent keeps every chunk. */
  payloadBudgetBytes?: number;
}

export interface ZarrDecodeMessage {
  type: "decode";
  requestId: number;
  generation: number;
  variableId: number;
  frameOffset: number;
  /** Tiles to decode, or absent for the whole plane; the reply echoes them
   * back as the plane's coverage. */
  tiles?: TileRect[];
}

export interface ZarrSeriesMessage {
  type: "series";
  requestId: number;
  generation: number;
  variableId: number;
  column: number;
  row: number;
}

export interface ZarrPrefetchWindowMessage {
  type: "prefetch-window";
  hours: number[];
  concurrency: number;
  tiles?: TileRect[];
}

export interface ZarrRecycleMessage {
  type: "recycle";
  buffer: ArrayBuffer;
}

export interface ZarrClearCacheMessage {
  type: "clear-cache";
}

/** The `.xue` worker's whole-buffer init, which this channel refuses: a
 * store is many objects and is only ever streamed. Named so the worker can
 * answer it with a clear error instead of an unknown-message silence. */
export interface ZarrRefusedInitMessage {
  type: "init";
}

export type ZarrWorkerRequest =
  | ZarrInitStreamMessage
  | ZarrRefusedInitMessage
  | ZarrDecodeMessage
  | ZarrSeriesMessage
  | ZarrPrefetchWindowMessage
  | ZarrRecycleMessage
  | ZarrClearCacheMessage;
