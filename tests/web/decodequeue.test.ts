import { describe, expect, it } from "vitest";

import { createDecodeQueue, type DecodeQueueDeps, type DecodedFrameMessage } from "../../web/src/decodequeue";
import type { BundleVariable } from "../../web/src/manifest";
import { cacheKey, createPlaneCache } from "../../web/src/planecache";
import type { VariableSession } from "../../web/src/session";
import type { TileRect } from "../../web/src/tiles";

/** A worker that records what it is sent. */
class FakeWorker {
  posted: { message: unknown; transfer: Transferable[] | undefined }[] = [];
  postMessage(message: unknown, transfer?: Transferable[]): void {
    this.posted.push({ message, transfer });
  }
  terminate(): void {}
}

type FakeSession = VariableSession & { worker: FakeWorker };

function session(key: number, variables = 1): FakeSession {
  return {
    key,
    streaming: true,
    resident: false,
    viewTiles: null,
    metadata: { grid: { width: 4, height: 4 } },
    variables: Array.from({ length: variables }, (_, index) => ({ numericId: index + 1 })),
    worker: new FakeWorker(),
  } as unknown as FakeSession;
}

function variable(owner: VariableSession, position = 0): BundleVariable {
  return owner.variables[position]!;
}

interface Decode {
  type: "decode";
  requestId: number;
  generation: number;
  variableId: number;
  frameOffset: number;
  tiles: unknown;
}

function decodes(owner: FakeSession): Decode[] {
  return owner.worker.posted.map((call) => call.message as Decode).filter((message) => message.type === "decode");
}

/** The worker's answer to a decode request. */
function answer(request: Decode): DecodedFrameMessage {
  return {
    requestId: request.requestId,
    variableId: request.variableId,
    frameOffset: request.frameOffset,
    decodeMs: 7,
    buffer: new ArrayBuffer(16),
  };
}

function harness(onScreen: VariableSession[], overrides: Partial<DecodeQueueDeps> = {}) {
  const planeCache = createPlaneCache({
    activeSession: onScreen[0] ?? null,
    playbackFps: 12,
    playing: false,
    defaultFps: 12,
    constrainedConnection: () => false,
    lowMemoryDevice: () => false,
    totalFrames: () => 0,
    displayedKeys: () => new Set(),
    frameCount: () => 10,
    slotSessions: () => onScreen,
    windowOffset: () => (index) => index,
    totalHours: () => [],
  });
  const landed: { key: string; frameOffset: number; wanted: boolean }[] = [];
  const arrived: string[] = [];
  let cacheChanges = 0;
  const queue = createDecodeQueue({
    ready: true,
    generation: 3,
    slotSessions: () => onScreen,
    totalFrames: () => 0,
    planeCache,
    planeArrived: (owner, item, _plane, message) => arrived.push(cacheKey(owner, item, message.frameOffset)),
    cacheChanged: () => {
      cacheChanges += 1;
    },
    frameLanded: (key, frameOffset, wanted) => landed.push({ key, frameOffset, wanted }),
    ...overrides,
  });
  return { queue, planeCache, landed, arrived, cacheChanges: () => cacheChanges };
}

