/**
 * Display units: what a value is shown in when that differs from the unit
 * the file carries.
 *
 * Every conversion here is display only: nothing changes a value that is
 * stored, compared or handed to a shader. A file unit belongs to one
 * quantity (a temperature, a wind speed, a pressure, a precipitation depth
 * or rate, a height, a distance), and the viewer picks a unit per quantity:
 * a preset (`auto`, `metric`, `imperial`) fills all of them, and a single
 * quantity can be overridden on top of it. `auto` reads the browser's
 * region, not the page language: a Japanese reader in Ohio still drives in
 * miles.
 *
 * A satellite brightness temperature is published in kelvin (the file, its
 * codebook and its palette stops all read in K), but a weather map reads
 * cloud tops as −50 °C, not 223 K, so kelvin is a temperature like any
 * other. A dimensionless quantity — an optical depth, whose unit the
 * encoder writes as `1` the way a CF file does — shows no unit at all. The
 * EDR's m^(2/3) s^-1 reads with the fraction glyph, and a volume or an area
 * per anything takes its superscript (m³/s, km²).
 *
 * Two quantities keep their unit whatever the choice, because the unit is
 * part of what they are: a geopotential height on a pressure surface is
 * charted in metres (and its contours drawn at metre intervals) the world
 * over, and a potential temperature (θ, θe) reads in kelvin — its legend is
 * ticked in K. Both are told apart by their GRIB2 parameter block, which
 * every helper here takes as an optional last argument.
 *
 * The choice is resolved once at load — `?units=`, then the stored choice,
 * then the preset `auto` — and changes in place: `units` is a live binding,
 * and whatever shows a value re-reads it on `onUnitsChange`.
 */

import type { BundleParameter, BundleVariable } from "./manifest";

export type TemperatureUnit = "C" | "F";
export type WindUnit = "ms" | "kmh" | "kt" | "mph";
export type PressureUnit = "hPa" | "kPa" | "inHg" | "mmHg";
export type PrecipitationUnit = "mm" | "in";
export type HeightUnit = "m" | "ft";
export type DistanceUnit = "km" | "mi" | "nmi";

/** The unit shown for each quantity. */
export interface UnitChoice {
  temperature: TemperatureUnit;
  wind: WindUnit;
  pressure: PressureUnit;
  precipitation: PrecipitationUnit;
  height: HeightUnit;
  distance: DistanceUnit;
}
export type UnitQuantity = keyof UnitChoice;
export type UnitPreset = "auto" | "metric" | "imperial";

/** What the viewer chose: a preset, and the quantities set apart from it. */
export interface UnitPrefs {
  preset: UnitPreset;
  overrides: Partial<UnitChoice>;
}

export const UNIT_QUANTITIES: readonly UnitQuantity[] = ["temperature", "wind", "pressure", "precipitation", "height", "distance"];
export const UNIT_PRESETS: readonly UnitPreset[] = ["auto", "metric", "imperial"];
/** The choices for each quantity, in the order the settings sheet lists them. */
export const UNIT_OPTIONS: { readonly [Q in UnitQuantity]: readonly UnitChoice[Q][] } = {
  temperature: ["C", "F"],
  wind: ["ms", "kmh", "kt", "mph"],
  pressure: ["hPa", "kPa", "inHg", "mmHg"],
  precipitation: ["mm", "in"],
  height: ["m", "ft"],
  distance: ["km", "mi", "nmi"],
};

/** Each choice as a readout prints it (the speed's, the depth's). A rate is
 * the depth's unit per hour (`precipitationRateLabel`). */
export const UNIT_LABELS: { readonly [Q in UnitQuantity]: Readonly<Record<UnitChoice[Q], string>> } = {
  temperature: { C: "°C", F: "°F" },
  wind: { ms: "m/s", kmh: "km/h", kt: "kt", mph: "mph" },
  pressure: { hPa: "hPa", kPa: "kPa", inHg: "inHg", mmHg: "mmHg" },
  precipitation: { mm: "mm", in: "in" },
  height: { m: "m", ft: "ft" },
  distance: { km: "km", mi: "mi", nmi: "NM" },
};

export const METRIC_UNITS: UnitChoice = { temperature: "C", wind: "ms", pressure: "hPa", precipitation: "mm", height: "m", distance: "km" };
export const IMPERIAL_UNITS: UnitChoice = { temperature: "F", wind: "mph", pressure: "inHg", precipitation: "in", height: "ft", distance: "mi" };

