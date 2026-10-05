/**
 * A surface station's window, turned into what the meteogram draws.
 *
 * The airport's reports already lay marks over the rows
 * (`observations.ts`); a weather station does the same with a denser and
 * steadier record — every ten minutes rather than every half hour, and at
 * stations a few kilometres from wherever the pin is rather than at the
 * nearest runway. Where the pin has a station at its own height, its marks
 * take the airport's place on the rows they both fill.
 *
 * The temperature is the one row that needs care: the station measures at
 * its own height, the model at its own terrain's, and on a mountain the two
 * are a kilometre apart. When the panel has the terrain-corrected row (the
 * model's 2 m temperature carried to the ground under the pin), that is
 * what a station at the pin's height is compared with, and the marks go
 * there instead.
 *
 * Arithmetic and tables; no DOM, no canvas, no fetching.
 */

import type { MeteogramObservation, MeteogramRowId } from "../meteogram";
import type { ObservationAxis } from "./observations";
import type { SynopStationSeries } from "./schema";

/** How near an observation must be to a frame's valid time to be shown
 * beside its readout. A station reports every ten minutes to every hour,
 * so half an hour separates a station that is reporting from one that has
 * gone quiet. */
export const SYNOP_MATCH_MS = 30 * 60 * 1000;

/** Which elements each row draws, in drawing order — the same order as the
 * row's own series, so the marks pair up with the traces. */
const ROW_ELEMENTS: Partial<Record<MeteogramRowId, readonly { key: string; kind: MeteogramObservation["kind"] }[]>> = {
  temperature: [{ key: "t", kind: "dot" }],
  terrain: [{ key: "t", kind: "dot" }],
  wind: [
    { key: "ws", kind: "dot" },
    { key: "gust", kind: "tick" },
  ],
  pressure: [{ key: "slp", kind: "dot" }],
};

/** The row a station's marks belong on: the temperature goes to the
 * terrain-corrected row when the panel has one, and only there. */
export function synopTakesRow(row: MeteogramRowId, hasTerrainRow: boolean): boolean {
  if (row === "temperature") return !hasTerrainRow;
  if (row === "terrain") return hasTerrainRow;
  return ROW_ELEMENTS[row] !== undefined;
}

/** One row's marks, oldest first: every report inside the run's span. */
export function synopRowObservations(
  row: MeteogramRowId,
  series: SynopStationSeries,
  axis: ObservationAxis,
): MeteogramObservation[] {
  const elements = ROW_ELEMENTS[row];
  if (!elements) return [];
  const marks: MeteogramObservation[] = [];
  series.time.forEach((seconds, position) => {
    const lead = seconds - axis.runTimeMs / 1000;
    if (lead < axis.firstLead || lead > axis.lastLead) return;
    for (const { key, kind } of elements) {
      const value = series.obs[key]?.[position];
      if (value === null || value === undefined || !Number.isFinite(value)) continue;
      marks.push({ x: lead, y: value, kind });
    }
  });
  return marks;
}

/** The row's headline element at the report nearest an instant, or null
 * when nothing was reported within `withinMs` of it. */
export function synopObservedValue(
  row: MeteogramRowId,
  series: SynopStationSeries,
  validTimeMs: number,
  withinMs: number = SYNOP_MATCH_MS,
): number | null {
  const key = ROW_ELEMENTS[row]?.[0]?.key;
  const column = key === undefined ? undefined : series.obs[key];
  if (!column) return null;
  let best: number | null = null;
  let bestDistance = Infinity;
  series.time.forEach((seconds, position) => {
    const value = column[position];
    if (value === null || value === undefined) return;
    const distance = Math.abs(seconds * 1000 - validTimeMs);
    if (distance < bestDistance) {
      best = value;
      bestDistance = distance;
    }
  });
  return bestDistance <= withinMs ? best : null;
}
