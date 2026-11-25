/**
 * The map's field: the level the height line is on, at the time the charts
 * hold, in the charts' own fill, with isotherms over it.
 *
 * It is read from the map's stores, a frame of one level a bundle, and not from
 * the column store, whose blocks hold every frame and every level of a few
 * cells: the view's worth of them is tens of megabytes where the map's tiles
 * are a few hundred kilobytes. Each bundle a member of the column store names
 * is one of these, so the two agree code for code. Only the tiles in view are
 * read; a move that leaves them reads the new ones.
 */

import type { Map as MaplibreMap } from "maplibre-gl";

import { FIELD_ANCHOR_LAYER, FORECAST_ANCHOR_LAYER } from "../basemap";
import { decodeTable } from "../composite";
import { equivalentPotentialTemperature } from "../crosssection/derive";
import { sectionBands, sectionLegend } from "../crosssection/ramps";
import type { SectionFill, SectionStore } from "../crosssection/types";
import type { ColumnQuantity } from "../identity";
import { geoGrid } from "../gridmap";
import { ForecastLayer, type ContourStyle } from "../layer";
import { MapBarbs } from "./mapbarbs";
import { axisUnitSeconds, frameOffsets, parseBundleMetadata, type BundleMetadata, type LoadedManifest } from "../manifest";
import type { DecodeChannel } from "../session";
import { coverageBox, parseTileGeometry, sameTileRects, viewportTileRects, WHOLE_PLANE_COVERAGE, type TileGeometry, type TileRect } from "../tiles";
import { openZarrHubChannel, zarrInitMessage, zarrStoreFor } from "../zarr/channel";

/** What the map is asked to show. */
export interface MapFieldView {
  store: SectionStore;
  run: LoadedManifest;
  fill: SectionFill;
  /** hPa. */
  level: number;
  /** Seconds after the run. */
  lead: number;
  /** Wind barbs of the level over it. */
  barbs: boolean;
}

/** The codes the fill is drawn with: a linear scale over the ramp, the top
 * code none. */
const TOP_CODE = 253;
const NONE = 255;
/** The fill under the lines and the labels, as the home page's is. */
const FILL_ALPHA = 0.82;
/** Compressed bytes a bundle's channel keeps. */
const HELD_BYTES = 24 * 1024 * 1024;

const ISOTHERMS: Omit<ContourStyle, "offset" | "scale" | "lineColor"> = {
  interval: 5,
  emphasisInterval: 0,
  values: [0],
  lineWidth: 0.6,
  emphasisWidth: 1.4,
  fillAlpha: 0,
  smoothing: 1.5,
};

/** What a fill is drawn from, by quantity, in the order `fillCodes` takes
 * them. The wind across a line is the line's, so the map has the wind's speed
 * for it. */
function fillInputs(fill: SectionFill): ColumnQuantity[] {
  switch (fill) {
    case "thetae":
      return ["tmp", "rh"];
    case "tmp":
      return ["tmp"];
    case "rh":
      return ["rh"];
    case "wind":
    case "cross":
      return ["ugrd", "vgrd"];
  }
}

/** Everything read: the fill's inputs, the temperature for the isotherms, and
 * the wind for the barbs. */
function quantitiesOf(fill: SectionFill, barbs: boolean): ColumnQuantity[] {
  return [...new Set<ColumnQuantity>([...fillInputs(fill), "tmp", ...(barbs ? (["ugrd", "vgrd"] as const) : [])])];
}

/** The fill the map's field is drawn in: the section's for every fill but the
 * wind across the line, whose speed the wind's ramp has. */
export function mapFill(fill: SectionFill): SectionFill {
  return fill === "cross" ? "wind" : fill;
}

/** The palette of the fill's codes: each the colour of the band its value is
 * in, so the map is stepped as the charts are. */
function fillPalette(fill: SectionFill): Uint8Array {
  const { min, max } = sectionLegend(fill);
  const bands = sectionBands(fill);
  const count = bands.rgb.length / 3;
  const palette = new Uint8Array(256 * 4);
  for (let code = 0; code <= TOP_CODE; code += 1) {
    const value = min + ((max - min) * code) / TOP_CODE;
    const band = Math.max(0, Math.min(count - 1, Math.floor((value - bands.min) / bands.step)));
    palette.set([bands.rgb[band * 3]!, bands.rgb[band * 3 + 1]!, bands.rgb[band * 3 + 2]!, Math.round(FILL_ALPHA * 255)], code * 4);
  }
  return palette;
}