/** Regions that read the US customary units: the United States and its
 * territories, Liberia, Myanmar. */
const IMPERIAL_REGIONS = new Set(["US", "PR", "GU", "VI", "AS", "MP", "UM", "LR", "MM"]);
/** Regions whose weather services and public read wind in metres per
 * second; the rest of the metric world reads km/h. */
const METRES_PER_SECOND_REGIONS = new Set([
  "JP", "CN", "KR", "KP", "TW", "MO", "MN",
  "RU", "BY", "UA", "KZ", "KG", "UZ", "TJ", "TM", "AM", "AZ", "GE", "MD",
  "SE", "NO", "FI", "DK", "IS", "EE", "LV", "LT", "PL", "CZ", "SK",
]);

/** What `auto` resolves to in a region (an ISO 3166 alpha-2 code, or null
 * when the browser names none). */
export function regionUnits(region: string | null): UnitChoice {
  const code = region?.toUpperCase() ?? null;
  if (code !== null && IMPERIAL_REGIONS.has(code)) return { ...IMPERIAL_UNITS };
  // The United Kingdom: Celsius and millibars on the forecast, miles and
  // mph on the road.
  if (code === "GB" || code === "IM" || code === "JE" || code === "GG") {
    return { ...METRIC_UNITS, wind: "mph", distance: "mi" };
  }
  // Environment Canada reports pressure in kilopascals.
  if (code === "CA") return { ...METRIC_UNITS, wind: "kmh", pressure: "kPa" };
  if (code === null || METRES_PER_SECOND_REGIONS.has(code)) return { ...METRIC_UNITS };
  return { ...METRIC_UNITS, wind: "kmh" };
}

/** Languages spoken across many regions, whose likely region (`en` → US,
 * `es` → ES) is a guess that would put a reader in the wrong units. */
const MULTI_REGION_LANGUAGES = new Set(["en", "es", "pt", "fr", "de", "ar", "zh", "ru", "nl", "it", "sv"]);

function regionOf(tag: string, guess: boolean): string | null {
  try {
    const locale = new Intl.Locale(tag);
    const region = locale.region ?? (guess && !MULTI_REGION_LANGUAGES.has(locale.language) ? locale.maximize().region : undefined);
    return region && /^[A-Z]{2}$/.test(region) ? region : null;
  } catch {
    // A malformed tag says nothing.
    return null;
  }
}

/** The region the browser's languages name. A tag that names one wins
 * wherever it sits in the list (`en, zh-CN` → `CN`: a bare `en` says
 * nothing about where the reader is); failing that, a bare language whose
 * region is all but certain fills it (`ja` → `JP`). */
export function browserRegion(languages: readonly string[] | null = typeof navigator === "undefined" ? null : navigator.languages): string | null {
  for (const guess of [false, true]) {
    for (const tag of languages ?? []) {
      const region = regionOf(tag, guess);
      if (region !== null) return region;
    }
  }
  return null;
}

/** The units a set of preferences shows. */
export function resolveUnits(prefs: UnitPrefs, region: string | null): UnitChoice {
  const base = prefs.preset === "metric" ? METRIC_UNITS : prefs.preset === "imperial" ? IMPERIAL_UNITS : regionUnits(region);
  return { ...base, ...prefs.overrides };
}

const TOKEN_UNITS: Record<string, [UnitQuantity, string]> = {};
for (const quantity of UNIT_QUANTITIES) {
  for (const option of UNIT_OPTIONS[quantity]) TOKEN_UNITS[option.toLowerCase()] = [quantity, option];
}

/** `?units=` and the stored choice: a comma list of a preset and unit
 * tokens (`imperial`, `metric,kt`, `f,kmh`). Unknown tokens are skipped;
 * nothing known reads as `auto`. */
export function parseUnitPrefs(value: string | null | undefined): UnitPrefs {
  const prefs: UnitPrefs = { preset: "auto", overrides: {} };
  for (const raw of (value ?? "").split(",")) {
    const token = raw.trim().toLowerCase();
    if ((UNIT_PRESETS as readonly string[]).includes(token)) {
      prefs.preset = token as UnitPreset;
      continue;
    }
    const known = TOKEN_UNITS[token];
    if (known) (prefs.overrides as Record<string, string>)[known[0]] = known[1];
  }
  return prefs;
}

/** The inverse of `parseUnitPrefs`: the preset, then each override in the
 * settings sheet's order. */
