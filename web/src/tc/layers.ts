/** The storm marks on the map: MapLibre's own GeoJSON layers over one
 * source, no shader of ours. Static geometry (the lines, the forecast
 * points, the ensemble members) is rebuilt when the set of storms or the
 * toggles change; the time-dependent part — the solid past of each
 * forecast, the current position, the wind radii at it and the name — is
 * rebuilt on every playhead move, from a few dozen points, so it stays
 * cheap enough to run at playback rate. */

import type { Feature, FeatureCollection, Geometry } from "geojson";
import type {
  ExpressionSpecification,
  FilterSpecification,
  GeoJSONSource,
  Map as MaplibreMap,
} from "maplibre-gl";

import {
  agencyCode,
  agencyColor,
  NEUTRAL_DARK_GROUND,
  NEUTRAL_LIGHT_GROUND,
  TC_BEST_AGENCY,
} from "./agencies";
import {
  isTcEnsemble,
  type TcPoint,
  type TcRadii,
  type TcStorm,
} from "./schema";
import {
  ensembleLines,
  forecastLine,
  modelTracks,
  pointTime,
  positionAt,
  radiiRing,
  unwrapLongitudes,
  type LonLat,
  type TrackPosition,
} from "./tracks";

export const TC_SOURCE = "tc-tracks";
const LAYERS = {
  radii: "tc-radii",
  radiiLine: "tc-radii-line",
  member: "tc-member",
  modelSolid: "tc-model-solid",
  modelDotted: "tc-model-dotted",
  best: "tc-best",
  agencyFuture: "tc-agency-future",
  agencyPast: "tc-agency-past",
  bestPoint: "tc-best-point",
  point: "tc-point",
  current: "tc-current",
  lineLabel: "tc-line-label",
  nameLabel: "tc-name-label",
} as const;

export const TC_LAYER_IDS: readonly string[] = Object.values(LAYERS);
/** The layers a click on the map may hit a storm point through, nearest
 * on top first. */
export const TC_CLICKABLE_LAYERS: readonly string[] = [
  LAYERS.current,
  LAYERS.point,
  LAYERS.bestPoint,
];

/** What a clicked point carries, as the feature's `data` property (a JSON
 * string: MapLibre flattens nested properties). */
export interface TcPointData {
  storm: string;
  name: string;
  /** `forecast` for an agency's forecast point, `best` for a best-track
   * point, `current` for the interpolated position at the playhead. */
  source: "forecast" | "best" | "current";
  /** The agency key, or the best track's key. */
  agency: string;
  code: string;
  time: string;
  /** Seconds from the forecast's base; absent on a best track. */
  lead?: number;
  number?: string;
  lat: number;
  lon: number;
  vmax: number | null;
  pmin: number | null;
  radii: TcRadii | null;
  class: string | null;
  rmw: number | null;
  gust: number | null;
}

export function pointDataOf(feature: {
  properties?: unknown;
}): TcPointData | null {
  const properties = feature.properties as
    Record<string, unknown> | null | undefined;
  const raw = properties?.data;
  if (typeof raw !== "string") return null;
  try {
    return JSON.parse(raw) as TcPointData;
  } catch {
    return null;
  }
}

/** What to draw of one storm. */
export interface StormView {
  storm: TcStorm;
  /** Agency forecasts to draw, by key. */
  agencies: ReadonlySet<string>;
  /** Deterministic model tracks to draw, by key (an ensemble's mean
   * counts under its own key). */
  models: ReadonlySet<string>;
  /** Draw ensemble members (the spaghetti) for the models drawn. */
  members: boolean;
  /** Draw the best tracks. */
  best: boolean;
}

interface Properties {
  kind: string;
  color: string;
  storm: string;
  code?: string;
  label?: string;
  threshold?: string;
  dotted?: boolean;
  opacity?: number;
  data?: string;
}

function pointData(
  storm: TcStorm,
  source: TcPointData["source"],
  agency: string,
  code: string,
  point: TcPoint,
  extra: { lead?: number; number?: string } = {},
): string {
  const data: TcPointData = {
    storm: storm.id,
    name: storm.name ?? storm.id,
    source,
    agency,
    code,
    time: point.time,
    lat: point.lat,
    lon: point.lon,
    vmax: point.vmax,
    pmin: point.pmin,
    radii: point.radii,
    class: point.class,
    rmw: point.rmw,
    gust: point.gust,
    ...extra,
  };
  return JSON.stringify(data);
}

