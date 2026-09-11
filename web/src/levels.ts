import { t, type MessageKey } from "./i18n";
import type { ChartFamily, VariableIdentity } from "./identity";
import { ISOBARIC_LEVELS, type ForecastBundleId, type IsobaricLevel } from "./manifest";
import { PRESSURE_BUNDLE_IDS, pressureLabel } from "./pressure";

/**
 * The variable *families*: what the layer rail shows one tile for, and what
 * the transport capsule's level row picks a surface within.
 *
 * A family is one physical quantity registered on the eight standard isobaric
 * surfaces (manifest.ts `ISOBARIC_LEVELS`), possibly with a near-surface
 * member of its own — 2 m temperature heads the temperature family, 10 m wind
 * the wind family, mean sea level pressure the pressure family. Precipitation,
 * radiation and reflectivity are single layers and belong to no family.
 *
 * Everything here is chart knowledge, not container knowledge: the codebook
 * ranges are copied from the encoders and held to them by
 * `tests/fixtures/isobaric-registry.json` (`tests/web/levels.test.ts`), the
 * way pressure.ts is held by its own registry.
 */
export type IsobaricFamily = "hgt" | "tmp" | "rh" | "spfh" | "wind" | "qflux";

export const ISOBARIC_FAMILIES: readonly IsobaricFamily[] = ["hgt", "tmp", "rh", "spfh", "wind", "qflux"];

export interface FamilyInfo {
  id: IsobaricFamily;
  /** Filled field, magnitude field of a u/v pair, or contour lines. */
  kind: "scalar" | "vector" | "lines";
  /** The near-surface member, when the family has one. */
  surface: ForecastBundleId | null;
  /** The instrument-panel word for the family — the level row's caption and
   * the first half of each member's code. English in both locales, like the
   * rest of the panel. */
  code: string;
  /** The level row's label for the surface member ("2M", "10M", "MSL"). */
  surfaceCode: string;
  /** The one glyph the rail tile carries. */
  glyphKey: MessageKey;
  /** The tile's visually hidden gloss. */
  glossKey: MessageKey;
}

export const FAMILIES: Record<IsobaricFamily, FamilyInfo> = {
  hgt: {
    id: "hgt",
    kind: "lines",
    surface: "prmsl",
    code: "PRESSURE",
    surfaceCode: "MSL",
    glyphKey: "glyphPressure",
    glossKey: "varPressureField",
  },
  tmp: { id: "tmp", kind: "scalar", surface: "tmp2m", code: "TEMP", surfaceCode: "2M", glyphKey: "glyphTmp2m", glossKey: "varTemp" },
  rh: { id: "rh", kind: "scalar", surface: null, code: "RH", surfaceCode: "", glyphKey: "glyphRh", glossKey: "varHumidity" },
  spfh: {
    id: "spfh",
    kind: "scalar",
    surface: null,
    code: "SPFH",
    surfaceCode: "",
    glyphKey: "glyphSpfh",
    glossKey: "varSpecificHumidity",
  },
  wind: { id: "wind", kind: "vector", surface: "wind10m", code: "WIND", surfaceCode: "10M", glyphKey: "glyphWind10m", glossKey: "varWind" },
  qflux: {
    id: "qflux",
    kind: "vector",
    surface: null,
    code: "QFLUX",
    surfaceCode: "",
    glyphKey: "glyphQflux",
    glossKey: "varVapourFlux",
  },
};

/** The family a bundle id *names*, or null for a single layer (precipitation,
 * radiation, reflectivity) and for any name the convention does not describe.
 *
 * A naming-convention reading of the id string — `<family><level>`, plus the
 * surface members — and nothing more. It is what the rail, the level row and
 * `?type=` have to go on before a bundle is open; once one is, its variables'
 * parameter blocks are the identity (identity.ts) and this is only a guess
 * checked against them. Unknown names return null rather than throwing. */
export function familyOf(id: ForecastBundleId): IsobaricFamily | null {
  for (const family of ISOBARIC_FAMILIES) {
    if (FAMILIES[family].surface === id) return family;
  }
  const match = /^([a-z]+)(\d+)$/.exec(id);
  if (!match) return null;
  const prefix = match[1] as IsobaricFamily;
  return ISOBARIC_FAMILIES.includes(prefix) && (ISOBARIC_LEVELS as readonly number[]).includes(Number(match[2])) ? prefix : null;
}

