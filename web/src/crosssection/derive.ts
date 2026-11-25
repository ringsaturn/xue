/**
 * A frame of a section on the drawing grid: the plan's points by rows evenly
 * spaced in ln p, from `P_BOTTOM` up to the chart's top.
 *
 * Each point is a column stood on the model's ground. The ground is the
 * pressure at the terrain's height (`sounding/column.ts`), the 2 m temperature
 * and dew point and the 10 m wind are its lowest values, and the levels the
 * run extrapolated under it are left out. Between its values a column is
 * linear in ln p, as a sounding is drawn; under the ground it holds its lowest
 * values, which the ground is painted over; above its top level it has none.
 *
 * Arithmetic only: no canvas, no DOM.
 */

import { surfacePressure, type ColumnSurface } from "../sounding/column";
import { KAPPA, dewPointFromRelativeHumidity, type ModelLevel } from "../sounding/profile";
import { P_BOTTOM, SECTION_ROWS } from "./chart";
import { GROUND_ARRAYS, groupArrays, type SectionArrayId, type SectionField, type SectionGroup } from "./field";
import type { SectionReading } from "./types";

const KELVIN = 273.15;

/** A level this close above the ground, in ln p, is the ground: the 2 m point
 * takes its place. */
const GROUND_CLEARANCE = 0.004;

/** %. A codebook's 0 % is under half a step of it, not no vapour at all. */
const LEAST_HUMIDITY = 1;

/** Frames kept drawn from: the one on screen and what a step back and forth
 * returns to. */
const SLICES = 3;

/** Saturation vapour pressure over water, hPa: Bolton (1980) eq. 10, as
 * `sounding/profile.ts` has it. */
function saturation(celsius: number): number {
  return 6.112 * Math.exp((17.67 * celsius) / (celsius + 243.5));
}

/** Relative humidity, %, from a temperature and dew point. */
export function relativeHumidity(t: number, td: number): number {
  return td >= t ? 100 : (100 * saturation(td)) / saturation(t);
}

/** Potential temperature, K. */
export function potentialTemperature(t: number, p: number): number {
  return (t + KELVIN) * Math.pow(1000 / p, KAPPA);
}

/** Equivalent potential temperature, K, from °C and hPa: Bolton (1980) eq. 43
 * with the condensation temperature of his eq. 15. `logRatio` is ln(1000/p). */
function thetaEAt(t: number, td: number, p: number, logRatio: number): number {
  const temperature = t + KELVIN;
  const dew = Math.min(td, t) + KELVIN;
  const e = saturation(dew - KELVIN);
  const r = (0.622 * e) / (p - e);
  const condensation = 1 / (1 / (dew - 56) + Math.log(temperature / dew) / 800) + 56;
  return temperature * Math.exp(0.2854 * (1 - 0.28 * r) * logRatio + (3376 / condensation - 2.54) * r * (1 + 0.81 * r));
}

export function equivalentPotentialTemperature(t: number, td: number, p: number): number {
  return thetaEAt(t, td, p, Math.log(1000 / p));
}

/** The component of a wind across a line running `bearing` (radians clockwise
 * from north), positive towards its right: with the line's start at the left
 * of the chart that is out of the page. */
export function acrossLine(u: number, v: number, bearing: number): number {
  return u * Math.cos(bearing) - v * Math.sin(bearing);
}

/** The values of one quantity of a column, by falling ln p. */
class Nodes {
  readonly x: Float64Array;
  readonly value: Float64Array;
  count = 0;

  constructor(size: number) {
    this.x = new Float64Array(size);
    this.value = new Float64Array(size);
  }

  add(x: number, value: number): void {
    if (value !== value) return;
    this.x[this.count] = x;
    this.value[this.count] = value;
    this.count += 1;
  }

