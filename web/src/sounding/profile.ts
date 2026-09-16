/**
 * A sounding as the chart wants it, and the thermodynamics the chart draws.
 *
 * The published product (`docs/sounding.md` §2) is fixed-point integers —
 * pressure in pascals, temperature in hundredths of a kelvin, `-32768` where
 * the bulletin reported nothing — because a few hundred levels of JSON floats
 * would double the file. That encoding is right for the wire and wrong for
 * every line of drawing code, which wants °C and hPa and one way of saying
 * "not reported". `profileFromSounding` converts once, at the edge, into a
 * `Profile`: parallel float arrays with `NaN` for missing, so an arithmetic
 * expression that touches a gap produces a gap rather than a number 33 000
 * kelvin off, and the drawing only ever has to ask `Number.isFinite`.
 *
 * `profileFromModel` builds the same shape out of a model column — the
 * isobaric levels a run publishes at the pinned point — so the chart can lay
 * a forecast profile over an observed one without knowing which is which.
 *
 * The thermodynamic functions here are the chart's background: the dry
 * adiabats, the pseudoadiabats and the saturation mixing-ratio lines are
 * curves, not data, and every one of them is a pure function of pressure.
 * They are exported because they are the part worth testing on its own — a
 * skew-T whose 10 g/kg line is a degree off is wrong in a way no screenshot
 * shows.
 *
 * No DOM, no canvas: this module is arithmetic.
 */

/** The missing value in every published level array (`docs/sounding.md` §2). */
export const MISSING = -32768;

/** Bits of `extendedVerticalSoundingSignificance` (BUFR flag table 0 08 042)
 * a reader is likely to want, from `docs/sounding.md` §2. The field is 18
 * bits numbered left to right, so bit 1 is `1 << 17`. */
export const SIG = {
  surface: 131072,
  standard: 65536,
  tropopause: 32768,
  maxWind: 16384,
  significantTemperature: 8192,
  significantHumidity: 4096,
  significantWind: 2048,
} as const;

/** Whether a level's flag word carries a bit. A missing word (`NaN`, which
 * `-32768` becomes) counts as unflagged, the reading §2 gives. */
export function hasSig(word: number, bit: number): boolean {
  return Number.isFinite(word) && (word & bit) !== 0;
}

/** The four numbers the publisher derived from the published levels
 * (`docs/sounding.md` §7). Heights are geopotential metres and may be
 * negative; `pw` is millimetres and `lapse850_500` K/km. */
export interface ProfileDerived {
  freezingLevel: number | null;
  pw: number | null;
  lapse850_500: number | null;
  tropopause: number | null;
}

/**
 * One ascent, ordered by descending pressure — the order the product
 * publishes and the order every walk here assumes ("up" is toward index
 * `n - 1`). All seven arrays have length `n`, and a value the bulletin did
 * not report is `NaN`.
 */
export interface Profile {
  n: number;
  /** Pressure, hPa. Finite and strictly descending on every published level. */
  p: Float64Array;
  /** Geopotential height, m. */
  z: Float64Array;
  /** Temperature, °C. */
  t: Float64Array;
  /** Dew-point temperature, °C. */
  td: Float64Array;
  /** Wind direction in the meteorological convention — degrees the wind
   * comes from, clockwise from north. */
  wd: Float64Array;
  /** Wind speed, m/s. */
  ws: Float64Array;
  /** The BUFR significance word, verbatim, or `NaN`. */
  sig: Float64Array;
  derived: ProfileDerived | null;
}

/** The level arrays of one sounding as `soundings.jsonl` carries them
 * (`docs/sounding.md` §5). Structural, not imported from the product's own
 * schema module: the chart reads what it needs and nothing else. */
export interface SoundingLevels {
  readonly p: readonly number[];
  readonly z: readonly number[];
  readonly t: readonly number[];
  readonly td: readonly number[];
  readonly wd: readonly number[];
  readonly ws: readonly number[];
  readonly sig: readonly number[];
  readonly derived?: Partial<ProfileDerived> | null;
}

