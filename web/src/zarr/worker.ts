/// <reference lib="webworker" />
/**
 * The Zarr decode worker: the third implementation of the decode protocol.
 *
 * It answers what worker.ts answers — `booted` → `init-stream` → `ready`,
 * `decode` → `frame` with the same coverage semantics, `series`,
 * `prefetch-window`, `clear-cache`, `progress` and `resident` — over a Zarr
 * v3 store (docs/zarr-profile.md) instead of a `.xue` file, so `main.ts`
 * holds it in the same field without branching. It takes no `recycle`: a
 * frame's plane is a new array out of the WASM reader, posted as it is. The store,
 * the shard indices and the assembly live in `session.ts`; the message loop
 * and the windowed prefetch pump are `endpoint.ts`, which this worker runs
 * one of. The pump mirrors the `.xue` worker's: the main thread names the
 * frame offsets ahead of the playhead plus a concurrency cap, the worker
 * keeps at most that many fetches in flight inside the window,
 * decode-triggered fetches always go first, and `resident` fires once every
 * frame of the axis is local within the tiles the window names.
 *
 * The WASM module reads the format for both channels, and a chunk decodes
 * down the container's own chunk path, so the only thing this channel
 * changes is where the bytes come from and how they are indexed, which is
 * what the comparison with the `.xue` channel is meant to isolate.
 */

import { createZarrEndpoint } from "./endpoint";
import type { ZarrWorkerRequest } from "./protocol";

const scope = self as unknown as DedicatedWorkerGlobalScope;
const endpoint = createZarrEndpoint((message, transfer = []) => scope.postMessage(message, transfer));

self.onmessage = (event: MessageEvent<ZarrWorkerRequest>) => {
  void endpoint.handle(event.data);
};

scope.postMessage({ type: "booted" });
