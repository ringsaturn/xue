/**
 * A model column as a sounding of its own: the run's isobaric levels at one
 * cell, stood on the model's ground.
 *
 * Beside an ascent the column is an overlay and needs nothing but its levels
 * (`profileFromModel`). Where there is no sonde it is the profile itself,
 * and then it needs a bottom: the 2 m temperature and dew point and the 10 m
 * wind are the lowest point, and they sit at the surface pressure, which no
 * run publishes as a field of its own. It follows from what the runs do
 * publish — each level's geopotential height, the mean sea-level pressure
 * (the pressure at height zero) and the model's terrain height — by reading
 * the pressure at the terrain's height off that column of heights. The
 * levels the run extrapolated under the ground are then dropped, as they are
 * beside an ascent (`clipBelowSurface`).
 *
 * The profile is as coarse as the levels are: a dozen points where a sonde
 * reports a hundred and fifty. What can honestly be read off it — where it
 * freezes, the lapse between two mandatory levels — is derived here, into
 * the same `derived` block an ascent carries, so the chart and the panel
 * treat the two alike; what cannot (an inversion, a tropopause above the
 * top level) is left unsaid.
 *
 * Arithmetic only: no DOM, no sessions, no fetching.
 */

import { profileFromModel, type ModelLevel, type Profile } from "./profile";

/** What the run says about the ground at a cell, each `null` until its
 * series has been read. */
export interface ColumnSurface {
  /** 2 m temperature and dew point, °C. */
  t: number | null;
  td: number | null;
  /** 10 m wind components, m/s. */
  u: number | null;
  v: number | null;
  /** Mean sea-level pressure, hPa. */
  prmsl: number | null;
  /** The model's terrain height at the cell, m. */
  orog: number | null;
}

/** A column ready to be drawn. */
export interface ModelColumn {
  profile: Profile;
  /** The surface pressure the column stands on, hPa, or null when the run
   * gave too little to place the ground. */
  pSurface: number | null;
  /** How many isobaric levels are above the ground — what the chart says
   * its resolution is. */
  levels: number;
  /** The highest level, hPa, or null for a column of none. */
  top: number | null;
}

/** A level this close above the ground is the ground: the 2 m point takes
 * its place rather than sharing a pixel with it. */
const GROUND_CLEARANCE_HPA = 1;

/**
 * The pressure at the model's terrain height, hPa.
 *
 * Every level's geopotential height is a (height, pressure) pair, and the
 * mean sea-level pressure is one more, at height zero. Between two
 * neighbouring pairs the logarithm of pressure is linear in height to well
 * within a hectopascal or two — the hypsometric equation with the layer's
 * own mean temperature — so the surface pressure is read off at the
 * terrain's height. Terrain below the lowest pair or above the highest is
 * extrapolated from the nearest two. `null` without a terrain height or
 * with fewer than two pairs.
 */
export function surfacePressure(
  levels: readonly { p: number; z?: number | null }[],
  prmsl: number | null,
  orog: number | null,
): number | null {
  if (orog === null || !Number.isFinite(orog)) return null;
  const pairs: { z: number; logP: number }[] = [];
  if (prmsl !== null && Number.isFinite(prmsl) && prmsl > 0) pairs.push({ z: 0, logP: Math.log(prmsl) });
  for (const level of levels) {
    if (level.z === undefined || level.z === null || !Number.isFinite(level.z)) continue;
    if (!Number.isFinite(level.p) || level.p <= 0) continue;
    pairs.push({ z: level.z, logP: Math.log(level.p) });
  }
  pairs.sort((a, b) => a.z - b.z);
  if (pairs.length < 2) return null;
  // The pair at or above the terrain, kept inside the list so the pair
  // under it exists: below the lowest that is the lowest two, above the
  // highest the highest two.
  let upper = pairs.findIndex((pair) => pair.z >= orog);
  if (upper < 0) upper = pairs.length - 1;
  if (upper === 0) upper = 1;
  const below = pairs[upper - 1]!;
  const above = pairs[upper]!;
  if (above.z === below.z) return Math.exp(below.logP);
  const weight = (orog - below.z) / (above.z - below.z);
  return Math.exp(below.logP + weight * (above.logP - below.logP));
}

/** The height at which the profile first cools to 0 °C on the way up, m, or
 * null when it starts at or below freezing or never reaches it. Linear in
 * height between the two levels that bracket it. */
export function freezingHeight(profile: Profile): number | null {
  let lower = -1;
  for (let index = 0; index < profile.n; index += 1) {
    const t = profile.t[index]!;
    const z = profile.z[index]!;
    if (!Number.isFinite(t) || !Number.isFinite(z)) continue;
    if (lower < 0) {
      if (t <= 0) return null;
      lower = index;
      continue;
    }
    if (t <= 0) {
      const t0 = profile.t[lower]!;
      const z0 = profile.z[lower]!;
      return z0 + ((0 - t0) / (t - t0)) * (z - z0);
    }
    lower = index;
  }
  return null;
}

/** The mean lapse rate between two levels the profile carries, K/km —
 * positive where it cools with height — or null unless both report a
 * temperature and a height. */
export function lapseRate(profile: Profile, lowerHpa: number, upperHpa: number): number | null {
  const at = (hpa: number): number => {
    for (let index = 0; index < profile.n; index += 1) {
      if (profile.p[index] === hpa) return index;
    }
    return -1;
  };
  const lower = at(lowerHpa);
  const upper = at(upperHpa);
  if (lower < 0 || upper < 0) return null;
  const depth = profile.z[upper]! - profile.z[lower]!;
  const cooling = profile.t[lower]! - profile.t[upper]!;
  if (!Number.isFinite(depth) || !Number.isFinite(cooling) || depth <= 0) return null;
  return (cooling / depth) * 1000;
}

/**
 * Stand a run's levels on its ground.
 *
 * The levels at or under the surface pressure go, the 2 m / 10 m point goes
 * in at that pressure and the terrain's height, and the two numbers a coarse
 * column can still give are derived. With no surface pressure to be had the
 * levels are kept as they are and the column has no ground point; with no
 * 2 m temperature yet it starts at its lowest level above the ground.
 */
export function modelColumn(levels: readonly ModelLevel[], surface: ColumnSurface): ModelColumn {
  const pSurface = surfacePressure(levels, surface.prmsl, surface.orog);
  const above = levels.filter(
    (level) => Number.isFinite(level.p) && (pSurface === null || level.p < pSurface - GROUND_CLEARANCE_HPA),
  );
  const rows: ModelLevel[] = [...above];
  if (pSurface !== null && surface.t !== null) {
    rows.push({ p: pSurface, t: surface.t, td: surface.td, z: surface.orog, u: surface.u, v: surface.v });
  }
  const profile = profileFromModel(rows);
  profile.derived = {
    freezingLevel: freezingHeight(profile),
    pw: null,
    lapse850_500: lapseRate(profile, 850, 500),
    tropopause: null,
  };
  let top: number | null = null;
  for (const level of above) top = top === null ? level.p : Math.min(top, level.p);
  return { profile, pSurface, levels: above.length, top };
}
