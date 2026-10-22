/**
 * A showcase case's river data on the map (`docs/river.md` §4): the
 * reaches of a network case as lines coloured by flow (`layer.ts`), the
 * gauges as hollow circles, the mapped flood extents as fills from the
 * time of their imagery on, and the case-panel controls that choose the
 * track and hide the extents.
 *
 * It follows the playhead and never holds it: a time before a track's
 * first frame draws no reaches, a time between two frames draws their
 * values blended linearly, as the field blends its planes. Geometry is
 * read by row group as the view needs it; values are read for the row
 * groups whose geometry is in hand.
 */

import type { FeatureCollection, Point } from "geojson";
import type { GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";

import { fetchImmutable } from "../fetchimmutable";
import { formatUtcHour } from "../format";
import { t } from "../i18n";
import { ZarrStore } from "../zarr/store";
import { loadDecodeChunk } from "./decode";
import type { TimedSeries } from "./hydrograph";
import { riverFileUrl, riverPathUrl, type LoadedRiverIndex, type RiverGauge, type RiverTrack } from "./index";
import { RiverLayer, RIVER_LAYER_ID, type RiverRamp } from "./layer";
import { riverClassColors } from "./palette";
import type { LonLatBox, ReachFile } from "./parquet";
import { blendValues, framePair, NetworkStore } from "./store";

export const RIVER_GAUGE_LAYER = "river-gauges";
const GAUGE_SOURCE = "river-gauges";
const EXTENT_PREFIX = "river-extent-";

type ExtentState = "idle" | "loading" | "ready" | "failed";

/** The track a case opens on: its analysis, else its first. */
export function defaultTrackId(tracks: readonly RiverTrack[]): string | null {
  return (tracks.find((track) => track.id === "analysis") ?? tracks.find((track) => track.kind === "analysis") ?? tracks[0])?.id ?? null;
}

/** A track's name in the switch: the analysis, or the forecast by cycle. */
export function trackLabel(track: RiverTrack): string {
  return track.kind === "analysis" ? t("riverTrackAnalysis") : t("riverTrackForecast", { cycle: formatUtcHour(track.runTime) });
}

/** The gauge marks' ink, on the ground the map is on. */
export function gaugeInk(darkGround: boolean): { stroke: string; halo: string } {
  return darkGround ? { stroke: "#dff1ff", halo: "rgba(0, 0, 0, 0.6)" } : { stroke: "#174a78", halo: "rgba(255, 255, 255, 0.85)" };
}

function gaugeFeatures(gauges: readonly RiverGauge[]): FeatureCollection<Point> {
  return {
    type: "FeatureCollection",
    features: gauges.map((gauge) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [gauge.lon, gauge.lat] },
      properties: { id: gauge.id },
    })),
  };
}

export interface RiverCaseOptions {
  map: MaplibreMap;
  loaded: LoadedRiverIndex;
  /** The case's `bbox`: a view holding it whole reads every row group. */
  bbox: LonLatBox;
  /** Whether the reaches are coloured through a bundle's palette
   * (`setRamp`), the catalog row's `river.color`, rather than by their
   * ratio to their usual flow. */
  byRamp?: boolean;
  /** Called when something the shell shows beside the map changed: the
   * credits (an extent came up), the track, a gauge's model series. */
  onChange: () => void;
}

export class RiverCase {
  readonly loaded: LoadedRiverIndex;
  private readonly map: MaplibreMap;
  private readonly bbox: LonLatBox;
  private readonly onChange: () => void;
  private readonly byRamp: boolean;
  private ramp: RiverRamp | null = null;
  private layer: RiverLayer | null = null;
  /** The GeoParquet's footer and the reader, which is loaded with it
   * (hyparquet is only needed on a network case). */
  private reaches: Promise<{ file: ReachFile; parquet: typeof import("./parquet") }> | null = null;
  private readonly loadingGroups = new Set<number>();
  private readonly loadedGroups = new Set<number>();
  private trackId: string | null;
  private readonly stores = new Map<string, Promise<NetworkStore>>();
  private store: NetworkStore | null = null;
  private readonly frames = new Map<string, Promise<Float32Array>>();
  private readonly series = new Map<string, TimedSeries | "pending">();
  private scratch: Float32Array | null = null;
  private playhead = Number.NaN;
  private applySequence = 0;
  private extentsOn = true;
  private readonly extentState: ExtentState[];
  private shownExtents = new Set<number>();
  private darkGround = false;
  private disposed = false;

  constructor(options: RiverCaseOptions) {
    this.map = options.map;
    this.loaded = options.loaded;
    this.bbox = options.bbox;
    this.onChange = options.onChange;
    this.byRamp = options.byRamp ?? false;
    this.trackId = defaultTrackId(options.loaded.index.tracks);
    this.extentState = options.loaded.index.extent.map(() => "idle");
  }

