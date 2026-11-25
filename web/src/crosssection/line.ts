/**
 * The line of a section: what is refused, where it is cut short, and how it
 * is sampled (`SectionPlan`). Plain functions over the store's grid.
 *
 * The points are evenly spaced along the great circle, about half a grid cell
 * apart, and each is read bilinearly, as the map's shader filters the field.
 * What a read costs is the blocks the line crosses rather than its length, so
 * that is what its length is limited by.
 */

import { AUTHALIC_RADIUS } from "../geodesy";
import { wrap } from "../gridmap";
import { distance, EARTH_RADIUS, greatCircle } from "../measure";
import type { LonLat, SectionLine, SectionPlan, SectionPoint, SectionStore } from "./types";

/** The most blocks a section reads: 3300 to 5600 km on a 0.25 degree grid,
 * with the direction it runs. */
export const MAX_BLOCKS = 32;
/** Metres. A shorter line is a few grid cells, which a section says nothing
 * about. */
export const MIN_LENGTH = 200_000;
/** The most points a section is sampled at. */
export const MAX_POINTS = 512;
/** Metres: two ends closer than this are one place. */
const COINCIDENT = 1_000;
/** Metres short of the antipode: from there on the great circle between two
 * ends is whichever way the last digit of their coordinates points. */
const ANTIPODAL = 200_000;
/** Cells: a point this near a row or a column is read off it alone, so that a
 * line along one does not read the next for nothing. */
const ON_CENTRE = 1e-6;

const RADIANS = Math.PI / 180;

/** The part of a store a line is planned on. */
export type SectionGrid = Pick<SectionStore, "grid" | "width" | "height" | "blockHeight" | "blockWidth">;

/** Why a line is not a section's. */
export type LineRefusal = "coincident" | "antipodal" | "short";

/** Why a line is refused, or null for one that can be drawn. */
export function validateLine(line: SectionLine): LineRefusal | null {
  const length = distance(line.a, line.b);
  if (length < COINCIDENT) return "coincident";
  const opposite: LonLat = [line.a[0] + 180, -line.a[1]];
  if (distance(opposite, line.b) < ANTIPODAL) return "antipodal";
  return length < MIN_LENGTH ? "short" : null;
}

type Vector = [number, number, number];

function unit([lon, lat]: LonLat): Vector {
  const phi = lat * RADIANS;
  const lambda = lon * RADIANS;
  return [Math.cos(phi) * Math.cos(lambda), Math.cos(phi) * Math.sin(lambda), Math.sin(phi)];
}

/** Which way the great circle from `a` to `b` runs at a point of it, radians
 * clockwise from north. A pole has no north: there the point's own longitude
 * says which meridian the direction is told from. */
function bearingAt(a: LonLat, b: LonLat, [lon, lat]: LonLat): number {
  const [ax, ay, az] = unit(a);
  const [bx, by, bz] = unit(b);
  const normal: Vector = [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx];
  const [px, py, pz] = unit([lon, lat]);
  const along: Vector = [normal[1] * pz - normal[2] * py, normal[2] * px - normal[0] * pz, normal[0] * py - normal[1] * px];
  const phi = lat * RADIANS;
  const lambda = lon * RADIANS;
  const east = -Math.sin(lambda) * along[0] + Math.cos(lambda) * along[1];
  const north = -Math.sin(phi) * Math.cos(lambda) * along[0] - Math.sin(phi) * Math.sin(lambda) * along[1] + Math.cos(phi) * along[2];
  return wrap(Math.atan2(east, north), 2 * Math.PI);
}

/** Half a cell of the grid, metres, along the finer of its axes. */
function spacing(grid: SectionGrid): number {
  const step = Math.min(grid.grid.nominalStepDegrees("longitude"), grid.grid.nominalStepDegrees("latitude"));
  return (step / 2) * RADIANS * AUTHALIC_RADIUS;
}

/** The columns go all the way round, so that the last one's neighbour is the
 * first. */
function wraps(grid: SectionGrid): boolean {
  const { west, east } = grid.grid.bounds();
  return east - west > 360 - 1e-6;
}

/** The two indexes a coordinate lies between and how far it is to the second,
 * the same one twice on a centre. */
function between(at: number): [number, number, number] {
  const low = Math.floor(at);
  const part = at - low;
  if (part < ON_CENTRE) return [low, low, 0];
  if (part > 1 - ON_CENTRE) return [low + 1, low + 1, 0];
  return [low, low + 1, part];
}

