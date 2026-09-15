/**
 * The Zarr channel against the container.
 *
 * The pure parts — the shard index, the range coalescer, the metadata
 * validators — are tested on bytes built here. The assembly is tested
 * against the synthetic fixture run: `tests/prepare_web_fixture.py` writes
 * every bundle as a `.xue` and, for three of them, the Zarr store derived
 * from it (`tmp2m.zarr`, `prate.zarr` for a RAW variable, `wind10m.zarr`
 * for a two-variable bundle, plus `tmp2m.delta.zarr` — the delta chain with
 * its index at the start of each shard — which the manifest never names).
 * Every frame and series the session assembles is compared byte for byte
 * to what the WASM `.xue` decoder gives for the same bundle, through the
 * same `decodeChunk` the worker uses, over a fetch that reads the fixture
 * directory with byte ranges. The whole-store index (`index.bin`) is walked
 * both ways: seeded, where a series costs one request per time chunk, and
 * missing or corrupt, where the per-shard index read takes over.
 */

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import { viewportTileRects, type TileRect } from "../../web/src/tiles";
import {
  crc32c,
  INDEX_ENTRY_BYTES,
  parseArrayMetadata,
  parseGroupMetadata,
  parseShardIndex,
  parseStoreIndexDescriptor,
  shardIndexLength,
  storeIndexLength,
  tileOf,
  tileShape,
} from "../../web/src/zarr/shard";
import { ZarrSession } from "../../web/src/zarr/session";
import { coalesceRanges, ZarrStore, type FetchLike } from "../../web/src/zarr/store";
import { localFetch, newFetchLog } from "../../web/tooling/localfetch";
import { parseBundleMetadata } from "../../web/src/manifest";

/** Under jsdom `import.meta.url` is not a file URL, so the repository is
 * found from the working directory: the directory holding pyproject.toml. */
function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

const REPOSITORY_ROOT = repositoryRoot();
const FIXTURE_ROOT = join(REPOSITORY_ROOT, "tests/fixtures/generated/web");
const WASM_DIR = join(REPOSITORY_ROOT, "web/src/wasm/");

type Wasm = typeof import("../../web/src/wasm/xue");
let wasm: Wasm;

/** The fixtures are generated (and gitignored); a run of this file on a
 * fresh checkout builds them the way the Playwright global setup does. */
function ensureFixtures(): void {
  if (existsSync(`${FIXTURE_ROOT}/tmp2m.delta.zarr/zarr.json`) && existsSync(`${FIXTURE_ROOT}/wind10m.zarr/zarr.json`)) return;
  const python = process.env.PYTHON || ".venv/bin/python";
  const result = spawnSync(python, ["tests/prepare_web_fixture.py"], { cwd: REPOSITORY_ROOT, encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`fixture generation failed (${python}): ${result.error?.message ?? result.stderr ?? result.stdout}`);
  }
}

beforeAll(async () => {
  ensureFixtures();
  wasm = await import("../../web/src/wasm/xue");
  await wasm.default({ module_or_path: readFileSync(`${WASM_DIR}xue_bg.wasm`) });
});

// -- pure parts ----------------------------------------------------------------

describe("crc32c", () => {
  it("is Castagnoli, not the container's IEEE polynomial", () => {
    expect(crc32c(new TextEncoder().encode("123456789"))).toBe(0xe3069283);
    expect(crc32c(new Uint8Array(0))).toBe(0);
  });
});

/** A shard index for the given entries, `null` for a chunk never written. */
function buildIndex(entries: ({ offset: number; length: number } | null)[]): Uint8Array {
  const bytes = new Uint8Array(shardIndexLength(entries.length));
  const view = new DataView(bytes.buffer);
  entries.forEach((entry, position) => {
    const offset = entry ? BigInt(entry.offset) : 0xffffffffffffffffn;
    const length = entry ? BigInt(entry.length) : 0xffffffffffffffffn;
    view.setBigUint64(position * INDEX_ENTRY_BYTES, offset, true);
    view.setBigUint64(position * INDEX_ENTRY_BYTES + 8, length, true);
  });
  view.setUint32(entries.length * INDEX_ENTRY_BYTES, crc32c(bytes.subarray(0, entries.length * INDEX_ENTRY_BYTES)), true);
  return bytes;
}

