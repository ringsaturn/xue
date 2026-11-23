/**
 * What a profile means, as numbers the chart can shade and the panel can put
 * into words: the surface level, a lifted parcel with its LFC, EL, CAPE and
 * CIN, the near-saturated layers, the inversions, the strongest wind and what
 * all of it would mean for precipitation.
 *
 * Everything here is read off a `Profile` and the thermodynamics of
 * `profile.ts`, and the parcel is lifted with the same `lcl`, `dryAdiabat`
 * and `moistAdiabatFrom` the chart's background curves come from, so a
 * shaded area always lies between two curves the reader can see. Every
 * function tolerates gaps, an empty ascent and an ascent without dew points,
 * and answers `null` or `[]` rather than a `NaN`.
 *
 * No DOM, no canvas: this module is arithmetic.
 */

import {
  KAPPA,
  RD,
  dryAdiabat,
  interpolateAtPressure,
  lcl,
  mixingRatioLine,
  moistAdiabatFrom,
  saturationMixingRatio,
} from "./profile";
import type { ParcelPath, Profile } from "./profile";

/** Depth of the mixed layer the default parcel averages over, hPa. */
const MIXED_DEPTH_HPA = 100;
/** Moist layers closer together than this, hPa, are one layer with a thin gap. */
const MOIST_MERGE_HPA = 15;
/** Two consecutive reporting levels further apart than this pressure ratio,
 * with levels between them silent on the field, are a hole in the record, not
 * a run to bridge: the product thins to about 3 % pressure spacing plus
 * flagged levels, so a quarter is a sensor outage. Without silent levels
 * between them they are a sparse ascent (one filed on the standard surfaces
 * only, up to 1.67 apart), and are bridged whatever their spacing. */
const MAX_LEVEL_RATIO = 1.25;

function potentialTemperature(tCelsius: number, p: number): number {
  return (tCelsius + 273.15) * (1000 / p) ** KAPPA;
}

function finite(...values: number[]): boolean {
  for (const value of values) if (!Number.isFinite(value)) return false;
  return true;
}

/** The pressure where a quantity linear in `ln p` between (p1, a1) and
 * (p2, a2) takes the value `target`. */
function crossingPressure(
  p1: number,
  a1: number,
  p2: number,
  a2: number,
  target: number,
): number {
  const weight = (a1 - target) / (a1 - a2);
  return Math.exp(Math.log(p1) + weight * (Math.log(p2) - Math.log(p1)));
}

function heightAt(profile: Profile, p: number): number {
  return interpolateAtPressure(profile, p, profile.z);
}

// --- Surface ----------------------------------------------------------------

/** The lowest level that reports a pressure, a temperature and a dew point. */
export interface SurfaceLevel {
  index: number;
  p: number;
  /** Geopotential height as reported, m; `NaN` when the level carries none. */
  z: number;
  t: number;
  td: number;
}

/** The level a parcel starts from, or `null` when no level reports all
 * three of p, T and Td. */
export function surfaceLevel(profile: Profile): SurfaceLevel | null {
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const t = profile.t[index]!;
    const td = profile.td[index]!;
    if (finite(p, t, td)) return { index, p, z: profile.z[index]!, t, td };
  }
  return null;
}

// --- The lifted parcel ------------------------------------------------------

export type ParcelKind = "mixed" | "surface";

export interface LiftedParcel {
  /** Which parcel was actually lifted (a "mixed" request falls back to
   * "surface" when the lowest 100 hPa has fewer than two levels with both T
   * and Td). */
  kind: ParcelKind;
  /** The parcel's starting state, at the surface pressure. */
  p0: number;
  t0: number;
  td0: number;
  /** The lifted path, from p0 upward: dry adiabat to the LCL, pseudoadiabat
   * above. Sampled at the profile's own levels at or above p0, with extra
   * vertices exactly at the LCL, the LFC and the EL (and at every other
   * crossing of the environment), so a polygon between this and
   * `environment` closes on them. Strictly descending pressure. */
  path: ParcelPath;
  /** Environment temperature at each `path.p`, °C, linear in ln p between
   * reporting levels; `NaN` where the ascent has no temperature to
   * interpolate. Same length as `path.p`. */
  environment: Float64Array;
  /** Level of free convection and equilibrium level, hPa, or `null` when the
   * parcel is never warmer than the environment above its LCL. */
  lfc: number | null;
  el: number | null;
  /** J/kg, >= 0: positive area between LFC and EL. 0 when there is no LFC. */
  cape: number;
  /** J/kg, <= 0: negative area between p0 and the LFC. `null` when there is
   * no LFC. */
  cin: number | null;
}

