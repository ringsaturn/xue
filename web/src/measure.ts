/** The ruler and the area tool on the view tile: armed by a press, each
 * click on the map then places a vertex, and the running length or the
 * enclosed area is drawn beside the pointer. A click back on the last
 * vertex (a double click does that) ends the shape, as does a click on the
 * first vertex of an area; the next click starts a new one. Escape ends the
 * shape, a second Escape or a second press puts the tool away, Backspace
 * takes the last vertex back.
 *
 * Clicks, not a sheet over the canvas as the volume tools use: a measure
 * takes many vertices, and the map has to keep panning between them.
 *
 * Lengths and areas are on the sphere, and the edges are drawn as the great
 * circles they are measured along, so a line across an ocean bows on the
 * Mercator map as the distance it reads says it should. The geometry is
 * plain functions so the tests can read it without a map. */

import type { Feature, FeatureCollection, Geometry } from "geojson";
import type {
  CircleLayerSpecification,
  FillLayerSpecification,
  LineLayerSpecification,
  Map as MaplibreMap,
  MapMouseEvent,
  SymbolLayerSpecification,
} from "maplibre-gl";

import { contextInk } from "./casecontext";

export type MeasureKind = "distance" | "area";

/** A longitude/latitude pair. */
export type LonLat = [number, number];

/** The mean Earth radius (IUGG), metres. */
export const EARTH_RADIUS = 6371008.8;

/** A great-circle edge is drawn in steps no longer than this. */
const STEP_METRES = 50_000;
const MAX_STEPS = 256;

/** Within this many screen pixels a click is on a vertex. */
export const SNAP_PIXELS = 8;

const RADIANS = Math.PI / 180;

/** The great-circle distance between two points, metres. */
export function distance(a: LonLat, b: LonLat): number {
  const phi1 = a[1] * RADIANS;
  const phi2 = b[1] * RADIANS;
  const dPhi = phi2 - phi1;
  const dLambda = (b[0] - a[0]) * RADIANS;
  const h = Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  return 2 * EARTH_RADIUS * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** The length of a path through `points`, metres. */
export function pathLength(points: readonly LonLat[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i += 1) total += distance(points[i - 1]!, points[i]!);
  return total;
}

/** `lon` moved by whole turns to lie within half a turn of `reference`, so
 * a path keeps going the short way across the antimeridian. */
export function unwrapLongitude(lon: number, reference: number): number {
  return lon + 360 * Math.round((reference - lon) / 360);
}

/** The great circle from `a` to `b` as points, `a` first and `b` last,
 * longitudes continuous from `a`'s. */
export function greatCircle(a: LonLat, b: LonLat): LonLat[] {
  const d = distance(a, b);
  const steps = Math.min(MAX_STEPS, Math.max(1, Math.ceil(d / STEP_METRES)));
  if (steps === 1) return [a, [unwrapLongitude(b[0], a[0]), b[1]]];
  const delta = d / EARTH_RADIUS;
  const vector = ([lon, lat]: LonLat): [number, number, number] => [
    Math.cos(lat * RADIANS) * Math.cos(lon * RADIANS),
    Math.cos(lat * RADIANS) * Math.sin(lon * RADIANS),
    Math.sin(lat * RADIANS),
  ];
  const [ax, ay, az] = vector(a);
  const [bx, by, bz] = vector(b);
  const out: LonLat[] = [a];
  let previous = a[0];
  for (let i = 1; i < steps; i += 1) {
    const f = i / steps;
    const wa = Math.sin((1 - f) * delta) / Math.sin(delta);
    const wb = Math.sin(f * delta) / Math.sin(delta);
    const x = wa * ax + wb * bx;
    const y = wa * ay + wb * by;
    const z = wa * az + wb * bz;
    const lon = unwrapLongitude(Math.atan2(y, x) / RADIANS, previous);
    out.push([lon, Math.atan2(z, Math.hypot(x, y)) / RADIANS]);
    previous = lon;
  }
  out.push([unwrapLongitude(b[0], previous), b[1]]);
  return out;
}

/** A path through `points` along great circles, closed back to the first
 * point when `closed`. */
export function densify(points: readonly LonLat[], closed = false): LonLat[] {
  const ring = closed && points.length > 2 ? [...points, points[0]!] : [...points];
  if (ring.length < 2) return ring;
  const out: LonLat[] = [ring[0]!];
  for (let i = 1; i < ring.length; i += 1) {
    const from = out[out.length - 1]!;
    out.push(...greatCircle(from, ring[i]!).slice(1));
  }
  return out;
}

/** The area a ring encloses on the sphere, square metres, whichever way it
 * winds (the line-integral form for a ring of short edges, which is why it
 * is taken over the densified ring). */
export function ringArea(points: readonly LonLat[]): number {
  if (points.length < 3) return 0;
  const ring = densify(points, true);
  let total = 0;
  // The densified ring repeats its first point last.
  const n = ring.length - 1;
  for (let i = 0; i < n; i += 1) {
    const before = ring[(i + n - 1) % n]!;
    const after = ring[(i + 1) % n]!;
    total += (after[0] - before[0]) * RADIANS * Math.sin(ring[i]![1] * RADIANS);
  }
  return Math.abs((total * EARTH_RADIUS * EARTH_RADIUS) / 2);
}

function grouped(value: number, digits: number): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A length as the ruler reads it: metres under a kilometre, a tenth of a
 * kilometre under a hundred, whole kilometres beyond. */
export function formatDistance(metres: number): string {
  if (metres < 1000) return `${Math.round(metres)} m`;
  const km = metres / 1000;
  return `${grouped(km, km < 100 ? 1 : 0)} km`;
}

/** An area as the tool reads it: square metres under a hectare, then km²
 * with fewer decimals as it grows. */
export function formatArea(squareMetres: number): string {
  if (squareMetres < 10_000) return `${grouped(Math.round(squareMetres), 0)} m²`;
  const km2 = squareMetres / 1e6;
  return `${grouped(km2, km2 < 10 ? 2 : km2 < 1000 ? 1 : 0)} km²`;
}

type MeasureProperties = { role: "edge" | "fill" | "vertex" | "label"; label?: string };

/** What the layers draw for a shape: its edges (to the pointer while it is
 * being drawn), an area's fill, the vertices, and the readings — a
 * distance's running total at each vertex past the first, an area's at the
 * pointer or, once closed, at its last vertex. */
export function measureFeatures(
  kind: MeasureKind,
  points: readonly LonLat[],
  cursor: LonLat | null,
): FeatureCollection<Geometry, MeasureProperties> {
  const features: Feature<Geometry, MeasureProperties>[] = [];
  const path = cursor ? [...points, cursor] : [...points];
  const closed = kind === "area";
  if (path.length >= 2) {
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: densify(path, closed) },
      properties: { role: "edge" },
    });
  }
  if (closed && path.length >= 3) {
    features.push({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [densify(path, true)] },
      properties: { role: "fill" },
    });
  }
  for (const point of points) {
    features.push({ type: "Feature", geometry: { type: "Point", coordinates: point }, properties: { role: "vertex" } });
  }
  const label = (at: LonLat, text: string): void => {
    features.push({ type: "Feature", geometry: { type: "Point", coordinates: at }, properties: { role: "label", label: text } });
  };
  if (kind === "distance") {
    let total = 0;
    for (let i = 1; i < path.length; i += 1) {
      total += distance(path[i - 1]!, path[i]!);
      label(path[i]!, formatDistance(total));
    }
  } else if (path.length >= 3) {
    label(path[path.length - 1]!, formatArea(ringArea(path)));
  }
  return { type: "FeatureCollection", features };
}

