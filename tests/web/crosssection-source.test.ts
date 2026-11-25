/** The column store as a section reads it, over fake channels. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createSectionSource, sectionStore, SectionError, type SectionSourceDeps } from "../../web/src/crosssection/source";
import type { SectionPlan } from "../../web/src/crosssection/types";
import type { ForecastManifest } from "../../web/src/manifest";
import type { DecodeChannel } from "../../web/src/session";
import { globalGrid, registryEntries, registryVariable } from "./helpers";

const RUN_TIME = "2026-08-15T06:00:00Z";
const MANIFEST_URL = "https://example.test/xue/gfs.2026081506/manifest.json?v=1";
const COLUMNS = { path: "columns.zarr", byteLength: 10, crc32: "0000abcd" };

function member(bundle: string, id: string): { bundle: string; variable: unknown } {
  const entry = registryEntries([id])[id]!;
  // The number is the bundle's own and means nothing in a column store.
  return { bundle, variable: { ...registryVariable(id, entry, entry.quality), numericId: 7 } };
}

/** Two levels of temperature over three frames, and a one-frame terrain. */
function block(overrides: Record<string, unknown> = {}): string {
  const { time, grid } = globalGrid();
  return JSON.stringify({
    version: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: RUN_TIME,
    profile: "quality",
    time,
    grid,
    arrays: [
      { id: "tmp", levels: [850, 500], members: [member("tmp850", "tmp850"), member("tmp500", "tmp500")] },
      {
        id: "orog",
        time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 1, frameStep: 1 },
        members: [member("orog", "orog")],
      },
    ],
    ...overrides,
  });
}

