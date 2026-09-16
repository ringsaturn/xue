/** The station marks on the map: two MapLibre GeoJSON sources — the
 * radiosondes and the airports — over whatever fill and lines are on
 * screen, drawn the way the storm tracks are and with no shader of ours.
 *
 * Both products are point sets an index already holds whole: seven
 * hundred soundings, five thousand airports, each with the one
 * observation its mark stands for. Nothing here fetches, and nothing is
 * rebuilt on a playhead move: the time-dependent part is the dimming of a
 * station whose observation is far from the playhead, and that is a paint
 * expression over a `time` property, so moving the playhead sets two
 * paint properties rather than rebuilding five thousand features.
 *
 * The feature-building, the colors and the two predicates are exported as
 * plain functions: they are what the tests read, and they need no map. */

import type { Feature, FeatureCollection } from "geojson";
import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  GeoJSONSource,
  GeoJSONSourceSpecification,
  Map as MaplibreMap,
} from "maplibre-gl";

import { NEUTRAL_DARK_GROUND, NEUTRAL_LIGHT_GROUND } from "../tc/agencies";
import type {
  AirportIndex,
  AirportStation,
  FlightCategory,
  SoundingIndex,
  SoundingStationEntry,
} from "./schema";

export const SOUNDING_SOURCE = "station-soundings";
export const AIRPORT_SOURCE = "station-airports";

const LAYERS = {
  sounding: "station-sounding",
  /** Airports with a current TAF, drawn at every zoom. */
  airportMajor: "station-airport-major",
  /** Every other airport, from `AIRPORT_ALL_ZOOM` up. */
  airportMinor: "station-airport-minor",
} as const;

export const STATION_LAYER_IDS: readonly string[] = Object.values(LAYERS);
/** The layers a click on the map may hit a station through, the sparser
 * set first so a sounding over an airport opens the sounding. */
export const STATION_CLICKABLE_LAYERS: readonly string[] = [
  LAYERS.sounding,
  LAYERS.airportMajor,
  LAYERS.airportMinor,
];

/** Below this zoom the airports thin to the ones with a current TAF. The
 * index carries no notion of a major airport, and a station the service
 * forecasts for is the closest thing to one it does carry; five thousand
 * dots on a world view are noise, and the thousand-odd forecast airports
 * read as a network. */
export const AIRPORT_ALL_ZOOM = 6;

/** How far from the playhead an observation may be before its mark is
 * drawn faint. An airport reports every half hour and a sonde flies twice
 * a day, so the two windows are hours apart; both are wide enough that a
 * station reporting normally never dims, and narrow enough that a
 * playhead a day away from the product leaves the map empty-looking
 * rather than wrong. */
export const AIRPORT_STALE_MS = 3 * 3600 * 1000;
export const SOUNDING_STALE_MS = 15 * 3600 * 1000;

const FULL_OPACITY = 0.92;
const STALE_OPACITY = 0.28;

const RADIUS = 4.5;
const HOVER_RADIUS = 5.5;

/** A sounding with no 500 hPa temperature is drawn hollow: the ring in
 * the map's ink and nothing inside it. */
const HOLLOW = "rgba(0, 0, 0, 0)";

/** The 500 hPa temperature ramp, cool to warm, in °C. A mid-latitude
 * autumn runs −5 °C over a warm trough to −35 °C under a cold one, so the
 * ends are set outside that: a marker layer's job is to separate the
 * ridge from the trough at a glance, not to be read as a value. Colors
 * outside the ends clamp; the ramp is the same on either ground, since a
 * temperature does not change with the basemap. */
const T500_RAMP: readonly (readonly [number, string])[] = [
  [-45, "#3b4cc0"],
  [-35, "#6f8fd6"],
  [-25, "#a8c0e8"],
  [-15, "#e8c39e"],
  [-5, "#c9482f"],
];

/** The aviation flight categories in their conventional colors — the ones
 * every chart and every pilot's app uses, so the reading is learned
 * already — with one set per ground tone and grey for a station the
 * service gives no category. */
const CATEGORY_COLORS: Record<"light" | "dark", Record<FlightCategory | "none", string>> = {
  light: {
    VFR: "#1f8a4c",
    MVFR: "#1d61c4",
    IFR: "#c62828",
    LIFR: "#a52bab",
    none: "#78808c",
  },
  dark: {
    VFR: "#3fc97a",
    MVFR: "#5aa9ff",
    IFR: "#ff6b6b",
    LIFR: "#e07ff0",
    none: "#98a2b0",
  },
};

