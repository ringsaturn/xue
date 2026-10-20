// @vitest-environment node
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

import {
  blendValues,
  codebookTable,
  framePair,
  NetworkStore,
  parseNetworkArrayMetadata,
  parseNetworkGroup,
  type DecodeChunkFn,
} from "../../web/src/river/store";
import { ZarrStore, type FetchLike } from "../../web/src/zarr/store";
import { buildNetworkStore, fixtureCode, LOG1P_CODEBOOK, type NetworkStoreFixture } from "./fixtures/river";

function repositoryRoot(): string {
  let directory = process.cwd();
  while (!existsSync(join(directory, "pyproject.toml"))) {
    const parent = dirname(directory);
    if (parent === directory) throw new Error("repository root not found from the working directory");
    directory = parent;
  }
  return directory;
}

let decodeChunk: DecodeChunkFn;

beforeAll(async () => {
  const wasm = await import("../../web/src/wasm/xue");
  await wasm.default({ module_or_path: readFileSync(join(repositoryRoot(), "web/src/wasm/xue_bg.wasm")) });
  decodeChunk = wasm.decodeChunk;
});

interface Served {
  fetch: FetchLike;
  requests: { path: string; range: string | null }[];
}

/** The fixture's files behind a range-capable origin at `mem://store/`. */
function serve(fixture: NetworkStoreFixture): Served {
  const requests: Served["requests"] = [];
  const fetch: FetchLike = async (url, init) => {
    const path = url.replace(/^mem:\/\/store\//, "").replace(/\?.*$/, "");
    const range = new Headers(init?.headers).get("Range");
    requests.push({ path, range });
    const body = fixture.files.get(path);
    if (!body) return new Response("missing", { status: 404 });
    if (range === null) return new Response(body.slice(), { status: 200 });
    const suffix = /^bytes=-(\d+)$/.exec(range);
    const span = /^bytes=(\d+)-(\d+)$/.exec(range);
    const start = suffix ? Math.max(0, body.byteLength - Number(suffix[1])) : Number(span![1]);
    const end = suffix ? body.byteLength - 1 : Math.min(Number(span![2]), body.byteLength - 1);
    return new Response(body.slice(start, end + 1), { status: 206 });
  };
  return { fetch, requests };
}

const FRAMES = 14;
const FEATURES = 168;
const ROWS = 64;

async function open(delta: boolean, missing: string[] = []) {
  const fixture = buildNetworkStore({ frameCount: FRAMES, featureCount: FEATURES, rowGroupRows: ROWS, delta, missing });
  const served = serve(fixture);
  const store = await NetworkStore.open(new ZarrStore("mem://store", fixture.crc32, { fetch: served.fetch }), decodeChunk);
  return { fixture, served, store };
}

describe("network store", () => {
  for (const delta of [true, false]) {
    it(`reads every frame ${delta ? "through the delta chain" : "under [bytes, zstd]"}`, async () => {
      const { store } = await open(delta);
      expect(store.layout).toMatchObject({ frameCount: FRAMES, featureCount: FEATURES, rowGroups: 3, timeChunks: 3, delta });
      for (let frame = 0; frame < FRAMES; frame += 1) {
        const codes = await store.readFrame(frame);
        expect(Array.from(codes)).toEqual(Array.from({ length: FEATURES }, (_, feature) => fixtureCode(frame, feature)));
      }
    });
  }

  it("reads the side arrays: valid times and the usual flow", async () => {
    const { fixture, store } = await open(true);
    expect(Array.from(store.times)).toEqual(fixture.times);
    expect(store.baseflow[1]).toBe(fixture.baseflow[1]);
    expect(Number.isNaN(store.baseflow[13]!)).toBe(true);
  });

  it("reads a frame as the shard index then one merged range", async () => {
    const { served, store } = await open(true);
    const before = served.requests.length;
    await store.readFrame(7);
    const reads = served.requests.slice(before);
    expect(reads.map((read) => read.path)).toEqual(["streamflow/c/0/0", "streamflow/c/0/0"]);
    expect(reads[0]!.range).toBe(`bytes=-${9 * 16 + 4}`);
    expect(reads[1]!.range).toMatch(/^bytes=\d+-\d+$/);
    // The same time chunk again is already decoded.
    await store.readFrame(8);
    expect(served.requests.length).toBe(before + 2);
  });

  it("reads only the row groups asked for, the rest no-data", async () => {
    const { store } = await open(true);
    const codes = await store.readFrame(3, [1]);
    expect(codes[0]).toBe(255);
    expect(codes[64]).toBe(fixtureCode(3, 64));
    expect(codes[127]).toBe(fixtureCode(3, 127));
    expect(codes[128]).toBe(255);
  });

  it("reads one reach's series, one chunk per time chunk", async () => {
    const { served, store } = await open(true);
    await store.readFrame(0, [0]);
    const before = served.requests.length;
    const series = await store.readSeries(150);
    expect(Array.from(series)).toEqual(Array.from({ length: FRAMES }, (_, frame) => fixtureCode(frame, 150)));
    // Three chunks of row group 2, at most three ranges, no index read.
    const reads = served.requests.slice(before);
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.length).toBeLessThanOrEqual(3);
    expect(reads.every((read) => read.range !== `bytes=-${9 * 16 + 4}`)).toBe(true);
  });

  it("reads a chunk the shard never held as no-data", async () => {
    const { store } = await open(true, ["2:2"]);
    const codes = await store.readFrame(13);
    expect(codes[100]).toBe(fixtureCode(13, 100));
    expect(codes[130]).toBe(255);
    const series = await store.readSeries(130);
    expect(series[11]).toBe(fixtureCode(11, 130));
    expect(series[12]).toBe(255);
  });

  it("dequantizes with the log1p codebook", async () => {
    const { store } = await open(true);
    const values = await store.readFrameValues(2);
    const code = fixtureCode(2, 10);
    expect(values[10]).toBeCloseTo(store.table[code]!, 6);
    expect(Number.isNaN(values[5]!)).toBe(true);
    const series = await store.readSeriesValues(10);
    expect(series[2]).toBeCloseTo(values[10]!, 6);
  });

  it("refuses a store that names another GeoParquet than the index", async () => {
    const fixture = buildNetworkStore({ frameCount: FRAMES, featureCount: FEATURES, rowGroupRows: ROWS, delta: true });
    const store = new ZarrStore("mem://store", fixture.crc32, { fetch: serve(fixture).fetch });
    await expect(
      NetworkStore.open(store, decodeChunk, { path: "reaches.parquet", byteLength: 1, crc32: "00000000", featureCount: FEATURES, rowGroupRows: 32 }),
    ).rejects.toThrow(/GeoParquet/);
  });
});