class FakeChannel implements DecodeChannel {
  posted: Record<string, unknown>[] = [];
  terminated = 0;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  postMessage(message: unknown): void {
    this.posted.push(message as Record<string, unknown>);
  }
  terminate(): void {
    this.terminated += 1;
  }
  emit(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
  get reads(): Record<string, unknown>[] {
    return this.posted.filter((message) => message.type === "cells");
  }
}

function manifest(overrides: Partial<ForecastManifest> = {}): ForecastManifest {
  return { model: "GFS", runTime: RUN_TIME, bundles: [], columns: COLUMNS, ...overrides } as unknown as ForecastManifest;
}

function bare(overrides: Partial<ForecastManifest> = {}): ForecastManifest {
  const { columns: _, ...rest } = manifest(overrides);
  return rest as ForecastManifest;
}

function harness(overrides: Partial<SectionSourceDeps> = {}) {
  const channels: FakeChannel[] = [];
  const deps = {
    manifest: manifest() as ForecastManifest | null,
    manifestUrl: MANIFEST_URL as string | null,
    dataBackend: "zarr" as SectionSourceDeps["dataBackend"],
    streams: async () => true,
    openChannel: () => {
      const channel = new FakeChannel();
      channels.push(channel);
      return channel;
    },
    ...overrides,
  };
  return { source: createSectionSource(deps), channels, deps };
}

async function opened(overrides: Partial<SectionSourceDeps> = {}, blockJson = block()) {
  const held = harness(overrides);
  const opening = held.source.open();
  held.channels[0]!.emit({ type: "booted" });
  held.channels[0]!.emit({ type: "ready", blockJson, blockShape: [8, 8] });
  return { ...held, channel: held.channels[0]!, store: await opening };
}

/** A plan of two cells, which is all of it a read looks at. */
const PLAN = { cells: Uint32Array.from([0, 0, 9, 9]) } as unknown as SectionPlan;

/** Why an open was refused. */
async function reasonOf(opening: Promise<unknown>): Promise<string> {
  const error = await opening.then(
    () => null,
    (failure: unknown) => failure,
  );
  expect(error).toBeInstanceOf(SectionError);
  return (error as SectionError).reason;
}

/** The name of what a read rejected with. */
async function nameOf(done: Promise<void>): Promise<string> {
  return done.then(
    () => "nothing",
    (error: Error) => error.name,
  );
}

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("the store a block describes", () => {
  it("has each array's axis in seconds and a codebook per level", () => {
    const store = sectionStore(block(), [8, 4]);
    expect(store).toMatchObject({ model: "GFS", runTime: Date.parse(RUN_TIME), width: 1440, height: 721, blockHeight: 8, blockWidth: 4 });
    expect([...store.arrays.keys()]).toEqual(["tmp", "orog"]);
    const tmp = store.arrays.get("tmp")!;
    expect(tmp.levels).toEqual([850, 500]);
    expect(tmp.offsets).toEqual([0, 3600, 7200]);
    expect(tmp.members.map((variable) => [variable.id, variable.numericId])).toEqual([
      ["tmp850", 1],
      ["tmp500", 2],
    ]);
    const orog = store.arrays.get("orog")!;
    expect(orog.levels).toBeNull();
    expect(orog.offsets).toEqual([0]);
    expect([tmp.quantity, orog.quantity]).toEqual(["tmp", "orog"]);
    expect(store.grid.lonLatToCell(-180, 90)).toEqual([0, 0]);
  });

  it("tells what an array is of by its members' parameters, not by its name", () => {
    const named = (id: string, ...members: string[]) => ({ id, ...(members.length > 1 && { levels: members.map((_, at) => 850 - at) }), members: members.map((member_) => member(member_, member_)) });
    const arrays = [
      named("a", "ugrd850", "ugrd500"),
      named("b", "ugrd10m"),
      named("c", "prmsl"),
      named("d", "dpt2m"),
      // One of each is neither.
      named("tmp", "tmp850", "rh850"),
      named("rh", "prate"),
    ];
    const store = sectionStore(block({ arrays }), [8, 8]);
    expect([...store.arrays.values()].map(({ quantity }) => quantity)).toEqual(["ugrd", "ugrd10m", "prmsl", "dpt2m", null, null]);
  });

  it("leaves out an array whose variables do not read", () => {
    const broken = { bundle: "rh850", variable: { numericId: 1, id: "rh850" } };
    const arrays = [
      { id: "rh", levels: [850], members: [broken] },
      { id: "tmp", levels: [850], members: [member("tmp850", "tmp850")] },
    ];
    expect([...sectionStore(block({ arrays }), [8, 8]).arrays.keys()]).toEqual(["tmp"]);
  });
});

describe("opening a section's store", () => {
  it("holds the chunk bytes it is told to", () => {
    const { source, channels } = harness({ heldBytes: 64 << 20 });
    source.open().catch(() => {});
    expect(channels[0]!.posted[0]).toMatchObject({ type: "init-columns", heldBytes: 64 << 20 });
    source.end();
  });

  it("opens one channel, under the store's crc, holding nothing, for all who ask", async () => {
    const { source, channel, channels, store } = await opened();
    expect(channel.posted).toEqual([
      { type: "init-columns", url: "https://example.test/xue/gfs.2026081506/columns.zarr", crc32: "0000abcd", heldBytes: 0 },
    ]);
    expect(await source.open()).toBe(store);
    expect(channels).toHaveLength(1);
  });

  it("reads a lender's store whole", async () => {
    const lent = "https://example.test/xue/gfs.2026081506/columns.zarr";
    const { channel, store } = await opened({
      manifest: bare({ model: "SFLUX", borrowed: { bundles: ["tmp500"], columns: { ...COLUMNS, path: lent } } } as Partial<ForecastManifest>),
      manifestUrl: "https://example.test/xue/sflux.2026081506/manifest.json",
    });
    expect(channel.posted[0]!.url).toBe(lent);
    expect([...store.arrays.keys()]).toEqual(["tmp", "orog"]);
  });

  it("says why there is none", async () => {
    expect(await reasonOf(harness({ manifest: null }).source.open())).toBe("model");
    expect(await reasonOf(harness({ manifest: bare() }).source.open())).toBe("model");
    expect(await reasonOf(harness({ dataBackend: "xue" }).source.open())).toBe("model");
    const lender = bare({ borrowed: { bundles: ["tmp500"] } } as Partial<ForecastManifest>);
    expect(await reasonOf(harness({ manifest: lender }).source.open())).toBe("run");
    // A later run of a dataset that has had one.
    const { source, deps } = await opened();
    source.end();
    deps.manifest = bare();
    expect(await reasonOf(source.open())).toBe("run");
  });

  it("fails on a group that does not open, an origin without ranges and another run's store, and can be tried again", async () => {
    const refused = harness();
    const first = refused.source.open();
    refused.channels[0]!.emit({ type: "error", message: "HTTP 404" });
    expect(await reasonOf(first)).toBe("failed");
    expect(refused.channels[0]!.terminated).toBe(1);
    const second = refused.source.open();
    refused.channels[1]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    expect((await second).model).toBe("GFS");

    const whole = harness({ streams: async () => false });
    const ranged = whole.source.open();
    whole.channels[0]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    expect(await reasonOf(ranged)).toBe("origin");

    const other = harness();
    const another = other.source.open();
    other.channels[0]!.emit({ type: "ready", blockJson: block({ runTime: "2026-08-15T12:00:00Z" }), blockShape: [8, 8] });
    expect(await reasonOf(another)).toBe("failed");

    const died = harness();
    const dying = died.source.open();
    died.channels[0]!.onerror?.({ message: "gone" } as ErrorEvent);
    expect(await reasonOf(dying)).toBe("failed");
  });

  it.each([
    ["a group", {}],
    ["a range probe", { streams: () => new Promise<boolean>(() => {}) }],
  ])("stops waiting for %s that never comes", async (_, overrides) => {
    vi.useFakeTimers();
    const { source, channels } = harness(overrides);
    const reason = reasonOf(source.open());
    if ("streams" in overrides) channels[0]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await reason).toBe("failed");
    expect(channels[0]!.terminated).toBe(1);
  });

  it("opens the store the manifest names now", async () => {
    const { source, channels, deps } = await opened();
    deps.manifest = manifest({ columns: { ...COLUMNS, crc32: "0000beef" } } as Partial<ForecastManifest>);
    const opening = source.open();
    expect(channels[0]!.terminated).toBe(1);
    expect(channels[1]!.posted[0]!.crc32).toBe("0000beef");
    channels[1]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    expect(await opening).toBeDefined();
  });
});

