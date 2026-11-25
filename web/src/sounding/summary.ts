/**
 * What a profile says, in words and numbers: the sentences, the table of
 * indices, the guide to the chart, the levels the chart names and the heights
 * on its right edge, for whichever page the profile is shown on. `analysis.ts`
 * finds the layers and lifts the parcel; the units and the language are put on
 * them here.
 */

import { element } from "../dom";
import { t } from "../i18n";
import {
  UNIT_LABELS,
  convertHeight,
  convertTemperatureDelta,
  formatPrecipitation,
  formatTemperature,
  formatWind,
  units,
} from "../units";
import type { ProfileAnalysis } from "./analysis";
import { interpolateAtPressure, type ConvectiveCondensation, type Profile } from "./profile";
import { pressureAtHeight, type SkewTMark } from "./skewt";

/** The spread the moist layers are found at, °C (`analysis.ts`'s default). */
export const MOIST_SPREAD = 3;
/** A wind at or above this, m/s, is called a jet. */
const JET_SPEED = 30;
/** One full barb, in m/s: ten knots. */
const BARB_MS = 10 / 1.9438444924406046;

/** Everything the section derives from one ascent, computed once when the
 * ascent is first shown: the parcel is integrated, and a pointer moving
 * over the chart must not integrate it again. */
export interface ProfileReading {
  profile: Profile;
  analysis: ProfileAnalysis;
  convective: ConvectiveCondensation | null;
  /** Set when the profile is a model column rather than an ascent: what it
   * is a column of, how coarse it is, and the run's own CAPE and CIN. */
  column: { run: string; stamp: string; levels: number; top: number | null; cape: number | null; cin: number | null } | null;
}

/** A height above sea level as the chart's right edge writes it: kilometres
 * where the viewer reads metres, feet otherwise. */
export function altitude(metres: number): string {
  if (units.height === "ft") return `${Math.round(convertHeight(metres) / 100) * 100} ft`;
  return Math.abs(metres) < 1000 ? `${Math.round(metres / 10) * 10} m` : `${(metres / 1000).toFixed(1)} km`;
}

/** The two ends of a layer, written as one range: the lower end drops its
 * unit where the upper end says the same one (`3.8–4.0 km`), and keeps it
 * where they differ (`800 m–1.2 km`). */
function altitudeSpan(from: number, to: number): { from: string; to: string } {
  const upper = altitude(to);
  const lower = altitude(from);
  const unit = (text: string): string => text.slice(text.lastIndexOf(" "));
  return { from: unit(lower) === unit(upper) ? lower.slice(0, lower.lastIndexOf(" ")) : lower, to: upper };
}

/** A temperature difference in the viewer's unit. */
export function temperatureDelta(kelvin: number, decimals = 1): string {
  return `${convertTemperatureDelta(kelvin).toFixed(decimals)} ${UNIT_LABELS.temperature[units.temperature]}`;
}

/** The heights the right edge of the chart names, in metres, and what it
 * calls each: whole kilometres, or thousands of feet. Close together near
 * the ground, where a reader asks how high a cloud base or an inversion is,
 * and sparse above it — the pressure axis is logarithmic, and a tick every
 * kilometre is a ladder up the side of the chart. */
export function heightScale(): { metres: number; label: string }[] {
  if (units.height === "ft") {
    return [3, 6, 10, 18, 24, 30, 39, 53].map((thousands) => ({
      metres: (thousands * 1000) / 3.280839895,
      label: `${thousands} kft`,
    }));
  }
  return [1, 2, 3, 5, 7, 9, 12, 16].map((kilometres) => ({
    metres: kilometres * 1000,
    label: `${kilometres} km`,
  }));
}

/** A wind as the readout writes it, direction first: `247° 40 m/s`. */
export function windText(direction: number, speed: number): string {
  return `${String(Math.round(direction) % 360).padStart(3, "0")}° ${formatWind(speed, 0)}`;
}

