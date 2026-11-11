import type { ExpressionSpecification, GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";
import type { Feature, FeatureCollection, LineString, Point } from "geojson";

import { BNG_ENVELOPE, BNG_MAX_EASTING, BNG_MAX_NORTHING, bngMajorSquare, bngSquareId, fromBng, toBng } from "./bng";
import { toGars, type GarsPrecision } from "./gars";
import { firstSymbolLayerId } from "./marks";
import {
  centralMeridian,
  fromUps,
  fromUtm,
  gzdCells,
  type GzdCell,
  type LatLon,
  toUps,
  toUtm,
  upsSquareId,
  utmSquareId,
  utmZone,
  UTM_NORTH_LIMIT,
  UTM_SOUTH_LIMIT,
  wrapLongitude,
} from "./mgrs";
import { anyGrid, NO_GRID, type GridKind, type GridState } from "./urlstate";

/**
 * The coordinate grids over the map: the MGRS grid as the GSI's UTM grid
 * draws it, the same lines numbered as UTM, GARS, the British National
 * Grid, and parallels and meridians. All are GeoJSON recomputed for the
 * viewport on every `moveend`, so they follow the plane and the globe
 * alike and cost nothing while they are off.
 *
 * MGRS draws by zoom: the grid zone boundaries (6° × 8°, the Norway and
 * Svalbard exceptions and the UPS caps) always, with their names from z3;
 * the 100 km squares from z5 and their letters from z6 (the zone's name in
 * front from z7, where the zone names give way); the 10 km lines from z8 and the 1 km lines from z11, numbered
 * in kilometres along the bottom and left edges of the view, as a survey
 * sheet prints its margin. A UTM line is straight in its zone's projection,
 * so each is sampled there, inverted to degrees and cut to its cell; the
 * caps' lines likewise in the polar stereographic. A level whose line count
 * would pass `MAX_LINES` for the viewport is skipped with every finer one.
 *
 * UTM draws the same lines (once, when MGRS is on too) and names them as
 * UTM does: the zone and hemisphere (`54N`) in each zone, and the edge
 * numbers as whole kilometre eastings and northings (`385`, `3950`), on
 * the top and right edges when MGRS's numbers hold the bottom and left.
 *
 * GARS is a lat/lon grid: its 30′ cells from z5 and their names from z6,
 * the 15′ quadrants and their numbers from z7, the 5′ keypads from z9,
 * each keypad named by quadrant and key (`39`).
 *
 * The British National Grid draws only over its own 700 km × 1 300 km box:
 * the box and its 500 km squares always, with their letters from z3; the
 * 100 km squares from z5 and their letters from z6; the 10 km and 1 km
 * lines at MGRS's zooms, numbered along the bottom and left edges as MGRS
 * numbers its own. Its lines are straight in the National Grid's
 * projection on OSGB36 and are sampled there.
 *
 * Everything sits under the basemap's first symbol layer, as the marks do:
 * above every field and line slot (those attach under the coast), under
 * the place labels.
 */

export const GRID_SOURCE = "coordinate-grid";
export const GRID_CASING_LAYER = "coordinate-grid-casing";
export const GRID_LINE_LAYER = "coordinate-grid-lines";
export const GRID_LABEL_LAYER = "coordinate-grid-labels";

/** The zooms each MGRS (and UTM and National Grid) level appears from. */
export const GZD_LABEL_MIN_ZOOM = 3;
export const SQUARE_MIN_ZOOM = 5;
export const SQUARE_LABEL_MIN_ZOOM = 6;
export const SQUARE_PREFIX_MIN_ZOOM = 7;
export const TEN_KM_MIN_ZOOM = 8;
export const ONE_KM_MIN_ZOOM = 11;

/** The zooms each GARS level appears from. */
export const GARS_MIN_ZOOM = 5;
/** A 30′ cell is ~22 px wide at z5, too narrow for its five characters. */
export const GARS_LABEL_MIN_ZOOM = 6;
export const GARS_QUADRANT_MIN_ZOOM = 7;
export const GARS_KEYPAD_MIN_ZOOM = 9;

const MAX_LINES = 2500;
const MAX_LABELS = 3000;
/** Below this the whole world is drawn: a far-out globe's bounds are not a
 * box worth clipping to. */
const WORLD_ZOOM = 3;
/** The narrowest spacing in pixels the parallels and meridians draw at. */
const GRATICULE_MIN_SPACING_PX = 100;
const GRATICULE_STEPS = [30, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01];
const EDGE_SAMPLE_PX = 8;

/** Line rank: 0 grid zone (or the National Grid's box and 500 km
 * squares), 1 100 km, 2 10 km, 3 1 km; GARS 1 30′, 2 15′, 3 5′;
 * parallels and meridians are their own kind. */
type Rank = 0 | 1 | 2 | 3;
type Kind = GridKind;
/** Where a label sits on its point; `below` is centred under it, clear
 * of a `center` label on the same spot. */
type Anchor = "center" | "below" | "bottom" | "left" | "top" | "right";

interface LineProps {
  kind: Kind;
  rank: Rank;
}
interface LabelProps extends LineProps {
  label: string;
  anchor: Anchor;
}

export type GridFeature = Feature<LineString, LineProps> | Feature<Point, LabelProps>;

/** What a grid is computed for: the zoom, the visible box (longitude
 * ranges in [-180, 180], split at the antimeridian) and the ground under
 * each edge of the view, sampled left to right and top to bottom. */
export interface GridView {
  zoom: number;
  ranges: readonly (readonly [number, number])[];
  south: number;
  north: number;
  edges: { top: LatLon[]; right: LatLon[]; bottom: LatLon[]; left: LatLon[] };
}

interface Rect {
  west: number;
  east: number;
  south: number;
  north: number;
}

/** A plane a cell's lines are straight in. */
interface Frame {
  forward(lat: number, lon: number): [number, number];
  inverse(x: number, y: number): LatLon;
}

function utmFrame(zone: number, north: boolean): Frame {
  return {
    forward: (lat, lon) => {
      const utm = toUtm(lat, lon, zone, north);
      return [utm.easting, utm.northing];
    },
    inverse: (x, y) => fromUtm(zone, north, x, y),
  };
}

function upsFrame(north: boolean): Frame {
  return {
    forward: (lat, lon) => {
      const ups = toUps(lat, lon, north);
      return [ups.easting, ups.northing];
    },
    inverse: (x, y) => fromUps(north, x, y),
  };
}

/** The National Grid's plane, through the Helmert shift to OSGB36. */
const BNG_FRAME: Frame = {
  forward: (lat, lon) => {
    const point = toBng(lat, lon);
    return [point.easting, point.northing];
  },
  inverse: (x, y) => fromBng(x, y),
};

const EPS = 1e-9;

/** The point's longitude as the rect counts it (180 for -180 on an east
 * edge), or `null` outside. */
function inside(rect: Rect, point: LatLon): number | null {
  if (!Number.isFinite(point.lat) || point.lat < rect.south - EPS || point.lat > rect.north + EPS) return null;
  let lon = point.lon;
  if (lon < rect.west - EPS && lon + 360 <= rect.east + EPS) lon += 360;
  return lon >= rect.west - EPS && lon <= rect.east + EPS ? lon : null;
}

function intersect(cell: Rect, view: GridView): Rect[] {
  const out: Rect[] = [];
  const south = Math.max(cell.south, view.south);
  const north = Math.min(cell.north, view.north);
  if (south >= north) return out;
  for (const [west, east] of view.ranges) {
    const w = Math.max(cell.west, west);
    const e = Math.min(cell.east, east);
    if (w < e) out.push({ west: w, east: e, south, north });
  }
  return out;
}

function frameOf(cell: GzdCell): Frame {
  return cell.zone === 0 ? upsFrame(cell.north > 0) : utmFrame(cell.zone, cell.south >= 0);
}

/** The projected box a rect covers: its edges sampled, the central meridian
 * (where a parallel's northing turns) included. */
function extent(frame: Frame, rect: Rect, steps: number, centralLon: number | null): [number, number, number, number] {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const add = (lat: number, lon: number) => {
    const [x, y] = frame.forward(lat, lon);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  };
  for (let i = 0; i <= steps; i += 1) {
    const lon = rect.west + ((rect.east - rect.west) * i) / steps;
    const lat = rect.south + ((rect.north - rect.south) * i) / steps;
    add(rect.south, lon);
    add(rect.north, lon);
    add(lat, rect.west);
    add(lat, rect.east);
  }
  if (centralLon !== null && centralLon > rect.west && centralLon < rect.east) {
    add(rect.south, centralLon);
    add(rect.north, centralLon);
  }
  return [minX, minY, maxX, maxY];
}

/** One straight frame line from `from` to `to` along the other axis, cut to
 * the rect, as [lon, lat] runs. Crossings are found by bisection, so a line
 * ends exactly on its cell's edge. */
function trace(frame: Frame, rect: Rect, vertical: boolean, value: number, from: number, to: number, segments: number): [number, number][][] {
  const at = (t: number) => (vertical ? frame.inverse(value, t) : frame.inverse(t, value));
  const runs: [number, number][][] = [];
  let run: [number, number][] | null = null;
  let previousT = from;
  let previousIn = false;
  for (let i = 0; i <= segments; i += 1) {
    const t = from + ((to - from) * i) / segments;
    const point = at(t);
    const lon = inside(rect, point);
    const isIn = lon !== null;
    if (i > 0 && isIn !== previousIn) {
      let a = previousT;
      let b = t;
      for (let step = 0; step < 24; step += 1) {
        const mid = (a + b) / 2;
        if ((inside(rect, at(mid)) !== null) === previousIn) a = mid;
        else b = mid;
      }
      const edge = at(previousIn ? a : b);
      const edgeLon = inside(rect, edge) ?? edge.lon;
      if (previousIn) {
        run?.push([edgeLon, edge.lat]);
        if (run && run.length >= 2) runs.push(run);
        run = null;
      } else {
        run = [[edgeLon, edge.lat]];
      }
    }
    if (lon !== null) (run ??= []).push([lon, point.lat]);
    previousT = t;
    previousIn = isIn;
  }
  if (run && run.length >= 2) runs.push(run);
  return runs;
}

function line(coordinates: [number, number][], kind: Kind, rank: Rank): Feature<LineString, LineProps> {
  return { type: "Feature", geometry: { type: "LineString", coordinates }, properties: { kind, rank } };
}

function label(lon: number, lat: number, text: string, kind: Kind, rank: Rank, anchor: Anchor): Feature<Point, LabelProps> {
  return { type: "Feature", geometry: { type: "Point", coordinates: [wrapLongitude(lon), lat] }, properties: { kind, rank, label: text, anchor } };
}

/** A run of points from a to b at most `step` degrees apart, so a long
 * parallel or meridian bends with the globe. */
function densify(a: [number, number], b: [number, number], step: number): [number, number][] {
  const count = Math.max(1, Math.ceil(Math.max(Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1])) / step));
  const out: [number, number][] = [];
  for (let i = 0; i <= count; i += 1) out.push([a[0] + ((b[0] - a[0]) * i) / count, a[1] + ((b[1] - a[1]) * i) / count]);
  return out;
}

