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
 * the wind family, mean sea level pressure the pressure family. Three
 * families are not isobaric at all and list their members outright: cloud
 * cover (the total and the three layers), sea ice (cover and thickness) and
 * waves (significant height and primary period), and the level row picks
 * among those the same way. Precipitation, radiation, reflectivity, the
 * other surface diagnostics (gust, CAPE, visibility, dew point, apparent
 * temperature) and the skin temperature are single layers and belong to no
 * family.
 *
 * Everything here is chart knowledge, not container knowledge: the codebook
 * ranges are copied from the encoders and held to them by
 * `tests/fixtures/isobaric-registry.json` (`tests/web/levels.test.ts`),
 * `tests/fixtures/surface-registry.json` (`tests/web/surface.test.ts`) and
 * `tests/fixtures/ocean-registry.json` (`tests/web/ocean.test.ts`), the way
 * pressure.ts is held by its own registry.
 */
export type IsobaricFamily = "hgt" | "tmp" | "rh" | "spfh" | "wind" | "qflux" | "vvel" | "thetae" | "cloud" | "ice" | "wave";

export const ISOBARIC_FAMILIES: readonly IsobaricFamily[] = [
  "hgt",
  "tmp",
  "rh",
  "spfh",
  "wind",
  "qflux",
  "vvel",
  "thetae",
  "cloud",
  "ice",
  "wave",
];

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
  /** The rail tile's visually hidden gloss, which the surface member's level
   * button repeats; null for a family the shell writes no tile for. */
  glossKey: MessageKey | null;
  /** A family whose members are not the eight isobaric surfaces lists them
   * outright, in level-row order, each with its level-row label. */
  members?: readonly { id: ForecastBundleId; code: string }[];
}

export const FAMILIES: Record<IsobaricFamily, FamilyInfo> = {
  hgt: {
    id: "hgt",
    kind: "lines",
    surface: "prmsl",
    code: "PRESSURE",
    surfaceCode: "MSL",
    glossKey: "varPressureField",
  },
  tmp: { id: "tmp", kind: "scalar", surface: "tmp2m", code: "TEMP", surfaceCode: "2M", glossKey: "varTemp" },
  rh: { id: "rh", kind: "scalar", surface: null, code: "RH", surfaceCode: "", glossKey: "varHumidity" },
  // Registered on both encoders and fetched by GFS as the vapour flux's
  // input, but no source publishes it, so the shell writes no tile.
  spfh: { id: "spfh", kind: "scalar", surface: null, code: "SPFH", surfaceCode: "", glossKey: null },
  wind: { id: "wind", kind: "vector", surface: "wind10m", code: "WIND", surfaceCode: "10M", glossKey: "varWind" },
  qflux: { id: "qflux", kind: "vector", surface: null, code: "QFLUX", surfaceCode: "", glossKey: "varVapourFlux" },
  vvel: { id: "vvel", kind: "scalar", surface: null, code: "OMEGA", surfaceCode: "", glossKey: "varOmega" },
  thetae: { id: "thetae", kind: "scalar", surface: null, code: "THETAE", surfaceCode: "", glossKey: "varThetaE" },
  // Cloud cover: the total heads the family and the three layers are its
  // members, so one rail tile and the level row serve all four.
  cloud: {
    id: "cloud",
    kind: "scalar",
    surface: "tcdc",
    code: "CLOUD",
    surfaceCode: "TOTAL",
    glossKey: "varCloud",
    members: [
      { id: "tcdc", code: "TOTAL" },
      { id: "lcdc", code: "LOW" },
      { id: "mcdc", code: "MID" },
      { id: "hcdc", code: "HIGH" },
    ],
  },
  // Sea ice: concentration heads the family, thickness is its other member.
  ice: {
    id: "ice",
    kind: "scalar",
    surface: "icec",
    code: "ICE",
    surfaceCode: "COVER",
    glossKey: "varIce",
    members: [
      { id: "icec", code: "COVER" },
      { id: "icetk", code: "THICK" },
    ],
  },
  // Waves: the significant height heads the family, the primary period is
  // its other member. The primary direction (`dirpw`) is published too but
  // is no member: a direction is not a fill — as a scalar it wraps at
  // north and paints land, which is 0 in the file, as north — so it waits
  // for the arrow or particle rendering that reads it properly, and is
  // reachable by URL (`?type=wavedirection`) meanwhile.
  wave: {
    id: "wave",
    kind: "scalar",
    surface: "htsgw",
    code: "WAVE",
    surfaceCode: "HEIGHT",
    glossKey: "varWave",
    members: [
      { id: "htsgw", code: "HEIGHT" },
      { id: "perpw", code: "PERIOD" },
    ],
  },
};

