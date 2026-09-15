/**
 * Requests and bytes per backend, on a real run directory.
 *
 * Run with `npm run measure:backends` (optionally `XUE_RUN_DIR=<dir>`; the
 * default is `web/public/data/gfs.2026091000`, which needs the stores
 * exported beside its bundles with `xue export-zarr`). Not part of
 * `npm run test:web`: its own vitest config includes only this file, and it
 * skips when the directory or its stores are missing.
 *
 * For each of `tmp2m`, `prate` and `wind10m` it opens the `.xue` bundle
 * through the streaming WASM reader and the Zarr store through the session
 * the worker drives, over the same file-backed range fetch, and counts the
 * HTTP requests and bytes of three reads: (a) the first frame at the global
 * view, (b) the first frame of a 6 × 4 tile viewport, and (c) one point's
 * whole series. The Zarr side is measured twice, with the store's range
 * coalescing on and switched off, so the cost of the design's one open
 * question — one request per inner chunk — is on the table beside the fix.
 * Each read opens the reader afresh, so the numbers include what it costs
 * to open (the container's structural prefix; the store's group and array
 * documents plus its whole-store index, `index.bin`, when the store carries
 * one), and every count is what the browser would issue: the
 * container's request set is exactly the spans its reader reports missing.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import { parseBundleMetadata } from "../src/manifest";
import type { TileRect } from "../src/tiles";
import { ZarrSession } from "../src/zarr/session";
import { ZarrStore } from "../src/zarr/store";
import { localFetch, newFetchLog, type LocalFetchLog } from "./localfetch";

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
const RUN_DIR = process.env.XUE_RUN_DIR ?? join(REPOSITORY_ROOT, "web/public/data/gfs.2026091000");
const BUNDLES = ["tmp2m", "prate", "wind10m"] as const;
/** The `.xue` worker's first fetch: enough of the header to size the prefix. */
const FIRST_FETCH_LENGTH = 16 * 1024;
/** A 6 x 4 tile viewport somewhere over the mid-latitudes of the 30 x 14 GFS
 * tiling — the shape the design note uses for its worked example. */
const VIEWPORT: TileRect = { firstColumn: 10, firstRow: 4, lastColumn: 15, lastRow: 7 };
/** A cell inside that viewport. */
const CELL = { column: 12 * 48 + 7, row: 5 * 52 + 9 };

interface Cost {
  requests: number;
  bytes: number;
}

interface Row {
  bundle: string;
  scenario: string;
  xue: Cost;
  zarr: Cost;
  zarrUncoalesced: Cost;
}

type Wasm = typeof import("../src/wasm/xue");

async function loadWasm(): Promise<Wasm> {
  const wasm = await import("../src/wasm/xue");
  await wasm.default({ module_or_path: readFileSync(join(REPOSITORY_ROOT, "web/src/wasm/xue_bg.wasm")) });
  return wasm;
}

function cost(log: LocalFetchLog): Cost {
  return { requests: log.requests, bytes: log.bytes };
}

/** The container over ranges, as worker.ts drives it: the prefix in one or
 * two requests, then exactly the spans the reader reports missing. */
async function openXue(wasm: Wasm, bundle: string) {
  const log = newFetchLog();
  const fetch = localFetch(RUN_DIR, log);
  const url = `local://${bundle}.xue`;
  const fetchRange = async (start: number, end: number): Promise<Uint8Array> => {
    const response = await fetch(url, { headers: { Range: `bytes=${start}-${end - 1}` } });
    return new Uint8Array(await response.arrayBuffer());
  };
  const size = readFileSync(join(RUN_DIR, `${bundle}.xue`)).byteLength;
  const first = await fetchRange(0, Math.min(FIRST_FETCH_LENGTH, size));
  const view = new DataView(first.buffer, first.byteOffset, first.byteLength);
  const dataOffset = Number(view.getBigUint64(56, true));
  let prefix = first.subarray(0, Math.min(dataOffset, first.byteLength));
  if (dataOffset > first.byteLength) {
    const rest = await fetchRange(first.byteLength, dataOffset);
    const joined = new Uint8Array(dataOffset);
    joined.set(prefix, 0);
    joined.set(rest, prefix.byteLength);
    prefix = joined;
  }
  const reader = new wasm.WasmStreamingBundle(prefix);
  const fetchSpans = async (flat: ArrayLike<number>) => {
    const tasks: Promise<void>[] = [];
    for (let index = 0; index + 1 < flat.length; index += 2) {
      const [start, end] = [flat[index]!, flat[index + 1]!];
      tasks.push(fetchRange(start, end).then((bytes) => reader.insertRange(start, bytes)));
    }
    await Promise.all(tasks);
  };
  const variables = parseBundleMetadata(reader.metadataJson()).variables.map((variable) => variable.numericId);
  const firstOffset = parseBundleMetadata(reader.metadataJson()).time.firstFrameOffset ?? 0;
  return { log, reader, variables, firstOffset, fetchSpans };
}

