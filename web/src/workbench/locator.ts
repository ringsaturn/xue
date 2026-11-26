/**
 * Where the charts are of: a map that places the line and the probe over the
 * level's field (`mapfield.ts`) and the settings sheet's coordinate grids. The
 * basemap is the site's, level and unturned; after it has shown a line or a
 * point its camera is the reader's.
 */

import { Map as MaplibreMap, MapMouseEvent, Marker, setWorkerUrl, type GeoJSONSource, type MapTouchEvent } from "maplibre-gl";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

import { applyBasemapStyleDiff, buildBasemapStyle, currentBasemapTheme, luminance, type BasemapStyle } from "../basemap";
import { SectionMapLayer } from "../crosssection/maplayer";
import type { LonLat, SectionLine } from "../crosssection/types";
import { element } from "../dom";
import { escapes } from "../escapestack";
import { CoordinateGrid } from "../gridlayer";
import { t } from "../i18n";
import { grid } from "../mapprefs";
import { greatCircle } from "../measure";
import { registerTerrainProtocol } from "../terrainprotocol";
import type { LinePreview } from "../volumetool";
import { MapField, type MapFieldView } from "./mapfield";
import { moveLine } from "./moveline";

setWorkerUrl(maplibreWorkerUrl);

export interface LocatorHandlers {
  /** The map was pressed at a place. */
  pressed(at: LonLat): void;
  /** The probe is being moved, and has been let go of or taken back. */
  probed(at: LonLat): void;
  settled(): void;
  cancelled(): void;
  /** A key on the probe steps it, east and north, or along a line by sign. */
  stepped(east: number, north: number): void;
  /** What a line being drawn would be. */
  preview(line: SectionLine): LinePreview;
  /** A line was let go of, the page's to take or refuse. */
  drew(line: SectionLine, keyed: boolean): void;
}

/** Pixels kept around a line when it is put in view. */
const PADDING = 48;
const POINT_ZOOM = 4.5;
/** Degrees a key moves an end of the line, or a point. */
export const KEY_DEGREES = 0.25;
const PREVIEW = "wb-preview";
const GRIP = "wb-line-grip";
/** Pixels a press on the line moves before it is a move: MapLibre's own. */
const GRIP_TOLERANCE = 3;

/** What an arrow key points at, east and north; null for another key. */
function arrowOf(event: KeyboardEvent): [number, number] | null {
  if (event.altKey || event.ctrlKey || event.metaKey) return null;
  switch (event.key) {
    case "ArrowLeft":
      return [-1, 0];
    case "ArrowRight":
      return [1, 0];
    case "ArrowUp":
      return [0, 1];
    case "ArrowDown":
      return [0, -1];
    default:
      return null;
  }
}

type End = "a" | "b";

export class Locator {
  readonly root = element("div", "wb-map");
  private map: MaplibreMap | null = null;
  private layer: SectionMapLayer | null = null;
  private field: MapField | null = null;
  /** The settings sheet's coordinate grids. */
  private grid: CoordinateGrid | null = null;
  private fieldView: MapFieldView | null = null;
  /** What the field is, over its top left corner. */
  private readonly title = element("div", "wb-map-title");
  private style: BasemapStyle = buildBasemapStyle();
  private ready = false;
  private line: SectionLine | null = null;
  private probe: LonLat | null = null;
  private editable = true;
  private readonly ends: { end: End; marker: Marker; button: HTMLButtonElement }[] = [];
  private probeMarker: Marker | null = null;
  private readonly probeNode = element("button", "wb-mark wb-mark-probe");
  private readonly tip = element("span", "wb-map-tip");
  /** The line under the hand, and where the hand took it. */
  private draft: SectionLine | null = null;
  private grip: { from: LonLat; at: { x: number; y: number }; line: SectionLine } | null = null;
  private dragging: "end" | "line" | "probe" | null = null;
  /** The line keys have moved, until it is the page's. */
  private keyed: SectionLine | null = null;
  private readonly surface = { isOpen: () => this.dragging !== null, close: () => this.takeBack() };
  /** What was last put in view. */
  private aimed = "";

  constructor(private readonly handlers: LocatorHandlers) {
    this.probeNode.type = "button";
    this.tip.hidden = true;
  }