/** The levels the chart names on its left edge. */
export function levelMarks(observed: ProfileReading): SkewTMark[] {
  const { profile, analysis } = observed;
  const marks: SkewTMark[] = [];
  const derived = profile.derived;
  if (derived?.freezingLevel != null) {
    marks.push({ p: pressureAtHeight(profile, derived.freezingLevel), label: formatTemperature(0, 0) });
  }
  const parcel = analysis.parcel;
  if (parcel) {
    marks.push({ p: parcel.path.lcl.p, label: "LCL" });
    // Where the parcel breaks free and where it stops are read off the
    // environment between levels, which a model column does not have.
    if (!observed.column) {
      // A parcel free from its condensation level has one level, not two.
      if (parcel.lfc !== null && Math.abs(parcel.lfc - parcel.path.lcl.p) > 3) marks.push({ p: parcel.lfc, label: "LFC" });
      if (parcel.el !== null) marks.push({ p: parcel.el, label: "EL" });
    }
  }
  if (derived?.tropopause != null) marks.push({ p: pressureAtHeight(profile, derived.tropopause), label: "TRP" });
  return marks;
}

/** What one barb, half barb and pennant are worth in the viewer's unit. */
export function barbParams(): Record<string, string> {
  return {
    full: formatWind(BARB_MS, 0),
    half: formatWind(BARB_MS / 2, 0),
    flag: formatWind(BARB_MS * 5, 0),
  };
}

/** The profile in a few sentences: where it is moist, where it is capped,
 * where it freezes, whether it is unstable, what that means for
 * precipitation and how hard it blows aloft. */
export function summaryLines(observed: ProfileReading): string[] {
  const { profile, analysis } = observed;
  const lines: string[] = [];
  const surface = analysis.surface;
  // The two deepest moist layers, from the ground up: more than that is a
  // list, not a reading.
  const moist = [...analysis.moist]
    .sort((a, b) => b.pBottom - b.pTop - (a.pBottom - a.pTop))
    .slice(0, 2)
    .sort((a, b) => b.pBottom - a.pBottom);
  for (const layer of moist) {
    if (!Number.isFinite(layer.zTop)) continue;
    const grounded = surface !== null && layer.pBottom >= surface.p - 5;
    if (grounded) lines.push(t("soundingSayMoistSurface", { to: altitude(layer.zTop) }));
    else if (Number.isFinite(layer.zBottom)) {
      lines.push(t("soundingSayMoist", altitudeSpan(layer.zBottom, layer.zTop)));
    }
  }
  if (analysis.moist.length === 0 && surface !== null) {
    lines.push(t("soundingSayDry", { spread: temperatureDelta(MOIST_SPREAD, 0) }));
  }
  // The surface inversion if there is one, else the strongest aloft.
  const inversion =
    analysis.inversions.find((layer) => layer.surfaceBased) ??
    [...analysis.inversions].sort((a, b) => b.strength - a.strength)[0];
  if (inversion && Number.isFinite(inversion.zTop)) {
    const strength = temperatureDelta(inversion.strength);
    if (inversion.surfaceBased) {
      lines.push(t("soundingSayInversionSurface", { strength, to: altitude(inversion.zTop) }));
    } else if (Number.isFinite(inversion.zBottom)) {
      lines.push(t("soundingSayInversion", { strength, ...altitudeSpan(inversion.zBottom, inversion.zTop) }));
    }
  }
  const freezing = profile.derived?.freezingLevel;
  if (freezing != null) lines.push(t("soundingSayFreezing", { height: altitude(freezing) }));
  else if (surface !== null && surface.t < 0) lines.push(t("soundingSayFrozen"));
  // An ascent's energy is the area on the chart; a column's is the number
  // the run computed on all of its own levels, where it publishes one.
  const parcel = analysis.parcel;
  const energy = observed.column
    ? observed.column.cape
    : parcel
      ? parcel.lfc === null
        ? 0
        : parcel.cape
      : null;
  if (energy !== null) {
    const cape = Math.round(energy);
    if (cape < 100) lines.push(t("soundingSayStable"));
    else {
      const key = cape < 1000 ? "soundingSayCapeWeak" : cape < 2500 ? "soundingSayCapeModerate" : "soundingSayCapeStrong";
      lines.push(t(key, { cape }));
    }
  }
  // What the moisture, the freezing level and the stability add up to for
  // precipitation — conditions, since a profile holds no lift. A thin
  // moist layer is not called drizzle where the air is unstable: a shower
  // builds its own depth. The phase is said only where something could
  // fall and the answer is not plain rain.
  const wet = analysis.precipitation;
  const unstable = energy !== null && Math.round(energy) >= 100;
  if (wet.deep) lines.push(t("soundingSayPrecipDeep", { depth: altitude(wet.depth) }));
  else if (wet.depth > 0 && !unstable) lines.push(t("soundingSayPrecipShallow", { depth: altitude(wet.depth) }));
  if (wet.evaporates) lines.push(t("soundingSayPrecipEvaporates"));
  if (unstable) lines.push(t("soundingSayPrecipShowery"));
  if (wet.depth > 0 || unstable) {
    if (wet.phase === "snow") lines.push(t("soundingSayPhaseSnow"));
    else if (wet.phase === "mix") lines.push(t("soundingSayPhaseMix", { depth: altitude(wet.meltingDepth) }));
    else if (wet.phase === "freezing") lines.push(t("soundingSayPhaseFreezing"));
  }
  const wind = analysis.maxWind;
  if (wind && Number.isFinite(wind.z)) {
    lines.push(
      t(wind.ws >= JET_SPEED ? "soundingSayJet" : "soundingSayWind", {
        speed: formatWind(wind.ws, 0),
        height: altitude(wind.z),
      }),
    );
  }
  // What a column this coarse cannot show is said last, once.
  if (observed.column && observed.column.top !== null) {
    lines.push(t("soundingSayColumn", { count: observed.column.levels, top: `${observed.column.top} hPa` }));
  }
  return lines;
}