describe("parseShardIndex", () => {
  const entries = [{ offset: 0, length: 40 }, null, { offset: 40, length: 7 }];

  it("reads the pairs and the missing-chunk sentinel", () => {
    expect(parseShardIndex(buildIndex(entries), 3)).toEqual(entries);
  });

  it("verifies the CRC-32C", () => {
    const corrupt = buildIndex(entries);
    corrupt[3] = corrupt[3]! ^ 0x01;
    expect(() => parseShardIndex(corrupt, 3)).toThrow(/CRC-32C/);
  });

  it("rejects an index of the wrong length", () => {
    expect(() => parseShardIndex(buildIndex(entries), 2)).toThrow(/bytes/);
    expect(() => parseShardIndex(buildIndex(entries).subarray(1), 3)).toThrow(/bytes/);
  });

  it("rejects an entry naming an empty chunk", () => {
    expect(() => parseShardIndex(buildIndex([{ offset: 0, length: 0 }]), 1)).toThrow(/empty/);
  });
});

describe("coalesceRanges", () => {
  it("merges adjacent ranges and keeps the members", () => {
    const runs = coalesceRanges([
      { offset: 100, length: 50 },
      { offset: 0, length: 100 },
      { offset: 150, length: 10 },
    ]);
    expect(runs).toEqual([{ offset: 0, length: 160, members: [1, 0, 2] }]);
  });

  it("merges across a gap up to the threshold and not beyond", () => {
    const near = coalesceRanges([{ offset: 0, length: 10 }, { offset: 20, length: 10 }], 10);
    expect(near).toHaveLength(1);
    expect(near[0]).toMatchObject({ offset: 0, length: 30 });
    const far = coalesceRanges([{ offset: 0, length: 10 }, { offset: 21, length: 10 }], 10);
    expect(far).toHaveLength(2);
  });

  it("merges overlapping ranges", () => {
    expect(coalesceRanges([{ offset: 0, length: 100 }, { offset: 50, length: 100 }], 0)).toEqual([
      { offset: 0, length: 150, members: [0, 1] },
    ]);
  });

  it("is switched off by a negative gap", () => {
    expect(coalesceRanges([{ offset: 0, length: 10 }, { offset: 10, length: 10 }], -1)).toHaveLength(2);
  });
});

function arrayMetadata(overrides: (document: Record<string, unknown>) => void = () => {}): string {
  const document: Record<string, unknown> = {
    zarr_format: 3,
    node_type: "array",
    shape: [121, 73, 144],
    data_type: "uint8",
    chunk_grid: { name: "regular", configuration: { chunk_shape: [6, 80, 144] } },
    chunk_key_encoding: { name: "default" },
    fill_value: 255,
    codecs: [
      {
        name: "sharding_indexed",
        configuration: {
          chunk_shape: [6, 16, 16],
          codecs: [{ name: "bytes" }, { name: "zstd", configuration: { level: 15, checksum: true } }],
          index_codecs: [{ name: "bytes", configuration: { endian: "little" } }, { name: "crc32c" }],
          index_location: "end",
        },
      },
    ],
    attributes: {},
    dimension_names: ["time", "latitude", "longitude"],
  };
  overrides(document);
  return JSON.stringify(document);
}

function shardingConfiguration(document: Record<string, unknown>): Record<string, unknown> {
  return (document.codecs as { configuration: Record<string, unknown> }[])[0]!.configuration;
}

