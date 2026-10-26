/**
 * NASA GIBS daily imagery as an underlay: the corrected-reflectance
 * pictures the polar orbiters make once a day — MODIS on Terra and Aqua,
 * VIIRS on S-NPP, NOAA-20 and NOAA-21 — in true colour, and in the false
 * colour that shows a burn scar dark red and an active fire bright
 * (MODIS bands 7-2-1, VIIRS M11-I2-I1). Tiles come straight from GIBS's
 * WMTS (EPSG:3857 "best", no key, CORS open): nothing is published for
 * this, and a day is one MapLibre raster source whose tile URLs carry the
 * date. The date follows the playhead's UTC day, never past today.
 */

import type { Map as MapLibreMap, RasterTileSource } from "maplibre-gl";

export interface GibsLayer {
  /** The `?gibs=` spelling. */
  id: string;
  /** GIBS's layer identifier. */
  layer: string;
  /** The platform a chip names (an instrument code, English everywhere). */
  code: string;
  /** True for the false-colour composites. */
  falseColor: boolean;
  /** The platform's own picture in true colour: the id its pair shares. */
  platform: string;
}

/** A platform's chip code on the level row, short enough for its glyph. */
export const GIBS_PLATFORM_GLYPH: Readonly<Record<string, string>> = {
  snpp: "NPP",
  noaa20: "N20",
  noaa21: "N21",
  terra: "TER",
  aqua: "AQU",
};

/** The deepest tile matrix GIBS serves these layers at in EPSG:3857
 * (`GoogleMapsCompatible_Level9`, about 300 m a pixel); MapLibre overzooms
 * past it. */
export const GIBS_MAX_LEVEL = 9;

export const GIBS_LAYERS: readonly GibsLayer[] = [
  { id: "snpp", layer: "VIIRS_SNPP_CorrectedReflectance_TrueColor", code: "S-NPP", falseColor: false, platform: "snpp" },
  { id: "noaa20", layer: "VIIRS_NOAA20_CorrectedReflectance_TrueColor", code: "NOAA-20", falseColor: false, platform: "noaa20" },
  { id: "noaa21", layer: "VIIRS_NOAA21_CorrectedReflectance_TrueColor", code: "NOAA-21", falseColor: false, platform: "noaa21" },
  { id: "terra", layer: "MODIS_Terra_CorrectedReflectance_TrueColor", code: "TERRA", falseColor: false, platform: "terra" },
  { id: "aqua", layer: "MODIS_Aqua_CorrectedReflectance_TrueColor", code: "AQUA", falseColor: false, platform: "aqua" },
  { id: "snpp-fc", layer: "VIIRS_SNPP_CorrectedReflectance_BandsM11-I2-I1", code: "S-NPP", falseColor: true, platform: "snpp" },
  { id: "noaa20-fc", layer: "VIIRS_NOAA20_CorrectedReflectance_BandsM11-I2-I1", code: "NOAA-20", falseColor: true, platform: "noaa20" },
  { id: "noaa21-fc", layer: "VIIRS_NOAA21_CorrectedReflectance_BandsM11-I2-I1", code: "NOAA-21", falseColor: true, platform: "noaa21" },
  { id: "terra-721", layer: "MODIS_Terra_CorrectedReflectance_Bands721", code: "TERRA", falseColor: true, platform: "terra" },
  { id: "aqua-721", layer: "MODIS_Aqua_CorrectedReflectance_Bands721", code: "AQUA", falseColor: true, platform: "aqua" },
];

export const GIBS_ATTRIBUTION = "NASA GIBS / Worldview";

export function gibsLayer(id: string | null): GibsLayer | null {
  return GIBS_LAYERS.find((layer) => layer.id === id) ?? null;
}

/** The platforms, in the order their chips are listed. */
export const GIBS_PLATFORMS: readonly string[] = [...new Set(GIBS_LAYERS.map((layer) => layer.platform))];

