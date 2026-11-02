/** The weather warnings in force, drawn as filled areas on the common 1–5
 * level (docs/alerts.md §2) over whatever field is on screen.
 *
 * A warning reaches the map one of two ways. One with its own polygon (an
 * NWS storm-based warning, every ECCC alert) is a feature of one GeoJSON
 * source. One that names areas by code (JMA, most NWS products) is drawn
 * from its network's boundary set, a PMTiles archive read tile by tile:
 * one vector source per set, one fill and one outline layer per layer of
 * the set, filtered to the codes in force and coloured by the highest
 * level any warning gives the code. Where a warning has both, its polygon
 * is the warned area and its codes only the zones it touches, so the
 * polygon alone is drawn — except for MeteoAlarm, whose codes are the
 * parts of the area the polygon does not draw, so both are.
 *
 * The warnings are a snapshot of what is in force now, not a forecast: the
 * playhead does not touch them. Rebuilding is per round (every few minutes
 * at most), never per frame. */

import type { Feature, FeatureCollection } from "geojson";
import type {
  ExpressionSpecification,
  FillLayerSpecification,
  GeoJSONSource,
  LineLayerSpecification,
  Map as MaplibreMap,
} from "maplibre-gl";

import { alertsAreasUrl, type LoadedAlertsIndex } from "./fetch";
import { pmtilesSourceUrl, registerPmtilesProtocol } from "./pmtiles";
import type { AlertLevel, AlertLine, AlertsNetwork } from "./schema";

const INLINE_SOURCE = "alerts-inline";
const INLINE_FILL = "alerts-inline-fill";
const INLINE_LINE = "alerts-inline-line";
const AREA_PREFIX = "alerts-area-";
const NONE = "rgba(0, 0, 0, 0)";

/** The five levels, statement to exceptional, one set per ground: a grey
 * statement under the yellow → orange → red ramp warning services share,
 * and level 5 — JMA's 特別警報 alone — a violet no other level is near. */
export const ALERT_LEVEL_COLORS: Record<"light" | "dark", readonly [string, string, string, string, string]> = {
  light: ["#7d8ca3", "#e2b100", "#ee7316", "#d42638", "#7a2bc0"],
  dark: ["#9fadc2", "#f5cf3c", "#ff9440", "#ff4f60", "#b878ff"],
};

const FILL_OPACITY = 0.3;
const LINE_OPACITY = 0.85;

/** NWS fire weather products resolve against the fire weather zones, which
 * share the public zones' codes but not their shapes (docs/alerts.md §9.1). */
const NWS_FIRE_EVENTS = new Set(["Red Flag Warning", "Fire Weather Watch", "Extreme Fire Danger", "Fire Warning"]);

export function alertLevelColor(level: AlertLevel, darkGround: boolean): string {
  return ALERT_LEVEL_COLORS[darkGround ? "dark" : "light"][level - 1]!;
}

/** Networks whose codes name the parts of a warning's area its polygon
 * does not draw (docs/alerts.md §6), rather than the zones it touches. */
const AREAS_BESIDE_GEOMETRY = new Set(["meteoalarm"]);

/** The layers of a boundary set a warning's codes are drawn on. */
export function areaLayersFor(set: string, layers: readonly string[], line: AlertLine): string[] {
  if (set !== "nws-zones") return [...layers];
  const fire = NWS_FIRE_EVENTS.has(line.native.event);
  return layers.filter((layer) => (layer === "fire") === fire);
}

/** One layer of one boundary set and what is in force on it. */
export interface AreaTarget {
  /** The network position the set belongs to. */
  network: number;
  set: string;
  version: string;
  sourceLayer: string;
  /** Each code's highest level. */
  levels: Map<string, AlertLevel>;
  /** Each code's warnings, by id. */
  alerts: Map<string, string[]>;
}

/** Every layer of every boundary set with codes in force, keyed by
 * `<set>.<version>/<layer>`. Pure: the tests read it without a map. */