/** The isotherms' ink on a light ground and on a dark one. */
function isothermInk(dark: boolean): readonly [number, number, number, number] {
  return dark ? [0.92, 0.92, 0.9, 0.7] : [0.11, 0.1, 0.09, 0.62];
}

/** One bundle's channel: open, or opening. */
interface Channel {
  bundle: string;
  channel: DecodeChannel;
  ready: Promise<{ metadata: BundleMetadata; tiles: TileGeometry | null }>;
  /** Decodes asked for and not answered, by request id. */
  waiting: Map<number, (plane: Uint8Array | null) => void>;
}

let nextRequest = 1;

function openChannel(run: LoadedManifest, bundle: string): Channel | null {
  const descriptor = run.manifest.bundles.find((entry) => entry.variable === bundle);
  const store = descriptor && zarrStoreFor("zarr", descriptor, null);
  if (!store) return null;
  const channel = openZarrHubChannel();
  const waiting = new Map<number, (plane: Uint8Array | null) => void>();
  const ready = new Promise<{ metadata: BundleMetadata; tiles: TileGeometry | null }>((resolve, reject) => {
    channel.onmessage = (event: MessageEvent) => {
      const message = event.data as Record<string, unknown>;
      switch (message.type) {
        case "booted":
          channel.postMessage(zarrInitMessage(store, bundle, run.manifestUrl, `${bundle}@workbench`, true, HELD_BYTES));
          break;
        case "ready":
          try {
            resolve({
              metadata: parseBundleMetadata(message.metadataJson as string),
              tiles: parseTileGeometry(message.tileGeometry as Uint32Array | undefined),
            });
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
          break;
        case "frame": {
          const done = waiting.get(message.requestId as number);
          waiting.delete(message.requestId as number);
          done?.(new Uint8Array(message.buffer as ArrayBuffer));
          break;
        }
        case "error": {
          const id = message.requestId as number | undefined;
          if (id !== undefined && waiting.has(id)) {
            waiting.get(id)!(null);
            waiting.delete(id);
          } else reject(new Error(String(message.message ?? "the map's store did not open")));
          break;
        }
        default:
          break;
      }
    };
  });
  // Nothing waits on a channel that failed before it was asked anything.
  ready.catch(() => undefined);
  return { bundle, channel, ready, waiting };
}

export class MapField {
  private readonly fillLayer = new ForecastLayer((message) => console.warn("workbench: map field:", message), "wb-field");
  private readonly lineLayer = new ForecastLayer((message) => console.warn("workbench: isotherms:", message), "wb-isotherms");
  private readonly channels = new Map<string, Channel>();
  private runKey = "";
  private view: MapFieldView | null = null;
  /** What is on the map, and the tiles it was read for. */
  private shownKey = "";
  private shownTiles: TileRect[] | null = null;
  private epoch = 0;
  /** What was last asked for, read or being read. */
  private askedKey = "";
  private added = false;
  private palettes = new Map<SectionFill, Uint8Array>();
  private dark = false;
  private readonly barbs: MapBarbs;

  constructor(private readonly map: MaplibreMap) {
    this.barbs = new MapBarbs(map);
    map.on("moveend", () => {
      this.barbs.place();
      this.read(true);
    });
  }

  /** The style is loaded: the layers go under the coastline and the labels. */
  attach(): void {
    if (this.added) return;
    const before = this.map.getLayer(FIELD_ANCHOR_LAYER) ? FIELD_ANCHOR_LAYER : this.map.getLayer(FORECAST_ANCHOR_LAYER) ? FORECAST_ANCHOR_LAYER : undefined;
    this.map.addLayer(this.fillLayer, before);
    this.map.addLayer(this.lineLayer, this.map.getLayer(FORECAST_ANCHOR_LAYER) ? FORECAST_ANCHOR_LAYER : undefined);
    this.fillLayer.setVisible(false);
    this.lineLayer.setVisible(false);
    this.barbs.attach(this.map.getLayer(FORECAST_ANCHOR_LAYER) ? FORECAST_ANCHOR_LAYER : undefined, this.dark);
    this.added = true;
    this.read(true);
  }

  /** The ground went dark or light. */
  setDark(dark: boolean): void {
    this.dark = dark;
    this.barbs.setDark(dark);
    this.shownKey = "";
    this.read(true);
  }

  show(view: MapFieldView | null): void {
    this.view = view;
    const runKey = view ? view.run.manifestUrl : "";
    if (runKey !== this.runKey) {
      for (const channel of this.channels.values()) close(channel);
      this.channels.clear();
      this.runKey = runKey;
      this.shownKey = "";
    }
    this.read(false);
  }

  private channel(run: LoadedManifest, bundle: string): Channel | null {
    const known = this.channels.get(bundle);
    if (known) return known;
    const opened = openChannel(run, bundle);
    if (opened) this.channels.set(bundle, opened);
    return opened;
  }

  private hide(): void {
    this.fillLayer.setVisible(false);
    this.lineLayer.setVisible(false);
    this.barbs.set(null);
    this.shownKey = "";
  }

  /** Read what the view asks for; again where `moved`, for the tiles. */
  private read(moved: boolean): void {
    const view = this.view;
    if (!this.added) return;
    if (!view) {
      this.askedKey = "";
      this.hide();
      return;
    }
    const fill = mapFill(view.fill);
    const bundles: string[] = [];
    const quantities = quantitiesOf(fill, view.barbs);
    for (const quantity of quantities) {
      const array = [...view.store.arrays.values()].find((candidate) => candidate.quantity === quantity && candidate.levels !== null);
      const index = array?.levels?.indexOf(view.level) ?? -1;
      const bundle = index >= 0 ? array!.bundles[index] : undefined;
      if (!bundle) {
        this.hide();
        return;
      }
      bundles.push(bundle);
    }
    const key = `${this.runKey} ${fill} ${view.level} ${view.lead} ${view.barbs} ${this.dark}`;
    if (!moved && key === this.askedKey) return;
    this.askedKey = key;
    const epoch = ++this.epoch;
    void this.draw(view.run, fill, quantities, bundles, view.lead, key, epoch);
  }

  private async draw(
    run: LoadedManifest,
    fill: SectionFill,
    quantities: ColumnQuantity[],
    bundles: string[],
    lead: number,
    key: string,
    epoch: number,
  ): Promise<void> {
    // A wind's two components are one bundle; it is read once and asked twice.
    const unique = [...new Set(bundles)];
    // A level or a fill left behind is not read again soon: its channel goes.
    for (const [bundle, channel] of this.channels) {
      if (unique.includes(bundle)) continue;
      close(channel);
      this.channels.delete(bundle);
    }
    const channels = unique.map((bundle) => this.channel(run, bundle));
    if (channels.some((channel) => channel === null)) {
      this.hide();
      return;
    }
    let opened: { metadata: BundleMetadata; tiles: TileGeometry | null }[];
    try {
      opened = await Promise.all(channels.map((channel) => channel!.ready));
    } catch (error) {
      console.warn("workbench: map field:", error instanceof Error ? error.message : error);
      if (epoch === this.epoch) this.hide();
      return;
    }
    if (epoch !== this.epoch) return;
    const { metadata, tiles: geometry } = opened[0]!;
    const bounds = this.map.getBounds();
    const rects = geometry
      ? viewportTileRects(metadata, geometry, { west: bounds.getWest(), east: bounds.getEast(), south: bounds.getSouth(), north: bounds.getNorth() })
      : null;
    if (key === this.shownKey && sameTileRects(rects, this.shownTiles)) return;
    const unit = axisUnitSeconds(metadata.time);
    const offset = lead / unit;
    if (!frameOffsets(metadata.time).includes(offset)) {
      this.hide();
      return;
    }
    // Each quantity's variable: the bundle's only one, or the wind's u or v.
    const asks = bundles.map((bundle, index) => {
      const at = unique.indexOf(bundle);
      const { metadata: own } = opened[at]!;
      const quantity = quantities[index]!;
      const variable = own.variables.length === 1 ? own.variables[0]! : own.variables[quantity === "vgrd" ? 1 : 0]!;
      return { channel: channels[at]!, variable };
    });
    const planes = await Promise.all(
      asks.map(
        ({ channel, variable }) =>
          new Promise<Uint8Array | null>((resolve) => {
            const requestId = nextRequest++;
            channel.waiting.set(requestId, resolve);
            channel.channel.postMessage({
              type: "decode",
              requestId,
              generation: 0,
              variableId: variable.numericId,
              frameOffset: offset,
              ...(rects ? { tiles: rects } : {}),
            });
          }),
      ),
    );
    if (epoch !== this.epoch) return;
    const cells = metadata.grid.width * metadata.grid.height;
    if (planes.some((plane) => plane === null || plane.length !== cells)) {
      this.hide();
      return;
    }
    const coverage = geometry ? coverageBox(geometry, metadata.grid.width, metadata.grid.height, rects) : WHOLE_PLANE_COVERAGE;
    const tables = asks.map(({ variable }) => decodeTable(variable));
    const of = (quantity: ColumnQuantity): number => quantities.indexOf(quantity);
    const inputs = fillInputs(fill).map(of);
    const level = this.view?.level ?? 0;
    const codes = fillCodes(
      fill,
      inputs.map((index) => planes[index]!),
      inputs.map((index) => tables[index]!),
      level,
    );
    const temperature = of("tmp");
    const tmp = asks[temperature]!.variable;
    if (tmp.quantization.type !== "linear") {
      this.hide();
      return;
    }
    let palette = this.palettes.get(fill);
    if (!palette) {
      palette = fillPalette(fill);
      this.palettes.set(fill, palette);
    }
    for (const layer of [this.fillLayer, this.lineLayer]) {
      layer.configureGrid(metadata);
      layer.setSampling("smooth");
      layer.setFloorNoData(false);
    }
    this.fillLayer.setPalette(palette);
    this.fillLayer.setFrame(codes, coverage);
    this.lineLayer.setPalette(palette);
    this.lineLayer.setContours({ ...ISOTHERMS, offset: tmp.quantization.offset, scale: tmp.quantization.scale, lineColor: isothermInk(this.dark) });
    this.lineLayer.setFrame(planes[temperature]!, coverage);
    this.fillLayer.setVisible(true);
    this.lineLayer.setVisible(true);
    const [u, v] = [of("ugrd"), of("vgrd")];
    this.barbs.set(
      u >= 0 && v >= 0 ? { grid: geoGrid(metadata), coverage, u: planes[u]!, v: planes[v]!, uTable: tables[u]!, vTable: tables[v]! } : null,
    );
    this.shownKey = key;
    this.shownTiles = rects;
  }

}

/** Close a channel, and answer what was waiting on it with nothing. */
function close(channel: Channel): void {
  channel.channel.terminate();
  for (const done of channel.waiting.values()) done(null);
  channel.waiting.clear();
}

/** The fill's codes out of the planes of its quantities, cell by cell. A cell
 * none of whose inputs is read stays none. */
export function fillCodes(fill: SectionFill, planes: readonly Uint8Array[], tables: readonly Float32Array[], level: number): Uint8Array {
  const { min, max } = sectionLegend(fill);
  const per = TOP_CODE / (max - min);
  const out = new Uint8Array(planes[0]!.length).fill(NONE);
  const [a, b] = planes;
  const [ta, tb] = tables;
  for (let cell = 0; cell < out.length; cell += 1) {
    let value: number;
    const x = ta![a![cell]!]!;
    switch (fill) {
      case "tmp":
      case "rh":
        value = x;
        break;
      case "thetae": {
        // °C and %, to a dew point.
        const rh = Math.max(1, tb![b![cell]!]!);
        const gamma = Math.log(rh / 100) + (17.67 * x) / (x + 243.5);
        value = equivalentPotentialTemperature(x, (243.5 * gamma) / (17.67 - gamma), level);
        break;
      }
      default:
        value = Math.hypot(x, tb![b![cell]!]!);
    }
    if (!Number.isFinite(value)) continue;
    out[cell] = Math.round(Math.max(0, Math.min(TOP_CODE, (value - min) * per)));
  }
  return out;
}