/** A parcel's starting state, or `null` when the ascent cannot supply one. */
interface ParcelStart {
  kind: ParcelKind;
  t0: number;
  td0: number;
}

/**
 * The mixed-layer parcel: the pressure-weighted mean potential temperature
 * and water-vapour mixing ratio of the lowest 100 hPa, which is what an
 * afternoon of turbulent mixing would leave the boundary layer with, and so a
 * fairer guess at the air a thunderstorm feeds on than one thermometer at
 * screen height. The means are trapezoids in pressure over the levels that
 * report both T and Td (a level missing either is stepped over, since a mean
 * of the layer is still a mean without it), with the layer's top interpolated
 * in ln p between the levels that bracket it. `null` when fewer than two such
 * levels lie in the layer.
 */
function mixedStart(
  profile: Profile,
  surface: SurfaceLevel,
): ParcelStart | null {
  const p0 = surface.p;
  const top = p0 - MIXED_DEPTH_HPA;
  const ps: number[] = [];
  const thetas: number[] = [];
  const ws: number[] = [];
  let above = -1;
  for (let index = surface.index; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const t = profile.t[index]!;
    const td = profile.td[index]!;
    if (!finite(p, t, td) || p > p0) continue;
    if (p < top) {
      above = index;
      break;
    }
    ps.push(p);
    thetas.push(potentialTemperature(t, p));
    ws.push(saturationMixingRatio(td, p));
  }
  if (ps.length < 2) return null;
  const lastP = ps[ps.length - 1]!;
  if (above >= 0 && lastP > top) {
    const pa = profile.p[above]!;
    const weight =
      (Math.log(lastP) - Math.log(top)) / (Math.log(lastP) - Math.log(pa));
    const thetaA = potentialTemperature(profile.t[above]!, pa);
    const wA = saturationMixingRatio(profile.td[above]!, pa);
    thetas.push(
      thetas[thetas.length - 1]! +
        weight * (thetaA - thetas[thetas.length - 1]!),
    );
    ws.push(ws[ws.length - 1]! + weight * (wA - ws[ws.length - 1]!));
    ps.push(top);
  }
  let thetaSum = 0;
  let wSum = 0;
  for (let index = 1; index < ps.length; index += 1) {
    const dp = ps[index - 1]! - ps[index]!;
    thetaSum += ((thetas[index - 1]! + thetas[index]!) / 2) * dp;
    wSum += ((ws[index - 1]! + ws[index]!) / 2) * dp;
  }
  const depth = ps[0]! - ps[ps.length - 1]!;
  if (!(depth > 0)) return null;
  const t0 = dryAdiabat(thetaSum / depth, p0);
  // The mean of saturated-or-drier levels cannot be supersaturated in
  // principle; the clamp only absorbs rounding, so the LCL is never below p0.
  const td0 = Math.min(mixingRatioLine((wSum / depth) * 1000, p0), t0);
  if (!finite(t0, td0)) return null;
  return { kind: "mixed", t0, td0 };
}