function convert(values: readonly number[], scale: (value: number) => number): Float64Array {
  const out = new Float64Array(values.length);
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]!;
    out[index] = value === MISSING ? Number.NaN : scale(value);
  }
  return out;
}

/** Convert one published sounding. The seven arrays must be parallel, which
 * the product guarantees; a file that breaks that is a bug upstream and is
 * refused here rather than drawn half-length. */
export function profileFromSounding(sounding: SoundingLevels): Profile {
  const n = sounding.p.length;
  for (const [name, values] of [
    ["z", sounding.z],
    ["t", sounding.t],
    ["td", sounding.td],
    ["wd", sounding.wd],
    ["ws", sounding.ws],
    ["sig", sounding.sig],
  ] as const) {
    if (values.length !== n) throw new Error(`sounding: ${name} has ${values.length} levels, p has ${n}`);
  }
  const derived = sounding.derived;
  return {
    n,
    // Pa → hPa, K×100 → °C, m/s×10 → m/s; gpm, degrees and the flag word are
    // already what the chart reads.
    p: convert(sounding.p, (value) => value / 100),
    z: convert(sounding.z, (value) => value),
    t: convert(sounding.t, (value) => value / 100 - 273.15),
    td: convert(sounding.td, (value) => value / 100 - 273.15),
    wd: convert(sounding.wd, (value) => value),
    ws: convert(sounding.ws, (value) => value / 10),
    sig: convert(sounding.sig, (value) => value),
    derived: derived
      ? {
          freezingLevel: derived.freezingLevel ?? null,
          pw: derived.pw ?? null,
          lapse850_500: derived.lapse850_500 ?? null,
          tropopause: derived.tropopause ?? null,
        }
      : null,
  };
}

/** One model pressure level at a point. `p` is hPa and `t` °C; humidity
 * arrives either as a dew point or as a relative humidity in percent, and the
 * wind as components in m/s, because that is what the runs publish. */
export interface ModelLevel {
  p: number;
  t: number | null;
  td?: number | null;
  rh?: number | null;
  z?: number | null;
  u?: number | null;
  v?: number | null;
}

/** Saturation vapour pressure over water, hPa, from a temperature in °C:
 * Bolton (1980) eq. 10, the same form `docs/sounding.md` §7 gives for `pw`
 * and the encoder's θe derivation uses, so every part of this project agrees
 * on what saturation means. */
export function saturationVapourPressure(tCelsius: number): number {
  return 6.112 * Math.exp((17.67 * tCelsius) / (tCelsius + 243.5));
}

/** The inverse: the dew point, °C, at which a vapour pressure `e` (hPa)
 * saturates. */
export function dewPointFromVapourPressure(e: number): number {
  const logged = Math.log(e / 6.112);
  return (243.5 * logged) / (17.67 - logged);
}

/** Dew point, °C, from a temperature and a relative humidity in percent —
 * Bolton inverted through `e = rh/100 · e_s(t)`. An `rh` of 100 returns the
 * temperature itself, exactly, which is the property the chart depends on
 * when it draws a saturated model level. */
export function dewPointFromRelativeHumidity(tCelsius: number, rhPercent: number): number {
  if (!Number.isFinite(tCelsius) || !Number.isFinite(rhPercent) || rhPercent <= 0) return Number.NaN;
  const e = (Math.min(rhPercent, 100) / 100) * saturationVapourPressure(tCelsius);
  return dewPointFromVapourPressure(e);
}

/** Wind speed (m/s) and meteorological direction (degrees the wind comes
 * from) from the components the runs publish. */
export function windFromComponents(u: number, v: number): { ws: number; wd: number } {
  const ws = Math.hypot(u, v);
  // atan2(v, u) is the direction the wind blows *towards*, counter-clockwise
  // from east; the meteorological convention is where it comes from,
  // clockwise from north. A calm level has no direction to report.
  const wd = ws === 0 ? Number.NaN : (270 - (Math.atan2(v, u) * 180) / Math.PI + 360) % 360;
  return { ws, wd };
}