describe("network metadata", () => {
  const fixture = buildNetworkStore({ frameCount: FRAMES, featureCount: FEATURES, rowGroupRows: ROWS, delta: true });
  const groupText = new TextDecoder().decode(fixture.files.get("zarr.json"));
  const array = () => JSON.parse(new TextDecoder().decode(fixture.files.get("streamflow/zarr.json"))) as Record<string, any>;

  it("reads the group's block", () => {
    const group = parseNetworkGroup(groupText);
    expect(group.frameCount).toBe(FRAMES);
    expect(group.variable.quantization).toEqual(LOG1P_CODEBOOK);
    expect(group.consolidated).not.toBeNull();
  });

  it("refuses a grid group, two blocks, or another version", () => {
    const group = JSON.parse(groupText);
    expect(() => parseNetworkGroup(JSON.stringify({ ...group, attributes: { xue: {}, xue_profile: 1 } }))).toThrow(/network/);
    expect(() => parseNetworkGroup(JSON.stringify({ ...group, attributes: { ...group.attributes, xue_polar: {} } }))).toThrow(/network/);
    const later = structuredClone(group);
    later.attributes.xue_network.version = 2;
    expect(() => parseNetworkGroup(JSON.stringify(later))).toThrow(/version/);
  });

  it("reads the array and refuses what the profile does not name", () => {
    expect(parseNetworkArrayMetadata(array())).toMatchObject({ rowGroupRows: ROWS, shardFrames: 18, chunksPerShard: 9 });
    const wrongType = array();
    wrongType.data_type = "uint16";
    expect(() => parseNetworkArrayMetadata(wrongType)).toThrow(/uint8/);
    const threeD = array();
    threeD.shape = [FRAMES, 1, FEATURES];
    expect(() => parseNetworkArrayMetadata(threeD)).toThrow(/shape/);
    const otherChunk = array();
    otherChunk.codecs[0].configuration.chunk_shape = [12, ROWS];
    expect(() => parseNetworkArrayMetadata(otherChunk)).toThrow(/6 frames/);
    const otherCodec = array();
    otherCodec.codecs[0].configuration.codecs = [{ name: "bytes" }, { name: "gzip" }];
    expect(() => parseNetworkArrayMetadata(otherCodec)).toThrow(/codec chain/);
    const twoShards = array();
    twoShards.chunk_grid.configuration.chunk_shape = [6, 3 * ROWS];
    expect(() => parseNetworkArrayMetadata(twoShards)).toThrow(/one shard/);
    const partialFeatures = array();
    partialFeatures.chunk_grid.configuration.chunk_shape = [18, 2 * ROWS];
    expect(() => parseNetworkArrayMetadata(partialFeatures)).toThrow(/row group/);
  });
});

describe("the log1p codebook", () => {
  const table = codebookTable(LOG1P_CODEBOOK);
  it("reads the reserved codes", () => {
    expect(table[0]).toBe(0);
    expect(table[254]).toBe(100000);
    expect(Number.isNaN(table[255]!)).toBe(true);
  });
  it("spans trace to maximum, about 4.7 % a step", () => {
    expect(table[1]).toBeCloseTo(0.01, 6);
    expect(table[253]! / 100000).toBeCloseTo(1, 4);
    expect(table[200]! / table[199]!).toBeGreaterThan(1.04);
    expect(table[200]! / table[199]!).toBeLessThan(1.05);
  });
});

describe("following the playhead", () => {
  const times = [1000, 2000, 3000];
  it("draws nothing before the first frame", () => {
    expect(framePair(times, 999)).toBeNull();
    expect(framePair([], 5)).toBeNull();
  });
  it("takes the latest frame at or before the playhead and the fraction to the next", () => {
    expect(framePair(times, 1000)).toEqual({ index: 0, next: 1, weight: 0 });
    expect(framePair(times, 2500)).toEqual({ index: 1, next: 2, weight: 0.5 });
    expect(framePair(times, 3000)).toEqual({ index: 2, next: 2, weight: 0 });
    expect(framePair(times, 9000)).toEqual({ index: 2, next: 2, weight: 0 });
  });
  it("blends linearly, a reach without a value staying without", () => {
    const a = Float32Array.from([10, Number.NaN, 4]);
    const b = Float32Array.from([20, 5, Number.NaN]);
    const out = blendValues(a, b, 0.25);
    expect(out[0]).toBeCloseTo(12.5);
    expect(Number.isNaN(out[1]!)).toBe(true);
    expect(out[2]).toBe(4);
  });
});