/**
 * Lift a parcel from the surface pressure and measure it against the
 * environment.
 *
 * `kind` "mixed" (the default) lifts the mean of the lowest 100 hPa (see
 * `mixedStart`), falling back to the surface parcel when the layer has fewer
 * than two levels with T and Td; "surface" lifts the surface level's own T
 * and Td. The path is the profile's own levels from the surface up with
 * the LCL inserted, the dry adiabat below it and `moistAdiabatFrom` the LCL
 * above it, so the curve sits exactly on the chart's background
 * pseudoadiabats.
 *
 * CAPE and CIN are `Rd · ∫ (Tp − Te) d(ln p)` by the trapezoid rule in ln p
 * over the path's vertices, on plain temperature with no virtual-temperature
 * correction: the number is then exactly the area the chart shades between
 * the two curves it draws, which a reader can check by eye, at the price of
 * running a little low against the virtual-temperature CAPE other services
 * quote.
 *
 * Every place the parcel crosses the environment becomes a vertex. The
 * crossing is first placed by linear interpolation of `Tp − Te` in ln p
 * between the two vertices around it and then refined against the true
 * parcel curve (the environment is linear in ln p between vertices, the
 * pseudoadiabat is not), so at the inserted vertex `path.t` and
 * `environment` agree to well under a millikelvin. The LFC is the lowest
 * crossing at or above the LCL into positive buoyancy — the LCL itself if the
 * parcel is already warmer there, or the start level if it is warmer all the
 * way from the ground — and the EL the top of the last positive layer (or the
 * last level with an environment temperature, if the parcel is still warmer
 * there). CAPE is all positive area between them, CIN all negative area
 * below the LFC.
 */
export function liftParcel(
  profile: Profile,
  kind: ParcelKind = "mixed",
): LiftedParcel | null {
  const surface = surfaceLevel(profile);
  if (!surface) return null;
  const p0 = surface.p;
  const start: ParcelStart = (kind === "mixed"
    ? mixedStart(profile, surface)
    : null) ?? { kind: "surface", t0: surface.t, td0: surface.td };
  const { t0, td0 } = start;
  const condensation = lcl(t0, td0, p0);
  if (!condensation) return null;
  const theta = potentialTemperature(t0, p0);
  const parcelAt = (p: number): number =>
    p >= condensation.p
      ? dryAdiabat(theta, p)
      : moistAdiabatFrom(condensation.p, condensation.t, p);

  // The profile's levels from the surface up, with the LCL where it falls.
  const levels: number[] = [];
  const push = (p: number): void => {
    const last = levels[levels.length - 1];
    if (last === undefined || p < last) levels.push(p);
  };
  for (let index = surface.index; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    if (!Number.isFinite(p)) continue;
    if (condensation.p < p0 && condensation.p > p) push(condensation.p);
    push(p);
  }
  if (levels.length === 0) push(p0);

  const ps: number[] = [];
  const tps: number[] = [];
  const tes: number[] = [];
  /** Buoyancy for the LFC/EL walk: Tp − Te, but exactly 0 at a crossing. */
  const bs: number[] = [];
  for (const p of levels) {
    const tp = parcelAt(p);
    const te = interpolateAtPressure(profile, p, profile.t);
    const b = tp - te;
    const index = ps.length - 1;
    if (index >= 0 && Number.isFinite(b) && Number.isFinite(bs[index]!)) {
      const bLow = bs[index]!;
      if ((bLow < 0 && b > 0) || (bLow > 0 && b < 0)) {
        const crossing = refineCrossing(
          ps[index]!,
          tes[index]!,
          bLow,
          p,
          te,
          b,
          parcelAt,
        );
        if (crossing !== null) {
          ps.push(crossing.p);
          tps.push(crossing.tp);
          tes.push(crossing.te);
          bs.push(0);
        } else {
          // The crossing rounds onto an end: make that end the zero.
          bs[index] = 0;
        }
      }
    }
    ps.push(p);
    tps.push(tp);
    tes.push(te);
    bs.push(b);
  }

  const path: ParcelPath = {
    p: Float64Array.from(ps),
    t: Float64Array.from(tps),
    lcl: condensation,
  };
  const environment = Float64Array.from(tes);
  const result: LiftedParcel = {
    kind: start.kind,
    p0,
    t0,
    td0,
    path,
    environment,
    lfc: null,
    el: null,
    cape: 0,
    cin: null,
  };

  // The walk only covers the contiguous run of vertices with a buoyancy;
  // above the last environment temperature there is nothing to compare.
  let m = 0;
  while (m < bs.length && Number.isFinite(bs[m]!)) m += 1;
  let lclIndex = -1;
  for (let index = 0; index < m; index += 1) {
    if (ps[index]! <= condensation.p) {
      lclIndex = index;
      break;
    }
  }
  if (lclIndex < 0) return result;

  let lfcIndex = -1;
  if (bs[lclIndex]! > 0) {
    lfcIndex = lclIndex;
    let warmFromGround = true;
    for (let index = 0; index <= lclIndex; index += 1)
      if (!(bs[index]! > 0)) warmFromGround = false;
    if (warmFromGround) lfcIndex = 0;
  } else {
    for (let index = lclIndex + 1; index < m; index += 1) {
      if (bs[index]! > 0 && bs[index - 1]! <= 0) {
        lfcIndex = index - 1;
        break;
      }
    }
  }
  if (lfcIndex < 0) return result;
  let lastWarm = -1;
  for (let index = lfcIndex; index < m; index += 1)
    if (bs[index]! > 0) lastWarm = index;
  if (lastWarm < 0) return result;
  const elIndex = lastWarm + 1 < m ? lastWarm + 1 : lastWarm;
  if (elIndex <= lfcIndex) return result;

  let cape = 0;
  let cin = 0;
  for (let index = 1; index < m; index += 1) {
    const b1 = tps[index - 1]! - tes[index - 1]!;
    const b2 = tps[index]! - tes[index]!;
    const dlnp = Math.log(ps[index - 1]! / ps[index]!);
    if (index <= lfcIndex) {
      cin += (RD * (Math.min(b1, 0) + Math.min(b2, 0)) * dlnp) / 2;
    } else if (index <= elIndex) {
      cape += (RD * (Math.max(b1, 0) + Math.max(b2, 0)) * dlnp) / 2;
    }
  }
  result.lfc = ps[lfcIndex]!;
  result.el = ps[elIndex]!;
  result.cape = cape + 0;
  // `+ 0` turns a −0 from an all-positive column into a plain 0.
  result.cin = cin + 0;
  return result;
}

