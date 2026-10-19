/** A showcase case's markers (`showcase-catalog.ts` `ShowcaseMarker`): a
 * handful of labelled points of context — a place, an event — drawn as one
 * MapLibre symbol layer over everything else on the map, each opening a
 * card with its note and its sources.
 *
 * The time-dependent part is a paint expression: an event the playhead has
 * not reached yet is drawn faint, so moving the playhead sets two paint
 * properties and rebuilds nothing. A place has no time and is always drawn
 * full.
 *
 * The features, the expressions and the card are plain functions so the
 * tests can read them without a map. */

import type { Feature, FeatureCollection, Point } from "geojson";
import type {
  ExpressionSpecification,
  GeoJSONSource,
  Map as MaplibreMap,
  SymbolLayerSpecification,
} from "maplibre-gl";

import { element } from "./dom";
import { formatPointDegrees } from "./format";
import { t } from "./i18n";
import { localizedText, type ShowcaseMarker } from "./showcase-catalog";

const SOURCE = "case-markers";
export const CASE_MARKER_LAYER = "case-markers";
const ICONS = { place: "xue-marker-place", event: "xue-marker-event" } as const;
const ICON_PX = 18;

/** How faint an event is before the playhead reaches it. */
export const MARKER_DIM_OPACITY = 0.35;

/** The glyphs drawn on the map, kept as text for the list under the
 * summary: a diamond for a place, a ringed dot for an event. */
export const MARKER_GLYPHS = { place: "◆", event: "◉" } as const;

/** A place is a slate diamond and an event a red ringed dot, each with a
 * white rim so the two read on a light ground and on a dark one alike. */
function markerIcon(kind: ShowcaseMarker["kind"], pixelRatio: number): ImageData {
  const size = Math.round(ICON_PX * pixelRatio);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("case markers: no 2d context");
  context.scale(size / ICON_PX, size / ICON_PX);
  context.strokeStyle = "rgba(255, 255, 255, 0.95)";
  context.lineWidth = 1.6;
  context.beginPath();
  if (kind === "place") {
    context.fillStyle = "#34506e";
    context.moveTo(9, 1.6);
    context.lineTo(16.4, 9);
    context.lineTo(9, 16.4);
    context.lineTo(1.6, 9);
    context.closePath();
    context.fill();
    context.stroke();
  } else {
    context.fillStyle = "#d2321e";
    context.arc(9, 9, 7.6, 0, Math.PI * 2);
    context.fill();
    context.stroke();
    context.fillStyle = "#ffffff";
    context.beginPath();
    context.arc(9, 9, 2.6, 0, Math.PI * 2);
    context.fill();
  }
  return context.getImageData(0, 0, size, size);
}

/** One feature per marker, its label in `locale`. A marker with no time
 * carries 0, which every playhead is past. */
export function markerFeatures(markers: readonly ShowcaseMarker[], locale: string): FeatureCollection<Point> {
  const features: Feature<Point>[] = markers.map((marker) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: [marker.lon, marker.lat] },
    properties: {
      id: marker.id,
      kind: marker.kind,
      label: localizedText(marker.label, locale),
      time: marker.time ?? 0,
    },
  }));
  return { type: "FeatureCollection", features };
}

/** Whether the playhead has reached a marker: always for one with no time. */
export function markerReached(marker: Pick<ShowcaseMarker, "time">, playhead: number): boolean {
  return marker.time === undefined || marker.time <= playhead;
}

/** Full opacity once the playhead reaches a marker's time, faint before. */
export function markerOpacityExpression(playhead: number): ExpressionSpecification {
  return ["case", ["<=", ["number", ["get", "time"]], playhead], 1, MARKER_DIM_OPACITY];
}

/** The label ink and its halo, as the basemap's own labels are inked. */
export function markerInk(darkGround: boolean): { text: string; halo: string } {
  return darkGround
    ? { text: "#eef1f4", halo: "rgba(0, 0, 0, 0.8)" }
    : { text: "#2b2924", halo: "rgba(255, 255, 255, 0.92)" };
}

function layerSpec(playhead: number, darkGround: boolean): SymbolLayerSpecification {
  const ink = markerInk(darkGround);
  const opacity = markerOpacityExpression(playhead);
  return {
    id: CASE_MARKER_LAYER,
    type: "symbol",
    source: SOURCE,
    layout: {
      "icon-image": ["match", ["get", "kind"], "place", ICONS.place, ICONS.event],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      "text-field": ["get", "label"],
      "text-font": ["Noto Sans Medium"],
      "text-size": 12.5,
      "text-variable-anchor": ["left", "right", "top", "bottom"],
      "text-radial-offset": 0.95,
      "text-justify": "auto",
      "text-max-width": 12,
      // The label gives way to a collision; the mark itself never does.
      "text-optional": true,
      "symbol-sort-key": ["match", ["get", "kind"], "event", 0, 1],
    },
    paint: {
      "icon-opacity": opacity,
      "text-opacity": opacity,
      "text-color": ink.text,
      "text-halo-color": ink.halo,
      "text-halo-width": 1.6,
      "text-halo-blur": 0.4,
    },
  };
}