  /** The value at `x`, the segment searched from `from` up. */
  at(x: number): number {
    const last = this.count - 1;
    if (last < 0) return Number.NaN;
    if (x >= this.x[0]!) return this.value[0]!;
    if (x < this.x[last]! - 1e-6) return Number.NaN;
    let m = 0;
    while (m < last - 1 && this.x[m + 1]! > x) m += 1;
    return this.between(m, x);
  }

  private between(m: number, x: number): number {
    const weight = Math.min(1, (this.x[m]! - x) / (this.x[m]! - this.x[m + 1]!));
    return this.value[m]! + weight * (this.value[m + 1]! - this.value[m]!);
  }

  /** Every row's value, rows by rising height, into one column of a grid. */
  fill(rowX: Float64Array, out: Float32Array, point: number, points: number): void {
    const last = this.count - 1;
    const { x: xs, value } = this;
    let m = 0;
    for (let row = 0, at = point; row < rowX.length; row += 1, at += points) {
      const x = rowX[row]!;
      if (last < 0) out[at] = Number.NaN;
      else if (x >= xs[0]!) out[at] = value[0]!;
      else if (x < xs[last]! - 1e-6) out[at] = Number.NaN;
      else {
        while (m < last - 1 && xs[m + 1]! > x) m += 1;
        out[at] = this.between(m, x);
      }
    }
  }
}

type Base = "t" | "td" | "u" | "v";
export type SectionQuantity = Base | "rh" | "theta" | "thetae" | "wind" | "cross";

const GROUP_BASES: Record<SectionGroup, readonly Base[]> = { t: ["t"], td: ["t", "td"], wind: ["u", "v"] };

/** The columns of a slice through time: seconds of lead time, evenly spaced. */
export interface TimeColumns {
  first: number;
  step: number;
  count: number;
}

/** One frame on the drawing grid, or one point through time, its columns then
 * standing in the points' place. */
export class SectionSlice {
  readonly rows = SECTION_ROWS;
  readonly rowX = new Float64Array(SECTION_ROWS);
  readonly rowP = new Float64Array(SECTION_ROWS);
  /** Surface pressure per point, hPa, NaN where it is not known. */
  readonly ps: Float32Array;
  /** Per point, whether it is drawn. */
  readonly drawn: Uint8Array;
  /** The mean height of each row above the ground, metres. */
  readonly heights = new Float64Array(SECTION_ROWS);
  key = "";
  usedAt = 0;
  private readonly grids = new Map<SectionQuantity, Float32Array>();
  private readonly derived = new Set<SectionQuantity>();

  constructor(
    readonly points: number,
    private readonly bearings: Float64Array,
    /** Degrees, per point. */
    readonly latitudes: Float64Array,
  ) {
    this.ps = new Float32Array(points);
    this.drawn = new Uint8Array(points);
  }

  /** Face every column the one way, at the one latitude. */
  face(bearing: number, latitude: number): void {
    this.bearings.fill(bearing);
    this.latitudes.fill(latitude);
  }

  start(key: string, top: number): void {
    this.key = key;
    this.derived.clear();
    for (let row = 0; row < this.rows; row += 1) {
      this.rowX[row] = Math.log(P_BOTTOM) + (row / (this.rows - 1)) * Math.log(top / P_BOTTOM);
      this.rowP[row] = Math.exp(this.rowX[row]!);
    }
  }

  grid(quantity: SectionQuantity): Float32Array {
    let grid = this.grids.get(quantity);
    if (!grid) {
      grid = new Float32Array(this.points * this.rows);
      this.grids.set(quantity, grid);
    }
    return grid;
  }

