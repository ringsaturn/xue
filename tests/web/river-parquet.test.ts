// @vitest-environment node
// hyparquet checks `instanceof ArrayBuffer`, which a jsdom realm's buffers
// fail; the reader runs in a browser realm or Node's, never jsdom's.
import { describe, expect, it } from "vitest";

import { openReaches, rangeAsyncBuffer, readRowGroupLines, rowGroupsMeeting, type RangeLog } from "../../web/src/river/parquet";
import { buildReachesParquet, sampleReaches } from "./fixtures/river";

const FEATURES = 168;
const ROWS = 64;
const reaches = sampleReaches(FEATURES, ROWS);
const file = buildReachesParquet(reaches, ROWS);

/** A range-capable origin over the fixture, answering 206 exactly. */
function rangeFetch(bytes: Uint8Array, wholeOnly = false) {
  return async (_url: string, init?: RequestInit): Promise<Response> => {
    const range = new Headers(init?.headers).get("Range");
    const span = range ? /^bytes=(\d+)-(\d+)$/.exec(range) : null;
    if (wholeOnly || !span) return new Response(bytes.slice(), { status: 200 });
    return new Response(bytes.slice(Number(span[1]), Number(span[2]) + 1), { status: 206 });
  };
}

function open(log: RangeLog, wholeOnly = false) {
  return openReaches(rangeAsyncBuffer("https://data.test/reaches.parquet?v=0", file.byteLength, rangeFetch(file, wholeOnly), log), {
    featureCount: FEATURES,
    rowGroupRows: ROWS,
  });
}

describe("reaches GeoParquet", () => {
  it("reads the footer first, as one suffix of the file", async () => {
    const log: RangeLog = { ranges: [] };
    const reachFile = await open(log);
    expect(log.ranges).toEqual([`bytes=${file.byteLength - 16384}-${file.byteLength - 1}`]);
    expect(reachFile.groups.map((group) => [group.rowStart, group.rowCount])).toEqual([
      [0, 64],
      [64, 64],
      [128, 40],
    ]);
  });

  it("takes each row group's box from the bbox statistics", async () => {
    const reachFile = await open({ ranges: [] });
    const [west, south, east, north] = reachFile.groups[1]!.bbox!;
    expect(west).toBeCloseTo(-98.8, 4);
    expect(south).toBeCloseTo(30, 4);
    expect(east).toBeGreaterThan(-98.1);
    expect(east).toBeLessThan(-97.9);
    expect(north).toBeCloseTo(30.703, 3);
  });

  it("prunes row groups to the view, and keeps all for the whole case", async () => {
    const reachFile = await open({ ranges: [] });
    expect(rowGroupsMeeting(reachFile.groups, [-98.7, 29.5, -98.5, 31])).toEqual([1]);
    expect(rowGroupsMeeting(reachFile.groups, [-99.5, 29.5, -98.5, 31])).toEqual([0, 1]);
    expect(rowGroupsMeeting(reachFile.groups, [-90, 29.5, -89, 31])).toEqual([]);
    expect(rowGroupsMeeting(reachFile.groups, null)).toEqual([0, 1, 2]);
  });

  it("reads one row group's geometry and nothing of the others", async () => {
    const log: RangeLog = { ranges: [] };
    const reachFile = await open(log);
    const lines = await readRowGroupLines(reachFile, 2);
    expect(lines).toHaveLength(40);
    expect(Array.from(lines[0]!)).toEqual(reaches[128]!.line);
    expect(Array.from(lines[39]!)).toEqual(reaches[167]!.line);
    // The footer, then the one geometry chunk, inside row group 2's bytes.
    expect(log.ranges).toHaveLength(2);
    const [, start] = /^bytes=(\d+)-/.exec(log.ranges[1]!)!;
    const group2 = reachFile.metadata.row_groups[2]!.columns[0]!.meta_data!.data_page_offset;
    expect(Number(start)).toBeGreaterThanOrEqual(Number(group2));
  });

  it("serves an origin without ranges from the one body", async () => {
    const log: RangeLog = { ranges: [] };
    const reachFile = await open(log, true);
    const lines = await readRowGroupLines(reachFile, 0);
    expect(Array.from(lines[5]!)).toEqual(reaches[5]!.line);
    expect(log.ranges).toHaveLength(1);
  });

  it("refuses a file that is not the one the index names", async () => {
    const buffer = rangeAsyncBuffer("https://data.test/r.parquet", file.byteLength, rangeFetch(file));
    await expect(openReaches(buffer, { featureCount: FEATURES + 1, rowGroupRows: ROWS })).rejects.toThrow(/rows/);
    await expect(openReaches(buffer, { featureCount: FEATURES, rowGroupRows: 32 })).rejects.toThrow(/row group/);
  });
});
