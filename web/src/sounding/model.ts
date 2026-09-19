/** Which of a run's bundles make a model column, and which frame of it to
 * read against an observed ascent.
 *
 * The skew-T draws two profiles: the sonde's, out of the sounding product,
 * and the model's, out of whatever isobaric levels the run on screen
 * happens to publish. The second is not a fixed list — GFS and ECMWF ship
 * tmp, rh and wind on all eight registered surfaces today, HRRR three
 * temperatures and three winds, AIFS no humidity at all — so the panel
 * takes what is there and draws a column of however many levels that is. A run with no
 * isobaric bundle at all simply has no model profile, and the chart says
 * so rather than drawing an empty dotted line.
 *
 * Recognition is by the naming convention (`identityForBundleId`), because
 * that is all a *manifest* carries: the GRIB2 parameter block lives in each
 * bundle's own metadata, which means opening a session, which is exactly
 * what this decides whether to do. Once a session is open `identity.ts`
 * wins, as everywhere else.
 *
 * Arithmetic and tables; no DOM, no sessions, no fetching.
 */

import { identityForBundleId } from "../identity";

/** The three quantities a column needs. Height comes along when the run
 * publishes it, but a skew-T is drawn on pressure and does not need it. */
export type ModelField = "tmp" | "rh" | "wind";

/** One bundle of a model column: which pressure level it is on and which
 * of the three quantities it carries. */
export interface ModelLevelBundle {
  id: string;
  /** Isobaric surface, hPa. */
  level: number;
  field: ModelField;
}

const FIELDS: readonly ModelField[] = ["tmp", "rh", "wind"];

/**
 * The bundles of a manifest that make up a model column, ordered from the
 * ground up (descending pressure) and, within a level, temperature then
 * humidity then wind — the order a reader would name them in.
 *
 * Only isobaric members count. `tmp2m` is a surface temperature, not a
 * level of the column, and the 10 m wind is not a level either; both are
 * already meteogram rows.
 */
export function modelProfileBundles(bundleIds: readonly string[]): ModelLevelBundle[] {
  const found: ModelLevelBundle[] = [];
  for (const id of bundleIds) {
    const identity = identityForBundleId(id);
    if (identity === null || identity.level === null) continue;
    if (!(FIELDS as readonly string[]).includes(identity.family)) continue;
    found.push({ id, level: identity.level, field: identity.family as ModelField });
  }
  return found.sort(
    (a, b) => b.level - a.level || FIELDS.indexOf(a.field) - FIELDS.indexOf(b.field),
  );
}

/** The levels a column would be drawn on, deepest first — one entry per
 * distinct pressure surface the run publishes anything on. */
export function modelProfileLevels(bundleIds: readonly string[]): number[] {
  return [...new Set(modelProfileBundles(bundleIds).map((bundle) => bundle.level))];
}

/**
 * How far a model frame may be from an ascent's nominal time and still be
 * laid over it: twelve hours, one sounding interval.
 *
 * The two axes are unrelated. A sonde is released at 00 and 12 UTC and the
 * panel shows the newest one it has; the run on screen is the newest
 * cycle, whose first frame is routinely six hours after that ascent (the
 * 18Z run over the 12Z sonde), and a tighter bound left the chart with
 * one curve most of the day. So the nearest frame within a sounding
 * interval is drawn, and the legend states both times and the gap between
 * them — the reader sees exactly which forecast hour is being compared
 * with which ascent, rather than being denied the comparison.
 */
export const MODEL_PROFILE_TOLERANCE_MS = 12 * 3600 * 1000;

/**
 * The frame of an axis whose valid time is nearest `targetMs`, or null when
 * the nearest is further than `toleranceMs` — the axis is the run's own
 * valid times, in order. Ties go to the earlier frame, as every other
 * nearest-frame rule in the shell does.
 */
export function nearestFrameForTime(
  validTimes: readonly number[],
  targetMs: number,
  toleranceMs: number = MODEL_PROFILE_TOLERANCE_MS,
): number | null {
  if (validTimes.length === 0 || !Number.isFinite(targetMs)) return null;
  let best = 0;
  let bestDistance = Math.abs(validTimes[0]! - targetMs);
  for (let index = 1; index < validTimes.length; index += 1) {
    const distance = Math.abs(validTimes[index]! - targetMs);
    if (distance < bestDistance) {
      best = index;
      bestDistance = distance;
    }
  }
  return bestDistance <= toleranceMs ? best : null;
}
