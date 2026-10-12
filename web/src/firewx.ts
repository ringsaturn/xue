/**
 * Fire weather from the published surface fields, computed in the shell:
 * the Fosberg fire weather index and a compound "dry and gusty" alert. No
 * bundle is published for either — both are arithmetic on the 2 m
 * temperature, the 2 m dew point and the 10 m wind or the gust, decoded
 * planes handed to an ordinary `ForecastLayer` as derived codes like the
 * other derived fields (composite.ts).
 */

import { DERIVED_MAX_CODE, decodeTable, nearestCell, sameGrid } from "./composite";
import type { BundleVariable } from "./manifest";
import type { GeoGrid } from "./probe";

/** One decoded scalar plane on its own grid. */
export interface ScalarInput {
  grid: GeoGrid;
  variable: BundleVariable;
  plane: Uint8Array;
}

/** One decoded vector pair on its own grid. */
export interface VectorInput {
  grid: GeoGrid;
  u: BundleVariable;
  v: BundleVariable;
  uPlane: Uint8Array;
  vPlane: Uint8Array;
}

/** The index's ceiling: Fosberg's scale is built so that 100 is a
 * saturated wind over fuel at zero moisture. */
export const FFWI_MAX = 100;

/** The alert: relative humidity under `humidity` percent and a gust of at
 * least `gust` m/s together — the red-flag pairing of dry air and wind
 * that spreads a fire faster than it can be fought. */
export const FIRE_WEATHER_ALERT = { humidity: 30, gust: 10 } as const;

const MS_TO_MPH = 2.2369362920544;

/** Relative humidity in percent from temperature and dew point in °C
 * (the Magnus form, Alduchov & Eskridge 1996 coefficients), capped at 100. */
export function relativeHumidity(temperatureC: number, dewPointC: number): number {
  const saturation = (celsius: number) => Math.exp((17.625 * celsius) / (243.04 + celsius));
  return Math.min(100, (100 * saturation(dewPointC)) / saturation(temperatureC));
}

/** Fosberg's fire weather index (Fosberg 1978): the equilibrium moisture
 * content of fine dead fuel from temperature and humidity (Simard 1968),
 * as a damping on the wind. Temperature in °C, humidity in percent, wind
 * in m/s; the result runs 0 … 100, 100 being dry fuel under a 30 mph
 * wind. */
export function fosbergIndex(temperatureC: number, humidity: number, windMs: number): number {
  const fahrenheit = temperatureC * 1.8 + 32;
  const h = Math.max(0, Math.min(100, humidity));
  let moisture: number;
  if (h < 10) moisture = 0.03229 + 0.281073 * h - 0.000578 * h * fahrenheit;
  else if (h <= 50) moisture = 2.22749 + 0.160107 * h - 0.01478 * fahrenheit;
  else moisture = 21.0606 + 0.005565 * h * h - 0.00035 * h * fahrenheit - 0.483199 * h;
  const m = Math.max(0, moisture) / 30;
  const damping = Math.max(0, 1 - 2 * m + 1.5 * m * m - 0.5 * m * m * m);
  const mph = windMs * MS_TO_MPH;
  return Math.min(FFWI_MAX, (damping * Math.sqrt(1 + mph * mph)) / 0.3002);
}

/** A plane's value at a cell of `grid`, read straight across when the
 * grids agree, else by nearest cell; NaN outside it or at a reserved code. */
function reader(grid: GeoGrid, input: ScalarInput): (cell: number, longitude: number, latitude: number) => number {
  const table = decodeTable(input.variable);
  const kelvin = input.variable.unit === "K";
  const value = (code: number) => {
    const decoded = table[code]!;
    return kelvin ? decoded - 273.15 : decoded;
  };
  if (sameGrid(grid, input.grid)) return (cell) => value(input.plane[cell]!);
  return (_cell, longitude, latitude) => {
    const source = nearestCell(input.grid, longitude, latitude);
    return source < 0 ? Number.NaN : value(input.plane[source]!);
  };
}

/** Walk every cell of `grid` with its coordinates. */
function eachCell(grid: GeoGrid, visit: (cell: number, longitude: number, latitude: number) => void): void {
  for (let row = 0; row < grid.height; row += 1) {
    const latitude = grid.firstLatitude + row * grid.latitudeStep;
    for (let column = 0; column < grid.width; column += 1) {
      visit(row * grid.width + column, grid.firstLongitude + column * grid.longitudeStep, latitude);
    }
  }
}