const SOURCE = "measure";
const LAYERS = {
  fill: "measure-fill",
  casing: "measure-casing",
  edge: "measure-edge",
  vertex: "measure-vertex",
  label: "measure-label",
} as const;

/** The layers, bottom to top, inked like a case's measured distances. */
export function measureLayerSpecs(
  darkGround: boolean,
): (FillLayerSpecification | LineLayerSpecification | CircleLayerSpecification | SymbolLayerSpecification)[] {
  const ink = contextInk(darkGround);
  return [
    {
      id: LAYERS.fill,
      type: "fill",
      source: SOURCE,
      filter: ["==", ["get", "role"], "fill"],
      paint: { "fill-color": ink.measure, "fill-opacity": 0.12 },
    },
    {
      id: LAYERS.casing,
      type: "line",
      source: SOURCE,
      filter: ["==", ["get", "role"], "edge"],
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": ink.halo, "line-width": 4 },
    },
    {
      id: LAYERS.edge,
      type: "line",
      source: SOURCE,
      filter: ["==", ["get", "role"], "edge"],
      layout: { "line-join": "round", "line-cap": "butt" },
      paint: { "line-color": ink.measure, "line-width": 1.8, "line-dasharray": [4, 3] },
    },
    {
      id: LAYERS.vertex,
      type: "circle",
      source: SOURCE,
      filter: ["==", ["get", "role"], "vertex"],
      paint: {
        "circle-radius": 3.5,
        "circle-color": ink.halo,
        "circle-stroke-color": ink.measure,
        "circle-stroke-width": 1.6,
      },
    },
    {
      id: LAYERS.label,
      type: "symbol",
      source: SOURCE,
      filter: ["==", ["get", "role"], "label"],
      layout: {
        "text-field": ["get", "label"],
        "text-font": ["Noto Sans Medium"],
        "text-size": 12,
        "text-anchor": "left",
        "text-offset": [0.8, 0],
        "text-allow-overlap": true,
        "text-ignore-placement": true,
      },
      paint: {
        "text-color": ink.measure,
        "text-halo-color": ink.halo,
        "text-halo-width": 1.6,
        "text-halo-blur": 0.4,
      },
    },
  ];
}