/** Bundles the shell has chart knowledge for but deliberately writes no rail
 * tile for: the primary wave direction, which is not a fill (see the wave
 * family above) and waits for an arrow rendering. Reachable by URL; a run
 * that ships one gets no generic tile for it either. */
export const UNTILED_BUNDLE_IDS: readonly ForecastBundleId[] = ["dirpw"];

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
    const info = FAMILIES[family];
    if (info.surface === id || info.members?.some((member) => member.id === id)) return family;
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
  const match = /^(?:hgt|tmp|rh|spfh|wind|qflux|vvel|thetae)(\d+)$/.exec(id);
  if (!match) return null;
  const level = Number(match[1]);
  return (ISOBARIC_LEVELS as readonly number[]).includes(level) ? (level as IsobaricLevel) : null;
}

/** Every member of a family in level-row order: the surface first, then the
 * isobaric surfaces from the ground up. */
export function familyMembers(family: IsobaricFamily): ForecastBundleId[] {
  const info = FAMILIES[family];
  if (info.members) return info.members.map((member) => member.id);
  const members: ForecastBundleId[] = info.surface ? [info.surface] : [];
  for (const level of ISOBARIC_LEVELS) members.push(`${family}${level}` as ForecastBundleId);
  return members;
}

/** The level row's label for one member: "MSL" / "2M" / "10M" for a surface
 * member, the pressure in hPa otherwise — or the member's own label in a
 * family that lists its members ("TOTAL", "LOW", "MID", "HIGH"). */
export function levelCode(id: ForecastBundleId): string {
  const family = familyOf(id);
  if (family === null) return id.toUpperCase();
  const listed = FAMILIES[family].members?.find((member) => member.id === id);
  if (listed) return listed.code;
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
  if (family === "vvel") return [-6.35, 6.35];
  if (family === "thetae") return [THETA_E_OFFSETS[level], THETA_E_OFFSETS[level] + 127];
  return null;
}

/** Codebook offsets of the equivalent potential temperature per level, in
 * K: a 127 K window at 0.5 K each. Copied from xuebuild/quantize.py. */
const THETA_E_OFFSETS: Record<IsobaricLevel, number> = {
  1000: 235,
  925: 232,
  850: 230,
  700: 235,
  500: 250,
  300: 285,
  250: 295,
  200: 305,
};

/** The θe ramp's domain on one surface: the middle hundred kelvin of the
 * level's codebook — 255–355 K at 850 hPa, where a moist tropical air mass
 * sits near 350 and a polar one near 280 — so the legend reads in twenties.
 * An unregistered level takes the 850 hPa window. */
export function thetaEPaletteDomain(level: number | null): readonly [number, number] {
  const offset = isRegisteredLevel(level) ? THETA_E_OFFSETS[level] : THETA_E_OFFSETS[850];
  return [offset + 25, offset + 125];
}

/** The ω ramp's domain: ±2.5 Pa/s, where synoptic ascent lives; the
 * codebook keeps going to ±6.35 so a convective core stays distinct in a
 * probe, but the ramp saturates before it. */
export const OMEGA_PALETTE_MAX = 2.5;

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

