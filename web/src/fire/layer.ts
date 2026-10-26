/** The active fire marks: one clustered MapLibre GeoJSON source of FIRMS
 * detections over whatever fill and lines are on screen.
 *
 * The product holds seven days; the map shows the `FIRE_SPAN_HOURS` ending
 * at the playhead. A day of the world's fires is ~180 000 points, so the
 * reads are per sensor and per span — one range request each, over a span
 * wider than the one shown (`FETCH_MARGIN_HOURS` either side), so a
 * playhead stepping through a run fetches again only when it walks out of
 * what was read. Past the window's end (every forecast frame) the span is
 * pinned to the end and never moves.
 *
 * The features are rebuilt only when the playhead's hour changes inside the
 * window; within the hour the detections after the playhead are hidden by
 * a filter and the age colors are paint expressions over a `time`
 * property. Clustering is the source's: below `CLUSTER_MAX_ZOOM` the points
 * merge into circles sized by their summed FRP and colored by their newest
 * detection, which keeps a global view to a few hundred circles.
 *
 * A case tells its fire's story instead (`setHistory`): every detection
 * from the window's start up to the playhead stays on the map, so the
 * ground burned so far reads as a whole, and the marks are colored by
 * overpass rather than by age. The polar orbiters see a place in two bursts
 * a day, minutes apart within a burst and half a day between them, so the
 * newest pass is the fire line now, the pass before it the line half a day
 * ago, and the rest the scar. Detections appear at their own acquisition
 * time as the playhead sweeps between frames, which in a case is cheap: the
 * features are rebuilt only when the playhead crosses a detection time. */

import type { Feature, FeatureCollection, Point } from "geojson";
import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  GeoJSONSource,
  GeoJSONSourceSpecification,
  Map as MaplibreMap,
} from "maplibre-gl";

import { NEUTRAL_DARK_GROUND, NEUTRAL_LIGHT_GROUND } from "../tc/agencies";
import { fetchFireLines, type LoadedFireIndex } from "./fetch";
import type { FireLine, FireSensor } from "./schema";

/** How much of the window the map shows: the day ending at the playhead. */
export const FIRE_SPAN_HOURS = 24;
/** Detections this recent are drawn at full heat; older ones cool to
 * `FIRE_SPAN_HOURS`. */
export const FIRE_HOT_HOURS = 6;
const FETCH_MARGIN_HOURS = 12;
export const CLUSTER_MAX_ZOOM = 6;

const HOUR_MS = 3600_000;
const SOURCE = "fire-detections";
const LAYERS = {
  cluster: "fire-cluster",
  point: "fire-point",
} as const;
const LAYER_IDS: readonly string[] = [LAYERS.cluster, LAYERS.point];
export const FIRE_CLICKABLE_LAYERS: readonly string[] = [LAYERS.point, LAYERS.cluster];

const HOT = "#ff2d14";
const WARM = "#e0621c";
const COOL = "#5a2a14";
/** A case's newest pass and the one before it. Yellow and red-orange, not
 * the age ramp's browns: over the false-colour pictures a case opens on, a
 * burn scar is itself red-brown, and a brown mark on it disappears. */
const LATEST = "#ffd84a";
const PREVIOUS = "#ff4d1f";
/** Older passes: the scar. A dark burnt red that melts into the scar a
 * false-colour picture already shows and reads as one on a basemap; a
 * light stipple over a whole scar reads as cloud. */
const SCAR = "#8c2a12";

/** Two detections further apart than this belong to different overpass
 * bursts: the orbiters in one burst follow each other by tens of minutes
 * (and Aqua or Terra an hour or two after the VIIRS three), and the next
 * burst comes half a day later. */
export const PASS_GAP_MS = 3 * HOUR_MS;

/** What a clicked detection's card shows. */
export interface FireDetection {
  sensor: FireSensor;
  lat: number;
  lon: number;
  time: number;
  frp: number;
  bt: number;
  conf: string;
  dn: string;
}

/** The span of hours to show for a playhead: the `FIRE_SPAN_HOURS` ending
 * at the playhead's hour, pinned inside the product's window. Epoch ms,
 * `[from, to)`. */