export function formatUnitPrefs(prefs: UnitPrefs): string {
  const tokens: string[] = [prefs.preset];
  for (const quantity of UNIT_QUANTITIES) {
    const value = prefs.overrides[quantity];
    if (value !== undefined) tokens.push(value.toLowerCase());
  }
  return tokens.join(",");
}

const STORAGE_KEY = "xue-units";

function detectPrefs(): UnitPrefs {
  if (typeof window === "undefined") return { preset: "auto", overrides: {} };
  const fromUrl = new URLSearchParams(window.location.search).get("units");
  if (fromUrl !== null) return parseUnitPrefs(fromUrl);
  try {
    return parseUnitPrefs(localStorage.getItem(STORAGE_KEY));
  } catch {
    // Storage can be unavailable (privacy modes); the region decides.
    return { preset: "auto", overrides: {} };
  }
}

/** The viewer's choice and the units it resolves to. Live bindings: read
 * them at the point of use, and re-read on `onUnitsChange`. */
export let unitPrefs: UnitPrefs = detectPrefs();
export let units: UnitChoice = resolveUnits(unitPrefs, browserRegion());

const listeners = new Set<() => void>();

/** Run `listener` after every change of units; it redraws what it owns. */
export function onUnitsChange(listener: () => void): void {
  listeners.add(listener);
}

/** Replace the viewer's choice: persist it and let every listener redraw.
 * The choice is not written into the address bar: a link shared by a
 * reader in Ohio should not open in Fahrenheit in Osaka. */
export function setUnitPrefs(next: UnitPrefs): void {
  unitPrefs = { preset: next.preset, overrides: { ...next.overrides } };
  units = resolveUnits(unitPrefs, browserRegion());
  try {
    localStorage.setItem(STORAGE_KEY, formatUnitPrefs(unitPrefs));
  } catch {
    // The choice still holds for this page.
  }
  for (const listener of listeners) listener();
}

/** Pick a preset; the per-quantity overrides are cleared with it. */
export function setUnitPreset(preset: UnitPreset): void {
  setUnitPrefs({ preset, overrides: {} });
}

/** Set one quantity apart from the preset, or (`null`, or the preset's own
 * unit) back to it. */
export function setUnitOverride<Q extends UnitQuantity>(quantity: Q, value: UnitChoice[Q] | null): void {
  const overrides = { ...unitPrefs.overrides };
  const base = resolveUnits({ preset: unitPrefs.preset, overrides: {} }, browserRegion());
  if (value === null || value === base[quantity]) delete overrides[quantity];
  else overrides[quantity] = value;
  setUnitPrefs({ preset: unitPrefs.preset, overrides });
}

// ---------------------------------------------------------------------------
// Conversion

const KELVIN_OFFSET = 273.15;

/** A value in one of the canonical units (°C, m/s, hPa, mm, m, km) in the
 * chosen unit. */
export function convertTemperature(celsius: number, to: TemperatureUnit = units.temperature): number {
  return to === "F" ? celsius * 1.8 + 32 : celsius;
}
/** A temperature difference (a spread, a lapse) — no offset. */
export function convertTemperatureDelta(kelvin: number, to: TemperatureUnit = units.temperature): number {
  return to === "F" ? kelvin * 1.8 : kelvin;
}
const WIND_FACTORS: Record<WindUnit, number> = { ms: 1, kmh: 3.6, kt: 3600 / 1852, mph: 3600 / 1609.344 };
export function convertWind(metresPerSecond: number, to: WindUnit = units.wind): number {
  return metresPerSecond * WIND_FACTORS[to];
}
const PRESSURE_FACTORS: Record<PressureUnit, number> = { hPa: 1, kPa: 0.1, inHg: 1 / 33.8638866667, mmHg: 1 / 1.33322387415 };
export function convertPressure(hectopascals: number, to: PressureUnit = units.pressure): number {
  return hectopascals * PRESSURE_FACTORS[to];
}
export function convertPrecipitation(millimetres: number, to: PrecipitationUnit = units.precipitation): number {
  return to === "in" ? millimetres / 25.4 : millimetres;
}
export function convertHeight(metres: number, to: HeightUnit = units.height): number {
  return to === "ft" ? metres / 0.3048 : metres;
}
const DISTANCE_FACTORS: Record<DistanceUnit, number> = { km: 1, mi: 1 / 1.609344, nmi: 1 / 1.852 };
export function convertDistance(kilometres: number, to: DistanceUnit = units.distance): number {
  return kilometres * DISTANCE_FACTORS[to];
}