export function areaTargets(
  networks: readonly AlertsNetwork[],
  linesByNetwork: readonly (readonly AlertLine[])[],
): Map<string, AreaTarget> {
  const targets = new Map<string, AreaTarget>();
  networks.forEach((network, position) => {
    const areas = network.areas;
    if (!areas) return;
    for (const line of linesByNetwork[position] ?? []) {
      if (line.areas.length === 0 || (line.geometry !== null && !AREAS_BESIDE_GEOMETRY.has(network.id))) continue;
      for (const sourceLayer of areaLayersFor(areas.set, areas.layers, line)) {
        const key = `${areas.set}.${areas.version}/${sourceLayer}`;
        let target = targets.get(key);
        if (!target) {
          target = { network: position, set: areas.set, version: areas.version, sourceLayer, levels: new Map(), alerts: new Map() };
          targets.set(key, target);
        }
        for (const code of line.areas) {
          target.levels.set(code, Math.max(target.levels.get(code) ?? 0, line.level) as AlertLevel);
          const ids = target.alerts.get(code);
          if (ids) ids.push(line.id);
          else target.alerts.set(code, [line.id]);
        }
      }
    }
  });
  return targets;
}

/** The warnings with their own polygons, one feature per part, each
 * carrying the id the card looks them up by and the level they are
 * coloured by. A part is its own feature because MapLibre's hit test
 * counts crossings over every ring of a feature at once: a point inside
 * two overlapping parts of one MultiPolygon (MeteoAlarm sends such) reads
 * as outside it, though it is drawn. */