export function shownSpan(
  playhead: number,
  window: { start: string; end: string },
): { from: number; to: number } {
  const start = Date.parse(window.start);
  // The newest granule can be acquired after the round's hour (§4), so the
  // window's end is an hour past `end` for what can hold a detection.
  const end = Date.parse(window.end) + HOUR_MS;
  const hour = Math.floor(playhead / HOUR_MS) * HOUR_MS;
  const to = Math.min(Math.max(hour + HOUR_MS, start + HOUR_MS), end);
  return { from: Math.max(to - FIRE_SPAN_HOURS * HOUR_MS, start), to };
}

/** The span to read for a shown span: wider, so the next steps of a
 * playhead fall inside it. */
export function fetchSpan(shown: { from: number; to: number }): { from: number; to: number } {
  return { from: shown.from - FETCH_MARGIN_HOURS * HOUR_MS, to: shown.to + FETCH_MARGIN_HOURS * HOUR_MS };
}

export function spanCovers(outer: { from: number; to: number }, inner: { from: number; to: number }): boolean {
  return outer.from <= inner.from && outer.to >= inner.to;
}

/** The detections of `lines` inside `[from, to)`, one feature each. The
 * sensor is its position in the index, so the card can name it. */
export function fireFeatures(
  lines: readonly { sensor: number; line: FireLine }[],
  from: number,
  to: number,
  passOf?: (time: number) => number,
): FeatureCollection<Point> {
  const features: Feature<Point>[] = [];
  for (const { sensor, line } of lines) {
    for (let i = 0; i < line.time.length; i++) {
      const time = line.time[i]!;
      if (time < from || time >= to) continue;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [line.lon[i]!, line.lat[i]!] },
        properties: {
          sensor,
          time,
          pass: passOf ? passOf(time) : -1,
          frp: line.frp[i]!,
          bt: line.bt[i]!,
          conf: line.conf[i]!,
          dn: line.dn[i]!,
        },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

/** The detection times of `lines`, ascending and distinct, each with the
 * number of the overpass burst it belongs to (0 for the first). */
export function passTimeline(lines: readonly { line: FireLine }[]): { times: number[]; passes: number[] } {
  const distinct = new Set<number>();
  for (const { line } of lines) for (const time of line.time) distinct.add(time);
  const times = [...distinct].sort((a, b) => a - b);
  const passes: number[] = [];
  let pass = 0;
  for (const [at, time] of times.entries()) {
    if (at > 0 && time - times[at - 1]! > PASS_GAP_MS) pass += 1;
    passes.push(pass);
  }
  return { times, passes };
}

/** The position in ascending `times` of the latest one not after `time`,
 * or -1 when every one is after it. */
export function latestNotAfter(times: readonly number[], time: number): number {
  let low = 0;
  let high = times.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (times[middle]! <= time) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

/** A case's mark colors: the newest pass, the pass before it, and the
 * scar. */
export function passColorExpression(latest: number, property: string, scar: string): ExpressionSpecification {
  return [
    "case",
    ["==", ["number", ["get", property]], latest],
    LATEST,
    ["==", ["number", ["get", property]], latest - 1],
    PREVIOUS,
    scar,
  ];
}

/** The heat a mark is drawn in: its age behind the playhead, hot within
 * `FIRE_HOT_HOURS`, cooling to brown at `FIRE_SPAN_HOURS`. */
export function ageColorExpression(time: number, property: string): ExpressionSpecification {
  return [
    "interpolate",
    ["linear"],
    ["-", time, ["number", ["get", property]]],
    FIRE_HOT_HOURS * HOUR_MS,
    HOT,
    (FIRE_HOT_HOURS + FIRE_SPAN_HOURS) / 2 * HOUR_MS,
    WARM,
    FIRE_SPAN_HOURS * HOUR_MS,
    COOL,
  ];
}

/** Hides what was acquired after the playhead, inside the playhead's hour. */
export function notAfterFilter(time: number): ExpressionSpecification {
  return ["all", ["!", ["has", "point_count"]], ["<=", ["number", ["get", "time"]], time]];
}

/** A night detection carries its ring: the same FRP reads hotter by day,
 * when the background is warm too. */
const NIGHT_RING: ExpressionSpecification = ["case", ["==", ["get", "dn"], "N"], 1, 0];

/** A mark's size by its FRP. */
const POINT_RADIUS: ExpressionSpecification = ["interpolate", ["linear"], ["get", "frp"], 0, 2.5, 10, 3.5, 100, 6, 1000, 10];

function layerSpecs(time: number, ink: string): CircleLayerSpecification[] {
  return [
    {
      id: LAYERS.cluster,
      type: "circle",
      source: SOURCE,
      filter: ["has", "point_count"],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["sqrt", ["get", "frpSum"]], 0, 5, 10, 7, 30, 11, 100, 18],
        "circle-color": ageColorExpression(time, "newest"),
        "circle-opacity": 0.75,
        "circle-stroke-color": ink,
        "circle-stroke-width": 0.6,
        "circle-stroke-opacity": 0.6,
      },
    },
    {
      id: LAYERS.point,
      type: "circle",
      source: SOURCE,
      filter: notAfterFilter(time),
      // A case's newest pass draws over the scar it is burning out of.
      layout: { "circle-sort-key": ["get", "pass"] },
      paint: {
        "circle-radius": POINT_RADIUS,
        "circle-color": ageColorExpression(time, "time"),
        "circle-opacity": 0.9,
        "circle-stroke-color": ink,
        "circle-stroke-width": NIGHT_RING,
      },
    },
  ];
}

function sourceSpec(data: FeatureCollection): GeoJSONSourceSpecification {
  return {
    type: "geojson",
    data,
    cluster: true,
    clusterMaxZoom: CLUSTER_MAX_ZOOM,
    clusterRadius: 30,
    clusterProperties: {
      frpSum: ["+", ["get", "frp"]],
      newest: ["max", ["get", "time"]],
      newestPass: ["max", ["get", "pass"]],
    },
  };
}

/** A paint/filter update is a repaint; the playhead is set every frame,
 * and a minute is below what the marks can show. */
const TIME_EPSILON_MS = 60_000;

/** The newest pass a case's marks are colored against, and the times
 * that change it. */
interface PassState {
  /** Every detection time read, ascending, and each one's pass. */
  times: number[];
  passes: number[];
  /** Position in `times` of the latest detection at or before the
   * playhead, -1 before the first. */
  at: number;
}

/** The newest pass at the playhead: when it began, the latest detection of
 * it so far, and where its detections sit (their mean position). */
export interface FirePass {
  start: number;
  until: number;
  center: [number, number];
}

export class FireLayers {
  private added = false;
  private ink = NEUTRAL_LIGHT_GROUND;
  private time = Date.now();
  private appliedTime: number | null = null;
  private loaded: LoadedFireIndex | null = null;
  /** A case's story mode (`setHistory`). */
  private history = false;
  private passState: PassState | null = null;
  /** What has been read, for which round and over which span. */
  private read: {
    key: string;
    indexUrl: string;
    span: { from: number; to: number };
    lines: { sensor: number; line: FireLine }[];
  } | null = null;
  private pending: string | null = null;
  /** A read that failed is not retried on every playhead move; a new
   * index, or a span it does not cover, asks again. */
  private failed: string | null = null;
  /** The span the features were built for. */
  private built: { key: string; from: number; to: number } | null = null;
  /** Told when the newest pass or its extent changes (the wind arrow). */
  onPassChange: (() => void) | null = null;

  constructor(
    private readonly map: MaplibreMap,
    private readonly onError: (error: unknown) => void,
  ) {}

  ensure(before?: string): void {
    if (this.added) return;
    if (!this.map.getSource(SOURCE)) this.map.addSource(SOURCE, sourceSpec({ type: "FeatureCollection", features: [] }));
    for (const layer of layerSpecs(this.time, this.ink))
      if (!this.map.getLayer(layer.id)) this.map.addLayer(layer, before);
    this.added = true;
    this.appliedTime = null;
    this.built = null;
    this.applyPaint();
    this.sync();
  }

  setInk(darkGround: boolean): void {
    this.ink = darkGround ? NEUTRAL_DARK_GROUND : NEUTRAL_LIGHT_GROUND;
    if (!this.added) return;
    for (const id of LAYER_IDS) this.map.setPaintProperty(id, "circle-stroke-color", this.ink);
  }

  /** Whether the marks tell a case's story: the whole window up to the
   * playhead, colored by pass. Off, they are the live product's day. */
  setHistory(on: boolean): void {
    if (this.history === on) return;
    this.history = on;
    this.read = null;
    this.pending = null;
    this.passState = null;
    this.built = null;
    this.appliedTime = null;
    this.applyPaint();
    this.sync();
  }

  /** The live index, or null to draw nothing. What was read for a round
   * stays while the switch is off, so turning it back on reads nothing
   * again; a new round reads afresh. */
  setIndex(loaded: LoadedFireIndex | null): void {
    if (loaded && this.read && this.read.indexUrl !== loaded.indexUrl) {
      this.read = null;
      this.passState = null;
    }
    if (loaded?.indexUrl !== this.loaded?.indexUrl) this.failed = null;
    this.loaded = loaded;
    this.sync();
  }

  setTime(time: number): void {
    this.time = time;
    if (this.history) {
      // Nothing changes on the map until the playhead crosses a detection.
      this.sync();
      return;
    }
    if (this.added && (this.appliedTime === null || Math.abs(time - this.appliedTime) >= TIME_EPSILON_MS)) {
      this.appliedTime = time;
      this.applyPaint();
    }
    this.sync();
  }

  /** The playhead swept between two frames: a case's marks follow it to
   * the minute; the live day keeps the frame's time. Whether it was taken. */
  sweep(time: number): boolean {
    if (!this.history) return false;
    this.setTime(time);
    return true;
  }

  /** The newest pass at the playhead, or null before the first detection,
   * with the marks off, and outside a case. */
  latestPass(): FirePass | null {
    const state = this.passState;
    if (!this.history || !this.loaded || !state || state.at < 0 || !this.read) return null;
    const pass = state.passes[state.at]!;
    let first = state.at;
    while (first > 0 && state.passes[first - 1] === pass) first -= 1;
    const start = state.times[first]!;
    const until = state.times[state.at]!;
    let lon = 0;
    let lat = 0;
    let count = 0;
    for (const { line } of this.read.lines) {
      for (let i = 0; i < line.time.length; i++) {
        const time = line.time[i]!;
        if (time < start || time > until) continue;
        lon += line.lon[i]!;
        lat += line.lat[i]!;
        count += 1;
      }
    }
    return count === 0 ? null : { start, until, center: [lon / count, lat / count] };
  }

  /** The detection a rendered point feature stands for; null for a
   * cluster, which has no single detection. */
  detectionOf(feature: { properties: Record<string, unknown> | null; geometry: { type: string } }): FireDetection | null {
    const properties = feature.properties;
    if (!properties || properties.point_count !== undefined || !this.loaded) return null;
    if (feature.geometry.type !== "Point") return null;
    const [lon, lat] = (feature.geometry as Point).coordinates as [number, number];
    const sensor = this.loaded.index.sensors[Number(properties.sensor)];
    if (!sensor) return null;
    return {
      sensor,
      lat,
      lon,
      time: Number(properties.time),
      frp: Number(properties.frp),
      bt: Number(properties.bt),
      conf: String(properties.conf),
      dn: String(properties.dn),
    };
  }

  /** The zoom a cluster opens at. */
  clusterZoom(clusterId: number): Promise<number> {
    const source = this.map.getSource(SOURCE) as GeoJSONSource | undefined;
    if (!source) return Promise.reject(new Error("fire source is gone"));
    return source.getClusterExpansionZoom(clusterId);
  }

  remove(): void {
    if (!this.added) return;
    for (const id of LAYER_IDS) if (this.map.getLayer(id)) this.map.removeLayer(id);
    if (this.map.getSource(SOURCE)) this.map.removeSource(SOURCE);
    this.added = false;
    this.built = null;
  }

  /** The colors, sizes and filter for the mode and the playhead: by age
   * for the live day, by pass for a case. */
  private applyPaint(): void {
    if (!this.added) return;
    if (!this.history) {
      const time = this.appliedTime ?? this.time;
      this.map.setPaintProperty(LAYERS.cluster, "circle-color", ageColorExpression(time, "newest"));
      this.map.setPaintProperty(LAYERS.point, "circle-color", ageColorExpression(time, "time"));
      this.map.setPaintProperty(LAYERS.point, "circle-radius", POINT_RADIUS);
      this.map.setPaintProperty(LAYERS.point, "circle-opacity", 0.9);
      this.map.setPaintProperty(LAYERS.point, "circle-stroke-width", NIGHT_RING);
      this.map.setFilter(LAYERS.point, notAfterFilter(time));
      return;
    }
    const state = this.passState;
    const latest = state && state.at >= 0 ? state.passes[state.at]! : -2;
    const scar = SCAR;
    this.map.setPaintProperty(LAYERS.cluster, "circle-color", passColorExpression(latest, "newestPass", scar));
    this.map.setPaintProperty(LAYERS.point, "circle-color", passColorExpression(latest, "pass", scar));
    this.map.setPaintProperty(LAYERS.point, "circle-radius", [
      "case",
      ["<", ["number", ["get", "pass"]], latest - 1],
      1.6,
      POINT_RADIUS,
    ]);
    this.map.setPaintProperty(LAYERS.point, "circle-opacity", [
      "case",
      ["<", ["number", ["get", "pass"]], latest - 1],
      0.6,
      0.95,
    ]);
    this.map.setPaintProperty(LAYERS.point, "circle-stroke-width", [
      "case",
      ["<", ["number", ["get", "pass"]], latest - 1],
      0,
      NIGHT_RING,
    ]);
    // The features already end at the playhead's latest detection.
    this.map.setFilter(LAYERS.point, ["!", ["has", "point_count"]]);
  }

  /** Read what the shown span needs when it is not read already, and
   * rebuild the features when the shown span moved. */
  private sync(): void {
    const loaded = this.loaded;
    if (!loaded) {
      if (this.built !== null) this.publish(null);
      return;
    }
    if (this.history) {
      this.syncHistory(loaded);
      return;
    }
    const shown = shownSpan(this.time, loaded.index.window);
    if (!this.read || !spanCovers(this.read.span, shown)) {
      this.fetch(loaded, fetchSpan(shown));
      // What was read before still draws what it covers until the new
      // read lands; the marks never wait.
    }
    if (this.read && (this.built?.key !== this.read.key || this.built.from !== shown.from || this.built.to !== shown.to))
      this.publish(shown);
  }

  /** A case reads its whole window once, then rebuilds the features only
   * when the playhead crosses a detection time. */
  private syncHistory(loaded: LoadedFireIndex): void {
    const window = { from: Date.parse(loaded.index.window.start), to: Date.parse(loaded.index.window.end) + HOUR_MS };
    if (!this.read || !spanCovers(this.read.span, window)) {
      this.fetch(loaded, window);
      return;
    }
    if (!this.passState || this.built?.key !== this.read.key) {
      const timeline = passTimeline(this.read.lines);
      this.passState = { ...timeline, at: -2 };
    }
    const state = this.passState;
    const at = latestNotAfter(state.times, this.time);
    if (at === state.at && this.built?.key === this.read.key) return;
    state.at = at;
    const to = at < 0 ? window.from : state.times[at]! + 1;
    if (!this.added) return;
    const source = this.map.getSource(SOURCE) as GeoJSONSource | undefined;
    const passOf = (time: number) => state.passes[latestNotAfter(state.times, time)]!;
    source?.setData(fireFeatures(this.read.lines, window.from, to, passOf));
    this.built = { key: this.read.key, from: window.from, to };
    this.applyPaint();
    this.onPassChange?.();
  }

  private fetch(loaded: LoadedFireIndex, span: { from: number; to: number }): void {
    const key = `${loaded.indexUrl}#${span.from}-${span.to}`;
    if (this.pending === key || this.failed === key) return;
    this.pending = key;
    const sensors = loaded.index.sensors.map((_, sensor) => sensor);
    Promise.all(
      sensors.map(async (sensor) =>
        (await fetchFireLines(loaded, sensor, span.from, span.to)).map((line) => ({ sensor, line })),
      ),
    )
      .then((perSensor) => {
        if (this.pending !== key || this.loaded !== loaded) return;
        this.pending = null;
        this.read = { key, indexUrl: loaded.indexUrl, span, lines: perSensor.flat() };
        this.sync();
      })
      .catch((error: unknown) => {
        if (this.pending === key) this.pending = null;
        this.failed = key;
        this.onError(error);
      });
  }

  private publish(shown: { from: number; to: number } | null): void {
    if (!this.added) return;
    const source = this.map.getSource(SOURCE) as GeoJSONSource | undefined;
    if (shown === null || !this.read) {
      source?.setData({ type: "FeatureCollection", features: [] });
      this.built = null;
      return;
    }
    source?.setData(fireFeatures(this.read.lines, shown.from, shown.to));
    this.built = { key: this.read.key, ...shown };
  }
}