  /** A quantity's values, row by row from the bottom, derived when first
   * asked for. The base ones it comes from are the slice's group's. */
  quantity(quantity: SectionQuantity): Float32Array {
    const out = this.grid(quantity);
    if (quantity === "t" || quantity === "td" || quantity === "u" || quantity === "v") return out;
    if (this.derived.has(quantity)) return out;
    this.derived.add(quantity);
    const { points, rows, rowP, rowX, ps, drawn } = this;
    const a = quantity === "wind" || quantity === "cross" ? this.grid("u") : this.grid("t");
    const b = quantity === "wind" || quantity === "cross" ? this.grid("v") : quantity === "theta" ? null : this.grid("td");
    for (let point = 0; point < points; point += 1) {
      if (!drawn[point]) {
        for (let at = point; at < out.length; at += points) out[at] = Number.NaN;
        continue;
      }
      const ground = ps[point]!;
      const groundLog = Math.log(1000 / ground);
      const sin = Math.sin(this.bearings[point]!);
      const cos = Math.cos(this.bearings[point]!);
      for (let row = 0, at = point; row < rows; row += 1, at += points) {
        const x = a[at]!;
        const under = rowP[row]! > ground;
        const p = under ? ground : rowP[row]!;
        switch (quantity) {
          case "wind":
            out[at] = Math.sqrt(x * x + b![at]! * b![at]!);
            break;
          case "cross":
            out[at] = x * cos - b![at]! * sin;
            break;
          case "rh":
            out[at] = relativeHumidity(x, b![at]!);
            break;
          case "theta":
            out[at] = (x + KELVIN) * Math.exp(KAPPA * (under ? groundLog : Math.log(1000) - rowX[row]!));
            break;
          case "thetae":
            // Under the ground the column holds its lowest values.
            out[at] = under && row > 0 ? out[at - points]! : thetaEAt(x, b![at]!, p, under ? groundLog : Math.log(1000) - rowX[row]!);
            break;
        }
      }
    }
    return out;
  }
}

/** One frame of a point through time: its surface pressure and bases by row. */
interface StoodFrame {
  lead: number;
  ps: number;
  values: Float32Array[];
}

/** Frames of a field and times of a point on the drawing grid, and readings off
 * its columns. */
export class SectionDeriver {
  private readonly blended = new Map<SectionArrayId, Float32Array>();
  private readonly one = new Map<SectionArrayId, Float32Array>();
  private readonly pointBuffers = new Map<SectionArrayId, Float32Array>();
  private readonly slices: SectionSlice[] = [];
  private through: SectionSlice | null = null;
  private uses = 0;
  private readonly nodes: Record<Base | "z", Nodes>;
  private readonly column: { p: number; z: number | null }[];
  /** The place in `rh` of each level of `tmp`, or -1. */
  private readonly humidity: Int32Array;
  private lnLevels = new Map<SectionArrayId, Float64Array>();

  constructor(private readonly field: SectionField) {
    let longest = 0;
    for (const { id, levels } of field.spec.arrays) {
      longest = Math.max(longest, levels?.length ?? 0);
      if (levels) this.lnLevels.set(id, Float64Array.from(levels, Math.log));
    }
    const make = (): Nodes => new Nodes(longest + 1);
    this.nodes = { t: make(), td: make(), u: make(), v: make(), z: make() };
    this.column = (field.array("hgt")?.levels ?? []).map((p) => ({ p, z: null }));
    const rh = field.array("rh")?.levels ?? [];
    this.humidity = Int32Array.from(field.array("tmp")?.levels ?? [], (level) => rh.indexOf(level));
  }

  private members(id: SectionArrayId): number {
    return this.field.array(id)?.levels?.length ?? 1;
  }

  /** The frames of some arrays at a store frame's lead time. */
  private blendAll(ids: readonly SectionArrayId[], lead: number): void {
    for (const id of ids) {
      let out = this.blended.get(id);
      if (!out) this.blended.set(id, (out = new Float32Array(this.field.spec.points * this.members(id))));
      this.field.blend(id, this.field.frame(id, lead), out);
    }
  }

