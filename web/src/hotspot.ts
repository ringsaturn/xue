/**
 * Fire hotspots from the geostationary disks, computed in the shell from
 * two published channels: the 3.9 µm shortwave window (`ir039`), where a
 * burning fraction of a pixel lifts the brightness temperature by tens of
 * kelvin, and the 10.4 µm window (`ir104`), which barely notices it. A
 * cell is a hotspot when its difference ΔT = BT3.9 − BT10.4 stands out
 * from the clear cells around it — the contextual test of the operational
 * fire products (MODIS MOD14, the GOES FDC), cut down to what a 0.04° grid
 * and two channels allow. No fire product is published for this: the
 * shell only reads the channels.
 *
 * Like the other derived fields (composite.ts) it is plain arithmetic on
 * decoded planes, handed to an ordinary `ForecastLayer` as a plane of
 * derived codes: 0 is nothing, 1..DERIVED_MAX_CODE how far the cell stands
 * out, 255 reserved.
 */

import { DERIVED_MAX_CODE, decodeTable, nearestCell, ramp, sameGrid } from "./composite";
import type { BundleVariable } from "./manifest";
import type { GeoGrid } from "./probe";
import { hourAngle, solarEphemeris } from "./shadow";

/** One decoded scalar plane on its own grid. */
export interface ChannelPlane {
  grid: GeoGrid;
  variable: BundleVariable;
  plane: Uint8Array;
}

/** The detection's thresholds, in kelvin unless named otherwise. */
export interface HotspotThresholds {
  /** BT3.9 above which a cell is hot outright, by day and by night — once
   * it also stands `minAnomaly` above its surroundings, so a desert
   * reading 320 K among neighbours at 318 K is not a fire. */
  absoluteDay: number;
  absoluteNight: number;
  /** A cell is a candidate only above these BT3.9s (by day, by night)
   * with ΔT above `candidateDelta` — MOD14's potential-fire test. Without
   * it the contextual test alone flags whatever is warmer at 3.9 µm than
   * its neighbours: sunlit low cloud over the sea, cloud edges, a clear
   * cell among stratus. */
  candidateDay: number;
  candidateNight: number;
  candidateDelta: number;
  /** By day a cell within this many degrees of the sun's specular point
   * seen from the satellite is glint, not fire: the sea mirrors the sun
   * at 3.9 µm by tens of kelvin. The operational products drop 10–12°. */
  glintAngle: number;
  /** ΔT must exceed the background's mean by at least this much. */
  minAnomaly: number;
  /** …and, for a cell short of the absolute threshold, by this many
   * standard deviations of the background's ΔT. */
  sigmaFactor: number;
  /** The background's standard deviation is never taken as less than
   * this: over a uniform sea or a quiet night the spread is a few tenths
   * of a kelvin, and four of those is no evidence. */
  sigmaFloor: number;
  /** By day a cell whose BT10.4 is at or below this is cloud: sunlight
   * reflected at 3.9 µm makes a bright cloud edge look hot. */
  dayCloudBt104: number;
  /** The sun's elevation, in degrees, above which a cell is in day. */
  dayElevation: number;
  /** The side of the square window the background is read over, in
   * cells; odd. 11 cells of 0.04° are about 45 km, the 21 × 21 window of
   * the 2 km pixels the operational tests use. */
  window: number;
  /** The share of the window that must be clear background for the
   * contextual test to speak at all. */
  minBackgroundShare: number;
  /** The anomaly at which the code reaches the top: a strong fire. */
  fullAnomaly: number;
}

export const HOTSPOT_THRESHOLDS: HotspotThresholds = {
  candidateDay: 310,
  candidateNight: 305,
  candidateDelta: 10,
  glintAngle: 12,
  absoluteDay: 320,
  absoluteNight: 310,
  minAnomaly: 10,
  sigmaFactor: 4,
  sigmaFloor: 2,
  dayCloudBt104: 275,
  dayElevation: 5,
  window: 11,
  minBackgroundShare: 0.25,
  fullAnomaly: 50,
};

