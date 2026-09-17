/**
 * The variable table: everything the shell knows about a field as a thing
 * to show, written once.
 *
 * A variable's *identity* is its GRIB2 parameter block (identity.ts reads
 * it off the file); its *chart* is the palette, range and ceiling keyed by
 * that identity (levels.ts, pressure.ts, palettes.ts). What is left — the
 * instrument code, the headline, the localized label, the legend's ticks,
 * the ground under it, the `?type=` spellings, the meteogram and showcase
 * codes, which rail family and which group of the field sheet it belongs
 * to — used to sit in ten tables across as many files, each keyed by the
 * bundle id, none of them the authority. This is that authority: one
 * `VariableSpec` per registered bundle id, and every one of those tables
 * derives from it. `identity.ts` reads the id ↔ identity maps off it;
 * `urlstate.ts` its aliases; `meteogram.ts` and `showcase.ts` their codes;
 * `main.ts` the copy, the legend, the ground and the sheet's groups.
 *
 * Two families live here and are not the same question. `chart` is what
 * the field *is* (a `ChartFamily`: the four cloud covers are four); `family`
 * is which rail tile stands for it (an `IsobaricFamily`: the four cloud
 * covers are one), which no parameter block can say. A single-tile field
 * has `family: null`.
 *
 * The isobaric entries are generated from the family registry rather than
 * written out — eight surfaces of eight families differ only in the level
 * — and the pressure family's from the level registry. The surface fields
 * are written out, in the order the field sheet lists them.
 */

import { t, type MessageKey } from "./i18n";
import type { ChartFamily, VariableIdentity } from "./identity";
import { familyLabel, isobaricCode, isobaricLegend, type IsobaricFamily } from "./levels";
import { ISOBARIC_LEVELS, type IsobaricLevel, type KnownBundleId } from "./manifest";
import { PRESSURE_BUNDLE_IDS, PRESSURE_LEVELS, pressureCode, pressureLabel, pressureLegend, type PressureBundleId } from "./pressure";

/** The field sheet's groups, in the order it lists them. A quantity's
 * group is chart knowledge — what a forecaster would file it under — and
 * the sheet's last group, for bundles no entry stands for, is the sheet's
 * own. */
export type FieldGroup = "temperature" | "moisture" | "wind" | "dynamics" | "radiation" | "ocean" | "satellite";
export const FIELD_GROUPS: readonly FieldGroup[] = ["temperature", "moisture", "wind", "dynamics", "radiation", "ocean", "satellite"];

/** The ground a field is drawn on, by what its palette needs (main.ts
 * maps each to its tones per theme): an opaque coat's near-void, a
 * translucent wash's slate, the wind's own, the radiation's and the
 * radar's, and the chart stock under the contour lines. */
export type GroundId = "coat" | "slate" | "wind" | "solar" | "radar" | "chart";

export interface VariableSpec {
  id: KnownBundleId;
  /** What the field is; what the palette and the legend range key on. */
  chart: ChartFamily;
  level: IsobaricLevel | null;
  vector: boolean;
  /** The rail tile the field is behind, or null for a tile of its own. */
  family: IsobaricFamily | null;
  /** The field sheet's group; null for the lines, which are no field. */
  group: FieldGroup | null;
  /** The instrument code, after the model's ("GFS / TMP 2M"). */
  code: string;
  /** The headline over the map, one line at display size. */
  title: readonly string[];
  /** The data card's name for the buffer. */
  bufferTitle: string;
  /** The localized name; read at use, never captured, so it follows a
   * language switch. */
  label: () => string;
  /** The legend's six ticks, coarsest first. */
  legend: () => readonly string[];
  /** Whether the legend bar is the stylesheet's hand-written gradient
   * (`body[data-variable=…] .legend-bar`) or read off the palette. */
  legendGradient: "stylesheet" | "palette";
  ground: GroundId;
  /** The canonical `?type=` spelling; the id itself is always accepted. */
  urlName: string;
  /** Other accepted spellings, lowercase; unique across the table. */
  urlAliases: readonly string[];
  /** The code a meteogram row labels the field by, where a row reads it. */
  meteogramCode: string | null;
  /** The code a showcase card names the field by. */
  showcaseCode: string;
}

interface SurfaceEntry {
  id: KnownBundleId;
  chart: ChartFamily;
  vector?: boolean;
  family?: IsobaricFamily;
  group: FieldGroup;
  code: string;
  title: readonly string[];
  bufferTitle: string;
  labelKey: MessageKey;
  /** Six fixed ticks, else the chart ceiling's (`isobaricLegend`). */
  legend?: readonly string[];
  legendGradient?: "stylesheet";
  ground: GroundId;
  urlName: string;
  urlAliases?: readonly string[];
  meteogramCode?: string;
  showcaseCode: string;
}

