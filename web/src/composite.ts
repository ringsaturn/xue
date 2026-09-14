/**
 * Experimental synoptic composite (`?x=true`): fields *computed* on the
 * frontend from the planes of several bundles, the way a forecaster's chart
 * combines what the model published rather than showing one variable at a
 * time.
 *
 * Everything here is pure arithmetic on decoded planes and geographic grids:
 * no WebGL, no sessions. main.ts feeds it whole planes from the sessions it
 * opened for the experiment and hands the result to an ordinary
 * `ForecastLayer` as a plane of derived codes with a palette of its own. A
 * derived plane uses codes 0..DERIVED_MAX_CODE (0 is nothing, the top is the
 * strongest signal); 255 stays reserved and transparent like every scalar
 * palette's no-data entry.
 *
 * Two derived fields so far, both aimed at the elements a surface chart
 * draws by hand over the precipitation and the isobars:
 *
 * - **warm moist inflow** — the 暖湿流 arrow: where the 850 hPa water vapour
 *   flux is strong *and* the air it carries is warm and moist (a high
 *   equivalent potential temperature). The product of two soft ramps, one
 *   per input, on the flux bundle's grid.
 * - **frontal zone** — an objective stand-in for the drawn front: where the
 *   850 hPa θe gradient is sharp. Thermal front parameter methods locate the
 *   line on the warm edge of the zone; this draws the zone itself as a band
 *   and leaves the line to the eye.
 */

import type { BundleVariable } from "./manifest";
import { decodeValue, precipitationColor } from "./palettes";
import type { GeoGrid } from "./probe";

/** One decoded scalar plane on its own grid. */
export interface ScalarPlane {
  grid: GeoGrid;
  variable: BundleVariable;
  plane: Uint8Array;
}

/** One decoded vector pair on its own grid. */
export interface VectorPlane {
  grid: GeoGrid;
  u: BundleVariable;
  v: BundleVariable;
  uPlane: Uint8Array;
  vPlane: Uint8Array;
}

/** The highest code a derived plane uses; 255 stays reserved. */
export const DERIVED_MAX_CODE = 254;

/** Thresholds of the warm moist inflow: the flux ramp in the vapour flux
 * codebook's own unit (g·cm⁻¹·hPa⁻¹·s⁻¹; the palette tops out at 50, but
 * over East Asia in September the 90th percentile of the smoothed
 * magnitude is near 13 and a marked stream 18–25), and the θe ramp in
 * kelvin — 332 K is ordinary maritime air, 344 K a tropical air mass. */
export interface InflowThresholds {
  flux: readonly [number, number];
  thetaE: readonly [number, number];
}

export const WARM_MOIST_INFLOW: InflowThresholds = {
  flux: [6, 18],
  thetaE: [332, 344],
};

/** The frontal zone ramp, in K per 100 km of θe gradient at 850 hPa after
 * the smoothing below: nothing under the first, full strength at the
 * second. A classical front carries several kelvin over a hundred
 * kilometres; on the GFS at 0.25° the 90th percentile of the smoothed
 * gradient over East Asia sits near 4.5 and a marked front past 10. */
export const FRONT_ZONE_GRADIENT: readonly [number, number] = [4.5, 11];

/** Standard deviation, in degrees, of the Gaussian the θe field gets before
 * its gradient is taken. The model's θe at 850 hPa is noisy at the grid
 * scale — neighbouring cells differ by a kelvin or two on average, up to
 * ten — and a raw gradient is speckle everywhere; a degree of smoothing
 * leaves the synoptic frontal band and little else. Stated in degrees so
 * the half-resolution tier and the full grid smooth the same distance. */
export const FRONT_SMOOTHING_DEGREES = 1;

/** The same for both inputs of the inflow — the flux magnitude and the θe
 * read under it — so the tint is one broad stream rather than a field of
 * patches. */
export const INFLOW_SMOOTHING_DEGREES = 1;

/** Kilometres per degree of latitude. */
const KM_PER_DEGREE = 111.32;

/** Smooth step from 0 at `from` to 1 at `to`, clamped. */
export function ramp(value: number, from: number, to: number): number {
  if (!(value > from)) return 0;
  if (value >= to) return 1;
  const t = (value - from) / (to - from);
  return t * t * (3 - 2 * t);
}