/** The isobaric surface a bundle id names, in hPa, or null for a surface
 * member and for anything outside the families. The same naming convention
 * `familyOf` reads, with the same standing. */
export function bundleLevel(id: ForecastBundleId): IsobaricLevel | null {
  const match = /^(?:hgt|tmp|rh|spfh|wind|qflux)(\d+)$/.exec(id);
  if (!match) return null;
  const level = Number(match[1]);
  return (ISOBARIC_LEVELS as readonly number[]).includes(level) ? (level as IsobaricLevel) : null;
}

/** Every member of a family in level-row order: the surface first, then the
 * isobaric surfaces from the ground up. */
export function familyMembers(family: IsobaricFamily): ForecastBundleId[] {
  const info = FAMILIES[family];
  const members: ForecastBundleId[] = info.surface ? [info.surface] : [];
  for (const level of ISOBARIC_LEVELS) members.push(`${family}${level}` as ForecastBundleId);
  return members;
}

/** The level row's label for one member: "MSL" / "2M" / "10M" for a surface
 * member, the pressure in hPa otherwise. */
export function levelCode(id: ForecastBundleId): string {
  const family = familyOf(id);
  if (family === null) return id.toUpperCase();
  const level = bundleLevel(id);
  return level === null ? FAMILIES[family].surfaceCode : String(level);
}

/** Codebook coverage of each filled isobaric scalar (the `quality` profile;
 * `balanced` is the same) — the palette's domain and the legend's range.
 * Copied from xuebuild/quantize.py and held to it by the shared registry. */
const TEMPERATURE_RANGES: Record<IsobaricLevel, readonly [number, number]> = {
  1000: [-60, 60],
  925: [-65, 50],
  850: [-70, 45],
  700: [-75, 35],
  500: [-85, 15],
  300: [-95, 0],
  250: [-100, -5],
  200: [-100, -10],
};
const SPECIFIC_HUMIDITY_MAX: Record<IsobaricLevel, number> = {
  1000: 50.8,
  925: 50.8,
  850: 25.4,
  700: 25.4,
  500: 5.08,
  300: 2.54,
  250: 1.27,
  200: 1.27,
};

/** True when a level is one of the eight the encoders register a family on.
 * Chart knowledge exists only there; a field on any other surface is drawn
 * generically from its own codebook. */
export function isRegisteredLevel(level: number | null): level is IsobaricLevel {
  return level !== null && (ISOBARIC_LEVELS as readonly number[]).includes(level);
}

/** Codebook coverage of one filled isobaric scalar, keyed by what the field
 * *is* rather than by what it is called. Null for a family or a surface the
 * encoders register nothing on. */
export function isobaricRange(family: ChartFamily, level: number | null): readonly [number, number] | null {
  if (!isRegisteredLevel(level)) return null;
  if (family === "tmp") return TEMPERATURE_RANGES[level];
  if (family === "rh") return [0, 100];
  if (family === "spfh") return [0, SPECIFIC_HUMIDITY_MAX[level]];
  return null;
}

/** The temperature ramp's domain on one surface. Up to 700 hPa the absolute
 * ramp the 2 m field uses, so the 0 °C isotherm keeps its colour where it
 * decides rain from snow; higher up the whole troposphere is below freezing
 * and the same ramp would paint every surface one blue, so the ramp is laid
 * over the level's own sixty-degree window instead. */
const UPPER_TEMPERATURE_DOMAINS: Partial<Record<IsobaricLevel, readonly [number, number]>> = {
  500: [-60, 0],
  300: [-75, -15],
  250: [-80, -20],
  200: [-85, -25],
};

/** `level` is the isobaric surface in hPa, or null for the 2 m member, which
 * takes the absolute ramp. */
export function temperaturePaletteDomain(level: number | null): readonly [number, number] {
  return (isRegisteredLevel(level) ? UPPER_TEMPERATURE_DOMAINS[level] : undefined) ?? [-60, 50];
}

/** The part of the ramp's domain the level's codebook can actually hold —
 * what the legend spans. 850 hPa stops at 45 °C, 700 hPa at 35. */