/** The four cells a point is blended from, `[row, column]`, and their
 * weights, or null off the grid: `probe.ts::bilinearCells` in cells. */
function cornersAt(
  grid: SectionGrid,
  around: boolean,
  lon: number,
  lat: number,
): { cells: [number, number][]; weights: number[] } | null {
  const at = grid.grid.lonLatToCell(lon, lat);
  if (!at) return null;
  const [column0, column1, fx] = between(at[0]);
  const [row0, row1, fy] = between(at[1]);
  const columnAt = (column: number) => (around ? wrap(column, grid.width) : Math.min(grid.width - 1, Math.max(0, column)));
  const rowAt = (row: number) => Math.min(grid.height - 1, Math.max(0, row));
  const cells: [number, number][] = [];
  const weights: number[] = [];
  for (const [row, wy] of [[row0, 1 - fy], [row1, fy]] as const) {
    for (const [column, wx] of [[column0, 1 - fx], [column1, fx]] as const) {
      cells.push([rowAt(row), columnAt(column)]);
      weights.push(wx * wy);
    }
  }
  return { cells, weights };
}

/** The block a cell lies in, as one number. */
function blockOf(grid: SectionGrid, row: number, column: number): number {
  return Math.floor(row / grid.blockHeight) * Math.ceil(grid.width / grid.blockWidth) + Math.floor(column / grid.blockWidth);
}

/**
 * Where a section is sampled, at `maxPoints` points at the most. Longitudes
 * are continuous from `a`'s, past the antimeridian too. A point off the grid
 * has four weights of zero, and its corners mean nothing.
 */
export function planSection(line: SectionLine, grid: SectionGrid, maxPoints = MAX_POINTS): SectionPlan {
  const path = greatCircle(line.a, line.b, spacing(grid), Math.max(1, Math.floor(maxPoints) - 1));
  const length = distance(line.a, line.b);
  const around = wraps(grid);
  const points: SectionPoint[] = [];
  const cells: number[] = [];
  const places = new Map<number, number>();
  const blocks = new Set<number>();
  const corners = new Uint32Array(path.length * 4);
  const weights = new Float32Array(path.length * 4);
  for (const [index, [lon, lat]] of path.entries()) {
    points.push({ lon, lat, along: (length * index) / (path.length - 1), bearing: bearingAt(line.a, line.b, [lon, lat]) });
    const found = cornersAt(grid, around, lon, lat);
    if (!found) continue;
    for (const [corner, [row, column]] of found.cells.entries()) {
      const key = row * grid.width + column;
      let place = places.get(key);
      if (place === undefined) {
        place = places.size;
        places.set(key, place);
        cells.push(row, column);
        blocks.add(blockOf(grid, row, column));
      }
      corners[index * 4 + corner] = place;
      weights[index * 4 + corner] = found.weights[corner]!;
    }
  }
  return { line, length, points, cells: Uint32Array.from(cells), corners, weights, blocks: blocks.size };
}

/**
 * The line as far from `a` as `maxBlocks` blocks reach, and whether that is
 * short of `b`. The cut is made along the line's own great circle, with
 * `planSection`'s count for the line handed back, whatever its points land on.
 */
export function clampLine(line: SectionLine, grid: SectionGrid, maxBlocks = MAX_BLOCKS): { line: SectionLine; clamped: boolean } {
  const step = spacing(grid);
  const path = greatCircle(line.a, line.b, step, Math.ceil((Math.PI * EARTH_RADIUS) / step));
  const around = wraps(grid);
  const blocks = new Set<number>();
  let last = 0;
  for (; last < path.length; last += 1) {
    const [lon, lat] = path[last]!;
    for (const [row, column] of cornersAt(grid, around, lon, lat)?.cells ?? []) blocks.add(blockOf(grid, row, column));
    if (blocks.size > maxBlocks) break;
  }
  if (last === path.length) {
    if (planSection(line, grid, Infinity).blocks <= maxBlocks) return { line, clamped: false };
  }
  // A shorter line's points land elsewhere on the same circle and can clip one
  // more block's corner than the walk did.
  for (last -= 1; last > 1; last -= 1) {
    const cut: SectionLine = { a: line.a, b: path[last]! };
    if (planSection(cut, grid, Infinity).blocks <= maxBlocks) return { line: cut, clamped: true };
  }
  return { line: { a: line.a, b: path[1]! }, clamped: true };
}
