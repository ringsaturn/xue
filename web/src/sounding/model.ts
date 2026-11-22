/** Which of a run's bundles make a model column, and which frame of it to
 * read against an observed ascent.
 *
 * The skew-T draws two profiles: the sonde's, out of the sounding product,
 * and the model's, out of whatever isobaric levels the run on screen
 * happens to publish. The second is not a fixed list — GFS ships tmp, rh
 * and wind on fifteen surfaces, ECMWF on twelve, HRRR three temperatures
 * and three winds, AIFS no humidity at all — so the panel takes what is
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

/** The quantities a column is made of. An overlay beside an ascent is drawn
 * on pressure and needs the first three; a column that is the profile
 * itself also needs each level's height, to find its own ground and to say
 * how high anything on it is. */
type ModelField = "tmp" | "rh" | "wind" | "hgt";

/** One bundle of a model column: which pressure level it is on and which
 * of the three quantities it carries. */
interface ModelLevelBundle {
  id: string;
  /** Isobaric surface, hPa. */
  level: number;
  field: ModelField;
}

const FIELDS: readonly ModelField[] = ["tmp", "rh", "wind"];
const COLUMN_FIELDS: readonly ModelField[] = ["tmp", "rh", "wind", "hgt"];

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
  return levelBundles(bundleIds, FIELDS);
}

function levelBundles(bundleIds: readonly string[], fields: readonly ModelField[]): ModelLevelBundle[] {
  const found: ModelLevelBundle[] = [];
  for (const id of bundleIds) {
    const identity = identityForBundleId(id);
    if (identity === null || identity.level === null) continue;
    if (!(fields as readonly string[]).includes(identity.family)) continue;
    found.push({ id, level: identity.level, field: identity.family as ModelField });
  }
  return found.sort((a, b) => b.level - a.level || fields.indexOf(a.field) - fields.indexOf(b.field));
}

/** The fewest levels a column may have and still be offered as a profile of
 * its own, and the fewest of them that must carry humidity. Three
 * temperatures are an overlay's worth; a profile a reader is asked to take
 * for the sounding that is not there has to span the troposphere. */
export const COLUMN_MIN_LEVELS = 5;
export const COLUMN_MIN_HUMID_LEVELS = 3;

/** The surface fields a column stands on: without them it has no ground
 * (`column.ts`). */
const COLUMN_GROUND_IDS: readonly string[] = ["tmp2m", "dpt2m", "prmsl", "orog"];
/** What the panel reads beside those when the run has it: the wind at the
 * ground, and the model's own convective energy, which it computed on far
 * more levels than it publishes. */
const COLUMN_EXTRA_IDS: readonly string[] = ["wind10m", "cape", "cin"];

/** The isobaric bundles of a column that is drawn as the profile itself:
 * the overlay's, and each level's height. */
export function modelColumnBundles(bundleIds: readonly string[]): ModelLevelBundle[] {
  return levelBundles(bundleIds, COLUMN_FIELDS);
}

/** The surface bundles such a column reads, of those the run publishes. */
export function modelColumnSurfaceIds(bundleIds: readonly string[]): string[] {
  return [...COLUMN_GROUND_IDS, ...COLUMN_EXTRA_IDS].filter((id) => bundleIds.includes(id));
}

/**
 * Whether a run's levels make a profile worth showing where there is no
 * ascent: the ground fields, at least `COLUMN_MIN_LEVELS` levels with both a
 * temperature and a height, and humidity on `COLUMN_MIN_HUMID_LEVELS` of
 * them. The global runs with the full families qualify; a run with three
 * temperatures and no humidity is an overlay and nothing more.
 */
export function modelColumnPossible(bundleIds: readonly string[]): boolean {
  if (!COLUMN_GROUND_IDS.every((id) => bundleIds.includes(id))) return false;
  const fields = new Map<number, Set<ModelField>>();
  for (const bundle of modelColumnBundles(bundleIds)) {
    const at = fields.get(bundle.level) ?? new Set<ModelField>();
    at.add(bundle.field);
    fields.set(bundle.level, at);
  }
  const complete = [...fields.values()].filter((at) => at.has("tmp") && at.has("hgt"));
  return complete.length >= COLUMN_MIN_LEVELS && complete.filter((at) => at.has("rh")).length >= COLUMN_MIN_HUMID_LEVELS;
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