/** m^(2/3)/s, m^(2/3) s^-1, m2/3 s-1, m**(2/3) s**-1 and the like. */
const EDR_UNIT = /^m\s*(?:\^|\*\*)?\s*\(?2\/3\)?\s*(?:\/\s*s|s\s*(?:\^|\*\*)?\s*-1|s⁻¹)$/;
/** m3/s, m3 s-1, m**3 s**-1, m^3/s. */
const CUBIC_METRES_PER_SECOND = /^m\s*(?:\^|\*\*)?\s*3\s*(?:\/\s*s|s\s*(?:\^|\*\*)?\s*-1|s⁻¹)$/;
/** km2, km^2, km**2. */
const SQUARE_KILOMETRES = /^km\s*(?:\^|\*\*)?\s*2$/;

/** How a file unit converts: the quantity it is, and the value in that
 * quantity's canonical unit. `null` for a unit no choice applies to. */
interface FileUnit {
  quantity: UnitQuantity | "rate";
  toCanonical: (value: number) => number;
}

/** A parameter block whose quantity keeps its file unit (module comment). */
export type UnitParameter = Pick<BundleParameter, "discipline" | "parameterCategory" | "parameterNumber" | "typeOfFirstFixedSurface">;

function keepsFileUnit(parameter: UnitParameter | undefined): boolean {
  if (parameter === undefined || parameter.discipline !== 0) return false;
  // 0/0/2 potential temperature, 0/0/3 equivalent potential temperature.
  if (parameter.parameterCategory === 0) return parameter.parameterNumber === 2 || parameter.parameterNumber === 3;
  // 0/3/5 geopotential height on an isobaric surface (code table 4.5: 100).
  return parameter.parameterCategory === 3 && parameter.parameterNumber === 5 && parameter.typeOfFirstFixedSurface === 100;
}

const identity = (value: number) => value;
const FILE_UNITS: Record<string, FileUnit> = {
  K: { quantity: "temperature", toCanonical: (value) => value - KELVIN_OFFSET },
  "°C": { quantity: "temperature", toCanonical: identity },
  degC: { quantity: "temperature", toCanonical: identity },
  C: { quantity: "temperature", toCanonical: identity },
  "m/s": { quantity: "wind", toCanonical: identity },
  "m s-1": { quantity: "wind", toCanonical: identity },
  kt: { quantity: "wind", toCanonical: (value) => value / WIND_FACTORS.kt },
  hPa: { quantity: "pressure", toCanonical: identity },
  Pa: { quantity: "pressure", toCanonical: (value) => value / 100 },
  mm: { quantity: "precipitation", toCanonical: identity },
  "kg/m²": { quantity: "precipitation", toCanonical: identity },
  "kg/m^2": { quantity: "precipitation", toCanonical: identity },
  "kg m-2": { quantity: "precipitation", toCanonical: identity },
  "mm/h": { quantity: "rate", toCanonical: identity },
  m: { quantity: "height", toCanonical: identity },
  km: { quantity: "distance", toCanonical: identity },
};

function fileUnit(unit: string, parameter?: UnitParameter): FileUnit | null {
  if (keepsFileUnit(parameter)) return null;
  return FILE_UNITS[unit] ?? null;
}

/** The quantity a file unit is, or null when no unit choice applies to it
 * (dBZ, %, g/kg, a geopotential height, …). A precipitation rate (mm/h) is
 * `"rate"`, shown in the precipitation unit per hour. */
export function unitQuantity(unit: string, parameter?: UnitParameter): UnitQuantity | "rate" | null {
  return fileUnit(unit, parameter)?.quantity ?? null;
}

/** The label of the precipitation unit per hour (mm/h, in/h). */
export function precipitationRateLabel(to: PrecipitationUnit = units.precipitation): string {
  return `${UNIT_LABELS.precipitation[to]}/h`;
}

/** The unit a readout shows a file unit as, in the viewer's units. */
export function displayUnit(unit: string, parameter?: UnitParameter): string {
  const known = fileUnit(unit, parameter);
  if (known) {
    if (known.quantity === "rate") return precipitationRateLabel();
    const quantity = known.quantity;
    return (UNIT_LABELS[quantity] as Record<string, string>)[units[quantity]]!;
  }
  if (unit === "1") return "";
  if (EDR_UNIT.test(unit)) return "m⅔/s";
  if (CUBIC_METRES_PER_SECOND.test(unit)) return "m³/s";
  if (SQUARE_KILOMETRES.test(unit)) return "km²";
  return unit;
}