describe("parseArrayMetadata", () => {
  it("reads the profile's layout", () => {
    const layout = parseArrayMetadata(arrayMetadata());
    expect(layout).toMatchObject({
      frameCount: 121,
      height: 73,
      width: 144,
      timeChunk: 6,
      tileHeight: 16,
      tileWidth: 16,
      tileRows: 5,
      tileColumns: 9,
      tileCount: 45,
      timeChunks: 21,
      delta: false,
      indexLocation: "end",
      fillValue: 255,
    });
    expect(tileShape(layout, 44)).toEqual({ height: 9, width: 16 });
    expect(tileOf(layout, 72, 143)).toBe(44);
  });

  it("accepts the delta chain and an index at the start", () => {
    const layout = parseArrayMetadata(
      arrayMetadata((document) => {
        const sharding = shardingConfiguration(document);
        sharding.codecs = [{ name: "xue.delta", configuration: { axis: 0 } }, { name: "bytes" }, { name: "zstd" }];
        sharding.index_location = "start";
      }),
    );
    expect(layout.delta).toBe(true);
    expect(layout.indexLocation).toBe("start");
  });

  it("rejects a data type other than uint8", () => {
    expect(() => parseArrayMetadata(arrayMetadata((document) => (document.data_type = "float32")))).toThrow(/uint8/);
  });

  it("rejects an array that is not sharded", () => {
    expect(() =>
      parseArrayMetadata(arrayMetadata((document) => (document.codecs = [{ name: "bytes" }, { name: "zstd" }]))),
    ).toThrow(/sharding_indexed/);
  });

  it("rejects an inner chain this profile does not read", () => {
    for (const chain of [
      [{ name: "bytes" }],
      [{ name: "numcodecs.delta" }, { name: "bytes" }, { name: "zstd" }],
      [{ name: "bytes" }, { name: "blosc" }],
      [{ name: "xue.delta", configuration: { axis: 1 } }, { name: "bytes" }, { name: "zstd" }],
    ]) {
      expect(() => parseArrayMetadata(arrayMetadata((document) => (shardingConfiguration(document).codecs = chain)))).toThrow();
    }
  });

  it("rejects a wrong index chain, location or outer chunk", () => {
    expect(() =>
      parseArrayMetadata(arrayMetadata((document) => (shardingConfiguration(document).index_codecs = [{ name: "bytes" }]))),
    ).toThrow(/index codecs/);
    expect(() =>
      parseArrayMetadata(arrayMetadata((document) => (shardingConfiguration(document).index_location = "middle"))),
    ).toThrow(/index location/);
    expect(() =>
      parseArrayMetadata(
        arrayMetadata((document) => ((document.chunk_grid as { configuration: { chunk_shape: number[] } }).configuration.chunk_shape = [6, 73, 144])),
      ),
    ).toThrow(/whole tiles/);
  });
});

describe("parseGroupMetadata", () => {
  it("passes attributes.xue through parseBundleMetadata", () => {
    const text = readFileSync(`${FIXTURE_ROOT}/tmp2m.zarr/zarr.json`, "utf8");
    const group = parseGroupMetadata(text);
    expect(group.profile).toBe(1);
    expect(group.metadata.variables[0]!.id).toBe("tmp2m");
    expect(group.metadata).toEqual(parseBundleMetadata(JSON.stringify(JSON.parse(text).attributes.xue)));
  });

  it("rejects a group without the block, or with a broken one", () => {
    expect(() => parseGroupMetadata(JSON.stringify({ zarr_format: 3, node_type: "group", attributes: {} }))).toThrow(/xue/);
    expect(() =>
      parseGroupMetadata(
        JSON.stringify({ zarr_format: 3, node_type: "group", attributes: { xue: { schemaVersion: 9 }, xue_profile: 1 } }),
      ),
    ).toThrow();
  });

  it("reads the whole-store index descriptor, and does without one", () => {
    const text = readFileSync(`${FIXTURE_ROOT}/tmp2m.zarr/zarr.json`, "utf8");
    const group = parseGroupMetadata(text);
    expect(group.index).toEqual({ path: "index.bin", shardIndexBytes: 724, timeChunks: 21, arrays: ["tmp2m"] });
    expect(storeIndexLength(group.index!)).toBe(21 * 724);
    const document = JSON.parse(text);
    delete document.attributes.xue_index;
    expect(parseGroupMetadata(JSON.stringify(document)).index).toBeUndefined();
  });

  it("rejects a malformed whole-store index descriptor", () => {
    const sound = { path: "index.bin", shardIndexBytes: 724, timeChunks: 21, arrays: ["tmp2m"] };
    expect(parseStoreIndexDescriptor(sound)).toEqual(sound);
    for (const broken of [
      { ...sound, path: "" },
      { ...sound, path: "tmp2m/index.bin" },
      { ...sound, shardIndexBytes: "724" },
      { ...sound, shardIndexBytes: 725 },
      { ...sound, shardIndexBytes: 4 },
      { ...sound, timeChunks: 0 },
      { ...sound, arrays: [] },
      { ...sound, arrays: ["tmp2m", "tmp2m"] },
      { ...sound, arrays: [1] },
      "index.bin",
    ]) {
      expect(() => parseStoreIndexDescriptor(broken)).toThrow(/xue_index/);
    }
  });
});