  /** Build the map, its element being on the page; false without WebGL. */
  open(): boolean {
    registerTerrainProtocol();
    try {
      this.map = new MaplibreMap({
        container: this.root,
        style: this.style,
        center: [135, 36],
        zoom: 3,
        attributionControl: { compact: true },
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        maxPitch: 0,
      });
    } catch (error) {
      console.warn("workbench: no map:", error instanceof Error ? error.message : error);
      return false;
    }
    const { map } = this;
    map.touchZoomRotate.disableRotation();
    map.keyboard.disableRotation();
    this.root.append(this.title, this.tip);
    this.field = new MapField(map);
    this.field.show(this.fieldView);
    this.grid = new CoordinateGrid({
      map,
      darkGround: () => luminance(currentBasemapTheme().ocean) < 0.5,
      // The edge numbers sit clear of the title and the line and point buttons.
      edgeInsets: () => {
        const outer = this.root.getBoundingClientRect();
        const below = (node: Element | null | undefined): number => {
          const box = node?.getBoundingClientRect();
          return box && box.height > 0 ? box.bottom - outer.top + 6 : 0;
        };
        return { top: Math.max(12, below(this.title.hidden ? null : this.title), below(this.root.parentElement?.querySelector(".wb-shape"))), right: 12, bottom: 28, left: 12 };
      },
    });
    for (const end of ["a", "b"] as const) this.ends.push(this.makeEnd(end));
    this.makeProbe();
    map.on("click", (event: MapMouseEvent) => {
      if (event.originalEvent.target === map.getCanvas()) this.handlers.pressed([event.lngLat.lng, event.lngLat.lat]);
    });
    map.on("mousedown", GRIP, (event) => this.take(event));
    map.on("touchstart", GRIP, (event) => this.take(event));
    map.on("mousemove", (event) => this.pull(event));
    map.on("touchmove", (event) => this.pull(event));
    map.on("mouseup", () => this.drop());
    map.on("touchend", () => this.drop());
    // A mouse let go of outside the map: a mark's drag hears only the map's
    // release, and would follow the mouse back in.
    window.addEventListener("mouseup", () => this.released());
    map.on("mouseenter", GRIP, () => {
      if (this.editable) map.getCanvas().style.cursor = "move";
    });
    map.on("mouseleave", GRIP, () => {
      map.getCanvas().style.cursor = "";
    });
    map.on("style.load", () => {
      this.ready = true;
      this.layer = new SectionMapLayer(map);
      this.layer.setInk(luminance(currentBasemapTheme().ocean) < 0.5);
      map.addSource(PREVIEW, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addSource(GRIP, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: GRIP,
        type: "line",
        source: GRIP,
        layout: { "line-cap": "round" },
        paint: { "line-color": "#000", "line-opacity": 0, "line-width": 18 },
      });
      map.addLayer({
        id: PREVIEW,
        type: "line",
        source: PREVIEW,
        layout: { "line-cap": "round" },
        paint: { "line-color": ["case", ["get", "valid"], "#0b5cad", "#b3541e"], "line-width": 2.5, "line-dasharray": [2, 1.6] },
      });
      this.field?.attach();
      this.field?.setDark(luminance(currentBasemapTheme().ocean) < 0.5);
      this.grid?.setState(grid);
      this.render();
    });
    return true;
  }

  private makeEnd(end: End): { end: End; marker: Marker; button: HTMLButtonElement } {
    const button = element("button", "wb-mark wb-mark-end", end.toUpperCase());
    button.type = "button";
    const marker = new Marker({ element: button, draggable: true });
    const other = (): End => (end === "a" ? "b" : "a");
    const lineAt = (at: LonLat): SectionLine => ({ ...this.line!, [end]: at, [other()]: this.line![other()] });
    marker.on("dragstart", () => this.held("end"));
    marker.on("drag", () => {
      const { lng, lat } = marker.getLngLat();
      if (this.dragging && this.line) this.drawing(lineAt([lng, lat]), map2d(this.map!, [lng, lat]));
    });
    marker.on("dragend", () => this.drop());
    button.addEventListener("click", (event) => event.stopPropagation());
    button.addEventListener("keydown", (event) => {
      const arrow = arrowOf(event);
      if (!arrow || !this.line || !this.editable) return;
      event.preventDefault();
      event.stopPropagation();
      const from = this.keyed ?? this.line;
      const step = KEY_DEGREES * (event.shiftKey ? 10 : 1);
      const at: LonLat = [from[end][0] + arrow[0] * step, Math.max(-90, Math.min(90, from[end][1] + arrow[1] * step))];
      const next: SectionLine = { ...from, [end]: at };
      const preview = this.handlers.preview(next);
      this.keyed = next;
      this.drawing(next, map2d(this.map!, at), preview);
      marker.setLngLat(at);
      this.handlers.drew(next, true);
    });
    return { end, marker, button };
  }