  get index() {
    return this.loaded.index;
  }

  get tracks(): readonly RiverTrack[] {
    return this.loaded.index.tracks;
  }

  get currentTrack(): string | null {
    return this.trackId;
  }

  get extentsVisible(): boolean {
    return this.extentsOn;
  }

  /** Put the layers on the map and start reading. */
  start(): void {
    const index = this.loaded.index;
    if (index.geometry) {
      this.layer = new RiverLayer(index.geometry.featureCount, this.byRamp);
      if (this.ramp) this.layer.setRamp(this.ramp);
      this.map.addLayer(this.layer, this.firstSymbolLayer());
      const geometry = index.geometry;
      const url = riverFileUrl(this.loaded.indexUrl, geometry.path, geometry.crc32);
      this.reaches = import("./parquet").then(async (parquet) => ({
        file: await parquet.openReaches(parquet.rangeAsyncBuffer(url, geometry.byteLength), geometry),
        parquet,
      }));
      this.reaches.catch((error: unknown) => this.warn("reaches not read", error));
      this.viewChanged();
      if (this.trackId) this.setTrack(this.trackId);
    }
    if (index.gauges.length > 0) {
      this.map.addSource(GAUGE_SOURCE, { type: "geojson", data: gaugeFeatures(index.gauges) });
      const ink = gaugeInk(this.darkGround);
      this.map.addLayer(
        {
          id: RIVER_GAUGE_LAYER,
          type: "circle",
          source: GAUGE_SOURCE,
          paint: {
            "circle-radius": 5.5,
            "circle-color": "rgba(0, 0, 0, 0)",
            "circle-stroke-width": 2.2,
            "circle-stroke-color": ink.stroke,
          },
        },
        this.firstSymbolLayer(),
      );
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.layer && this.map.getLayer(RIVER_LAYER_ID)) this.map.removeLayer(RIVER_LAYER_ID);
    if (this.map.getLayer(RIVER_GAUGE_LAYER)) this.map.removeLayer(RIVER_GAUGE_LAYER);
    if (this.map.getSource(GAUGE_SOURCE)) this.map.removeSource(GAUGE_SOURCE);
    this.extentState.forEach((_, position) => {
      const id = `${EXTENT_PREFIX}${position}`;
      if (this.map.getLayer(id)) this.map.removeLayer(id);
      if (this.map.getSource(id)) this.map.removeSource(id);
    });
    this.layer = null;
  }

  private warn(what: string, error: unknown): void {
    console.warn(`river: ${what}:`, error instanceof Error ? error.message : error);
  }

  private firstSymbolLayer(): string | undefined {
    return (this.map.getStyle().layers ?? []).find((entry) => entry.type === "symbol")?.id;
  }

  /** The view's box, or null when it holds the whole case. */
  private viewBox(): LonLatBox | null {
    const bounds = this.map.getBounds();
    const view: LonLatBox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
    const [west, south, east, north] = this.bbox;
    if (view[0] <= west && view[1] <= south && view[2] >= east && view[3] >= north) return null;
    return view;
  }

  /** Read the row groups the view needs and does not have. */
  viewChanged(): void {
    const reaches = this.reaches;
    if (!reaches || !this.layer) return;
    const view = this.viewBox();
    void reaches.then(async ({ file, parquet }) => {
      const wanted = parquet.rowGroupsMeeting(file.groups, view).filter((group) => !this.loadedGroups.has(group) && !this.loadingGroups.has(group));
      await Promise.all(
        wanted.map(async (group) => {
          this.loadingGroups.add(group);
          try {
            const lines = await parquet.readRowGroupLines(file, group);
            if (this.disposed) return;
            this.layer?.setGroup(group, file.groups[group]!.rowStart, lines);
            this.loadedGroups.add(group);
          } catch (error) {
            this.warn(`row group ${group} not read`, error);
          } finally {
            this.loadingGroups.delete(group);
          }
        }),
      );
      if (wanted.length > 0) void this.apply();
    }, () => undefined);
  }

  /** Switch to a track (analysis or a forecast cycle). */
  setTrack(id: string): void {
    const track = this.tracks.find((item) => item.id === id);
    if (!track) return;
    this.trackId = id;
    this.store = null;
    this.layer?.setValues(null);
    let opening = this.stores.get(id);
    if (!opening) {
      const url = riverPathUrl(this.loaded.indexUrl, track.store.path);
      opening = loadDecodeChunk().then((decode) =>
        NetworkStore.open(new ZarrStore(url, track.store.crc32), decode, this.loaded.index.geometry),
      );
      opening.catch((error: unknown) => {
        this.stores.delete(id);
        this.warn(`track ${id} not opened`, error);
      });
      this.stores.set(id, opening);
    }
    void opening.then((store) => {
      if (this.disposed || this.trackId !== id) return;
      this.store = store;
      this.layer?.setBaseflow(store.baseflow);
      void this.apply();
      this.onChange();
    }, () => undefined);
    this.onChange();
  }