/**
 * Where the parcel meets the environment between two vertices whose
 * buoyancies `b1` (at `p1`) and `b2` (at `p2`) have opposite signs. Seeded by
 * linear interpolation in ln p and refined by the Illinois variant of regula
 * falsi on the true parcel curve against the environment's straight line;
 * `null` when the answer rounds onto either end.
 */
function refineCrossing(
  p1: number,
  te1: number,
  b1: number,
  p2: number,
  te2: number,
  b2: number,
  parcelAt: (p: number) => number,
): { p: number; tp: number; te: number } | null {
  const x1 = Math.log(p1);
  const x2 = Math.log(p2);
  const envAt = (x: number): number =>
    te1 + ((x - x1) / (x2 - x1)) * (te2 - te1);
  let a = x1;
  let fa = b1;
  let c = x2;
  let fc = b2;
  let x = a + (fa / (fa - fc)) * (c - a);
  let side = 0;
  for (let iteration = 0; iteration < 40; iteration += 1) {
    const fx = parcelAt(Math.exp(x)) - envAt(x);
    if (!Number.isFinite(fx) || Math.abs(fx) < 1e-7) break;
    if (Math.sign(fx) === Math.sign(fa)) {
      a = x;
      fa = fx;
      if (side === -1) fc /= 2;
      side = -1;
    } else {
      c = x;
      fc = fx;
      if (side === 1) fa /= 2;
      side = 1;
    }
    x = a + (fa / (fa - fc)) * (c - a);
  }
  const p = Math.exp(x);
  if (!(p < p1 && p > p2)) return null;
  return { p, tp: parcelAt(p), te: envAt(x) };
}

// --- Moist layers -----------------------------------------------------------

/** A layer where the air is near saturation: temperature minus dew point
 * <= `spread`. */
export interface MoistLayer {
  pBottom: number;
  pTop: number;
  /** Heights interpolated from `profile.z`, m; `NaN` if unreported. */
  zBottom: number;
  zTop: number;
}

/**
 * Near-saturated layers, from the ground up: contiguous runs of levels whose
 * dew-point depression is at most `spread` (3 °C by default), the usual
 * reading of "probably cloud" off a sonde. A run's edges are placed where the
 * depression crosses the threshold, linear in ln p between the levels on
 * either side. The runs are found on the levels that report both T and Td,
 * as if the others were not in the ascent — sondes interleave wind-only
 * levels that are no gap in the humidity record — except that two reporting
 * neighbours more than `MAX_LEVEL_RATIO` apart in pressure, with silent levels
 * between them, are a hole, which ends a run at the lower one. Only pressures at or below the surface down to `pTop`
 * (300 hPa by default) count, because above that the sonde's humidity sensor
 * and the ice/water ambiguity make the depression say little about cloud.
 * Runs separated by less than 15 hPa merge, and layers thinner than
 * `minDepthHpa` (30 hPa) are dropped.
 */