/** Every code of a variable decoded once: NaN for the reserved codes. */
export function decodeTable(variable: BundleVariable): Float32Array {
  const table = new Float32Array(256);
  for (let code = 0; code < 256; code += 1) {
    const value = decodeValue(variable, code);
    table[code] = value === null ? Number.NaN : value;
  }
  return table;
}

/** Whether two grids place the same cells at the same coordinates. */
export function sameGrid(a: GeoGrid, b: GeoGrid): boolean {
  return (
    a.width === b.width &&
    a.height === b.height &&
    a.firstLongitude === b.firstLongitude &&
    a.firstLatitude === b.firstLatitude &&
    a.longitudeStep === b.longitudeStep &&
    a.latitudeStep === b.latitudeStep
  );
}

/** The cell of `grid` nearest to a coordinate, or -1 outside it. A wrapping
 * grid takes every longitude; a cropped one only what its columns cover. */
export function nearestCell(grid: GeoGrid, longitude: number, latitude: number): number {
  const row = Math.round((latitude - grid.firstLatitude) / grid.latitudeStep);
  if (row < 0 || row >= grid.height) return -1;
  let column = Math.round((longitude - grid.firstLongitude) / grid.longitudeStep);
  if (grid.wraps) {
    column = ((column % grid.width) + grid.width) % grid.width;
  } else if (column < 0 || column >= grid.width) {
    return -1;
  }
  return row * grid.width + column;
}

/** The warm moist inflow index on the flux grid: the (smoothed) flux
 * magnitude's ramp times the (smoothed) θe ramp, the θe read from its own
 * grid by nearest cell (both planes of a run are usually on the same grid,
 * and then read straight across). A cell missing either input is nothing. */
export function warmMoistInflow(
  flow: VectorPlane,
  warmth: ScalarPlane,
  thresholds: InflowThresholds = WARM_MOIST_INFLOW,
  smoothingDegrees: number = INFLOW_SMOOTHING_DEGREES,
): Uint8Array {
  const { grid } = flow;
  const cells = grid.width * grid.height;
  const out = new Uint8Array(cells);
  const uTable = decodeTable(flow.u);
  const vTable = decodeTable(flow.v);
  const theta = smoothedField(warmth, smoothingDegrees);
  const direct = sameGrid(grid, warmth.grid);
  const [fluxFrom, fluxTo] = thresholds.flux;
  const [thetaFrom, thetaTo] = thresholds.thetaE;
  let magnitude: Float32Array = new Float32Array(cells);
  for (let cell = 0; cell < cells; cell += 1) {
    const u = uTable[flow.uPlane[cell]!]!;
    const v = vTable[flow.vPlane[cell]!]!;
    magnitude[cell] = Number.isNaN(u) || Number.isNaN(v) ? Number.NaN : Math.hypot(u, v);
  }
  const sigma = smoothingDegrees / Math.abs(grid.latitudeStep);
  if (sigma > 0) magnitude = gaussianSmooth(magnitude, grid.width, grid.height, grid.wraps, sigma);
  for (let row = 0; row < grid.height; row += 1) {
    const latitude = grid.firstLatitude + row * grid.latitudeStep;
    for (let column = 0; column < grid.width; column += 1) {
      const cell = row * grid.width + column;
      const flux = magnitude[cell]!;
      if (Number.isNaN(flux)) continue;
      const source = direct ? cell : nearestCell(warmth.grid, grid.firstLongitude + column * grid.longitudeStep, latitude);
      if (source < 0) continue;
      const value = theta[source]!;
      if (Number.isNaN(value)) continue;
      const index = ramp(flux, fluxFrom, fluxTo) * ramp(value, thetaFrom, thetaTo);
      out[cell] = Math.round(index * DERIVED_MAX_CODE);
    }
  }
  return out;
}

/** The magnitude of the θe gradient, in K per 100 km, ramped into a plane on
 * the θe grid. The field is smoothed first (FRONT_SMOOTHING_DEGREES, or the
 * width given); the gradient is then central differences, with the
 * longitude spacing shrunk by cos(latitude). Poleward of 85° there is no
 * meaningful spacing and nothing is drawn. */