  /** Stand point `index` of some blended values on its ground; the surface
   * pressure, NaN where it is not known. */
  private stand(values: Map<SectionArrayId, Float32Array>, index: number, groups: readonly SectionGroup[]): number {
    const at = (id: SectionArrayId): number => values.get(id)?.[index] ?? Number.NaN;
    const { nodes, column } = this;
    const hgt = values.get("hgt");
    const orog = at("orog");
    const prmsl = at("prmsl");
    for (let level = 0; level < column.length; level += 1) {
      const z = hgt ? hgt[index * column.length + level]! : Number.NaN;
      column[level]!.z = z === z ? z : null;
    }
    const pressure = surfacePressure(column, prmsl === prmsl ? prmsl : null, orog === orog ? orog : null);
    const ps = pressure === null ? Number.NaN : pressure;
    const x0 = pressure === null ? Number.NaN : Math.log(pressure);
    const limit = pressure === null ? Number.POSITIVE_INFINITY : x0 - GROUND_CLEARANCE;
    const levels = (id: SectionArrayId, into: Nodes, surface: number): void => {
      into.count = 0;
      if (pressure !== null) into.add(x0, surface);
      const source = values.get(id);
      const lnP = this.lnLevels.get(id);
      if (!source || !lnP) return;
      const size = lnP.length;
      for (let level = 0; level < size; level += 1) {
        if (lnP[level]! < limit) into.add(lnP[level]!, source[index * size + level]!);
      }
    };
    levels("hgt", nodes.z, orog);
    if (groups.includes("t") || groups.includes("td")) levels("tmp", nodes.t, at("tmp2m"));
    if (groups.includes("wind")) {
      levels("ugrd", nodes.u, at("ugrd10m"));
      levels("vgrd", nodes.v, at("vgrd10m"));
    }
    if (groups.includes("td")) {
      const { td } = nodes;
      td.count = 0;
      if (pressure !== null) td.add(x0, at("dpt2m"));
      const tmp = values.get("tmp");
      const rh = values.get("rh");
      const lnP = this.lnLevels.get("tmp");
      if (tmp && rh && lnP) {
        const size = lnP.length;
        const rhSize = this.members("rh");
        for (let level = 0; level < size; level += 1) {
          const place = this.humidity[level]!;
          if (place < 0 || lnP[level]! >= limit) continue;
          const humidity = Math.max(rh[index * rhSize + place]!, LEAST_HUMIDITY);
          td.add(lnP[level]!, dewPointFromRelativeHumidity(tmp[index * size + level]!, humidity));
        }
      }
    }
    return ps;
  }

  /**
   * The frame at a playhead on the grid under a top, with what `groups` need,
   * or null where the store has no frame there. A point of which some array is
   * still to come is NaN all the way up.
   */
  slice(lead: number, top: number, groups: readonly SectionGroup[]): SectionSlice | null {
    const { field } = this;
    const stored = field.leadOf(lead);
    if (stored === null) return null;
    const key = `${stored}/${top}/${[...groups].sort().join()}/${field.version}`;
    const known = this.slices.find((slice) => slice.key === key);
    this.uses += 1;
    if (known) {
      known.usedAt = this.uses;
      return known;
    }
    const { points, bearings, latitudes } = field.spec;
    let slice: SectionSlice;
    if (this.slices.length < SLICES) this.slices.push((slice = new SectionSlice(points, bearings, latitudes)));
    else slice = this.slices.reduce((oldest, other) => (other.usedAt < oldest.usedAt ? other : oldest));
    slice.usedAt = this.uses;
    slice.start(key, top);

    const arrays = [...new Set([...GROUND_ARRAYS, ...groups.flatMap(groupArrays)])].filter((id) => field.has(id));
    this.blendAll(arrays, stored);
    slice.drawn.fill(1);
    for (const id of arrays) {
      const ready = field.ready(id)!;
      for (let point = 0; point < points; point += 1) slice.drawn[point]! &= ready[point]!;
    }
    const bases = [...new Set(groups.flatMap((group) => GROUP_BASES[group]))].map((base) => [this.nodes[base], slice.grid(base)] as const);
    const { rowX, rows, heights } = slice;
    heights.fill(0);
    const counts = new Uint32Array(rows);
    for (let point = 0; point < points; point += 1) {
      if (!slice.drawn[point]) {
        slice.ps[point] = Number.NaN;
        for (const [, grid] of bases) for (let at = point; at < grid.length; at += points) grid[at] = Number.NaN;
        continue;
      }
      slice.ps[point] = this.stand(this.blended, point, groups);
      for (const [nodes, grid] of bases) nodes.fill(rowX, grid, point, points);
      this.addHeights(rowX, heights, counts);
    }
    for (let row = 0; row < rows; row += 1) heights[row] = counts[row]! > 0 ? heights[row]! / counts[row]! : Number.NaN;
    return slice;
  }

