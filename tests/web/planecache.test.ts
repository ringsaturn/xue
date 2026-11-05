import { describe, expect, it } from "vitest";

import type { BundleVariable } from "../../web/src/manifest";
import { cacheKey, createPlaneCache, MEBIBYTE, type PlaneCacheDeps } from "../../web/src/planecache";
import type { DecodedFrame, VariableSession } from "../../web/src/session";

const DEFAULT_FPS = 12;

/** A worker that records what it is sent. */
class FakeWorker {
  posted: { message: unknown; transfer: Transferable[] | undefined }[] = [];
  postMessage(message: unknown, transfer?: Transferable[]): void {
    this.posted.push({ message, transfer });
  }
  terminate(): void {}
}

function session(key: number, width = 1024, height = 1024, variables = 1): VariableSession & { worker: FakeWorker } {
  return {
    key,
    streaming: true,
    resident: false,
    viewTiles: null,
    metadata: { grid: { width, height } },
    variables: Array.from({ length: variables }, (_, index) => ({ numericId: index + 1 })),
    worker: new FakeWorker(),
  } as unknown as VariableSession & { worker: FakeWorker };
}

/** A decoded plane that claims `mebibytes` without allocating them. */
function frame(owner: VariableSession, mebibytes: number): DecodedFrame {
  const plane = { byteLength: mebibytes * MEBIBYTE, buffer: new ArrayBuffer(8) } as unknown as Uint8Array;
  return { plane, decodeMs: 1, tiles: null, session: owner };
}

function deps(overrides: Partial<PlaneCacheDeps> = {}): PlaneCacheDeps {
  return {
    activeSession: null,
    playbackFps: DEFAULT_FPS,
    playing: false,
    defaultFps: DEFAULT_FPS,
    constrainedConnection: () => false,
    lowMemoryDevice: () => false,
    totalFrames: () => 0,
    displayedKeys: () => new Set(),
    frameCount: () => 20,
    slotSessions: () => [],
    windowOffset: () => (index) => index * 3,
    totalHours: () => [],
    ...overrides,
  };
}

/** A constrained connection's budget is a flat 24 MiB, whatever the session. */
const constrained = { constrainedConnection: () => true };

function recycled(worker: FakeWorker): unknown[] {
  return worker.posted.map((call) => (call.message as { buffer: unknown }).buffer);
}