  private makeProbe(): void {
    const marker = new Marker({ element: this.probeNode, draggable: true });
    this.probeMarker = marker;
    marker.on("dragstart", () => this.held("probe"));
    marker.on("drag", () => {
      const { lng, lat } = marker.getLngLat();
      if (this.dragging) this.handlers.probed([lng, lat]);
    });
    marker.on("dragend", () => {
      if (!this.dragging) return;
      this.dragging = null;
      this.handlers.settled();
    });
    // A press on a mark is the mark's.
    this.probeNode.addEventListener("click", (event) => event.stopPropagation());
    this.probeNode.addEventListener("keydown", (event) => {
      const arrow = arrowOf(event);
      if (!arrow) return;
      event.preventDefault();
      event.stopPropagation();
      const stride = event.shiftKey ? 10 : 1;
      this.handlers.stepped(arrow[0] * stride, arrow[1] * stride);
    });
  }

  /** Whether the line can be moved here: on a phone it cannot. */
  setEditable(editable: boolean): void {
    this.editable = editable;
    for (const { marker } of this.ends) marker.setDraggable(editable);
  }

  /** Mark the place read on a chart, which is not where the probe is. */
  setHover(at: LonLat | null): void {
    this.layer?.setHover(at);
  }

  /** Put a line and the probe on the map; where either is new to the page
   * (`aim`), in view. */
  show(line: SectionLine | null, probe: LonLat | null, aim: boolean): void {
    const changed = this.line !== line;
    this.line = line;
    this.probe = probe;
    if (changed) this.undraw();
    this.render(changed);
    if (aim) this.aim();
  }

  /** The line offered is not taken: back to the one there is. */
  refused(): void {
    this.undraw();
    this.render(false);
  }

  /** The theme or the language changed. */
  refresh(): void {
    const { map } = this;
    if (map && this.ready) {
      const next = buildBasemapStyle();
      applyBasemapStyleDiff(map, this.style, next);
      this.style = next;
      this.layer?.setInk(luminance(currentBasemapTheme().ocean) < 0.5);
      this.field?.setDark(luminance(currentBasemapTheme().ocean) < 0.5);
      this.grid?.setState(grid);
      this.grid?.setInk(luminance(currentBasemapTheme().ocean) < 0.5);
    }
    this.render();
  }

  /** The field under the line: a level at a time, or none; and what to call it. */
  setField(view: MapFieldView | null, title: [string, string] | null): void {
    this.fieldView = view;
    this.field?.show(view);
    this.title.hidden = title === null;
    if (title) this.title.replaceChildren(element("b", "", title[0]), element("span", "", title[1]));
  }

  /** Whether the field is still being read for what it was last shown. */
  get fieldBusy(): boolean {
    return this.field?.busy ?? false;
  }

  resize(): void {
    this.map?.resize();
  }

  private held(what: "end" | "line" | "probe"): void {
    this.dragging = what;
    escapes.opened(this.surface);
  }

  private take(event: MapMouseEvent | MapTouchEvent): void {
    if (!this.line || !this.editable || this.dragging || (event instanceof MapMouseEvent && event.originalEvent.button !== 0)) return;
    // A press on a mark (the probe sits on the line) is the mark's own drag.
    if (event.originalEvent.target !== this.map?.getCanvas()) return;
    // The map would pan.
    event.preventDefault();
    this.grip = { from: [event.lngLat.lng, event.lngLat.lat], at: { x: event.point.x, y: event.point.y }, line: this.line };
    this.held("line");
  }

  private pull(event: MapMouseEvent | MapTouchEvent): void {
    if (this.dragging !== "line" || !this.grip) return;
    // A click that shook is not a move: it would read the line again.
    if (!this.draft && Math.hypot(event.point.x - this.grip.at.x, event.point.y - this.grip.at.y) < GRIP_TOLERANCE) return;
    this.drawing(moveLine(this.grip.line, this.grip.from, [event.lngLat.lng, event.lngLat.lat]), event.point);
  }

  private drop(): void {
    if (this.dragging !== "end" && this.dragging !== "line") return;
    const { draft } = this;
    this.dragging = null;
    this.grip = null;
    this.draft = null;
    if (draft) this.handlers.drew(draft, false);
  }