  /** Add the heights of the column stood last to the sums of the rows it has
   * them at, above its ground. */
  private addHeights(rowX: Float64Array, heights: Float64Array, counts: Uint32Array): void {
    const { z } = this.nodes;
    if (z.count < 2) return;
    for (let row = 0, m = 0; row < rowX.length; row += 1) {
      const x = rowX[row]!;
      if (x > z.x[0]!) continue;
      if (x < z.x[z.count - 1]! - 1e-6) break;
      while (m < z.count - 2 && z.x[m + 1]! > x) m += 1;
      heights[row]! += z.value[m]! + ((z.x[m]! - x) / (z.x[m]! - z.x[m + 1]!)) * (z.value[m + 1]! - z.value[m]!);
      counts[row]! += 1;
    }
  }

  /** The values of one point at a store frame's lead time, of the arrays that
   * have arrived there. */
  private pointValues(ids: readonly SectionArrayId[], stored: number, point: number): void {
    const { field, one } = this;
    one.clear();
    for (const id of ids) {
      let out = this.pointBuffers.get(id);
      if (!out) this.pointBuffers.set(id, (out = new Float32Array(this.members(id))));
      field.blend(id, field.frame(id, stored), out, point, point + 1);
      one.set(id, out);
    }
  }

  /** The leads of the store's frames around a lead, and how far it is from the
   * one to the other; null past either end of the axis. */
  private bracket(lead: number): { from: number; to: number; weight: number } | null {
    const from = this.field.leadOf(lead);
    if (from === null) return null;
    if (from === lead) return { from, to: from, weight: 0 };
    const to = this.field.leadAfter(from)!;
    return { from, to, weight: (lead - from) / (to - from) };
  }