/** One platform's picture in true or false colour. */
export function gibsLayerFor(platform: string, falseColor: boolean): GibsLayer | null {
  return GIBS_LAYERS.find((layer) => layer.platform === platform && layer.falseColor === falseColor) ?? null;
}

/** The days the dataset offers, as an axis: `count` UTC days ending today,
 * the first day's midnight and the number of days. Today's picture fills
 * in over the day as the orbiters pass. */
export function gibsDays(nowMs: number, count: number): { startMs: number; count: number } {
  const today = Date.UTC(new Date(nowMs).getUTCFullYear(), new Date(nowMs).getUTCMonth(), new Date(nowMs).getUTCDate());
  return { startMs: today - (count - 1) * 86_400_000, count };
}

/** The UTC day a picture is asked for: the playhead's, or today's when the
 * playhead is in the future (a forecast frame), since GIBS has no day
 * after today. */
export function gibsDate(validMs: number, nowMs: number): string {
  return new Date(Math.min(validMs, nowMs)).toISOString().slice(0, 10);
}

/** The picture drawn on `date` for `layer`: the platform a case names for
 * that day (GIBS serves a black picture for a day an orbiter has none), in
 * the colour the viewer chose; `layer` itself on every other day. */
export function gibsLayerOnDay(
  layer: GibsLayer,
  date: string,
  days: Readonly<Record<string, string>> | undefined,
): GibsLayer {
  const standIn = gibsLayer(days?.[date] ?? null);
  if (standIn === null) return layer;
  return gibsLayerFor(standIn.platform, layer.falseColor) ?? layer;
}

/** The XYZ tile template of one layer on one day. */
export function gibsTileUrl(layer: GibsLayer, date: string): string {
  return (
    `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/${layer.layer}/default/${date}/` +
    `GoogleMapsCompatible_Level${GIBS_MAX_LEVEL}/{z}/{y}/{x}.jpg`
  );
}

const SOURCE_ID = "gibs-imagery";
const LAYER_ID = "gibs-imagery";

/** The underlay on the map: one source and one raster layer, added the
 * first time a layer is chosen and kept (hidden) after. Not part of the
 * built style, so the basemap's property diff leaves it alone. */
export class GibsImagery {
  private chosen: GibsLayer | null = null;
  private date: string | null = null;
  private before: string | undefined = undefined;

  constructor(private readonly map: MapLibreMap) {}

  /** Show `layer` on `date` beneath `beforeId` (the field), or nothing. */
  show(layer: GibsLayer | null, date: string, beforeId: string | undefined): void {
    const map = this.map;
    if (layer === null) {
      if (this.chosen !== null && map.getLayer(LAYER_ID)) map.setLayoutProperty(LAYER_ID, "visibility", "none");
      this.chosen = null;
      return;
    }
    // Called on every frame step: nothing to do unless the picture, its
    // day or the layer it sits under changed.
    if (this.chosen === layer && this.date === date && this.before === beforeId && map.getLayer(LAYER_ID)) return;
    const tiles = [gibsTileUrl(layer, date)];
    const source = map.getSource(SOURCE_ID) as RasterTileSource | undefined;
    if (!source) {
      map.addSource(SOURCE_ID, {
        type: "raster",
        tiles,
        tileSize: 256,
        maxzoom: GIBS_MAX_LEVEL,
        attribution: GIBS_ATTRIBUTION,
      });
    } else if (this.chosen !== layer || this.date !== date) {
      source.setTiles(tiles);
    }
    if (!map.getLayer(LAYER_ID)) {
      map.addLayer({ id: LAYER_ID, type: "raster", source: SOURCE_ID, paint: { "raster-fade-duration": 0 } }, beforeId);
    } else if (beforeId !== undefined && map.getLayer(beforeId)) {
      map.moveLayer(LAYER_ID, beforeId);
    }
    map.setLayoutProperty(LAYER_ID, "visibility", "visible");
    this.chosen = layer;
    this.date = date;
    this.before = beforeId;
  }
}
