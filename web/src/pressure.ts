import { linearCodebookRange } from "./codebooks";
import { t } from "./i18n";
import type { VariableIdentity } from "./identity";
import type { DataVariableId, ForecastBundleId, PressureBundleId } from "./manifest";
import { REGISTRY } from "./registry";

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
 * The ids, the codebook ranges and the contour intervals are the
 * registry's (`registry.ts`), so none of them can move on one side alone;
 * `tests/web/pressure.test.ts` holds every contour half a code off.
 */
interface PressureLevelInfo {
  id: PressureBundleId;
  /** Isobaric surface in hPa; null for mean sea level pressure. */
  levelHpa: number | null;
  /** Contour interval in the variable's own unit (hPa or gpm). */
  contourInterval: number;
  /** A regular sub-family drawn heavier — every 20 hPa on a surface chart. */
  emphasisInterval?: number;
  /** Particular contours drawn heavier, when no interval expresses them. */
  emphasisContours?: readonly number[];
  /** Codebook coverage (the registry's), which is also the fill palette's range. */
  range: readonly [number, number];
}

export type { PressureBundleId };

export const PRESSURE_BUNDLE_IDS: readonly PressureBundleId[] = REGISTRY.families.pressure;

/** Each level's contours, from the registry: the intervals are the
 * encoder's, which placed every codebook so its contours land half a code
 * off. 4 hPa is the surface-chart interval and every fifth line (20 hPa)
 * the grid the chart is read on; 5880 and 5840 gpm — "588" and "584" —
 * are the two lines the subtropical high is read by, adjacent contours
 * rather than a sub-family, so they are named outright. */
export const PRESSURE_LEVELS = Object.fromEntries(
  PRESSURE_BUNDLE_IDS.map((id) => {
    const contours: { interval: number; emphasisInterval?: number; emphasisContours?: readonly number[] } = REGISTRY.contours[id];
    const info: PressureLevelInfo = {
      id,
      levelHpa: id === "prmsl" ? null : Number(id.slice("hgt".length)),
      contourInterval: contours.interval,
      ...(contours.emphasisInterval === undefined ? {} : { emphasisInterval: contours.emphasisInterval }),
      ...(contours.emphasisContours === undefined ? {} : { emphasisContours: contours.emphasisContours }),
      range: linearCodebookRange(id),
    };
    return [id, info];
  }),
) as Record<PressureBundleId, PressureLevelInfo>;

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
  // The family's ids are its level's, so the identity names its bundle
  // without the variable table (which reads this module).
  const id = identity.level === null ? "prmsl" : `hgt${identity.level}`;
  return isPressureBundle(id) ? PRESSURE_LEVELS[id] : null;
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
