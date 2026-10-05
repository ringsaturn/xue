/** The single-site radar as an overlay: the sites of a window as marks, the
 * chosen site's sweep under the playhead as a polar layer, and the chip that
 * names what is shown.
 *
 * It follows the playhead and never holds it, like the station marks: the
 * playhead's valid time picks each frame's sweep — the newest at or before
 * it, within `SWEEP_MAX_AGE_MS` — and a sweep still on its way leaves the
 * last one up rather than a blank. A playhead past the window (a forecast
 * dataset under it) shows the site's newest sweep, faded, so the overlay
 * reads as "the latest radar" instead of disappearing. */

import type { FeatureCollection } from "geojson";
import type { GeoJSONSource, Map as MaplibreMap, SymbolLayerSpecification } from "maplibre-gl";

import { RadarLayer } from "./layer";
import { GATES, type RadarProduct, type RadarSite, type RadarWindow } from "./schema";
import { RadarSession, SWEEP_MAX_AGE_MS, type RadarSessionStats } from "./session";

const SITES_SOURCE = "radar-sites";
export const RADAR_SITE_LAYER = "radar-sites";
const SITE_LABEL_LAYER = "radar-site-labels";
/** Site names appear from this zoom: below it the marks are a network,
 * above it a choice between neighbours. */
const LABEL_ZOOM = 5;
const LOWEST_SWEEP_DEGREES = 0.5;
/** The opacity of the newest sweep shown for a playhead past the window. */
const FADED = 0.5;
const SHOWN = 0.88;

export interface RadarReadout {
  site: RadarSite;
  product: RadarProduct;
  /** The sweep on screen, or the one the playhead stands on while it is on
   * its way; null when the site has none near the playhead. */
  sweepTime: number | null;
  /** How far the sweep on screen is behind the playhead, milliseconds. */
  age: number | null;
  loading: boolean;
  stale: boolean;
}

/** A budget the decoded rounds stay under: about forty rounds of a busy
 * site on a desktop, a quarter of that on a phone-sized screen. */
export function radarBudgetBytes(): number {
  const narrow = typeof window !== "undefined" && window.matchMedia?.("(max-width: 720px)").matches;
  return (narrow ? 96 : 384) * 1024 * 1024;
}

export function siteFeatures(window: RadarWindow | null, selected: string | null): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (window?.sites ?? []).map((site) => ({
      type: "Feature",
      id: site.id,
      geometry: { type: "Point", coordinates: [site.lon, site.lat] },
      properties: { id: site.id, icao: site.icao, selected: site.id === selected },
    })),
  };
}

const GLYPH_OFF = "radar-site-off";
const GLYPH_ON = "radar-site-on";
/** CSS pixels across the glyph; drawn at twice that for sharpness. */
const GLYPH_SIZE = 20;
const GLYPH_RATIO = 2;

/** A radar site's mark: the WSR-88D's own silhouette, a radome — the
 * "golf ball" cut flat at its base — on a short lattice tower. Not a dot or
 * a ring (the probe pin's and the station marks' shapes) and no arcs or
 * waves (which read as a Wi-Fi logo): the thing itself, which is
 * recognisable at 16 px. Paper inside an ink outline when idle, inked solid
 * with the lattice in paper when chosen. */
export function radarGlyph(ink: string, paper: string, selected: boolean): ImageData {
  const size = GLYPH_SIZE * GLYPH_RATIO;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d")!;
  context.scale(GLYPH_RATIO, GLYPH_RATIO);
  context.lineJoin = "round";
  const fill = selected ? ink : paper;
  const detail = selected ? paper : ink;
  const centre = GLYPH_SIZE / 2;
  // The radome: a sphere cut flat a little below its middle.
  const domeY = 7.2;
  const domeR = 6.2;
  const cut = 3.6; // below the centre
  const cutHalf = Math.sqrt(domeR * domeR - cut * cut);
  const start = Math.PI - Math.asin(cut / domeR);
  const end = Math.asin(cut / domeR);
  // The tower: a frustum from the dome's cut to the ground.
  const towerTop = domeY + cut;
  const ground = GLYPH_SIZE - 1.2;
  const topHalf = cutHalf * 0.55;
  const footHalf = cutHalf * 1.05;
  context.beginPath();
  context.moveTo(centre - topHalf, towerTop);
  context.lineTo(centre - footHalf, ground);
  context.lineTo(centre + footHalf, ground);
  context.lineTo(centre + topHalf, towerTop);
  context.closePath();
  context.fillStyle = fill;
  context.fill();
  context.lineWidth = 1.5;
  context.strokeStyle = ink;
  context.stroke();
  // The lattice: one X between the legs.
  context.beginPath();
  context.moveTo(centre - topHalf, towerTop);
  context.lineTo(centre + footHalf, ground);
  context.moveTo(centre + topHalf, towerTop);
  context.lineTo(centre - footHalf, ground);
  context.lineWidth = 1;
  context.strokeStyle = detail;
  context.stroke();
  // The dome over the tower's top.
  context.beginPath();
  context.arc(centre, domeY, domeR, start, end + Math.PI * 2, false);
  context.closePath();
  context.fillStyle = fill;
  context.fill();
  context.lineWidth = 1.5;
  context.strokeStyle = ink;
  context.stroke();
  return context.getImageData(0, 0, size, size);
}