  /**
   * A point of the plan through time on the grid under a top, with what
   * `groups` need: a column of it per time. A column between two frames is
   * linear in what has been stood on each of their grounds; it is there to be
   * drawn, and a value is read off a frame (`reading`). A column the store's
   * axis does not reach is NaN, and so are all of them until every array has
   * arrived at the point.
   */
  timeSlice(point: number, columns: TimeColumns, top: number, groups: readonly SectionGroup[]): SectionSlice {
    const { field } = this;
    const { points, bearings, latitudes } = field.spec;
    const { first, step, count } = columns;
    const key = `${point}/${first}/${step}/${count}/${top}/${[...groups].sort().join()}/${field.version}`;
    if (this.through?.key === key) return this.through;
    if (point < 0 || point >= points) throw new Error(`section: no point ${point}`);
    if (this.through?.points !== count) {
      this.through = new SectionSlice(count, new Float64Array(count), new Float64Array(count));
    }
    const slice = this.through;
    slice.face(bearings[point]!, latitudes[point]!);
    slice.start(key, top);
    const { rowX, rows, heights, ps, drawn } = slice;
    const bases = [...new Set(groups.flatMap((group) => GROUP_BASES[group]))].map((base) => [this.nodes[base], slice.grid(base)] as const);
    drawn.fill(0);
    ps.fill(Number.NaN);
    heights.fill(Number.NaN);
    for (const [, grid] of bases) grid.fill(Number.NaN);
    const arrays = [...new Set([...GROUND_ARRAYS, ...groups.flatMap(groupArrays)])].filter((id) => field.has(id));
    if (!arrays.every((id) => field.ready(id)![point] === 1)) return slice;

    // The frames on either side of the columns under way: the surface pressure
    // and the bases of each, a row after the other.
    const unstood = (): StoodFrame => ({ lead: Number.NaN, ps: Number.NaN, values: bases.map(() => new Float32Array(rows)) });
    const counts = new Uint32Array(rows);
    heights.fill(0);
    const stand = (frame: StoodFrame, lead: number): void => {
      this.pointValues(arrays, lead, point);
      frame.lead = lead;
      frame.ps = this.stand(this.one, 0, groups);
      bases.forEach(([nodes], at) => nodes.fill(rowX, frame.values[at]!, 0, 1));
      this.addHeights(rowX, heights, counts);
    };
    let before = unstood();
    let after = unstood();
    for (let column = 0; column < count; column += 1) {
      const place = this.bracket(first + column * step);
      if (!place) continue;
      if (before.lead !== place.from) {
        if (after.lead === place.from) [before, after] = [after, before];
        else stand(before, place.from);
      }
      if (place.weight > 0 && after.lead !== place.to) stand(after, place.to);
      const { weight } = place;
      drawn[column] = 1;
      ps[column] = weight === 0 ? before.ps : before.ps + weight * (after.ps - before.ps);
      bases.forEach(([, grid], at) => {
        const a = before.values[at]!;
        const b = after.values[at]!;
        for (let row = 0, to = column; row < rows; row += 1, to += count) {
          grid[to] = weight === 0 ? a[row]! : a[row]! + weight * (b[row]! - a[row]!);
        }
      });
    }
    for (let row = 0; row < rows; row += 1) heights[row] = counts[row]! > 0 ? heights[row]! / counts[row]! : Number.NaN;
    return slice;
  }

  /** The arrays of which a point has arrived, or null before its ground has:
   * until that is placed there is no saying what is above it. */
  private arrivedAt(point: number): SectionArrayId[] | null {
    const { field } = this;
    const arrays = field.spec.arrays.map(({ id }) => id).filter((id) => field.ready(id)![point] === 1);
    return arrays.length === 0 || GROUND_ARRAYS.some((id) => field.has(id) && !arrays.includes(id)) ? null : arrays;
  }

  /**
   * The column of a point of the plan in the frame at a playhead, as
   * `sounding/column.ts` stands one on its ground; null where the store has no
   * frame there or the point's ground has not arrived. A level's dew point is
   * the one the section is drawn with.
   */
  columnAt(lead: number, point: number): { levels: ModelLevel[]; surface: ColumnSurface } | null {
    const { field } = this;
    const stored = field.leadOf(lead);
    if (stored === null || point < 0 || point >= field.spec.points) return null;
    const arrays = this.arrivedAt(point);
    if (!arrays) return null;
    this.pointValues(arrays, stored, point);
    const levels = new Map<number, ModelLevel>();
    const read = (id: SectionArrayId, set: (level: ModelLevel, value: number) => void): void => {
      const values = this.one.get(id);
      const pressures = field.array(id)?.levels;
      if (!values || !pressures) return;
      pressures.forEach((p, at) => {
        const value = values[at]!;
        if (value !== value) return;
        let level = levels.get(p);
        if (!level) levels.set(p, (level = { p, t: null }));
        set(level, value);
      });
    };
    read("hgt", (level, z) => (level.z = z));
    read("tmp", (level, t) => (level.t = t));
    read("rh", (level, rh) => {
      if (level.t !== null) level.td = dewPointFromRelativeHumidity(level.t, Math.max(rh, LEAST_HUMIDITY));
    });
    read("ugrd", (level, u) => (level.u = u));
    read("vgrd", (level, v) => (level.v = v));
    const at = (id: SectionArrayId): number | null => {
      const value = this.one.get(id)?.[0];
      return value === undefined || value !== value ? null : value;
    };
    return {
      levels: [...levels.values()].sort((a, b) => b.p - a.p),
      surface: { t: at("tmp2m"), td: at("dpt2m"), u: at("ugrd10m"), v: at("vgrd10m"), prmsl: at("prmsl"), orog: at("orog") },
    };
  }

