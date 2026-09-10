/**
 * Contour labels for the pressure family: where a line is, so a value can be
 * written on it, and where a high or a low is, so it can be marked.
 *
 * The lines themselves are drawn on the GPU straight from the plane
 * (`layer.ts`), and nothing here changes that. What a label needs is
 * *geometry* — a polyline to lay text along, a point to put an "H" on — and
 * geometry is a CPU product: reading it back from the GPU would stall the
 * render pipeline on a sync, which is worse for the frame rate than doing
 * the arithmetic elsewhere. So this module is pure functions over a decoded
 * plane, run in `labels.worker.ts` off the main thread, and its output is a
 * small list of polylines and points that MapLibre's own symbol layers place
 * the way they place the basemap's road names.
 *
 * The pipeline is: reduce the plane to the cells in view (block means over
 * `stride` cells, masked by the coverage a partial plane actually holds),
 * smooth it with the same Gaussian the shader uses so the traced lines sit
 * on the drawn ones, run marching squares on the interval family, join the
 * segments into polylines, and search the field for windowed extrema. The
 * reduction is what keeps the cost flat: the stride is chosen so a whole
 * world and a zoomed-in coast both come to about the same number of cells.
 */

import type { GeoGrid } from "./probe";
import { wrap } from "./probe";
import type { CoverageBox } from "./tiles";

/** A rectangle of full-resolution cells. `column0 + columns` may exceed the
 * grid width on a wrapping grid; columns are then taken modulo it. */
export interface CellWindow {
  column0: number;
  row0: number;
  columns: number;
  rows: number;
}

/** A reduced field: `columns` x `rows` block means in code units, row-major,
 * NaN where the block held no covered cell. */
export interface Field {
  columns: number;
  rows: number;
  values: Float32Array;
  /** Where block (0, 0) starts on the full grid, and how many cells one
   * block spans on each axis. */
  column0: number;
  row0: number;
  stride: number;
  /** The blocks cover the grid's full width and it wraps, so the field's
   * own columns are periodic. */
  wraps: boolean;
}

/** One traced contour, in field coordinates (fractional block indices). */
export interface Polyline {
  level: number;
  /** [x0, y0, x1, y1, ...] */
  points: number[];
  closed: boolean;
}

export interface Center {
  kind: "high" | "low";
  /** Field coordinates of the block. */
  x: number;
  y: number;
  /** The block's mean code. */
  code: number;
}

/** The stride that brings a window of cells down to about `budget` blocks:
 * a whole quarter-degree world reduces by three, a regional view not at all. */
export function strideFor(cells: number, budget = 120_000): number {
  return Math.max(1, Math.ceil(Math.sqrt(cells / budget)));
}

/** Cells in the coverage box, as a predicate on columns and one on rows: a
 * box is separable, so the mask is two arrays rather than one per cell. */
function coverageMasks(grid: GeoGrid, coverage: CoverageBox): { column: Uint8Array; row: Uint8Array } {
  const column = new Uint8Array(grid.width);
  const row = new Uint8Array(grid.height);
  // Same test as the shaders: a cell's center, in texture space.
  for (let c = 0; c < grid.width; c += 1) {
    const u = (c + 0.5) / grid.width;
    column[c] =
      coverage.uStart <= coverage.uEnd
        ? u >= coverage.uStart && u <= coverage.uEnd
          ? 1
          : 0
        : u >= coverage.uStart || u <= coverage.uEnd
          ? 1
          : 0;
  }
  for (let r = 0; r < grid.height; r += 1) {
    const v = (r + 0.5) / grid.height;
    row[r] = v >= coverage.vStart && v <= coverage.vEnd ? 1 : 0;
  }
  return { column, row };
}

/** Block means of the plane over the window, `stride` cells to a block.
 * Cells outside the grid (a cropped grid's edge, the rows past the poles)
 * or outside the coverage box are left out of the mean; a block with none
 * is NaN. */
export function reduceField(
  plane: Uint8Array,
  grid: GeoGrid,
  window: CellWindow,
  coverage: CoverageBox,
  stride: number,
): Field {
  const columns = Math.ceil(window.columns / stride);
  const rows = Math.ceil(window.rows / stride);
  const values = new Float32Array(columns * rows);
  const masks = coverageMasks(grid, coverage);
  const wraps = grid.wraps && window.columns >= grid.width;
  for (let by = 0; by < rows; by += 1) {
    for (let bx = 0; bx < columns; bx += 1) {
      let sum = 0;
      let count = 0;
      for (let dy = 0; dy < stride; dy += 1) {
        const r = window.row0 + by * stride + dy;
        if (r < 0 || r >= grid.height || !masks.row[r]) continue;
        for (let dx = 0; dx < stride; dx += 1) {
          let c = window.column0 + bx * stride + dx;
          if (grid.wraps) c = wrap(c, grid.width);
          else if (c < 0 || c >= grid.width) continue;
          if (!masks.column[c]) continue;
          sum += plane[r * grid.width + c]!;
          count += 1;
        }
      }
      values[by * columns + bx] = count > 0 ? sum / count : Number.NaN;
    }
  }
  return { columns, rows, values, column0: window.column0, row0: window.row0, stride, wraps };
}