describe("decode queue", () => {
  it("sends a request once while it is in flight", () => {
    const owner = session(1);
    const { queue } = harness([owner]);
    queue.request(owner, variable(owner), 6);
    queue.request(owner, variable(owner), 6);
    expect(decodes(owner)).toEqual([
      { type: "decode", requestId: 1, generation: 3, variableId: 1, frameOffset: 6, tiles: undefined },
    ]);
    expect(queue.inflightCount).toBe(1);
  });

  it("sends nothing before a run is open", () => {
    const owner = session(1);
    const { queue } = harness([owner], { ready: false });
    queue.request(owner, variable(owner), 6);
    expect(owner.worker.posted).toEqual([]);
  });

  it("files a landed plane, clears its entry and reports it", () => {
    const owner = session(1);
    const { queue, planeCache, landed, arrived, cacheChanges } = harness([owner]);
    queue.request(owner, variable(owner), 6);
    const key = cacheKey(owner, variable(owner), 6);
    queue.desiredKey = key;
    queue.handleFrame(owner, answer(decodes(owner)[0]!));
    expect(queue.inflightCount).toBe(0);
    expect(planeCache.get(key)?.session).toBe(owner);
    expect(planeCache.get(key)?.decodeMs).toBe(7);
    expect(arrived).toEqual([key]);
    expect(cacheChanges()).toBe(1);
    expect(landed).toEqual([{ key, frameOffset: 6, wanted: true }]);
    expect(queue.lastDecodeMs).toBe(7);
    expect(queue.decodeEvents.map((event) => event.bytes)).toEqual([16]);
    // Answered, the same frame can be asked for again.
    queue.request(owner, variable(owner), 6);
    expect(decodes(owner)).toHaveLength(2);
  });

  it("reports a plane nobody waits on as not wanted", () => {
    const owner = session(1);
    const { queue, landed } = harness([owner]);
    queue.request(owner, variable(owner), 6);
    queue.handleFrame(owner, answer(decodes(owner)[0]!));
    expect(landed[0]!.wanted).toBe(false);
  });

  it("ignores a plane for a variable its session does not have", () => {
    const owner = session(1);
    const { queue, planeCache, landed } = harness([owner]);
    queue.request(owner, variable(owner), 6);
    queue.handleFrame(owner, { ...answer(decodes(owner)[0]!), variableId: 9 });
    expect(queue.inflightCount).toBe(0);
    expect(planeCache.frames.size).toBe(0);
    expect(landed).toEqual([]);
  });

  it("holds back the newest request at the cap and sends it when one lands", () => {
    // One session of one variable on screen: the cap is two in flight.
    const owner = session(1);
    const { queue } = harness([owner]);
    queue.request(owner, variable(owner), 0);
    queue.request(owner, variable(owner), 1);
    queue.request(owner, variable(owner), 2);
    queue.request(owner, variable(owner), 3);
    expect(decodes(owner).map((message) => message.frameOffset)).toEqual([0, 1]);
    queue.handleFrame(owner, answer(decodes(owner)[0]!));
    // Only the newest held request survives.
    expect(decodes(owner).map((message) => message.frameOffset)).toEqual([0, 1, 3]);
  });

  it("does not send a held request whose plane is already cached", () => {
    const owner = session(1);
    const { queue, planeCache } = harness([owner]);
    queue.request(owner, variable(owner), 0);
    queue.request(owner, variable(owner), 1);
    queue.request(owner, variable(owner), 2);
    const plane = new Uint8Array(4);
    planeCache.store(cacheKey(owner, variable(owner), 2), { plane, decodeMs: 1, tiles: null, session: owner });
    queue.handleFrame(owner, answer(decodes(owner)[0]!));
    expect(decodes(owner).map((message) => message.frameOffset)).toEqual([0, 1]);
  });

  it("widens the cap with the planes a frame of the view needs", () => {
    const wind = session(1, 2);
    const overlay = session(2);
    const { queue } = harness([wind, overlay]);
    for (let hour = 0; hour < 8; hour += 1) queue.request(wind, variable(wind), hour);
    // Three planes a frame: six in flight.
    expect(decodes(wind)).toHaveLength(6);
  });

  it("narrows a session on screen to its view and asks others for whole planes", () => {
    // Two planes a frame on screen: a cap of four, so all three requests go out.
    const shown = session(1, 2);
    const background = session(2);
    const tiles: TileRect[] = [{ firstColumn: 0, firstRow: 0, lastColumn: 0, lastRow: 0 }];
    (shown as { viewTiles: unknown }).viewTiles = tiles;
    (background as { viewTiles: unknown }).viewTiles = tiles;
    const { queue } = harness([shown]);
    queue.request(shown, variable(shown), 0);
    queue.request(background, variable(background), 0);
    queue.request(background, variable(background), 1, tiles);
    expect(decodes(shown)[0]!.tiles).toBe(tiles);
    expect(decodes(background).map((message) => message.tiles)).toEqual([undefined, tiles]);
  });

  it("settles a request a worker answered with an error", () => {
    const owner = session(1);
    const { queue } = harness([owner]);
    queue.request(owner, variable(owner), 6);
    queue.settle(decodes(owner)[0]!.requestId);
    queue.settle(undefined);
    expect(queue.inflightCount).toBe(0);
  });

  it("drops a disposed session's requests, in flight and held back", () => {
    const gone = session(1);
    const kept = session(11);
    const { queue } = harness([gone]);
    queue.request(kept, variable(kept), 0);
    queue.request(gone, variable(gone), 0);
    queue.request(gone, variable(gone), 1);
    expect(queue.inflightCount).toBe(2);
    queue.dropSession(gone);
    // Session 11's key does not start with "1:": its request stays.
    expect(queue.inflightCount).toBe(1);
    queue.handleFrame(kept, answer(decodes(kept)[0]!));
    // The held request was the disposed session's: nothing more is sent.
    expect(decodes(gone)).toHaveLength(1);
    expect(queue.inflightCount).toBe(0);
  });

  it("forgets everything but the request counter on reset", () => {
    const owner = session(1);
    const { queue } = harness([owner]);
    const events = queue.decodeEvents;
    queue.request(owner, variable(owner), 0);
    queue.handleFrame(owner, answer(decodes(owner)[0]!));
    queue.request(owner, variable(owner), 1);
    queue.desiredKey = "x";
    queue.reset();
    expect(queue.inflightCount).toBe(0);
    expect(queue.desiredKey).toBeNull();
    expect(queue.lastDecodeMs).toBeNull();
    expect(queue.decodeEvents).toBe(events);
    expect(events).toEqual([]);
    queue.request(owner, variable(owner), 1);
    expect(decodes(owner).at(-1)!.requestId).toBe(3);
  });

  it("shares its request ids with the controllers that post their own", () => {
    const owner = session(1);
    const { queue } = harness([owner]);
    queue.nextRequestId = 40;
    queue.request(owner, variable(owner), 0);
    expect(decodes(owner)[0]!.requestId).toBe(40);
    expect(queue.nextRequestId).toBe(41);
  });
});