  /** The values at a point of the plan and a pressure in the frame at a
   * playhead, NaN where what they come from is not here; null before anything
   * of the point has arrived. */
  reading(lead: number, point: number, pressure: number): SectionReading | null {
    const { field } = this;
    const stored = field.leadOf(lead);
    if (stored === null || point < 0 || point >= field.spec.points) return null;
    const arrays = this.arrivedAt(point);
    if (!arrays) return null;
    this.pointValues(arrays, stored, point);
    const ps = this.stand(this.one, 0, ["t", "td", "wind"]);
    const belowGround = pressure > ps;
    const none = Number.NaN;
    const reading: SectionReading = {
      point,
      pressure,
      belowGround,
      surfacePressure: ps,
      height: none,
      temperature: none,
      humidity: none,
      windSpeed: none,
      windDirection: none,
      theta: none,
      thetaE: none,
    };
    if (belowGround) return reading;
    const x = Math.log(pressure);
    const t = this.nodes.t.at(x);
    const td = this.nodes.td.at(x);
    const u = this.nodes.u.at(x);
    const v = this.nodes.v.at(x);
    reading.height = this.nodes.z.at(x);
    reading.temperature = t;
    reading.humidity = relativeHumidity(t, td);
    reading.windSpeed = Math.hypot(u, v);
    reading.windDirection = u === 0 && v === 0 ? none : ((Math.atan2(-u, -v) * 180) / Math.PI + 360) % 360;
    reading.theta = potentialTemperature(t, pressure);
    reading.thetaE = equivalentPotentialTemperature(t, td, pressure);
    return reading;
  }
}

/** The pressure each multiple of `step` metres of height is at on a slice,
 * averaged along the line. */
export function roundHeights(slice: SectionSlice, step: number): { metres: number; pressure: number }[] {
  const found: { metres: number; pressure: number }[] = [];
  let metres = step;
  for (let row = 1; row < slice.rows; row += 1) {
    const low = slice.heights[row - 1]!;
    const high = slice.heights[row]!;
    if (!(high > low)) continue;
    while (metres < low) metres += step;
    for (; metres <= high; metres += step) {
      const weight = (metres - low) / (high - low);
      found.push({ metres, pressure: Math.exp(slice.rowX[row - 1]! + weight * (slice.rowX[row]! - slice.rowX[row - 1]!)) });
    }
  }
  return found;
}

/** 1-2-1 along the rows, twice: the codebooks' steps would otherwise show as
 * teeth on a contour. A neighbour that is missing counts as the value itself. */
export function smoothAlong(values: Float32Array, points: number, out: Float32Array): void {
  for (let row = 0; row + points <= values.length; row += points) {
    let before = values[row]!;
    for (let point = 0; point < points; point += 1) {
      const value = values[row + point]!;
      const after = point + 1 < points ? values[row + point + 1]! : value;
      out[row + point] = (2 * value + (before === before ? before : value) + (after === after ? after : value)) / 4;
      before = value;
    }
    before = out[row]!;
    for (let point = 0; point < points; point += 1) {
      const value = out[row + point]!;
      const after = point + 1 < points ? out[row + point + 1]! : value;
      out[row + point] = (2 * value + (before === before ? before : value) + (after === after ? after : value)) / 4;
      before = value;
    }
  }
}