function surface(entry: SurfaceEntry): VariableSpec {
  const identity: VariableIdentity = { family: entry.chart, level: null, vector: entry.vector ?? false };
  return {
    id: entry.id,
    chart: entry.chart,
    level: null,
    vector: entry.vector ?? false,
    family: entry.family ?? null,
    group: entry.group,
    code: entry.code,
    title: entry.title,
    bufferTitle: entry.bufferTitle,
    label: () => t(entry.labelKey),
    legend: entry.legend ? () => entry.legend! : () => isobaricLegend(identity) ?? [],
    legendGradient: entry.legendGradient ?? "palette",
    ground: entry.ground,
    urlName: entry.urlName,
    urlAliases: entry.urlAliases ?? [],
    meteogramCode: entry.meteogramCode ?? null,
    showcaseCode: entry.showcaseCode,
  };
}

interface IsobaricEntry {
  family: IsobaricFamily & ChartFamily;
  vector?: boolean;
  group: FieldGroup;
  /** The headline's word after the level ("850 hPa Temperature"). */
  word: string;
  /** The showcase card's family word ("T 850MB"). */
  showcaseWord: string;
  ground: GroundId;
  /** Spellings for particular levels, on top of the family rule
   * `urlstate.ts` resolves (`t850`, `z500`). */
  urlAliases?: Partial<Record<IsobaricLevel, readonly string[]>>;
}

function isobaric(entry: IsobaricEntry): VariableSpec[] {
  return ISOBARIC_LEVELS.map((level) => {
    const id = `${entry.family}${level}` as KnownBundleId;
    const identity: VariableIdentity = { family: entry.family, level, vector: entry.vector ?? false };
    return {
      id,
      chart: entry.family,
      level,
      vector: entry.vector ?? false,
      family: entry.family,
      group: entry.group,
      code: isobaricCode(id),
      title: [`${level} hPa`, entry.word],
      bufferTitle: `${entry.word} buffer`,
      label: () => familyLabel(id),
      legend: () => isobaricLegend(identity) ?? [],
      legendGradient: "palette",
      ground: entry.ground,
      urlName: id,
      urlAliases: entry.urlAliases?.[level] ?? [],
      meteogramCode: null,
      showcaseCode: `${entry.showcaseWord} ${level}MB`,
    };
  });
}

/** The pressure family, from the level registry: drawn as lines, never a
 * field, so no group; the sea level pressure is the one meteogram reads. */
function pressure(): VariableSpec[] {
  return PRESSURE_BUNDLE_IDS.map((id: PressureBundleId) => {
    const level = PRESSURE_LEVELS[id].levelHpa as IsobaricLevel | null;
    return {
      id,
      chart: "hgt",
      level,
      vector: false,
      family: "hgt",
      group: null,
      code: pressureCode(id),
      title: level === null ? ["Sea Level", "Pressure"] : [`${level} hPa`, "Height"],
      bufferTitle: level === null ? "Pressure buffer" : "Height buffer",
      label: () => pressureLabel(id),
      legend: () => pressureLegend(id),
      legendGradient: "palette",
      ground: "chart",
      urlName: id === "prmsl" ? "pressure" : id,
      // The subtropical high is read off the 500 hPa chart, so the view has
      // the name people look for as well as the level's own.
      urlAliases: id === "prmsl" ? ["mslp", "msl"] : id === "hgt500" ? ["subtropicalhigh"] : [],
      meteogramCode: id === "prmsl" ? "PRMSL" : null,
      showcaseCode: id === "prmsl" ? "MSLP" : `HGT ${level}MB`,
    };
  });
}

/** The table, in the field sheet's order: group by group, a family's
 * levels beside its surface member. Built on first use rather than at
 * load: this module sits on a cycle (identity.ts reads it, and the level
 * and pressure registries it reads are behind identity.ts), so nothing
 * here may run while those are still initialising. */