export function inlineFeatures(lines: readonly AlertLine[]): FeatureCollection {
  const features: Feature[] = [];
  for (const line of lines) {
    if (line.geometry === null) continue;
    for (const polygon of line.geometry) {
      features.push({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: polygon },
        properties: { alert: line.id, level: line.level },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

function levelMatch(darkGround: boolean): ExpressionSpecification {
  const colors = ALERT_LEVEL_COLORS[darkGround ? "dark" : "light"];
  return ["match", ["get", "level"], 1, colors[0], 2, colors[1], 3, colors[2], 4, colors[3], 5, colors[4], NONE];
}

const CODE: ExpressionSpecification = ["to-string", ["get", "id"]];

/** A boundary layer's colour: a match of each code to its level's colour,
 * the codes grouped by level (a match label may appear only once). */
export function areaColorExpression(target: AreaTarget, darkGround: boolean): ExpressionSpecification {
  const byLevel: string[][] = [[], [], [], [], []];
  for (const [code, level] of target.levels) byLevel[level - 1]!.push(code);
  const branches: (string | string[])[] = [];
  byLevel.forEach((codes, at) => {
    if (codes.length) branches.push(codes.sort(), alertLevelColor((at + 1) as AlertLevel, darkGround));
  });
  return ["match", CODE, ...branches, NONE] as ExpressionSpecification;
}

export function areaFilter(target: AreaTarget): ExpressionSpecification {
  return ["in", CODE, ["literal", [...target.levels.keys()].sort()]];
}

function areaSourceId(target: AreaTarget): string {
  return `${AREA_PREFIX}${target.set}.${target.version}`;
}

function areaLayerId(target: AreaTarget, part: "fill" | "line"): string {
  return `${AREA_PREFIX}${target.set}-${target.sourceLayer}-${part}`;
}

/** One warning under the pointer and the network that issued it. */
export interface AlertHit {
  line: AlertLine;
  network: AlertsNetwork;
}

/** Highest level first, then the newest message. */
export function sortHits(hits: AlertHit[]): AlertHit[] {
  return hits.sort((a, b) => b.line.level - a.line.level || (a.line.sent < b.line.sent ? 1 : a.line.sent > b.line.sent ? -1 : 0));
}

export class AlertLayers {
  private darkGround = false;
  private before: string | undefined;
  private loaded: LoadedAlertsIndex | null = null;
  private byId = new Map<string, AlertHit>();
  private targets = new Map<string, AreaTarget>();
  /** Fill layer id → its target, for the hit test. */
  private fillTargets = new Map<string, AreaTarget>();
  private layerIds: string[] = [];
  private sourceIds = new Set<string>();

  constructor(private readonly map: MaplibreMap) {
    registerPmtilesProtocol();
  }

  /** Where the layers go: under the basemap's first symbol layer. */
  setBefore(before: string | undefined): void {
    this.before = before;
  }

  /** A round and its networks' lines (`lines[i]` network `i`'s, empty
   * when not read), or null to take the warnings off the map. */
  setData(loaded: LoadedAlertsIndex | null, lines: readonly (readonly AlertLine[])[]): void {
    this.loaded = loaded;
    this.byId.clear();
    if (loaded) {
      loaded.index.networks.forEach((network, position) => {
        for (const line of lines[position] ?? []) this.byId.set(line.id, { line, network });
      });
    }
    this.targets = loaded ? areaTargets(loaded.index.networks, lines) : new Map();
    this.rebuild(loaded ? lines.flat() : []);
  }

  setInk(darkGround: boolean): void {
    if (darkGround === this.darkGround) return;
    this.darkGround = darkGround;
    for (const [id, target] of this.fillTargets) {
      if (!this.map.getLayer(id)) continue;
      const color = areaColorExpression(target, darkGround);
      this.map.setPaintProperty(id, "fill-color", color);
      this.map.setPaintProperty(areaLayerId(target, "line"), "line-color", color);
    }
    if (this.map.getLayer(INLINE_FILL)) {
      this.map.setPaintProperty(INLINE_FILL, "fill-color", levelMatch(darkGround));
      this.map.setPaintProperty(INLINE_LINE, "line-color", levelMatch(darkGround));
    }
  }

  /** Every warning drawn under a screen point, highest level first. */
  alertsAt(point: { x: number; y: number }): AlertHit[] {
    const layers = [...this.fillTargets.keys(), INLINE_FILL].filter((id) => this.map.getLayer(id));
    if (!layers.length) return [];
    const box: [[number, number], [number, number]] = [
      [point.x - 2, point.y - 2],
      [point.x + 2, point.y + 2],
    ];
    const ids = new Set<string>();
    for (const feature of this.map.queryRenderedFeatures(box, { layers })) {
      const properties = feature.properties as Record<string, unknown> | null;
      if (feature.layer.id === INLINE_FILL) {
        if (typeof properties?.alert === "string") ids.add(properties.alert);
        continue;
      }
      const target = this.fillTargets.get(feature.layer.id);
      const code = properties?.id;
      if (!target || code === undefined || code === null) continue;
      for (const id of target.alerts.get(String(code)) ?? []) ids.add(id);
    }
    return sortHits([...ids].map((id) => this.byId.get(id)).filter((hit): hit is AlertHit => hit !== undefined));
  }

  remove(): void {
    this.setData(null, []);
  }

  private rebuild(lines: readonly AlertLine[]): void {
    const map = this.map;
    for (const id of this.layerIds) if (map.getLayer(id)) map.removeLayer(id);
    this.layerIds = [];
    this.fillTargets.clear();
    const wantedSources = new Set<string>();
    const loaded = this.loaded;
    if (loaded) {
      for (const target of this.targets.values()) {
        const sourceId = areaSourceId(target);
        wantedSources.add(sourceId);
        if (!map.getSource(sourceId)) {
          const areas = loaded.index.networks[target.network]!.areas!;
          map.addSource(sourceId, { type: "vector", url: pmtilesSourceUrl(alertsAreasUrl(loaded, areas)) });
        }
        const color = areaColorExpression(target, this.darkGround);
        const fill: FillLayerSpecification = {
          id: areaLayerId(target, "fill"),
          type: "fill",
          source: sourceId,
          "source-layer": target.sourceLayer,
          filter: areaFilter(target),
          paint: { "fill-color": color, "fill-opacity": FILL_OPACITY },
        };
        const line: LineLayerSpecification = {
          id: areaLayerId(target, "line"),
          type: "line",
          source: sourceId,
          "source-layer": target.sourceLayer,
          filter: areaFilter(target),
          paint: { "line-color": color, "line-width": 0.8, "line-opacity": LINE_OPACITY },
        };
        map.addLayer(fill, this.before);
        map.addLayer(line, this.before);
        this.layerIds.push(fill.id, line.id);
        this.fillTargets.set(fill.id, target);
      }
      const features = inlineFeatures(lines);
      wantedSources.add(INLINE_SOURCE);
      const inline = map.getSource(INLINE_SOURCE) as GeoJSONSource | undefined;
      if (inline) inline.setData(features);
      else map.addSource(INLINE_SOURCE, { type: "geojson", data: features });
      map.addLayer(
        {
          id: INLINE_FILL,
          type: "fill",
          source: INLINE_SOURCE,
          layout: { "fill-sort-key": ["get", "level"] },
          paint: { "fill-color": levelMatch(this.darkGround), "fill-opacity": FILL_OPACITY },
        },
        this.before,
      );
      map.addLayer(
        {
          id: INLINE_LINE,
          type: "line",
          source: INLINE_SOURCE,
          layout: { "line-sort-key": ["get", "level"] },
          paint: { "line-color": levelMatch(this.darkGround), "line-width": 1.2, "line-opacity": LINE_OPACITY },
        },
        this.before,
      );
      this.layerIds.push(INLINE_FILL, INLINE_LINE);
    }
    for (const id of this.sourceIds) if (!wantedSources.has(id) && map.getSource(id)) map.removeSource(id);
    this.sourceIds = wantedSources;
  }
}