export function siteLayerSpec(): SymbolLayerSpecification {
  return {
    id: RADAR_SITE_LAYER,
    type: "symbol",
    source: SITES_SOURCE,
    layout: {
      "icon-image": ["case", ["boolean", ["get", "selected"], false], GLYPH_ON, GLYPH_OFF],
      // The tower stands on its site.
      "icon-anchor": "bottom",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      // The chosen site over its neighbours.
      "symbol-sort-key": ["case", ["boolean", ["get", "selected"], false], 1, 0],
    },
  };
}

/** The site's ICAO id beside its mark, in the basemap's own label font so
 * no glyphs are fetched for it. */
export function siteLabelSpec(ink: string, halo: string, font: string[]): SymbolLayerSpecification {
  return {
    id: SITE_LABEL_LAYER,
    type: "symbol",
    source: SITES_SOURCE,
    minzoom: LABEL_ZOOM,
    layout: {
      "text-field": ["get", "icao"],
      "text-font": font,
      "text-size": 11,
      "text-offset": [0, 0.3],
      "text-anchor": "top",
      "text-allow-overlap": false,
    },
    paint: { "text-color": ink, "text-halo-color": halo, "text-halo-width": 1.4 },
  };
}

export class RadarOverlay {
  private readonly layer = new RadarLayer();
  private window: RadarWindow | null = null;
  private windowUrl = "";
  private session: RadarSession | null = null;
  private product: RadarProduct = "n0b";
  private time: number | null = null;
  private added = false;
  private shown: { key: string; time: number } | null = null;
  private ink = "#1d2430";
  private halo = "#f3efe6";

  constructor(
    private readonly map: MaplibreMap,
    private readonly onReadout: (readout: RadarReadout | null) => void,
  ) {}

  get siteId(): string | null {
    return this.session?.site.id ?? null;
  }

  get currentProduct(): RadarProduct {
    return this.product;
  }

  get sites(): readonly RadarSite[] {
    return this.window?.sites ?? [];
  }

  /** The window to draw from (a case's or the live one), or none. */
  setWindow(window: RadarWindow | null, windowUrl: string): void {
    if (window === this.window && windowUrl === this.windowUrl) return;
    const site = this.siteId;
    this.window = window;
    this.windowUrl = windowUrl;
    this.session?.close();
    this.session = null;
    this.shown = null;
    if (window && site) this.setSite(site);
    this.publishSites();
    this.refresh();
  }

  /** Draw this site, or none. A site the window does not have draws none. */
  setSite(id: string | null): void {
    if (id === this.siteId && this.session) return;
    this.session?.close();
    this.session = null;
    this.shown = null;
    const index = id === null || !this.window ? -1 : this.window.sites.findIndex((site) => site.id === id);
    if (index >= 0) {
      this.session = new RadarSession(this.window!, this.windowUrl, index, this.product, radarBudgetBytes(), () => this.refresh());
      if (this.time !== null) this.session.setTime(this.time);
    }
    this.publishSites();
    this.refresh();
  }

  setProduct(product: RadarProduct): void {
    if (product === this.product) return;
    this.product = product;
    this.shown = null;
    this.session?.setProduct(product);
    this.refresh();
  }

  setTime(time: number | null): void {
    this.time = time;
    if (time !== null) this.session?.setTime(this.clamp(time));
    this.refresh();
  }

  setInk(ink: string, halo = this.halo): void {
    if (ink === this.ink && halo === this.halo && this.map.hasImage(GLYPH_ON)) return;
    this.ink = ink;
    this.halo = halo;
    if (this.map.getLayer(SITE_LABEL_LAYER)) {
      this.map.setPaintProperty(SITE_LABEL_LAYER, "text-color", ink);
      this.map.setPaintProperty(SITE_LABEL_LAYER, "text-halo-color", halo);
    }
    this.putGlyphs();
  }