/** A value in the file's unit, in the unit `displayUnit` names. */
export function displayValue(unit: string, value: number, parameter?: UnitParameter): number {
  const known = fileUnit(unit, parameter);
  if (!known) return value;
  return displayValueCanonical(known.quantity, known.toCanonical(value));
}

/** `displayUnit` of a bundle variable, its parameter block considered. */
export function variableDisplayUnit(variable: Pick<BundleVariable, "unit" | "parameter">): string {
  return displayUnit(variable.unit, variable.parameter);
}

/** `displayValue` of a bundle variable, its parameter block considered. */
export function variableDisplayValue(variable: Pick<BundleVariable, "unit" | "parameter">, value: number): number {
  return displayValue(variable.unit, value, variable.parameter);
}

function displayValueCanonical(quantity: UnitQuantity | "rate", canonical: number): number {
  switch (quantity) {
    case "temperature":
      return convertTemperature(canonical);
    case "wind":
      return convertWind(canonical);
    case "pressure":
      return convertPressure(canonical);
    case "precipitation":
    case "rate":
      return convertPrecipitation(canonical);
    case "height":
      return convertHeight(canonical);
    case "distance":
      return convertDistance(canonical);
  }
}

/** Whether a file unit shows in a unit other than the canonical one the
 * shell's hand-written legends and digits were set for. */
export function unitConverts(unit: string, parameter?: UnitParameter): boolean {
  const known = fileUnit(unit, parameter);
  if (!known) return false;
  if (known.quantity === "rate") return units.precipitation !== "mm";
  const quantity = known.quantity;
  return units[quantity] !== METRIC_UNITS[quantity];
}

/** The decimals a converted value is worth: what the canonical value showed
 * with, widened where the target unit is coarser (an inch is 25 mm, an
 * inch of mercury 34 hPa). */
export function displayDecimals(unit: string, canonicalDecimals: number, parameter?: UnitParameter): number {
  const known = fileUnit(unit, parameter);
  if (!known || !unitConverts(unit, parameter)) return canonicalDecimals;
  return quantityDecimals(known.quantity, canonicalDecimals);
}

function quantityDecimals(quantity: UnitQuantity | "rate", canonicalDecimals: number): number {
  switch (quantity) {
    case "precipitation":
    case "rate":
      return units.precipitation === "in" ? Math.max(canonicalDecimals, 2) : canonicalDecimals;
    case "pressure":
      return units.pressure === "inHg" ? 2 : units.pressure === "kPa" ? Math.max(1, canonicalDecimals + 1) : canonicalDecimals;
    default:
      return canonicalDecimals;
  }
}

/** Whether a variable is river discharge, by its parameter block
 * (hydrology 1/0/7, `identity.ts`). */
function isDischarge(variable: BundleVariable): boolean {
  const parameter = variable.parameter;
  return parameter !== undefined && parameter.discipline === 1 && parameter.parameterCategory === 0 && parameter.parameterNumber === 7;
}

/** A value already in the display unit, as a readout prints it, with the
 * digits worth showing: a linear codebook resolves exactly as far as its
 * step, and a logarithmic one resolves light rain much more finely than
 * heavy. River discharge reads whole above 100 m³/s and to a tenth below,
 * as a hydrograph is read: a brook's 3.4 m³/s means something, a river's
 * 1240.5 does not. */
export function formatValueReadout(variable: BundleVariable, value: number): string {
  if (isDischarge(variable)) return value.toFixed(value >= 100 ? 0 : 1);
  if (variable.quantization.type === "linear") {
    const step = variable.quantization.scale;
    return value.toFixed(displayDecimals(variable.unit, step >= 1 ? 0 : step >= 0.1 ? 1 : 2, variable.parameter));
  }
  const magnitude = Math.abs(value);
  // A logarithmic codebook's digits follow the magnitude on screen, which
  // already reads in the display unit.
  return value.toFixed(magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : 2);
}

/** A legend tick written in a canonical unit ("-20", "1013", "0.5"),
 * relabelled in the viewer's unit at the same place on the bar. Ticks that
 * are not numbers (a "+", a "≥50") keep their decoration; the number inside
 * converts. A kelvin field's hand-written legend is ticked in °C. */
