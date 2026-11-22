import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecodeChannel } from "../../web/src/session";

/** A hub worker that records what it is sent and lets the test answer. */
class FakeHub {
  static spawned: FakeHub[] = [];
  posted: unknown[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  constructor() {
    FakeHub.spawned.push(this);
  }
  postMessage(message: unknown): void {
    this.posted.push(message);
  }
  terminate(): void {}
  emit(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
}

/** A fresh copy of the module, so each case starts with no hubs. */
async function opener(cores: number): Promise<() => DecodeChannel> {
  vi.stubGlobal("navigator", { hardwareConcurrency: cores });
  vi.resetModules();
  const { openZarrHubChannel } = await import("../../web/src/zarr/channel");
  return openZarrHubChannel;
}

/** What one channel was handed, in order. */
function listen(channel: DecodeChannel): unknown[] {
  const heard: unknown[] = [];
  channel.onmessage = (event) => heard.push(event.data);
  return heard;
}

describe("a pin's channels on shared workers", () => {
  beforeEach(() => {
    FakeHub.spawned = [];
    vi.stubGlobal("Worker", FakeHub);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts at most four hubs and gives the next channel to the least loaded", async () => {
    const open = await opener(8);
    for (let index = 0; index < 6; index += 1) open();
    expect(FakeHub.spawned).toHaveLength(4);
    // One channel each until every hub has one, then round again.
    expect(FakeHub.spawned.map((hub) => hub.posted)).toEqual([
      [
        { kind: "open", channel: 1 },
        { kind: "open", channel: 5 },
      ],
      [
        { kind: "open", channel: 2 },
        { kind: "open", channel: 6 },
      ],
      [{ kind: "open", channel: 3 }],
      [{ kind: "open", channel: 4 }],
    ]);
  });

  it("keeps to one hub on a machine with two cores", async () => {
    const open = await opener(2);
    for (let index = 0; index < 5; index += 1) open();
    expect(FakeHub.spawned).toHaveLength(1);
    expect(FakeHub.spawned[0]!.posted).toHaveLength(5);
  });

  it("wraps what a session posts and hands it only its own replies", async () => {
    const open = await opener(2);
    const first = open();
    const second = open();
    const hub = FakeHub.spawned[0]!;
    const heardFirst = listen(first);
    const heardSecond = listen(second);

    hub.emit({ channel: 2, message: { type: "booted" } });
    expect(heardFirst).toEqual([]);
    expect(heardSecond).toEqual([{ type: "booted" }]);

    second.postMessage({ type: "clear-cache" });
    expect(hub.posted.at(-1)).toEqual({ kind: "message", channel: 2, message: { type: "clear-cache" } });
  });

  it("closes a channel without touching its neighbours", async () => {
    const open = await opener(2);
    const first = open();
    const second = open();
    const hub = FakeHub.spawned[0]!;
    const heardFirst = listen(first);
    const heardSecond = listen(second);

    second.terminate();
    expect(hub.posted.at(-1)).toEqual({ kind: "close", channel: 2 });
    // A reply that was on its way is dropped, and the session says no more.
    hub.emit({ channel: 2, message: { type: "series" } });
    second.postMessage({ type: "clear-cache" });
    second.terminate();
    expect(heardSecond).toEqual([]);
    expect(hub.posted).toHaveLength(3);
    // The other channel is as it was.
    hub.emit({ channel: 1, message: { type: "ready" } });
    expect(heardFirst).toEqual([{ type: "ready" }]);
  });

  it("tells every session on a hub that the hub failed", async () => {
    const open = await opener(2);
    const failures: string[] = [];
    for (const name of ["a", "b"]) {
      const channel = open();
      channel.onerror = () => failures.push(name);
    }
    FakeHub.spawned[0]!.onerror?.({ message: "boom" } as ErrorEvent);
    expect(failures).toEqual(["a", "b"]);
  });
});
