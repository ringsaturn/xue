/** How the storm map tells intensities apart: the Saffir–Simpson classes
 * a one-minute sustained wind falls in (the US best track's, NHC's and
 * JTWC's own averaging), in the colors the scale is usually drawn in, and
 * the three wind-radii thresholds in a violet ramp that stays clear of
 * them. Codes (`TD`, `TS`, `H1`…`H5`, `34KT`) are instrument text. */

const KNOT = 0.514444;

export interface IntensityClass {
  code: string;
  /** Lowest one-minute wind in the class, knots. */
  minKt: number;
  color: string;
}

export const SSHS_CLASSES: readonly IntensityClass[] = [
  { code: "TD", minKt: 0, color: "#5ebaff" },
  { code: "TS", minKt: 34, color: "#00c8c0" },
  { code: "H1", minKt: 64, color: "#f0e080" },
  { code: "H2", minKt: 83, color: "#ffd24a" },
  { code: "H3", minKt: 96, color: "#ffa530" },
  { code: "H4", minKt: 113, color: "#ff7020" },
  { code: "H5", minKt: 137, color: "#e8303a" },
];

/** The class of a wind in m/s, or null when the source gave none. */
export function sshsClass(vmax: number | null): IntensityClass | null {
  if (vmax === null) return null;
  const kt = vmax / KNOT;
  let found = SSHS_CLASSES[0]!;
  for (const item of SSHS_CLASSES) if (kt >= item.minKt - 0.5) found = item;
  return found;
}

/** The fill and outline of each radii threshold, palest for the widest. */
export const RADII_COLORS: Record<"34" | "50" | "64", string> = {
  "34": "#b9a6ea",
  "50": "#8a64d2",
  "64": "#5a2ca0",
};
