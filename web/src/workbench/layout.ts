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

/** `both` charts beside the profile, `one` of them at a time, or a `phone`'s
 * one chart at a time. */
export type Arrangement = "both" | "one" | "phone";

export interface Layout {
  arrangement: Arrangement;
  locator: number;
  plot: number;
  lanes: boolean;
}

/** `bar` is the bar's own height, which wraps where the window is narrow. */
export function workbenchLayout(width: number, height: number, hasLine: boolean, bar: number = ROWS.bar): Layout {
  const arrangement: Arrangement = width < PHONE ? "phone" : hasLine && width < WIDE ? "one" : "both";
  const fixed = ROWS.header + bar + ROWS.head + 2 * ROWS.gutter + ROWS.readout + ROWS.axis;
  const free = height - fixed;
  const laneRoom = LANES * ROWS.lane;
  const locatorAt = (share: number): number => Math.round(Math.min(LOCATOR.max, Math.max(LOCATOR.min, share * height)));
  let locator = locatorAt(LOCATOR.share);
  let lanes = true;
  if (free - locator - laneRoom < SHORT_PLOT) locator = locatorAt(LOCATOR.shortShare);
  if (free - locator - laneRoom < SHORT_PLOT) lanes = false;
  const plot = Math.max(LEAST_PLOT, Math.floor(free - locator - (lanes ? laneRoom : 0)));
  return { arrangement, locator, plot, lanes };
}