/** The ocean set's chart ceilings, in the same spirit. Sea ice thickness
 * reads to 5 m, the whole codebook. Significant wave height saturates at
 * 10 m: a storm sea, past which the codebook's 25.4 m keeps the extreme
 * distinct in a probe. The primary period reads to 20 s, the longest swell
 * that crosses an ocean. Held to `tests/fixtures/ocean-registry.json` by
 * `tests/web/ocean.test.ts`. */
export const ICE_THICKNESS_CHART_MAX = 5;
export const WAVE_HEIGHT_CHART_MAX = 10;
export const WAVE_PERIOD_CHART_MAX = 20;

/** The surface diagnostics' chart ceilings — where each palette saturates
 * and what its legend spans, narrower than the codebook where the codebook
 * keeps headroom the chart does not need. Gust reuses the wind speed ramp
 * stretched to 50 m/s: a 10 m wind rarely passes 30, a gust routinely does,
 * and 50 is a category-3 typhoon's. CAPE saturates at 5000 J/kg, the top
 * class of a severe-weather chart; the codebook's 6350 keeps the rare
 * extreme distinct in a probe without stretching the ramp for it. Cloud
 * cover is the whole 0–100 %. Held to `tests/fixtures/surface-registry.json`
 * by `tests/web/surface.test.ts`. */
export const GUST_SPEED_MAX = 50;
export const CAPE_CHART_MAX = 5000;

/** The value span a filled scalar's legend reads over, or null where the
 * span is the codebook's own: a registered temperature surface takes its
 * windowed domain, a surface diagnostic its chart ceiling. */
export function scalarLegendRange(identity: VariableIdentity): readonly [number, number] | null {
  const { family, level, vector } = identity;
  if (vector) return null;
  if (family === "tmp" && isRegisteredLevel(level)) return temperatureLegendRange(level);
  if (family === "thetae" && isRegisteredLevel(level)) return thetaEPaletteDomain(level);
  if (family === "vvel" && isRegisteredLevel(level)) return [-OMEGA_PALETTE_MAX, OMEGA_PALETTE_MAX];
  if (family === "gust") return [0, GUST_SPEED_MAX];
  if (family === "tcdc" || family === "lcdc" || family === "mcdc" || family === "hcdc") return [0, 100];
  if (family === "cape") return [0, CAPE_CHART_MAX];
  if (family === "vis") return [0, VISIBILITY_CHART_MAX];
  if (family === "dpt2m") return DEW_POINT_CHART_RANGE;
  if (family === "aptmp2m") return APPARENT_CHART_RANGE;
  // The skin temperature reads over the 2 m temperature's ramp; the
  // codebook's 67 °C desert top holds the ramp's last colour.
  if (family === "tmpsfc") return temperaturePaletteDomain(null);
  if (family === "icec") return [0, 100];
  if (family === "icetk") return [0, ICE_THICKNESS_CHART_MAX];
  if (family === "htsgw") return [0, WAVE_HEIGHT_CHART_MAX];
  if (family === "perpw") return [0, WAVE_PERIOD_CHART_MAX];
  if (family === "dirpw") return [0, 360];
  return null;
}

/** Visibility reads to 25 km — the codebook's 25.4 without the odd tenth;
 * the ramp is transparent long before that. */
export const VISIBILITY_CHART_MAX = 25;
/** The dew point ramp's domain: the moisture a reader cares about lives
 * between an arid -30 °C and a tropical 30. */
export const DEW_POINT_CHART_RANGE: readonly [number, number] = [-30, 30];
/** The apparent temperature reads over the temperature ramp's own domain,
 * trimmed to ±50 so the legend ticks in twenties; the codebook's extremes
 * beyond it hold the ramp's end colours. */