export function thermalFrontZone(
  warmth: ScalarPlane,
  thresholds: readonly [number, number] = FRONT_ZONE_GRADIENT,
  smoothingDegrees: number = FRONT_SMOOTHING_DEGREES,
): Uint8Array {
  const { grid } = warmth;
  const { width, height } = grid;
  const cells = width * height;
  const out = new Uint8Array(cells);
  if (width < 3 || height < 3) return out;
  const smooth = smoothedField(warmth, smoothingDegrees);
  const dy = KM_PER_DEGREE * Math.abs(grid.latitudeStep) * 2;
  const [from, to] = thresholds;
  for (let row = 1; row < height - 1; row += 1) {
    const latitude = grid.firstLatitude + row * grid.latitudeStep;
    const cosine = Math.cos((latitude * Math.PI) / 180);
    if (Math.abs(latitude) >= 85) continue;
    const dx = KM_PER_DEGREE * Math.abs(grid.longitudeStep) * cosine * 2;
    for (let column = 0; column < width; column += 1) {
      let left = column - 1;
      let right = column + 1;
      if (grid.wraps) {
        left = (left + width) % width;
        right %= width;
      } else if (left < 0 || right >= width) {
        continue;
      }
      const cell = row * width + column;
      const east = smooth[row * width + right]!;
      const west = smooth[row * width + left]!;
      const south = smooth[(row + 1) * width + column]!;
      const north = smooth[(row - 1) * width + column]!;
      if (Number.isNaN(east) || Number.isNaN(west) || Number.isNaN(south) || Number.isNaN(north)) continue;
      const gradient = Math.hypot((east - west) / dx, (south - north) / dy) * 100;
      out[cell] = Math.round(ramp(gradient, from, to) * DERIVED_MAX_CODE);
    }
  }
  return out;
}

/** A scalar plane decoded to physical values and smoothed by a Gaussian of
 * the given width in degrees (none at 0). */
export function smoothedField(scalar: ScalarPlane, smoothingDegrees: number): Float32Array {
  const { grid } = scalar;
  const cells = grid.width * grid.height;
  const table = decodeTable(scalar.variable);
  const decoded = new Float32Array(cells);
  for (let cell = 0; cell < cells; cell += 1) decoded[cell] = table[scalar.plane[cell]!]!;
  const sigma = smoothingDegrees / Math.abs(grid.latitudeStep);
  return sigma > 0 ? gaussianSmooth(decoded, grid.width, grid.height, grid.wraps, sigma) : decoded;
}

/** A separable Gaussian over the cells that hold a value, renormalised by
 * the coverage so a no-data neighbour neither pulls the mean nor spreads;
 * a cell with no valid cell in reach stays NaN. Columns wrap on a global
 * grid; rows and a cropped grid's columns stop at the edge. */
export function gaussianSmooth(
  values: Float32Array,
  width: number,
  height: number,
  wraps: boolean,
  sigma: number,
): Float32Array {
  const radius = Math.max(1, Math.ceil(sigma * 3));
  const weights = new Float32Array(radius * 2 + 1);
  for (let at = -radius; at <= radius; at += 1) weights[at + radius] = Math.exp(-(at * at) / (2 * sigma * sigma));
  const cells = width * height;
  // Pass one, along the rows: weighted sums and the weight that found data.
  const sum = new Float32Array(cells);
  const cover = new Float32Array(cells);
  for (let row = 0; row < height; row += 1) {
    const base = row * width;
    for (let column = 0; column < width; column += 1) {
      let s = 0;
      let w = 0;
      for (let at = -radius; at <= radius; at += 1) {
        let c = column + at;
        if (wraps) c = ((c % width) + width) % width;
        else if (c < 0 || c >= width) continue;
        const value = values[base + c]!;
        if (Number.isNaN(value)) continue;
        const weight = weights[at + radius]!;
        s += value * weight;
        w += weight;
      }
      sum[base + column] = s;
      cover[base + column] = w;
    }
  }
  // Pass two, down the columns, over the row sums and their coverage.
  const out = new Float32Array(cells);
  for (let column = 0; column < width; column += 1) {
    for (let row = 0; row < height; row += 1) {
      let s = 0;
      let w = 0;
      for (let at = -radius; at <= radius; at += 1) {
        const r = row + at;
        if (r < 0 || r >= height) continue;
        const weight = weights[at + radius]!;
        s += sum[r * width + column]! * weight;
        w += cover[r * width + column]! * weight;
      }
      out[row * width + column] = w > 0 ? s / w : Number.NaN;
    }
  }
  return out;
}

