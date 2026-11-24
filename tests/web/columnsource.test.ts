import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  askingOf,
  columnBundles,
  createColumnSource,
  readingFrom,
  type ColumnSeriesReply,
  type ColumnSourceDeps,
} from "../../web/src/columnsource";
import type { ForecastManifest } from "../../web/src/manifest";
import type { DecodeChannel, PointSession } from "../../web/src/session";
import { globalGrid, registryEntries, registryVariable } from "./helpers";

const RUN_TIME = "2026-08-15T06:00:00Z";
const MANIFEST_URL = "https://example.test/xue/gfs.2026081506/manifest.json?v=1";

function member(bundle: string, id: string): { bundle: string; variable: unknown } {
  const entry = registryEntries([id])[id]!;
  // The number is the bundle's own and means nothing in a column store.
  return { bundle, variable: { ...registryVariable(id, entry, entry.quality), numericId: 7 } };
}

/** Two levels of temperature and wind, and a one-frame terrain. */
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
      { id: "ugrd", levels: [850, 500], members: [member("wind850", "ugrd850"), member("wind500", "ugrd500")] },
      { id: "vgrd", levels: [850, 500], members: [member("wind850", "vgrd850"), member("wind500", "vgrd500")] },
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
  /** The column reads posted so far. */
  get reads(): Record<string, unknown>[] {
    return this.posted.filter((message) => message.type === "column");
  }
}

const COLUMNS = { path: "columns.zarr", byteLength: 10, crc32: "0000abcd" };

function manifest(overrides: Partial<ForecastManifest> = {}): ForecastManifest {
  return {
    runTime: RUN_TIME,
    bundles: ["tmp850", "tmp500", "wind850", "wind500", "orog", "prate"].map((variable) => ({ variable })),
    columns: COLUMNS,
    ...overrides,
  } as unknown as ForecastManifest;
}

/** A source over fake channels, with what it told its listener. */
function harness(overrides: Partial<ColumnSourceDeps> = {}) {
  const channels: FakeChannel[] = [];
  const replies: { session: PointSession; reply: ColumnSeriesReply }[] = [];
  let changes = 0;
  let key = 100;
  const source = createColumnSource({
    manifest: manifest(),
    manifestUrl: MANIFEST_URL,
    dataBackend: "zarr",
    nextKey: () => key++,
    streams: async () => true,
    openChannel: () => {
      const channel = new FakeChannel();
      channels.push(channel);
      return channel;
    },
    ...overrides,
  });
  source.listen({
    series: (session, reply) => replies.push({ session, reply }),
    changed: () => {
      changes += 1;
    },
  });
  return { source, channels, replies, changes: () => changes };
}

/** Let the promises of an open settle. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 30; turn += 1) await Promise.resolve();
}

async function opened(overrides: Partial<ColumnSourceDeps> = {}, blockJson = block()) {
  const held = harness(overrides);
  expect(held.source.awaiting("tmp850")).toBe(true);
  held.channels[0]!.emit({ type: "booted" });
  held.channels[0]!.emit({ type: "ready", blockJson });
  await settle();
  return held;
}

/** Ask a served bundle for its first variable's series. */
function ask(session: PointSession, requestId: number, variable = 0, column = 3, row = 2): void {
  session.worker.postMessage({
    type: "series",
    requestId,
    generation: 5,
    variableId: session.variables[variable]!.numericId,
    column,
    row,
  });
}

/** Three frames of two levels. */
const COLUMN = Uint8Array.from([1, 2, 3, 4, 5, 6]);

describe("the bundles a block describes", () => {
  it("numbers a bundle's variables itself and gives an array its own axis", () => {
    const bundles = columnBundles(block());
    expect([...bundles.keys()]).toEqual(["tmp850", "tmp500", "wind850", "wind500", "orog"]);
    expect(bundles.get("tmp500")!.metadata.variables.map((variable) => variable.numericId)).toEqual([1]);
    expect(bundles.get("tmp500")!.members.get(1)).toEqual({ array: "tmp", level: 1, levels: 2 });
    const wind = bundles.get("wind850")!;
    expect(wind.identity.vector).toBe(true);
    expect(wind.variables.map((variable) => variable.id)).toEqual(["ugrd850", "vgrd850"]);
    expect(wind.variables.map((variable) => wind.members.get(variable.numericId)!.array)).toEqual(["ugrd", "vgrd"]);
    expect(bundles.get("orog")!.metadata.time.frameCount).toBe(1);
    expect(bundles.get("tmp850")!.metadata.time.frameCount).toBe(3);
  });

  it("leaves out half a wind pair, a pair on two axes and a bundle whose name is another chart's", () => {
    const own = { unitSeconds: 3600, firstFrameOffset: 1, frameCount: 2, frameStep: 1 };
    const arrays = [
      { id: "ugrd", levels: [850, 500], members: [member("wind850", "ugrd850"), member("wind500", "ugrd500")] },
      { id: "vgrd", time: own, levels: [850], members: [member("wind850", "vgrd850")] },
      { id: "tmp", levels: [850], members: [member("rh850", "tmp850")] },
      { id: "orog", members: [member("orog", "orog")] },
    ];
    expect([...columnBundles(block({ arrays })).keys()]).toEqual(["orog"]);
  });
});