/** The hotspots of one frame on the 3.9 µm grid, as derived codes. The
 * 10.4 µm plane is read straight across when it shares the grid, else by
 * nearest cell. `timeMs` is the frame's valid time, which places the
 * terminator; `subLongitude` the satellite, which places the glint (none
 * is dropped without it). */
export function hotspots(
  shortwave: ChannelPlane,
  longwave: ChannelPlane,
  timeMs: number,
  subLongitude: number | null = null,
  thresholds: HotspotThresholds = HOTSPOT_THRESHOLDS,
): Uint8Array {
  const { grid } = shortwave;
  const { width, height } = grid;
  const cells = width * height;
  const out = new Uint8Array(cells);
  if (cells === 0) return out;
  const shortTable = decodeTable(shortwave.variable);
  const longTable = decodeTable(longwave.variable);
  const direct = sameGrid(grid, longwave.grid);
  const day = dayMask(grid, timeMs, thresholds.dayElevation);
  const glint = subLongitude === null ? null : glintMask(grid, timeMs, subLongitude, thresholds.glintAngle);

  // Per cell: ΔT where both channels read and the cell is not cloud by day
  // (NaN otherwise), and whether it may stand in a neighbour's background.
  const delta = new Float32Array(cells);
  const background = new Uint8Array(cells);
  const hot = new Uint8Array(cells);
  const candidate = new Uint8Array(cells);
  for (let row = 0; row < height; row += 1) {
    const latitude = grid.firstLatitude + row * grid.latitudeStep;
    for (let column = 0; column < width; column += 1) {
      const cell = row * width + column;
      // Code 0, the codebook's floor, is no data on a disk (outside it, a
      // missing segment), not a 180 K reading.
      const shortCode = shortwave.plane[cell]!;
      const bt39 = shortCode === 0 ? Number.NaN : shortTable[shortCode]!;
      const source = direct ? cell : nearestCell(longwave.grid, grid.firstLongitude + column * grid.longitudeStep, latitude);
      const longCode = source < 0 ? 0 : longwave.plane[source]!;
      const bt104 = longCode === 0 ? Number.NaN : longTable[longCode]!;
      const isDay = day[cell] === 1;
      if (Number.isNaN(bt39) || Number.isNaN(bt104) || (isDay && bt104 <= thresholds.dayCloudBt104)) {
        delta[cell] = Number.NaN;
        continue;
      }
      delta[cell] = bt39 - bt104;
      const floor = isDay ? thresholds.candidateDay : thresholds.candidateNight;
      if (bt39 > floor && bt39 - bt104 > thresholds.candidateDelta && !(isDay && glint?.[cell] === 1)) candidate[cell] = 1;
      const absolute = isDay ? thresholds.absoluteDay : thresholds.absoluteNight;
      if (bt39 > absolute) hot[cell] = 1;
      // A cell hot outright is not background for its neighbours: a fire
      // front three cells wide must not raise its own bar.
      else background[cell] = 1;
    }
  }

  const stats = backgroundStatistics(delta, background, width, height, grid.wraps, thresholds.window);
  const needed = Math.max(1, Math.ceil(thresholds.minBackgroundShare * (thresholds.window * thresholds.window - 1)));
  for (let cell = 0; cell < cells; cell += 1) {
    if (candidate[cell] !== 1) continue;
    const value = delta[cell]!;
    const count = stats.count[cell]!;
    if (count < needed) continue;
    const mean = stats.sum[cell]! / count;
    const variance = Math.max(0, stats.sumSquares[cell]! / count - mean * mean);
    const sigma = Math.max(thresholds.sigmaFloor, Math.sqrt(variance));
    const anomaly = value - mean;
    if (!(anomaly > thresholds.minAnomaly)) continue;
    if (hot[cell] !== 1 && !(anomaly > thresholds.sigmaFactor * sigma)) continue;
    out[cell] = 1 + Math.round(ramp(anomaly, thresholds.minAnomaly, thresholds.fullAnomaly) * (DERIVED_MAX_CODE - 1));
  }
  return out;
}