const BAND_EDGES = [-80, -72, -64, -56, -48, -40, -32, -24, -16, -8, 0, 8, 16, 24, 32, 40, 48, 56, 64, 72, UTM_NORTH_LIMIT];

function zoneBoundaries(view: GridView, kind: Kind, out: GridFeature[]): void {
  for (const lat of BAND_EDGES) {
    if (lat < view.south || lat > view.north) continue;
    for (const [west, east] of view.ranges) out.push(line(densify([west, lat], [east, lat], 2), kind, 0));
  }
  const meridians: [number, number, number][] = [];
  for (const cell of gzdCells()) if (cell.zone !== 0) meridians.push([cell.west, cell.south, cell.north]);
  // The caps split at the 0° / 180° meridian.
  for (const lon of [0, -180]) {
    meridians.push([lon, -90, UTM_SOUTH_LIMIT]);
    meridians.push([lon, UTM_NORTH_LIMIT, 90]);
  }
  for (const [lon, cellSouth, cellNorth] of meridians) {
    const south = Math.max(cellSouth, view.south);
    const north = Math.min(cellNorth, view.north);
    if (south >= north) continue;
    for (const [west, east] of view.ranges) {
      const x = lon >= west - EPS && lon <= east + EPS ? lon : lon === -180 && east >= 180 - EPS ? 180 : null;
      if (x !== null) out.push(line(densify([x, south], [x, north], 2), kind, 0));
    }
  }
}