export function moistLayers(
  profile: Profile,
  options: { spread?: number; minDepthHpa?: number; pTop?: number } = {},
): MoistLayer[] {
  const spread = options.spread ?? 3;
  const minDepth = options.minDepthHpa ?? 30;
  const pTop = options.pTop ?? 300;
  const runs: { bottom: number; top: number }[] = [];
  let bottom: number | null = null;
  let prevP = Number.NaN;
  let prevS = Number.NaN;
  let prevValid = false;
  let silent = false;
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const s = profile.t[index]! - profile.td[index]!;
    // A level without T or Td (a wind-only significant level, most often)
    // says nothing about saturation: the series is the reporting levels alone.
    if (!finite(p, s)) {
      if (Number.isFinite(p)) silent = true;
      continue;
    }
    if (prevValid && silent && prevP / p > MAX_LEVEL_RATIO) {
      if (bottom !== null) runs.push({ bottom, top: prevP });
      bottom = null;
      prevValid = false;
    }
    silent = false;
    if (p < pTop) {
      if (bottom !== null && prevValid) {
        // The run is cut at pTop unless the air dries out before it.
        const edge =
          s > spread ? crossingPressure(prevP, prevS, p, s, spread) : pTop;
        runs.push({ bottom, top: Math.max(edge, pTop) });
      } else if (prevValid && prevS > spread && s <= spread) {
        const edge = crossingPressure(prevP, prevS, p, s, spread);
        if (edge >= pTop) runs.push({ bottom: edge, top: pTop });
      }
      bottom = null;
      prevValid = false;
      break;
    }
    const moist = s <= spread;
    if (!prevValid) {
      if (moist) bottom = p;
    } else if (bottom === null && moist) {
      bottom = crossingPressure(prevP, prevS, p, s, spread);
    } else if (bottom !== null && !moist) {
      runs.push({ bottom, top: crossingPressure(prevP, prevS, p, s, spread) });
      bottom = null;
    }
    prevP = p;
    prevS = s;
    prevValid = true;
  }
  if (bottom !== null && prevValid) runs.push({ bottom, top: prevP });

  const merged: { bottom: number; top: number }[] = [];
  for (const run of runs) {
    const last = merged[merged.length - 1];
    if (last && last.top - run.bottom < MOIST_MERGE_HPA) last.top = run.top;
    else merged.push({ ...run });
  }
  return merged
    .filter((run) => run.bottom - run.top >= minDepth)
    .map((run) => ({
      pBottom: run.bottom,
      pTop: run.top,
      zBottom: heightAt(profile, run.bottom),
      zTop: heightAt(profile, run.top),
    }));
}

// --- Inversions -------------------------------------------------------------

/** A layer where temperature rises with height. */
export interface Inversion {
  pBottom: number;
  pTop: number;
  /** Heights interpolated from `profile.z`, m; `NaN` if unreported. */
  zBottom: number;
  zTop: number;
  /** T(top) − T(bottom), °C, > 0. */
  strength: number;
  /** True when the layer starts at the lowest temperature-reporting level. */
  surfaceBased: boolean;
}

/**
 * Inversions, from the ground up: runs of consecutive temperature-reporting
 * levels over which the temperature rises with height, from the level where
 * it starts rising to the local maximum that ends the rise. Such a layer caps
 * mixing from below — it is where haze, fog and the morning's pollution sit,
 * and what a parcel has to punch through. The runs are found on the levels
 * that report a temperature, as if the others were not in the ascent, except
 * that two reporting neighbours more than `MAX_LEVEL_RATIO` apart in pressure,
 * with silent levels between them, are a hole that ends a run at the lower
 * one; only pressures at or
 * above `pTop` (500 hPa by default) are searched, so the stratosphere's
 * permanent warming upward is left out; and layers warming by less than
 * `minStrength` (1 °C) are dropped as noise.
 */