/** 1 where the sun stands above `elevation` degrees at a cell's centre. */
export function dayMask(grid: GeoGrid, timeMs: number, elevation: number): Uint8Array {
  const { width, height } = grid;
  const mask = new Uint8Array(width * height);
  const ephemeris = solarEphemeris(timeMs);
  const sinDeclination = Math.sin(ephemeris.declination);
  const cosDeclination = Math.cos(ephemeris.declination);
  const threshold = Math.sin((elevation * Math.PI) / 180);
  const cosHour = new Float64Array(width);
  for (let column = 0; column < width; column += 1) {
    cosHour[column] = Math.cos(hourAngle(ephemeris, grid.firstLongitude + column * grid.longitudeStep));
  }
  for (let row = 0; row < height; row += 1) {
    const latitude = ((grid.firstLatitude + row * grid.latitudeStep) * Math.PI) / 180;
    const a = Math.sin(latitude) * sinDeclination;
    const b = Math.cos(latitude) * cosDeclination;
    for (let column = 0; column < width; column += 1) {
      if (a + b * cosHour[column]! > threshold) mask[row * width + column] = 1;
    }
  }
  return mask;
}

/** 1 where the sea would mirror the sun into the satellite's view: the
 * angle between the line of sight and the sun's ray reflected off a level
 * surface at the cell is under `angle` degrees. The satellite sits on the
 * equator at `subLongitude`; the sun is far enough to be a direction. */
export function glintMask(grid: GeoGrid, timeMs: number, subLongitude: number, angle: number): Uint8Array {
  const { width, height } = grid;
  const mask = new Uint8Array(width * height);
  const ephemeris = solarEphemeris(timeMs);
  // The subsolar point: the declination's latitude, the longitude where
  // the hour angle is zero.
  const sunLongitude = -hourAngle(ephemeris, 0);
  const cosD = Math.cos(ephemeris.declination);
  const sun = [cosD * Math.cos(sunLongitude), cosD * Math.sin(sunLongitude), Math.sin(ephemeris.declination)] as const;
  // Geostationary radius in Earth radii (42 164 km / 6 378 km).
  const orbit = 6.6107;
  const satLongitude = (subLongitude * Math.PI) / 180;
  const sat = [orbit * Math.cos(satLongitude), orbit * Math.sin(satLongitude), 0] as const;
  const threshold = Math.cos((angle * Math.PI) / 180);
  for (let row = 0; row < height; row += 1) {
    const latitude = ((grid.firstLatitude + row * grid.latitudeStep) * Math.PI) / 180;
    const cosLat = Math.cos(latitude);
    const z = Math.sin(latitude);
    for (let column = 0; column < width; column += 1) {
      const longitude = ((grid.firstLongitude + column * grid.longitudeStep) * Math.PI) / 180;
      const x = cosLat * Math.cos(longitude);
      const y = cosLat * Math.sin(longitude);
      const sunNormal = x * sun[0] + y * sun[1] + z * sun[2];
      if (sunNormal <= 0) continue;
      // The sun's ray reflected about the surface normal.
      const rx = 2 * sunNormal * x - sun[0];
      const ry = 2 * sunNormal * y - sun[1];
      const rz = 2 * sunNormal * z - sun[2];
      const vx = sat[0] - x;
      const vy = sat[1] - y;
      const vz = sat[2] - z;
      const length = Math.hypot(vx, vy, vz);
      if (x * vx + y * vy + z * vz <= 0) continue;
      if ((rx * vx + ry * vy + rz * vz) / length > threshold) mask[row * width + column] = 1;
    }
  }
  return mask;
}

