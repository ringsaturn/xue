import type { ExpressionSpecification, GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";
import type { Feature, FeatureCollection, LineString, Point } from "geojson";

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
import type { GridState } from "./urlstate";

/**
 * The coordinate grids over the map: the MGRS grid as the GSI's UTM grid
 * draws it, and parallels and meridians. Both are GeoJSON recomputed for
 * the viewport on every `moveend`, so they follow the plane and the globe
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
 * Everything sits under the basemap's first symbol layer, as the marks do:
 * above every field and line slot (those attach under the coast), under
 * the place labels.
 */

export const GRID_SOURCE = "coordinate-grid";
export const GRID_CASING_LAYER = "coordinate-grid-casing";
export const GRID_LINE_LAYER = "coordinate-grid-lines";
export const GRID_LABEL_LAYER = "coordinate-grid-labels";

/** The zooms each MGRS level appears from. */
export const GZD_LABEL_MIN_ZOOM = 3;
export const SQUARE_MIN_ZOOM = 5;
export const SQUARE_LABEL_MIN_ZOOM = 6;
export const SQUARE_PREFIX_MIN_ZOOM = 7;
export const TEN_KM_MIN_ZOOM = 8;
export const ONE_KM_MIN_ZOOM = 11;

const MAX_LINES = 2500;
const MAX_LABELS = 3000;
/** Below this the whole world is drawn: a far-out globe's bounds are not a
 * box worth clipping to. */
const WORLD_ZOOM = 3;
/** The narrowest spacing in pixels the parallels and meridians draw at. */
const GRATICULE_MIN_SPACING_PX = 100;
const GRATICULE_STEPS = [30, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01];
const EDGE_SAMPLE_PX = 8;

/** Line rank: 0 grid zone, 1 100 km, 2 10 km, 3 1 km; parallels and
 * meridians are their own kind. */
type Rank = 0 | 1 | 2 | 3;
type Kind = "mgrs" | "latlon";
type Anchor = "center" | "bottom" | "left" | "top" | "right";

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

const EPS = 1e-9;

/** The point's longitude as the rect counts it (180 for -180 on an east
 * edge), or `null` outside. */
function inside(rect: Rect, point: LatLon): number | null {
  if (!Number.isFinite(point.lat) || point.lat < rect.south - EPS || point.lat > rect.north + EPS) return null;
  let lon = point.lon;
  if (lon < rect.west - EPS && lon + 360 <= rect.east + EPS) lon += 360;
  return lon >= rect.west - EPS && lon <= rect.east + EPS ? lon : null;
}

function intersect(cell: GzdCell, view: GridView): Rect[] {
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
function extent(frame: Frame, rect: Rect, zone: number): [number, number, number, number] {
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
  const steps = zone === 0 ? 48 : 16;
  for (let i = 0; i <= steps; i += 1) {
    const lon = rect.west + ((rect.east - rect.west) * i) / steps;
    const lat = rect.south + ((rect.north - rect.south) * i) / steps;
    add(rect.south, lon);
    add(rect.north, lon);
    add(lat, rect.west);
    add(lat, rect.east);
  }
  if (zone !== 0) {
    const cm = centralMeridian(zone);
    if (cm > rect.west && cm < rect.east) {
      add(rect.south, cm);
      add(rect.north, cm);
    }
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

function zoneBoundaries(view: GridView, out: GridFeature[]): void {
  for (const lat of BAND_EDGES) {
    if (lat < view.south || lat > view.north) continue;
    for (const [west, east] of view.ranges) out.push(line(densify([west, lat], [east, lat], 2), "mgrs", 0));
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
      if (x !== null) out.push(line(densify([x, south], [x, north], 2), "mgrs", 0));
    }
  }
}

interface Level {
  rank: Rank;
  step: number;
}

/** The MGRS grid for a view. */
export function mgrsFeatures(view: GridView): GridFeature[] {
  const out: GridFeature[] = [];
  zoneBoundaries(view, out);
  const z = view.zoom;
  const visible: { cell: GzdCell; rect: Rect; frame: Frame; box: [number, number, number, number] }[] = [];
  for (const cell of gzdCells()) {
    for (const rect of intersect(cell, view)) {
      if (z >= GZD_LABEL_MIN_ZOOM && z < SQUARE_PREFIX_MIN_ZOOM) {
        out.push(label((rect.west + rect.east) / 2, (rect.south + rect.north) / 2, cell.name, "mgrs", 0, "center"));
      }
      if (z >= SQUARE_MIN_ZOOM) {
        const frame = frameOf(cell);
        visible.push({ cell, rect, frame, box: extent(frame, rect, cell.zone) });
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
          for (const run of runs) out.push(line(run, "mgrs", level.rank));
        }
      }
    }
    drawn = level;
  }
  if (z >= SQUARE_LABEL_MIN_ZOOM) squareLabels(visible, z, out);
  if (drawn !== null && drawn.rank >= 2) edgeLabels(view, drawn, out);
  return out;
}

function squareLabels(
  visible: { cell: GzdCell; rect: Rect; frame: Frame; box: [number, number, number, number] }[],
  zoom: number,
  out: GridFeature[],
): void {
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
  latlon: string;
  casing: string;
  halo: string;
}

/** Thin ink for the ground the map is on: near-black or near-white for the
 * MGRS grid, a magenta for the parallels and meridians so the two read
 * apart where both are on. */
export function gridInk(darkGround: boolean): GridInk {
  return darkGround
    ? { mgrs: "rgba(236, 240, 246, 0.8)", latlon: "rgba(255, 170, 225, 0.85)", casing: "rgba(0, 0, 0, 0.55)", halo: "rgba(0, 0, 0, 0.75)" }
    : { mgrs: "rgba(24, 28, 36, 0.75)", latlon: "rgba(160, 30, 110, 0.8)", casing: "rgba(243, 239, 230, 0.55)", halo: "rgba(243, 239, 230, 0.92)" };
}

/** A quieter ink for a map whose subject is drawn in thin grey-white
 * lines itself (a storm track without an intensity class): a muted steel
 * blue that reads apart from the track, at half the strength, and no
 * casing. */
export function quietGridInk(darkGround: boolean): GridInk {
  return darkGround
    ? { mgrs: "rgba(120, 165, 215, 0.6)", latlon: "rgba(220, 140, 200, 0.55)", casing: "rgba(0, 0, 0, 0)", halo: "rgba(0, 0, 0, 0.8)" }
    : { mgrs: "rgba(40, 85, 140, 0.55)", latlon: "rgba(150, 40, 110, 0.5)", casing: "rgba(0, 0, 0, 0)", halo: "rgba(243, 239, 230, 0.92)" };
}

const kindColor = (ink: GridInk): ExpressionSpecification => ["match", ["get", "kind"], "latlon", ink.latlon, ink.mgrs];

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
  private state: GridState = { mgrs: false, latlon: false };

  constructor(private readonly options: CoordinateGridOptions) {
    this.map = options.map;
    this.map.on("moveend", this.refresh);
    this.map.on("resize", this.refresh);
  }

  setState(state: GridState): void {
    this.state = { mgrs: state.mgrs, latlon: state.latlon };
    if (!this.state.mgrs && !this.state.latlon) this.remove();
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
    if (!this.state.mgrs && !this.state.latlon) return;
    const view = this.view();
    const features: GridFeature[] = [];
    if (this.state.latlon) features.push(...graticuleFeatures(view));
    if (this.state.mgrs) features.push(...mgrsFeatures(view));
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
        filter: ["all", isLine, ["==", ["get", "kind"], "mgrs"], ["<=", ["get", "rank"], 1]],
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
          "text-size": ["case", ["==", ["get", "anchor"], "center"], ["match", ["get", "rank"], 0, 13, 12], 10],
          "text-anchor": ["get", "anchor"] as ExpressionSpecification,
          "text-offset": [
            "match",
            ["get", "anchor"],
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
