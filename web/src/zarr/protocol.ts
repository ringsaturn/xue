/**
 * The Zarr channel's side of the decode-worker protocol.
 *
 * `main.ts` never holds a file, only a `DecodeChannel` that speaks
 * `booted` → `init-stream` → `ready`, then `decode` → `frame`, `series` →
 * `series`, `prefetch-window`, `recycle` and `clear-cache` (worker.ts is the
 * `.xue` implementation). The Zarr worker is the other one and answers the
 * same messages with the same replies, but for `recycle`, which it is never
 * sent: its planes are new arrays, not buffers out of a pool. What differs
 * otherwise is only how it is opened, which is the one message declared
 * here. The request shapes below mirror worker.ts's and exist so the
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
  /** The bundle to read: the manifest entry's `variable`. A shared store
   * (profile 2) holds every bundle of its tier and is opened as this one's;
   * a one-bundle store is that bundle's anyway. */
  bundle: string;
  /** The manifest descriptor's `byteLength` — what this bundle reads from
   * the store — which `progress` reports against. */
  byteLength: number;
  /** A rolling window's segments (docs/zarr-profile.md, "Segmented
   * store"), in time order, each opened as its own store under its own
   * `?v=` and joined into one axis; `url` and `crc32` then name the first.
   * Absent for a plain store. */
  segments?: { url: string; crc32: string }[];
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

interface ZarrPrefetchWindowMessage {
  type: "prefetch-window";
  hours: number[];
  concurrency: number;
  tiles?: TileRect[];
}

interface ZarrClearCacheMessage {
  type: "clear-cache";
}

/** The `.xue` worker's whole-buffer init, which this channel refuses: a
 * store is many objects and is only ever streamed. Named so the worker can
 * answer it with a clear error instead of an unknown-message silence. */
interface ZarrRefusedInitMessage {
  type: "init";
}

export type ZarrWorkerRequest =
  | ZarrInitStreamMessage
  | ZarrRefusedInitMessage
  | ZarrDecodeMessage
  | ZarrSeriesMessage
  | ZarrPrefetchWindowMessage
  | ZarrClearCacheMessage;

/** Open a column store (`columns.ts`); answered with `ready`, which carries
 * the group's `xue_columns` block as JSON (`blockJson`) and the block its
 * first array is cut in (`blockShape`, `[height, width]` cells). */
export interface ColumnInitMessage {
  type: "init-columns";
  /** The store's root, and the CRC every object of it is fetched under. */
  url: string;
  crc32: string;
  /** The compressed chunk bytes kept for reading again; absent is a pin's. */
  heldBytes?: number;
}

/** Read one array's column at one cell; answered with `column`, whose
 * buffer holds the code of every level of every frame, frame by frame. */
export interface ColumnReadMessage {
  type: "column";
  requestId: number;
  array: string;
  row: number;
  column: number;
}

/** Read one array at many cells, `[row, column, row, column, ...]`; answered
 * with `cells` messages, a block's worth of them each (`ColumnCellsReply`). */
export interface ColumnCellsMessage {
  type: "cells";
  requestId: number;
  array: string;
  cells: Uint32Array;
}

/** Some of the cells a `ColumnCellsMessage` asked for. */
export interface ColumnCellsReply {
  type: "cells";
  requestId: number;
  /** Which of the request's cells these are. */
  indices: Uint32Array;
  /** Their codes, cell by cell, each the code of every level of every frame,
   * frame by frame. */
  buffer: ArrayBuffer;
  /** Every cell asked for has been answered. */
  done: boolean;
}

/** The pin has moved, or the line: what has not been asked of the network yet
 * is let go, unanswered. */
interface ColumnSupersedeMessage {
  type: "supersede";
}

/** What a column channel is sent: not the decode protocol, which a column
 * store has no frames to answer. */
export type ColumnWorkerRequest = ColumnInitMessage | ColumnReadMessage | ColumnCellsMessage | ColumnSupersedeMessage;

/** The two kinds of channel a hub hosts. */
export type ZarrHubEndpoint = "bundle" | "columns";

/** What the main thread sends a hub worker (`hub.worker.ts`), which hosts
 * many channels: a channel is opened, spoken to in the protocol above, and
 * closed, each under the number it was opened with. A channel is a bundle's
 * unless it says otherwise. */
export type ZarrHubRequest =
  | { kind: "open"; channel: number; endpoint?: ZarrHubEndpoint }
  | { kind: "message"; channel: number; message: ZarrWorkerRequest | ColumnWorkerRequest }
  | { kind: "close"; channel: number };

/** What a hub worker sends back: a channel's own reply, in an envelope
 * that names the channel. */
export interface ZarrHubReply {
  channel: number;
  message: unknown;
}
