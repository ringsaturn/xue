/** A showcase case's context lines (`showcase-catalog.ts` `ShowcaseContext`):
 * a border, a protected area, the watercourses and roads around an event,
 * the route its story follows, lines read off imagery and measured
 * distances — one GeoJSON file, one MapLibre source, a layer per way of
 * drawing a line.
 *
 * Every feature is styled by its `kind` alone, through expressions, so the
 * file carries no paint of its own. The lines are the ground the event sits
 * on: they draw over the field and under every mark the app adds over it
 * (`contextBeforeLayer`).
 *
 * The parse, the layer specs and the placement are plain functions so the
 * tests can read them without a map. */

import type { Feature, FeatureCollection, LineString, MultiLineString, MultiPolygon, Polygon } from "geojson";
import type {
  ExpressionSpecification,
  FillLayerSpecification,
  LineLayerSpecification,
  Map as MaplibreMap,
  SymbolLayerSpecification,
} from "maplibre-gl";

import { fetchImmutable } from "./fetchimmutable";
import type { ShowcaseContext } from "./showcase-catalog";

type PaintName = Parameters<MaplibreMap["setPaintProperty"]>[1];
type PaintValue = Parameters<MaplibreMap["setPaintProperty"]>[2];

/** What a feature is, which is all it is drawn by (`xuebuild/showcase.py`
 * `CONTEXT_KINDS`). */
export const CONTEXT_KINDS = [
  "border",
  "protected-area",
  "river",
  "stream",
  "road",
  "track",
  "route",
  "trace",
  "measure",
] as const;
export type ContextKind = (typeof CONTEXT_KINDS)[number];

type ContextGeometry = LineString | MultiLineString | Polygon | MultiPolygon;
/** A feature's label is drawn along it (a measured distance); the builder
 * holds it to one short line. */
type ContextProperties = { kind: ContextKind; label?: string };
export type ContextFeature = Feature<ContextGeometry, ContextProperties>;
export type ContextCollection = FeatureCollection<ContextGeometry, ContextProperties>;

const MAX_LABEL = 40;

/** How many arrays deep each geometry's positions sit, the innermost
 * being a line (a polygon's rings are lines too), and how many positions
 * that line needs to be drawn. */
const GEOMETRIES = new Map<string, { depth: number; minimum: number }>([
  ["LineString", { depth: 1, minimum: 2 }],
  ["MultiLineString", { depth: 2, minimum: 2 }],
  ["Polygon", { depth: 2, minimum: 4 }],
  ["MultiPolygon", { depth: 3, minimum: 4 }],
]);

const SOURCE = "case-context";
const LAYERS = {
  fill: "case-context-fill",
  halo: "case-context-halo",
  solid: "case-context-solid",
  track: "case-context-track",
  trace: "case-context-trace",
  border: "case-context-border",
  measure: "case-context-measure",
  routeCasing: "case-context-route-casing",
  route: "case-context-route",
  label: "case-context-label",
} as const;

function isKind(value: unknown): value is ContextKind {
  return (CONTEXT_KINDS as readonly unknown[]).includes(value);
}

function isPosition(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.length >= 2 &&
    value.every((number) => typeof number === "number" && Number.isFinite(number)) &&
    Math.abs(value[1] as number) <= 90
  );
}

/** Whether `value` nests `depth` arrays above its positions, every
 * innermost one a line of at least `minimum` of them. */
function coordinatesOk(value: unknown, depth: number, minimum: number): boolean {
  if (!Array.isArray(value) || value.length === 0) return false;
  if (depth === 1) return value.length >= minimum && value.every(isPosition);
  return value.every((part) => coordinatesOk(part, depth - 1, minimum));
}

/** The drawable features of a fetched context file, each carrying only its
 * kind and its label. A file that is no FeatureCollection throws; a feature of a kind
 * this shell does not know, or whose geometry it cannot draw, is dropped
 * with one warning for the lot, and the rest still draw — a kind added
 * after this shell was deployed leaves the others standing. */