export function inversions(
  profile: Profile,
  options: { minStrength?: number; pTop?: number } = {},
): Inversion[] {
  const minStrength = options.minStrength ?? 1;
  const pTop = options.pTop ?? 500;
  const found: Inversion[] = [];
  let lowest = -1;
  let prev = -1;
  let runBottom = -1;
  let silent = false;
  const close = (top: number): void => {
    if (runBottom < 0) return;
    const strength = profile.t[top]! - profile.t[runBottom]!;
    if (strength >= minStrength) {
      const pBottom = profile.p[runBottom]!;
      const pTopLevel = profile.p[top]!;
      found.push({
        pBottom,
        pTop: pTopLevel,
        zBottom: heightAt(profile, pBottom),
        zTop: heightAt(profile, pTopLevel),
        strength,
        surfaceBased: runBottom === lowest,
      });
    }
    runBottom = -1;
  };
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const t = profile.t[index]!;
    // A level without a temperature is not in the series at all.
    if (!finite(p, t)) {
      if (Number.isFinite(p)) silent = true;
      continue;
    }
    if (prev >= 0 && silent && profile.p[prev]! / p > MAX_LEVEL_RATIO) {
      close(prev);
      prev = -1;
    }
    silent = false;
    if (p < pTop) break;
    if (lowest < 0) lowest = index;
    if (prev >= 0) {
      if (t > profile.t[prev]!) {
        if (runBottom < 0) runBottom = prev;
      } else {
        close(prev);
      }
    }
    prev = index;
  }
  if (prev >= 0) close(prev);
  return found;
}

// --- Wind -------------------------------------------------------------------

export interface MaxWind {
  p: number;
  /** m; the level's own height, or interpolated when the level reports
   * none, `NaN` when neither is possible. */
  z: number;
  ws: number;
  /** Degrees the wind comes from; `NaN` if the level reports no direction. */
  wd: number;
}

/** The strongest reported wind in the ascent — the lowest such level on a
 * tie — or `null` with no wind at all. */
export function maxWind(profile: Profile): MaxWind | null {
  let best = -1;
  for (let index = 0; index < profile.n; index += 1) {
    const ws = profile.ws[index]!;
    if (!finite(profile.p[index]!, ws)) continue;
    if (best < 0 || ws > profile.ws[best]!) best = index;
  }
  if (best < 0) return null;
  const p = profile.p[best]!;
  const z = profile.z[best]!;
  return {
    p,
    z: Number.isFinite(z) ? z : heightAt(profile, p),
    ws: profile.ws[best]!,
    wd: profile.wd[best]!,
  };
}

// --- Precipitation ------------------------------------------------------------

/** Standard gravity, m/s². */
const G = 9.80665;
/** A moist layer at least this thick, m, holds cloud enough to rain or snow
 * steadily: nimbostratus is a couple of kilometres deep and more, and the
 * stratus under that gives drizzle or a few flakes. */
const PRECIPITATING_DEPTH_M = 2000;
/** A moist layer whose base is further above the ground than this, m, is
 * middle or high cloud: however thick, what falls from it is not what the
 * ground gets. */
const PRECIPITATING_BASE_M = 3000;
/** A moist layer whose base is this far above the ground, m, over air this
 * far from saturation on average, °C: part of what falls out of it
 * evaporates on the way. */
const EVAPORATION_BASE_M = 1000;
const EVAPORATION_SPREAD = 7;
/** Air above freezing next to the ground shallower than this, m, does not
 * melt all of the snow that falls through it. */
const MELTING_DEPTH_M = 500;
/** How warm a layer aloft has to be, °C, to melt what falls into it over a
 * frozen surface: through a warm nose cooler than this a flake comes out a
 * flake. */
const WARM_NOSE_C = 1;
/** How cold the ground has to be, °C, for that melted water to freeze on
 * it: within half a degree of zero under a warm nose it is rain. */
const GLAZE_GROUND_C = -0.5;
/** The pressure, hPa, above which the temperature is not asked about: what
 * reaches the ground forms under it, and `moistLayers` stops there too. */
const PRECIPITATION_TOP_HPA = 300;

/** What would reach the ground: `mix` is rain with snow or wet snow, and
 * `freezing` is freezing rain or ice pellets, which a temperature profile
 * alone does not tell apart. */