/** Separable Gaussian over the field, in place. NaN blocks contribute
 * nothing and stay NaN; the kernel is renormalised by what it lands on, the
 * same one-sided mean the shader's pass produces at an edge. */
export function smoothField(field: Field, sigma: number): void {
  if (sigma <= 0) return;
  const radius = Math.max(1, Math.ceil(3 * sigma));
  const weights = new Float64Array(radius + 1);
  for (let offset = 0; offset <= radius; offset += 1) {
    weights[offset] = Math.exp(-(offset * offset) / (2 * sigma * sigma));
  }
  const { columns, rows, values } = field;
  const scratch = new Float32Array(values.length);
  const pass = (source: Float32Array, target: Float32Array, dx: number, dy: number) => {
    for (let y = 0; y < rows; y += 1) {
      for (let x = 0; x < columns; x += 1) {
        const index = y * columns + x;
        if (Number.isNaN(source[index]!)) {
          target[index] = Number.NaN;
          continue;
        }
        let sum = 0;
        let weight = 0;
        for (let offset = -radius; offset <= radius; offset += 1) {
          let sx = x + offset * dx;
          const sy = y + offset * dy;
          if (sy < 0 || sy >= rows) continue;
          if (sx < 0 || sx >= columns) {
            if (!field.wraps || dx === 0) continue;
            sx = wrap(sx, columns);
          }
          const value = source[sy * columns + sx]!;
          if (Number.isNaN(value)) continue;
          const w = weights[Math.abs(offset)]!;
          sum += w * value;
          weight += w;
        }
        target[index] = weight > 0 ? sum / weight : Number.NaN;
      }
    }
  };
  pass(values, scratch, 1, 0);
  pass(scratch, values, 0, 1);
}

/** Where the segment from `a` (at 0) to `b` (at 1) crosses `level`. */
function crossing(a: number, b: number, level: number): number {
  return (level - a) / (b - a);
}

/**
 * Marching squares over the field for every multiple of `interval` (offset
 * by `phase`, which the pressure family leaves at zero), with the segments
 * joined into polylines.
 *
 * Levels are enumerated per cell from its corner range, so the cost follows
 * the crossings rather than the number of levels. Each segment end lies on a
 * cell edge, and an edge is shared by exactly two cells, so joining is a
 * walk over a map from edge to segment: no tolerance, no distance search.
 * The saddle (two opposite corners above, two below) is split by the cell's
 * mean, the usual choice.
 */
