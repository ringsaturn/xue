import type { Map as MaplibreMap } from "maplibre-gl";
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
 * whenever a field attaches above them. Their ink follows the ground like
 * the coast's does (`setInk`).
 */

/** The zoom the lines appear from: a 200 m interval on a z12 DEM is a
 * scribble wider out, and the shadows draw from the same zoom. */
export const CONTOUR_MIN_ZOOM = 9;
/** The deepest contour tile asked for; past it MapLibre overzooms the last. */
export const CONTOUR_MAX_ZOOM = 15;

/** The minor and major intervals in metres by zoom: what a topographic map
 * prints at that scale, with the major line every fifth. A zoom without an
 * entry uses the next lower one. */
export const CONTOUR_THRESHOLDS: Record<number, [number, number]> = {
  9: [200, 1000],
  11: [100, 500],
  13: [50, 250],
  14: [20, 100],
};

export const CONTOUR_SOURCE = "terrain-contours";
export const CONTOUR_LINE_LAYER = "terrain-contour-lines";
export const CONTOUR_LABEL_LAYER = "terrain-contour-labels";

export interface ContourInk {
  line: string;
  text: string;
  halo: string;
}

/** The lines' ink per ground: a warm brown on paper, as a survey sheet
 * prints its relief, and a pale sand on the dark ground; both translucent so
 * the field shows through and a minor line stays lighter than the coast. */
export function contourInk(darkGround: boolean): ContourInk {
  return darkGround
    ? { line: "rgba(255, 226, 180, 0.55)", text: "#f3e6cc", halo: "rgba(0, 0, 0, 0.75)" }
    : { line: "rgba(120, 82, 40, 0.6)", text: "#5a3e1c", halo: "rgba(243, 239, 230, 0.92)" };
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
    this.map.setPaintProperty(CONTOUR_LABEL_LAYER, "text-color", ink.text);
    this.map.setPaintProperty(CONTOUR_LABEL_LAYER, "text-halo-color", ink.halo);
  }

  private add(): void {
    const map = this.map;
    if (map.getSource(CONTOUR_SOURCE)) return;
    const ink = contourInk(this.options.darkGround());
    map.addSource(CONTOUR_SOURCE, { type: "vector", tiles: [this.tiles], maxzoom: CONTOUR_MAX_ZOOM });
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
          "line-width": ["case", [">=", ["get", "level"], 1], 1.3, 0.6],
          "line-opacity": ["case", [">=", ["get", "level"], 1], 1, 0.8],
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
        filter: [">=", ["get", "level"], 1],
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
    for (const id of [CONTOUR_LABEL_LAYER, CONTOUR_LINE_LAYER]) if (map.getLayer(id)) map.removeLayer(id);
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
    if (anchor < 2) return;
    if (layers[anchor - 1]?.id === CONTOUR_LABEL_LAYER && layers[anchor - 2]?.id === CONTOUR_LINE_LAYER) return;
    if (!this.map.getLayer(CONTOUR_LINE_LAYER)) return;
    this.map.moveLayer(CONTOUR_LINE_LAYER, this.options.anchorLayer);
    this.map.moveLayer(CONTOUR_LABEL_LAYER, this.options.anchorLayer);
  };
}