export const APPARENT_CHART_RANGE: readonly [number, number] = [-50, 50];

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
    const listed = MEMBER_LABEL_KEYS[id];
    if (listed) return t(listed);
    return id;
  }
  const key: MessageKey = {
    tmp: "varLabelTempAtLevel",
    rh: "varLabelRhAtLevel",
    spfh: "varLabelSpfhAtLevel",
    wind: "varLabelWindAtLevel",
    qflux: "varLabelQfluxAtLevel",
    hgt: "varLabelHeightAtLevel",
    vvel: "varLabelVvelAtLevel",
    thetae: "varLabelThetaeAtLevel",
    cloud: "varLabelTcdc",
    ice: "varLabelIcec",
    wave: "varLabelHtsgw",
  }[family!] as MessageKey;
  return t(key).replace("{level}", String(level));
}

/** The labels of the listed members — the cloud layers, the ice and wave
 * fields — which have no level to substitute. */
const MEMBER_LABEL_KEYS: Record<string, MessageKey> = {
  tcdc: "varLabelTcdc",
  lcdc: "varLabelLcdc",
  mcdc: "varLabelMcdc",
  hcdc: "varLabelHcdc",
  icec: "varLabelIcec",
  icetk: "varLabelIcetk",
  htsgw: "varLabelHtsgw",
  perpw: "varLabelPerpw",
};

/** The instrument-panel code of one isobaric member: "TMP 850MB", "RH
 * 700MB", "WIND 850MB", "QFLUX 850MB". */
export function isobaricCode(id: ForecastBundleId): string {
  const level = bundleLevel(id);
  const family = familyOf(id);
  if (level === null || family === null) return id.toUpperCase();
  const word = {
    hgt: "HGT",
    tmp: "TMP",
    rh: "RH",
    spfh: "SPFH",
    wind: "WIND",
    qflux: "QFLUX",
    vvel: "OMEGA",
    thetae: "THETAE",
    cloud: "CLOUD",
    ice: "ICE",
    wave: "WAVE",
  }[family];
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
 * palette domain, a vector field from its magnitude ceiling down to zero,
 * a surface diagnostic across its chart ceiling. Null where the pair has no
 * registered legend — the caller then builds one from the file's own
 * codebook. */
export function isobaricLegend(identity: VariableIdentity): string[] | null {
  const { family, level, vector } = identity;
  if (vector) return rangeLegend([0, vectorMaxMagnitude(family, level)], 10);
  if (family === "gust") return rangeLegend([0, GUST_SPEED_MAX], 10);
  if (family === "tcdc" || family === "lcdc" || family === "mcdc" || family === "hcdc") return rangeLegend([0, 100], 20);
  if (family === "cape") return rangeLegend([0, CAPE_CHART_MAX], 1000);
  if (family === "vis") return rangeLegend([0, VISIBILITY_CHART_MAX], 5);
  if (family === "dpt2m") return rangeLegend(DEW_POINT_CHART_RANGE, 6);
  if (family === "aptmp2m") return rangeLegend(APPARENT_CHART_RANGE, 10);
  if (family === "tmpsfc") return rangeLegend(temperaturePaletteDomain(null), 10);
  if (family === "icec") return rangeLegend([0, 100], 20);
  if (family === "icetk") return rangeLegend([0, ICE_THICKNESS_CHART_MAX], 1);
  if (family === "htsgw") return rangeLegend([0, WAVE_HEIGHT_CHART_MAX], 2);
  if (family === "perpw") return rangeLegend([0, WAVE_PERIOD_CHART_MAX], 4);
  if (family === "dirpw") return rangeLegend([0, 360], 45);
  if (family === "vvel" && isRegisteredLevel(level)) return rangeLegend([-OMEGA_PALETTE_MAX, OMEGA_PALETTE_MAX], 0.5);
  if (family === "thetae" && isRegisteredLevel(level)) return rangeLegend(thetaEPaletteDomain(level), 5);
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
export const ISOBARIC_FILL_IDS: readonly ForecastBundleId[] = (["tmp", "rh", "spfh", "wind", "qflux", "vvel", "thetae"] as const).flatMap(
  (family) => ISOBARIC_LEVELS.map((level) => `${family}${level}` as ForecastBundleId),
);
