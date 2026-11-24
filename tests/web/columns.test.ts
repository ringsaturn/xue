/** The column store session against the committed fixture
 * (tests/prepare_column_golden.py), read through a fetch over the directory. */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { beforeAll, describe, expect, it, vi } from "vitest";

import { columnBundles, levelSeries } from "../../web/src/columnsource";
import { ColumnSession, type ColumnCodec } from "../../web/src/zarr/columns";
import { ZarrStore, type FetchLike } from "../../web/src/zarr/store";
import { localFetch, newFetchLog, type LocalFetchLog } from "../../web/tooling/localfetch";

function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

const ROOT = join(repositoryRoot(), "tests/fixtures/column");

interface Expected {
  cells: { row: number; column: number; arrays: Record<string, number[][]> }[];
}

const expected = JSON.parse(readFileSync(join(ROOT, "expected.json"), "utf8")) as Expected;
/** The arrays of the fixture, as its cells list them. */
const ARRAYS = Object.keys(expected.cells[0]!.arrays);

let codec: ColumnCodec;

beforeAll(async () => {
  const wasm = await import("../../web/src/wasm/xue");
  await wasm.default({ module_or_path: readFileSync(join(repositoryRoot(), "web/src/wasm/xue_bg.wasm")) });
  codec = wasm;
});

async function open(
  options: { fetch?: FetchLike; log?: LocalFetchLog; heldLimit?: number; codec?: ColumnCodec } = {},
): Promise<ColumnSession> {
  const fetch = options.fetch ?? localFetch(ROOT, options.log ?? newFetchLog());
  return ColumnSession.open(
    new ZarrStore("local://columns.zarr", "deadbeef", { fetch, retryDelays: [] }),
    options.codec ?? codec,
    options.heldLimit,
  );
}

/** Lets a test hold the answers to the requests it picks. */
function gate(served: FetchLike, held: (url: string) => boolean): { fetch: FetchLike; release: () => void; urls: string[] } {
  const waiting: (() => void)[] = [];
  const urls: string[] = [];
  return {
    urls,
    fetch: async (url, init) => {
      urls.push(url);
      if (held(url)) await new Promise<void>((resolve) => waiting.push(resolve));
      return served(url, init);
    },
    release: () => {
      for (const resolve of waiting.splice(0)) resolve();
    },
  };
}