export function convertLegendLabel(unit: string, label: string, parameter?: UnitParameter): string {
  return relabel(unit, label, parameter, true);
}

/** A legend tick written in the file's own unit (a kelvin field's in K),
 * as a codebook-spanning or host-declared legend is, relabelled in the
 * viewer's unit. */
export function convertFileLegendLabel(unit: string, label: string, parameter?: UnitParameter): string {
  return relabel(unit, label, parameter, false);
}

/** A number in a tick, signed with a hyphen or the typographic minus
 * (U+2212), its exponent form (`2.4e10`) included. */
const TICK_NUMBER = /[-\u2212]?\d+(?:\.\d+)?(?:e[-+]?\d+)?/i;

function relabel(unit: string, label: string, parameter: UnitParameter | undefined, celsiusForKelvin: boolean): string {
  const known = fileUnit(unit, parameter);
  // A kelvin field's legend in K reads in °C even when °C is the choice.
  const kelvinInFile = !celsiusForKelvin && unit === "K";
  if (!known || (!unitConverts(unit, parameter) && !kelvinInFile)) return label;
  return label.replace(TICK_NUMBER, (text) => {
    // The tick's own minus glyph is kept on the converted number.
    const typographic = text.startsWith("\u2212");
    const number = Number(typographic ? `-${text.slice(1)}` : text);
    const canonicalDecimals = /e/i.test(text) || !text.includes(".") ? 0 : text.split(".")[1]!.length;
    const canonical = celsiusForKelvin && known.quantity === "temperature" ? number : known.toCanonical(number);
    const value = displayValueCanonical(known.quantity, canonical);
    const written = Number(value.toFixed(legendDecimals(known.quantity, canonicalDecimals))).toString();
    return typographic ? written.replace(/^-/, "\u2212") : written;
  });
}

function legendDecimals(quantity: UnitQuantity | "rate", canonicalDecimals: number): number {
  if ((quantity === "precipitation" || quantity === "rate") && units.precipitation === "in") return 2;
  return quantityDecimals(quantity, canonicalDecimals);
}

// ---------------------------------------------------------------------------
// Point readouts: a value in a canonical unit as a card or a table prints
// it, number and unit symbol. `decimals` is what the canonical unit shows;
// a coarser target unit takes more (an inch, an inch of mercury) and a
// finer one fewer (a knot or a km/h is already finer than 0.1 m/s reads).

function joined(value: number, decimals: number, label: string): string {
  return `${value.toFixed(Math.max(0, decimals))} ${label}`;
}

/** Degrees Celsius, as the chosen temperature unit. */
export function formatTemperature(celsius: number, decimals = 1): string {
  return joined(convertTemperature(celsius), decimals, UNIT_LABELS.temperature[units.temperature]);
}

/** Metres per second, as the chosen wind unit. */
export function formatWind(metresPerSecond: number, decimals = 1): string {
  const unit = units.wind;
  return joined(convertWind(metresPerSecond), unit === "ms" ? decimals : decimals - 1, UNIT_LABELS.wind[unit]);
}

/** Knots, as the chosen wind unit: a tropical cyclone's intensity is
 * reported in whole knots, so a knot choice keeps the agency's number. */
export function formatWindKnots(knots: number): string {
  const unit = units.wind;
  if (unit === "kt") return `${Math.round(knots)} kt`;
  return joined(convertWind(knots / WIND_FACTORS.kt), 0, UNIT_LABELS.wind[unit]);
}

/** Hectopascals, as the chosen pressure unit. */
export function formatPressure(hectopascals: number, decimals = 1): string {
  return joined(convertPressure(hectopascals), quantityDecimals("pressure", decimals), UNIT_LABELS.pressure[units.pressure]);
}

/** Millimetres, as the chosen precipitation unit. */
export function formatPrecipitation(millimetres: number, decimals = 1): string {
  return joined(convertPrecipitation(millimetres), quantityDecimals("precipitation", decimals), UNIT_LABELS.precipitation[units.precipitation]);
}

/** Metres, as the chosen height unit. */
export function formatHeight(metres: number, decimals = 0): string {
  return joined(convertHeight(metres), decimals, UNIT_LABELS.height[units.height]);
}

/** Kilometres, as the chosen distance unit. */
export function formatDistance(kilometres: number, decimals = 1): string {
  return joined(convertDistance(kilometres), decimals, UNIT_LABELS.distance[units.distance]);
}