function buildSpecs(): readonly VariableSpec[] {
  return [
  // Temperature. The opaque coats sit on the near-void ground.
  surface({
    id: "tmp2m",
    chart: "tmp",
    family: "tmp",
    group: "temperature",
    code: "TMP 2M",
    title: ["Surface", "Temperature"],
    bufferTitle: "Temperature buffer",
    labelKey: "varLabelTmp2m",
    legend: ["50", "30", "10", "-10", "-30", "-60"],
    legendGradient: "stylesheet",
    ground: "coat",
    urlName: "temp",
    urlAliases: ["temperature", "tmp", "t2m"],
    meteogramCode: "TMP",
    showcaseCode: "TEMP",
  }),
  ...isobaric({ family: "tmp", group: "temperature", word: "Temperature", showcaseWord: "T", ground: "coat" }),
  surface({
    id: "aptmp2m",
    chart: "aptmp2m",
    group: "temperature",
    code: "APTMP 2M",
    title: ["Apparent", "Temperature"],
    bufferTitle: "Apparent temperature buffer",
    labelKey: "varLabelAptmp2m",
    ground: "coat",
    urlName: "feelslike",
    urlAliases: ["apparent", "aptmp"],
    showcaseCode: "FEELS",
  }),
  surface({
    id: "dpt2m",
    chart: "dpt2m",
    group: "temperature",
    code: "DPT 2M",
    title: ["Dew", "Point"],
    bufferTitle: "Dew point buffer",
    labelKey: "varLabelDpt2m",
    ground: "coat",
    urlName: "dewpoint",
    urlAliases: ["dew", "td"],
    meteogramCode: "DPT",
    showcaseCode: "DEWPT",
  }),
  // The skin temperature is a coat like the 2 m temperature; `sst` is what
  // it is searched for, though over land it is the ground's skin.
  surface({
    id: "tmpsfc",
    chart: "tmpsfc",
    group: "temperature",
    code: "TMP SFC",
    title: ["Sea Surface", "Temperature"],
    bufferTitle: "Surface temperature buffer",
    labelKey: "varLabelTmpsfc",
    ground: "coat",
    urlName: "sst",
    urlAliases: ["skin", "skintemp", "tsfc"],
    showcaseCode: "SST",
  }),
  // Moisture. The translucent washes — humidity, precipitation, cloud,
  // the reduced-visibility ramp — sit on the slate, where the map carries
  // the geography and the low end of each ramp still shows.
  ...isobaric({ family: "rh", group: "moisture", word: "Humidity", showcaseWord: "RH", ground: "slate" }),
  ...isobaric({ family: "spfh", group: "moisture", word: "Specific Humidity", showcaseWord: "Q", ground: "slate" }),
  surface({
    id: "prate",
    chart: "prate",
    group: "moisture",
    code: "PRATE SFC",
    title: ["Precipitation", "Rate"],
    bufferTitle: "Precipitation buffer",
    labelKey: "varLabelPrate",
    legend: ["128", "40", "20", "5", "1", "0"],
    legendGradient: "stylesheet",
    ground: "slate",
    urlName: "precip",
    urlAliases: ["precipitation", "rain"],
    meteogramCode: "PRATE",
    showcaseCode: "PRECIP",
  }),
  // Radar echoes are small and bright; the ground stays dark enough for a
  // 5 dBZ edge to read against it.
  surface({
    id: "cref",
    chart: "cref",
    group: "moisture",
    code: "CREF EATM",
    title: ["Composite", "Reflectivity"],
    bufferTitle: "Reflectivity buffer",
    labelKey: "varLabelCref",
    legend: ["75", "60", "45", "30", "15", "0"],
    legendGradient: "stylesheet",
    ground: "radar",
    urlName: "radar",
    urlAliases: ["reflectivity"],
    showcaseCode: "RADAR",
  }),
  surface({
    id: "tcdc",
    chart: "tcdc",
    family: "cloud",
    group: "moisture",
    code: "TCDC EATM",
    title: ["Cloud", "Cover"],
    bufferTitle: "Cloud buffer",
    labelKey: "varLabelTcdc",
    ground: "slate",
    urlName: "cloud",
    urlAliases: ["clouds", "cloudcover"],
    meteogramCode: "TCDC",
    showcaseCode: "CLOUD",
  }),
  surface({
    id: "lcdc",
    chart: "lcdc",
    family: "cloud",
    group: "moisture",
    code: "LCDC LOW",
    title: ["Low", "Cloud"],
    bufferTitle: "Cloud buffer",
    labelKey: "varLabelLcdc",
    ground: "slate",
    urlName: "lowcloud",
    meteogramCode: "LOW",
    showcaseCode: "CLOUD LOW",
  }),
  surface({
    id: "mcdc",
    chart: "mcdc",
    family: "cloud",
    group: "moisture",
    code: "MCDC MID",
    title: ["Middle", "Cloud"],
    bufferTitle: "Cloud buffer",
    labelKey: "varLabelMcdc",
    ground: "slate",
    urlName: "midcloud",
    urlAliases: ["middlecloud"],
    meteogramCode: "MID",
    showcaseCode: "CLOUD MID",
  }),
  surface({
    id: "hcdc",
    chart: "hcdc",
    family: "cloud",
    group: "moisture",
    code: "HCDC HIGH",
    title: ["High", "Cloud"],
    bufferTitle: "Cloud buffer",
    labelKey: "varLabelHcdc",
    ground: "slate",
    urlName: "highcloud",
    meteogramCode: "HIGH",
    showcaseCode: "CLOUD HIGH",
  }),
  // Wind. The speed fields and the gust, translucent at the low end, on
  // the wind's own ground.
  surface({
    id: "wind10m",
    chart: "wind",
    vector: true,
    family: "wind",
    group: "wind",
    code: "WIND 10M",
    title: ["Surface", "Wind"],
    bufferTitle: "Wind buffer",
    labelKey: "varLabelWind10m",
    legend: ["40", "30", "20", "10", "5", "0"],
    legendGradient: "stylesheet",
    ground: "wind",
    urlName: "wind",
    meteogramCode: "WIND",
    showcaseCode: "WIND",
  }),
  ...isobaric({ family: "wind", vector: true, group: "wind", word: "Wind", showcaseWord: "WIND", ground: "wind" }),
  surface({
    id: "gust",
    chart: "gust",
    group: "wind",
    code: "GUST SFC",
    title: ["Wind", "Gust"],
    bufferTitle: "Gust buffer",
    labelKey: "varLabelGust",
    ground: "wind",
    urlName: "gust",
    urlAliases: ["gusts"],
    meteogramCode: "GUST",
    showcaseCode: "GUST",
  }),
  // Dynamics. Stable air is the map, so CAPE's pale ramp takes the slate;
  // θe is a coat like the temperature.
  surface({
    id: "cape",
    chart: "cape",
    group: "dynamics",
    code: "CAPE SFC",
    title: ["Convective", "Energy"],
    bufferTitle: "CAPE buffer",
    labelKey: "varLabelCape",
    ground: "slate",
    urlName: "cape",
    urlAliases: ["instability"],
    showcaseCode: "CAPE",
  }),
  ...isobaric({ family: "vvel", group: "dynamics", word: "Vertical Velocity", showcaseWord: "OMEGA", ground: "slate" }),
  ...isobaric({ family: "thetae", group: "dynamics", word: "Theta-e", showcaseWord: "THETAE", ground: "coat" }),
  ...isobaric({ family: "qflux", vector: true, group: "dynamics", word: "Vapour Flux", showcaseWord: "QFLUX", ground: "slate" }),
  // Radiation and visibility. Clear air is the map, and the reduced-
  // visibility ramp comes in translucent, on the slate; the radiation has
  // a ground of its own.
  surface({
    id: "vis",
    chart: "vis",
    group: "radiation",
    code: "VIS SFC",
    title: ["Surface", "Visibility"],
    bufferTitle: "Visibility buffer",
    labelKey: "varLabelVis",
    ground: "slate",
    urlName: "visibility",
    urlAliases: ["fog"],
    showcaseCode: "VIS",
  }),
  surface({
    id: "dswrf",
    chart: "dswrf",
    group: "radiation",
    code: "DSWRF SFC",
    title: ["Solar", "Radiation"],
    bufferTitle: "Radiation buffer",
    labelKey: "varLabelDswrf",
    legend: ["1200", "900", "600", "300", "100", "0"],
    ground: "solar",
    urlName: "solar",
    urlAliases: ["radiation"],
    showcaseCode: "SOLAR",
  }),
  // The ocean. The ice and wave fields leave open water and land to the
  // map — the ice ramp starts pale and translucent, the wave ramps at
  // nothing — so they take the slate.
  surface({
    id: "icec",
    chart: "icec",
    family: "ice",
    group: "ocean",
    code: "ICEC SFC",
    title: ["Sea Ice", "Cover"],
    bufferTitle: "Sea ice buffer",
    labelKey: "varLabelIcec",
    ground: "slate",
    urlName: "seaice",
    urlAliases: ["ice", "icecover", "iceconcentration"],
    showcaseCode: "ICE",
  }),
  surface({
    id: "icetk",
    chart: "icetk",
    family: "ice",
    group: "ocean",
    code: "ICETK SFC",
    title: ["Sea Ice", "Thickness"],
    bufferTitle: "Sea ice buffer",
    labelKey: "varLabelIcetk",
    ground: "slate",
    urlName: "icethickness",
    showcaseCode: "ICE THK",
  }),
  // The wave vector: the height as the fill, the direction as particles,
  // so its legend is the height's.
  surface({
    id: "wave",
    chart: "wave",
    vector: true,
    family: "wave",
    group: "ocean",
    code: "WAVE SFC",
    title: ["Waves", "Height · Direction"],
    bufferTitle: "Wave buffer",
    labelKey: "varLabelWave",
    ground: "slate",
    urlName: "waves",
    showcaseCode: "WAVE",
  }),
  surface({
    id: "htsgw",
    chart: "htsgw",
    family: "wave",
    group: "ocean",
    code: "HTSGW SFC",
    title: ["Significant", "Wave Height"],
    bufferTitle: "Wave buffer",
    labelKey: "varLabelHtsgw",
    ground: "slate",
    urlName: "waveheight",
    urlAliases: ["swh", "hs"],
    showcaseCode: "WAVE HS",
  }),
  surface({
    id: "perpw",
    chart: "perpw",
    family: "wave",
    group: "ocean",
    code: "PERPW SFC",
    title: ["Primary", "Wave Period"],
    bufferTitle: "Wave buffer",
    labelKey: "varLabelPerpw",
    ground: "slate",
    urlName: "waveperiod",
    urlAliases: ["period"],
    showcaseCode: "WAVE TP",
  }),
  surface({
    id: "dirpw",
    chart: "dirpw",
    group: "ocean",
    code: "DIRPW SFC",
    title: ["Primary", "Wave Direction"],
    bufferTitle: "Wave buffer",
    labelKey: "varLabelDirpw",
    ground: "slate",
    urlName: "wavedirection",
    urlAliases: ["wavedir"],
    showcaseCode: "WAVE DIR",
  }),
  // The satellite infrared window: cloud tops read cold and bright on an
  // inverted grey scale, the coldest enhanced in colour; clear sea and
  // land read dark, so the picture is the one every weather bulletin
  // shows. Outside the disk is the codebook bottom, painted as nothing.
  // The file is in kelvin, the channel's own unit; the legend and every
  // readout show it in Celsius (`units.ts`), like the other temperatures.
  surface({
    id: "ir104",
    chart: "ir104",
    group: "satellite",
    code: "IR 10.4",
    title: ["Infrared", "Imagery"],
    bufferTitle: "Imagery buffer",
    labelKey: "varLabelIr104",
    legend: ["60", "30", "0", "-30", "-60", "-90"],
    ground: "slate",
    urlName: "infrared",
    urlAliases: ["ir", "satellite", "sat", "bt"],
    meteogramCode: "IR",
    showcaseCode: "IR",
  }),
  // The lines.
  ...pressure(),
  ];
}