/** The interpolated position as a point, for the current marker's card. */
function positionPoint(time: number, position: TrackPosition): TcPoint {
  return {
    time: new Date(time).toISOString().replace(/\.\d{3}Z$/, "Z"),
    lat: position.lat,
    lon: position.lon,
    vmax: position.vmax,
    pmin: position.pmin,
    radii: position.radii,
    class: position.class,
    rmw: null,
    gust: null,
    cone: null,
  };
}

function feature(geometry: Geometry, properties: Properties): Feature {
  return { type: "Feature", geometry, properties };
}

function line(coordinates: LonLat[], properties: Properties): Feature | null {
  return coordinates.length > 1
    ? feature({ type: "LineString", coordinates }, properties)
    : null;
}

function empty(): FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

const RADII_OPACITY: Record<string, number> = {
  "34": 0.12,
  "50": 0.18,
  "64": 0.26,
};

export class StormLayers {
  private views: StormView[] = [];
  private time: number | null = null;
  private neutral = NEUTRAL_LIGHT_GROUND;
  private halo = "#ffffff";
  private staticFeatures: Feature[] = [];
  private added = false;

  constructor(private readonly map: MaplibreMap) {}

  /** Add the source and layers once the style is loaded; `before` is the
   * layer they go under (the basemap's first symbol layer, so a city keeps
   * its name over a track's). */
  ensure(before?: string): boolean {
    if (this.added) return true;
    const map = this.map;
    if (map.getSource(TC_SOURCE)) {
      this.added = true;
      return true;
    }
    map.addSource(TC_SOURCE, { type: "geojson", data: empty(), buffer: 64 });
    const kind = (value: string): ExpressionSpecification => [
      "==",
      ["get", "kind"],
      value,
    ];
    const both = (
      a: ExpressionSpecification,
      b: ExpressionSpecification,
    ): FilterSpecification => ["all", a, b];
    const width = (base: number): ExpressionSpecification => [
      "interpolate",
      ["linear"],
      ["zoom"],
      2,
      base * 0.7,
      6,
      base,
      10,
      base * 1.6,
    ];
    map.addLayer(
      {
        id: LAYERS.radii,
        type: "fill",
        source: TC_SOURCE,
        filter: kind("radii"),
        paint: {
          "fill-color": ["get", "color"],
          "fill-opacity": ["get", "opacity"],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.radiiLine,
        type: "line",
        source: TC_SOURCE,
        filter: kind("radii"),
        paint: {
          "line-color": ["get", "color"],
          "line-width": 0.8,
          "line-opacity": 0.6,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.member,
        type: "line",
        source: TC_SOURCE,
        filter: kind("member"),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(0.7),
          "line-opacity": 0.35,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.modelSolid,
        type: "line",
        source: TC_SOURCE,
        filter: both(kind("model"), ["!", ["to-boolean", ["get", "dotted"]]]),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(1.4),
          "line-opacity": 0.85,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.modelDotted,
        type: "line",
        source: TC_SOURCE,
        filter: both(kind("model"), ["to-boolean", ["get", "dotted"]]),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(1.4),
          "line-opacity": 0.85,
          "line-dasharray": [0.1, 2],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.best,
        type: "line",
        source: TC_SOURCE,
        filter: kind("best"),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(1.6),
          "line-opacity": 0.7,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.bestPoint,
        type: "circle",
        source: TC_SOURCE,
        filter: kind("bestpoint"),
        paint: {
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            2,
            1.4,
            6,
            2.2,
            10,
            3.5,
          ],
          "circle-color": this.halo,
          "circle-stroke-color": ["get", "color"],
          "circle-stroke-width": 1,
          "circle-opacity": 0.9,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.agencyFuture,
        type: "line",
        source: TC_SOURCE,
        filter: kind("agency"),
        layout: { "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(2.2),
          "line-dasharray": [2, 1.6],
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.agencyPast,
        type: "line",
        source: TC_SOURCE,
        filter: kind("agency-past"),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["get", "color"], "line-width": width(2.2) },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.point,
        type: "circle",
        source: TC_SOURCE,
        filter: kind("point"),
        paint: {
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            2,
            1.8,
            6,
            2.6,
            10,
            4,
          ],
          "circle-color": ["get", "color"],
          "circle-stroke-color": this.halo,
          "circle-stroke-width": 0.8,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.current,
        type: "circle",
        source: TC_SOURCE,
        filter: kind("current"),
        paint: {
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            2,
            4,
            6,
            5.5,
            10,
            8,
          ],
          "circle-color": ["get", "color"],
          "circle-stroke-color": this.halo,
          "circle-stroke-width": 1.8,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.lineLabel,
        type: "symbol",
        source: TC_SOURCE,
        filter: kind("agency"),
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 300,
          "text-field": ["get", "code"],
          "text-font": ["Noto Sans Medium"],
          "text-size": 10,
          "text-letter-spacing": 0.08,
          "text-max-angle": 30,
          "text-padding": 2,
          "text-rotation-alignment": "map",
          "text-pitch-alignment": "viewport",
        },
        paint: {
          "text-color": ["get", "color"],
          "text-halo-color": this.halo,
          "text-halo-width": 1.6,
        },
      },
      before,
    );
    map.addLayer(
      {
        id: LAYERS.nameLabel,
        type: "symbol",
        source: TC_SOURCE,
        filter: kind("current"),
        layout: {
          "text-field": ["get", "label"],
          "text-font": ["Noto Sans Medium"],
          "text-size": 12,
          "text-anchor": "left",
          "text-offset": [0.9, 0],
          "text-padding": 4,
          "text-optional": true,
        },
        paint: {
          "text-color": ["get", "color"],
          "text-halo-color": this.halo,
          "text-halo-width": 1.8,
        },
      },
      before,
    );
    this.added = true;
    this.applyInk();
    this.publish();
    return true;
  }

  /** The neutral ink and the halo follow the ground the map is on; the
   * agency colors never do. */
  setInk(darkGround: boolean): void {
    const neutral = darkGround ? NEUTRAL_DARK_GROUND : NEUTRAL_LIGHT_GROUND;
    if (neutral === this.neutral && this.added) return;
    this.neutral = neutral;
    this.halo = darkGround
      ? "rgba(12, 16, 24, 0.9)"
      : "rgba(255, 255, 255, 0.92)";
    this.rebuildStatic();
    this.applyInk();
    this.publish();
  }

  private applyInk(): void {
    if (!this.added) return;
    for (const id of [LAYERS.point, LAYERS.current])
      this.map.setPaintProperty(id, "circle-stroke-color", this.halo);
    this.map.setPaintProperty(LAYERS.bestPoint, "circle-color", this.halo);
    for (const id of [LAYERS.lineLabel, LAYERS.nameLabel])
      this.map.setPaintProperty(id, "text-halo-color", this.halo);
  }

  setViews(views: StormView[]): void {
    this.views = views;
    this.rebuildStatic();
    this.publish();
  }

  setTime(time: number | null): void {
    this.time = time;
    this.publish();
  }

  clear(): void {
    this.views = [];
    this.staticFeatures = [];
    this.publish();
  }

  private colorOf(agency: string): string {
    return agencyColor(agency) ?? this.neutral;
  }

  private rebuildStatic(): void {
    const features: Feature[] = [];
    for (const view of this.views) {
      const storm = view.storm;
      if (view.best) {
        for (const [key, track] of Object.entries(storm.best)) {
          const agency = TC_BEST_AGENCY[key] ?? null;
          const color = agency ? this.colorOf(agency) : this.neutral;
          const code = key.toUpperCase();
          const drawn = line(forecastLine(track.points), {
            kind: "best",
            color,
            storm: storm.id,
            code,
          });
          if (drawn) features.push(drawn);
          for (const point of track.points) {
            features.push(
              feature(
                { type: "Point", coordinates: [point.lon, point.lat] },
                {
                  kind: "bestpoint",
                  color,
                  storm: storm.id,
                  code,
                  data: pointData(storm, "best", key, code, point),
                },
              ),
            );
          }
        }
      }
      for (const [model, value] of Object.entries(storm.models)) {
        if (!view.models.has(model)) continue;
        if (view.members && isTcEnsemble(value)) {
          for (const { member, line: coordinates } of ensembleLines(value)) {
            const drawn = line(coordinates, {
              kind: "member",
              color: this.neutral,
              storm: storm.id,
              code: `${model}:${member}`,
            });
            if (drawn) features.push(drawn);
          }
        }
      }
      for (const { model, forecast, fromEnsemble } of modelTracks(storm)) {
        if (!view.models.has(model)) continue;
        const drawn = line(forecastLine(forecast.points), {
          kind: "model",
          color: this.neutral,
          storm: storm.id,
          code: agencyCode(model),
          dotted: fromEnsemble || model !== "gfs",
        });
        if (drawn) features.push(drawn);
      }
      for (const [agency, forecast] of Object.entries(storm.agencies)) {
        if (!view.agencies.has(agency)) continue;
        const color = this.colorOf(agency);
        const code = agencyCode(agency);
        const drawn = line(forecastLine(forecast.points), {
          kind: "agency",
          color,
          storm: storm.id,
          code,
        });
        if (drawn) features.push(drawn);
        for (const point of forecast.points) {
          features.push(
            feature(
              { type: "Point", coordinates: [point.lon, point.lat] },
              {
                kind: "point",
                color,
                storm: storm.id,
                code,
                data: pointData(storm, "forecast", agency, code, point, {
                  lead: point.lead,
                  number: forecast.number,
                }),
              },
            ),
          );
        }
      }
    }
    this.staticFeatures = features;
  }

  /** The time-dependent features for one storm at the current time. */
  private dynamicFeatures(view: StormView): Feature[] {
    const time = this.time;
    if (time === null) return [];
    const storm = view.storm;
    const features: Feature[] = [];
    let named = false;
    const name = storm.name ?? storm.id;
    for (const [agency, forecast] of Object.entries(storm.agencies)) {
      if (!view.agencies.has(agency)) continue;
      const points = forecast.points;
      if (points.length === 0) continue;
      const color = this.colorOf(agency);
      const code = agencyCode(agency);
      const last = pointTime(points[points.length - 1]!);
      if (time > last) {
        const drawn = line(forecastLine(points), {
          kind: "agency-past",
          color,
          storm: storm.id,
          code,
        });
        if (drawn) features.push(drawn);
        continue;
      }
      const position = positionAt(points, time);
      if (position === null) continue;
      const past = unwrapLongitudes([
        ...points
          .slice(0, position.index + 1)
          .map((point): LonLat => [point.lon, point.lat]),
        [position.lon, position.lat],
      ]);
      const drawn = line(past, {
        kind: "agency-past",
        color,
        storm: storm.id,
        code,
      });
      if (drawn) features.push(drawn);
      if (position.radii) {
        for (const threshold of ["34", "50", "64"] as const) {
          const quadrants = position.radii[threshold];
          if (!quadrants) continue;
          const ring = radiiRing(position.lat, position.lon, quadrants);
          if (!ring) continue;
          features.push(
            feature(
              { type: "Polygon", coordinates: [ring] },
              {
                kind: "radii",
                color,
                storm: storm.id,
                threshold,
                opacity: RADII_OPACITY[threshold],
              },
            ),
          );
        }
      }
      const label = named
        ? ""
        : position.class
          ? `${name} · ${position.class}`
          : name;
      named = true;
      features.push(
        feature(
          { type: "Point", coordinates: [position.lon, position.lat] },
          {
            kind: "current",
            color,
            storm: storm.id,
            code,
            label,
            data: pointData(
              storm,
              "current",
              agency,
              code,
              positionPoint(time, position),
              {
                lead: Math.round((time - Date.parse(forecast.base)) / 1000),
                number: forecast.number,
              },
            ),
          },
        ),
      );
    }
    if (!named && view.best) {
      // Before any forecast starts the playhead is in the observed past:
      // the marker rides the best track instead.
      for (const [key, track] of Object.entries(storm.best)) {
        const position = positionAt(track.points, time);
        if (position === null) continue;
        const agency = TC_BEST_AGENCY[key] ?? null;
        const color = agency ? this.colorOf(agency) : this.neutral;
        const label = position.class ? `${name} · ${position.class}` : name;
        const code = key.toUpperCase();
        features.push(
          feature(
            { type: "Point", coordinates: [position.lon, position.lat] },
            {
              kind: "current",
              color,
              storm: storm.id,
              code,
              label,
              data: pointData(
                storm,
                "current",
                key,
                code,
                positionPoint(time, position),
              ),
            },
          ),
        );
        break;
      }
    }
    return features;
  }

  private publish(): void {
    if (!this.added) return;
    const source = this.map.getSource(TC_SOURCE) as GeoJSONSource | undefined;
    if (!source) return;
    const features = [...this.staticFeatures];
    for (const view of this.views) features.push(...this.dynamicFeatures(view));
    source.setData({ type: "FeatureCollection", features });
  }
}