/** The numbers a forecaster takes off the chart, each under its name. */
export function indexItems(observed: ProfileReading): HTMLElement[] {
  const { profile, analysis, convective } = observed;
  const derived = profile.derived;
  const surface = analysis.surface;
  const aboveGround = (p: number): string | null => {
    if (!surface) return null;
    const z = interpolateAtPressure(profile, p, profile.z) - surface.z;
    return Number.isFinite(z) ? altitude(Math.max(0, z)) : null;
  };
  const joules = (value: number | null): string | null => (value === null ? null : `${Math.round(value)} J/kg`);
  const entries: [string, string | null][] = [
    [t("soundingIndexFreezing"), derived?.freezingLevel != null ? altitude(derived.freezingLevel) : null],
    [t("soundingIndexTropopause"), derived?.tropopause != null ? altitude(derived.tropopause) : null],
    [t("soundingIndexPw"), derived?.pw != null ? formatPrecipitation(derived.pw, 1) : null],
    [t("soundingIndexLapse"), derived?.lapse850_500 != null ? `${derived.lapse850_500.toFixed(1)} K/km` : null],
    [t("soundingIndexLcl"), analysis.parcel ? aboveGround(analysis.parcel.path.lcl.p) : null],
    ...(observed.column
      ? ([
          [t("soundingIndexModelOutput", { name: "CAPE" }), joules(observed.column.cape)],
          [t("soundingIndexModelOutput", { name: "CIN" }), joules(observed.column.cin)],
        ] satisfies [string, string | null][])
      : ([
          ["CAPE", analysis.parcel ? joules(analysis.parcel.cape) : null],
          ["CIN", joules(analysis.parcel?.cin ?? null)],
        ] satisfies [string, string | null][])),
    [t("soundingIndexCcl"), convective ? aboveGround(convective.p) : null],
    [t("soundingIndexTcon"), convective ? formatTemperature(convective.tcon, 0) : null],
  ];
  return entries
    .filter((item): item is [string, string] => item[1] !== null)
    .map(([name, value]) => {
      const item = element("div", "sounding-index");
      item.append(element("dt", "", name), element("dd", "", value));
      return item;
    });
}

/** The guide's six sentences. */
export function guideItems(): HTMLElement[] {
  return (
    [
      "soundingGuideAxes",
      "soundingGuideMoisture",
      "soundingGuideInversion",
      "soundingGuideFreezing",
      "soundingGuideParcel",
      "soundingGuideWind",
    ] as const
  ).map((key) => element("li", "", t(key, barbParams())));
}
