// The decoded-plane cache (the frame cache): every plane a decode worker
// has handed back, keyed by `cacheKey`, in least-recently-used order, under
// a byte budget sized to the primary session's frame, and the windowed
// prefetch that tells each session's worker which frames to keep resident
// ahead of the playhead. Eviction hands a plane's buffer back to the worker
// that decoded it. What is on screen, and so what eviction must spare, stays
// with `main.ts`, which reports it through `displayedKeys`.
import type { BundleVariable } from "./manifest";
import type { DecodedFrame, VariableSession } from "./session";
import { frameCacheKey } from "./sessionkeys";

export const MEBIBYTE = 1024 * 1024;

/** The frame cache's key. A numericId is file-local — the encoder numbers a
 * bundle's variables 1..n, so `tmp2m`'s only variable and `prmsl`'s are both
 * 1 — and the session it belongs to is what makes the pair unique
 * (sessionkeys.ts). Every reader and writer of this cache, the probe series
 * included, keys the same way. */
export function cacheKey(session: VariableSession, variable: BundleVariable, offset: number): string {
  return frameCacheKey(session, variable, offset);
}

/** What the cache reads off the page: the primary session, the playback
 * rate and whether it is playing (live, through getters), the device and
 * connection class, the frames a total on screen keeps behind the playhead,
 * the keys on screen that eviction must spare, and the axis the prefetch
 * window walks. */
export interface PlaneCacheDeps {
  readonly activeSession: VariableSession | null;
  readonly playbackFps: number;
  readonly playing: boolean;
  readonly defaultFps: number;
  readonly constrainedConnection: () => boolean;
  readonly lowMemoryDevice: () => boolean;
  /** Frames the total on screen keeps resident behind the playhead. */
  readonly totalFrames: () => number;
  /** Every key a layer on screen is drawing: the blend path may still
   * sample them, and wind keeps a u/v pair displayed. */
  readonly displayedKeys: () => Set<string>;
  readonly frameCount: () => number;
  /** Every session on screen. */
  readonly slotSessions: () => VariableSession[];
  /** How a session reads the primary axis: a frame index to its own frame
   * offset, or null where its axis has no such lead. An overlay goes
   * through its slot (or the lead itself, outside a slot), the primary
   * through its own axis. */
  readonly windowOffset: (session: VariableSession, overlay: boolean) => (frame: number) => number | null;
  /** The frame offsets the total on screen reads for frame `index` (its
   * window behind the playhead); none while the primary is drawn as it is. */
  readonly totalHours: (session: VariableSession, index: number) => number[];
}