describe("plane cache", () => {
  it("keeps everything that fits the budget", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps(constrained));
    cache.store("a", frame(owner, 10));
    cache.store("b", frame(owner, 10));
    cache.evict("b");
    expect([...cache.frames.keys()]).toEqual(["a", "b"]);
    expect(cache.bytes).toBe(20 * MEBIBYTE);
    expect(owner.worker.posted).toEqual([]);
  });

  it("uncounts the plane a store replaces", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps(constrained));
    cache.store("a", frame(owner, 10));
    cache.store("a", frame(owner, 4));
    expect(cache.bytes).toBe(4 * MEBIBYTE);
  });

  it("evicts oldest first until the bytes fit", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps(constrained));
    const planes = ["a", "b", "c", "d"].map((key) => [key, frame(owner, 10)] as const);
    for (const [key, plane] of planes.slice(0, 3)) cache.store(key, plane);
    cache.evict("c");
    expect([...cache.frames.keys()]).toEqual(["b", "c"]);
    cache.store("d", planes[3]![1]);
    cache.evict("d");
    expect([...cache.frames.keys()]).toEqual(["c", "d"]);
    expect(cache.bytes).toBe(20 * MEBIBYTE);
    expect(recycled(owner.worker)).toEqual([planes[0]![1].plane.buffer, planes[1]![1].plane.buffer]);
  });

  it("follows the order refresh leaves, and get leaves it alone", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps(constrained));
    const a = frame(owner, 10);
    cache.store("a", a);
    cache.store("b", frame(owner, 10));
    cache.refresh("a", a);
    expect(cache.get("b")).toBeDefined();
    cache.store("c", frame(owner, 10));
    cache.evict("c");
    expect([...cache.frames.keys()]).toEqual(["a", "c"]);
  });

  it("refreshes a key on screen instead of evicting it", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps({ ...constrained, displayedKeys: () => new Set(["a"]) }));
    for (const key of ["a", "b", "c"]) cache.store(key, frame(owner, 10));
    cache.evict("c");
    expect([...cache.frames.keys()]).toEqual(["c", "a"]);
    expect(owner.worker.posted).toHaveLength(1);
  });

  it("never evicts the plane just inserted, even over budget", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps({ ...constrained, displayedKeys: () => new Set(["a", "b"]) }));
    for (const key of ["a", "b", "c"]) cache.store(key, frame(owner, 10));
    cache.evict("c");
    expect([...cache.frames.keys()]).toEqual(["c", "a", "b"]);
    expect(cache.bytes).toBe(30 * MEBIBYTE);
    expect(owner.worker.posted).toEqual([]);
  });

  it("keeps two planes whatever their size", () => {
    const owner = session(1);
    const cache = createPlaneCache(deps(constrained));
    cache.store("a", frame(owner, 100));
    cache.store("b", frame(owner, 100));
    cache.evict("b");
    expect(cache.frames.size).toBe(2);
  });

  it("recycles a buffer into the worker of the session that decoded it", () => {
    const filer = session(1);
    const owner = session(2);
    const cache = createPlaneCache(deps(constrained));
    const evicted = frame(owner, 10);
    cache.store("1:1:0", evicted);
    cache.store("1:1:1", frame(filer, 10));
    cache.store("1:1:2", frame(filer, 10));
    cache.evict("1:1:2");
    expect(filer.worker.posted).toEqual([]);
    expect(owner.worker.posted).toHaveLength(1);
    const [call] = owner.worker.posted;
    expect(call!.message).toEqual({ type: "recycle", buffer: evicted.plane.buffer });
    expect(call!.transfer).toEqual([evicted.plane.buffer]);
    expect(call!.transfer![0]).toBe(evicted.plane.buffer);
  });

  it("drops a disposed session's planes without recycling them", () => {
    const kept = session(1);
    const gone = session(2);
    const cache = createPlaneCache(deps(constrained));
    cache.store("a", frame(gone, 5));
    cache.store("b", frame(kept, 5));
    cache.store("c", frame(gone, 5));
    cache.dropSession(gone);
    expect([...cache.frames.keys()]).toEqual(["b"]);
    expect(cache.bytes).toBe(5 * MEBIBYTE);
    expect(gone.worker.posted).toEqual([]);
    cache.reset();
    expect(cache.frames.size).toBe(0);
    expect(cache.bytes).toBe(0);
  });

  it("keys a plane by session, variable and offset", () => {
    const owner = session(7);
    expect(cacheKey(owner, { numericId: 2 } as BundleVariable, 36)).toBe("7:2:36");
  });
});

describe("plane cache budget and prefetch window", () => {
  const mebibyteFrame = session(1, 1024, 1024);
  const thirtyMebibyteFrame = session(1, 1024, 1024 * 30);

  it("holds the floor and the whole window before a session opens", () => {
    const cache = createPlaneCache(deps());
    expect(cache.budgetBytes()).toBe(64 * MEBIBYTE);
    expect(cache.capBytes()).toBe(256 * MEBIBYTE);
    expect(cache.prefetchWindowFrames()).toBe(10);
  });

  it("keeps a small frame's window whole under the floor", () => {
    const cache = createPlaneCache(deps({ activeSession: mebibyteFrame }));
    expect(cache.budgetBytes()).toBe(64 * MEBIBYTE);
    expect(cache.prefetchWindowFrames()).toBe(10);
  });

  it("scales the window with the playback rate", () => {
    const cache = createPlaneCache(deps({ activeSession: mebibyteFrame, playbackFps: DEFAULT_FPS * 2 }));
    expect(cache.prefetchWindowFrames()).toBe(20);
  });

  it("shrinks the window to what the cap holds for a large frame", () => {
    const cache = createPlaneCache(deps({ activeSession: thirtyMebibyteFrame }));
    expect(cache.budgetBytes()).toBe(256 * MEBIBYTE);
    // floor(256 / 30) = 8 frames, less the two on screen.
    expect(cache.prefetchWindowFrames()).toBe(6);
  });

  it("leaves room for a total's frames behind the playhead", () => {
    const cache = createPlaneCache(deps({ activeSession: thirtyMebibyteFrame, totalFrames: () => 3 }));
    expect(cache.prefetchWindowFrames()).toBe(3);
  });

  it("never narrows the window below two frames", () => {
    const cache = createPlaneCache(deps({ activeSession: thirtyMebibyteFrame, lowMemoryDevice: () => true }));
    expect(cache.budgetBytes()).toBe(128 * MEBIBYTE);
    expect(cache.capBytes()).toBe(128 * MEBIBYTE);
    expect(cache.prefetchWindowFrames()).toBe(2);
    // floor(256 / 100) = 2 frames leaves none for the window, which stays at two.
    const huge = createPlaneCache(deps({ activeSession: session(1, 1024, 1024 * 100) }));
    expect(huge.prefetchWindowFrames()).toBe(2);
  });

  it("holds a constrained connection to a flat budget and a short window", () => {
    const cache = createPlaneCache(deps({ ...constrained, activeSession: mebibyteFrame }));
    expect(cache.budgetBytes()).toBe(24 * MEBIBYTE);
    expect(cache.capBytes()).toBe(24 * MEBIBYTE);
    expect(cache.prefetchWindowFrames()).toBe(4);
  });
});

