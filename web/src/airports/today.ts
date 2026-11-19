/** The day's observed extremes at an airport: the highest and lowest
 * temperature its METARs have reported since midnight on the airport's own
 * clock. Arithmetic over the reports the product carries; no DOM. */

import type { Metar } from "../stations/schema";

/** The calendar day an instant falls on in a zone, `YYYY-MM-DD`. */
export function localDay(timeMs: number, zone: string): string {
  // en-CA writes the ISO order.
  return new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(timeMs);
}

export interface DayExtremes {
  /** The local day, `YYYY-MM-DD`. */
  day: string;
  high: { t: number; time: string };
  low: { t: number; time: string };
  /** Reports with a temperature counted. */
  reports: number;
}

/** The extremes of the reports on the local day `nowMs` falls on, or null
 * when none of them reports a temperature. On a tie the earlier report
 * names the time, which is when the value was first reached. */
export function dayExtremes(metars: readonly Metar[], zone: string, nowMs: number): DayExtremes | null {
  const day = localDay(nowMs, zone);
  const todays = metars
    .filter((metar) => metar.t !== null && localDay(Date.parse(metar.time), zone) === day)
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time));
  if (todays.length === 0) return null;
  let high = todays[0]!;
  let low = todays[0]!;
  for (const metar of todays) {
    if (metar.t! > high.t!) high = metar;
    if (metar.t! < low.t!) low = metar;
  }
  return { day, high: { t: high.t!, time: high.time }, low: { t: low.t!, time: low.time }, reports: todays.length };
}
