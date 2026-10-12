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
 * detection, which keeps a global view to a few hundred circles. */

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
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "frp"], 0, 2.5, 10, 3.5, 100, 6, 1000, 10],
        "circle-color": ageColorExpression(time, "time"),
        "circle-opacity": 0.9,
        "circle-stroke-color": ink,
        // A night detection carries its ring: the same FRP reads hotter by
        // day, when the background is warm too.
        "circle-stroke-width": ["case", ["==", ["get", "dn"], "N"], 1, 0],
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
    },
  };
}

/** A paint/filter update is a repaint; the playhead is set every frame,
 * and a minute is below what the marks can show. */
const TIME_EPSILON_MS = 60_000;

export class FireLayers {
  private added = false;
  private ink = NEUTRAL_LIGHT_GROUND;
  private time = Date.now();
  private appliedTime: number | null = null;
  private loaded: LoadedFireIndex | null = null;
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
    this.appliedTime = this.time;
    this.built = null;
    this.sync();
  }

  setInk(darkGround: boolean): void {
    this.ink = darkGround ? NEUTRAL_DARK_GROUND : NEUTRAL_LIGHT_GROUND;
    if (!this.added) return;
    for (const id of LAYER_IDS) this.map.setPaintProperty(id, "circle-stroke-color", this.ink);
  }

  /** The live index, or null to draw nothing. What was read for a round
   * stays while the switch is off, so turning it back on reads nothing
   * again; a new round reads afresh. */
  setIndex(loaded: LoadedFireIndex | null): void {
    if (loaded && this.read && this.read.indexUrl !== loaded.indexUrl) this.read = null;
    if (loaded?.indexUrl !== this.loaded?.indexUrl) this.failed = null;
    this.loaded = loaded;
    this.sync();
  }

  setTime(time: number): void {
    this.time = time;
    if (this.added && (this.appliedTime === null || Math.abs(time - this.appliedTime) >= TIME_EPSILON_MS)) {
      this.appliedTime = time;
      this.map.setPaintProperty(LAYERS.cluster, "circle-color", ageColorExpression(time, "newest"));
      this.map.setPaintProperty(LAYERS.point, "circle-color", ageColorExpression(time, "time"));
      this.map.setFilter(LAYERS.point, notAfterFilter(time));
    }
    this.sync();
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

  /** Read what the shown span needs when it is not read already, and
   * rebuild the features when the shown span moved. */
  private sync(): void {
    const loaded = this.loaded;
    if (!loaded) {
      if (this.built !== null) this.publish(null);
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