/** A paint update is a repaint; a minute is below what the marks show. */
const TIME_EPSILON_MS = 60_000;

export class CaseMarkerLayers {
  private added = false;
  private markers: readonly ShowcaseMarker[] = [];
  private locale = "en";
  private time = 0;
  private appliedTime: number | null = null;
  private darkGround = false;

  constructor(private readonly map: MaplibreMap) {}

  /** The case's markers, or none, labelled in `locale`. Nothing is added
   * to the map until a case names one. */
  setMarkers(markers: readonly ShowcaseMarker[], locale: string): void {
    this.markers = markers;
    this.locale = locale;
    if (markers.length === 0 && !this.added) return;
    this.ensure();
    (this.map.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(markerFeatures(markers, locale));
  }

  setLocale(locale: string): void {
    if (locale === this.locale) return;
    this.setMarkers(this.markers, locale);
  }

  setInk(darkGround: boolean): void {
    this.darkGround = darkGround;
    if (!this.added) return;
    const ink = markerInk(darkGround);
    this.map.setPaintProperty(CASE_MARKER_LAYER, "text-color", ink.text);
    this.map.setPaintProperty(CASE_MARKER_LAYER, "text-halo-color", ink.halo);
  }

  setTime(time: number): void {
    this.time = time;
    if (!this.added || (this.appliedTime !== null && Math.abs(time - this.appliedTime) < TIME_EPSILON_MS)) return;
    this.appliedTime = time;
    const opacity = markerOpacityExpression(time);
    this.map.setPaintProperty(CASE_MARKER_LAYER, "icon-opacity", opacity);
    this.map.setPaintProperty(CASE_MARKER_LAYER, "text-opacity", opacity);
  }

  /** The marker under a screen point, if any. */
  markerAt(point: { x: number; y: number }): ShowcaseMarker | null {
    if (!this.added || !this.map.getLayer(CASE_MARKER_LAYER)) return null;
    const box: [[number, number], [number, number]] = [
      [point.x - 8, point.y - 8],
      [point.x + 8, point.y + 8],
    ];
    for (const feature of this.map.queryRenderedFeatures(box, { layers: [CASE_MARKER_LAYER] })) {
      const found = this.markers.find((marker) => marker.id === feature.properties?.id);
      if (found) return found;
    }
    return null;
  }

  private ensure(): void {
    if (this.added) return;
    const pixelRatio = Math.max(1, Math.ceil(window.devicePixelRatio || 1));
    for (const kind of ["place", "event"] as const) {
      if (!this.map.hasImage(ICONS[kind])) this.map.addImage(ICONS[kind], markerIcon(kind, pixelRatio), { pixelRatio });
    }
    if (!this.map.getSource(SOURCE)) this.map.addSource(SOURCE, { type: "geojson", data: markerFeatures([], this.locale) });
    // On top of everything: a marker is the case's own annotation.
    if (!this.map.getLayer(CASE_MARKER_LAYER)) this.map.addLayer(layerSpec(this.time, this.darkGround));
    this.added = true;
    this.appliedTime = this.time;
  }
}

/** The card a marker opens: its label and kind, its time, the note, and
 * the sources it rests on as links. Laid out in the station card's
 * classes so the cards read as one family. */
export function buildMarkerCard(
  marker: ShowcaseMarker,
  options: { locale: string; formatTime(time: number): string },
): HTMLElement {
  const root = element("div", "station-card marker-card");
  root.setAttribute("role", "dialog");
  const label = localizedText(marker.label, options.locale);
  root.setAttribute("aria-label", label);

  const head = element("div", "station-card-head");
  head.append(element("span", "station-card-name", label));
  head.append(element("span", "station-card-kind", t(marker.kind === "place" ? "markerPlace" : "markerEvent")));
  root.append(head);

  if (marker.time !== undefined) {
    const when = element("div", "station-card-time");
    const time = element("time", "", options.formatTime(marker.time));
    time.dateTime = new Date(marker.time).toISOString();
    when.append(time);
    root.append(when);
  }

  root.append(element("p", "marker-card-note", localizedText(marker.note, options.locale)));

  if (marker.sources.length > 0) {
    root.append(element("div", "station-card-kind", t("markerSources")));
    const list = element("ul", "marker-card-sources");
    for (const source of marker.sources) {
      const item = element("li", "");
      const link = element("a", "", source.title);
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
      list.append(item);
    }
    root.append(list);
  }

  root.append(
    element("div", "station-card-position", `${formatPointDegrees(marker.lat, "NS")} ${formatPointDegrees(marker.lon, "EW")}`),
  );
  return root;
}