export function traceContours(field: Field, interval: number, phase = 0): Polyline[] {
  const { columns, rows, values } = field;
  const result: Polyline[] = [];
  if (interval <= 0 || columns < 2 || rows < 2) return result;
  const at = (x: number, y: number) => values[y * columns + x]!;
  // A wrapping field closes its last column onto its first.
  const cellColumns = field.wraps ? columns : columns - 1;
  const right = (x: number) => (x + 1) % columns;

  // Edge keys. A horizontal edge runs from (x, y) to (x + 1, y); a vertical
  // one from (x, y) to (x, y + 1). Interleaving them into one integer keeps
  // the map keys cheap.
  const horizontal = (x: number, y: number) => ((y * columns + x) << 1) | 0;
  const vertical = (x: number, y: number) => ((y * columns + x) << 1) | 1;

  interface Segment {
    edgeA: number;
    ax: number;
    ay: number;
    edgeB: number;
    bx: number;
    by: number;
  }
  const byLevel = new Map<number, Segment[]>();
  const push = (level: number, segment: Segment) => {
    let list = byLevel.get(level);
    if (!list) byLevel.set(level, (list = []));
    list.push(segment);
  };

  for (let y = 0; y < rows - 1; y += 1) {
    for (let x = 0; x < cellColumns; x += 1) {
      const xr = right(x);
      const v00 = at(x, y);
      const v10 = at(xr, y);
      const v01 = at(x, y + 1);
      const v11 = at(xr, y + 1);
      if (Number.isNaN(v00) || Number.isNaN(v10) || Number.isNaN(v01) || Number.isNaN(v11)) continue;
      const low = Math.min(v00, v10, v01, v11);
      const high = Math.max(v00, v10, v01, v11);
      const first = Math.ceil((low - phase) / interval);
      const last = Math.floor((high - phase) / interval);
      for (let k = first; k <= last; k += 1) {
        const level = k * interval + phase;
        // A corner exactly on the level counts as above: the codebooks put
        // every contour half a code off, so this never decides anything
        // real, but it keeps the case table total.
        const index = (v00 >= level ? 1 : 0) | (v10 >= level ? 2 : 0) | (v11 >= level ? 4 : 0) | (v01 >= level ? 8 : 0);
        if (index === 0 || index === 15) continue;
        // The four edges' crossing points, computed as needed.
        const top = () => [horizontal(x, y), x + crossing(v00, v10, level), y] as const;
        const bottom = () => [horizontal(x, y + 1), x + crossing(v01, v11, level), y + 1] as const;
        const left = () => [vertical(x, y), x, y + crossing(v00, v01, level)] as const;
        const rightEdge = () => [vertical(xr, y), x + 1, y + crossing(v10, v11, level)] as const;
        const join = (a: readonly [number, number, number], b: readonly [number, number, number]) =>
          push(level, { edgeA: a[0], ax: a[1], ay: a[2], edgeB: b[0], bx: b[1], by: b[2] });
        switch (index) {
          case 1:
          case 14:
            join(left(), top());
            break;
          case 2:
          case 13:
            join(top(), rightEdge());
            break;
          case 3:
          case 12:
            join(left(), rightEdge());
            break;
          case 4:
          case 11:
            join(rightEdge(), bottom());
            break;
          case 6:
          case 9:
            join(top(), bottom());
            break;
          case 7:
          case 8:
            join(left(), bottom());
            break;
          case 5:
          case 10: {
            const centerAbove = (v00 + v10 + v01 + v11) / 4 >= level;
            // 5: corners 00 and 11 above. 10: corners 10 and 01 above.
            if ((index === 5) === centerAbove) {
              join(left(), bottom());
              join(top(), rightEdge());
            } else {
              join(left(), top());
              join(rightEdge(), bottom());
            }
            break;
          }
          default:
            break;
        }
      }
    }
  }

  for (const [level, segments] of byLevel) {
    // Every edge holds at most two segment ends per level (one per cell on
    // either side of it); the walk follows them.
    const ends = new Map<number, number[]>();
    const add = (edge: number, segment: number) => {
      const list = ends.get(edge);
      if (list) list.push(segment);
      else ends.set(edge, [segment]);
    };
    segments.forEach((segment, index) => {
      add(segment.edgeA, index);
      add(segment.edgeB, index);
    });
    const used = new Uint8Array(segments.length);
    // Extend from one end of a segment as far as the chain goes, appending
    // points; returns whether it came back to the start.
    const walk = (start: number, fromEdge: number, points: number[]): boolean => {
      let edge = fromEdge;
      let closed = false;
      for (;;) {
        const candidates = ends.get(edge);
        const nextIndex = candidates?.find((candidate) => !used[candidate]);
        if (nextIndex === undefined) {
          if (candidates?.includes(start) && points.length > 4) closed = true;
          break;
        }
        used[nextIndex] = 1;
        const next = segments[nextIndex]!;
        if (next.edgeA === edge) {
          points.push(next.bx, next.by);
          edge = next.edgeB;
        } else {
          points.push(next.ax, next.ay);
          edge = next.edgeA;
        }
      }
      return closed;
    };
    for (let index = 0; index < segments.length; index += 1) {
      if (used[index]) continue;
      used[index] = 1;
      const seed = segments[index]!;
      const forward: number[] = [seed.ax, seed.ay, seed.bx, seed.by];
      const closed = walk(index, seed.edgeB, forward);
      let points = forward;
      if (!closed) {
        const backward: number[] = [seed.bx, seed.by, seed.ax, seed.ay];
        walk(index, seed.edgeA, backward);
        // The backward walk lists the points from the seed outward; reverse
        // it and drop its copy of the seed to prepend.
        const head: number[] = [];
        for (let position = backward.length - 2; position >= 4; position -= 2) {
          head.push(backward[position]!, backward[position + 1]!);
        }
        points = head.concat(forward);
      }
      result.push({ level, points, closed });
    }
  }
  return result;
}

/**
 * Windowed extrema: a block whose value tops (or bottoms) every block within
 * `radius`, by at least `prominence` over the mean of the window's rim. The
 * rim test is what separates a closed high from a bump on a slope, and the
 * window is what stops one broad high from being marked at every block of
 * its plateau.
 */