  /** The palette and codebook of the bundle the reaches are coloured
   * through; the reaches are not drawn until it is given. */
  setRamp(ramp: RiverRamp): void {
    this.ramp = ramp;
    this.layer?.setRamp(ramp);
  }

  /** Show or hide the flood extents. */
  setExtentsVisible(on: boolean): void {
    this.extentsOn = on;
    this.syncExtents();
  }

  setInk(darkGround: boolean): void {
    this.darkGround = darkGround;
    if (this.map.getLayer(RIVER_GAUGE_LAYER)) this.map.setPaintProperty(RIVER_GAUGE_LAYER, "circle-stroke-color", gaugeInk(darkGround).stroke);
  }

  /** Follow the playhead: the case's valid time, epoch milliseconds,
   * fractional between two frames while the field blends. */
  setTime(time: number): void {
    if (time === this.playhead) return;
    this.playhead = time;
    this.syncExtents();
    void this.apply();
  }

  private groupsKey(): string {
    return [...this.loadedGroups].sort((a, b) => a - b).join(",");
  }

  private frame(store: NetworkStore, frame: number): Promise<Float32Array> {
    const groups = [...this.loadedGroups].sort((a, b) => a - b);
    const key = `${this.trackId}:${frame}:${this.groupsKey()}`;
    let held = this.frames.get(key);
    if (!held) {
      held = store.readFrameValues(frame, groups);
      held.catch(() => this.frames.delete(key));
      this.frames.set(key, held);
    }
    return held;
  }

  private async apply(): Promise<void> {
    const store = this.store;
    const layer = this.layer;
    if (!store || !layer || this.loadedGroups.size === 0 || !Number.isFinite(this.playhead)) return;
    const pair = framePair(store.times, this.playhead);
    const sequence = ++this.applySequence;
    if (!pair) {
      layer.setValues(null);
      return;
    }
    try {
      const [a, b] = await Promise.all([this.frame(store, pair.index), this.frame(store, pair.next)]);
      if (sequence !== this.applySequence || store !== this.store || this.disposed) return;
      if (!this.scratch || this.scratch.length !== a.length) this.scratch = new Float32Array(a.length);
      layer.setValues(blendValues(a, b, pair.weight, this.scratch));
    } catch (error) {
      if (sequence === this.applySequence) this.warn("frame not read", error);
    }
  }

  /** The gauge whose mark is under a screen point. */
  gaugeAt(point: { x: number; y: number }): RiverGauge | null {
    if (!this.map.getLayer(RIVER_GAUGE_LAYER)) return null;
    const box: [[number, number], [number, number]] = [
      [point.x - 6, point.y - 6],
      [point.x + 6, point.y + 6],
    ];
    for (const feature of this.map.queryRenderedFeatures(box, { layers: [RIVER_GAUGE_LAYER] })) {
      const gauge = this.index.gauges.find((item) => item.id === feature.properties?.id);
      if (gauge) return gauge;
    }
    return null;
  }

  /** The model's series at a network gauge on the current track, by its
   * own clock, or null while the track opens (onChange fires when it can
   * be asked again) or on a grid case. */
  modelSeries(gauge: RiverGauge): TimedSeries | null {
    const store = this.store;
    if (gauge.feature === undefined || !store) return null;
    const key = `${this.trackId}:${gauge.feature}`;
    const held = this.series.get(key);
    if (held !== undefined) return held === "pending" ? null : held;
    this.series.set(key, "pending");
    const times = Array.from(store.times);
    store.readSeriesValues(gauge.feature).then(
      (values) => {
        this.series.set(key, { times, values: Array.from(values, (value) => (Number.isFinite(value) ? value : null)) });
        this.onChange();
      },
      (error: unknown) => {
        this.series.delete(key);
        this.warn(`series at ${gauge.id} not read`, error);
      },
    );
    return null;
  }

  /** What the credit line names for the river data on screen: the index's
   * credits, then each extent shown. */
  credits(): string[] {
    const extents = [...this.shownExtents].sort((a, b) => a - b).map((position) => this.index.extent[position]!.credit);
    return [...this.index.credits, ...extents];
  }