interface Level {
  rank: Rank;
  step: number;
}

interface Visible {
  cell: GzdCell;
  rect: Rect;
  frame: Frame;
  box: [number, number, number, number];
}

/** How one of the two grids on the UTM lines is drawn: MGRS names the
 * squares, UTM the zones and whole eastings and northings. `lines` is off
 * for UTM when MGRS has drawn them; `beside` moves UTM's numbers off the
 * spots MGRS's take. */
interface UtmStyle {
  kind: "mgrs" | "utm";
  lines: boolean;
  beside: boolean;
}

/** The MGRS grid for a view. */
export function mgrsFeatures(view: GridView): GridFeature[] {
  return utmLineFeatures(view, { kind: "mgrs", lines: true, beside: false });
}

/** The UTM grid for a view; with `withMgrs` only its names, over the lines
 * `mgrsFeatures` draws. */
export function utmFeatures(view: GridView, withMgrs = false): GridFeature[] {
  return utmLineFeatures(view, { kind: "utm", lines: !withMgrs, beside: withMgrs });
}

function zoneName(cell: GzdCell): string {
  if (cell.zone === 0) return cell.north > 0 ? "UPS N" : "UPS S";
  return `${cell.zone}${cell.south >= 0 ? "N" : "S"}`;
}

function utmLineFeatures(view: GridView, style: UtmStyle): GridFeature[] {
  const out: GridFeature[] = [];
  const { kind } = style;
  if (style.lines) zoneBoundaries(view, kind, out);
  const z = view.zoom;
  const visible: Visible[] = [];
  for (const cell of gzdCells()) {
    for (const rect of intersect(cell, view)) {
      const middle: [number, number] = [(rect.west + rect.east) / 2, (rect.south + rect.north) / 2];
      if (kind === "mgrs" && z >= GZD_LABEL_MIN_ZOOM && z < SQUARE_PREFIX_MIN_ZOOM) {
        out.push(label(middle[0], middle[1], cell.name, kind, 0, "center"));
      }
      // UTM has no square names to carry the zone, so it keeps it at every
      // zoom.
      if (kind === "utm" && z >= GZD_LABEL_MIN_ZOOM) {
        out.push(label(middle[0], middle[1], zoneName(cell), kind, 0, style.beside && z < SQUARE_PREFIX_MIN_ZOOM ? "below" : "center"));
      }
      if (z >= SQUARE_MIN_ZOOM) {
        const frame = frameOf(cell);
        const box = cell.zone === 0 ? extent(frame, rect, 48, null) : extent(frame, rect, 16, centralMeridian(cell.zone));
        visible.push({ cell, rect, frame, box });
      }
    }
  }
  if (visible.length === 0) return out;
  const levels: Level[] = [{ rank: 1, step: 100_000 }];
  if (z >= TEN_KM_MIN_ZOOM) levels.push({ rank: 2, step: 10_000 });
  if (z >= ONE_KM_MIN_ZOOM) levels.push({ rank: 3, step: 1_000 });
  let drawn: Level | null = null;
  for (const [at, level] of levels.entries()) {
    const coarser = levels[at - 1]?.step ?? null;
    let count = 0;
    for (const { box } of visible) count += (box[2] - box[0] + box[3] - box[1]) / level.step + 4;
    if (count > MAX_LINES) break;
    drawn = level;
    if (!style.lines) continue;
    for (const { cell, rect, frame, box } of visible) {
      const segments = cell.zone === 0 ? (level.rank === 1 ? 32 : 12) : level.rank === 1 ? 16 : 8;
      // A line is straight in its frame, so the sampled box is padded by a
      // step: a bound missed between samples drops no line.
      const pad = level.step + 0.01 * Math.max(box[2] - box[0], box[3] - box[1]);
      const [minX, minY, maxX, maxY] = [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad];
      for (const vertical of [true, false]) {
        const lo = vertical ? minX : minY;
        const hi = vertical ? maxX : maxY;
        for (let k = Math.ceil(lo / level.step); k * level.step <= hi; k += 1) {
          if (coarser !== null && (k * level.step) % coarser === 0) continue;
          const runs = vertical
            ? trace(frame, rect, true, k * level.step, minY, maxY, segments)
            : trace(frame, rect, false, k * level.step, minX, maxX, segments);
          for (const run of runs) out.push(line(run, kind, level.rank));
        }
      }
    }
  }
  if (kind === "mgrs") {
    if (z >= SQUARE_LABEL_MIN_ZOOM) squareLabels(visible, z, out);
    if (drawn !== null && drawn.rank >= 2) edgeLabels(view, drawn, out);
  } else if (drawn !== null) {
    // Without square names, the 100 km lines are numbered too.
    utmEdgeLabels(view, drawn, style.beside, out);
  }
  return out;
}

function squareLabels(visible: Visible[], zoom: number, out: GridFeature[]): void {
  let count = 0;
  for (const { box } of visible) count += Math.ceil((box[2] - box[0]) / 100_000 + 1) * Math.ceil((box[3] - box[1]) / 100_000 + 1);
  if (count > MAX_LABELS) return;
  for (const { cell, rect, frame, box } of visible) {
    for (let x = Math.floor(box[0] / 100_000) * 100_000; x < box[2]; x += 100_000) {
      for (let y = Math.floor(box[1] / 100_000) * 100_000; y < box[3]; y += 100_000) {
        // The middle of the square's visible part, so a square larger than
        // the view is still named on screen.
        const cx = (Math.max(x, box[0]) + Math.min(x + 100_000, box[2])) / 2;
        const cy = (Math.max(y, box[1]) + Math.min(y + 100_000, box[3])) / 2;
        const point = frame.inverse(cx, cy);
        const lon = inside(rect, point);
        if (lon === null) continue;
        const square = cell.zone === 0 ? upsSquareId(cell.north > 0, cx, cy) : utmSquareId(cell.zone, cx, cy);
        if (square === null) continue;
        out.push(label(lon, point.lat, zoom >= SQUARE_PREFIX_MIN_ZOOM ? `${cell.name} ${square}` : square, "mgrs", 1, "center"));
      }
    }
  }
}