export function temperatureLegendRange(level: number | null): readonly [number, number] {
  const [low, high] = temperaturePaletteDomain(level);
  const range = isobaricRange("tmp", level);
  if (!range) return [low, high];
  return [Math.max(low, range[0]), Math.min(high, range[1])];
}

/** Ceiling of a vector field's magnitude palette: the 10 m wind's 40 m/s,
 * more for the isobaric winds (a jet core passes 80), and the vapour flux's
 * own scale — strong transport is 20–40 g·cm⁻¹·hPa⁻¹·s⁻¹. Keyed by the pair
 * the field *is*; a surface with no registered ceiling takes the 10 m one. */
export function vectorMaxMagnitude(family: ChartFamily, level: number | null): number {
  if (family === "qflux") return 50;
  if (!isRegisteredLevel(level)) return 40;
  if (level >= 700) return 60;
  if (level === 500) return 80;
  return 100;
}

/** Human-facing name of one family member. */
export function familyLabel(id: ForecastBundleId): string {
  const family = familyOf(id);
  const level = bundleLevel(id);
  if (family === "hgt") return pressureLabel(id as (typeof PRESSURE_BUNDLE_IDS)[number]);
  if (level === null) {
    if (id === "tmp2m") return t("varLabelTmp2m");
    if (id === "wind10m") return t("varLabelWind10m");
    return id;
  }
  const key: MessageKey = {
    tmp: "varLabelTempAtLevel",
    rh: "varLabelRhAtLevel",
    spfh: "varLabelSpfhAtLevel",
    wind: "varLabelWindAtLevel",
    qflux: "varLabelQfluxAtLevel",
    hgt: "varLabelHeightAtLevel",
  }[family!] as MessageKey;
  return t(key).replace("{level}", String(level));
}

/** The instrument-panel code of one isobaric member: "TMP 850MB", "RH
 * 700MB", "WIND 850MB", "QFLUX 850MB". */
export function isobaricCode(id: ForecastBundleId): string {
  const level = bundleLevel(id);
  const family = familyOf(id);
  if (level === null || family === null) return id.toUpperCase();
  const word = { hgt: "HGT", tmp: "TMP", rh: "RH", spfh: "SPFH", wind: "WIND", qflux: "QFLUX" }[family];
  return `${word} ${level}MB`;
}

/** Six legend ticks, coarsest first, across a range — the same shape every
 * other variable's legend has. `step` rounds the ticks to chart values. */
export function rangeLegend(range: readonly [number, number], step: number): string[] {
  const [low, high] = range;
  const ticks: string[] = [];
  for (let index = 0; index < 6; index += 1) {
    const value = high - (index * (high - low)) / 5;
    const rounded = Math.round(value / step) * step;
    ticks.push(Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(step < 0.1 ? 2 : 1));
  }
  return ticks;
}

/** A step that puts six readable ticks across a span — the fallback for a
 * field with no registered legend, whose range is its codebook's own. */
export function niceStep(span: number): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  return 10 ** Math.floor(Math.log10(span / 5));
}

/** The legend ticks of one identified field: a filled scalar across its
 * palette domain, a vector field from its magnitude ceiling down to zero.
 * Null where the pair has no registered legend — the caller then builds one
 * from the file's own codebook. */
export function isobaricLegend(identity: VariableIdentity): string[] | null {
  const { family, level, vector } = identity;
  if (vector) return rangeLegend([0, vectorMaxMagnitude(family, level)], 10);
  if (family === "tmp" && isRegisteredLevel(level)) return rangeLegend(temperatureLegendRange(level), 5);
  if (family === "rh" && isRegisteredLevel(level)) return rangeLegend([0, 100], 20);
  if (family === "spfh" && isRegisteredLevel(level)) {
    const max = SPECIFIC_HUMIDITY_MAX[level];
    return rangeLegend([0, max], max >= 20 ? 5 : max >= 5 ? 1 : 0.25);
  }
  return null;
}

/** The isobaric members that are filled fields or vector fields — everything
 * the level row can put in the fill slot. The pressure family is the lines
 * slot's and lives in pressure.ts. */
export const ISOBARIC_FILL_IDS: readonly ForecastBundleId[] = (["tmp", "rh", "spfh", "wind", "qflux"] as const).flatMap(
  (family) => ISOBARIC_LEVELS.map((level) => `${family}${level}` as ForecastBundleId),
);
