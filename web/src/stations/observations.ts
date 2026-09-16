/**
 * The nearest airport's reports, turned into what the meteogram draws.
 *
 * A meteogram is a forecast, and the one question a viewer asks of a
 * forecast is whether it is right. The airport product can answer that for
 * the hours already past: the same quantities the rows carry — temperature
 * and dew point, wind and gust, pressure, cloud — measured at a runway a
 * few kilometres from the pin, every half hour for the last day. So the
 * observations are laid *over* the rows rather than given rows of their
 * own: the model's trace and the marks that should be on it read as one
 * row, and the divergence is the reading.
 *
 * Two conversions make that possible and are the whole of this module.
 * Placement: an observation's clock becomes lead seconds from the run time,
 * which is the coordinate `meteogram.ts` places anything by, and a report
 * from before the run started or after its last frame has no place on the
 * chart and is dropped rather than clamped onto an end. Quantity: a METAR
 * reports sky cover as up to four layer codes, and the cloud row is drawn
 * in percent, so the codes become one total cover.
 *
 * The aerodrome forecast is the other direction — ahead of the playhead
 * rather than behind it — and becomes bands on its own row.
 *
 * Arithmetic and tables; no DOM, no canvas, no fetching.
 */

import type { MeteogramBand, MeteogramObservation, MeteogramRowId } from "../meteogram";
import type { CloudLayer, Metar, Taf, TafPeriod } from "./schema";

/**
 * Sky cover in eighths, by the code a layer is reported under.
 *
 * The three-letter codes are amounts, not names: `FEW` is one or two
 * eighths, `SCT` three or four, `BKN` five to seven, `OVC` eight, and the
 * midpoint of each is what a total cover is drawn at. The clear-sky codes
 * are zero — `SKC` and `CLR` say the observer or the sensor saw none, `NSC`
 * and `NCD` that there is none worth reporting, `CAVOK` that ceiling and
 * visibility are both unrestricted. `OVX` is an obscured sky, which is
 * total cover of a kind a cloud layer cannot describe, and reads as eight.
 */
export const CLOUD_OKTAS: Readonly<Record<string, number>> = {
  SKC: 0,
  CLR: 0,
  NSC: 0,
  NCD: 0,
  CAVOK: 0,
  FEW: 2,
  SCT: 4,
  BKN: 6,
  OVC: 8,
  OVX: 8,
};

/**
 * Total sky cover in eighths, or null when the report says nothing about
 * the sky (no cloud group at all, or only codes this reader does not know).
 *
 * METAR layers are cumulative and reported from the lowest base up, so the
 * highest layer carries the greatest amount; taking the largest okta over
 * the layers is that, and survives a report whose layers arrive in another
 * order.
 */
export function cloudOktas(layers: readonly CloudLayer[]): number | null {
  let total: number | null = null;
  for (const layer of layers) {
    const oktas = CLOUD_OKTAS[layer.cover.toUpperCase()];
    if (oktas === undefined) continue;
    total = total === null ? oktas : Math.max(total, oktas);
  }
  return total;
}

/** The same, in the percent the cloud row is drawn in. */
export function cloudCoverPercent(layers: readonly CloudLayer[]): number | null {
  const oktas = cloudOktas(layers);
  return oktas === null ? null : (oktas / 8) * 100;
}

/** The axis an observation is placed against: the run it is laid over and
 * the lead seconds of its first and last frame. */
export interface ObservationAxis {
  /** Epoch milliseconds of the run time — lead zero. */
  runTimeMs: number;
  firstLead: number;
  lastLead: number;
}

/** Lead seconds of an instant on a run's axis, or null when it falls
 * outside the run's span. A report older than the run's first frame or
 * later than its last has no column, and is not drawn. */
export function leadSecondsOf(time: string, axis: ObservationAxis): number | null {
  const at = Date.parse(time);
  if (!Number.isFinite(at)) return null;
  const lead = (at - axis.runTimeMs) / 1000;
  return lead < axis.firstLead || lead > axis.lastLead ? null : lead;
}

/** What each row reads off a METAR, in drawing order — the same order the
 * row's own series are in, so the marks and the traces pair up.
 *
 * Pressure is the one row whose quantity is not reported directly by every
 * station: `slp` is the sea-level pressure the report states, and where it
 * does not, `qnh` — the altimeter setting — stands in. The two agree to a
 * fraction of a hectopascal at a low-lying aerodrome (both reduce the same
 * station pressure to sea level, by conventions that differ only in the
 * temperature profile assumed), and diverge on a high plateau, where the
 * mark will sit visibly off the PRMSL trace. That is the honest failure:
 * the row is drawing what the station published. */
const ROW_READERS: Partial<
  Record<MeteogramRowId, readonly { read: (metar: Metar) => number | null; kind: MeteogramObservation["kind"] }[]>
> = {
  temperature: [
    { read: (metar) => metar.t, kind: "dot" },
    { read: (metar) => metar.td, kind: "hollow" },
  ],
  wind: [
    { read: (metar) => metar.ws, kind: "dot" },
    { read: (metar) => metar.gust, kind: "tick" },
  ],
  pressure: [{ read: (metar) => metar.slp ?? metar.qnh, kind: "dot" }],
  cloud: [{ read: (metar) => cloudCoverPercent(metar.cloud), kind: "step" }],
};