// -- the store over a directory -------------------------------------------------

function openStore(name: string, gap?: number) {
  const log = newFetchLog();
  const store = new ZarrStore(`local://${name}`, "deadbeef", { fetch: localFetch(FIXTURE_ROOT, log), gap });
  return { store, log };
}

describe("ZarrStore", () => {
  it("appends the store's ?v= to every object", () => {
    const { store } = openStore("tmp2m.zarr");
    expect(store.url("tmp2m/c/0/0/0")).toBe("local://tmp2m.zarr/tmp2m/c/0/0/0?v=deadbeef");
  });

  it("merges the ranges of one turn into one request and splits them back", async () => {
    const { store, log } = openStore("tmp2m.zarr");
    const whole = readFileSync(`${FIXTURE_ROOT}/tmp2m.zarr/zarr.json`);
    const [a, b, c] = await Promise.all([
      store.getRange("zarr.json", { offset: 40, length: 10 }),
      store.getRange("zarr.json", { offset: 0, length: 20 }),
      store.getRange("zarr.json", { offset: 25, length: 5 }),
    ]);
    expect(log.requests).toBe(1);
    expect(log.entries[0]!.range).toBe("bytes=0-49");
    expect(store.stats).toEqual({ requests: 1, ranges: 3, bytes: 50 });
    expect(Buffer.from(a)).toEqual(whole.subarray(40, 50));
    expect(Buffer.from(b)).toEqual(whole.subarray(0, 20));
    expect(Buffer.from(c)).toEqual(whole.subarray(25, 30));
  });

  it("leaves ranges apart beyond the gap, and separate turns separate", async () => {
    const { store, log } = openStore("tmp2m.zarr", 4);
    await Promise.all([store.getRange("zarr.json", { offset: 0, length: 4 }), store.getRange("zarr.json", { offset: 9, length: 4 })]);
    expect(log.requests).toBe(2);
    await store.getRange("zarr.json", { offset: 0, length: 4 });
    await store.getRange("zarr.json", { offset: 4, length: 4 });
    expect(log.requests).toBe(4);
  });

  it("reads a suffix without knowing the length", async () => {
    const { store } = openStore("tmp2m.zarr");
    const whole = readFileSync(`${FIXTURE_ROOT}/tmp2m.zarr/zarr.json`);
    const tail = await store.getRange("zarr.json", { suffixLength: 8 });
    expect(Buffer.from(tail)).toEqual(whole.subarray(whole.byteLength - 8));
  });

  it("refuses a missing object and a non-range answer", async () => {
    const { store } = openStore("tmp2m.zarr");
    await expect(store.get("nope/zarr.json")).rejects.toThrow(/HTTP 404/);
    await expect(store.getRange("nope/zarr.json", { offset: 0, length: 4 })).rejects.toThrow(/HTTP 404/);
  });
});

// -- assembly against the container ---------------------------------------------

interface Opened {
  session: ZarrSession;
  bundle: InstanceType<Wasm["WasmBundle"]>;
  log: ReturnType<typeof newFetchLog>;
}

async function open(storeName: string, bundleName: string, gap?: number): Promise<Opened> {
  const { store, log } = openStore(storeName, gap);
  const session = await ZarrSession.open(store, wasm.decodeChunk);
  const bundle = new wasm.WasmBundle(readFileSync(`${FIXTURE_ROOT}/${bundleName}`));
  return { session, bundle, log };
}

function region(plane: Uint8Array, width: number, rect: { row: number; column: number; height: number; width: number }): Uint8Array {
  const out = new Uint8Array(rect.height * rect.width);
  for (let row = 0; row < rect.height; row += 1) {
    out.set(plane.subarray((rect.row + row) * width + rect.column, (rect.row + row) * width + rect.column + rect.width), row * rect.width);
  }
  return out;
}