/**
 * A model column as a `Profile`. Levels are sorted to descending pressure —
 * a manifest's isobaric family is listed however the frontend listed it — and
 * a level with no temperature still takes its row, so the arrays stay
 * parallel and a gap in the model draws as a gap.
 *
 * `sig` is `NaN` throughout: a model level is not a sonde's significant
 * level, and nothing downstream should mistake one for the other (the wind
 * barbs, which read `sig`, fall back to a stride for exactly this case).
 */
export function profileFromModel(levels: readonly ModelLevel[]): Profile {
  const sorted = levels
    .filter((level) => Number.isFinite(level.p))
    .slice()
    .sort((a, b) => b.p - a.p);
  const n = sorted.length;
  const profile: Profile = {
    n,
    p: new Float64Array(n),
    z: new Float64Array(n),
    t: new Float64Array(n),
    td: new Float64Array(n),
    wd: new Float64Array(n),
    ws: new Float64Array(n),
    sig: new Float64Array(n).fill(Number.NaN),
    derived: null,
  };
  for (const [index, level] of sorted.entries()) {
    const t = level.t ?? Number.NaN;
    profile.p[index] = level.p;
    profile.z[index] = level.z ?? Number.NaN;
    profile.t[index] = t;
    profile.td[index] =
      level.td !== undefined && level.td !== null
        ? level.td
        : level.rh !== undefined && level.rh !== null
          ? dewPointFromRelativeHumidity(t, level.rh)
          : Number.NaN;
    if (level.u !== undefined && level.u !== null && level.v !== undefined && level.v !== null) {
      const wind = windFromComponents(level.u, level.v);
      profile.ws[index] = wind.ws;
      profile.wd[index] = wind.wd;
    } else {
      profile.ws[index] = Number.NaN;
      profile.wd[index] = Number.NaN;
    }
  }
  return profile;
}

// --- The chart's background curves ------------------------------------------

/** Gas constant for dry air, J/(kg·K). */
const RD = 287.04;
/** Specific heat of dry air at constant pressure, J/(kg·K). */
const CP = 1005.7;
/** Poisson's constant, Rd/cp — the exponent of the dry adiabat. */
const KAPPA = RD / CP;
/** Ratio of the molecular weights of water vapour and dry air. */
const EPSILON = 0.622;
/** Latent heat of vaporisation at 0 °C, J/kg. */
const LV = 2.501e6;
/** The reference pressure potential temperature is defined at, hPa. */
export const REFERENCE_PRESSURE = 1000;

/**
 * The dry adiabat of potential temperature `theta` (K) at pressure `p` (hPa),
 * in °C: `T = θ (p/1000)^κ`. At 1000 hPa it is `θ − 273.15` exactly, which is
 * how the chart labels the curve.
 */
export function dryAdiabat(theta: number, p: number): number {
  return theta * (p / REFERENCE_PRESSURE) ** KAPPA - 273.15;
}

/** Saturation mixing ratio, kg/kg, at a temperature (°C) and pressure (hPa). */
export function saturationMixingRatio(tCelsius: number, p: number): number {
  const e = saturationVapourPressure(tCelsius);
  return (EPSILON * e) / Math.max(p - e, 1e-6);
}

/**
 * The pseudoadiabatic lapse rate in pressure coordinates, K/hPa, at a
 * saturated parcel's temperature (K) and pressure (hPa):
 *
 * ```
 * dT/dp = (Rd·T + Lv·ws) / (p · (cp + Lv²·ws·ε / (Rd·T²)))
 * ```
 *
 * the usual pseudo-adiabatic form — all condensate falls out, the heat
 * capacity of the liquid is ignored — with `ws` the saturation mixing ratio
 * at (T, p). It is written in pressure rather than height because the chart
 * wants a temperature at a pressure and never asks about a height, and
 * because the expression is then free of the hydrostatic assumption. It is
 * scale-free in `p`: `ws` is dimensionless and `Rd·T/p` carries whatever unit
 * `p` is in, so hPa in gives K/hPa out.
 */
