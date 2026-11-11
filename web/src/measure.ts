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
 * A length is the WGS84 ellipsoid's (`geodesy.ts`): the horizontal distance
 * along the sea-level surface, what a chart or a route reads, not a path
 * over the hills. Areas are on the sphere of the ellipsoid's area. The
 * edges are drawn as great circles, within a hair of the ellipsoid's
 * geodesics at the map's scale, so a line across an ocean bows on the
 * Mercator map as the distance it reads says it should.
 *
 * A finished distance also reads the ground under it off the DEM
 * (`terrain.ts`): the length along the surface with the climb and the
 * descent, and the straight line between its two ends at their heights.
 * A surface length depends on the resolution it is sampled at — the finer
 * the DEM, the longer the path — so the reading names the resolution.
 * With the 3D terrain on, a card at its end draws the profile itself
 * (`measureprofile.ts`), and the point under the pointer is marked on the
 * line.
 *
 * The geometry is plain functions so the tests can read it without a map. */

import type { Feature, FeatureCollection, Geometry } from "geojson";
import type {
  Popup,
  CircleLayerSpecification,
  FillLayerSpecification,
  LineLayerSpecification,
  Map as MaplibreMap,
  MapMouseEvent,
  SymbolLayerSpecification,
} from "maplibre-gl";

import { cardPopup } from "./cardpopup";
import { contextInk } from "./casecontext";
import { profileChart, type ProfilePoint } from "./measureprofile";
import { AUTHALIC_RADIUS, geodesicDistance, straightLineDistance } from "./geodesy";
import { onLocaleChange, t } from "./i18n";
import { demElevationsAt, demPixelMetres, TERRAIN_MAX_ZOOM } from "./terrain";
import { convertDistance, convertHeight, formatHeight, onUnitsChange, UNIT_LABELS, units } from "./units";

export type MeasureKind = "distance" | "area";

/** A longitude/latitude pair. */
export type LonLat = [number, number];

/** The mean Earth radius (IUGG), metres: the sphere the edges are drawn
 * on. */
export const EARTH_RADIUS = 6371008.8;

/** A great-circle edge is drawn in steps no longer than this. */
const STEP_METRES = 50_000;
const MAX_STEPS = 256;

/** Within this many screen pixels a click is on a vertex. */
export const SNAP_PIXELS = 8;

const RADIANS = Math.PI / 180;

/** The ellipsoid distance between two points, metres. */
export function distance(a: LonLat, b: LonLat): number {
  return geodesicDistance(a[0], a[1], b[0], b[1]);
}

/** The great-circle angle between two points, radians: what the drawn
 * edges are interpolated along. */
