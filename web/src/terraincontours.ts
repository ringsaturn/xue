import type { ExpressionSpecification, Map as MaplibreMap } from "maplibre-gl";
import { addProtocol } from "maplibre-gl";
import mlcontour from "maplibre-contour";

import { TERRAIN_MAX_ZOOM, TERRAIN_TILE_URL } from "./terrain";

/**
 * Elevation contour lines over the relief, cut in the browser from the same
 * Terrarium tiles the hillshade and the 3D terrain draw (maplibre-contour:
 * a protocol that fetches a DEM tile with its neighbours, traces the
 * isolines in a worker and answers a vector tile). Nothing is published for
 * it, so every dataset has them, on the plane and on the globe, wherever
 * Mapterhorn has ground.
 *
 * The lines sit directly under the forecast anchor — above every field and
 * line slot, under the coast and the labels — and are moved back there
 * whenever a field attaches above them. Each line is drawn twice, a pale
 * casing under the ink, so it reads over a field of any colour; the ink
 * follows the ground like the coast's does (`setInk`).
 */

/** The zoom the lines appear from: a 200 m interval on a z12 DEM is a
 * scribble wider out, and the shadows draw from the same zoom. */
export const CONTOUR_MIN_ZOOM = 9;
/** The deepest contour tile asked for; past it MapLibre overzooms the last. */
export const CONTOUR_MAX_ZOOM = 15;

/** The intervals in metres by zoom, three tiers each: the fine line, the
 * middle line and the index line (`level` 0, 1, 2 on each feature — the
 * largest tier the elevation is a multiple of). They follow what a
 * topographic sheet prints at the zoom's scale — z9 is about 1:1 000 000,
 * z11 1:270 000, z13 1:70 000, z15 1:17 000 — on the side of fewer lines,
 * since the relief is shaded under them: a Himalayan flank of 3 800 m over
 * 10 km is 36 px wide at z9, where 200 m lines would hatch it solid. Each
 * rung's middle tier is the rung before's fine one, so zooming in adds a
 * finer tier under lines that keep their weight and never thins or
 * thickens what was drawn; the first rung has no middle tier (its 500 m
 * lines are the faint ones under 1 000 m index lines). Every tier divides
 * the one above it, since the plugin draws lines at multiples of the
 * finest alone. A zoom without an entry uses the next lower one. */
export const CONTOUR_THRESHOLDS: Record<number, [number, number, number]> = {
  9: [500, 1000, 1000],
  11: [250, 500, 1000],
  13: [50, 250, 500],
  15: [25, 50, 250],
};
const INDEX_LEVEL = 2;

/** The fine tier is drawn faint and darkens as the map zooms into its rung,
 * so a steep slope reads as a tone under the index lines rather than a
 * hatch; the two tiers above keep their ink. */
const FINE_OPACITY: ExpressionSpecification = [
  "interpolate",
  ["linear"],
  ["zoom"],
  9, 0.2, 10.9, 0.5,
  11, 0.2, 12.9, 0.55,
  13, 0.2, 14.9, 0.55,
  15, 0.2, 16.5, 0.6,
];
const isFine: ExpressionSpecification = ["==", ["get", "level"], 0];
const isIndex: ExpressionSpecification = ["==", ["get", "level"], INDEX_LEVEL];

export const CONTOUR_SOURCE = "terrain-contours";
export const CONTOUR_CASING_LAYER = "terrain-contour-casing";
export const CONTOUR_LINE_LAYER = "terrain-contour-lines";
export const CONTOUR_LABEL_LAYER = "terrain-contour-labels";

export interface ContourInk {
  line: string;
  /** The wider, paler line under the ink: what keeps a line legible over a
   * field as dense as a temperature fill, as the halo does for the labels. */
  casing: string;
  text: string;
  halo: string;
}

/** The lines' ink per ground: a dark brown on paper, as a survey sheet
 * prints its relief, over a casing of the paper's own tone; a pale sand over
 * a dark casing on the dark ground. The casing is what the line is read
 * against where the field under it is any colour — a brown line alone
 * disappears on an orange temperature fill. */
export function contourInk(darkGround: boolean): ContourInk {
  return darkGround
    ? { line: "rgba(255, 230, 190, 0.85)", casing: "rgba(0, 0, 0, 0.45)", text: "#f3e6cc", halo: "rgba(0, 0, 0, 0.75)" }
    : { line: "rgba(70, 45, 20, 0.85)", casing: "rgba(243, 239, 230, 0.6)", text: "#4a3014", halo: "rgba(243, 239, 230, 0.92)" };
}

export interface TerrainContoursOptions {
  map: MaplibreMap;
  /** The layer the lines stay directly under. */
  anchorLayer: string;
  darkGround: () => boolean;
  /** The lines were switched on or off. */
  onChange?: () => void;
}

export class TerrainContours {
  private readonly map: MaplibreMap;
  private readonly tiles: string;
  private on = false;