export function contextFeatures(input: unknown): ContextCollection {
  const value = input as { type?: unknown; features?: unknown } | null;
  if (value?.type !== "FeatureCollection" || !Array.isArray(value.features)) {
    throw new Error("context file is not a GeoJSON FeatureCollection");
  }
  const features: ContextFeature[] = [];
  let dropped = 0;
  for (const entry of value.features as unknown[]) {
    const feature = entry as
      | { type?: unknown; properties?: { kind?: unknown; label?: unknown } | null; geometry?: unknown }
      | null;
    const kind = feature?.properties?.kind;
    const geometry = feature?.geometry as { type?: unknown; coordinates?: unknown } | null | undefined;
    const shape = typeof geometry?.type === "string" ? GEOMETRIES.get(geometry.type) : undefined;
    if (feature?.type !== "Feature" || !isKind(kind) || !shape || !coordinatesOk(geometry!.coordinates, shape.depth, shape.minimum)) {
      dropped += 1;
      continue;
    }
    const drawn = { type: geometry!.type, coordinates: geometry!.coordinates } as ContextGeometry;
    const properties: ContextProperties = { kind };
    // A label this shell cannot draw on one line is dropped; the line stays.
    const label = feature!.properties?.label;
    if (typeof label === "string" && label.trim() && label.length <= MAX_LABEL) properties.label = label.replace(/\s+/g, " ").trim();
    features.push({ type: "Feature", geometry: drawn, properties });
  }
  if (dropped > 0) console.warn(`context: skipped ${dropped} feature(s) this shell cannot draw`);
  return { type: "FeatureCollection", features };
}

/** The case's context file, at its `?v=`. */
export async function fetchCaseContext(
  baseUrl: string,
  context: ShowcaseContext,
): Promise<ContextCollection> {
  const url = new URL(context.filePath, new URL(baseUrl, document.baseURI));
  url.searchParams.set("v", context.crc32);
  const response = await fetchImmutable(url);
  if (!response.ok) throw new Error(`context request failed: ${response.status}`);
  return contextFeatures(await response.json());
}

/** The colours each kind is drawn in, on the ground the map is on. The
 * route is the case's own line, orange-red on either ground; the rest are
 * a chart's: neutral roads and border, blue water, a green reserve,
 * magenta for what is only read off a picture. */
export function contextInk(darkGround: boolean): {
  border: string;
  reserve: string;
  water: string;
  road: string;
  route: string;
  casing: string;
  trace: string;
  measure: string;
  halo: string;
} {
  return darkGround
    ? {
        border: "#ece8df",
        reserve: "#7fd08c",
        water: "#80bdf2",
        road: "#cfc8bc",
        route: "#ff5a2a",
        casing: "rgba(0, 0, 0, 0.7)",
        trace: "#f27ad6",
        measure: "#ffe08a",
        halo: "rgba(0, 0, 0, 0.8)",
      }
    : {
        border: "#36332d",
        reserve: "#2c7a38",
        water: "#2b6cb3",
        road: "#4f4a42",
        route: "#e0401a",
        casing: "rgba(255, 255, 255, 0.92)",
        trace: "#c0279a",
        measure: "#1f2b3a",
        halo: "rgba(255, 255, 255, 0.92)",
      };
}

const KIND: ExpressionSpecification = ["get", "kind"];

/** A width that is `width(1)` CSS px at z10, a little narrower zoomed out
 * and wider in, so a regional view is no tangle and a close one still
 * reads. */
function zoomWidth(width: (factor: number) => number | ExpressionSpecification): ExpressionSpecification {
  return ["interpolate", ["linear"], ["zoom"], 5, width(0.7), 10, width(1), 15, width(1.6)];
}

function px(width: number): (factor: number) => number {
  return (factor) => Math.round(width * factor * 100) / 100;
}

