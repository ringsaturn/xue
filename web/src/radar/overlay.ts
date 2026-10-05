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
import type { CircleLayerSpecification, GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";

import { RadarLayer } from "./layer";
import { GATES, type RadarProduct, type RadarSite, type RadarWindow } from "./schema";
import { RadarSession, SWEEP_MAX_AGE_MS, type RadarSessionStats } from "./session";

const SITES_SOURCE = "radar-sites";
export const RADAR_SITE_LAYER = "radar-sites";
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
      properties: { id: site.id, selected: site.id === selected },
    })),
  };
}

export function siteLayerSpec(ink: string): CircleLayerSpecification {
  return {
    id: RADAR_SITE_LAYER,
    type: "circle",
    source: SITES_SOURCE,
    paint: {
      "circle-radius": ["case", ["boolean", ["get", "selected"], false], 7, 5],
      "circle-color": ["case", ["boolean", ["get", "selected"], false], ink, "rgba(0,0,0,0)"],
      "circle-stroke-color": ink,
      "circle-stroke-width": 2,
    },
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

  setInk(ink: string): void {
    this.ink = ink;
    if (this.map.getLayer(RADAR_SITE_LAYER)) {
      this.map.setPaintProperty(RADAR_SITE_LAYER, "circle-stroke-color", ink);
      this.map.setPaintProperty(RADAR_SITE_LAYER, "circle-color", ["case", ["boolean", ["get", "selected"], false], ink, "rgba(0,0,0,0)"]);
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
    if (!this.map.getLayer(RADAR_SITE_LAYER)) this.map.addLayer(siteLayerSpec(this.ink), before);
    this.added = true;
    this.refresh();
  }

  remove(): void {
    this.session?.close();
    this.session = null;
    this.shown = null;
    if (this.map.getLayer(RADAR_SITE_LAYER)) this.map.removeLayer(RADAR_SITE_LAYER);
    if (this.map.getSource(SITES_SOURCE)) this.map.removeSource(SITES_SOURCE);
    if (this.map.getLayer(this.layer.id)) this.map.removeLayer(this.layer.id);
    this.added = false;
    this.onReadout(null);
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
    this.onReadout({
      site: session.site,
      product: this.product,
      sweepTime: this.shown?.time ?? wanted,
      loading: wanted !== null && sweep === null,
      stale,
    });
  }
}