/** The frame a point's lines are numbered in, and its coordinate there. */
function mgrsAxisAt(point: LatLon, easting: boolean): { key: string; value: number } | null {
  if (!Number.isFinite(point.lat) || point.lat < -90 || point.lat > 90) return null;
  if (point.lat >= UTM_SOUTH_LIMIT && point.lat < UTM_NORTH_LIMIT) {
    const zone = utmZone(point.lat, point.lon);
    const utm = toUtm(point.lat, point.lon, zone);
    return { key: `${zone}${utm.north ? "N" : "S"}`, value: easting ? utm.easting : utm.northing };
  }
  const ups = toUps(point.lat, point.lon);
  return { key: ups.north ? "Y" : "A", value: easting ? ups.easting : ups.northing };
}

/** Where the lines of `step` cross a sampled edge: each crossing between
 * two samples in the same frame, placed by linear interpolation (the
 * samples are a few pixels apart). */
function crossings(
  samples: LatLon[],
  valueAt: (point: LatLon) => { key: string; value: number } | null,
  step: number,
  emit: (point: LatLon, value: number) => void,
): void {
  let previous: { point: LatLon; key: string; value: number } | null = null;
  for (const point of samples) {
    const here = valueAt(point);
    if (here !== null && previous !== null && previous.key === here.key && previous.value !== here.value) {
      const lo = Math.min(previous.value, here.value);
      const hi = Math.max(previous.value, here.value);
      for (let k = Math.ceil(lo / step); k * step < hi; k += 1) {
        const t = (k * step - previous.value) / (here.value - previous.value);
        let dLon = point.lon - previous.point.lon;
        if (dLon > 180) dLon -= 360;
        if (dLon < -180) dLon += 360;
        emit({ lat: previous.point.lat + (point.lat - previous.point.lat) * t, lon: previous.point.lon + dLon * t }, k * step);
      }
    }
    previous = here === null ? null : { point, ...here };
  }
}

/** Kilometres within the 100 km square, `07`. */
function kilometres(value: number): string {
  const within = (((Math.round(value) % 100_000) + 100_000) % 100_000) / 1000;
  return String(Math.floor(within + 1e-6)).padStart(2, "0");
}

function edgeLabels(view: GridView, level: Level, out: GridFeature[]): void {
  crossings(view.edges.bottom, (p) => mgrsAxisAt(p, true), level.step, (p, v) =>
    out.push(label(p.lon, p.lat, kilometres(v), "mgrs", level.rank, "bottom")),
  );
  crossings(view.edges.left, (p) => mgrsAxisAt(p, false), level.step, (p, v) =>
    out.push(label(p.lon, p.lat, kilometres(v), "mgrs", level.rank, "left")),
  );
}

/** UTM's numbers: whole kilometres from the zone's origin, `385` and
 * `3950`, along the bottom and left edges, or the top and right `beside`
 * MGRS's. */
function utmEdgeLabels(view: GridView, level: Level, beside: boolean, out: GridFeature[]): void {
  const km = (value: number) => String(Math.round(value / 1000));
  crossings(beside ? view.edges.top : view.edges.bottom, (p) => mgrsAxisAt(p, true), level.step, (p, v) =>
    out.push(label(p.lon, p.lat, km(v), "utm", level.rank, beside ? "top" : "bottom")),
  );
  crossings(beside ? view.edges.right : view.edges.left, (p) => mgrsAxisAt(p, false), level.step, (p, v) =>
    out.push(label(p.lon, p.lat, km(v), "utm", level.rank, beside ? "right" : "left")),
  );
}

/** A GARS level: its rank, its step in minutes and the precision its
 * names carry. */
interface GarsLevel {
  rank: Rank;
  minutes: number;
  precision: GarsPrecision;
}

const GARS_LEVELS: readonly GarsLevel[] = [
  { rank: 1, minutes: 30, precision: 30 },
  { rank: 2, minutes: 15, precision: 15 },
  { rank: 3, minutes: 5, precision: 5 },
];

/** The GARS grid for a view. */
export function garsFeatures(view: GridView): GridFeature[] {
  const out: GridFeature[] = [];
  const z = view.zoom;
  if (z < GARS_MIN_ZOOM) return out;
  const south = Math.max(-90, view.south);
  const north = Math.min(90, view.north);
  if (south >= north) return out;
  const levels = GARS_LEVELS.filter(
    (level) => level.rank === 1 || (level.rank === 2 && z >= GARS_QUADRANT_MIN_ZOOM) || (level.rank === 3 && z >= GARS_KEYPAD_MIN_ZOOM),
  );
  // Lines are counted in whole minutes from 90°S and 180°W, so a 5′ line
  // lands on the minute and the coarser lines are skipped exactly.
  const minuteSpan = (lo: number, hi: number, minutes: number): [number, number] => [Math.ceil((lo * 60) / minutes - EPS), Math.floor((hi * 60) / minutes + EPS)];
  let drawn: GarsLevel | null = null;
  for (const [at, level] of levels.entries()) {
    const coarser = levels[at - 1]?.minutes ?? null;
    let count = ((north - south) * 60) / level.minutes;
    for (const [west, east] of view.ranges) count += ((east - west) * 60) / level.minutes;
    if (count > MAX_LINES) break;
    drawn = level;
    const along = Math.min(2, level.minutes / 15);
    const [k0, k1] = minuteSpan(south + 90, north + 90, level.minutes);
    for (let k = k0; k <= k1; k += 1) {
      const minute = k * level.minutes;
      if (coarser !== null && minute % coarser === 0) continue;
      const lat = minute / 60 - 90;
      if (Math.abs(lat) >= 90 - EPS) continue;
      for (const [west, east] of view.ranges) out.push(line(densify([west, lat], [east, lat], along), "gars", level.rank));
    }
    for (const [west, east] of view.ranges) {
      const [j0, j1] = minuteSpan(west + 180, east + 180, level.minutes);
      for (let j = j0; j <= j1; j += 1) {
        const minute = j * level.minutes;
        if (coarser !== null && minute % coarser === 0) continue;
        // -180 and 180 are the one meridian.
        if (minute >= 360 * 60 && west <= -180 + EPS) continue;
        out.push(line(densify([minute / 60 - 180, south], [minute / 60 - 180, north], along), "gars", level.rank));
      }
    }
  }
  if (drawn !== null && z >= GARS_LABEL_MIN_ZOOM) garsLabels(view, south, north, drawn, out);
  return out;
}