/** For every cell, the count, sum and sum of squares of the background
 * values in the `side` × `side` window around it, the cell itself left
 * out. Box sums, separable: one pass along the rows into three planes,
 * then a running sum down the columns, so the cost is the same for any
 * window. Columns wrap on a global grid; rows stop at the edge. */
export function backgroundStatistics(
  values: Float32Array,
  background: Uint8Array,
  width: number,
  height: number,
  wraps: boolean,
  side: number,
): { count: Float32Array; sum: Float32Array; sumSquares: Float32Array } {
  const radius = Math.floor(side / 2);
  const cells = width * height;
  const rowCount = new Float32Array(cells);
  const rowSum = new Float32Array(cells);
  const rowSquares = new Float32Array(cells);
  for (let row = 0; row < height; row += 1) {
    const base = row * width;
    for (let column = 0; column < width; column += 1) {
      let count = 0;
      let sum = 0;
      let squares = 0;
      for (let at = -radius; at <= radius; at += 1) {
        let c = column + at;
        if (wraps) c = ((c % width) + width) % width;
        else if (c < 0 || c >= width) continue;
        if (background[base + c] !== 1) continue;
        const value = values[base + c]!;
        count += 1;
        sum += value;
        squares += value * value;
      }
      rowCount[base + column] = count;
      rowSum[base + column] = sum;
      rowSquares[base + column] = squares;
    }
  }
  const count = new Float32Array(cells);
  const sum = new Float32Array(cells);
  const sumSquares = new Float32Array(cells);
  const runCount = new Float64Array(width);
  const runSum = new Float64Array(width);
  const runSquares = new Float64Array(width);
  // Prime the running sums with rows 0..radius-1, then slide.
  for (let row = 0; row < Math.min(radius, height); row += 1) {
    for (let column = 0; column < width; column += 1) {
      const cell = row * width + column;
      runCount[column] = runCount[column]! + rowCount[cell]!;
      runSum[column] = runSum[column]! + rowSum[cell]!;
      runSquares[column] = runSquares[column]! + rowSquares[cell]!;
    }
  }
  for (let row = 0; row < height; row += 1) {
    const entering = row + radius;
    const leaving = row - radius - 1;
    for (let column = 0; column < width; column += 1) {
      if (entering < height) {
        const cell = entering * width + column;
        runCount[column] = runCount[column]! + rowCount[cell]!;
        runSum[column] = runSum[column]! + rowSum[cell]!;
        runSquares[column] = runSquares[column]! + rowSquares[cell]!;
      }
      if (leaving >= 0) {
        const cell = leaving * width + column;
        runCount[column] = runCount[column]! - rowCount[cell]!;
        runSum[column] = runSum[column]! - rowSum[cell]!;
        runSquares[column] = runSquares[column]! - rowSquares[cell]!;
      }
      const cell = row * width + column;
      let n = runCount[column]!;
      let s = runSum[column]!;
      let q = runSquares[column]!;
      if (background[cell] === 1) {
        const value = values[cell]!;
        n -= 1;
        s -= value;
        q -= value * value;
      }
      count[cell] = Math.round(n);
      sum[cell] = s;
      sumSquares[cell] = q;
    }
  }
  return { count, sum, sumSquares };
}

/** The hotspot palette: nothing at code 0, then opaque heat — orange at
 * the first evidence through yellow to white at the strongest. */
export function hotspotPalette(): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let code = 1; code <= DERIVED_MAX_CODE; code += 1) {
    const t = (code - 1) / (DERIVED_MAX_CODE - 1);
    const r = 255;
    const g = Math.round(90 + 165 * Math.min(1, t * 1.4));
    const b = Math.round(t > 0.6 ? ((t - 0.6) / 0.4) * 220 : 0);
    palette.set([r, g, b, 255], code * 4);
  }
  return palette;
}