// The plane cache is byte-budgeted (not frame-count-limited) and the
// background prefetch is windowed — a handful of frames ahead of the
// playhead, a few fetches at a time, narrower on constrained connections.
// The two agree with the frame size of the primary session: the cache
// grows to hold the window plus the two frames on screen, up to a cap, and
// the window shrinks to what the cache can hold when the cap is hit. A
// frame's planes are always allocated at the tier's whole grid, even when
// the session decodes only the viewport's tiles into them, so the frame
// size is the grid's cells times the variables a frame needs, whatever the
// view shows.
export function createPlaneCache(deps: PlaneCacheDeps) {
  const frames = new Map<string, DecodedFrame>();
  let bytes = 0;
  /** Last prefetch window sent per session, to skip redundant messages. */
  const lastPrefetchWindow = new Map<number, string>();

  /** Bytes one frame of the primary session occupies in the plane cache, or
   * null before a session is open. */
  function primaryFrameBytes(): number | null {
    const session = deps.activeSession;
    if (!session) return null;
    const grid = session.metadata.grid;
    return grid.width * grid.height * session.variables.length;
  }

  /** The frames the window would keep ahead of the playhead if memory were
   * no object. The window is runway in time, not in frames: faster playback
   * burns through the same number of frames sooner, so it needs
   * proportionally more of them resident ahead of the playhead. */
  function prefetchWindowWanted(): number {
    const base = deps.constrainedConnection() ? 4 : 10;
    return Math.max(3, Math.round((base * deps.playbackFps) / deps.defaultFps));
  }

  /** The plane cache's ceiling: the floor it has always had (64 MB, 24 MB
   * on a constrained connection, where the floor is also the ceiling — the
   * budget there is the network's, not the device's), raised to hold the
   * wanted window plus two frames of the primary session, and capped at
   * 256 MB (128 MB on a four-gigabyte device). A GFS run never leaves the
   * floor; a full satellite disk (9 MB a plane, 27 MB a Dust RGB frame)
   * fills the cap, and the window below gives way to it. */
  function budgetBytes(): number {
    if (deps.constrainedConnection()) return 24 * MEBIBYTE;
    const floor = 64 * MEBIBYTE;
    const cap = (deps.lowMemoryDevice() ? 128 : 256) * MEBIBYTE;
    const frame = primaryFrameBytes();
    if (frame === null) return floor;
    return Math.min(cap, Math.max(floor, frame * (prefetchWindowWanted() + 2 + deps.totalFrames())));
  }

  /** The plane cache's cap, whatever the session: what a total's window has
   * to fit under (`offeredWindows`). */
  function capBytes(): number {
    if (deps.constrainedConnection()) return 24 * MEBIBYTE;
    return (deps.lowMemoryDevice() ? 128 : 256) * MEBIBYTE;
  }

  /** The frames to keep resident ahead of the playhead: the wanted window,
   * clamped so that it and the two frames on screen fit the cache — never
   * below two, since the blend needs the next frame and playback the one
   * after it. */
  function prefetchWindowFrames(): number {
    const wanted = prefetchWindowWanted();
    const frame = primaryFrameBytes();
    if (frame === null) return wanted;
    const fits = Math.floor(budgetBytes() / frame) - 2 - deps.totalFrames();
    return Math.max(2, Math.min(wanted, fits));
  }

  function prefetchConcurrency(): number {
    return deps.constrainedConnection() ? 1 : 3;
  }

  /** Tell every session on screen which frames to keep resident (windowed
   * prefetch): the window ahead of the playhead, wrapped at the loop point,
   * and the tiles the view needs of them. The primary gets the connection's
   * concurrency; an overlay one fetch at a time, so it never crowds out the
   * frame that gates playback, and on a constrained connection an overlay
   * only prefetches while playback is stopped. */
  function sendPrefetchWindow(index: number): void {
    const total = deps.frameCount();
    for (const session of deps.slotSessions()) {
      if (!session.streaming || session.resident) continue;
      const overlay = session !== deps.activeSession;
      if (overlay && deps.playing && deps.constrainedConnection()) continue;
      const offsetOf = deps.windowOffset(session, overlay);
      const hours: number[] = [];
      for (let step = 0; step <= prefetchWindowFrames(); step += 1) {
        const frame = (index + step) % total;
        const offset = offsetOf(frame);
        if (offset !== null && !hours.includes(offset)) hours.push(offset);
      }
      // A total keeps its window behind the playhead resident too.
      if (!overlay) {
        for (const hour of deps.totalHours(session, index)) if (!hours.includes(hour)) hours.push(hour);
      }
      if (hours.length === 0) continue;
      const key = `${hours[0]}/${hours.length}`;
      if (lastPrefetchWindow.get(session.key) === key) continue;
      lastPrefetchWindow.set(session.key, key);
      session.worker.postMessage({
        type: "prefetch-window",
        hours,
        concurrency: overlay ? 1 : prefetchConcurrency(),
        tiles: session.viewTiles ?? undefined,
      });
    }
  }

  /** Forget the window last sent to a session, so the next send goes out
   * even when it names the same first frame and length: the memo is keyed
   * by those alone, not by the tiles or the total the window covers. */
  function forgetSession(sessionKey: number): void {
    lastPrefetchWindow.delete(sessionKey);
  }

  /** The plane under `key`, leaving its place in the eviction order alone. */
  function get(key: string): DecodedFrame | undefined {
    return frames.get(key);
  }

  function has(key: string): boolean {
    return frames.has(key);
  }

  /** Move a plane a layer is about to draw to the newest end of the
   * eviction order. */
  function refresh(key: string, frame: DecodedFrame): void {
    frames.delete(key);
    frames.set(key, frame);
  }

  /** File a decoded plane, uncounting whatever was under its key. Nothing
   * is evicted here: `evict` runs once the plane's readers have seen it,
   * since eviction may recycle buffers back into their workers. */
  function store(key: string, frame: DecodedFrame): void {
    const previous = frames.get(key);
    if (previous) bytes -= previous.plane.byteLength;
    frames.set(key, frame);
    bytes += frame.plane.byteLength;
  }

  /** Evict by byte budget, oldest first; never evict the just-inserted
   * frame, and refresh rather than evict the ones on screen. */
  function evict(justInserted: string): void {
    const displayedKeys = deps.displayedKeys();
    while (bytes > budgetBytes() && frames.size > 2) {
      const oldest = frames.keys().next().value;
      if (oldest === undefined || oldest === justInserted) break;
      const evicted = frames.get(oldest)!;
      frames.delete(oldest);
      if (displayedKeys.has(oldest)) {
        // Refresh instead of evicting: move to the newest LRU position.
        frames.set(oldest, evicted);
        continue;
      }
      bytes -= evicted.plane.byteLength;
      // The buffer goes back to the worker that produced it — which the frame
      // itself names, not the key it was filed under.
      const buffer = evicted.plane.buffer as ArrayBuffer;
      evicted.session.worker.postMessage({ type: "recycle", buffer }, [buffer]);
    }
  }

  /** Drop a disposed session's window memo and every plane it decoded.
   * Its buffers are not recycled — the worker they belong to is gone. */
  function dropSession(session: VariableSession): void {
    forgetSession(session.key);
    for (const [key, frame] of [...frames]) {
      if (frame.session !== session) continue;
      frames.delete(key);
      bytes -= frame.plane.byteLength;
    }
  }

  /** Forget everything, for a new run. */
  function reset(): void {
    frames.clear();
    bytes = 0;
    lastPrefetchWindow.clear();
  }

  return {
    /** The map itself, for the controllers that read and refresh planes
     * directly (probe panel, marks, derived fields). */
    frames,
    get bytes() {
      return bytes;
    },
    get,
    has,
    refresh,
    store,
    evict,
    dropSession,
    reset,
    budgetBytes,
    capBytes,
    prefetchWindowFrames,
    sendPrefetchWindow,
    forgetSession,
  };
}

export type PlaneCache = ReturnType<typeof createPlaneCache>;
