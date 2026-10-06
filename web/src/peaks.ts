/**
 * Named peaks over the 3D relief.
 *
 * Protomaps tiles carry OSM peaks only from zoom 11–14, so the mountains a
 * regional view should name are absent below that. `peaks.json` fills the
 * gap: peaks from Wikidata with a topographic prominence, each with the zoom
 * it first shows at (`scripts/build_peaks.py`). Prominence also ranks them
 * when labels collide, so a massif's summit wins over its shoulders.
 *
 * The layer sits above the basemap's own POIs. MapLibre places the upper
 * layer first, so where a tile's OSM peak and this one name the same summit
 * the tile's copy loses the collision and drops out.
 *
 * The file is fetched the first time the relief is switched on, never on a
 * flat map.
 */

import type { Map as MaplibreMap, SymbolLayerSpecification } from "maplibre-gl";

const PEAK_SOURCE = "peaks";
export const PEAK_LAYER = "peaks";
const PEAKS_URL = "peaks.json";
/** The basemap layer the peaks go under: place names stay on top. */
const BEFORE_LAYER = "places_subplace";
/** The mark is drawn here rather than taken from the flavor's sprite: the
 * white flavor's sprite has no peak icon. */
const PEAK_ICON = "xue-peak";
const PEAK_ICON_PX = 18;

/** A round badge with a white two-summit glyph, legible on both grounds. */
function peakIcon(pixelRatio: number): ImageData {
  const size = Math.round(PEAK_ICON_PX * pixelRatio);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("peaks: no 2d context");
  context.scale(size / 18, size / 18);
  context.fillStyle = "#6e6259";
  context.strokeStyle = "rgba(255, 255, 255, 0.9)";
  context.lineWidth = 1.2;
  context.beginPath();
  context.arc(9, 9, 8.2, 0, Math.PI * 2);
  context.fill();
  context.stroke();
  context.fillStyle = "#ffffff";
  context.beginPath();
  context.moveTo(3.4, 12.6);
  context.lineTo(7.6, 5.4);
  context.lineTo(9.9, 9.2);
  context.lineTo(11.3, 7.4);
  context.lineTo(14.6, 12.6);
  context.closePath();
  context.fill();
  return context.getImageData(0, 0, size, size);
}

/** The label: the name in the basemap language (English when Wikidata has
 * none), and the height on a second, smaller line. */
function peakText(basemapLang: string, htmlLang: string): SymbolLayerSpecification["layout"] {
  return {
    "text-field": [
      "format",
      ["coalesce", ["get", `name:${basemapLang}`], ["get", "name"]],
      {},
      "\n",
      {},
      ["concat", ["number-format", ["get", "elevation"], { locale: htmlLang }], " m"],
      { "font-scale": 0.85 },
    ],
  };
}

function peakLayer(basemapLang: string, htmlLang: string): SymbolLayerSpecification {
  return {
    id: PEAK_LAYER,
    type: "symbol",
    source: PEAK_SOURCE,
    filter: [">=", ["zoom"], ["get", "min_zoom"]],
    layout: {
      ...peakText(basemapLang, htmlLang),
      "icon-image": PEAK_ICON,
      "text-font": ["Noto Sans Medium"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 3, 11, 10, 13],
      "text-line-height": 1.15,
      "text-variable-anchor": ["left", "right"],
      "text-radial-offset": 0.9,
      "text-justify": "auto",
      "text-max-width": 9,
      "symbol-sort-key": ["-", ["get", "prominence"]],
    },
    paint: {
      "text-halo-width": 1.4,
    },
  };
}

export class PeakLabels {
  private added = false;
  private visible = false;

  constructor(
    private readonly map: MaplibreMap,
    private readonly language: () => { basemapLang: string; htmlLang: string },
  ) {}

  /** Show or hide the peaks; the first show adds the source and fetches the
   * file. Returns whether the layer was added just now, so the caller can
   * ink it like the basemap's own labels. */
  setVisible(visible: boolean): boolean {
    this.visible = visible;
    if (!visible && !this.added) return false;
    const map = this.map;
    let addedNow = false;
    if (!this.added) {
      const pixelRatio = Math.max(1, Math.ceil(window.devicePixelRatio || 1));
      if (!map.hasImage(PEAK_ICON)) map.addImage(PEAK_ICON, peakIcon(pixelRatio), { pixelRatio });
      map.addSource(PEAK_SOURCE, { type: "geojson", data: PEAKS_URL, attribution: "Wikidata" });
      const { basemapLang, htmlLang } = this.language();
      map.addLayer(peakLayer(basemapLang, htmlLang), map.getLayer(BEFORE_LAYER) ? BEFORE_LAYER : undefined);
      this.added = true;
      addedNow = true;
    }
    map.setLayoutProperty(PEAK_LAYER, "visibility", this.visible ? "visible" : "none");
    return addedNow;
  }

  /** Follow the UI language: the name key and the digit grouping. */
  syncLanguage(): void {
    if (!this.added) return;
    const { basemapLang, htmlLang } = this.language();
    const layout = peakText(basemapLang, htmlLang);
    this.map.setLayoutProperty(PEAK_LAYER, "text-field", layout?.["text-field"]);
  }
}
