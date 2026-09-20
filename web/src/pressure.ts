import { t } from "./i18n";
import { registeredBundleId, type VariableIdentity } from "./identity";
import type { DataVariableId, ForecastBundleId, PressureBundleId } from "./manifest";

/**
 * The pressure family: mean sea level pressure and geopotential height on the
 * standard isobaric surfaces. These are the only bundles the viewer draws as
 * contour lines rather than a filled field, and this module is what the
 * contour renderer reads.
 *
 * Everything here is *chart* knowledge, not container knowledge. The bundles
 * are ordinary linear-codebook scalars; nothing in the `.xue` file says what
 * interval to draw or which line to thicken. What does live in the file — and
 * what the whole rendering depends on — is that the encoder chose each
 * codebook's offset so every contour value below lands exactly half a code
 * off (docs/format.md, "Registered linear codebooks"). A contour drawn where
 * the code is flat would light up a whole plateau instead of a line.
 *
 * The numbers are held to the encoders by
 * `tests/fixtures/pressure-registry.json`: `tests/web/pressure.test.ts` reads
 * that file, as do `tests/test_pressure.py` and the Rust encoder's unit
 * tests, so an interval cannot move on one side alone.
 */
export interface PressureLevelInfo {
  id: PressureBundleId;
  /** Isobaric surface in hPa; null for mean sea level pressure. */
  levelHpa: number | null;
  /** Contour interval in the variable's own unit (hPa or gpm). */
  contourInterval: number;
  /** A regular sub-family drawn heavier — every 20 hPa on a surface chart. */
  emphasisInterval?: number;
  /** Particular contours drawn heavier, when no interval expresses them. */
  emphasisContours?: readonly number[];
  /** Codebook coverage, which is also the fill palette's range. */
  range: readonly [number, number];
}

export type { PressureBundleId };

export const PRESSURE_BUNDLE_IDS: readonly PressureBundleId[] = [
  "prmsl",
  "hgt1000",
  "hgt925",
  "hgt850",
  "hgt700",
  "hgt500",
  "hgt300",
  "hgt250",
  "hgt200",
];

export const PRESSURE_LEVELS: Record<PressureBundleId, PressureLevelInfo> = {
  // 4 hPa is the surface-chart interval; every fifth line (20 hPa) is the
  // grid the chart is actually read on.
  prmsl: { id: "prmsl", levelHpa: null, contourInterval: 4, emphasisInterval: 20, range: [870.5, 1124.5] },
  hgt1000: { id: "hgt1000", levelHpa: 1000, contourInterval: 30, range: [-905, 1635] },
  hgt925: { id: "hgt925", levelHpa: 925, contourInterval: 30, range: [-249, 1275] },
  hgt850: { id: "hgt850", levelHpa: 850, contourInterval: 30, range: [423, 1947] },
  hgt700: { id: "hgt700", levelHpa: 700, contourInterval: 30, range: [1911, 3435] },
  // 5880 and 5840 gpm — "588" and "584" — are the two lines the subtropical
  // high is read by, and they are adjacent contours rather than a sub-family,
  // so they are named outright.
  hgt500: {
    id: "hgt500",
    levelHpa: 500,
    contourInterval: 40,
    emphasisContours: [5840, 5880],
    range: [4252, 6284],
  },
  hgt300: { id: "hgt300", levelHpa: 300, contourInterval: 120, range: [7505, 10045] },
  hgt250: { id: "hgt250", levelHpa: 250, contourInterval: 120, range: [8598, 11646] },
  hgt200: { id: "hgt200", levelHpa: 200, contourInterval: 120, range: [10086, 13134] },
};

/** True when a field is drawn as contours rather than filled. Takes either
 * kind of id: a pressure bundle carries exactly one variable of the same
 * name, so the bundle-level and data-level questions have one answer. */
export function isPressureBundle(id: ForecastBundleId | DataVariableId): id is PressureBundleId {
  return id in PRESSURE_LEVELS;
}

/** The contour settings for a field named by the convention, or null when it
 * is not a pressure one — which is what turns the renderer's contour pass
 * off. A guess from the id string; an open session asks
 * `pressureLevelForIdentity` instead. */
export function pressureLevel(id: ForecastBundleId | DataVariableId): PressureLevelInfo | null {
  return isPressureBundle(id) ? PRESSURE_LEVELS[id] : null;
}

/** The contour settings for what a field *is*: the pressure family is the
 * `hgt` family, sea level pressure its surface member. Null for every filled
 * field, and for a height on a surface nothing is registered on. */
export function pressureLevelForIdentity(identity: VariableIdentity | null): PressureLevelInfo | null {
  if (identity === null || identity.family !== "hgt" || identity.vector) return null;
  const id = registeredBundleId(identity);
  return id !== null && isPressureBundle(id) ? PRESSURE_LEVELS[id] : null;
}

/** Human-facing name of one level. Sea level pressure is a field with a name;
 * every height is "<level> hPa <height>", built from one translated word so
 * eight near-identical strings do not have to live in the locale table. */
export function pressureLabel(id: PressureBundleId): string {
  const level = PRESSURE_LEVELS[id];
  if (level.levelHpa === null) return t("varLabelPrmsl");
  return t("varLabelHeightAtLevel").replace("{level}", String(level.levelHpa));
}

/** The instrument-panel code, English in both locales like the rest of the
 * panel: "PRMSL MSL", "HGT 500MB". */
export function pressureCode(id: PressureBundleId): string {
  const level = PRESSURE_LEVELS[id];
  return level.levelHpa === null ? "PRMSL MSL" : `HGT ${level.levelHpa}MB`;
}

/** Six legend ticks across the level's codebook range, coarsest first — the
 * same shape every other variable's legend has. Rounded to the contour
 * interval so the numbers read as chart values rather than codebook edges. */
export function pressureLegend(id: PressureBundleId): string[] {
  const { range, contourInterval } = PRESSURE_LEVELS[id];
  const [low, high] = range;
  const step = (high - low) / 6;
  const ticks: string[] = [];
  for (let index = 0; index < 6; index += 1) {
    const value = high - step / 2 - index * step;
    ticks.push(String(Math.round(value / contourInterval) * contourInterval));
  }
  return ticks;
}

/** Longitude step, in degrees, at or above which a grid is drawn with the
 * narrow contour smoothing: 0.9°, so a degree-scale grid is narrow and
 * 0.25°, 0.1° and 0.03° are not. */
export const COARSE_CONTOUR_STEP_DEGREES = 0.9;

/** Standard deviation, in grid cells, of the smoothing a plane gets before
 * it is contoured. The figure is in cells but the reason is in degrees: sea
 * level pressure is stored in 1 hPa codes and drawn every 4 hPa, so in a
 * weak gradient one code spans several cells and the raw contour is a
 * staircase along their edges, and about half a degree of Gaussian is what
 * recovers the smooth field underneath without blunting a low. Two cells is
 * that half degree on a 0.25° grid and less than it on the finer ones, so
 * they all take two; a degree-scale grid would read it as two whole degrees
 * and flatten the very systems the chart is drawn for, so it takes one —
 * still a degree of smoothing, which is as little as a cell-wide kernel can
 * ask for. The heights are quantized finer relative to their interval and
 * need less, but one figure per grid keeps every level of the family
 * reading the same way. */
export function contourSmoothingCells(longitudeStep: number): number {
  const step = Math.abs(longitudeStep);
  return Number.isFinite(step) && step >= COARSE_CONTOUR_STEP_DEGREES ? 1 : 2;
}