/** The 30′ cells' names, and the quadrant numbers or the keypads' (by
 * quadrant and key, `39`) at the finest level drawn. The names sit on the
 * cell's middle, where its quadrant lines cross, or the middle of its
 * visible part when that is off screen; a number on its own cell's. */
function garsLabels(view: GridView, south: number, north: number, finest: GarsLevel, out: GridFeature[]): void {
  const cellsIn = (minutes: number) => {
    let count = Math.ceil(((north - south) * 60) / minutes + 1);
    let across = 0;
    for (const [west, east] of view.ranges) across += Math.ceil(((east - west) * 60) / minutes + 1);
    count *= across;
    return count;
  };
  if (cellsIn(30) + (finest.rank === 1 ? 0 : cellsIn(finest.minutes)) > MAX_LABELS) return;
  const each = (minutes: number, visit: (west: number, south: number, size: number) => void) => {
    const size = minutes / 60;
    const i0 = Math.floor((south + 90) / size + EPS);
    const i1 = Math.ceil((north + 90) / size - EPS);
    for (const [west, east] of view.ranges) {
      const j0 = Math.floor((west + 180) / size + EPS);
      const j1 = Math.ceil((east + 180) / size - EPS);
      for (let i = i0; i < i1; i += 1) for (let j = j0; j < j1; j += 1) visit(j * size - 180, i * size - 90, size);
    }
  };
  const ranges = view.ranges;
  const shown = (lat: number, lon: number) => lat >= south && lat <= north && ranges.some(([w, e]) => lon >= w && lon <= e);
  each(30, (west, cellSouth, size) => {
    const name = toGars(cellSouth + size / 2, west + size / 2, 30);
    if (name === null) return;
    let lon = west + size / 2;
    let lat = cellSouth + size / 2;
    if (!shown(lat, lon)) {
      const range = ranges.find(([w, e]) => w < west + size && e > west);
      if (range === undefined) return;
      lon = (Math.max(west, range[0]) + Math.min(west + size, range[1])) / 2;
      lat = (Math.max(cellSouth, south) + Math.min(cellSouth + size, north)) / 2;
    }
    out.push(label(lon, lat, name, "gars", 1, "center"));
  });
  if (finest.rank === 1) return;
  each(finest.minutes, (west, cellSouth, size) => {
    const lat = cellSouth + size / 2;
    const lon = west + size / 2;
    if (!shown(lat, lon)) return;
    const reference = toGars(lat, lon, finest.precision);
    if (reference !== null) out.push(label(lon, lat, reference.slice(5), "gars", finest.rank, "center"));
  });
}

const BNG_MAJOR_EASTINGS = [0, 500_000, BNG_MAX_EASTING];
const BNG_MAJOR_NORTHINGS = [0, 500_000, 1_000_000, BNG_MAX_NORTHING];

/** The British National Grid for a view; nothing where the view is clear
 * of the grid's box. */
export function bngFeatures(view: GridView): GridFeature[] {
  const out: GridFeature[] = [];
  const rects = intersect(BNG_ENVELOPE, view);
  if (rects.length === 0) return out;
  const z = view.zoom;
  let [minX, minY, maxX, maxY] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const rect of rects) {
    const [x0, y0, x1, y1] = extent(BNG_FRAME, rect, 16, -2);
    [minX, minY, maxX, maxY] = [Math.min(minX, x0), Math.min(minY, y0), Math.max(maxX, x1), Math.max(maxY, y1)];
  }
  // The box padded by a hundredth for the curvature missed between
  // samples, then clipped to the grid.
  const pad = 0.01 * Math.max(maxX - minX, maxY - minY);
  const box: [number, number, number, number] = [
    Math.max(0, minX - pad),
    Math.max(0, minY - pad),
    Math.min(BNG_MAX_EASTING, maxX + pad),
    Math.min(BNG_MAX_NORTHING, maxY + pad),
  ];
  if (!(box[0] < box[2] && box[1] < box[3])) return out;
  const run = (vertical: boolean, value: number, segments: number): [number, number][] => {
    const from = vertical ? box[1] : box[0];
    const to = vertical ? box[3] : box[2];
    const points: [number, number][] = [];
    for (let i = 0; i <= segments; i += 1) {
      const t = from + ((to - from) * i) / segments;
      const point = vertical ? fromBng(value, t) : fromBng(t, value);
      points.push([point.lon, point.lat]);
    }
    return points;
  };
  for (const x of BNG_MAJOR_EASTINGS) if (x >= box[0] && x <= box[2]) out.push(line(run(true, x, 32), "bng", 0));
  for (const y of BNG_MAJOR_NORTHINGS) if (y >= box[1] && y <= box[3]) out.push(line(run(false, y, 32), "bng", 0));
  const levels: Level[] = [];
  if (z >= SQUARE_MIN_ZOOM) levels.push({ rank: 1, step: 100_000 });
  if (z >= TEN_KM_MIN_ZOOM) levels.push({ rank: 2, step: 10_000 });
  if (z >= ONE_KM_MIN_ZOOM) levels.push({ rank: 3, step: 1_000 });
  let drawn: Level | null = null;
  for (const [at, level] of levels.entries()) {
    const coarser = levels[at - 1]?.step ?? null;
    if ((box[2] - box[0] + box[3] - box[1]) / level.step + 4 > MAX_LINES) break;
    drawn = level;
    const segments = level.rank === 1 ? 16 : 8;
    for (const vertical of [true, false]) {
      const lo = vertical ? box[0] : box[1];
      const hi = vertical ? box[2] : box[3];
      const majors = vertical ? BNG_MAJOR_EASTINGS : BNG_MAJOR_NORTHINGS;
      for (let k = Math.ceil(lo / level.step); k * level.step <= hi; k += 1) {
        const value = k * level.step;
        if (majors.includes(value) || (coarser !== null && value % coarser === 0)) continue;
        out.push(line(run(vertical, value, segments), "bng", level.rank));
      }
    }
  }
  bngSquareLabels(box, z, out);
  if (drawn !== null && drawn.rank >= 2) {
    const axis = (point: LatLon, easting: boolean) => {
      const lon = wrapLongitude(point.lon);
      if (!Number.isFinite(point.lat) || point.lat < BNG_ENVELOPE.south || point.lat > BNG_ENVELOPE.north) return null;
      if (lon < BNG_ENVELOPE.west || lon > BNG_ENVELOPE.east) return null;
      const grid = toBng(point.lat, lon);
      if (bngSquareId(grid.easting, grid.northing) === null) return null;
      return { key: "bng", value: easting ? grid.easting : grid.northing };
    };
    const step = drawn.step;
    const rank = drawn.rank;
    crossings(view.edges.bottom, (p) => axis(p, true), step, (p, v) => out.push(label(p.lon, p.lat, kilometres(v), "bng", rank, "bottom")));
    crossings(view.edges.left, (p) => axis(p, false), step, (p, v) => out.push(label(p.lon, p.lat, kilometres(v), "bng", rank, "left")));
  }
  return out;
}

