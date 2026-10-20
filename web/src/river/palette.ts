/**
 * How a reach is drawn: coloured by its flow against its usual flow, wide
 * by its flow.
 *
 * The ratio is `Q / max(baseflow, 1 m³/s)` — the floor keeps a dry
 * headwater whose usual flow is a trickle from reading as a flood at the
 * first litre — in four classes whose hues are the EFAS return-period
 * classes' (`levels.ts::DISRP_CLASSES`) where they correspond: under twice
 * the usual flow the below-threshold grey, then the 2-year yellow, the
 * 5-year red and the 20-year purple. The width is `log10(Q)` from 1 device
 * pixel at 1 m³/s to 5 at 10 000 m³/s, so a creek stays a hairline and a
 * river in flood a band. A reach with no value is a thin faint grey.
 *
 * These functions are the shader's arithmetic in TypeScript (`layer.ts`
 * computes the same per vertex); the tests hold the two to the table.
 */

import { DISRP_CLASSES } from "../levels";

/** Ratios at which the class steps up. */
export const RIVER_RATIO_BREAKS = [2, 10, 100] as const;

/** The least usual flow a ratio is taken against, m³/s. */
export const RIVER_BASEFLOW_FLOOR = 1;

export const RIVER_WIDTH_MIN_PX = 1;
export const RIVER_WIDTH_MAX_PX = 5;

function disrp(code: number): readonly [number, number, number] {
  const entry = DISRP_CLASSES.find((item) => item.code === code);
  if (!entry) throw new Error(`no return-period class ${code}`);
  return entry.rgb;
}

/** The four classes' colours, `[r, g, b, a]` in 0–1, grey to purple. The
 * grey is drawn nearly opaque: the EFAS key's grey is translucent over a
 * field, but a line is thin enough to need the weight. */
export function riverClassColors(): [number, number, number, number][] {
  const rgba = (rgb: readonly [number, number, number], alpha: number): [number, number, number, number] => [
    rgb[0] / 255,
    rgb[1] / 255,
    rgb[2] / 255,
    alpha,
  ];
  return [rgba(disrp(1), 0.8), rgba(disrp(3), 1), rgba(disrp(4), 1), rgba(disrp(5), 1)];
}

/** A reach with no value. */
export const RIVER_NODATA_COLOR: [number, number, number, number] = [150 / 255, 170 / 255, 190 / 255, 0.4];

/** The ratio of a flow to its reach's usual flow, floored; an unknown
 * usual flow (NaN) is taken as the floor. */
export function reachRatio(flow: number, baseflow: number): number {
  const usual = Number.isFinite(baseflow) ? Math.max(baseflow, RIVER_BASEFLOW_FLOOR) : RIVER_BASEFLOW_FLOOR;
  return flow / usual;
}

/** The class of a reach, 0 (grey) to 3 (purple), or -1 for no value. */
export function reachClass(flow: number, baseflow: number): number {
  if (!Number.isFinite(flow)) return -1;
  const ratio = reachRatio(flow, baseflow);
  let index = 0;
  for (const limit of RIVER_RATIO_BREAKS) if (ratio >= limit) index += 1;
  return index;
}

/** The line's width in device pixels: 1 at or under 1 m³/s, 5 at or over
 * 10 000, linear in `log10(Q)` between; 1 for no value. */
export function reachWidthPx(flow: number): number {
  if (!Number.isFinite(flow) || flow <= 1) return RIVER_WIDTH_MIN_PX;
  return Math.min(RIVER_WIDTH_MAX_PX, RIVER_WIDTH_MIN_PX + Math.log10(flow));
}