/** A derived palette: transparent at code 0, a tone whose opacity climbs to
 * `alpha` at the top code, and the reserved 255 transparent. */
function derivedPalette(tone: readonly [number, number, number], alpha: number, floor: number): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let code = 1; code <= DERIVED_MAX_CODE; code += 1) {
    const t = code / DERIVED_MAX_CODE;
    const a = floor + (alpha - floor) * t;
    palette.set([tone[0], tone[1], tone[2], Math.round(a * 255)], code * 4);
  }
  return palette;
}

/** The inflow tint: the brown-red a Japanese chart draws the warm moist
 * stream in, translucent so the rain under it still reads. */
export function inflowPalette(): Uint8Array {
  return derivedPalette([205, 68, 38], 0.72, 0.18);
}

/** The frontal zone: a violet band, denser where the gradient is sharper. */
export function frontPalette(): Uint8Array {
  return derivedPalette([116, 44, 150], 0.7, 0.1);
}

/** The stepped precipitation key of the experiment: the lower bound of
 * each band in mm/h. The colours are not a table of their own but the
 * viewer's continuous precipitation ramp sampled at each band's geometric
 * middle and made opaque — so the key is Xue's ramp banded the way a
 * broadcast chart bands its scale, and a value keeps the hue it has on
 * the continuous key. Below the first band nothing is drawn. */
export const PRECIPITATION_STEP_BOUNDS: readonly number[] = [0.1, 1, 2, 5, 10, 20, 50];

/** One band's colour: the ramp at the geometric middle of the band, the
 * top band sampled a step above its floor. */
export function precipitationStepColor(step: number): readonly [number, number, number] {
  const from = PRECIPITATION_STEP_BOUNDS[step]!;
  const to = PRECIPITATION_STEP_BOUNDS[step + 1] ?? from * 2;
  const [r, g, b] = precipitationColor(Math.sqrt(from * to));
  return [r, g, b];
}

export const PRECIPITATION_STEPS: ReadonlyArray<readonly [number, readonly [number, number, number]]> =
  PRECIPITATION_STEP_BOUNDS.map((from, step) => [from, precipitationStepColor(step)] as const);

/** The band a rate falls in, or -1 below the first. */
export function precipitationStep(rate: number): number {
  let step = -1;
  for (const [from] of PRECIPITATION_STEPS) {
    if (rate >= from) step += 1;
    else break;
  }
  return step;
}

/** The 256×1 RGBA palette of the stepped key over one precipitation
 * variable's own codebook: every code decodes to its rate and takes its
 * band's colour, opaque; the reserved codes and the dry code stay clear. */
export function steppedPrecipitationPalette(variable: BundleVariable): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let code = 0; code < 256; code += 1) {
    const rate = decodeValue(variable, code);
    if (rate === null) continue;
    const step = precipitationStep(rate);
    if (step < 0) continue;
    const [, [r, g, b]] = PRECIPITATION_STEPS[step]!;
    palette.set([r, g, b, 255], code * 4);
  }
  return palette;
}

/** The legend of the stepped key: one equal band per step, strongest at
 * the top, and a label per band reading its lower bound. */
export function steppedPrecipitationLegend(): { gradient: string; labels: string[] } {
  const bands = PRECIPITATION_STEPS.length;
  const stops: string[] = [];
  for (let at = 0; at < bands; at += 1) {
    const [, [r, g, b]] = PRECIPITATION_STEPS[bands - 1 - at]!;
    const from = ((at / bands) * 100).toFixed(2);
    const to = (((at + 1) / bands) * 100).toFixed(2);
    stops.push(`rgb(${r}, ${g}, ${b}) ${from}% ${to}%`);
  }
  return {
    gradient: `linear-gradient(to bottom, ${stops.join(", ")})`,
    labels: PRECIPITATION_STEPS.map(([from]) => String(from)).reverse(),
  };
}