describe("a column store session", () => {
  it("reads every array of every expected cell", async () => {
    const session = await open();
    for (const cell of expected.cells) {
      await Promise.all(
        ARRAYS.map(async (array) => {
          const codes = await session.read(array, cell.row, cell.column);
          expect(Array.from(codes!), `${array} at ${cell.row},${cell.column}`).toEqual(cell.arrays[array]!.flat());
        }),
      );
    }
  });

  it("reads the group once, then an index and a chunk per array, all plain ranges", async () => {
    const log = newFetchLog();
    const session = await open({ log });
    expect(log.entries).toEqual([{ path: "columns.zarr/zarr.json", range: null }]);
    const cell = expected.cells[0]!;
    await Promise.all(ARRAYS.map((array) => session.read(array, cell.row, cell.column)));
    const reads = log.entries.slice(1);
    expect(reads.every((entry) => /^bytes=\d+-\d+$/.test(entry.range!))).toBe(true);
    for (const array of ARRAYS) {
      const own = reads.filter((entry) => entry.path.startsWith(`columns.zarr/${array}/`));
      // The index from the start of the shard; a chunk too, unless the
      // block was never written.
      expect(own[0]!.range).toMatch(/^bytes=0-/);
      expect(own.length).toBeLessThanOrEqual(2);
    }
  });

  it("holds indices and chunks, and folds concurrent reads of one chunk", async () => {
    const log = newFetchLog();
    const session = await open({ log });
    const { row, column } = expected.cells[0]!;
    const array = ARRAYS[0]!;
    const [first, second] = await Promise.all([session.read(array, row, column), session.read(array, row, column)]);
    expect(second).toEqual(first);
    const requests = log.requests;
    expect(await session.read(array, row, column)).toEqual(first);
    expect(log.requests).toBe(requests);
  });

  it("lets the oldest chunk go past its budget, and reads it again", async () => {
    const log = newFetchLog();
    // Room for nothing but the newest chunk.
    const session = await open({ log, heldLimit: 1 });
    const { row, column } = expected.cells[0]!;
    const [a, b] = ARRAYS as [string, string];
    const first = await session.read(a, row, column);
    await session.read(b, row, column);
    const requests = log.requests;
    expect(await session.read(a, row, column)).toEqual(first);
    expect(log.requests).toBe(requests + 1);
  });

  it("lets a superseded read go before its chunk, and keeps the index it fetched", async () => {
    const array = ARRAYS[0]!;
    const { row, column } = expected.cells[0]!;
    const gated = gate(localFetch(ROOT), (url) => url.includes(`/${array}/c/`));
    const session = await open({ fetch: gated.fetch });
    const dropped = session.read(array, row, column);
    // Until the index request is out.
    await new Promise((resolve) => setTimeout(resolve, 0));
    session.supersede();
    gated.release();
    expect(await dropped).toBeNull();
    const before = gated.urls.length;
    const again = session.read(array, row, column);
    await new Promise((resolve) => setTimeout(resolve, 0));
    gated.release();
    expect(Array.from((await again)!)).toEqual(expected.cells[0]!.arrays[array]!.flat());
    expect(gated.urls.length).toBe(before + 1);
  });

  it("fails a read the origin does not answer, and asks afresh the next time", async () => {
    const served = localFetch(ROOT);
    let down = false;
    const session = await open({ fetch: (url, init) => (down ? Promise.reject(new TypeError("Failed to fetch")) : served(url, init)) });
    const { row, column } = expected.cells[0]!;
    down = true;
    await expect(session.read(ARRAYS[0]!, row, column)).rejects.toThrow(/Failed to fetch/);
    down = false;
    expect(await session.read(ARRAYS[0]!, row, column)).not.toBeNull();
  });

  it("refuses an index that does not verify, and a store that is not one", async () => {
    const served = localFetch(ROOT);
    const corrupt: FetchLike = async (url, init) => {
      const response = await served(url, init);
      if (!url.includes("/c/")) return response;
      const bytes = new Uint8Array(await response.arrayBuffer());
      bytes[0] = bytes[0]! ^ 0xff;
      return new Response(bytes, { status: response.status });
    };
    const session = await open({ fetch: corrupt });
    const { row, column } = expected.cells[0]!;
    await expect(session.read(ARRAYS[0]!, row, column)).rejects.toThrow(/CRC-32C/);
    const map: FetchLike = async () => new Response(JSON.stringify({ zarr_format: 3, node_type: "group", attributes: {} }));
    await expect(open({ fetch: map })).rejects.toThrow();
  });

  it("answers nothing once disposed, and frees its reader when the last read is back", async () => {
    const array = ARRAYS[0]!;
    const { row, column } = expected.cells[0]!;
    const gated = gate(localFetch(ROOT), (url) => url.includes("/c/"));
    let freed: ReturnType<typeof vi.spyOn> | null = null;
    const watched: ColumnCodec = {
      WasmColumnStore: {
        fromGroup: (text) => {
          const reader = codec.WasmColumnStore.fromGroup(text);
          freed = vi.spyOn(reader, "free");
          return reader;
        },
      },
    };
    const session = await open({ fetch: gated.fetch, codec: watched });
    const running = session.read(array, row, column);
    // Until the index request is out.
    await new Promise((resolve) => setTimeout(resolve, 0));
    session.dispose();
    // The read on its way would come back to a freed reader.
    expect(freed).not.toHaveBeenCalled();
    gated.release();
    expect(await running).toBeNull();
    expect(freed).toHaveBeenCalledTimes(1);
    expect(await session.read(array, row, column)).toBeNull();
    session.dispose();
    expect(freed).toHaveBeenCalledTimes(1);
  });
});

describe("the bundles of a store", () => {
  it("gives every bundle its members' codes, on its own axis and codebook", async () => {
    const session = await open();
    const bundles = columnBundles(session.blockJson);
    const block = JSON.parse(session.blockJson) as {
      arrays: { id: string; levels?: number[]; members: { bundle: string; variable: { id: string; quantization: unknown } }[] }[];
    };
    expect(bundles.size).toBeGreaterThan(0);
    const cell = expected.cells[0]!;
    for (const array of block.arrays) {
      const codes = (await session.read(array.id, cell.row, cell.column))!;
      for (const [level, member] of array.members.entries()) {
        const bundle = bundles.get(member.bundle);
        expect(bundle, member.bundle).toBeDefined();
        const variable = bundle!.metadata.variables.find((item) => item.id === member.variable.id)!;
        expect(variable.quantization).toEqual(member.variable.quantization);
        const place = bundle!.members.get(variable.numericId)!;
        expect(place).toEqual({ array: array.id, level, levels: array.members.length });
        const series = levelSeries(codes, place);
        expect(series.length).toBe(bundle!.metadata.time.frameCount);
        expect(Array.from(series)).toEqual(cell.arrays[array.id]!.map((frame) => frame[level]));
      }
    }
  });
});
