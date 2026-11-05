// The decode request queue: the bookkeeping between "a plane is wanted" and
// "a worker handed it back". Requests in flight by request id, the one
// request held back while the inflight cap is reached, the key the primary
// waits on, and the decode timings the stats panel graphs. A landed plane
// is filed into the plane cache here; what it changes on screen stays with
// `main.ts`, behind `planeArrived` and `frameLanded`. Which planes to ask
// for, and whether one already cached still covers the view, is the
// caller's decision.
import type { BundleVariable } from "./manifest";
import { cacheKey, type PlaneCache } from "./planecache";
import type { VariableSession } from "./session";
import type { TileRect } from "./tiles";

/** The window the stats panel's activity graph spans, and its bar width. */
export const DECODE_GRAPH_WINDOW_MS = 30_000;
export const DECODE_GRAPH_BUCKET_MS = 500;

/** A worker's `frame` message: one decoded plane. */
export interface DecodedFrameMessage {
  requestId?: number;
  variableId: number;
  frameOffset: number;
  decodeMs: number;
  /** Absent when the whole plane is valid. */
  tiles?: TileRect[];
  buffer: ArrayBuffer;
}

/** What the queue reads off the page: whether a run is open to decode
 * from, the generation every request is stamped with, the sessions on
 * screen (whose requests narrow to their view, and whose planes per frame
 * size the inflight cap), the frames a total on screen waits on, and the
 * plane cache landed planes go into. */
export interface DecodeQueueDeps {
  readonly ready: boolean;
  readonly generation: number;
  /** Every session on screen, the primary first. */
  readonly slotSessions: () => VariableSession[];
  /** Frames the total on screen waits on beyond the frame itself. */
  readonly totalFrames: () => number;
  readonly planeCache: PlaneCache;
  /** A plane was filed, before eviction can recycle its buffer: its readers
   * that keep a copy or a cell of it (relief, columns, the probe) see it now. */
  readonly planeArrived: (session: VariableSession, variable: BundleVariable, plane: Uint8Array, message: DecodedFrameMessage) => void;
  /** The cache's contents changed (a plane filed, others evicted). */
  readonly cacheChanged: () => void;
  /** A plane landed and the queue has moved on. `wanted` says whether it is
   * the plane the primary frame was waiting on. */
  readonly frameLanded: (key: string, frameOffset: number, wanted: boolean) => void;
}

interface QueuedRequest {
  session: VariableSession;
  variable: BundleVariable;
  hour: number;
}