export type PrecipitationPhase = "rain" | "mix" | "snow" | "freezing";

/**
 * What a profile says about precipitation: conditions, not a forecast. A
 * profile holds the moisture, the stability and the temperatures something
 * would fall through, and nothing of the lift that decides whether anything
 * falls — so every field answers "if it precipitated".
 */
export interface PrecipitationReading {
  /** Thickness, m, of the thickest moist layer low enough to reach the
   * ground with what falls from it (its base within 3 km of the ground);
   * 0 with no such layer, or where the heights cannot say. */
  depth: number;
  /** Whether that layer is thick enough for steady rain or snow. */
  deep: boolean;
  /** Whether such a layer stands well above the ground over dry air, so that
   * part of what falls evaporates before it lands. */
  evaporates: boolean;
  /** What would reach the ground, or `null` where the temperatures near the
   * ground cannot say. */
  phase: PrecipitationPhase | null;
  /** How deep the air above freezing next to the ground is, m: 0 over a
   * frozen surface, `NaN` where `phase` is `null`. */
  meltingDepth: number;
}

/** The lowest level that reports a pressure and a temperature: the ground,
 * as far as what falls is concerned. -1 for an ascent with none. */
function groundIndex(profile: Profile): number {
  for (let index = 0; index < profile.n; index += 1) {
    if (finite(profile.p[index]!, profile.t[index]!)) return index;
  }
  return -1;
}

/**
 * The profile's heights with the ground's filled in. A station's surface
 * level is routinely filed without a height, and every depth here is
 * measured from it: the first reported height above, less the thickness of
 * the air between the two (the hypsometric equation on the mean of their
 * temperatures), stands in for it. The array is the profile's own where the
 * ground already has a height, or nothing above it has one.
 */
function heightsFromGround(profile: Profile, ground: number): Float64Array {
  if (ground < 0 || Number.isFinite(profile.z[ground]!)) return profile.z;
  const p0 = profile.p[ground]!;
  const t0 = profile.t[ground]!;
  for (let index = ground + 1; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const z = profile.z[index]!;
    if (!finite(p, z)) continue;
    const t = profile.t[index]!;
    const mean = (Number.isFinite(t) ? (t0 + t) / 2 : t0) + 273.15;
    const filled = Float64Array.from(profile.z);
    filled[ground] = z - ((RD * mean) / G) * Math.log(p0 / p);
    return filled;
  }
  return profile.z;
}

/** Whether the air between the ground and a pressure is dry on the whole:
 * its dew-point depression, averaged over that depth in pressure, is at
 * least `EVAPORATION_SPREAD`. One dry level in a moist column is a level,
 * not the air a raindrop falls through. */
function dryBelow(profile: Profile, ground: number, pBase: number): boolean {
  let sum = 0;
  let total = 0;
  let below = -1;
  for (let index = ground; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const spread = profile.t[index]! - profile.td[index]!;
    if (!finite(p, spread)) continue;
    if (below >= 0) {
      const belowP = profile.p[below]!;
      const belowSpread = profile.t[below]! - profile.td[below]!;
      // The last stretch ends at the base, not at the level above it.
      const top = Math.max(p, pBase);
      const topSpread = belowSpread + ((spread - belowSpread) * (belowP - top)) / (belowP - p);
      sum += ((belowSpread + topSpread) / 2) * (belowP - top);
      total += belowP - top;
    }
    if (p <= pBase) break;
    below = index;
  }
  return total > 0 && sum / total >= EVAPORATION_SPREAD;
}

/** The phase at the ground by the temperatures on the way down from
 * `ceiling`, the pressure at the top of the cloud that would precipitate:
 * frozen all the way is snow, a warm layer over a frozen surface melts it
 * and leaves it to freeze again, and above freezing at the ground it is the
 * depth of that warm air that decides between rain and a mixture. Air above
 * the cloud is not on the way: the inversion that caps a winter stratus is
 * warm, and nothing falls through it. */