/** The Fosberg index on the temperature's grid, as derived codes: code 0
 * where an input is missing or the index is 0, else the index scaled onto
 * 1 … DERIVED_MAX_CODE. */
export function fosbergPlane(temperature: ScalarInput, dewPoint: ScalarInput, wind: VectorInput): Uint8Array {
  const { grid } = temperature;
  const out = new Uint8Array(grid.width * grid.height);
  const readT = reader(grid, temperature);
  const readTd = reader(grid, dewPoint);
  const uTable = decodeTable(wind.u);
  const vTable = decodeTable(wind.v);
  const direct = sameGrid(grid, wind.grid);
  eachCell(grid, (cell, longitude, latitude) => {
    const t = readT(cell, longitude, latitude);
    const td = readTd(cell, longitude, latitude);
    const source = direct ? cell : nearestCell(wind.grid, longitude, latitude);
    if (source < 0 || Number.isNaN(t) || Number.isNaN(td)) return;
    const u = uTable[wind.uPlane[source]!]!;
    const v = vTable[wind.vPlane[source]!]!;
    if (Number.isNaN(u) || Number.isNaN(v)) return;
    out[cell] = fosbergCode(fosbergIndex(t, relativeHumidity(t, td), Math.hypot(u, v)));
  });
  return out;
}

/** An index value as a derived code: 0 stays nothing, anything above
 * takes at least code 1. */
export function fosbergCode(index: number): number {
  if (!(index > 0)) return 0;
  return Math.max(1, Math.round((Math.min(index, FFWI_MAX) / FFWI_MAX) * DERIVED_MAX_CODE));
}

/** The alert on the temperature's grid: DERIVED_MAX_CODE where the air is
 * drier than the alert's humidity and the gust at least its speed, 0
 * elsewhere. */
export function fireWeatherAlert(
  temperature: ScalarInput,
  dewPoint: ScalarInput,
  gust: ScalarInput,
  alert: { humidity: number; gust: number } = FIRE_WEATHER_ALERT,
): Uint8Array {
  const { grid } = temperature;
  const out = new Uint8Array(grid.width * grid.height);
  const readT = reader(grid, temperature);
  const readTd = reader(grid, dewPoint);
  const readGust = reader(grid, gust);
  eachCell(grid, (cell, longitude, latitude) => {
    const t = readT(cell, longitude, latitude);
    const td = readTd(cell, longitude, latitude);
    const g = readGust(cell, longitude, latitude);
    if (Number.isNaN(t) || Number.isNaN(td) || Number.isNaN(g)) return;
    if (relativeHumidity(t, td) < alert.humidity && g >= alert.gust) out[cell] = DERIVED_MAX_CODE;
  });
  return out;
}

/** The index's ramp, after the operational fire danger classes it maps
 * onto: nothing under 10, a pale straw wash through orange to deep red at
 * 50 and above (a red-flag day), purple past 75. Translucent throughout,
 * so the terrain and the coast under it still read. */
const FFWI_STOPS: ReadonlyArray<readonly [number, number, number, number, number]> = [
  [0, 255, 255, 255, 0],
  [10, 250, 230, 150, 0],
  [15, 250, 220, 120, 90],
  [25, 250, 175, 60, 140],
  [35, 240, 110, 30, 170],
  [50, 210, 30, 20, 195],
  [75, 130, 20, 120, 215],
  [100, 80, 0, 90, 230],
];

/** The Fosberg index's palette over the derived codes. */
export function fosbergPalette(): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let code = 1; code <= DERIVED_MAX_CODE; code += 1) {
    const index = (code / DERIVED_MAX_CODE) * FFWI_MAX;
    let at = 1;
    while (at < FFWI_STOPS.length - 1 && FFWI_STOPS[at]![0] < index) at += 1;
    const [x0, ...c0] = FFWI_STOPS[at - 1]!;
    const [x1, ...c1] = FFWI_STOPS[at]!;
    const f = Math.max(0, Math.min(1, (index - x0) / (x1 - x0)));
    palette.set(
      c0.map((channel, position) => Math.round(channel + (c1[position]! - channel) * f)),
      code * 4,
    );
  }
  return palette;
}

/** The alert's palette: a red wash inside the region, outlined by the
 * layer's contour. */
export function fireWeatherAlertPalette(): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  palette.set([225, 30, 40, 110], DERIVED_MAX_CODE * 4);
  return palette;
}
