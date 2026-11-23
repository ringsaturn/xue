/**
 * Heights for an ascent that was filed without any.
 *
 * A bulletin of significant levels (a TEMP's part B, and the BUFR some
 * centres make of one) carries pressure, temperature and dew point but no
 * geopotential height. Every reading in metres — the moist layers, the
 * inversions, the freezing level, the depths behind the precipitation lines
 * — needs one, and all of it follows from what the bulletin does report: the
 * ground is at the station's elevation, and the thickness between two
 * pressures is the hypsometric equation on the virtual temperature between
 * them.
 *
 * The product keeps publishing what was reported. This is the viewer's
 * reading of it, done where the ascent becomes a `Profile`.
 *
 * No DOM, no canvas: this module is arithmetic.
 */

import { freezingHeight, lapseRate } from "./column";
import { RD, SIG, hasSig, saturationMixingRatio } from "./profile";
import type { Profile } from "./profile";

/** Standard gravity, m/s². */
const G = 9.80665;
/** Two temperatures further apart than this pressure ratio say too little of
 * the air between them to measure its depth, and the filling ends at the
 * lower one. The standard surfaces are up to 1.67 apart, and an ascent filed
 * on those alone is filled whole. */
const MAX_THICKNESS_RATIO = 1.7;

/** Virtual temperature, K; the temperature itself where the dew point is
 * unreported, which is the dry air near the top. */
function virtualTemperature(t: number, td: number, p: number): number {
  const kelvin = t + 273.15;
  const w = saturationMixingRatio(td, p);
  return Number.isFinite(w) ? kelvin * (1 + 0.61 * (w / (1 + w))) : kelvin;
}

/**
 * Fill in the heights of an ascent that reports none above its ground,
 * upward from the station's elevation (m), and the derived heights the
 * publisher could not compute without them. The profile is changed in
 * place; returns whether it was.
 *
 * Nothing is filled unless the lowest level is flagged as the surface and
 * has a temperature: an ascent that starts aloft (a part C filed alone, a
 * bulletin cut short at the bottom) has no height to count up from. One
 * reported height above the ground and the ascent is left as it was filed.
 */
export function fillHeights(profile: Profile, elevation: number | null): boolean {
  const { n, p, t, td, z, sig } = profile;
  if (n === 0 || !hasSig(sig[0]!, SIG.surface) || !Number.isFinite(p[0]!) || !Number.isFinite(t[0]!)) return false;
  for (let index = 1; index < n; index += 1) if (Number.isFinite(z[index]!)) return false;
  const ground = Number.isFinite(z[0]!) ? z[0]! : elevation;
  if (ground === null || !Number.isFinite(ground)) return false;

  z[0] = ground;
  let last = 0;
  let filled = false;
  for (let index = 1; index < n; index += 1) {
    if (!Number.isFinite(p[index]!) || !Number.isFinite(t[index]!)) continue;
    if (p[last]! / p[index]! > MAX_THICKNESS_RATIO) break;
    const below = virtualTemperature(t[last]!, td[last]!, p[last]!);
    const above = virtualTemperature(t[index]!, td[index]!, p[index]!);
    const base = z[last]!;
    const depth = Math.log(p[last]! / p[index]!);
    // A wind-only level on the way: the temperature is linear in ln p
    // between its neighbours, and so is the mean up to it.
    for (let between = last + 1; between <= index; between += 1) {
      const part = Math.log(p[last]! / p[between]!);
      if (!Number.isFinite(part)) continue;
      const top = below + (above - below) * (part / depth);
      z[between] = base + ((RD * (below + top)) / 2 / G) * part;
    }
    last = index;
    filled = true;
  }
  if (!filled) return false;

  const derived = profile.derived;
  let tropopause = derived?.tropopause ?? null;
  for (let index = 0; index < n && tropopause === null; index += 1) {
    if (hasSig(sig[index]!, SIG.tropopause) && Number.isFinite(z[index]!)) tropopause = z[index]!;
  }
  profile.derived = {
    pw: derived?.pw ?? null,
    freezingLevel: derived?.freezingLevel ?? freezingHeight(profile),
    lapse850_500: derived?.lapse850_500 ?? lapseRate(profile, 850, 500),
    tropopause,
  };
  return true;
}