function phaseAtGround(
  profile: Profile,
  ground: number,
  z: Float64Array,
  ceiling: number,
): Pick<PrecipitationReading, "phase" | "meltingDepth"> {
  const unknown = { phase: null, meltingDepth: Number.NaN };
  if (ground < 0) return unknown;
  let last = ground;
  let warmestAloft = Number.NEGATIVE_INFINITY;
  let freezing = Number.NaN;
  for (let index = ground + 1; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const t = profile.t[index]!;
    if (!finite(p, t)) continue;
    if (p < ceiling) break;
    warmestAloft = Math.max(warmestAloft, t);
    const below = profile.t[last]!;
    // The first fall through zero from the ground up: the top of the warm
    // air the ground stands in, when it stands in any.
    if (Number.isNaN(freezing) && below > 0 && t <= 0) freezing = crossingPressure(profile.p[last]!, below, p, t, 0);
    last = index;
  }
  // One temperature is the ground's and says nothing of the way down.
  if (last === ground) return unknown;
  const groundT = profile.t[ground]!;
  if (groundT <= 0) {
    if (warmestAloft < WARM_NOSE_C) return { phase: "snow", meltingDepth: 0 };
    return { phase: groundT <= GLAZE_GROUND_C ? "freezing" : "rain", meltingDepth: 0 };
  }
  // An ascent that ends before it freezes still says how deep the warm air
  // is at least.
  const top = Number.isNaN(freezing) ? profile.p[last]! : freezing;
  const depth = interpolateAtPressure(profile, top, z) - z[ground]!;
  if (!Number.isFinite(depth)) return unknown;
  if (depth >= MELTING_DEPTH_M) return { phase: "rain", meltingDepth: depth };
  return Number.isNaN(freezing) ? unknown : { phase: "mix", meltingDepth: depth };
}

/**
 * The profile read for precipitation: whether a moist layer low enough to
 * matter is deep enough to rain or snow steadily, whether dry air under it
 * would evaporate part of what falls, and what would reach the ground.
 * `moist` is `moistLayers(profile)` unless the caller already has it.
 */
export function precipitationReading(
  profile: Profile,
  moist: readonly MoistLayer[] = moistLayers(profile),
): PrecipitationReading {
  const ground = groundIndex(profile);
  const z = heightsFromGround(profile, ground);
  const groundZ = ground < 0 ? Number.NaN : z[ground]!;
  let depth = 0;
  let base = Number.NaN;
  let basePressure = Number.NaN;
  // The top of the highest cloud low enough to precipitate to the ground;
  // with none, whatever falls is taken to come from the whole depth asked
  // about, as a shower's cloud would.
  let ceiling = Number.POSITIVE_INFINITY;
  for (const layer of moist) {
    // A layer that starts at the ground has the ground's height, filled in
    // or not; one aloft is between levels that report theirs.
    const bottom = interpolateAtPressure(profile, layer.pBottom, z) - groundZ;
    const thickness = interpolateAtPressure(profile, layer.pTop, z) - groundZ - bottom;
    if (!finite(bottom, thickness) || bottom > PRECIPITATING_BASE_M) continue;
    ceiling = Math.min(ceiling, layer.pTop);
    if (thickness <= depth) continue;
    depth = thickness;
    base = bottom;
    basePressure = layer.pBottom;
  }
  const deep = depth >= PRECIPITATING_DEPTH_M;
  return {
    depth,
    deep,
    evaporates: deep && base >= EVAPORATION_BASE_M && dryBelow(profile, ground, basePressure),
    ...phaseAtGround(profile, ground, z, Number.isFinite(ceiling) ? ceiling : PRECIPITATION_TOP_HPA),
  };
}

// --- Everything at once ------------------------------------------------------

export interface ProfileAnalysis {
  surface: SurfaceLevel | null;
  /** `liftParcel(profile, "mixed")`. */
  parcel: LiftedParcel | null;
  moist: MoistLayer[];
  inversions: Inversion[];
  maxWind: MaxWind | null;
  precipitation: PrecipitationReading;
}

/** Every reading above with its defaults, for the chart and the panel. */
export function analyseProfile(profile: Profile): ProfileAnalysis {
  const moist = moistLayers(profile);
  return {
    surface: surfaceLevel(profile),
    parcel: liftParcel(profile, "mixed"),
    moist,
    inversions: inversions(profile),
    maxWind: maxWind(profile),
    precipitation: precipitationReading(profile, moist),
  };
}