function arc(a: LonLat, b: LonLat): number {
  const phi1 = a[1] * RADIANS;
  const phi2 = b[1] * RADIANS;
  const h = Math.sin((phi2 - phi1) / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(((b[0] - a[0]) * RADIANS) / 2) ** 2;
  return 2 * Math.asin(Math.min(1, Math.sqrt(h)));
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
export function greatCircle(a: LonLat, b: LonLat, stepMetres = STEP_METRES, maxSteps = MAX_STEPS): LonLat[] {
  const delta = arc(a, b);
  const steps = Math.min(maxSteps, Math.max(1, Math.ceil((delta * EARTH_RADIUS) / stepMetres)));
  if (steps === 1) return [a, [unwrapLongitude(b[0], a[0]), b[1]]];
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

/** The area a ring encloses on the authalic sphere, square metres (within
 * a fraction of a percent of the ellipsoid's), whichever way it
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
  return Math.abs((total * AUTHALIC_RADIUS * AUTHALIC_RADIUS) / 2);
}

function grouped(value: number, digits: number): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A length as the ruler reads it, in the viewer's distance unit: the
 * short unit (metres, or feet in miles and nautical miles) under a tenth
 * of the long one — under a kilometre in kilometres — a tenth of the long
 * unit under a hundred, whole ones beyond. */
export function formatDistance(metres: number): string {
  const unit = units.distance;
  const long = convertDistance(metres / 1000);
  if (unit === "km" ? metres < 1000 : long < 0.1) return `${grouped(Math.round(shortLength(metres)), 0)} ${shortLabel()}`;
  return `${grouped(long, long < 100 ? 1 : 0)} ${UNIT_LABELS.distance[unit]}`;
}

/** An area as the tool reads it: the short unit squared under a hectare's
 * worth, then the long unit squared with fewer decimals as it grows. */
export function formatArea(squareMetres: number): string {
  if (squareMetres < 10_000) return `${grouped(Math.round(shortLength(Math.sqrt(squareMetres)) ** 2), 0)} ${shortLabel()}²`;
  const long = convertDistance(Math.sqrt(squareMetres) / 1000) ** 2;
  return `${grouped(long, long < 10 ? 2 : long < 1000 ? 1 : 0)} ${UNIT_LABELS.distance[units.distance]}²`;
}

/** Metres, or feet where distances read in miles or nautical miles. */
function shortLength(metres: number): number {
  return units.distance === "km" ? metres : convertHeight(metres, "ft");
}

function shortLabel(): string {
  return units.distance === "km" ? "m" : "ft";
}

/** What a finished distance reads off the DEM: the length along the
 * ground, the climb and the descent on the way, and the straight line
 * between its two ends at their heights (null where the DEM has no height
 * for one), all in metres, and the DEM resolution they were sampled at. */
export interface GroundProfile {
  surface: number;
  climb: number;
  descent: number;
  straight: number | null;
  resolution: number;
  /** Every sample, with the horizontal distance to it along the line. */
  track: ProfilePoint[];
}

/** At most this many heights in a profile, and about this many DEM tiles. */
const MAX_PROFILE_SAMPLES = 4000;
const MAX_PROFILE_TILES = 24;

/** The points a profile of `points` reads, one a DEM pixel at the finest
 * zoom that keeps it within the sample and tile budget, and that zoom and
 * the spacing. A longer line is read off a coarser DEM, and says so. */
export function profilePlan(points: readonly LonLat[]): { samples: LonLat[]; zoom: number; resolution: number } {
  const length = pathLength(points);
  const latitude = points.reduce((sum, point) => sum + point[1], 0) / Math.max(1, points.length);
  let zoom = TERRAIN_MAX_ZOOM;
  for (; zoom > 0; zoom -= 1) {
    const count = length / demPixelMetres(zoom, latitude);
    if (count <= MAX_PROFILE_SAMPLES && count / 512 + points.length <= MAX_PROFILE_TILES) break;
  }
  const resolution = Math.max(demPixelMetres(zoom, latitude), length / MAX_PROFILE_SAMPLES);
  const samples: LonLat[] = [];
  for (let i = 1; i < points.length; i += 1) {
    const run = greatCircle(samples[samples.length - 1] ?? points[0]!, points[i]!, resolution, MAX_PROFILE_SAMPLES);
    samples.push(...(samples.length === 0 ? run : run.slice(1)));
  }
  return { samples, zoom, resolution };
}

/** A profile from heights read at `samples`: each step's ground length is
 * the hypotenuse of its horizontal length and its rise. A sample the DEM
 * has no height for keeps the last one's, so a hole in the tiles reads as
 * level ground rather than a cliff. */
export function profileFromHeights(samples: readonly LonLat[], heights: readonly (number | null)[], resolution: number): GroundProfile {
  let surface = 0;
  let climb = 0;
  let descent = 0;
  let previous: number | null = null;
  let along = 0;
  const track: ProfilePoint[] = [];
  for (let i = 0; i < samples.length; i += 1) {
    const height: number | null = heights[i] ?? previous;
    if (i > 0) {
      const run = distance(samples[i - 1]!, samples[i]!);
      along += run;
      const rise = height !== null && previous !== null ? height - previous : 0;
      surface += Math.hypot(run, rise);
      if (rise > 0) climb += rise;
      else descent -= rise;
    }
    if (height !== null) track.push({ lon: samples[i]![0], lat: samples[i]![1], along, height });
    previous = height;
  }
  const first = samples[0];
  const last = samples[samples.length - 1];
  const start = heights[0] ?? null;
  const end = heights[heights.length - 1] ?? null;
  // The DEM's heights are above the geoid, not the ellipsoid; between two
  // ends the geoid differs by metres, which moves the chord by less.
  const straight =
    first && last && start !== null && end !== null
      ? straightLineDistance({ lon: first[0], lat: first[1], height: start }, { lon: last[0], lat: last[1], height: end })
      : null;
  return { surface, climb, descent, straight, resolution, track };
}

/** The ground under a path, read off the DEM. */
export async function groundProfile(points: readonly LonLat[]): Promise<GroundProfile> {
  const { samples, zoom, resolution } = profilePlan(points);
  const heights = await demElevationsAt(
    samples.map(([longitude, latitude]) => ({ latitude, longitude })),
    zoom,
  );
  // Mapterhorn publishes no tile over the open sea, so a missing height is
  // the sea surface; a ground below it (the Dead Sea) has its own tile.
  return profileFromHeights(samples, heights.map((height) => height ?? 0), resolution);
}

/** The lines a finished distance's reading adds under its length. */
export function profileLines(profile: GroundProfile): string[] {
  const resolution = `${grouped(Math.round(shortLength(profile.resolution)), 0)} ${shortLabel()}`;
  const lines = [t("measureSurface", { length: formatDistance(profile.surface), climb: formatHeight(profile.climb), descent: formatHeight(profile.descent) })];
  if (profile.straight !== null) lines.push(t("measureStraight", { length: formatDistance(profile.straight) }));
  lines.push(t("measureDem", { resolution }));
  return lines;
}

type MeasureProperties = { role: "edge" | "fill" | "vertex" | "label" | "hover"; label?: string };

/** What the layers draw for a shape: its edges (to the pointer while it is
 * being drawn), an area's fill, the vertices, and the readings — a
 * distance's running total at each vertex past the first, an area's at the
 * pointer or, once closed, at its last vertex. */
export function measureFeatures(
  kind: MeasureKind,
  points: readonly LonLat[],
  cursor: LonLat | null,
  profile: GroundProfile | null = null,
  hover: LonLat | null = null,
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
      const last = i === path.length - 1;
      label(path[i]!, last && profile ? [formatDistance(total), ...profileLines(profile)].join("\n") : formatDistance(total));
    }
  } else if (path.length >= 3) {
    label(path[path.length - 1]!, formatArea(ringArea(path)));
  }
  if (hover) features.push({ type: "Feature", geometry: { type: "Point", coordinates: hover }, properties: { role: "hover" } });
  return { type: "FeatureCollection", features };
}

const SOURCE = "measure";
const LAYERS = {
  fill: "measure-fill",
  casing: "measure-casing",
  edge: "measure-edge",
  vertex: "measure-vertex",
  hover: "measure-hover",
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
      id: LAYERS.hover,
      type: "circle",
      source: SOURCE,
      filter: ["==", ["get", "role"], "hover"],
      paint: {
        "circle-radius": 5,
        "circle-color": ink.measure,
        "circle-stroke-color": ink.halo,
        "circle-stroke-width": 2,
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
        // A finished distance reads on several lines of its own; none wraps.
        "text-max-width": 40,
        "text-justify": "left",
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
  /** The ground under the finished distance, once read; `generation`
   * drops a reading that comes back for a shape since changed. */
  private profile: GroundProfile | null = null;
  private generation = 0;
  /** The profile card at the line's end, open while the 3D terrain is on,
   * and the sample its pointer is over. */
  private card: Popup | null = null;
  private hover: LonLat | null = null;

  constructor(
    private readonly map: MaplibreMap,
    private readonly onArm: (kind: MeasureKind | null) => void,
  ) {
    // The readings are written in the viewer's distance unit, and the
    // profile's words in the viewer's language.
    onUnitsChange(() => {
      this.render();
      this.syncCard(true);
    });
    onLocaleChange(() => {
      this.render();
      this.syncCard(true);
    });
    // The profile is drawn while the ground is: the terrain switch opens
    // and closes it.
    map.on("terrain", () => this.syncCard());
  }

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
    this.forgetProfile();
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
      this.forgetProfile();
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
    if (this.kind === "distance" && this.points.length >= 2) {
      const generation = ++this.generation;
      groundProfile(this.points)
        .then((profile) => {
          if (generation !== this.generation) return;
          this.profile = profile;
          this.render();
          this.syncCard();
        })
        // No DEM (offline, a tile server down): the length stands alone.
        .catch(() => undefined);
    }
  }

  private forgetProfile(): void {
    this.profile = null;
    this.generation += 1;
    this.syncCard();
  }

  /** Open the profile card at the line's end while there is a profile and
   * the 3D terrain is on, close it otherwise; `rebuild` redraws an open one
   * (a units or language change). */
  private syncCard(rebuild = false): void {
    const end = this.points[this.points.length - 1];
    const wanted = this.kind === "distance" && this.profile !== null && end !== undefined && this.map.getTerrain() !== null;
    if (!wanted) {
      this.card?.remove();
      this.card = null;
      this.setHover(null);
      return;
    }
    if (this.card && !rebuild) return;
    const chart = profileChart(this.profile!.track, {
      formatDistance,
      formatHeight: (metres) => formatHeight(metres),
      onHover: (point) => this.setHover(point ? [point.lon, point.lat] : null),
    });
    if (this.card) {
      this.card.setDOMContent(chart);
      return;
    }
    const card = cardPopup({ className: "station-popup measure-popup", maxWidth: "320px", offset: 14 }).setLngLat(end!).setDOMContent(chart);
    card.on("close", () => {
      if (this.card === card) this.card = null;
      this.setHover(null);
    });
    this.card = card.addTo(this.map);
  }

  private setHover(point: LonLat | null): void {
    if (this.hover === point) return;
    this.hover = point;
    this.schedule();
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
    const data = measureFeatures(this.kind, this.points, this.ended ? null : this.cursor, this.ended ? this.profile : null, this.hover);
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