describe("the column source", () => {
  beforeEach(() => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("opens the run's store once, under its crc, and says when it knows", async () => {
    const { source, channels, changes } = await opened();
    expect(channels).toHaveLength(1);
    expect(channels[0]!.posted[0]).toEqual({
      type: "init-columns",
      url: "https://example.test/xue/gfs.2026081506/columns.zarr",
      crc32: "0000abcd",
    });
    expect(changes()).toBe(1);
    expect(source.awaiting("tmp850")).toBe(false);
    expect(channels).toHaveLength(1);
    expect(source.session("tmp850")?.metadata.runTime).toBe(RUN_TIME);
    expect(source.session("prate")).toBeUndefined();
    // Keys are the registry's: no two bundles share one.
    expect(source.session("tmp850")!.key).not.toBe(source.session("tmp500")!.key);
  });

  it("reads an array once for every level asked of it, and cuts each its own", async () => {
    const { source, channels, replies } = await opened();
    ask(source.session("tmp850")!, 1);
    ask(source.session("tmp500")!, 2);
    ask(source.session("tmp500")!, 3, 0, 4);
    expect(channels[0]!.reads.map(({ array, row, column }) => [array, row, column])).toEqual([
      ["tmp", 2, 3],
      ["tmp", 2, 4],
    ]);
    channels[0]!.emit({ type: "column", requestId: channels[0]!.reads[0]!.requestId, buffer: COLUMN.slice().buffer });
    expect(replies.map(({ session, reply }) => [session.id, reply.column, reply.row, [...new Uint8Array(reply.buffer)]])).toEqual([
      ["tmp850", 3, 2, [1, 3, 5]],
      ["tmp500", 3, 2, [2, 4, 6]],
    ]);
    // The panel keeps its books by the request's own id.
    expect(replies.map(({ reply }) => reply.requestId)).toEqual([1, 2]);
    expect(replies[0]!.reply.generation).toBe(5);
  });

  it("withdraws the bundles of a read that failed, so their sessions open", async () => {
    const { source, channels, replies, changes } = await opened();
    ask(source.session("tmp850")!, 1);
    ask(source.session("tmp500")!, 2);
    channels[0]!.emit({ type: "error", requestId: channels[0]!.reads[0]!.requestId, message: "HTTP 404" });
    expect(source.session("tmp850")).toBeUndefined();
    expect(source.session("tmp500")).toBeUndefined();
    expect(source.session("wind850")).toBeDefined();
    expect(replies).toEqual([]);
    expect(changes()).toBe(2);
  });

  it("withdraws a bundle whose column is not its axis's", async () => {
    const { source, channels, replies } = await opened();
    ask(source.session("tmp850")!, 1);
    channels[0]!.emit({ type: "column", requestId: channels[0]!.reads[0]!.requestId, buffer: new ArrayBuffer(4) });
    expect(source.session("tmp850")).toBeUndefined();
    expect(replies).toEqual([]);
  });

  it("withdraws everything when its worker dies", async () => {
    const { source, channels } = await opened();
    channels[0]!.onerror?.({ message: "gone" } as ErrorEvent);
    expect(source.session("tmp850")).toBeUndefined();
    expect(source.session("orog")).toBeUndefined();
  });

  it("lets the old pin's reads go, without failing the new pin's", async () => {
    const { source, channels, replies, changes } = await opened();
    ask(source.session("tmp850")!, 1);
    source.supersede();
    expect(channels[0]!.posted.at(-1)).toEqual({ type: "supersede" });
    ask(source.session("tmp850")!, 2);
    const [old, now] = channels[0]!.reads;
    channels[0]!.emit({ type: "error", requestId: old!.requestId, message: "too late" });
    expect(source.session("tmp850")).toBeDefined();
    channels[0]!.emit({ type: "column", requestId: now!.requestId, buffer: COLUMN.slice().buffer });
    expect(replies).toHaveLength(1);
    expect(changes()).toBe(1);
  });

  it("serves nothing, at once, without a store, before a run and on the container path", () => {
    const { columns: _, ...bare } = manifest();
    for (const overrides of [{ manifest: bare as ForecastManifest }, { manifest: null }, { dataBackend: "xue" as const }]) {
      const { source, channels } = harness(overrides);
      expect(source.awaiting("tmp850")).toBe(false);
      expect(channels).toHaveLength(0);
      expect(source.session("tmp850")).toBeUndefined();
    }
  });

  it("serves nothing where the group does not open, the origin has no ranges or the run is another", async () => {
    const refused = harness();
    refused.source.awaiting("tmp850");
    refused.channels[0]!.emit({ type: "error", message: "store object zarr.json failed (HTTP 404)" });
    await settle();
    const other = await opened({}, block({ runTime: "2026-08-15T12:00:00Z" }));
    const whole = await opened({ streams: async () => false });
    for (const { source, channels, changes } of [refused, other, whole]) {
      expect(source.awaiting("tmp850")).toBe(false);
      expect(source.session("tmp850")).toBeUndefined();
      expect(channels[0]!.terminated).toBe(1);
      // The panel is told, or the pin would wait for good.
      expect(changes()).toBe(1);
    }
  });

  it.each([
    ["a group", {}],
    ["a range probe", { streams: () => new Promise<boolean>(() => {}) }],
  ])("stops waiting for %s that never comes", async (_, overrides) => {
    vi.useFakeTimers();
    const { source, channels, changes } = harness(overrides);
    source.awaiting("tmp850");
    if ("streams" in overrides) channels[0]!.emit({ type: "ready", blockJson: block() });
    expect(source.awaiting("tmp850")).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(source.awaiting("tmp850")).toBe(false);
    expect(changes()).toBe(1);
    expect(source.session("tmp850")).toBeUndefined();
  });

  it("ends with the run and answers nothing of it afterwards", async () => {
    const { source, channels, replies } = await opened();
    ask(source.session("tmp850")!, 1);
    source.end();
    expect(channels[0]!.terminated).toBe(1);
    channels[0]!.emit({ type: "column", requestId: channels[0]!.reads[0]!.requestId, buffer: COLUMN.slice().buffer });
    expect(replies).toEqual([]);
    expect(source.session("tmp850")).toBeUndefined();
    // Another run is asked afresh.
    expect(source.awaiting("tmp850")).toBe(true);
    expect(channels).toHaveLength(2);
  });

  it("waits for nothing where only the borrowed bundles have a store", () => {
    const { columns: _, ...bare } = manifest({ borrowed: { columns: COLUMNS, bundles: ["tmp500"] } });
    const { source, channels } = harness({ manifest: bare as ForecastManifest });
    expect(source.awaiting("tmp850")).toBe(false);
    expect(source.awaiting("tmp500")).toBe(true);
    expect(channels).toHaveLength(1);
  });

  it("reads a lender's store for the borrowed bundles alone, and its own for the rest", async () => {
    const lent = "https://example.test/xue/gfs.2026081506/columns.zarr";
    const { source, channels } = harness({
      manifest: manifest({
        borrowed: { columns: { ...COLUMNS, path: lent, crc32: "0000beef" }, bundles: ["tmp500"] },
      }),
      manifestUrl: "https://example.test/xue/sflux.2026081506/manifest.json",
    });
    expect(source.awaiting("tmp500")).toBe(true);
    expect(channels.map((channel) => [channel.posted[0]!.url, channel.posted[0]!.crc32])).toEqual([
      ["https://example.test/xue/sflux.2026081506/columns.zarr", "0000abcd"],
      [lent, "0000beef"],
    ]);
    channels[1]!.emit({ type: "ready", blockJson: block() });
    await settle();
    // Each bundle waits for the one store that may serve it.
    expect(source.awaiting("tmp500")).toBe(false);
    expect(source.awaiting("tmp850")).toBe(true);
    channels[0]!.emit({ type: "ready", blockJson: block() });
    await settle();
    ask(source.session("tmp850")!, 1);
    ask(source.session("tmp500")!, 2);
    expect(channels.map((channel) => channel.reads.length)).toEqual([1, 1]);
  });
});

describe("what a pinned bundle is read through", () => {
  const column = { id: "tmp850", key: 1 } as PointSession;
  const session = { id: "tmp850", key: 2 } as PointSession;

  const yes = () => true;
  const no = () => false;

  it("reads a layer on screen once it holds its series, then the store, then whatever is open", () => {
    expect(readingFrom(column, session, yes, yes)).toBe(session);
    // A layer that has read nothing yet does not displace the store's series.
    expect(readingFrom(column, session, no, yes)).toBe(column);
    expect(readingFrom(column, session, no, no)).toBe(session);
    expect(readingFrom(column, undefined, yes, no)).toBe(column);
    expect(readingFrom(undefined, session, no, yes)).toBe(session);
    expect(readingFrom(undefined, undefined, yes, yes)).toBeUndefined();
  });

  it("asks the layer on screen, then the store, then another open session, and opens one for neither", () => {
    expect(askingOf(column, session, () => true)).toEqual({ session });
    expect(askingOf(column, session, () => false)).toEqual({ column });
    expect(askingOf(column, undefined, () => false)).toEqual({ column });
    expect(askingOf(undefined, session, () => false)).toEqual({ session });
    expect(askingOf(undefined, undefined, () => false)).toBeNull();
  });
});
