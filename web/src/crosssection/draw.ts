/**
 * The section's plot: what a slice looks like on a canvas that is nothing but
 * the plot. The axes, the crosshair and the readings are the panel's.
 *
 * The field is opaque and the same in both themes, so the inks over it are
 * too; only the ground follows the theme. What is not drawn is left clear, and
 * the panel's own paper shows.
 */

import { traceContours, type Polyline } from "../isolines";
import { drawBarb } from "../sounding/skewt";
import { pointX, pressureY, P_BOTTOM } from "./chart";
import { smoothAlong, type SectionQuantity, type SectionSlice } from "./derive";
import type { SectionBands } from "./ramps";
import type { SectionDrawState } from "./types";

type Context = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;

/** The canvas, CSS pixels and device pixels to one. */
export interface PlotSize {
  width: number;
  height: number;
  dpr: number;
}

const INK = "#1b1a17";
const THETA_INK = "rgba(20, 20, 18, 0.75)";
const ISOTACH_INK = "#3b1f8f";
const FREEZING_INK = "#0a3fd0";
const HALO = "rgba(255, 255, 255, 0.85)";
const BARB_HALO = "rgba(255, 255, 255, 0.55)";
const LEVEL_DOTS = "rgba(0, 0, 0, 0.28)";
const GROUND = { light: { fill: "#8a8275", line: "#4d483f" }, dark: { fill: "#5b564d", line: "#b8b1a3" } };

const THETA_STEP = 5;

/** The surfaces barbs are drawn on, the ones given up last first. */
const BARB_PRIORITY = [850, 500, 700, 300, 200, 925, 400, 250, 600, 150, 1000, 100, 950, 800, 900, 650, 550, 450, 350];
const BARB_ROW_SPACING = 17;
/** How far a barb stands from the chart's edges to keep its shaft inside. */
const BARB_MARGIN = 10;

/** What drawing keeps from one frame to the next, so that a frame allocates
 * nothing the size of the plot or of the grid. */
export class DrawBuffers {
  image: ImageData | null = null;
  pixels = new Uint32Array(0);
  line = new Float32Array(0);
  smoothed = new Float32Array(0);
  column = new Int32Array(0);
  weight = new Float32Array(0);
  palette = new Uint32Array(0);
}

/** A little-endian RGBA pixel, opaque. */
function packed(r: number, g: number, b: number): number {
  return ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0;
}

/**
 * The stepped fill, a pixel at a time, into RGBA pixels: the grid is bilinear
 * between its points and the bands are cut from the interpolated value. A
 * pixel next to a missing value is left clear.
 */
export function paintFill(
  buffers: DrawBuffers,
  pixels: Uint32Array,
  width: number,
  height: number,
  values: Float32Array,
  points: number,
  rows: number,
  bands: SectionBands,
): void {
  const count = bands.rgb.length / 3;
  if (buffers.palette.length !== count) buffers.palette = new Uint32Array(count);
  if (buffers.column.length !== width) {
    buffers.column = new Int32Array(width);
    buffers.weight = new Float32Array(width);
  }
  if (buffers.line.length !== points) buffers.line = new Float32Array(points);
  const { palette, column, weight, line } = buffers;
  for (let band = 0; band < count; band += 1) {
    palette[band] = packed(bands.rgb[band * 3]!, bands.rgb[band * 3 + 1]!, bands.rgb[band * 3 + 2]!);
  }
  for (let x = 0; x < width; x += 1) {
    const at = ((x + 0.5) / width) * (points - 1);
    column[x] = Math.min(points - 2, at | 0);
    weight[x] = at - column[x]!;
  }
  const { min } = bands;
  const perStep = 1 / bands.step;
  const last = count - 1;
  for (let y = 0, out = 0; y < height; y += 1) {
    const at = (1 - (y + 0.5) / height) * (rows - 1);
    const row = Math.min(rows - 2, at | 0);
    const share = at - row;
    const base = row * points;
    for (let point = 0; point < points; point += 1) {
      const low = values[base + point]!;
      line[point] = low + share * (values[base + points + point]! - low);
    }
    for (let x = 0; x < width; x += 1, out += 1) {
      const left = column[x]!;
      const a = line[left]!;
      const band = (a + weight[x]! * (line[left + 1]! - a) - min) * perStep;
      pixels[out] = band !== band ? 0 : palette[band <= 0 ? 0 : band >= last ? last : band | 0]!;
    }
  }
}

interface Label {
  x: number;
  y: number;
  text: string;
  color: string;
}

interface Frame {
  context: Context;
  size: PlotSize;
  slice: SectionSlice;
  state: SectionDrawState;
  buffers: DrawBuffers;
  labels: Label[];
}

/** The place along a contour for its label: where it is flattest, off the
 * edges and the ground, and away from the labels there are. */