function pseudoadiabaticLapse(tKelvin: number, p: number): number {
  const ws = saturationMixingRatio(tKelvin - 273.15, p);
  const numerator = RD * tKelvin + LV * ws;
  const denominator = p * (CP + (LV * LV * ws * EPSILON) / (RD * tKelvin * tKelvin));
  return numerator / denominator;
}

/** The integration step of every pseudoadiabat here, hPa. Five hectopascals
 * under a midpoint (RK2) step holds the curve to a few hundredths of a kelvin
 * over the whole chart — far below a pixel — and a 1050 → 100 hPa curve is
 * 190 steps, which is nothing to draw a dozen of per frame. */
export const MOIST_STEP_HPA = 5;

/**
 * Follow the pseudoadiabat through (`p0`, `t0` in °C) to pressure `p`, in °C.
 * Integrated with a fixed `MOIST_STEP_HPA` step, midpoint rule, in whichever
 * direction `p` lies; the last step is short so the curve lands on `p`
 * exactly rather than near it.
 */
export function moistAdiabatFrom(p0: number, t0: number, p: number): number {
  if (!Number.isFinite(p0) || !Number.isFinite(t0) || !Number.isFinite(p) || p <= 0 || p0 <= 0) return Number.NaN;
  let temperature = t0 + 273.15;
  let pressure = p0;
  const direction = Math.sign(p - p0);
  if (direction === 0) return t0;
  const steps = Math.ceil(Math.abs(p - p0) / MOIST_STEP_HPA);
  for (let step = 0; step < steps; step += 1) {
    const next = direction > 0 ? Math.min(p, pressure + MOIST_STEP_HPA) : Math.max(p, pressure - MOIST_STEP_HPA);
    const h = next - pressure;
    const half = pressure + h / 2;
    const midpoint = temperature + (h / 2) * pseudoadiabaticLapse(temperature, pressure);
    temperature += h * pseudoadiabaticLapse(midpoint, half);
    pressure = next;
  }
  return temperature - 273.15;
}

/**
 * The pseudoadiabat labelled by its wet-bulb potential temperature: the curve
 * through (1000 hPa, `thetaW` °C), evaluated at `p` (hPa), in °C. `thetaW` is
 * in °C to match the chart's own axis — potential temperature is
 * conventionally in kelvin and θw conventionally in °C, and the two arguments
 * keep their conventions rather than a shared one neither reader expects.
 */
export function moistAdiabat(thetaW: number, p: number): number {
  return moistAdiabatFrom(REFERENCE_PRESSURE, thetaW, p);
}

/**
 * The temperature (°C) at which the saturation mixing ratio at pressure `p`
 * (hPa) is `w` (g/kg) — the isohume the chart draws. Inverted from the
 * mixing-ratio definition `w = ε e / (p − e)` through Bolton's saturation
 * curve, so at 1000 hPa the 10 g/kg line sits at 13.9 °C.
 */
export function mixingRatioLine(w: number, p: number): number {
  const ratio = w / 1000;
  const e = (ratio * p) / (EPSILON + ratio);
  return dewPointFromVapourPressure(e);
}

/** A lifting condensation level: where a parcel lifted dry-adiabatically
 * first saturates. */
export interface Lcl {
  /** Pressure, hPa. */
  p: number;
  /** Temperature, °C. */
  t: number;
}

/**
 * The LCL of a parcel at (`p` hPa, `t` °C, dew point `td` °C).
 *
 * The temperature is Bolton (1980) eq. 15,
 * `T_L = 1/(1/(Td − 56) + ln(T/Td)/800) + 56` in kelvin — a closed form for
 * what is otherwise the intersection of a dry adiabat and an isohume, found
 * by iteration — and the pressure follows it down the dry adiabat,
 * `p_L = p (T_L/T)^(1/κ)`. A parcel already saturated, or reported with a dew
 * point above its temperature as a rounded bulletin can, condenses where it
 * stands.
 */