export class MeasureTool {
  private kind: MeasureKind | null = null;
  private points: LonLat[] = [];
  private cursor: LonLat | null = null;
  /** The shape is ended: the next click starts another. */
  private ended = false;
  private darkGround = false;
  private frame = 0;

  constructor(
    private readonly map: MaplibreMap,
    private readonly onArm: (kind: MeasureKind | null) => void,
  ) {}

  get armed(): MeasureKind | null {
    return this.kind;
  }

  /** Take clicks as `kind`, or put the tool away and its shape with it. */
  arm(kind: MeasureKind | null): void {
    const was = this.kind;
    this.kind = kind;
    this.points = [];
    this.cursor = null;
    this.ended = false;
    const container = this.map.getContainer();
    if (kind && !was) {
      // A double click ends a shape here; it must not also zoom.
      this.map.doubleClickZoom.disable();
      this.map.on("mousemove", this.move);
      this.map.getCanvasContainer().addEventListener("mouseleave", this.leave);
      window.addEventListener("keydown", this.key);
    } else if (!kind && was) {
      this.map.doubleClickZoom.enable();
      this.map.off("mousemove", this.move);
      this.map.getCanvasContainer().removeEventListener("mouseleave", this.leave);
      window.removeEventListener("keydown", this.key);
    }
    container.classList.toggle("measuring", kind !== null);
    if (kind) this.render();
    else this.remove();
    if (kind !== was) this.onArm(kind);
  }

  /** A click on the map: a vertex, the shape's end, or a new shape. Whether
   * the tool took it — while armed it takes every click, so the probe and
   * the marks under the pointer wait until it is put away. */
  click(event: MapMouseEvent): boolean {
    if (!this.kind) return false;
    if (this.ended) {
      this.points = [];
      this.ended = false;
    }
    const last = this.points[this.points.length - 1];
    if (last && this.near(last, event)) {
      this.end();
      return true;
    }
    const first = this.points[0];
    if (this.kind === "area" && first && this.points.length >= 3 && this.near(first, event)) {
      this.end();
      return true;
    }
    const lon = last ? unwrapLongitude(event.lngLat.lng, last[0]) : event.lngLat.lng;
    this.points.push([lon, event.lngLat.lat]);
    this.cursor = null;
    this.render();
    return true;
  }

  /** Repaint the colours for the ground. */
  setInk(darkGround: boolean): void {
    this.darkGround = darkGround;
    if (!this.map.getSource(SOURCE)) return;
    for (const layer of measureLayerSpecs(darkGround)) {
      for (const [name, value] of Object.entries(layer.paint ?? {})) {
        if (!name.endsWith("-color") || !this.map.getLayer(layer.id)) continue;
        this.map.setPaintProperty(layer.id, name as never, value as never);
      }
    }
  }

  private near(point: LonLat, event: MapMouseEvent): boolean {
    const at = this.map.project(point);
    return Math.hypot(at.x - event.point.x, at.y - event.point.y) <= SNAP_PIXELS;
  }

  private end(): void {
    const minimum = this.kind === "area" ? 3 : 2;
    if (this.points.length < minimum) this.points = [];
    this.ended = true;
    this.cursor = null;
    this.render();
  }

  private readonly move = (event: MapMouseEvent): void => {
    const last = this.points[this.points.length - 1];
    if (!last || this.ended) return;
    this.cursor = [unwrapLongitude(event.lngLat.lng, last[0]), event.lngLat.lat];
    this.schedule();
  };

  private readonly leave = (): void => {
    if (!this.cursor) return;
    this.cursor = null;
    this.schedule();
  };

  private readonly key = (event: KeyboardEvent): void => {
    if (!this.kind) return;
    if (event.key === "Escape") {
      if (this.points.length > 0 && !this.ended) this.end();
      else this.arm(null);
    } else if ((event.key === "Backspace" || event.key === "Delete") && !this.ended && this.points.length > 0) {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, [contenteditable]")) return;
      event.preventDefault();
      this.points.pop();
      if (this.points.length === 0) this.cursor = null;
      this.render();
    }
  };

  /** Coalesce pointer moves to one repaint a frame. */
  private schedule(): void {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.render();
    });
  }

  private render(): void {
    if (!this.kind) return;
    const data = measureFeatures(this.kind, this.points, this.ended ? null : this.cursor);
    const source = this.map.getSource(SOURCE) as { setData?: (data: unknown) => void } | undefined;
    if (source?.setData) {
      source.setData(data);
      return;
    }
    this.map.addSource(SOURCE, { type: "geojson", data });
    // On top of everything: what is being measured is what is looked at.
    for (const layer of measureLayerSpecs(this.darkGround)) this.map.addLayer(layer);
  }

  private remove(): void {
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
    for (const id of Object.values(LAYERS)) if (this.map.getLayer(id)) this.map.removeLayer(id);
    if (this.map.getSource(SOURCE)) this.map.removeSource(SOURCE);
  }
}