function mixChannel(from: number, to: number, fraction: number): number {
  return Math.round(from + (to - from) * fraction);
}

function hexChannels(color: string): [number, number, number] {
  return [
    parseInt(color.slice(1, 3), 16),
    parseInt(color.slice(3, 5), 16),
    parseInt(color.slice(5, 7), 16),
  ];
}

function hex(channels: readonly number[]): string {
  return `#${channels.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

/** The fill a sounding's mark takes from its 500 hPa temperature: the
 * ramp above, clamped at both ends, and hollow where the ascent never
 * reached the level. */
export function soundingFill(t500: number | null): string {
  if (t500 === null) return HOLLOW;
  const first = T500_RAMP[0]!;
  if (t500 <= first[0]) return first[1];
  const last = T500_RAMP[T500_RAMP.length - 1]!;
  if (t500 >= last[0]) return last[1];
  for (let index = 1; index < T500_RAMP.length; index += 1) {
    const [upper, upperColor] = T500_RAMP[index]!;
    if (t500 > upper) continue;
    const [lower, lowerColor] = T500_RAMP[index - 1]!;
    const fraction = (t500 - lower) / (upper - lower);
    const from = hexChannels(lowerColor);
    const to = hexChannels(upperColor);
    return hex(from.map((channel, position) => mixChannel(channel, to[position]!, fraction)));
  }
  return last[1];
}

export function categoryColor(category: FlightCategory | null, darkGround: boolean): string {
  return CATEGORY_COLORS[darkGround ? "dark" : "light"][category ?? "none"];
}

/** Whether an airport is drawn at a zoom: the ones with a current TAF
 * always, every other from `AIRPORT_ALL_ZOOM` up. */
export function airportDrawnAtZoom(station: AirportStation, zoom: number): boolean {
  return station.tafPresent || zoom >= AIRPORT_ALL_ZOOM;
}

/** Whether a station's observation is far enough from the playhead to be
 * drawn faint. Symmetric: a playhead in a run's forecast hours is ahead
 * of every observation, and one scrubbed into the past is behind them. */
export function isStale(observed: number, time: number, limit: number): boolean {
  return Math.abs(observed - time) > limit;
}

/** What a clicked mark carries, as the feature's `data` property (a JSON
 * string: MapLibre flattens nested properties). */
export type StationPointData =
  | { kind: "sounding"; station: SoundingStationEntry }
  | { kind: "airport"; station: AirportStation };

export function stationDataOf(feature: { properties?: unknown }): StationPointData | null {
  const properties = feature.properties as Record<string, unknown> | null | undefined;
  const raw = properties?.data;
  if (typeof raw !== "string") return null;
  try {
    return JSON.parse(raw) as StationPointData;
  } catch {
    return null;
  }
}

function point(lon: number, lat: number, properties: Record<string, unknown>): Feature {
  return {
    type: "Feature",
    id: properties.id as string,
    geometry: { type: "Point", coordinates: [lon, lat] },
    properties,
  };
}

function collection(features: Feature[]): FeatureCollection {
  return { type: "FeatureCollection", features };
}

/** One circle per sounding station, coloured by its 500 hPa temperature
 * and stamped with its newest nominal time. */
export function soundingFeatures(index: SoundingIndex | null): FeatureCollection {
  if (index === null) return collection([]);
  return collection(
    index.stations.map((station) =>
      point(station.lon, station.lat, {
        id: station.id,
        kind: "sounding",
        fill: soundingFill(station.headline.t500),
        time: Date.parse(station.latest),
        data: JSON.stringify({ kind: "sounding", station } satisfies StationPointData),
      }),
    ),
  );
}

/** One circle per airport, carrying its category (coloured by an
 * expression, so a ground change costs no rebuild), whether it has a
 * current TAF, and its observation time. */
export function airportFeatures(index: AirportIndex | null): FeatureCollection {
  if (index === null) return collection([]);
  return collection(
    index.stations.map((station) =>
      point(station.lon, station.lat, {
        id: station.icao,
        kind: "airport",
        category: station.category ?? "none",
        taf: station.tafPresent,
        time: Date.parse(station.obsTime),
        data: JSON.stringify({ kind: "airport", station } satisfies StationPointData),
      }),
    ),
  );
}

/** The opacity a mark is drawn at for a playhead at `time`: full inside
 * the product's window, faint outside it. An expression over the feature's
 * own `time`, so a playhead move is two paint properties rather than a
 * rebuilt source. `null` (no dataset open yet) draws everything full. */
export function freshnessOpacity(time: number | null, limit: number): ExpressionSpecification | number {
  if (time === null) return FULL_OPACITY;
  return [
    "case",
    [">", ["abs", ["-", ["number", ["get", "time"]], time]], limit],
    STALE_OPACITY,
    FULL_OPACITY,
  ];
}

/** The category colors as a match over the feature's own category. */
export function categoryColorExpression(darkGround: boolean): ExpressionSpecification {
  const colors = CATEGORY_COLORS[darkGround ? "dark" : "light"];
  return [
    "match",
    ["get", "category"],
    "VFR",
    colors.VFR,
    "MVFR",
    colors.MVFR,
    "IFR",
    colors.IFR,
    "LIFR",
    colors.LIFR,
    colors.none,
  ];
}

const RADIUS_EXPRESSION: ExpressionSpecification = [
  "case",
  ["boolean", ["feature-state", "hover"], false],
  HOVER_RADIUS,
  RADIUS,
];

/** How far the playhead must move before the dimming is recomputed. The
 * readout calls `setTime` on every frame, and a paint property set is a
 * repaint; half a minute is far below either product's cadence. */
const TIME_EPSILON_MS = 30_000;

/** The two sources, as the style carries them. `promoteId` is what lets a
 * mark carry a hover state: the station's own id is the feature id. */
export function stationSourceSpecs(data: {
  soundings: SoundingIndex | null;
  airports: AirportIndex | null;
}): { id: string; source: GeoJSONSourceSpecification }[] {
  return [
    {
      id: SOUNDING_SOURCE,
      source: { type: "geojson", data: soundingFeatures(data.soundings), promoteId: "id" },
    },
    {
      id: AIRPORT_SOURCE,
      source: { type: "geojson", data: airportFeatures(data.airports), promoteId: "id" },
    },
  ];
}

/** The three circle layers, in the order they are added: each goes under
 * the same symbol layer, so the last one added draws on top and the
 * soundings — the sparser set — stay over the airports.
 *
 * The airports are two layers rather than one because `zoom` is not an
 * expression a filter may use: the ones with a current TAF are drawn at
 * every zoom, the rest from `AIRPORT_ALL_ZOOM` up. */
export function stationLayerSpecs(state: {
  darkGround: boolean;
  time: number | null;
  ink: string;
}): CircleLayerSpecification[] {
  const airportOpacity = freshnessOpacity(state.time, AIRPORT_STALE_MS);
  const airportPaint = {
    "circle-radius": RADIUS_EXPRESSION,
    "circle-color": categoryColorExpression(state.darkGround),
    "circle-stroke-color": state.ink,
    "circle-stroke-width": 0.8,
    "circle-opacity": airportOpacity,
    "circle-stroke-opacity": airportOpacity,
  } as const;
  const soundingOpacity = freshnessOpacity(state.time, SOUNDING_STALE_MS);
  return [
    {
      id: LAYERS.airportMinor,
      type: "circle",
      source: AIRPORT_SOURCE,
      minzoom: AIRPORT_ALL_ZOOM,
      filter: ["!", ["to-boolean", ["get", "taf"]]],
      paint: { ...airportPaint },
    },
    {
      id: LAYERS.airportMajor,
      type: "circle",
      source: AIRPORT_SOURCE,
      filter: ["to-boolean", ["get", "taf"]],
      paint: { ...airportPaint },
    },
    {
      id: LAYERS.sounding,
      type: "circle",
      source: SOUNDING_SOURCE,
      paint: {
        "circle-radius": RADIUS_EXPRESSION,
        "circle-color": ["get", "fill"],
        "circle-stroke-color": state.ink,
        "circle-stroke-width": 1.4,
        "circle-opacity": soundingOpacity,
        "circle-stroke-opacity": soundingOpacity,
      },
    },
  ];
}

export class StationLayers {
  private added = false;
  private ink = NEUTRAL_LIGHT_GROUND;
  private darkGround = false;
  private time: number | null = null;
  private appliedTime: number | null = null;
  private soundings: SoundingIndex | null = null;
  private airports: AirportIndex | null = null;
  private hovered: { source: string; id: string } | null = null;

  constructor(private readonly map: MaplibreMap) {}

  /** Add the sources and layers once the style is loaded; `before` is the
   * layer they go under (the basemap's first symbol layer, so a city keeps
   * its name over a station's mark). */
  ensure(before?: string): boolean {
    if (this.added) return true;
    const map = this.map;
    for (const { id, source } of stationSourceSpecs({
      soundings: this.soundings,
      airports: this.airports,
    })) {
      if (!map.getSource(id)) map.addSource(id, source);
    }
    for (const layer of stationLayerSpecs({
      darkGround: this.darkGround,
      time: this.time,
      ink: this.ink,
    })) {
      if (!map.getLayer(layer.id)) map.addLayer(layer, before);
    }
    this.added = true;
    this.appliedTime = this.time;
    return true;
  }

  /** The stroke ink and the category colors follow the ground the map is
   * on; the sounding ramp never does. */
  setInk(darkGround: boolean): void {
    if (this.added && darkGround === this.darkGround) return;
    this.darkGround = darkGround;
    this.ink = darkGround ? NEUTRAL_DARK_GROUND : NEUTRAL_LIGHT_GROUND;
    if (!this.added) return;
    for (const id of STATION_LAYER_IDS)
      this.map.setPaintProperty(id, "circle-stroke-color", this.ink);
    for (const id of [LAYERS.airportMajor, LAYERS.airportMinor])
      this.map.setPaintProperty(id, "circle-color", categoryColorExpression(darkGround));
  }

  setSoundings(index: SoundingIndex | null): void {
    this.soundings = index;
    this.publish(SOUNDING_SOURCE, soundingFeatures(index));
  }

  setAirports(index: AirportIndex | null): void {
    this.airports = index;
    this.publish(AIRPORT_SOURCE, airportFeatures(index));
  }

  /** The playhead's valid time, which is only ever what a mark's opacity
   * is measured against: the marks take no session and never gate it. */
  setTime(time: number | null): void {
    this.time = time;
    if (!this.added) return;
    const applied = this.appliedTime;
    if (time !== null && applied !== null && Math.abs(time - applied) < TIME_EPSILON_MS) return;
    this.appliedTime = time;
    for (const id of [LAYERS.airportMajor, LAYERS.airportMinor]) {
      const opacity = freshnessOpacity(time, AIRPORT_STALE_MS);
      this.map.setPaintProperty(id, "circle-opacity", opacity);
      this.map.setPaintProperty(id, "circle-stroke-opacity", opacity);
    }
    const opacity = freshnessOpacity(time, SOUNDING_STALE_MS);
    this.map.setPaintProperty(LAYERS.sounding, "circle-opacity", opacity);
    this.map.setPaintProperty(LAYERS.sounding, "circle-stroke-opacity", opacity);
  }

  /** The mark under the pointer, which grows by a pixel; null clears it. */
  setHover(data: StationPointData | null): void {
    if (!this.added) return;
    const next =
      data === null
        ? null
        : data.kind === "sounding"
          ? { source: SOUNDING_SOURCE, id: data.station.id }
          : { source: AIRPORT_SOURCE, id: data.station.icao };
    const previous = this.hovered;
    if (previous?.source === next?.source && previous?.id === next?.id) return;
    if (previous) this.map.setFeatureState(previous, { hover: false });
    if (next) this.map.setFeatureState(next, { hover: true });
    this.hovered = next;
  }

  /** Take both products off the map — a case, or a root that publishes
   * neither — leaving the style as it was found. */
  remove(): void {
    if (!this.added) return;
    for (const id of STATION_LAYER_IDS) if (this.map.getLayer(id)) this.map.removeLayer(id);
    for (const source of [SOUNDING_SOURCE, AIRPORT_SOURCE])
      if (this.map.getSource(source)) this.map.removeSource(source);
    this.added = false;
    this.hovered = null;
  }

  private publish(id: string, data: FeatureCollection): void {
    if (!this.added) return;
    const source = this.map.getSource(id) as GeoJSONSource | undefined;
    source?.setData(data);
  }
}