function labelPlace(frame: Frame, xs: Float64Array, ys: Float64Array, taken: readonly Label[]): { x: number; y: number } | null {
  const { size, slice, state } = frame;
  let best: { x: number; y: number } | null = null;
  let flattest = Number.POSITIVE_INFINITY;
  for (let at = 8; at < xs.length - 8; at += 3) {
    const x = xs[at]!;
    const y = ys[at]!;
    if (x < 30 || x > size.width - 30 || y < 12) continue;
    const point = Math.round((x / size.width) * (slice.points - 1));
    const ps = slice.ps[point]!;
    if (y > size.height - 10 || (ps === ps && y > pressureY(Math.min(ps, P_BOTTOM), state.top, size.height) - 12)) continue;
    const slope = Math.abs(ys[at + 5]! - ys[at - 5]!) / (Math.abs(xs[at + 5]! - xs[at - 5]!) + 1);
    if (slope >= flattest) continue;
    if (taken.some((label) => Math.hypot(label.x - x, label.y - y) < 46)) continue;
    best = { x, y };
    flattest = slope;
  }
  return best;
}

function contours(
  frame: Frame,
  quantity: SectionQuantity,
  interval: number,
  first: number,
  style: { color: string; width: (level: number) => number; text: (level: number) => string; dash?: number[]; scale?: number },
): void {
  const { context, size, slice, buffers } = frame;
  const { points, rows } = slice;
  if (buffers.smoothed.length !== points * rows) buffers.smoothed = new Float32Array(points * rows);
  smoothAlong(slice.quantity(quantity), points, buffers.smoothed);
  if (style.scale !== undefined) {
    const { smoothed } = buffers;
    for (let at = 0; at < smoothed.length; at += 1) smoothed[at]! *= style.scale;
  }
  const lines: Polyline[] = traceContours(
    { columns: points, rows, values: buffers.smoothed, column0: 0, row0: 0, stride: 1, wraps: false },
    interval,
  );
  context.strokeStyle = style.color;
  context.lineJoin = "round";
  context.setLineDash(style.dash ?? []);
  for (const line of lines) {
    const count = line.points.length / 2;
    if (line.level < first || count < 6) continue;
    const xs = new Float64Array(count);
    const ys = new Float64Array(count);
    context.lineWidth = style.width(line.level);
    context.beginPath();
    for (let at = 0; at < count; at += 1) {
      const x = (xs[at] = pointX(line.points[at * 2]!, points, size.width));
      const y = (ys[at] = (1 - line.points[at * 2 + 1]! / (rows - 1)) * size.height);
      if (at === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    }
    if (line.closed) context.closePath();
    context.stroke();
    if (count <= 24) continue;
    const place = labelPlace(frame, xs, ys, frame.labels);
    if (place) frame.labels.push({ ...place, text: style.text(line.level), color: style.color });
  }
  context.setLineDash([]);
}

/** A line across the chart at a pressure, the dash pattern its own. */
function across(context: Context, size: PlotSize, y: number, dash: number[]): void {
  context.setLineDash(dash);
  context.beginPath();
  context.moveTo(0, y);
  context.lineTo(size.width, y);
  context.stroke();
  context.setLineDash([]);
}

/** The surfaces the map is on. One surface for both is the fill's. */
function mapLevels(frame: Frame): void {
  const { context, size, state } = frame;
  const { fill, lines } = state.mapLevels;
  const draw = (pressure: number | null, dash: number[]): void => {
    if (pressure === null || pressure < state.top || pressure > P_BOTTOM) return;
    const y = pressureY(pressure, state.top, size.height);
    context.strokeStyle = HALO;
    context.lineWidth = 3;
    across(context, size, y, []);
    context.strokeStyle = INK;
    context.lineWidth = 1.2;
    across(context, size, y, dash);
  };
  if (lines !== fill) draw(lines, [6, 5]);
  draw(fill, []);
}

function ground(frame: Frame): void {
  const { context, size, slice, state } = frame;
  const { points, ps } = slice;
  const ink = state.dark ? GROUND.dark : GROUND.light;
  const x = (point: number): number => pointX(point, points, size.width);
  const y = (point: number): number => pressureY(Math.min(Math.max(ps[point]!, state.top), P_BOTTOM), state.top, size.height);
  // The points whose ground is known, a run of them at a time.
  for (let from = 0; from < points; ) {
    if (ps[from] !== ps[from]) {
      from += 1;
      continue;
    }
    let to = from;
    while (to + 1 < points && ps[to + 1] === ps[to + 1]) to += 1;
    context.beginPath();
    for (let point = from; point <= to; point += 1) {
      if (point === from) context.moveTo(x(point), y(point));
      else context.lineTo(x(point), y(point));
    }
    context.strokeStyle = ink.line;
    context.lineWidth = 1.5;
    context.lineJoin = "round";
    context.stroke();
    context.lineTo(x(to), size.height + 2);
    context.lineTo(x(from), size.height + 2);
    context.closePath();
    context.fillStyle = ink.fill;
    context.fill();
    from = to + 1;
  }
}

/** The levels of a store that carry barbs on a chart, no two of them closer
 * than a barb can be read. */
export function barbLevels(levels: readonly number[], top: number, height: number): number[] {
  const kept: number[] = [];
  for (const pressure of BARB_PRIORITY) {
    if (!levels.includes(pressure) || pressure <= top) continue;
    const y = pressureY(pressure, top, height);
    if (y < BARB_MARGIN || y > height - 4) continue;
    if (kept.every((other) => Math.abs(pressureY(other, top, height) - y) >= BARB_ROW_SPACING)) kept.push(pressure);
  }
  return kept;
}

function barbs(frame: Frame, levels: readonly number[]): void {
  const { context, size, slice, state } = frame;
  const { points, rows } = slice;
  const u = slice.quantity("u");
  const v = slice.quantity("v");
  const tall = size.height >= 320;
  const scale = tall ? 1.5 : 1.25;
  const each = Math.max(1, Math.round(((tall ? 62 : 54) / size.width) * (points - 1)));
  const shown = barbLevels(levels, state.top, size.height);
  context.lineCap = "round";
  for (let point = Math.round(each / 2); point < points; point += each) {
    const x = pointX(point, points, size.width);
    for (const pressure of shown) {
      if (pressure > slice.ps[point]! - 8) continue;
      const y = pressureY(pressure, state.top, size.height);
      const at = Math.round((1 - y / size.height) * (rows - 1)) * points + point;
      const east = u[at]!;
      const north = v[at]!;
      const speed = Math.hypot(east, north);
      if (speed !== speed) continue;
      const side = slice.latitudes[point]! < 0 ? -1 : 1;
      const from = (Math.atan2(-east, -north) * 180) / Math.PI;
      context.lineWidth = 3;
      context.strokeStyle = context.fillStyle = BARB_HALO;
      drawBarb(context, x, y, from, speed, scale, side);
      context.lineWidth = 1.1;
      context.strokeStyle = context.fillStyle = INK;
      drawBarb(context, x, y, from, speed, scale, side);
    }
  }
  context.lineCap = "butt";
}

/** What of a state can be drawn from the slice's groups. */
export interface DrawnParts {
  fill: boolean;
  wind: boolean;
  temperature: boolean;
}

/** One frame. The canvas has its size and is clear. */
export function drawSection(
  context: Context,
  size: PlotSize,
  slice: SectionSlice,
  state: SectionDrawState,
  bands: SectionBands,
  levels: readonly number[],
  parts: DrawnParts,
  buffers: DrawBuffers,
): void {
  const { points, rows } = slice;
  if (points < 2) return;
  const frame: Frame = { context, size, slice, state, buffers, labels: [] };
  const width = Math.round(size.width * size.dpr);
  const height = Math.round(size.height * size.dpr);
  if (parts.fill) {
    if (!buffers.image || buffers.image.width !== width || buffers.image.height !== height) {
      buffers.image = context.createImageData(width, height);
      buffers.pixels = new Uint32Array(buffers.image.data.buffer);
    }
    const quantity: SectionQuantity = state.fill === "tmp" ? "t" : state.fill;
    paintFill(buffers, buffers.pixels, width, height, slice.quantity(quantity), points, rows, bands);
    context.putImageData(buffers.image, 0, 0);
  }
  context.setTransform(size.dpr, 0, 0, size.dpr, 0, 0);
  if (parts.wind && state.lines.wind) {
    const { scale, interval, first, heavy } = state.units.wind;
    contours(frame, "wind", interval, first, {
      color: ISOTACH_INK,
      width: (level) => (level >= heavy ? 1.9 : 1.2),
      text: String,
      scale,
    });
  }
  if (parts.temperature && state.lines.theta) {
    contours(frame, "theta", THETA_STEP, 0, {
      color: THETA_INK,
      width: (level) => (level % 20 === 0 ? 1.4 : 0.7),
      text: String,
    });
  }
  if (parts.temperature && state.lines.zero) {
    // An interval no temperature reaches twice leaves the one line at zero.
    contours(frame, "t", 1000, 0, { color: FREEZING_INK, width: () => 2.4, text: () => state.units.freezing, dash: [9, 4] });
  }
  context.strokeStyle = LEVEL_DOTS;
  context.lineWidth = 1;
  for (const pressure of levels) {
    if (pressure > state.top) across(context, size, pressureY(pressure, state.top, size.height), [1.5, 7.5]);
  }
  mapLevels(frame);
  ground(frame);
  if (parts.wind && state.barbs) barbs(frame, levels);
  context.font = "600 11px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.lineJoin = "round";
  for (const label of frame.labels) {
    context.lineWidth = 3;
    context.strokeStyle = HALO;
    context.strokeText(label.text, label.x, label.y);
    context.fillStyle = label.color;
    context.fillText(label.text, label.x, label.y);
  }
}
