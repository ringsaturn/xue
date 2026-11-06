/**
 * The one place a bundle's `grid` block is read and a cell is turned into a
 * coordinate and back.
 *
 * Three shapes (docs/format.md, "Regional Grids"): a regular
 * latitude/longitude grid; the same with a `gaussian` block, whose rows really
 * lie on Legendre roots; and a Lambert conformal grid, the model's own cells.
 * `GeoGrid` is the parsed block; `gridMapFor` gives its `GridMap`.
 *
 * A Gaussian grid keeps its regular fields because the shaders render from
 * them: the rows differ from equal spacing by at most 0.014 of a cell, below
 * any filter's resolution, and a table per bundle would buy nothing there.
 * `GridMap` is what uses the true latitudes, for which cell a point falls in
 * and where that cell is. Its extents stay the regular ones, so what is fetched
 * covers what the shader draws.
 */

import {
  LAMBERT_LATITUDE_LIMIT,
  lambertCone,
  lambertForward,
  lambertInverse,
  type LambertCone,
  type LambertDomain,
} from "./domain";

export interface LambertProjection {
  kind: "lambert-conformal";
  /** Sphere radius in metres. */
  radius: number;
  standardParallel1: number;
  standardParallel2: number;
  latitudeOfOrigin: number;
  centralMeridian: number;
  /** The first cell's centre in projected metres; `y0` is the north row. */
  x0: number;
  y0: number;
  /** Metres per column, and per row (negative: rows run north to south). */
  dx: number;
  dy: number;
}

/** Where a Gaussian grid's rows sit among the 2n Legendre roots, so a crop or
 * a thinned tier names its own. */
export interface GaussianRows {
  n: number;
  firstRow: number;
  rowStride: number;
}

export interface RegularGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
  /** The columns cover the full 360 degrees, so they wrap at the
   * antimeridian; a cropped grid's do not. */
  wraps: boolean;
  gaussian?: GaussianRows;
  projection?: undefined;
}

export interface LambertGrid {
  width: number;
  height: number;
  wraps: false;
  projection: LambertProjection;
  gaussian?: undefined;
}

export type GeoGrid = RegularGrid | LambertGrid;

/** A box of the globe in degrees. `east` may exceed `west` by more than 360
 * when the map is zoomed out past one world; `east < west` never happens
 * because MapLibre unwraps the eastern edge instead. */
export interface GeoBox {
  west: number;
  east: number;
  south: number;
  north: number;
}

/** A rectangle of cells, inclusive of both ends. On a wrapping grid the
 * columns may run past the width: they count from the view's west edge. */
export interface CellRange {
  firstColumn: number;
  lastColumn: number;
  firstRow: number;
  lastRow: number;
}

export interface GridMap {
  /** The continuous cell of a point, centres on the integers, or null when no
   * cell's half-width reaches it. A wrapping grid takes every longitude. */
  lonLatToCell(longitude: number, latitude: number): [number, number] | null;
  /** The point at a continuous cell. Longitudes are not normalised. */
  cellToLonLat(column: number, row: number): [number, number];
  /** The box around the grid's outer cell edges. */
  bounds(): GeoBox;
  /** The cells a box of the globe touches, or null when it misses the grid. */
  coverOfBounds(box: GeoBox): CellRange | null;
  /** The size of a cell in degrees of arc: the grid's own step on a regular
   * grid, the metres over the sphere's degree on a projected one. For what
   * wants a scale rather than a coordinate. */
  nominalStepDegrees(axis?: "longitude" | "latitude"): number;
}

/** Positive remainder, the way GLSL's mod() and the spec's degree wrap work. */
export function wrap(value: number, modulus: number): number {
  return ((value % modulus) + modulus) % modulus;
}

// -- parsing -----------------------------------------------------------------

const REGULAR_FIELDS = ["firstLongitude", "firstLatitude", "longitudeStep", "latitudeStep"] as const;
const CONE_FIELDS = ["radius", "standardParallel1", "standardParallel2", "latitudeOfOrigin", "centralMeridian"] as const;
const PROJECTION_FIELDS = [...CONE_FIELDS, "x0", "y0", "dx", "dy"] as const;