/** Whether a row has anything an airport can put over it. Precipitation
 * has not: a METAR reports what fell as a code, never as a rate. */
export function rowTakesObservations(row: MeteogramRowId): boolean {
  return ROW_READERS[row] !== undefined;
}

/** The one value of a report that belongs beside a row's readout: its
 * headline quantity, the first the row draws. The companion (the dew
 * point, the gust) is on the chart and would not fit the line. */
export function rowObservedValue(row: MeteogramRowId, metar: Metar): number | null {
  const value = ROW_READERS[row]?.[0]?.read(metar) ?? null;
  return value !== null && Number.isFinite(value) ? value : null;
}

/**
 * One row's observation marks, oldest first — the order the stepped rows
 * need and the one that makes every row read left to right.
 */
export function rowObservations(
  row: MeteogramRowId,
  metars: readonly Metar[],
  axis: ObservationAxis,
): MeteogramObservation[] {
  const readers = ROW_READERS[row];
  if (!readers) return [];
  const marks: MeteogramObservation[] = [];
  for (const metar of metars) {
    const lead = leadSecondsOf(metar.time, axis);
    if (lead === null) continue;
    for (const reader of readers) {
      const value = reader.read(metar);
      if (value === null || !Number.isFinite(value)) continue;
      marks.push({ x: lead, y: value, kind: reader.kind });
    }
  }
  return marks.sort((a, b) => a.x - b.x);
}

/** How near an observation must be to a frame's valid time to be shown
 * beside its readout: ninety minutes, which is three scheduled reports. A
 * station reports on the hour and often at the half hour, so anything the
 * playhead sits on has a report within that unless the station has gone
 * quiet — and then there is nothing to show, which is the point of the
 * bound. */
export const OBSERVATION_MATCH_MS = 90 * 60 * 1000;

/**
 * The report nearest an instant, or null when the nearest is further than
 * `withinMs`. The history is newest first and need not be scanned in any
 * particular order for this; the whole day is a few dozen entries.
 */
export function nearestObservation(
  metars: readonly Metar[],
  validTimeMs: number,
  withinMs: number = OBSERVATION_MATCH_MS,
): Metar | null {
  let best: Metar | null = null;
  let bestDistance = Infinity;
  for (const metar of metars) {
    const at = Date.parse(metar.time);
    if (!Number.isFinite(at)) continue;
    const distance = Math.abs(at - validTimeMs);
    if (distance < bestDistance) {
      best = metar;
      bestDistance = distance;
    }
  }
  return best !== null && bestDistance <= withinMs ? best : null;
}

/** The wind of a report or a forecast group, as the bands and the readouts
 * write it: the direction it comes from and the speed. `VRB` where the
 * direction is variable — the code the report itself uses, since there is
 * no direction to write — and null where there is no wind at all. */
export function formatWind(wd: number | null, ws: number | null): string | null {
  if (ws === null || !Number.isFinite(ws)) return null;
  const from = wd === null ? "VRB" : `${String(Math.round(wd)).padStart(3, "0")}°`;
  return `${from} ${Math.round(ws)} m/s`;
}

/**
 * The aerodrome forecast as bands on the axis.
 *
 * Every period is placed by its own validity, clipped to the run's span: a
 * forecast that starts before the first frame begins at it, one that runs
 * past the last ends there, and one entirely outside is dropped. `FM` and
 * `BECMG` are the forecast proper and are solid; `TEMPO` and `PROB` are
 * hatched, since they qualify what runs under them rather than replacing
 * it.
 *
 * A group states only what it changes (`docs/airport.md` §5), so a period
 * with no wind of its own inherits the prevailing group's, exactly as the
 * bulletin is read — otherwise a `TEMPO` group about visibility alone would
 * draw as a band with no wind in a column of bands that all have one.
 */
export function tafBands(taf: Taf, axis: ObservationAxis): MeteogramBand[] {
  const prevailing: TafPeriod | undefined = taf.periods.find((period) => period.change === null)
    ?? taf.periods[0];
  const bands: MeteogramBand[] = [];
  for (const period of taf.periods) {
    const from = Date.parse(period.from);
    const to = Date.parse(period.to);
    if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) continue;
    const fromLead = (from - axis.runTimeMs) / 1000;
    const toLead = (to - axis.runTimeMs) / 1000;
    if (toLead < axis.firstLead || fromLead > axis.lastLead) continue;
    const inherited = period === prevailing ? period : (prevailing ?? period);
    const wd = period.wd ?? inherited.wd;
    const ws = period.ws ?? inherited.ws;
    const wx = period.wx ?? inherited.wx;
    const label = [formatWind(wd, ws), wx].filter((part): part is string => Boolean(part)).join(" ");
    const temporary = period.change === "TEMPO" || period.change === "PROB" || period.prob !== null;
    bands.push({
      from: Math.max(axis.firstLead, fromLead),
      to: Math.min(axis.lastLead, toLead),
      style: temporary ? "hatched" : "solid",
      label,
      prob: period.prob,
    });
  }
  return bands;
}