export function createDecodeQueue(deps: DecodeQueueDeps) {
  /** Request id → cache key of every decode a worker has not answered. */
  const inflight = new Map<number, string>();
  let nextRequestId = 1;
  /** The plane the primary frame waits on: its landing retries the frame. */
  let desiredKey: string | null = null;
  let queuedRequest: QueuedRequest | null = null;
  /** Worker-reported decode time of the most recent plane, for the stats panel. */
  let lastDecodeMs: number | null = null;
  /** Rolling record of decoded planes (bytes + worker decode time), feeding the
   * stats panel's activity graph — the YouTube network-activity analog. The
   * download is one-shot, but decode work streams for as long as playback
   * prefetches planes. */
  const decodeEvents: { at: number; bytes: number; ms: number }[] = [];

  function recordDecodeEvent(bytes: number, ms: number): void {
    const at = performance.now();
    decodeEvents.push({ at, bytes, ms });
    const cutoff = at - DECODE_GRAPH_WINDOW_MS - DECODE_GRAPH_BUCKET_MS;
    while (decodeEvents.length > 0 && decodeEvents[0]!.at < cutoff) decodeEvents.shift();
  }

  /** Decoded bytes per second, averaged over the last two seconds. */
  function decodeRateBytesPerSec(): number {
    const cutoff = performance.now() - 2_000;
    let sum = 0;
    for (let index = decodeEvents.length - 1; index >= 0; index -= 1) {
      if (decodeEvents[index]!.at < cutoff) break;
      sum += decodeEvents[index]!.bytes;
    }
    return sum / 2;
  }

  /** Planes one frame of the whole view needs: the u/v pair for wind, one per
   * scalar, summed over the slots on screen. */
  function planesPerFrame(): number {
    let planes = 0;
    for (const session of deps.slotSessions()) planes += session.variables.length;
    // A total waits on every frame of its window, not one.
    planes += deps.totalFrames();
    return Math.max(1, planes);
  }

  /** Ask one session's decoder for one of its variables at one frame offset.
   * The session is passed in rather than looked up: a numericId names a
   * variable only inside the file it came from. */
  function request(
    session: VariableSession,
    variable: BundleVariable,
    hour: number,
    /** The tiles to decode for a session that is not on screen, which is
     * otherwise asked for whole planes. */
    tiles?: TileRect[] | null,
  ): void {
    if (!deps.ready) return;
    const key = cacheKey(session, variable, hour);
    if ([...inflight.values()].includes(key)) return;
    // The wind session needs two planes per frame and an overlay adds its own,
    // so the inflight cap scales with the planes one frame of the view needs.
    if (inflight.size >= 2 * planesPerFrame()) {
      // Keep only the newest queued request while scrubbing.
      queuedRequest = { session, variable, hour };
      return;
    }
    const requestId = nextRequestId++;
    inflight.set(requestId, key);
    session.worker.postMessage({
      type: "decode",
      requestId,
      generation: deps.generation,
      variableId: variable.numericId,
      frameOffset: hour,
      // Only a session on screen narrows the decode to its view; a background
      // session (the other variable, preloading) is asked for whole planes.
      tiles: (tiles !== undefined ? tiles : deps.slotSessions().includes(session) ? session.viewTiles : null) ?? undefined,
    });
  }

  /** A worker answered a request with an error: it is no longer in flight. */
  function settle(requestId: unknown): void {
    if (typeof requestId === "number") inflight.delete(requestId);
  }

  /** File a plane a session's worker decoded, evict by byte budget, send
   * the request held back by the cap, and hand the landing to the page. */
  function handleFrame(session: VariableSession, message: DecodedFrameMessage): void {
    settle(message.requestId);
    // The message names a variable of *its own* file; the session it arrived
    // on is what makes that a key.
    const variable = session.variables.find((item) => item.numericId === message.variableId);
    if (!variable) return;
    const key = cacheKey(session, variable, message.frameOffset);
    const plane = new Uint8Array(message.buffer);
    deps.planeCache.store(key, { plane, decodeMs: message.decodeMs, tiles: message.tiles ?? null, session });
    deps.planeArrived(session, variable, plane, message);
    lastDecodeMs = message.decodeMs;
    recordDecodeEvent(plane.byteLength, message.decodeMs);
    // Evict by byte budget; the planes on screen are spared.
    deps.planeCache.evict(key);
    deps.cacheChanged();

    if (queuedRequest) {
      const queued = queuedRequest;
      queuedRequest = null;
      const queuedKey = cacheKey(queued.session, queued.variable, queued.hour);
      if (queuedKey !== key && !deps.planeCache.has(queuedKey)) {
        request(queued.session, queued.variable, queued.hour);
      }
    }
    deps.frameLanded(key, message.frameOffset, desiredKey === key);
  }

  /** Forget a disposed session's requests, in flight or held back. */
  function dropSession(session: VariableSession): void {
    for (const [requestId, key] of [...inflight]) {
      if (key.startsWith(`${session.key}:`)) inflight.delete(requestId);
    }
    if (queuedRequest?.session === session) queuedRequest = null;
  }

  /** Forget everything, for a new run. Request ids keep counting. */
  function reset(): void {
    desiredKey = null;
    queuedRequest = null;
    inflight.clear();
    lastDecodeMs = null;
    decodeEvents.length = 0;
  }

  return {
    request,
    settle,
    handleFrame,
    dropSession,
    reset,
    decodeRateBytesPerSec,
    /** The live record; `reset` empties it in place, so a reader may keep it. */
    decodeEvents,
    get inflightCount() {
      return inflight.size;
    },
    get lastDecodeMs() {
      return lastDecodeMs;
    },
    get desiredKey() {
      return desiredKey;
    },
    set desiredKey(value: string | null) {
      desiredKey = value;
    },
    /** Shared with the controllers that post their own requests (series). */
    get nextRequestId() {
      return nextRequestId;
    },
    set nextRequestId(value: number) {
      nextRequestId = value;
    },
  };
}

export type DecodeQueue = ReturnType<typeof createDecodeQueue>;