function finite(block: Record<string, unknown>, key: string, at: string): number {
  const value = block[key];
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${at}.${key} must be a number`);
  return value;
}

function positiveInteger(block: Record<string, unknown>, key: string, at: string): number {
  const value = finite(block, key, at);
  if (!Number.isInteger(value) || value <= 0) throw new Error(`${at}.${key} must be a positive integer`);
  return value;
}

function closedBlock(block: Record<string, unknown>, known: readonly string[], at: string): void {
  for (const key of Object.keys(block)) if (!known.includes(key)) throw new Error(`${at}.${key} is not a field of this block`);
}

function checkOrder(block: Record<string, unknown>, key: string, only: string): void {
  if (block[key] !== undefined && block[key] !== only) throw new Error(`bundle grid ${key} must be ${only}`);
}

function parseProjection(input: unknown): LambertProjection {
  if (typeof input !== "object" || input === null || Array.isArray(input)) throw new Error("bundle grid projection must be an object");
  const block = input as Record<string, unknown>;
  closedBlock(block, ["kind", ...PROJECTION_FIELDS], "bundle grid projection");
  // A closed set: a kind this shell does not know lays its cells some way it
  // would misread, so the bundle is refused rather than drawn wrong.
  if (block.kind !== "lambert-conformal") throw new Error(`unknown bundle grid projection kind ${String(block.kind)}`);
  const values = Object.fromEntries(PROJECTION_FIELDS.map((key) => [key, finite(block, key, "bundle grid projection")]));
  const projection = { kind: "lambert-conformal", ...values } as unknown as LambertProjection;
  if (projection.radius <= 0) throw new Error("bundle grid projection radius must be positive");
  if (projection.dx <= 0 || projection.dy >= 0) throw new Error("bundle grid projection must run west to east and north to south");
  for (const key of ["standardParallel1", "standardParallel2", "latitudeOfOrigin"] as const) {
    if (Math.abs(projection[key]) >= 90) throw new Error(`bundle grid projection ${key} must lie strictly inside the poles`);
  }
  // Parallels summing to zero have a cone constant of zero, a cylinder.
  if (projection.standardParallel1 + projection.standardParallel2 === 0) throw new Error("bundle grid projection parallels must not sum to zero");
  if (Math.abs(projection.centralMeridian) > 180) throw new Error("bundle grid projection centralMeridian must lie within 180 degrees");
  return projection;
}

function parseGaussian(input: unknown, height: number, latitudeStep: number): GaussianRows {
  if (typeof input !== "object" || input === null || Array.isArray(input)) throw new Error("bundle grid gaussian must be an object");
  const block = input as Record<string, unknown>;
  closedBlock(block, ["n", "firstRow", "rowStride"], "bundle grid gaussian");
  const n = positiveInteger(block, "n", "bundle grid gaussian");
  if (n > 32768) throw new Error("bundle grid gaussian.n must be at most 32768");
  const firstRow = finite(block, "firstRow", "bundle grid gaussian");
  if (!Number.isInteger(firstRow) || firstRow < 0) throw new Error("bundle grid gaussian.firstRow must be a whole number");
  const rowStride = positiveInteger(block, "rowStride", "bundle grid gaussian");
  if (firstRow + (height - 1) * rowStride >= 2 * n) throw new Error("bundle grid gaussian runs past its last row");
  if (latitudeStep >= 0) throw new Error("a gaussian grid runs north to south");
  return { n, firstRow, rowStride };
}

/** A `grid` block, whole or refused. Only a regular grid may leave fields out,
 * and only because published files do: format.md's regular global grid is what
 * those fields default to in every reader. A projected grid has no defaults,
 * and a field that is present and wrong is refused on any grid. */
export function parseGrid(input: unknown): GeoGrid {
  if (typeof input !== "object" || input === null || Array.isArray(input)) throw new Error("bundle grid must be an object");
  const block = input as Record<string, unknown>;
  const width = positiveInteger(block, "width", "bundle grid");
  const height = positiveInteger(block, "height", "bundle grid");
  checkOrder(block, "layout", "row-major");
  checkOrder(block, "rowOrder", "north-to-south");
  checkOrder(block, "columnOrder", "west-to-east");
  if (block.projection !== undefined) {
    const present = REGULAR_FIELDS.filter((key) => block[key] !== undefined);
    if (present.length > 0) throw new Error(`a projected bundle grid carries no ${present.join(", ")}`);
    if (block.gaussian !== undefined) throw new Error("a projected bundle grid carries no gaussian block");
    if (block.wrapLongitude !== false) throw new Error("a projected bundle grid must declare wrapLongitude false");
    return { width, height, wraps: false, projection: parseProjection(block.projection) };
  }
  const optional = (key: (typeof REGULAR_FIELDS)[number], fallback: number) => (block[key] === undefined ? fallback : finite(block, key, "bundle grid"));
  const longitudeStep = optional("longitudeStep", 0.25);
  const latitudeStep = optional("latitudeStep", -0.25);
  if (longitudeStep <= 0) throw new Error("bundle grid longitudeStep must be positive");
  if (latitudeStep === 0) throw new Error("bundle grid latitudeStep must not be zero");
  let wraps = block.wrapLongitude;
  if (wraps === undefined) wraps = Math.abs(width * longitudeStep - 360) < 1e-6;
  if (typeof wraps !== "boolean") throw new Error("bundle grid wrapLongitude must be a boolean");
  const grid: RegularGrid = {
    width,
    height,
    firstLongitude: optional("firstLongitude", -180),
    firstLatitude: optional("firstLatitude", 90),
    longitudeStep,
    latitudeStep,
    wraps,
  };
  if (block.gaussian !== undefined) grid.gaussian = parseGaussian(block.gaussian, height, latitudeStep);
  return grid;
}

const parsedGrids = new WeakMap<object, GeoGrid>();

/** A bundle's grid, parsed once per metadata object. */
export function geoGrid(metadata: { grid: unknown }): GeoGrid {
  let grid = parsedGrids.get(metadata);
  if (!grid) {
    grid = parseGrid(metadata.grid);
    parsedGrids.set(metadata, grid);
  }
  return grid;
}

// -- Gaussian latitudes ------------------------------------------------------

const gaussianTables = new Map<number, Float64Array>();

/** The latitudes of the 2n rows of a Gaussian grid, north to south: the roots
 * of the Legendre polynomial of degree 2n, by the Newton iteration the encoders
 * run operation for operation. Tens of milliseconds at n = 768, so it runs when
 * a lookup first wants it and the table is kept. */
export function gaussianLatitudes(n: number): Float64Array {
  const known = gaussianTables.get(n);
  if (known) return known;
  const degree = 2 * n;
  const table = new Float64Array(degree);
  for (let i = 0; i < degree; i += 1) {
    let x = Math.cos((Math.PI * (i + 0.75)) / (degree + 0.5));
    for (let iteration = 0; iteration < 100; iteration += 1) {
      let previous = 1;
      let value = x;
      for (let k = 2; k <= degree; k += 1) {
        const next = ((2 * k - 1) * x * value - (k - 1) * previous) / k;
        previous = value;
        value = next;
      }
      const slope = (degree * (x * value - previous)) / (x * x - 1);
      const step = value / slope;
      x -= step;
      if (Math.abs(step) < 1e-15) break;
    }
    table[i] = (Math.asin(x) * 180) / Math.PI;
  }
  gaussianTables.set(n, table);
  return table;
}

// -- the maps ----------------------------------------------------------------

function clampIndex(value: number, last: number): number {
  return Math.min(last, Math.max(0, value));
}

class RegularMap implements GridMap {
  constructor(protected readonly grid: RegularGrid) {}

  /** The continuous row of a latitude; past the grid is left to the caller. */
  protected rowOf(latitude: number): number {
    return (latitude - this.grid.firstLatitude) / this.grid.latitudeStep;
  }

  protected latitudeOf(row: number): number {
    return this.grid.firstLatitude + row * this.grid.latitudeStep;
  }

  lonLatToCell(longitude: number, latitude: number): [number, number] | null {
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
    const { grid } = this;
    // Degrees east of the grid origin, wrapped into [0, 360): a window that
    // crosses the antimeridian stays contiguous instead of splitting in two.
    const column = wrap(longitude - grid.firstLongitude, 360) / grid.longitudeStep;
    if (!grid.wraps && column >= grid.width - 0.5) return null;
    const row = this.rowOf(latitude);
    if (row < -0.5 || row >= grid.height - 0.5) return null;
    return [column, row];
  }

  cellToLonLat(column: number, row: number): [number, number] {
    return [this.grid.firstLongitude + column * this.grid.longitudeStep, this.latitudeOf(row)];
  }

  bounds(): GeoBox {
    const { grid } = this;
    const west = grid.firstLongitude - grid.longitudeStep / 2;
    const edgeA = grid.firstLatitude - grid.latitudeStep / 2;
    const edgeB = edgeA + grid.height * grid.latitudeStep;
    return { west, east: west + grid.width * grid.longitudeStep, south: Math.min(edgeA, edgeB), north: Math.max(edgeA, edgeB) };
  }

  coverOfBounds(box: GeoBox): CellRange | null {
    const { grid } = this;
    const edges = [(box.north - grid.firstLatitude) / grid.latitudeStep, (box.south - grid.firstLatitude) / grid.latitudeStep];
    const top = Math.floor(Math.min(...edges));
    const bottom = Math.ceil(Math.max(...edges));
    if (bottom < 0 || top > grid.height - 1) return null;
    const span = box.east - box.west;
    if (!Number.isFinite(span) || span < 0) return null;
    const rows = { firstRow: clampIndex(top, grid.height - 1), lastRow: clampIndex(bottom, grid.height - 1) };
    if (grid.wraps) {
      // Every longitude is on some column, so the answer is the columns the
      // span reaches from the view's west edge, left for the caller to wrap.
      const origin = wrap(box.west - grid.firstLongitude, 360) / grid.longitudeStep;
      return { firstColumn: Math.floor(origin), lastColumn: Math.floor(origin + span / grid.longitudeStep), ...rows };
    }
    const columns = this.croppedColumns(box.west, span);
    return columns && { firstColumn: columns[0], lastColumn: columns[1], ...rows };
  }

  /** The band is tried in every copy of the world that can reach the grid — a
   * view west of a cropped window is at a negative cell, not most of the way
   * around the world — and a view wider than the world (zoomed out on a wide
   * screen) can reach it from two copies at once, which together may cover
   * all of it. */
  private croppedColumns(west: number, span: number): [number, number] | null {
    const { grid } = this;
    const extent = grid.width * grid.longitudeStep;
    const base = wrap(west - grid.firstLongitude, 360);
    let first = Number.POSITIVE_INFINITY;
    let last = Number.NEGATIVE_INFINITY;
    for (let start = base - 360 * Math.ceil(span / 360 + 1); start <= extent; start += 360) {
      if (start + span < 0) continue;
      first = Math.min(first, Math.floor(start / grid.longitudeStep));
      last = Math.max(last, Math.ceil((start + span) / grid.longitudeStep));
    }
    if (last < 0 || first > grid.width - 1) return null;
    return [clampIndex(first, grid.width - 1), clampIndex(last, grid.width - 1)];
  }

  nominalStepDegrees(axis: "longitude" | "latitude" = "longitude"): number {
    return Math.abs(axis === "longitude" ? this.grid.longitudeStep : this.grid.latitudeStep);
  }
}

class GaussianMap extends RegularMap {
  private readonly table: Float64Array;
  private readonly gaussian: GaussianRows;

  constructor(grid: RegularGrid, gaussian: GaussianRows) {
    super(grid);
    this.gaussian = gaussian;
    this.table = gaussianLatitudes(gaussian.n);
  }

  /** The latitude of a row of this grid, extended past both ends along the
   * rows the crop left out. Null past the last root: nothing lies beyond. */
  private rowLatitude(row: number): number | null {
    const { firstRow, rowStride } = this.gaussian;
    return this.table[firstRow + row * rowStride] ?? null;
  }

  protected override rowOf(latitude: number): number {
    const last = this.grid.height - 1;
    const north = this.rowLatitude(0)!;
    const south = this.rowLatitude(last)!;
    if (latitude >= north) {
      const beyond = this.rowLatitude(-1);
      // Rows are nearest-latitude cells, so the polar cap is the first row's:
      // no row lies north of it to take the half past its spacing.
      return beyond === null ? 0 : (north - latitude) / (beyond - north);
    }
    if (latitude <= south) {
      const beyond = this.rowLatitude(last + 1);
      return beyond === null ? last : last + (south - latitude) / (south - beyond);
    }
    let low = 0;
    let high = last;
    while (high - low > 1) {
      const middle = (low + high) >> 1;
      if (this.rowLatitude(middle)! >= latitude) low = middle;
      else high = middle;
    }
    const a = this.rowLatitude(low)!;
    const b = this.rowLatitude(high)!;
    return low + (a - latitude) / (a - b);
  }

  protected override latitudeOf(row: number): number {
    const last = this.grid.height - 1;
    if (row < 0 || row > last) {
      const edge = row < 0 ? 0 : last;
      return Math.max(-90, Math.min(90, this.rowLatitude(edge)! + (row - edge) * this.grid.latitudeStep));
    }
    const low = Math.floor(row);
    const a = this.rowLatitude(low)!;
    if (low === row) return a;
    return a + (row - low) * (this.rowLatitude(low + 1)! - a);
  }
}

/** Samples per edge of a box, at the least. */
const COVER_SAMPLES = 64;

class LambertMap implements GridMap {
  private readonly projection: LambertProjection;
  private readonly cone: LambertCone;
  private box: GeoBox | null = null;

  constructor(private readonly grid: LambertGrid) {
    this.projection = grid.projection;
    this.cone = lambertCone(grid.projection);
  }

  /** The continuous cell of a point, whether or not it is on the grid. */
  private cellOf(longitude: number, latitude: number): [number, number] {
    const limit = Number(LAMBERT_LATITUDE_LIMIT);
    const held = Math.max(-limit, Math.min(limit, latitude));
    const [x, y] = lambertForward(this.projection, this.cone, longitude, held);
    return [(x - this.projection.x0) / this.projection.dx, (y - this.projection.y0) / this.projection.dy];
  }

  lonLatToCell(longitude: number, latitude: number): [number, number] | null {
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
    const [column, row] = this.cellOf(longitude, latitude);
    if (column < -0.5 || column >= this.grid.width - 0.5) return null;
    if (row < -0.5 || row >= this.grid.height - 0.5) return null;
    return [column, row];
  }

  cellToLonLat(column: number, row: number): [number, number] {
    const { x0, y0, dx, dy } = this.projection;
    return lambertInverse(this.projection, this.cone, x0 + column * dx, y0 + row * dy);
  }

  bounds(): GeoBox {
    if (this.box) return this.box;
    const { width, height } = this.grid;
    let west = Number.POSITIVE_INFINITY;
    let east = Number.NEGATIVE_INFINITY;
    let south = Number.POSITIVE_INFINITY;
    let north = Number.NEGATIVE_INFINITY;
    const visit = (column: number, row: number) => {
      const [longitude, latitude] = this.cellToLonLat(column, row);
      west = Math.min(west, longitude);
      east = Math.max(east, longitude);
      south = Math.min(south, latitude);
      north = Math.max(north, latitude);
    };
    for (let k = 0; k <= COVER_SAMPLES; k += 1) {
      const across = -0.5 + (k / COVER_SAMPLES) * width;
      const down = -0.5 + (k / COVER_SAMPLES) * height;
      visit(across, -0.5);
      visit(across, height - 0.5);
      visit(-0.5, down);
      visit(width - 0.5, down);
    }
    this.box = { west, east, south, north };
    return this.box;
  }

  /** The box's boundary projected and its bounding rectangle taken: a
   * conformal map sends interior to interior, so the boundary holds every
   * extreme, but for the meridian opposite the cone's centre, where the
   * projection is cut and the box's two sides of it land apart. That line is
   * walked too. One cell more all round is what the filter kernel reads past
   * the last centre covered. */
  coverOfBounds(box: GeoBox): CellRange | null {
    const span = box.east - box.west;
    const height = box.north - box.south;
    if (!Number.isFinite(span) || !Number.isFinite(height) || span < 0 || height < 0) return null;
    let minColumn = Number.POSITIVE_INFINITY;
    let maxColumn = Number.NEGATIVE_INFINITY;
    let minRow = Number.POSITIVE_INFINITY;
    let maxRow = Number.NEGATIVE_INFINITY;
    const visit = (longitude: number, latitude: number) => {
      const [column, row] = this.cellOf(longitude, latitude);
      minColumn = Math.min(minColumn, column);
      maxColumn = Math.max(maxColumn, column);
      minRow = Math.min(minRow, row);
      maxRow = Math.max(maxRow, row);
    };
    // A parallel projects to an arc, and a chord between samples cuts inside
    // it. A degree of longitude at a time keeps that under a cell on any
    // world-wide view; a meridian is straight, so the same count is plenty.
    const samples = Math.max(COVER_SAMPLES, Math.ceil(span));
    for (let k = 0; k <= samples; k += 1) {
      const longitude = box.west + (k / samples) * span;
      visit(longitude, box.north);
      visit(longitude, box.south);
    }
    const walkMeridian = (longitude: number) => {
      for (let k = 0; k <= COVER_SAMPLES; k += 1) visit(longitude, box.south + (k / COVER_SAMPLES) * height);
    };
    walkMeridian(box.west);
    walkMeridian(box.east);
    const meridian = this.projection.centralMeridian;
    for (let cut = meridian + 180 + 360 * Math.ceil((box.west - meridian - 180) / 360); cut <= box.east; cut += 360) {
      walkMeridian(cut - 1e-6);
      walkMeridian(cut + 1e-6);
    }
    const first = Math.floor(minColumn) - 1;
    const last = Math.ceil(maxColumn) + 1;
    const top = Math.floor(minRow) - 1;
    const bottom = Math.ceil(maxRow) + 1;
    const { width, height: rows } = this.grid;
    if (last < 0 || first > width - 1 || bottom < 0 || top > rows - 1) return null;
    return {
      firstColumn: clampIndex(first, width - 1),
      lastColumn: clampIndex(last, width - 1),
      firstRow: clampIndex(top, rows - 1),
      lastRow: clampIndex(bottom, rows - 1),
    };
  }

  nominalStepDegrees(axis: "longitude" | "latitude" = "longitude"): number {
    const metres = Math.abs(axis === "longitude" ? this.projection.dx : this.projection.dy);
    return (metres / this.projection.radius) * (180 / Math.PI);
  }
}

const gridMaps = new WeakMap<GeoGrid, GridMap>();

/** The map of a parsed grid, kept for as long as the grid is. */
export function gridMapFor(grid: GeoGrid): GridMap {
  let map = gridMaps.get(grid);
  if (!map) {
    if (grid.projection) map = new LambertMap(grid);
    else if (grid.gaussian) map = new GaussianMap(grid, grid.gaussian);
    else map = new RegularMap(grid);
    gridMaps.set(grid, map);
  }
  return map;
}

/** Whether two grids put their cells in the same place. */
export function sameGrid(a: GeoGrid, b: GeoGrid): boolean {
  if (a.width !== b.width || a.height !== b.height) return false;
  if (a.projection || b.projection) {
    const [pa, pb] = [a.projection, b.projection];
    return !!pa && !!pb && PROJECTION_FIELDS.every((key) => pa[key] === pb[key]);
  }
  return (
    a.firstLongitude === b.firstLongitude &&
    a.firstLatitude === b.firstLatitude &&
    a.longitudeStep === b.longitudeStep &&
    a.latitudeStep === b.latitudeStep &&
    a.gaussian?.n === b.gaussian?.n &&
    a.gaussian?.firstRow === b.gaussian?.firstRow &&
    a.gaussian?.rowStride === b.gaussian?.rowStride
  );
}

/** Whether two grids' columns and rows are straight lines of one another, so
 * a cell of one maps to a cell of the other one axis at a time: two tiers of
 * one projection, or two regular grids. A Gaussian grid's rows are not evenly
 * spaced, so it only qualifies against itself. */
export function axesAlign(a: GeoGrid, b: GeoGrid): boolean {
  if (a.projection || b.projection) {
    const [pa, pb] = [a.projection, b.projection];
    return !!pa && !!pb && CONE_FIELDS.every((key) => pa[key] === pb[key]);
  }
  return sameGrid(a, b) || (!a.gaussian && !b.gaussian);
}

/** The first and step of a grid in the terms its own cells are spaced in:
 * degrees on a regular grid, projected metres on a Lambert one, column then
 * row. These with the cone are the uniforms `GRID_GLSL` reads. */
export interface GridForm {
  first: [number, number];
  step: [number, number];
  /** (n, scale, origin radius, central meridian in radians); all zero on a
   * regular grid, which is how the shader tells. */
  cone: [number, number, number, number];
}

export function gridForm(grid: GeoGrid): GridForm {
  if (!grid.projection) {
    return {
      first: [grid.firstLongitude, grid.firstLatitude],
      step: [grid.longitudeStep, grid.latitudeStep],
      cone: [0, 0, 0, 0],
    };
  }
  const { x0, y0, dx, dy, centralMeridian } = grid.projection;
  const cone = lambertCone(grid.projection);
  return { first: [x0, y0], step: [dx, dy], cone: [cone.n, cone.scale, cone.originRadius, (centralMeridian * Math.PI) / 180] };
}

/** The grid a window of `grid` is: the same steps from its own first cell. A
 * window never wraps, and a view across the antimeridian takes one that runs
 * past the grid's last column. */
export function cropGrid(grid: GeoGrid, column: number, row: number, width: number, height: number): GeoGrid {
  if (grid.projection) {
    const { x0, y0, dx, dy } = grid.projection;
    return { width, height, wraps: false, projection: { ...grid.projection, x0: x0 + column * dx, y0: y0 + row * dy } };
  }
  const window: RegularGrid = {
    width,
    height,
    firstLongitude: grid.firstLongitude + column * grid.longitudeStep,
    firstLatitude: grid.firstLatitude + row * grid.latitudeStep,
    longitudeStep: grid.longitudeStep,
    latitudeStep: grid.latitudeStep,
    wraps: false,
  };
  if (grid.gaussian) {
    window.gaussian = { ...grid.gaussian, firstRow: grid.gaussian.firstRow + row * grid.gaussian.rowStride };
  }
  return window;
}

/** Walk every cell of a grid with its centre. A row's latitude is taken once
 * where a row has one. */
export function forEachCell(grid: GeoGrid, visit: (cell: number, longitude: number, latitude: number) => void): void {
  const map = gridMapFor(grid);
  for (let row = 0; row < grid.height; row += 1) {
    if (grid.projection) {
      for (let column = 0; column < grid.width; column += 1) {
        const [longitude, latitude] = map.cellToLonLat(column, row);
        visit(row * grid.width + column, longitude, latitude);
      }
      continue;
    }
    const latitude = map.cellToLonLat(0, row)[1];
    for (let column = 0; column < grid.width; column += 1) {
      visit(row * grid.width + column, grid.firstLongitude + column * grid.longitudeStep, latitude);
    }
  }
}

/** The footprint a layer clips to: a model's `domain` applies to a grid the
 * encoder resampled, whose corners hold an extension of the edge. A native
 * grid is its own footprint. */
export function clipDomain(grid: GeoGrid, domain: LambertDomain | null | undefined): LambertDomain | null {
  return grid.projection ? null : (domain ?? null);
}