/** The 500 km letters out to z6, the 100 km squares' two letters from
 * there, each in the middle of the square's part inside `box`. */
function bngSquareLabels(box: [number, number, number, number], zoom: number, out: GridFeature[]): void {
  if (zoom < GZD_LABEL_MIN_ZOOM) return;
  const name = (minX: number, minY: number, maxX: number, maxY: number, text: (x: number, y: number) => string | null, rank: Rank) => {
    const x0 = Math.max(minX, box[0]);
    const x1 = Math.min(maxX, box[2]);
    const y0 = Math.max(minY, box[1]);
    const y1 = Math.min(maxY, box[3]);
    if (x0 >= x1 || y0 >= y1) return;
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const value = text(cx, cy);
    if (value === null) return;
    const point = fromBng(cx, cy);
    out.push(label(point.lon, point.lat, value, "bng", rank, "center"));
  };
  if (zoom < SQUARE_LABEL_MIN_ZOOM) {
    for (const [i, x] of BNG_MAJOR_EASTINGS.slice(0, -1).entries()) {
      for (const [j, y] of BNG_MAJOR_NORTHINGS.slice(0, -1).entries()) {
        name(x, y, BNG_MAJOR_EASTINGS[i + 1] ?? x, BNG_MAJOR_NORTHINGS[j + 1] ?? y, bngMajorSquare, 0);
      }
    }
    return;
  }
  if (Math.ceil((box[2] - box[0]) / 100_000 + 1) * Math.ceil((box[3] - box[1]) / 100_000 + 1) > MAX_LABELS) return;
  for (let x = Math.floor(box[0] / 100_000) * 100_000; x < box[2]; x += 100_000) {
    for (let y = Math.floor(box[1] / 100_000) * 100_000; y < box[3]; y += 100_000) name(x, y, x + 100_000, y + 100_000, bngSquareId, 1);
  }
}

/** The parallels' and meridians' spacing in degrees at a zoom: the finest
 * step still `GRATICULE_MIN_SPACING_PX` apart on the equator. */
export function graticuleStep(zoom: number): number {
  const pxPerDegree = (512 * 2 ** zoom) / 360;
  let chosen = 30;
  for (const step of GRATICULE_STEPS) if (step * pxPerDegree >= GRATICULE_MIN_SPACING_PX) chosen = step;
  return chosen;
}

function decimals(step: number): number {
  let places = 0;
  while (places < 4 && Math.abs(step * 10 ** places - Math.round(step * 10 ** places)) > 1e-9) places += 1;
  return places;
}

/** `35°N`, `139.25°E`, `0°`, `180°`. */
export function formatGraticule(value: number, latitude: boolean, step: number): string {
  const places = decimals(step);
  const rounded = Number(value.toFixed(places));
  const wrapped = latitude ? rounded : Math.abs(rounded) === 180 ? 180 : Number(wrapLongitude(rounded).toFixed(places));
  const text = Math.abs(wrapped).toFixed(places);
  if (wrapped === 0 || (!latitude && wrapped === 180)) return `${text}°`;
  return `${text}°${latitude ? (wrapped > 0 ? "N" : "S") : wrapped > 0 ? "E" : "W"}`;
}