  constructor(private readonly options: TerrainContoursOptions) {
    this.map = options.map;
    const dem = new mlcontour.DemSource({
      url: TERRAIN_TILE_URL,
      encoding: "terrarium",
      maxzoom: TERRAIN_MAX_ZOOM,
      worker: true,
      cacheSize: 100,
      timeoutMs: 10_000,
    });
    dem.setupMaplibre({ addProtocol });
    this.tiles = dem.contourProtocolUrl({
      thresholds: CONTOUR_THRESHOLDS,
      contourLayer: "contours",
      elevationKey: "ele",
      levelKey: "level",
      extent: 4096,
      buffer: 1,
    });
    // A field attaching above the lines puts itself under the anchor too;
    // the lines go back on top of it. `moveLayer` fires this event again, so
    // only an out-of-place layer is moved.
    this.map.on("styledata", this.keepAboveFields);
  }

  get enabled(): boolean {
    return this.on;
  }

  setEnabled(enabled: boolean): void {
    if (enabled === this.on) return;
    this.on = enabled;
    if (enabled) this.add();
    else this.remove();
    this.options.onChange?.();
  }

  /** Ink for the ground the map is on; a no-op until the lines are drawn. */
  setInk(darkGround: boolean): void {
    if (!this.map.getLayer(CONTOUR_LINE_LAYER)) return;
    const ink = contourInk(darkGround);
    this.map.setPaintProperty(CONTOUR_LINE_LAYER, "line-color", ink.line);
    this.map.setPaintProperty(CONTOUR_CASING_LAYER, "line-color", ink.casing);
    this.map.setPaintProperty(CONTOUR_LABEL_LAYER, "text-color", ink.text);
    this.map.setPaintProperty(CONTOUR_LABEL_LAYER, "text-halo-color", ink.halo);
  }

  private add(): void {
    const map = this.map;
    if (map.getSource(CONTOUR_SOURCE)) return;
    const ink = contourInk(this.options.darkGround());
    map.addSource(CONTOUR_SOURCE, { type: "vector", tiles: [this.tiles], maxzoom: CONTOUR_MAX_ZOOM });
    const width: ExpressionSpecification = ["case", isIndex, 1.4, isFine, 0.5, 0.9];
    // The casing goes under the middle and index lines only: a cased fine
    // line on a dense slope reads as a worm, and the fine tier is faint.
    map.addLayer(
      {
        id: CONTOUR_CASING_LAYER,
        type: "line",
        source: CONTOUR_SOURCE,
        "source-layer": "contours",
        minzoom: CONTOUR_MIN_ZOOM,
        filter: [">=", ["get", "level"], 1],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": ink.casing, "line-width": ["case", isIndex, 3.2, 2.2], "line-blur": 0.8 },
      },
      this.anchor(),
    );
    map.addLayer(
      {
        id: CONTOUR_LINE_LAYER,
        type: "line",
        source: CONTOUR_SOURCE,
        "source-layer": "contours",
        minzoom: CONTOUR_MIN_ZOOM,
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          "line-color": ink.line,
          "line-width": width,
          "line-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            ...FINE_OPACITY.slice(3).flatMap((stop, at, stops) => (at % 2 === 0 ? [stop, ["case", isFine, stops[at + 1], 0.9]] : [])),
          ] as ExpressionSpecification,
        },
      },
      this.anchor(),
    );
    map.addLayer(
      {
        id: CONTOUR_LABEL_LAYER,
        type: "symbol",
        source: CONTOUR_SOURCE,
        "source-layer": "contours",
        minzoom: CONTOUR_MIN_ZOOM,
        filter: isIndex,
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 320,
          "text-field": ["concat", ["number-format", ["get", "ele"], {}], " m"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 10,
          "text-letter-spacing": 0.04,
          "text-max-angle": 25,
          "text-padding": 4,
          "text-rotation-alignment": "map",
          "text-pitch-alignment": "viewport",
        },
        paint: { "text-color": ink.text, "text-halo-color": ink.halo, "text-halo-width": 1.5, "text-halo-blur": 0.5 },
      },
      this.anchor(),
    );
  }

  private remove(): void {
    const map = this.map;
    for (const id of [CONTOUR_LABEL_LAYER, CONTOUR_LINE_LAYER, CONTOUR_CASING_LAYER]) if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(CONTOUR_SOURCE)) map.removeSource(CONTOUR_SOURCE);
  }

  /** The anchor while the style has it; the top of the stack otherwise. */
  private anchor(): string | undefined {
    return this.map.getLayer(this.options.anchorLayer) ? this.options.anchorLayer : undefined;
  }

  private readonly keepAboveFields = (): void => {
    if (!this.on) return;
    const layers = this.map.getStyle().layers ?? [];
    const anchor = layers.findIndex((layer) => layer.id === this.options.anchorLayer);
    if (anchor < 3) return;
    const stacked = [CONTOUR_CASING_LAYER, CONTOUR_LINE_LAYER, CONTOUR_LABEL_LAYER];
    if (stacked.every((id, at) => layers[anchor - 3 + at]?.id === id)) return;
    if (!this.map.getLayer(CONTOUR_LINE_LAYER)) return;
    for (const id of stacked) this.map.moveLayer(id, this.options.anchorLayer);
  };
}
