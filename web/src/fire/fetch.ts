/** Reading the fire product off the data root: the pointer → index fetch
 * (`pointerindex.ts`, null when the root publishes no fire product) and,
 * per sensor, one range request covering the consecutive hour lines a time
 * span needs. Nothing here touches the map. */

import { loadPointerIndex, type LoadedIndex } from "../pointerindex";
import { fetchSpanBytes, stationFileUrl } from "../stations/fetch";
import {
  FIRE_POINTER_FILENAME,
  parseFireIndex,
  parseFireLine,
  parseFirePointer,
  type FireHour,
  type FireIndex,
  type FireLine,
  type FirePointer,
} from "./schema";

export type LoadedFireIndex = LoadedIndex<FirePointer, FireIndex>;

export function fetchFireIndex(baseUrl: string): Promise<LoadedFireIndex | null> {
  return loadPointerIndex(baseUrl, FIRE_POINTER_FILENAME, parseFirePointer, parseFireIndex, "fire");
}

/** One sensor's rows whose hour overlaps `[from, to)` (epoch ms), and the
 * byte span they occupy together — a sensor's rows address consecutive
 * lines, so any run of them is one request. Null when none overlap. */
export function sensorSpan(
  index: FireIndex,
  sensor: number,
  from: number,
  to: number,
): { rows: FireHour[]; offset: number; length: number } | null {
  const rows = index.hours.filter(
    (row) => row.sensor === sensor && row.hour + 3600_000 > from && row.hour < to,
  );
  if (rows.length === 0) return null;
  const first = rows[0]!;
  const last = rows[rows.length - 1]!;
  return { rows, offset: first.offset, length: last.offset + last.length - first.offset };
}

/** Split a span's bytes into its lines at the offsets the rows give — the
 * index, not a newline scan, says where a line is — and parse each. */
export function splitLines(bytes: Uint8Array, rows: readonly FireHour[], sensorId: string): FireLine[] {
  const base = rows[0]?.offset ?? 0;
  const decoder = new TextDecoder();
  return rows.map((row) => {
    const start = row.offset - base;
    const line = parseFireLine(JSON.parse(decoder.decode(bytes.subarray(start, start + row.length))) as unknown);
    if (line.sensor !== sensorId) throw new Error(`${sensorId}.jsonl span names ${line.sensor}`);
    if (line.hour * 1000 !== row.hour) throw new Error(`${sensorId}.jsonl line is not the hour its row names`);
    return line;
  });
}

/** One sensor's detections over `[from, to)`, in one range request. */
export async function fetchFireLines(
  loaded: LoadedFireIndex,
  sensor: number,
  from: number,
  to: number,
): Promise<FireLine[]> {
  const span = sensorSpan(loaded.index, sensor, from, to);
  if (span === null) return [];
  const entry = loaded.index.sensors[sensor]!;
  const url = stationFileUrl(loaded.indexUrl, entry.file);
  return splitLines(await fetchSpanBytes(url, span.offset, span.length), span.rows, entry.id);
}
