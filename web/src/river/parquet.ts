/**
 * The reaches' GeoParquet (`zarr-profile.md`, "Network store"), read by
 * range through `hyparquet`: the footer first, then only the row groups
 * whose `bbox` covering statistics meet the view.
 *
 * The file is cut so that row group `g` holds rows `[g × rowGroupRows,
 * (g + 1) × rowGroupRows)`, which are the network store's feature chunk
 * `g`: a row's index is its feature, and a reader that wants one area reads
 * that area's row groups here and exactly the matching inner chunks of the
 * store. The geometry column is WKB, decoded by `wkb.ts` (hyparquet's own
 * GeoParquet conversion is switched off: it builds GeoJSON objects per
 * reach where a typed array of vertices is what the line layer uploads).
 */

import { parquetMetadataAsync, parquetRead, type AsyncBuffer, type FileMetaData } from "hyparquet";
import { compressors } from "hyparquet-compressors";

import { fetchImmutable } from "../fetchimmutable";
import { decodeWkbLineString } from "./wkb";

/** `[west, south, east, north]` in degrees. */
export type LonLatBox = [number, number, number, number];

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

/** Bytes asked for in the first read: the Guadalupe file's footer is
 * 6.6 KB, and hyparquet reads further on its own if a footer is longer. */
const FOOTER_FETCH_BYTES = 16 * 1024;

export interface RangeLog {
  /** Every request as its Range header (`null` for a whole GET). */
  ranges: (string | null)[];
}

/**
 * An `AsyncBuffer` over one immutable file at `url`, every `slice` one HTTP
 * range request. An origin that answers a range with the whole object
 * (200) is served from that one body for the buffer's life, the store's
 * whole-object mode in miniature.
 */
export function rangeAsyncBuffer(
  url: string,
  byteLength: number,
  fetchImpl: FetchLike = (target, init) => fetchImmutable(target, init),
  log?: RangeLog,
): AsyncBuffer {
  let whole: ArrayBuffer | null = null;
  return {
    byteLength,
    async slice(start: number, end: number = byteLength): Promise<ArrayBuffer> {
      if (whole) return whole.slice(start, end);
      if (end <= start) return new ArrayBuffer(0);
      const range = `bytes=${start}-${end - 1}`;
      log?.ranges.push(range);
      const response = await fetchImpl(url, { headers: { Range: range } });
      if (!response.ok) throw new Error(`reaches range request failed: HTTP ${response.status}`);
      const body = await response.arrayBuffer();
      if (response.status === 206) {
        if (body.byteLength !== end - start) throw new Error("reaches range response length mismatch");
        return body;
      }
      if (body.byteLength !== byteLength) throw new Error("reaches file is not the length the index names");
      whole = body;
      return body.slice(start, end);
    },
  };
}

export interface ReachRowGroup {
  index: number;
  /** The group's first row, which is its first feature. */
  rowStart: number;
  rowCount: number;
  /** The union of its rows' `bbox` covering, or null when the file carries
   * no statistics for it (the group then meets every view). */
  bbox: LonLatBox | null;
}

export interface ReachFile {
  file: AsyncBuffer;
  metadata: FileMetaData;
  groups: ReachRowGroup[];
}

function statNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "bigint") return Number(value);
  return null;
}

/** The row groups' bounding boxes from the `bbox.{xmin,ymin,xmax,ymax}`
 * column chunk statistics. */
function groupBox(columns: FileMetaData["row_groups"][number]["columns"]): LonLatBox | null {
  const stat = (field: string, which: "min_value" | "max_value"): number | null => {
    const column = columns.find((entry) => entry.meta_data?.path_in_schema.join(".") === `bbox.${field}`);
    return statNumber(column?.meta_data?.statistics?.[which]);
  };
  const west = stat("xmin", "min_value");
  const south = stat("ymin", "min_value");
  const east = stat("xmax", "max_value");
  const north = stat("ymax", "max_value");
  if (west === null || south === null || east === null || north === null) return null;
  return [west, south, east, north];
}

/** Read the footer and lay out the row groups, held to what the index says
 * of the file: its row count, and row groups of exactly `rowGroupRows`
 * rows, the last one shorter. */
export async function openReaches(file: AsyncBuffer, expected: { featureCount: number; rowGroupRows: number }): Promise<ReachFile> {
  const metadata = await parquetMetadataAsync(file, { geoparquet: false, initialFetchSize: FOOTER_FETCH_BYTES });
  if (Number(metadata.num_rows) !== expected.featureCount) {
    throw new Error(`reaches file holds ${metadata.num_rows} rows, not the ${expected.featureCount} the index names`);
  }
  const geometry = metadata.schema.find((element) => element.name === "geometry");
  if (!geometry || geometry.type !== "BYTE_ARRAY") throw new Error("reaches file has no WKB geometry column");
  let rowStart = 0;
  const groups = metadata.row_groups.map((group, index) => {
    const rowCount = Number(group.num_rows);
    const last = index === metadata.row_groups.length - 1;
    if (rowCount > expected.rowGroupRows || (!last && rowCount !== expected.rowGroupRows) || rowCount <= 0) {
      throw new Error(`reaches row group ${index} holds ${rowCount} rows, not ${expected.rowGroupRows}`);
    }
    const entry: ReachRowGroup = { index, rowStart, rowCount, bbox: groupBox(group.columns) };
    rowStart += rowCount;
    return entry;
  });
  return { file, metadata, groups };
}

/** Whether two boxes meet. A box crossing the antimeridian is not a case
 * this format has (a river network is regional), so plain intervals. */
export function boxesMeet(a: LonLatBox, b: LonLatBox): boolean {
  return a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
}

/** The row groups the view needs: those whose box meets it, every group
 * when the view is null (the whole case on screen). */
export function rowGroupsMeeting(groups: readonly ReachRowGroup[], view: LonLatBox | null): number[] {
  return groups.filter((group) => view === null || group.bbox === null || boxesMeet(group.bbox, view)).map((group) => group.index);
}

/** One row group's reaches as vertex arrays, in row order. */
export async function readRowGroupLines(reaches: ReachFile, groupIndex: number): Promise<Float64Array[]> {
  const group = reaches.groups[groupIndex];
  if (!group) throw new Error(`reaches file has no row group ${groupIndex}`);
  let rows: unknown[][] = [];
  await parquetRead({
    file: reaches.file,
    metadata: reaches.metadata,
    columns: ["geometry"],
    rowStart: group.rowStart,
    rowEnd: group.rowStart + group.rowCount,
    utf8: false,
    geoparquet: false,
    compressors,
    onComplete: (data: unknown[][]) => {
      rows = data;
    },
  });
  if (rows.length !== group.rowCount) throw new Error(`reaches row group ${groupIndex} read ${rows.length} rows, not ${group.rowCount}`);
  return rows.map((row, position) => {
    const bytes = row[0];
    if (!(bytes instanceof Uint8Array)) throw new Error(`reach ${group.rowStart + position} has no geometry`);
    return decodeWkbLineString(bytes);
  });
}