/** The solid layer's widths, per kind. */
function solidWidth(factor: number): ExpressionSpecification {
  const w = (width: number) => px(width)(factor);
  return ["match", KIND, "protected-area", w(1.4), "river", w(1.4), "stream", w(0.8), w(1)];
}

/** The halo under the solid, track and border lines: each kind's own width
 * and a fixed margin either side. */
const HALO_MARGIN = 0.9;

function haloWidth(factor: number): ExpressionSpecification {
  const w = (width: number) => Math.round((width * factor + 2 * HALO_MARGIN) * 100) / 100;
  return ["match", KIND, "protected-area", w(1.4), "river", w(1.4), "stream", w(0.8), "track", w(0.8), "border", w(1.6), w(1)];
}

function isKindFilter(...kinds: ContextKind[]): ExpressionSpecification {
  return kinds.length === 1 ? ["==", KIND, kinds[0]!] : ["in", KIND, ["literal", kinds]];
}

const ROUND = { "line-join": "round", "line-cap": "round" } as const;

/** The layers, bottom to top: the reserve's tint; a halo in the ground's
 * opposite tone under the solid, track and border lines, since the ink is
 * picked for the base map while the field under them may be a photograph
 * (the true colour picture is dark under the light theme's dark ink, and a
 * pale cloud under the dark theme's light one); the solid lines (the
 * reserve's outline, water, roads), roads over water; the dashed tracks,
 * the dotted traces, the dash-dot border and the dashed measures, each a
 * layer of its own since a dash pattern is not per feature; the route over
 * them, on a casing that lifts it off whatever field is under it; and the
 * labels on top. */