export function lcl(t: number, td: number, p: number): Lcl | null {
  if (!Number.isFinite(t) || !Number.isFinite(td) || !Number.isFinite(p) || p <= 0) return null;
  if (td >= t) return { p, t };
  const tK = t + 273.15;
  const tdK = td + 273.15;
  const tL = 1 / (1 / (tdK - 56) + Math.log(tK / tdK) / 800) + 56;
  return { p: p * (tL / tK) ** (1 / KAPPA), t: tL - 273.15 };
}

/** The path of a lifted parcel: parallel pressure (hPa) and temperature (°C)
 * arrays from its start upward, with the LCL it passes through. */
export interface ParcelPath {
  p: Float64Array;
  t: Float64Array;
  lcl: Lcl;
}

/**
 * Lift the surface parcel: the lowest level that reports a pressure, a
 * temperature and a dew point, dry-adiabatically to its LCL and
 * pseudoadiabatically above it.
 *
 * The path is sampled at the profile's own levels — the chart draws it
 * against that profile and nothing else — with the LCL inserted where it
 * falls, so the kink in the curve is a vertex rather than a chord across two
 * mandatory levels. No CAPE, no CIN: the areas are a later phase, and the
 * curve is what the chart needs first.
 */
export function parcelPath(profile: Profile): ParcelPath | null {
  let start = -1;
  for (let index = 0; index < profile.n; index += 1) {
    if (
      Number.isFinite(profile.p[index]!) &&
      Number.isFinite(profile.t[index]!) &&
      Number.isFinite(profile.td[index]!)
    ) {
      start = index;
      break;
    }
  }
  if (start < 0) return null;
  const p0 = profile.p[start]!;
  const t0 = profile.t[start]!;
  const condensation = lcl(t0, profile.td[start]!, p0);
  if (!condensation) return null;
  const theta = (t0 + 273.15) * (REFERENCE_PRESSURE / p0) ** KAPPA;

  const pressures: number[] = [];
  const push = (p: number): void => {
    const last = pressures[pressures.length - 1];
    if (last === undefined || p < last) pressures.push(p);
  };
  for (let index = start; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    if (!Number.isFinite(p)) continue;
    if (condensation.p < p0 && condensation.p > p) push(condensation.p);
    push(p);
  }
  if (pressures.length === 0) push(p0);

  const path: ParcelPath = {
    p: new Float64Array(pressures.length),
    t: new Float64Array(pressures.length),
    lcl: condensation,
  };
  for (const [index, p] of pressures.entries()) {
    path.p[index] = p;
    path.t[index] = p >= condensation.p ? dryAdiabat(theta, p) : moistAdiabatFrom(condensation.p, condensation.t, p);
  }
  return path;
}

/**
 * A profile's value at an arbitrary pressure, linear in `ln p` between the
 * two levels that bracket it and both report the field. Outside the profile's
 * range it is `NaN`: a sounding that stops at 100 hPa says nothing about 50.
 */
export function interpolateAtPressure(profile: Profile, p: number, field: Float64Array): number {
  if (!Number.isFinite(p) || p <= 0) return Number.NaN;
  let lower = -1;
  for (let index = 0; index < profile.n; index += 1) {
    const levelP = profile.p[index]!;
    if (!Number.isFinite(levelP) || !Number.isFinite(field[index]!)) continue;
    if (levelP >= p) {
      lower = index;
      continue;
    }
    if (lower < 0) return Number.NaN;
    const p0 = profile.p[lower]!;
    if (p0 === p) return field[lower]!;
    const weight = (Math.log(p0) - Math.log(p)) / (Math.log(p0) - Math.log(levelP));
    return field[lower]! + weight * (field[index]! - field[lower]!);
  }
  return lower >= 0 && profile.p[lower]! === p ? field[lower]! : Number.NaN;
}