interface VariableTable {
  specs: readonly VariableSpec[];
  /** By id. Complete over `KnownBundleId`: a bundle id added to the type
   * without a row is a compile error in `buildSpecs`' return. */
  byId: Readonly<Record<KnownBundleId, VariableSpec>>;
  byIdentity: ReadonlyMap<string, VariableSpec>;
  /** The chart families registered on isobaric surfaces, with whether
   * their bundles are vectors. */
  isobaricFamilies: ReadonlyMap<ChartFamily, boolean>;
}

let built: VariableTable | null = null;

function table(): VariableTable {
  if (built) return built;
  const specs = buildSpecs();
  built = {
    specs,
    byId: Object.fromEntries(specs.map((spec) => [spec.id, spec])) as Record<KnownBundleId, VariableSpec>,
    byIdentity: new Map(specs.map((spec) => [`${spec.chart}/${spec.level}/${spec.vector}`, spec])),
    isobaricFamilies: new Map(specs.filter((spec) => spec.level !== null).map((spec) => [spec.chart, spec.vector])),
  };
  return built;
}

/** Every registered id, in the table's order. */
export function variableIds(): readonly KnownBundleId[] {
  return table().specs.map((spec) => spec.id);
}

/** The spec of a bundle id, or null for one this build has no row for. */
export function variableSpec(id: string): VariableSpec | null {
  return (table().byId as Record<string, VariableSpec | undefined>)[id] ?? null;
}

/** The spec whose identity this is, or null for an identity no row has —
 * a surface nothing is registered on, or a parameter block this build
 * does not chart. */
export function specForIdentity(identity: VariableIdentity | null): VariableSpec | null {
  if (identity === null) return null;
  return table().byIdentity.get(`${identity.family}/${identity.level}/${identity.vector}`) ?? null;
}

/** Whether a chart family is registered on isobaric surfaces — and if so
 * whether its bundles are vectors — which is what lets a level nothing is
 * registered on still be read by the naming convention. Undefined for a
 * family that is not. */
export function isobaricChartFamily(family: ChartFamily): boolean | undefined {
  return table().isobaricFamilies.get(family);
}