/** Parallels and meridians for a view. */
export function graticuleFeatures(view: GridView): GridFeature[] {
  const out: GridFeature[] = [];
  const step = graticuleStep(view.zoom);
  const south = Math.max(-90, view.south);
  const north = Math.min(90, view.north);
  let count = (north - south) / step;
  for (const [west, east] of view.ranges) count += (east - west) / step;
  if (count > MAX_LINES) return out;
  const along = Math.min(2, step * 4);
  for (let k = Math.ceil(south / step); k * step <= north + EPS; k += 1) {
    const lat = k * step;
    if (Math.abs(lat) >= 90 - EPS) continue;
    for (const [west, east] of view.ranges) out.push(line(densify([west, lat], [east, lat], along), "latlon", 1));
  }
  for (const [west, east] of view.ranges) {
    for (let k = Math.ceil(west / step); k * step <= east + EPS; k += 1) {
      // -180 and 180 are the one meridian.
      if (k * step >= 180 - EPS && west <= -180 + EPS) continue;
      out.push(line(densify([k * step, south], [k * step, north], along), "latlon", 1));
    }
  }
  if (view.zoom < WORLD_ZOOM + 1 || view.edges.top.length === 0) {
    // Far out the view's edges are space around the globe; the lines are
    // named where they cross the middle of the view instead.
    const lat0 = Math.max(-80, Math.min(80, Math.round((south + north) / 2 / step) * step));
    const [west0, east0] = view.ranges[0] ?? [-180, 180];
    const lon0 = Math.round((west0 + east0) / 2 / step) * step;
    for (const [west, east] of view.ranges) {
      for (let k = Math.ceil(west / step); k * step < east - EPS; k += 1) out.push(label(k * step, lat0, formatGraticule(k * step, false, step), "latlon", 1, "bottom"));
    }
    for (let k = Math.ceil(south / step); k * step <= north + EPS; k += 1) {
      if (Math.abs(k * step) < 90 - EPS) out.push(label(lon0, k * step, formatGraticule(k * step, true, step), "latlon", 1, "left"));
    }
    return out;
  }
  let previousLon: number | null = null;
  crossings(
    view.edges.top,
    (point) => {
      // The samples' longitudes run on unwrapped, so a meridian is counted
      // once across the antimeridian.
      if (!Number.isFinite(point.lon)) return null;
      let lon = point.lon;
      if (previousLon !== null) while (lon - previousLon > 180) lon -= 360;
      if (previousLon !== null) while (lon - previousLon < -180) lon += 360;
      previousLon = lon;
      return { key: "lon", value: lon };
    },
    step,
    (point, value) => out.push(label(point.lon, point.lat, formatGraticule(value, false, step), "latlon", 1, "top")),
  );
  crossings(
    view.edges.right,
    (point) => (Number.isFinite(point.lat) ? { key: "lat", value: point.lat } : null),
    step,
    (point, value) => out.push(label(point.lon, point.lat, formatGraticule(value, true, step), "latlon", 1, "right")),
  );
  return out;
}

export interface GridInk {
  mgrs: string;
  utm: string;
  gars: string;
  bng: string;
  latlon: string;
  casing: string;
  halo: string;
}

/** Thin ink for the ground the map is on: near-black or near-white for the
 * MGRS grid and UTM (the same lines), an amber for GARS, a teal for the
 * National Grid and a magenta for the parallels and meridians, so any two
 * read apart where both are on. */
export function gridInk(darkGround: boolean): GridInk {
  return darkGround
    ? {
        mgrs: "rgba(236, 240, 246, 0.8)",
        utm: "rgba(236, 240, 246, 0.8)",
        gars: "rgba(255, 196, 92, 0.85)",
        bng: "rgba(96, 222, 200, 0.85)",
        latlon: "rgba(255, 170, 225, 0.85)",
        casing: "rgba(0, 0, 0, 0.55)",
        halo: "rgba(0, 0, 0, 0.75)",
      }
    : {
        mgrs: "rgba(24, 28, 36, 0.75)",
        utm: "rgba(24, 28, 36, 0.75)",
        gars: "rgba(165, 95, 0, 0.85)",
        bng: "rgba(0, 118, 108, 0.85)",
        latlon: "rgba(160, 30, 110, 0.8)",
        casing: "rgba(243, 239, 230, 0.55)",
        halo: "rgba(243, 239, 230, 0.92)",
      };
}

/** A quieter ink for a map whose subject is drawn in thin grey-white
 * lines itself (a storm track without an intensity class): a muted steel
 * blue that reads apart from the track, at half the strength, and no
 * casing. */
export function quietGridInk(darkGround: boolean): GridInk {
  return darkGround
    ? {
        mgrs: "rgba(120, 165, 215, 0.6)",
        utm: "rgba(120, 165, 215, 0.6)",
        gars: "rgba(215, 170, 95, 0.55)",
        bng: "rgba(105, 195, 180, 0.55)",
        latlon: "rgba(220, 140, 200, 0.55)",
        casing: "rgba(0, 0, 0, 0)",
        halo: "rgba(0, 0, 0, 0.8)",
      }
    : {
        mgrs: "rgba(40, 85, 140, 0.55)",
        utm: "rgba(40, 85, 140, 0.55)",
        gars: "rgba(145, 90, 15, 0.5)",
        bng: "rgba(15, 110, 100, 0.5)",
        latlon: "rgba(150, 40, 110, 0.5)",
        casing: "rgba(0, 0, 0, 0)",
        halo: "rgba(243, 239, 230, 0.92)",
      };
}

const kindColor = (ink: GridInk): ExpressionSpecification => [
  "match",
  ["get", "kind"],
  "latlon",
  ink.latlon,
  "utm",
  ink.utm,
  "gars",
  ink.gars,
  "bng",
  ink.bng,
  ink.mgrs,
];

export interface EdgeInsets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface CoordinateGridOptions {
  map: MaplibreMap;
  darkGround: () => boolean;
  /** How far in from each side of the map the edge numbers sit, in pixels:
   * clear of the controls laid over it. */
  edgeInsets?: () => EdgeInsets;
  /** The ink for a ground; `gridInk` unless the map's own lines need the
   * grid quieter (`quietGridInk`). */
  ink?: (darkGround: boolean) => GridInk;
}

export class CoordinateGrid {
  private readonly map: MaplibreMap;
  private state: GridState = { ...NO_GRID };

  constructor(private readonly options: CoordinateGridOptions) {
    this.map = options.map;
    this.map.on("moveend", this.refresh);
    this.map.on("resize", this.refresh);
  }

  setState(state: GridState): void {
    this.state = { ...state };
    if (!anyGrid(this.state)) this.remove();
    else this.refresh();
  }