export function findCenters(field: Field, radius: number, prominence: number): Center[] {
  const { columns, rows, values } = field;
  const centers: Center[] = [];
  if (radius < 1) return centers;
  const at = (x: number, y: number): number => {
    if (y < 0 || y >= rows) return Number.NaN;
    if (x < 0 || x >= columns) {
      if (!field.wraps) return Number.NaN;
      x = wrap(x, columns);
    }
    return values[y * columns + x]!;
  };
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < columns; x += 1) {
      const value = at(x, y);
      if (Number.isNaN(value)) continue;
      let isHigh = true;
      let isLow = true;
      let rimSum = 0;
      let rimCount = 0;
      for (let dy = -radius; dy <= radius && (isHigh || isLow); dy += 1) {
        for (let dx = -radius; dx <= radius; dx += 1) {
          if (dx === 0 && dy === 0) continue;
          const other = at(x + dx, y + dy);
          if (Number.isNaN(other)) {
            // An unknown neighbour could be the true center; do not mark
            // this one on a window it cannot see.
            isHigh = false;
            isLow = false;
            break;
          }
          if (other > value || (other === value && (dy < 0 || (dy === 0 && dx < 0)))) isHigh = false;
          if (other < value || (other === value && (dy < 0 || (dy === 0 && dx < 0)))) isLow = false;
          if (Math.abs(dx) === radius || Math.abs(dy) === radius) {
            rimSum += other;
            rimCount += 1;
          }
        }
      }
      if (!isHigh && !isLow) continue;
      const rim = rimSum / rimCount;
      if (isHigh && value - rim >= prominence) centers.push({ kind: "high", x, y, code: value });
      if (isLow && rim - value >= prominence) centers.push({ kind: "low", x, y, code: value });
    }
  }
  return centers;
}

/** Longitude and latitude of a point in field coordinates. */
export function fieldToGeo(field: Field, grid: GeoGrid, x: number, y: number): [number, number] {
  // Block b spans cells [b * stride, (b + 1) * stride); its center is the
  // middle of that span, and a cell's center is where its value sits.
  const column = field.column0 + x * field.stride + (field.stride - 1) / 2;
  const row = field.row0 + y * field.stride + (field.stride - 1) / 2;
  return [grid.firstLongitude + column * grid.longitudeStep, grid.firstLatitude + row * grid.latitudeStep];
}

/** What the worker is asked for, and everything it needs to answer. */
export interface LabelRequest {
  grid: GeoGrid;
  window: CellWindow;
  coverage: CoverageBox;
  stride: number;
  /** Standard deviation of the smoothing in full-resolution cells, matched
   * to the shader's. */
  smoothingCells: number;
  /** Dequantization, `value = offset + code * scale`. */
  offset: number;
  scale: number;
  /** Contour interval in the variable's unit. */
  interval: number;
  /** Search radius for a high or low, in degrees. */
  centerRadiusDegrees: number;
  /** How far a center has to stand above (below) its surroundings. */
  prominence: number;
}

export interface LabelLine {
  value: number;
  /** [longitude, latitude] pairs. */
  coordinates: [number, number][];
}

export interface LabelCenter {
  kind: "high" | "low";
  longitude: number;
  latitude: number;
  value: number;
}

export interface LabelResult {
  lines: LabelLine[];
  centers: LabelCenter[];
}

/** The whole job, from a plane to geometry in degrees. */
export function computeLabels(plane: Uint8Array, request: LabelRequest): LabelResult {
  const { grid, stride } = request;
  const field = reduceField(plane, grid, request.window, request.coverage, stride);
  smoothField(field, request.smoothingCells / stride);
  // Contours are found on codes, so the interval is expressed in codes; the
  // codebook puts every contour on an exact code offset, which is `phase`.
  const codeInterval = request.interval / request.scale;
  const codePhase = wrap(-request.offset / request.scale, codeInterval);
  const lines: LabelLine[] = [];
  for (const polyline of traceContours(field, codeInterval, codePhase)) {
    const coordinates: [number, number][] = [];
    for (let position = 0; position < polyline.points.length; position += 2) {
      coordinates.push(fieldToGeo(field, grid, polyline.points[position]!, polyline.points[position + 1]!));
    }
    if (coordinates.length < 2) continue;
    lines.push({ value: Math.round(request.offset + polyline.level * request.scale), coordinates });
  }
  const cellDegrees = Math.abs(grid.latitudeStep) * stride;
  const radius = Math.max(1, Math.round(request.centerRadiusDegrees / cellDegrees));
  const centers: LabelCenter[] = [];
  for (const center of findCenters(field, radius, request.prominence / request.scale)) {
    const [longitude, latitude] = fieldToGeo(field, grid, center.x, center.y);
    // The polar plateau of a reduced-to-sea-level field is not a weather
    // system, and the map never shows it anyway.
    if (Math.abs(latitude) > 80) continue;
    centers.push({
      kind: center.kind,
      longitude,
      latitude,
      value: Math.round(request.offset + center.code * request.scale),
    });
  }
  return { lines, centers };
}