export function contextLayerSpecs(
  darkGround: boolean,
): (FillLayerSpecification | LineLayerSpecification | SymbolLayerSpecification)[] {
  const ink = contextInk(darkGround);
  return [
    {
      id: LAYERS.fill,
      type: "fill",
      source: SOURCE,
      // A fill draws polygons alone, so a reserve given as a line is only
      // its outline.
      filter: isKindFilter("protected-area"),
      paint: { "fill-color": ink.reserve, "fill-opacity": 0.08 },
    },
    {
      id: LAYERS.halo,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("protected-area", "river", "stream", "road", "track", "border"),
      layout: ROUND,
      paint: { "line-color": ink.halo, "line-width": zoomWidth(haloWidth), "line-opacity": 0.7, "line-blur": 0.6 },
    },
    {
      id: LAYERS.solid,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("protected-area", "river", "stream", "road"),
      layout: { ...ROUND, "line-sort-key": ["match", KIND, "protected-area", 0, "stream", 1, "river", 2, 3] },
      paint: {
        "line-color": ["match", KIND, "protected-area", ink.reserve, "river", ink.water, "stream", ink.water, ink.road],
        "line-width": zoomWidth(solidWidth),
        "line-opacity": ["match", KIND, "stream", 0.6, 0.9],
      },
    },
    {
      id: LAYERS.track,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("track"),
      layout: { "line-join": "round", "line-cap": "butt" },
      paint: {
        "line-color": ink.road,
        "line-width": zoomWidth(px(0.8)),
        "line-opacity": 0.9,
        "line-dasharray": [3, 2],
      },
    },
    {
      id: LAYERS.trace,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("trace"),
      layout: ROUND,
      // Round caps on near-zero dashes draw dots.
      paint: { "line-color": ink.trace, "line-width": zoomWidth(px(1.2)), "line-dasharray": [0.1, 2] },
    },
    {
      id: LAYERS.border,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("border"),
      layout: { "line-join": "round", "line-cap": "butt" },
      paint: {
        "line-color": ink.border,
        "line-width": zoomWidth(px(1.6)),
        "line-opacity": 0.85,
        // A dash and a dot, the way a chart draws a frontier.
        "line-dasharray": [5, 2, 1, 2],
      },
    },
    {
      id: LAYERS.measure,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("measure"),
      layout: { "line-join": "round", "line-cap": "butt" },
      paint: {
        "line-color": ink.measure,
        "line-width": zoomWidth(px(1.1)),
        "line-opacity": 0.85,
        "line-dasharray": [4, 3],
      },
    },
    {
      id: LAYERS.routeCasing,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("route"),
      layout: ROUND,
      paint: { "line-color": ink.casing, "line-width": zoomWidth(px(5.4)), "line-blur": 0.5 },
    },
    {
      id: LAYERS.route,
      type: "line",
      source: SOURCE,
      filter: isKindFilter("route"),
      layout: ROUND,
      paint: { "line-color": ink.route, "line-width": zoomWidth(px(3)) },
    },
    {
      id: LAYERS.label,
      type: "symbol",
      source: SOURCE,
      filter: ["has", "label"],
      layout: {
        "text-field": ["get", "label"],
        "text-font": ["Noto Sans Medium"],
        "text-size": 11.5,
        // Midway along the line, so a distance reads as that line's.
        "symbol-placement": "line-center",
        "text-keep-upright": true,
        "text-offset": [0, -0.7],
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

/** Where the context layers go in a style, its layers bottom to top: right
 * above the field's anchor (the fields are all inserted under it), past the
 * basemap's own lines over it (the boundaries), and under the first layer
 * that is a label or anything else the app put there — fire detections, a
 * river's reaches, the case's markers, which are all inserted under the
 * first label or on top. So whichever of them comes first, a later one
 * lands over the context, an earlier one stays over it, and a field added
 * later still lands under it. Undefined puts the layers on top. */
export function contextBeforeLayer(
  layers: readonly { id: string; type: string }[],
  anchor: string,
  isBasemapLayer: (id: string) => boolean,
): string | undefined {
  const at = layers.findIndex((layer) => layer.id === anchor);
  // Without the anchor the field has no fixed place; the first label is
  // the next best ceiling.
  if (at < 0) return layers.find((layer) => layer.type === "symbol")?.id;
  return layers.slice(at + 1).find((layer) => layer.type === "symbol" || !isBasemapLayer(layer.id))?.id;
}

export interface CaseContextOptions {
  /** The layer the fields are inserted under (`main.ts` `FORECAST_ANCHOR_LAYER`). */
  anchorLayer: string;
  /** Whether a layer is the basemap's own rather than the app's. */
  isBasemapLayer(id: string): boolean;
}

export class CaseContextLayers {
  private added = false;
  private darkGround = false;

  constructor(
    private readonly map: MaplibreMap,
    private readonly options: CaseContextOptions,
  ) {}

  /** Draw one case's lines in place of any shown before. The layers are
   * placed afresh each time, since what else is on the map differs from
   * case to case. */
  show(collection: ContextCollection): void {
    this.remove();
    this.map.addSource(SOURCE, { type: "geojson", data: collection });
    const order = this.map.getLayersOrder().map((id) => ({ id, type: this.map.getLayer(id)?.type ?? "" }));
    const before = contextBeforeLayer(order, this.options.anchorLayer, this.options.isBasemapLayer);
    // Each goes in directly under `before`, so they keep their own order.
    for (const layer of contextLayerSpecs(this.darkGround)) this.map.addLayer(layer, before);
    this.added = true;
  }

  /** Repaint the colours for the ground. The basemap's own ink pass only
   * touches its labels and boundaries, so these keep theirs until here. */
  setInk(darkGround: boolean): void {
    this.darkGround = darkGround;
    if (!this.added) return;
    for (const layer of contextLayerSpecs(darkGround)) {
      for (const [name, value] of Object.entries(layer.paint ?? {})) {
        if (!name.endsWith("-color") || !this.map.getLayer(layer.id)) continue;
        this.map.setPaintProperty(layer.id, name as PaintName, value as PaintValue);
      }
    }
  }

  remove(): void {
    for (const id of Object.values(LAYERS)) if (this.map.getLayer(id)) this.map.removeLayer(id);
    if (this.map.getSource(SOURCE)) this.map.removeSource(SOURCE);
    this.added = false;
  }
}
