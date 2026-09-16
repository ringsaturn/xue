/** Which of a run's bundles make a model column, and which frame of it to
 * read against an observed ascent.
 *
 * The skew-T draws two profiles: the sonde's, out of the sounding product,
 * and the model's, out of whatever isobaric levels the run on screen
 * happens to publish. The second is not a fixed list — GFS ships
 * tmp925/850/500, rh850/700/500 and wind925/850/250 today and something
 * else tomorrow, ECMWF ships its own set — so the panel takes what is
 * there and draws a column of however many levels that is. A run with no
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
 * laid over it: three hours, half a synoptic interval.
 *
 * The bound exists because the two axes are unrelated. A sonde is released
 * at 00 and 12 UTC and the panel shows the newest one it has; the playhead
 * is wherever the viewer left it, and a run's own frames are three- or
 * six-hourly out in the tail. Past three hours the dotted curve would be a
 * different air mass drawn as though it were the same one, which is worse
 * than no curve — so there is none, and the legend says why.
 */
export const MODEL_PROFILE_TOLERANCE_MS = 3 * 3600 * 1000;

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
