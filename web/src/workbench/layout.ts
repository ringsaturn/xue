/**
 * How the page is shared out: which charts are beside each other, and what
 * gives way as the window gets short. The charts are to be tall, since height
 * is the axis they have in common; the map gives way first, then the lanes,
 * and in the end the page scrolls.
 */

/** CSS pixels, as the stylesheet has them. */
export const ROWS = {
  header: 48,
  bar: 64,
  head: 34,
  /** Above and below a plot, for what its marks lean out by. */
  gutter: 20,
  readout: 28,
  lane: 36,
  axis: 24,
} as const;

/** Lanes under the charts. */
export const LANES = 4;

const WIDE = 1400;
const PHONE = 720;
/** A plot shorter than this has what is around it give way. */
const SHORT_PLOT = 240;
/** And it is never shorter than this: the page scrolls. */
const LEAST_PLOT = 220;
const LOCATOR = { share: 0.27, shortShare: 0.2, min: 110, max: 300 };

/** How much of the page the map is given: the charts' most, half of it, or
 * the map's most. */
export type MapSize = "charts" | "even" | "map";
export const MAP_SIZES: readonly MapSize[] = ["map", "even", "charts"];
/** The map's share of what is under the bar, for a size other than the
 * charts'. */
const MAP_SHARE: Record<Exclude<MapSize, "charts">, number> = { even: 0.4, map: 0.66 };

/** `both` charts beside the profile, `one` of them at a time, or a `phone`'s
 * one chart at a time. */
export type Arrangement = "both" | "one" | "phone";

export interface Layout {
  arrangement: Arrangement;
  locator: number;
  plot: number;
  lanes: boolean;
}

/** `bar` is the bar's own height, which wraps where the window is narrow. A
 * phone has no choice of size. */
export function workbenchLayout(width: number, height: number, hasLine: boolean, bar: number = ROWS.bar, size: MapSize = "charts"): Layout {
  const arrangement: Arrangement = width < PHONE ? "phone" : hasLine && width < WIDE ? "one" : "both";
  const fixed = ROWS.header + bar + ROWS.head + 2 * ROWS.gutter + ROWS.readout + ROWS.axis;
  const free = height - fixed;
  const laneRoom = LANES * ROWS.lane;
  const locatorAt = (share: number): number => Math.round(Math.min(LOCATOR.max, Math.max(LOCATOR.min, share * height)));
  let locator = locatorAt(LOCATOR.share);
  let lanes = true;
  if (free - locator - laneRoom < SHORT_PLOT) locator = locatorAt(LOCATOR.shortShare);
  if (free - locator - laneRoom < SHORT_PLOT) lanes = false;
  if (size !== "charts" && arrangement !== "phone") {
    // The map was asked to be large: the lanes give way to it, and it gives
    // way only so far as to leave the charts their least.
    const asked = Math.round(MAP_SHARE[size] * (height - ROWS.header - bar));
    lanes = free - asked - laneRoom >= SHORT_PLOT;
    locator = Math.max(locator, Math.min(asked, free - (lanes ? laneRoom : 0) - LEAST_PLOT));
  }
  const plot = Math.max(LEAST_PLOT, Math.floor(free - locator - (lanes ? laneRoom : 0)));
  return { arrangement, locator, plot, lanes };
}
