/** The horizontal axes under the lanes: distance along the line, and the days
 * of the hours shown. */

import type { PadAxis } from "./pad";
import { axisDistance } from "../crosssection/displayunits";
import { pointX, xPoint } from "../crosssection/chart";
import { element } from "../dom";
import { intlLocale } from "../i18n";
import { displayZone, formatDayMark, zoneOffsetMinutes } from "../timezone";

export const HOUR = 3600;
const DAY_MS = 86_400_000;

/** The points of a line, evenly spaced from edge to edge. */
export function lineAxis(points: number): PadAxis {
  return { count: points, x: (index, width) => pointX(index, points, width), index: (x, width) => xPoint(x, points, width) };
}

/** The frames of a run over so many hours from its first; a frame past them is
 * off the chart. `leads` are seconds. */
export function timeAxis(leads: readonly number[], hours: number): PadAxis {
  const first = leads[0] ?? 0;
  const x = (index: number, width: number): number => ((leads[index]! - first) / (hours * HOUR)) * width;
  return {
    count: leads.length,
    x,
    index: (at, width) => {
      let best = 0;
      for (let index = 1; index < leads.length; index += 1) {
        if (x(index, width) <= width + 0.5 && Math.abs(x(index, width) - at) < Math.abs(x(best, width) - at)) best = index;
      }
      return best;
    },
  };
}

/** Local midnights between two instants, in a zone. */
export function midnights(from: number, to: number, zone: string): number[] {
  const found: number[] = [];
  const local = from + zoneOffsetMinutes(zone, from) * 60_000;
  for (let day = Math.ceil(local / DAY_MS); ; day += 1) {
    const guess = day * DAY_MS - zoneOffsetMinutes(zone, from) * 60_000;
    // The offset may have changed on the way there.
    const at = day * DAY_MS - zoneOffsetMinutes(zone, guess) * 60_000;
    if (at > to) return found;
    if (at >= from) found.push(at);
  }
}

function label(text: string, left: string, side: "start" | "middle" | "end"): HTMLElement {
  const node = element("span", "wb-axis-label", text);
  node.style.left = left;
  node.dataset.side = side;
  return node;
}

export function distanceLabels(length: number, width: number): HTMLElement[] {
  const steps = Math.max(1, Math.min(6, Math.floor(width / 110)));
  return Array.from({ length: steps + 1 }, (_, at) =>
    label(axisDistance((length * at) / steps), `${(100 * at) / steps}%`, at === 0 ? "start" : at === steps ? "end" : "middle"),
  );
}

export function dayLabels(from: number, hours: number, width: number): HTMLElement[] {
  const to = from + hours * HOUR * 1000;
  const days = midnights(from, to, displayZone);
  const every = Math.max(1, Math.ceil((days.length * 62) / Math.max(1, width)));
  return days
    .filter((_, at) => at % every === 0)
    .map((at) => label(formatDayMark(at, displayZone, intlLocale), `${(100 * (at - from)) / (to - from)}%`, (to - at) / (to - from) < 0.06 ? "end" : "start"));
}

/** The lines across a chart through time: the midnights and the present. */
export function timeRules(from: number, hours: number, now: number): HTMLElement[] {
  const to = from + hours * HOUR * 1000;
  const rule = (className: string, at: number): HTMLElement => {
    const node = element("div", `wb-rule-fixed ${className}`);
    node.style.left = `${(100 * (at - from)) / (to - from)}%`;
    return node;
  };
  return [
    ...midnights(from, to, displayZone).map((at) => rule("wb-rule-day", at)),
    ...(now > from && now < to ? [rule("wb-rule-now", now)] : []),
  ];
}