describe("prefetch window messages", () => {
  it("sends the primary its window once, until the session is forgotten", () => {
    const primary = session(1);
    const cache = createPlaneCache(deps({ activeSession: primary, slotSessions: () => [primary] }));
    cache.sendPrefetchWindow(0);
    expect(primary.worker.posted).toHaveLength(1);
    expect(primary.worker.posted[0]!.message).toEqual({
      type: "prefetch-window",
      hours: Array.from({ length: 11 }, (_, step) => step * 3),
      concurrency: 3,
      tiles: undefined,
    });
    cache.sendPrefetchWindow(0);
    expect(primary.worker.posted).toHaveLength(1);
    cache.forgetSession(primary.key);
    cache.sendPrefetchWindow(0);
    expect(primary.worker.posted).toHaveLength(2);
  });

  it("wraps at the loop point and appends a total's hours", () => {
    const primary = session(1);
    const cache = createPlaneCache(
      deps({
        activeSession: primary,
        slotSessions: () => [primary],
        frameCount: () => 12,
        windowOffset: () => (index) => index,
        totalHours: () => [10, 4, 3],
      }),
    );
    cache.sendPrefetchWindow(5);
    const hours = (primary.worker.posted[0]!.message as { hours: number[] }).hours;
    expect(hours).toEqual([5, 6, 7, 8, 9, 10, 11, 0, 1, 2, 3, 4]);
  });

  it("gives an overlay one fetch at a time and holds it back while playing on a constrained link", () => {
    const primary = session(1);
    const overlay = session(2);
    const page = { activeSession: primary, slotSessions: () => [primary, overlay] };
    createPlaneCache(deps(page)).sendPrefetchWindow(0);
    expect((primary.worker.posted[0]!.message as { concurrency: number }).concurrency).toBe(3);
    expect((overlay.worker.posted[0]!.message as { concurrency: number }).concurrency).toBe(1);
    createPlaneCache(deps({ ...page, ...constrained, playing: true })).sendPrefetchWindow(0);
    expect((primary.worker.posted[1]!.message as { concurrency: number }).concurrency).toBe(1);
    expect(overlay.worker.posted).toHaveLength(1);
  });

  it("forgets the memo on dropSession and reset", () => {
    const primary = session(1);
    const cache = createPlaneCache(deps({ activeSession: primary, slotSessions: () => [primary] }));
    cache.sendPrefetchWindow(0);
    cache.dropSession(primary);
    cache.sendPrefetchWindow(0);
    cache.reset();
    cache.sendPrefetchWindow(0);
    expect(primary.worker.posted).toHaveLength(3);
  });

  it("skips a resident session and one that does not stream", () => {
    const resident = session(1);
    resident.resident = true;
    const whole = session(2);
    whole.streaming = false;
    const cache = createPlaneCache(deps({ activeSession: resident, slotSessions: () => [resident, whole] }));
    cache.sendPrefetchWindow(0);
    expect(resident.worker.posted).toEqual([]);
    expect(whole.worker.posted).toEqual([]);
  });
});