  private released(): void {
    const was = this.dragging;
    if (was === "probe" && this.probeMarker) {
      letGo(this.probeMarker, this.probeNode, this.map);
      this.dragging = null;
      this.handlers.settled();
      return;
    }
    if (was === "end") for (const { marker, button } of this.ends) letGo(marker, button, this.map);
    this.drop();
  }

  private takeBack(): void {
    const was = this.dragging;
    this.dragging = null;
    this.grip = null;
    if (was === "probe") {
      if (this.probeMarker) letGo(this.probeMarker, this.probeNode, this.map);
      this.handlers.cancelled();
      return;
    }
    for (const { marker, button } of this.ends) if (was === "end") letGo(marker, button, this.map);
    this.refused();
  }

  /** Draw what a line would be, and say what is to be said of it. */
  private drawing(line: SectionLine, at: { x: number; y: number }, preview = this.handlers.preview(line)): void {
    this.draft = line;
    const source = this.map?.getSource(PREVIEW) as GeoJSONSource | undefined;
    source?.setData({ type: "Feature", geometry: { type: "LineString", coordinates: preview.path }, properties: { valid: preview.valid } });
    this.tip.textContent = preview.label;
    this.tip.hidden = preview.label === "";
    this.tip.dataset.valid = String(preview.valid);
    this.tip.style.left = `${at.x}px`;
    this.tip.style.top = `${at.y}px`;
  }

  private undraw(): void {
    this.draft = null;
    this.keyed = null;
    this.tip.hidden = true;
    const source = this.map?.getSource(PREVIEW) as GeoJSONSource | undefined;
    source?.setData({ type: "FeatureCollection", features: [] });
  }

  private render(lineChanged = true): void {
    const { map, line, probe } = this;
    if (!map) return;
    const path = line && greatCircle(line.a, line.b);
    if (this.ready && lineChanged) {
      this.layer?.setLine(line, true);
      const source = map.getSource(GRIP) as GeoJSONSource | undefined;
      source?.setData({ type: "FeatureCollection", features: path ? [{ type: "Feature", geometry: { type: "LineString", coordinates: path }, properties: {} }] : [] });
      // The hand's layer stays over the line's, which are put on anew.
      if (map.getLayer(GRIP)) map.moveLayer(GRIP);
      if (map.getLayer(PREVIEW)) map.moveLayer(PREVIEW);
    }
    for (const { end, marker, button } of this.ends) {
      button.setAttribute("aria-label", t("wbEndAria", { letter: end.toUpperCase() }));
      if (!path) marker.remove();
      else if (this.dragging !== "end" && !this.keyed) put(marker, button, map, end === "a" ? path[0]! : path[path.length - 1]!);
    }
    this.probeNode.setAttribute("aria-label", t("wbProbeAria"));
    if (!probe) this.probeMarker?.remove();
    else if (this.probeMarker && this.dragging !== "probe") put(this.probeMarker, this.probeNode, map, probe);
  }

  private aim(): void {
    const { map, line, probe } = this;
    const key = line ? `${line.a},${line.b}` : String(probe);
    if (!map || key === this.aimed) return;
    this.aimed = key;
    if (!line) {
      if (probe) map.jumpTo({ center: probe, zoom: Math.max(map.getZoom(), POINT_ZOOM) });
      return;
    }
    const path = greatCircle(line.a, line.b);
    const lons = path.map(([lon]) => lon);
    const lats = path.map(([, lat]) => lat);
    map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: PADDING, animate: false, maxZoom: 7 });
  }
}

/** Place a mark, adding it only where it is not on the map: `addTo` removes it
 * first, and a removed marker drops its drag mid-way (MapLibre unhooks the
 * release, leaving the mark deaf to the pointer and the drag never ended). */
function put(marker: Marker, node: HTMLElement, map: MaplibreMap, at: LonLat): void {
  marker.setLngLat(at);
  if (!node.isConnected) marker.addTo(map);
}

/** End a mark's drag at once, by taking it off the map and putting it back. */
function letGo(marker: Marker, node: HTMLElement, map: MaplibreMap | null): void {
  if (!map || !node.isConnected) return;
  marker.remove().addTo(map);
  node.style.pointerEvents = "";
}

function map2d(map: MaplibreMap, at: LonLat): { x: number; y: number } {
  return map.project(at);
}