  /** Ink for the ground the map is on; a no-op while the grids are off. */
  setInk(darkGround: boolean): void {
    if (!this.map.getLayer(GRID_LINE_LAYER)) return;
    const ink = (this.options.ink ?? gridInk)(darkGround);
    this.map.setPaintProperty(GRID_LINE_LAYER, "line-color", kindColor(ink));
    this.map.setPaintProperty(GRID_CASING_LAYER, "line-color", ink.casing);
    this.map.setPaintProperty(GRID_LABEL_LAYER, "text-color", kindColor(ink));
    this.map.setPaintProperty(GRID_LABEL_LAYER, "text-halo-color", ink.halo);
  }

  private readonly refresh = (): void => {
    if (!anyGrid(this.state)) return;
    const view = this.view();
    const features: GridFeature[] = [];
    if (this.state.latlon) features.push(...graticuleFeatures(view));
    if (this.state.mgrs) features.push(...mgrsFeatures(view));
    if (this.state.utm) features.push(...utmFeatures(view, this.state.mgrs));
    if (this.state.bng) features.push(...bngFeatures(view));
    if (this.state.gars) features.push(...garsFeatures(view));
    this.add();
    const collection: FeatureCollection = { type: "FeatureCollection", features };
    (this.map.getSource(GRID_SOURCE) as GeoJSONSource | undefined)?.setData(collection);
  };

  private view(): GridView {
    const map = this.map;
    const zoom = map.getZoom();
    const edges: GridView["edges"] = { top: [], right: [], bottom: [], left: [] };
    if (zoom < WORLD_ZOOM) return { zoom, ranges: [[-180, 180]], south: -90, north: 90, edges };
    const bounds = map.getBounds();
    const south = Math.max(-90, bounds.getSouth());
    const north = Math.min(90, bounds.getNorth());
    const west = bounds.getWest();
    const east = bounds.getEast();
    let ranges: [number, number][];
    if (east - west >= 360 || !Number.isFinite(west) || !Number.isFinite(east)) ranges = [[-180, 180]];
    else {
      const w = wrapLongitude(west);
      const e = w + (east - west);
      ranges = e > 180 ? [[w, 180], [-180, e - 360]] : [[w, e]];
    }
    const container = map.getContainer();
    const width = container.clientWidth;
    const height = container.clientHeight;
    const inset = this.options.edgeInsets?.() ?? { top: 12, right: 12, bottom: 12, left: 12 };
    const sample = (x0: number, y0: number, x1: number, y1: number): LatLon[] => {
      const count = Math.max(2, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / EDGE_SAMPLE_PX));
      const out: LatLon[] = [];
      for (let i = 0; i <= count; i += 1) {
        const at = map.unproject([x0 + ((x1 - x0) * i) / count, y0 + ((y1 - y0) * i) / count]);
        out.push({ lat: at.lat, lon: at.lng });
      }
      return out;
    };
    const left = inset.left;
    const right = width - inset.right;
    const top = inset.top;
    const bottom = height - inset.bottom;
    if (right > left && bottom > top) {
      edges.top = sample(left, top, right, top);
      edges.bottom = sample(left, bottom, right, bottom);
      edges.left = sample(left, top, left, bottom);
      edges.right = sample(right, top, right, bottom);
    }
    return { zoom, ranges, south, north, edges };
  }

  private add(): void {
    const map = this.map;
    if (map.getSource(GRID_SOURCE)) return;
    const ink = (this.options.ink ?? gridInk)(this.options.darkGround());
    const before = firstSymbolLayerId(map);
    map.addSource(GRID_SOURCE, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    const isLine: ExpressionSpecification = ["==", ["geometry-type"], "LineString"];
    // The casing goes under the zone and 100 km lines only, as the
    // contours case their index lines: a cased kilometre line is a ladder.
    map.addLayer(
      {
        id: GRID_CASING_LAYER,
        type: "line",
        source: GRID_SOURCE,
        filter: ["all", isLine, ["match", ["get", "kind"], ["mgrs", "utm", "bng"], true, false], ["<=", ["get", "rank"], 1]],
        layout: { "line-join": "round" },
        paint: { "line-color": ink.casing, "line-width": ["match", ["get", "rank"], 0, 3.6, 3], "line-blur": 0.6 },
      },
      before,
    );
    map.addLayer(
      {
        id: GRID_LINE_LAYER,
        type: "line",
        source: GRID_SOURCE,
        filter: isLine,
        layout: { "line-join": "round" },
        paint: {
          "line-color": kindColor(ink),
          "line-width": ["case", ["==", ["get", "kind"], "latlon"], 0.8, ["match", ["get", "rank"], 0, 1.6, 1, 1.2, 2, 1, 0.8]],
          "line-opacity": ["case", ["==", ["get", "kind"], "latlon"], 0.85, ["match", ["get", "rank"], 0, 1, 1, 0.95, 2, 0.85, 0.75]],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: GRID_LABEL_LAYER,
        type: "symbol",
        source: GRID_SOURCE,
        filter: ["==", ["geometry-type"], "Point"],
        layout: {
          "text-field": ["get", "label"],
          "text-font": ["Noto Sans Medium"],
          "text-size": ["match", ["get", "anchor"], ["center", "below"], ["match", ["get", "rank"], 0, 13, 12], 10],
          "text-anchor": ["match", ["get", "anchor"], "below", "top", ["get", "anchor"]] as ExpressionSpecification,
          "text-offset": [
            "match",
            ["get", "anchor"],
            "below", ["literal", [0, 0.8]],
            "bottom", ["literal", [0, -0.2]],
            "top", ["literal", [0, 0.2]],
            "left", ["literal", [0.3, 0]],
            "right", ["literal", [-0.3, 0]],
            ["literal", [0, 0]],
          ],
          "text-letter-spacing": 0.04,
          "text-padding": 2,
        },
        paint: { "text-color": kindColor(ink), "text-halo-color": ink.halo, "text-halo-width": 1.5, "text-halo-blur": 0.5 },
      },
      before,
    );
  }

  private remove(): void {
    const map = this.map;
    for (const id of [GRID_LABEL_LAYER, GRID_LINE_LAYER, GRID_CASING_LAYER]) if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(GRID_SOURCE)) map.removeSource(GRID_SOURCE);
  }
}