  private syncExtents(): void {
    let changed = false;
    this.index.extent.forEach((extent, position) => {
      const wanted = this.extentsOn && Number.isFinite(this.playhead) && this.playhead >= extent.atMs;
      const id = `${EXTENT_PREFIX}${position}`;
      const state = this.extentState[position];
      if (wanted && state === "idle") {
        this.extentState[position] = "loading";
        void this.loadExtent(position);
        return;
      }
      if (state !== "ready") return;
      this.map.setLayoutProperty(id, "visibility", wanted ? "visible" : "none");
      const shown = this.shownExtents.has(position);
      if (shown !== wanted) {
        if (wanted) this.shownExtents.add(position);
        else this.shownExtents.delete(position);
        changed = true;
      }
    });
    if (changed) this.onChange();
  }

  private async loadExtent(position: number): Promise<void> {
    const extent = this.index.extent[position]!;
    const id = `${EXTENT_PREFIX}${position}`;
    try {
      const response = await fetchImmutable(riverFileUrl(this.loaded.indexUrl, extent.path, extent.crc32));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = (await response.json()) as FeatureCollection;
      if (data?.type !== "FeatureCollection" || !Array.isArray(data.features)) throw new Error("not a GeoJSON FeatureCollection");
      if (this.disposed) return;
      if (!this.map.getSource(id)) this.map.addSource(id, { type: "geojson", data });
      else (this.map.getSource(id) as GeoJSONSource).setData(data);
      // Under the reaches and the gauges, over the field.
      const before = this.map.getLayer(RIVER_LAYER_ID)
        ? RIVER_LAYER_ID
        : this.map.getLayer(RIVER_GAUGE_LAYER)
          ? RIVER_GAUGE_LAYER
          : this.firstSymbolLayer();
      if (!this.map.getLayer(id)) {
        this.map.addLayer(
          {
            id,
            type: "fill",
            source: id,
            layout: { visibility: "none" },
            paint: { "fill-color": "#2f7ad8", "fill-opacity": 0.42, "fill-outline-color": "#1d5aa8" },
          },
          before,
        );
      }
      this.extentState[position] = "ready";
    } catch (error) {
      this.extentState[position] = "failed";
      this.warn(`extent ${extent.path} not read`, error);
      return;
    }
    this.syncExtents();
  }

  /** The case panel's river block: the track switch, the extents switch,
   * the key, then the disclaimer and the credits. */
  renderPanel(container: HTMLElement): void {
    const index = this.index;
    const children: HTMLElement[] = [];
    const heading = document.createElement("p");
    heading.className = "case-river-heading";
    heading.textContent = t("riverHeading");
    children.push(heading);
    if (index.tracks.length > 0) {
      const tracks = document.createElement("div");
      tracks.className = "case-river-tracks";
      tracks.setAttribute("role", "group");
      tracks.setAttribute("aria-label", t("riverTracksAria"));
      for (const track of index.tracks) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.track = track.id;
        button.textContent = trackLabel(track);
        button.setAttribute("aria-pressed", String(track.id === this.trackId));
        button.addEventListener("click", () => {
          if (this.trackId === track.id) return;
          this.setTrack(track.id);
          this.renderPanel(container);
        });
        tracks.append(button);
      }
      children.push(tracks);
    }
    if (index.tracks.length > 0 && this.byRamp) {
      const note = document.createElement("p");
      note.className = "case-river-note";
      note.textContent = t("riverKeyField");
      children.push(note);
    } else if (index.tracks.length > 0) {
      const key = document.createElement("ul");
      key.className = "case-river-key";
      key.setAttribute("aria-label", t("riverKeyAria"));
      const labels = [t("riverRatio2"), t("riverRatio10"), t("riverRatio100"), t("riverRatioMax")];
      riverClassColors().forEach(([r, g, b], position) => {
        const item = document.createElement("li");
        const swatch = document.createElement("span");
        swatch.className = "case-river-swatch";
        swatch.style.background = `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
        item.append(swatch, document.createTextNode(labels[position]!));
        key.append(item);
      });
      children.push(key);
    }
    if (index.extent.length > 0) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "case-river-extents";
      toggle.textContent = t("riverExtents");
      toggle.setAttribute("aria-pressed", String(this.extentsOn));
      toggle.addEventListener("click", () => {
        this.setExtentsVisible(!this.extentsOn);
        toggle.setAttribute("aria-pressed", String(this.extentsOn));
      });
      children.push(toggle);
    }
    if (index.gauges.length > 0) {
      const hint = document.createElement("p");
      hint.className = "case-river-note";
      hint.textContent = t("riverGaugeHint");
      children.push(hint);
    }
    if (index.disclaimer) {
      const disclaimer = document.createElement("p");
      disclaimer.className = "case-river-note";
      disclaimer.textContent = index.disclaimer;
      children.push(disclaimer);
    }
    if (index.credits.length > 0) {
      const credits = document.createElement("p");
      credits.className = "case-river-credits";
      credits.textContent = index.credits.join(" · ");
      children.push(credits);
    }
    container.replaceChildren(...children);
    container.hidden = false;
  }
}