describe("reading a section", () => {
  /** Two cells of the tmp array, in one batch or the first of two. */
  function reply(channel: FakeChannel, at: number, indices: number[], done: boolean, perCell: number): void {
    channel.emit({
      type: "cells",
      requestId: channel.reads[at]!.requestId,
      indices: Uint32Array.from(indices),
      buffer: new Uint8Array(indices.length * perCell).buffer,
      done,
    });
  }

  it("asks for every array over all the cells, and is done with the last of them", async () => {
    const { source, channel } = await opened();
    const got: [string, number[], number][] = [];
    const read = source.read(PLAN, ["tmp", "orog", "rh", "tmp"], (array, indices, codes) => got.push([array, [...indices], codes.length]));
    // One it does not hold is passed over, one named twice is read once.
    expect(channel.reads.map(({ array, cells }) => [array, cells])).toEqual([
      ["tmp", PLAN.cells],
      ["orog", PLAN.cells],
    ]);
    let done = false;
    void read.done.then(() => {
      done = true;
    });
    reply(channel, 0, [0], false, 6);
    reply(channel, 0, [1], true, 6);
    await Promise.resolve();
    expect(done).toBe(false);
    reply(channel, 1, [0, 1], true, 1);
    await read.done;
    expect(got).toEqual([
      ["tmp", [0], 6],
      ["tmp", [1], 6],
      ["orog", [0, 1], 2],
    ]);
    // A finished read has nothing to call off.
    read.cancel();
    expect(channel.posted.at(-1)!.type).toBe("cells");
  });

  it("lets a read go when another begins, when it is cancelled and with the run", async () => {
    const { source, channel } = await opened();
    const got: string[] = [];
    const first = source.read(PLAN, ["tmp"], (array) => got.push(`first ${array}`));
    const second = source.read(PLAN, ["orog"], (array) => got.push(`second ${array}`));
    expect(await nameOf(first.done)).toBe("AbortError");
    expect(channel.posted.map((message) => message.type)).toEqual(["init-columns", "cells", "supersede", "cells"]);
    reply(channel, 0, [0, 1], true, 6);
    channel.emit({ type: "error", requestId: channel.reads[0]!.requestId, message: "too late" });
    reply(channel, 1, [0], false, 1);
    // The first read's handle is not the second's.
    first.cancel();
    reply(channel, 1, [1], true, 1);
    await second.done;
    expect(got).toEqual(["second orog", "second orog"]);

    const third = source.read(PLAN, ["tmp"], () => got.push("third"));
    third.cancel();
    expect(await nameOf(third.done)).toBe("AbortError");
    expect(channel.posted.at(-1)).toEqual({ type: "supersede" });
    reply(channel, 2, [0, 1], true, 6);

    const fourth = source.read(PLAN, ["tmp"], () => got.push("fourth"));
    source.end();
    expect(await nameOf(fourth.done)).toBe("AbortError");
    expect(channel.terminated).toBe(1);
    reply(channel, 3, [0, 1], true, 6);
    expect(got).toHaveLength(2);
    await expect(source.read(PLAN, ["tmp"], () => {}).done).rejects.toMatchObject({ reason: "failed" });
  });

  it("fails with the array that failed, and can be read again", async () => {
    const { source, channel } = await opened();
    const failed = source.read(PLAN, ["tmp", "orog"], () => {});
    channel.emit({ type: "error", requestId: channel.reads[1]!.requestId, message: "HTTP 503" });
    await expect(failed.done).rejects.toThrow("HTTP 503");
    // What is left of it is not read for nothing.
    expect(channel.posted.at(-1)).toEqual({ type: "supersede" });
    const short = source.read(PLAN, ["tmp"], () => {});
    reply(channel, 2, [0], false, 5);
    await expect(short.done).rejects.toThrow(/axis/);
    const again = source.read(PLAN, ["orog"], () => {});
    reply(channel, 3, [0, 1], true, 1);
    await again.done;
  });

  it("fails when its worker dies, and opens another", async () => {
    const { source, channels, channel } = await opened();
    const read = source.read(PLAN, ["tmp"], () => {});
    channel.onerror?.({ message: "gone" } as ErrorEvent);
    await expect(read.done).rejects.toThrow("gone");
    const opening = source.open();
    channels[1]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    await opening;
  });

  it("fails when nothing more is heard, and opens another channel", async () => {
    const { source, channels, channel } = await opened();
    vi.useFakeTimers();
    const read = source.read(PLAN, ["tmp"], () => {});
    const outcome = read.done.then(
      () => "done",
      (error: Error) => error.message,
    );
    await vi.advanceTimersByTimeAsync(40_000);
    // A word puts the end off.
    reply(channel, 0, [0], false, 6);
    await vi.advanceTimersByTimeAsync(40_000);
    expect(channel.terminated).toBe(0);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await outcome).toMatch(/stopped answering/);
    expect(channel.terminated).toBe(1);
    const opening = source.open();
    channels[1]!.emit({ type: "ready", blockJson: block(), blockShape: [8, 8] });
    await opening;
  });

  it("fails a read whose taker throws, and goes on", async () => {
    const { source, channel } = await opened();
    const read = source.read(PLAN, ["tmp"], () => {
      throw new Error("no room");
    });
    reply(channel, 0, [0], false, 6);
    await expect(read.done).rejects.toThrow("no room");
    const again = source.read(PLAN, ["orog"], () => {});
    reply(channel, 1, [0, 1], true, 1);
    await again.done;
  });

  it("knows whether the store open is the page's, published again or not", async () => {
    const { source, deps } = await opened();
    expect(source.current).toBe(true);
    // The manifest of the same run again, under another crc.
    deps.manifestUrl = MANIFEST_URL.replace("v=1", "v=2");
    deps.manifest = manifest();
    expect(source.current).toBe(true);
    deps.manifest = null;
    expect(source.current).toBe(false);
    deps.manifest = manifest({ columns: { ...COLUMNS, crc32: "0000beef" } } as Partial<ForecastManifest>);
    expect(source.current).toBe(false);
    source.end();
    deps.manifest = manifest();
    expect(source.current).toBe(false);
  });

  it("is done at once with nothing to read", async () => {
    const { source, channel } = await opened();
    await source.read(PLAN, ["rh"], () => {}).done;
    await source.read({ cells: new Uint32Array(0) } as unknown as SectionPlan, ["tmp"], () => {}).done;
    expect(channel.reads).toEqual([]);
  });
});