async function openZarr(wasm: Wasm, bundle: string, gap?: number) {
  const log = newFetchLog();
  const store = new ZarrStore(`local://${bundle}.zarr`, "measure", { fetch: localFetch(RUN_DIR, log), gap });
  const session = await ZarrSession.open(store, wasm.decodeChunk);
  return { log, session, variables: session.numericIds, firstOffset: session.frameOffsets[0]! };
}

async function measureXue(wasm: Wasm, bundle: string, scenario: string): Promise<Cost> {
  const { log, reader, variables, firstOffset, fetchSpans } = await openXue(wasm, bundle);
  for (const variable of variables) {
    if (scenario === "global") {
      const span = reader.missingGroupSpan(variable, firstOffset);
      if (span) await fetchSpans(span);
      reader.decodeFrame(variable, firstOffset);
    } else if (scenario === "viewport") {
      const rects = Uint32Array.from([VIEWPORT.firstColumn, VIEWPORT.firstRow, VIEWPORT.lastColumn, VIEWPORT.lastRow]);
      await fetchSpans(reader.missingTileSpans(variable, firstOffset, rects));
      reader.decodeFrameTiles(variable, firstOffset, rects);
    } else {
      await fetchSpans(reader.missingSeriesSpans(variable, CELL.column, CELL.row));
      reader.decodeSeries(variable, CELL.column, CELL.row);
    }
  }
  return cost(log);
}

async function measureZarr(wasm: Wasm, bundle: string, scenario: string, gap?: number): Promise<Cost> {
  const { log, session, variables, firstOffset } = await openZarr(wasm, bundle, gap);
  for (const variable of variables) {
    if (scenario === "global") await session.decodeFrame(variable, firstOffset);
    else if (scenario === "viewport") await session.decodeFrame(variable, firstOffset, [VIEWPORT]);
    else await session.decodeSeries(variable, CELL.column, CELL.row);
  }
  return cost(log);
}

function formatTable(rows: Row[]): string {
  const header = "| bundle | read | .xue requests | .xue bytes | Zarr requests | Zarr bytes | Zarr requests (no coalescing) | Zarr bytes (no coalescing) |";
  const rule = "|---|---|---:|---:|---:|---:|---:|---:|";
  const lines = rows.map(
    (row) =>
      `| ${row.bundle} | ${row.scenario} | ${row.xue.requests} | ${row.xue.bytes.toLocaleString("en-US")} | ${row.zarr.requests} | ${row.zarr.bytes.toLocaleString("en-US")} | ${row.zarrUncoalesced.requests} | ${row.zarrUncoalesced.bytes.toLocaleString("en-US")} |`,
  );
  return [header, rule, ...lines].join("\n");
}

const available = BUNDLES.every(
  (bundle) => existsSync(join(RUN_DIR, `${bundle}.xue`)) && existsSync(join(RUN_DIR, `${bundle}.zarr/zarr.json`)),
);

describe.skipIf(!available)("backend request costs", () => {
  it("counts requests and bytes for both backends", async () => {
    const wasm = await loadWasm();
    const rows: Row[] = [];
    for (const bundle of BUNDLES) {
      for (const scenario of ["global", "viewport", "series"]) {
        rows.push({
          bundle,
          scenario,
          xue: await measureXue(wasm, bundle, scenario),
          zarr: await measureZarr(wasm, bundle, scenario),
          zarrUncoalesced: await measureZarr(wasm, bundle, scenario, -1),
        });
      }
    }
    // Straight to stdout: the runner swallows console output of a passing test.
    process.stdout.write(`\nrun: ${RUN_DIR}\n${formatTable(rows)}\n\n`);
    // The comparison is the output; the assertion only guards that the
    // harness measured something on both sides.
    expect(rows.every((row) => row.xue.requests > 0 && row.zarr.requests > 0)).toBe(true);
  }, 120_000);
});