describe("ZarrSession", () => {
  it("opens the store as the bundle's metadata and tiling", async () => {
    const { session, bundle } = await open("tmp2m.zarr", "tmp2m.xue");
    expect(parseBundleMetadata(session.metadataJson)).toEqual(parseBundleMetadata(bundle.metadataJson()));
    expect(session.planeLength).toBe(bundle.planeLength());
    expect([...session.tileGeometry()]).toEqual([...bundle.tileGeometry()!]);
    expect(session.numericIds).toEqual([1]);
  });

  for (const [storeName, bundleName, variables] of [
    ["tmp2m.zarr", "tmp2m.xue", [1]],
    ["tmp2m.delta.zarr", "tmp2m.xue", [1]],
    ["prate.zarr", "prate.xue", [1]],
    ["wind10m.zarr", "wind10m.xue", [1, 2]],
    ["tmp2m.half.zarr", "tmp2m.half.xue", [1]],
  ] as const) {
    it(`assembles ${storeName} frames byte for byte as the container decodes ${bundleName}`, async () => {
      const { session, bundle } = await open(storeName, bundleName);
      // The chunk boundary at 6, the last frame of the axis in a padded
      // time chunk, and a frame in the middle of a chunk.
      for (const offset of [0, 5, 6, 7, 64, 120]) {
        for (const variable of variables) {
          const plane = await session.decodeFrame(variable, offset);
          expect(Buffer.from(plane)).toEqual(Buffer.from(bundle.decodeFrame(variable, offset)));
        }
      }
    });

    it(`reads ${storeName} series as the container does`, async () => {
      const { session, bundle } = await open(storeName, bundleName);
      const metadata = parseBundleMetadata(session.metadataJson);
      const { width, height } = metadata.grid;
      // The grid's corners, a cell inside a tile, and one on the clipped
      // last tile row — scaled, since the half tier's grid is smaller.
      const cells: [number, number][] = [
        [0, 0],
        [width - 1, height - 1],
        [Math.floor(width / 4) + 1, Math.floor(height / 4) + 2],
        [Math.floor(width / 8) + 1, height - 2],
      ];
      for (const [column, row] of cells) {
        for (const variable of variables) {
          const series = await session.decodeSeries(variable, column, row);
          expect(Buffer.from(series)).toEqual(Buffer.from(bundle.decodeSeries(variable, column, row)));
        }
      }
    });
  }

  it("decodes only the tiles asked for, with the same coverage the container gives", async () => {
    const { session, bundle } = await open("tmp2m.zarr", "tmp2m.xue");
    const metadata = parseBundleMetadata(session.metadataJson);
    const rects = viewportTileRects(metadata, { tileWidth: 16, tileHeight: 16, columns: 9, rows: 5 }, { west: -60, east: 30, south: -10, north: 40 }, 0);
    expect(rects).not.toBeNull();
    const plane = await session.decodeFrame(1, 9, rects);
    const reference = bundle.decodeFrame(1, 9);
    for (const rect of rects as TileRect[]) {
      const box = {
        row: rect.firstRow * 16,
        column: rect.firstColumn * 16,
        height: Math.min((rect.lastRow + 1) * 16, 73) - rect.firstRow * 16,
        width: Math.min((rect.lastColumn + 1) * 16, 144) - rect.firstColumn * 16,
      };
      expect(Buffer.from(region(plane, 144, box))).toEqual(Buffer.from(region(reference, 144, box)));
    }
    // Nothing outside was touched: the plane's own zeros are still there at
    // a corner no rectangle covers.
    expect(plane[0]).toBe(0);
  });

  /** A fetch over the fixture directory whose answer for `index.bin` is
   * rewritten, or refused, so the session's fallback paths can be walked. */
  function fetchWithIndex(
    log: ReturnType<typeof newFetchLog>,
    doctor: (bytes: Uint8Array<ArrayBuffer>) => Uint8Array<ArrayBuffer> | null,
  ): FetchLike {
    const base = localFetch(FIXTURE_ROOT, log);
    return async (url, init) => {
      const response = await base(url, init);
      if (!new URL(url).pathname.endsWith("/index.bin")) return response;
      const bytes = doctor(new Uint8Array(await response.arrayBuffer()));
      return bytes ? new Response(bytes, { status: 200 }) : new Response("missing", { status: 404 });
    };
  }

  /** Rewrite one block of `index.bin` in place and reseal its CRC-32C. */
  function rewriteBlock(bytes: Uint8Array<ArrayBuffer>, timeChunk: number, edit: (block: Uint8Array) => void): Uint8Array<ArrayBuffer> {
    const block = bytes.subarray(timeChunk * 724, (timeChunk + 1) * 724);
    edit(block);
    new DataView(block.buffer, block.byteOffset).setUint32(724 - 4, crc32c(block.subarray(0, 724 - 4)), true);
    return bytes;
  }

  it("fills a chunk the shard never held with the fill value", async () => {
    const { session } = await open("tmp2m.zarr", "tmp2m.xue");
    const metadata = parseBundleMetadata(session.metadataJson);
    // Rewrite one shard index entry to the sentinel in the session's own
    // cache by going through a store whose whole-store index is doctored:
    // the first tile's pair of time chunk 0 becomes all ones.
    const log = newFetchLog();
    const doctored = new ZarrStore("local://tmp2m.zarr", "deadbeef", {
      fetch: fetchWithIndex(log, (bytes) => rewriteBlock(bytes, 0, (block) => block.fill(0xff, 0, INDEX_ENTRY_BYTES))),
    });
    const doctoredSession = await ZarrSession.open(doctored, wasm.decodeChunk);
    expect(doctoredSession.storeIndex).toEqual({ bytes: 21 * 724, blocks: 21, seeded: 21 });
    const plane = await doctoredSession.decodeFrame(1, 0);
    const nodata = (metadata.variables[0]!.quantization as { nodataCode: number }).nodataCode;
    expect(plane[0]).toBe(nodata);
    expect(plane[15 * 144 + 15]).toBe(nodata);
    expect(plane[16 * 144 + 15]).not.toBe(nodata);
  });

  it("costs the requests the design predicts, before and after coalescing", async () => {
    const merged = await open("tmp2m.zarr", "tmp2m.xue");
    await merged.session.decodeFrame(1, 0);
    // The group and the array document, the whole-store index, and the
    // whole shard body in one merged range: no per-shard index read.
    expect(merged.log.requests).toBe(4);
    expect(merged.log.entries.map((entry) => entry.path)).toEqual([
      "tmp2m.zarr/zarr.json",
      "tmp2m.zarr/tmp2m/zarr.json",
      "tmp2m.zarr/index.bin",
      "tmp2m.zarr/tmp2m/c/0/0/0",
    ]);
    expect(merged.log.entries.some((entry) => entry.range?.startsWith("bytes=-"))).toBe(false);
    expect(merged.session.storeIndex).toEqual({ bytes: 21 * 724, blocks: 21, seeded: 21 });
    expect(merged.session.store.stats.ranges).toBe(45);
    // Six tiles of one row are one request; a 3 x 2 rectangle two rows on,
    // whose shard index the seed already holds.
    const before = merged.log.requests;
    await merged.session.decodeFrame(1, 6, [{ firstColumn: 2, firstRow: 1, lastColumn: 4, lastRow: 2 }]);
    expect(merged.log.requests - before).toBe(1);

    const separate = await open("tmp2m.zarr", "tmp2m.xue", -1);
    await separate.session.decodeFrame(1, 0);
    expect(separate.log.requests).toBe(3 + 45);

    // A series is opening plus one chunk per time chunk — exactly what the
    // container costs beyond its prefix — and nothing proportional to the
    // frame count.
    const series = await open("tmp2m.zarr", "tmp2m.xue");
    await series.session.decodeSeries(1, 37, 20);
    expect(series.log.requests).toBe(3 + 21);
    expect(series.log.entries.filter((entry) => entry.range !== null)).toHaveLength(21);
  });

  it("falls back to the per-shard index when index.bin is missing", async () => {
    const log = newFetchLog();
    const store = new ZarrStore("local://tmp2m.zarr", "deadbeef", { fetch: fetchWithIndex(log, () => null) });
    const session = await ZarrSession.open(store, wasm.decodeChunk);
    expect(session.storeIndex).toBeNull();
    const bundle = new wasm.WasmBundle(readFileSync(`${FIXTURE_ROOT}/tmp2m.xue`));
    // The failed fetch is one request; the shard index then comes as a
    // suffix range, and the body as one merged range.
    const plane = await session.decodeFrame(1, 0);
    expect(Buffer.from(plane)).toEqual(Buffer.from(bundle.decodeFrame(1, 0)));
    expect(log.requests).toBe(3 + 1 + 1);
    expect(log.entries[3]!.range).toBe("bytes=-724");
    // A series is back to an index and a chunk per time chunk (the first
    // time chunk's are already held from the plane).
    const series = await session.decodeSeries(1, 37, 20);
    expect(Buffer.from(series)).toEqual(Buffer.from(bundle.decodeSeries(1, 37, 20)));
    expect(log.requests).toBe(5 + 20 * 2);
  });

  it("falls back to the shard's own index for a block that fails to verify, and only that one", async () => {
    const log = newFetchLog();
    const store = new ZarrStore("local://tmp2m.zarr", "deadbeef", {
      fetch: fetchWithIndex(log, (bytes) => {
        bytes[3] = bytes[3]! ^ 0x01;
        return bytes;
      }),
    });
    const session = await ZarrSession.open(store, wasm.decodeChunk);
    expect(session.storeIndex).toEqual({ bytes: 21 * 724, blocks: 21, seeded: 20 });
    const bundle = new wasm.WasmBundle(readFileSync(`${FIXTURE_ROOT}/tmp2m.xue`));
    expect(Buffer.from(await session.decodeFrame(1, 0))).toEqual(Buffer.from(bundle.decodeFrame(1, 0)));
    expect(log.requests).toBe(3 + 1 + 1);
    expect(log.entries[3]!.range).toBe("bytes=-724");
    expect(Buffer.from(await session.decodeFrame(1, 6))).toEqual(Buffer.from(bundle.decodeFrame(1, 6)));
    expect(log.requests).toBe(5 + 1);
  });

  it("ignores an index.bin of the wrong length", async () => {
    const log = newFetchLog();
    const store = new ZarrStore("local://tmp2m.zarr", "deadbeef", { fetch: fetchWithIndex(log, (bytes) => bytes.subarray(1)) });
    const session = await ZarrSession.open(store, wasm.decodeChunk);
    expect(session.storeIndex).toBeNull();
    await session.decodeFrame(1, 0);
    expect(log.entries[3]!.range).toBe("bytes=-724");
  });

  it("refuses an index descriptor that does not describe the arrays", async () => {
    const base = localFetch(FIXTURE_ROOT);
    const withGroup = (edit: (index: Record<string, unknown>) => void): FetchLike => async (url, init) => {
      const response = await base(url, init);
      if (!new URL(url).pathname.endsWith("/zarr.json") || new URL(url).pathname !== "/zarr.json") return response;
      const document = JSON.parse(await response.text());
      edit(document.attributes.xue_index);
      return new Response(JSON.stringify(document), { status: 200 });
    };
    for (const [edit, message] of [
      [(index: Record<string, unknown>) => (index.shardIndexBytes = 724 + 16), /shardIndexBytes/],
      [(index: Record<string, unknown>) => (index.timeChunks = 20), /timeChunks/],
      [(index: Record<string, unknown>) => (index.arrays = ["prate"]), /cover/],
    ] as const) {
      const store = new ZarrStore("local://tmp2m.zarr", "deadbeef", { fetch: withGroup(edit) });
      await expect(ZarrSession.open(store, wasm.decodeChunk)).rejects.toThrow(message);
    }
  });

  it("refuses a cell or a frame outside the array", async () => {
    const { session } = await open("tmp2m.zarr", "tmp2m.xue");
    await expect(session.decodeSeries(1, 144, 0)).rejects.toThrow(/outside/);
    await expect(session.decodeFrame(1, 121)).rejects.toThrow(/no plane/);
    await expect(session.decodeFrame(2, 0)).rejects.toThrow(/no array/);
  });
});
