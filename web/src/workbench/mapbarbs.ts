/**
 * Wind barbs of the map's level: one every so many pixels of the view, each
 * the wind of the cell under it, drawn as the sounding draws its barbs. A barb
 * is an icon of its speed, in steps of 5 kt, turned to its direction; they are
 * placed again when the map has moved, not while it moves.
 */

import type { GeoJSONSource, Map as MaplibreMap } from "maplibre-gl";

import { nearestCell } from "../composite";
import type { GeoGrid } from "../gridmap";
import { drawBarb } from "../sounding/skewt";
import type { CoverageBox } from "../tiles";

/** What the barbs are read from. */
export interface BarbPlanes {
  grid: GeoGrid;
  coverage: CoverageBox;
  u: Uint8Array;
  v: Uint8Array;
  uTable: Float32Array;
  vTable: Float32Array;
}

const SOURCE = "wb-barbs";
/** CSS pixels between two barbs. */
const SPACING = 56;
const KNOT = 1852 / 3600;
/** The fastest barb drawn, kt; a faster wind has this one. */
const TOP_KNOTS = 195;
/** Pixels of an icon a side, at one device pixel a pixel; the barb's foot is
 * its middle. */
const ICON = 60;
const SCALE = 0.8;

function iconName(knots: number, side: 1 | -1): string {
  return `wb-barb-${knots}-${side > 0 ? "n" : "s"}`;
}

export class MapBarbs {
  private planes: BarbPlanes | null = null;
  private ink = "";
  private added = false;

  constructor(private readonly map: MaplibreMap) {}

  attach(before: string | undefined, dark: boolean): void {
    if (this.added) return;
    this.map.addSource(SOURCE, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    this.map.addLayer(
      {
        id: SOURCE,
        type: "symbol",
        source: SOURCE,
        layout: {
          "icon-image": ["get", "icon"],
          "icon-rotate": ["get", "from"],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      },
      before,
    );
    this.added = true;
    this.setDark(dark);
  }

  /** The icons in the ink of the ground. */
  setDark(dark: boolean): void {
    const ink = dark ? "rgba(235, 235, 230, 0.85)" : "rgba(20, 20, 20, 0.75)";
    if (!this.added || ink === this.ink) return;
    this.ink = ink;
    const ratio = Math.max(1, Math.round(window.devicePixelRatio || 1));
    for (const side of [1, -1] as const) {
      for (let knots = 0; knots <= TOP_KNOTS; knots += 5) {
        const size = ICON * ratio;
        const canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        const context = canvas.getContext("2d")!;
        context.scale(ratio, ratio);
        context.strokeStyle = ink;
        context.fillStyle = ink;
        context.lineWidth = 1;
        context.lineCap = "round";
        // From the north, so that the layer's rotation is the wind's direction.
        drawBarb(context, ICON / 2, ICON / 2, 0, (knots + 0.5) * KNOT, SCALE, side);
        const image = context.getImageData(0, 0, size, size);
        const name = iconName(knots, side);
        if (this.map.hasImage(name)) this.map.updateImage(name, image);
        else this.map.addImage(name, image, { pixelRatio: ratio });
      }
    }
  }

  set(planes: BarbPlanes | null): void {
    this.planes = planes;
    this.place();
  }

  /** Put a barb at every so many pixels of the view the planes cover. */
  place(): void {
    const source = this.added ? (this.map.getSource(SOURCE) as GeoJSONSource | undefined) : undefined;
    if (!source) return;
    const planes = this.planes;
    const features: GeoJSON.Feature<GeoJSON.Point>[] = [];
    if (planes) {
      const canvas = this.map.getCanvas();
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const { grid, coverage, u, v, uTable, vTable } = planes;
      for (let y = SPACING / 2; y < height; y += SPACING) {
        for (let x = SPACING / 2; x < width; x += SPACING) {
          const { lng, lat } = this.map.unproject([x, y]);
          const cell = nearestCell(grid, lng, lat);
          if (cell < 0 || !covered(coverage, cell % grid.width, Math.floor(cell / grid.width), grid)) continue;
          const east = uTable[u[cell]!]!;
          const north = vTable[v[cell]!]!;
          if (!Number.isFinite(east) || !Number.isFinite(north)) continue;
          const knots = Math.min(TOP_KNOTS, Math.floor(Math.hypot(east, north) / KNOT / 5) * 5);
          const from = ((Math.atan2(-east, -north) * 180) / Math.PI + 360) % 360;
          features.push({
            type: "Feature",
            geometry: { type: "Point", coordinates: [lng, lat] },
            properties: { icon: iconName(knots, lat < 0 ? -1 : 1), from },
          });
        }
      }
    }
    source.setData({ type: "FeatureCollection", features });
  }
}

/** Whether a cell is in the part of the plane that was read. */
function covered(box: CoverageBox, column: number, row: number, grid: GeoGrid): boolean {
  const s = (column + 0.5) / grid.width;
  const t = (row + 0.5) / grid.height;
  const across = box.uStart <= box.uEnd ? s >= box.uStart && s <= box.uEnd : s >= box.uStart || s <= box.uEnd;
  return across && t >= box.vStart && t <= box.vEnd;
}
