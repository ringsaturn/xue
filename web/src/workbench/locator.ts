/**
 * Where the charts are of: a map that places the line and the probe, and draws
 * no field. The basemap is the site's, level and unturned; after it has shown a
 * line or a point its camera is the reader's.
 */

import { Map as MaplibreMap, Marker, setWorkerUrl, type MapMouseEvent } from "maplibre-gl";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

import { applyBasemapStyleDiff, buildBasemapStyle, currentBasemapTheme, luminance, type BasemapStyle } from "../basemap";
import { SectionMapLayer } from "../crosssection/maplayer";
import type { LonLat, SectionLine } from "../crosssection/types";
import { element } from "../dom";
import { t } from "../i18n";
import { greatCircle } from "../measure";
import { registerTerrainProtocol } from "../terrainprotocol";

setWorkerUrl(maplibreWorkerUrl);

export interface LocatorHandlers {
  /** The map was pressed at a place. */
  pressed(at: LonLat): void;
  /** The probe is being moved, and has been let go of. */
  probed(at: LonLat): void;
  settled(): void;
}

/** Pixels kept around a line when it is put in view. */
const PADDING = 48;
const POINT_ZOOM = 4.5;

export class Locator {
  readonly root = element("div", "wb-map");
  private map: MaplibreMap | null = null;
  private layer: SectionMapLayer | null = null;
  private style: BasemapStyle = buildBasemapStyle();
  private ready = false;
  private line: SectionLine | null = null;
  private probe: LonLat | null = null;
  private readonly ends: { letter: "A" | "B"; marker: Marker; button: HTMLElement }[] = [];
  private probeMarker: Marker | null = null;
  private readonly probeNode = element("button", "wb-mark wb-mark-probe");
  /** What was last put in view. */
  private aimed = "";

  constructor(private readonly handlers: LocatorHandlers) {
    this.probeNode.type = "button";
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
    for (const letter of ["A", "B"] as const) {
      const button = element("span", "wb-mark wb-mark-end", letter);
      button.setAttribute("role", "img");
      this.ends.push({ letter, button, marker: new Marker({ element: button }) });
    }
    this.probeMarker = new Marker({ element: this.probeNode, draggable: true });
    this.probeMarker.on("drag", () => {
      const { lng, lat } = this.probeMarker!.getLngLat();
      this.handlers.probed([lng, lat]);
    });
    this.probeMarker.on("dragend", () => this.handlers.settled());
    // A press on a mark is the mark's.
    this.probeNode.addEventListener("click", (event) => event.stopPropagation());
    map.on("click", (event: MapMouseEvent) => this.handlers.pressed([event.lngLat.lng, event.lngLat.lat]));
    map.on("style.load", () => {
      this.ready = true;
      this.layer = new SectionMapLayer(map);
      this.layer.setInk(luminance(currentBasemapTheme().ocean) < 0.5);
      this.render();
    });
    return true;
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
    this.render(changed);
    if (aim) this.aim();
  }

  /** The theme or the language changed. */
  refresh(): void {
    const { map } = this;
    if (map && this.ready) {
      const next = buildBasemapStyle();
      applyBasemapStyleDiff(map, this.style, next);
      this.style = next;
      this.layer?.setInk(luminance(currentBasemapTheme().ocean) < 0.5);
    }
    this.render();
  }

  resize(): void {
    this.map?.resize();
  }

  private render(lineChanged = true): void {
    const { map, line, probe } = this;
    if (!map) return;
    if (this.ready && lineChanged) this.layer?.setLine(line, true);
    const path = line && greatCircle(line.a, line.b);
    for (const { letter, marker, button } of this.ends) {
      button.setAttribute("aria-label", t("wbEndAria", { letter }));
      if (!path) marker.remove();
      else marker.setLngLat(letter === "A" ? path[0]! : path[path.length - 1]!).addTo(map);
    }
    this.probeNode.setAttribute("aria-label", t("wbProbeAria"));
    if (!probe) this.probeMarker?.remove();
    else this.probeMarker?.setLngLat(probe).addTo(map);
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