  /** The two glyphs in the current ink, added or replaced. */
  private putGlyphs(): void {
    for (const [name, selected] of [[GLYPH_OFF, false], [GLYPH_ON, true]] as const) {
      const image = radarGlyph(this.ink, this.halo, selected);
      if (this.map.hasImage(name)) this.map.updateImage(name, image);
      else this.map.addImage(name, image, { pixelRatio: GLYPH_RATIO });
    }
  }

  /** Add the layers once the style is loaded: the sweep under `before`, the
   * site marks over it. */
  ensure(before?: string): void {
    if (this.added) return;
    if (!this.map.getLayer(this.layer.id)) this.map.addLayer(this.layer, before);
    if (!this.map.getSource(SITES_SOURCE)) {
      this.map.addSource(SITES_SOURCE, { type: "geojson", data: siteFeatures(this.window, this.siteId), promoteId: "id" });
    }
    this.putGlyphs();
    if (!this.map.getLayer(RADAR_SITE_LAYER)) this.map.addLayer(siteLayerSpec(), before);
    const font = this.labelFont();
    if (font && !this.map.getLayer(SITE_LABEL_LAYER)) this.map.addLayer(siteLabelSpec(this.ink, this.halo, font));
    this.added = true;
    this.refresh();
  }

  remove(): void {
    this.session?.close();
    this.session = null;
    this.shown = null;
    if (this.map.getLayer(SITE_LABEL_LAYER)) this.map.removeLayer(SITE_LABEL_LAYER);
    if (this.map.getLayer(RADAR_SITE_LAYER)) this.map.removeLayer(RADAR_SITE_LAYER);
    if (this.map.getSource(SITES_SOURCE)) this.map.removeSource(SITES_SOURCE);
    if (this.map.getLayer(this.layer.id)) this.map.removeLayer(this.layer.id);
    this.added = false;
    this.onReadout(null);
  }

  /** The basemap's own label font, so the site names cost no glyph
   * request of their own; none when the style draws no text. */
  private labelFont(): string[] | null {
    for (const layer of this.map.getStyle().layers ?? []) {
      if (layer.type !== "symbol") continue;
      const font = (layer.layout as Record<string, unknown> | undefined)?.["text-font"];
      if (Array.isArray(font) && font.every((item) => typeof item === "string")) return font as string[];
    }
    return null;
  }

  stats(): RadarSessionStats | null {
    return this.session?.statsSnapshot() ?? null;
  }

  /** The window's sweeps end at its newest; a playhead past them stands on
   * the newest, shown faded. */
  private newest(): number | null {
    const times = this.session?.sweepTimes() ?? [];
    return times.length ? times[times.length - 1]! : null;
  }

  private clamp(time: number): number {
    const newest = this.newest();
    return newest !== null && time > newest + SWEEP_MAX_AGE_MS ? newest : time;
  }

  private publishSites(): void {
    const source = this.map.getSource(SITES_SOURCE) as GeoJSONSource | undefined;
    source?.setData(siteFeatures(this.window, this.siteId));
  }

  private refresh(): void {
    const session = this.session;
    if (!session) {
      this.layer.set(null);
      this.onReadout(null);
      return;
    }
    const playhead = this.time ?? this.newest() ?? Date.now();
    const at = this.clamp(playhead);
    const stale = at !== playhead;
    const sweep = session.sweepAt(at);
    const wanted = session.sweepTimeAt(at);
    if (sweep) this.shown = { key: `${session.site.id}:${this.product}:${sweep.time}`, time: sweep.time };
    if (sweep) {
      this.layer.set({
        latitude: session.site.lat,
        longitude: session.site.lon,
        product: this.product,
        gates: GATES[this.product],
        elevation: LOWEST_SWEEP_DEGREES,
        codes: sweep.codes,
        key: this.shown!.key,
        opacity: stale ? FADED : SHOWN,
      });
    } else if (wanted === null) {
      // Nothing near the playhead: the site is quiet then, so nothing shows.
      this.layer.set(null);
      this.shown = null;
    }
    // A sweep on its way leaves the last one drawn.
    const sweepTime = this.shown?.time ?? wanted;
    this.onReadout({
      site: session.site,
      product: this.product,
      sweepTime,
      age: sweepTime === null ? null : Math.max(0, playhead - sweepTime),
      loading: wanted !== null && sweep === null,
      stale,
    });
  }
}
