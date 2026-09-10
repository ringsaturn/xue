/**
 * Viewport to tile rectangles — the frontend half of container v2.
 *
 * A v2 bundle's smallest addressable payload is a chunk: one spatial tile of
 * one temporal group of one variable (docs/format.md, "Container v2"). The
 * decoder takes a rectangle of tiles, so what the map has to answer is: which
 * tiles does the current view touch?
 *
 * Two things make that more than a division. A view straddling the
 * antimeridian covers two disjoint column ranges of a wrapping grid, so the
 * answer is a *list* of rectangles rather than one. And a view that already
 * touches every tile is better expressed as "the whole plane" — `null` here —
 * because that is the path with no coverage bookkeeping at all.
 *
 * The degree-to-cell arithmetic is the same as `probe.ts` (and the fragment
 * shader): longitude wrapped into [0, 360) east of the grid origin, then
 * divided by the step.
 */

import { geoGrid, wrap, type GeoGrid } from "./probe";
import type { BundleMetadata } from "./manifest";

/** A bundle's tiling, as `WasmBundle.tileGeometry()` reports it. */
export interface TileGeometry {
  tileWidth: number;
  tileHeight: number;
  columns: number;
  rows: number;
}

/** A rectangle of tiles, inclusive of both ends — the decoder's own shape. */
export interface TileRect {
  firstColumn: number;
  firstRow: number;
  lastColumn: number;
  lastRow: number;
}

/** Map bounds in degrees. `east` may exceed `west` by more than 360 when the
 * map is zoomed out past one world copy; `east < west` never happens because
 * MapLibre unwraps the eastern edge instead. */
export interface ViewportBounds {
  west: number;
  east: number;
  south: number;
  north: number;
}

/** The four numbers `tileGeometry()` returns, or null on a plane-major v1
 * bundle (and on the video path, which has no tiles at all). */
export function parseTileGeometry(raw: Uint32Array | undefined | null): TileGeometry | null {
  if (!raw || raw.length < 4) return null;
  const [tileWidth, tileHeight, columns, rows] = raw;
  if (!tileWidth || !tileHeight || !columns || !rows) return null;
  return { tileWidth, tileHeight, columns, rows };
}

/** Flatten rectangles for the WASM boundary: `[firstColumn, firstRow,
 * lastColumn, lastRow, ...]`. */
export function flattenTileRects(rects: readonly TileRect[]): Uint32Array {
  const flat = new Uint32Array(rects.length * 4);
  for (const [index, rect] of rects.entries()) {
    flat.set([rect.firstColumn, rect.firstRow, rect.lastColumn, rect.lastRow], index * 4);
  }
  return flat;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** The inclusive cell rows a latitude band touches, clamped to the grid, or
 * null when the band misses it entirely. */
function rowRange(grid: GeoGrid, south: number, north: number): [number, number] | null {
  const edges = [
    (north - grid.firstLatitude) / grid.latitudeStep,
    (south - grid.firstLatitude) / grid.latitudeStep,
  ];
  const first = Math.floor(Math.min(...edges));
  const last = Math.ceil(Math.max(...edges));
  if (last < 0 || first > grid.height - 1) return null;
  return [clamp(first, 0, grid.height - 1), clamp(last, 0, grid.height - 1)];
}

/** The inclusive cell columns a longitude band touches on a grid whose
 * columns do not wrap, or null when the band misses it entirely. The offset
 * is signed — a view west of a cropped window is at a negative cell, not most
 * of the way around the world. */
function croppedColumnRange(grid: GeoGrid, west: number, span: number): [number, number] | null {
  const offset = wrap(west - grid.firstLongitude + 180, 360) - 180;
  const first = Math.floor(offset / grid.longitudeStep);
  const last = Math.ceil((offset + span) / grid.longitudeStep);
  if (last < 0 || first > grid.width - 1) return null;
  return [clamp(first, 0, grid.width - 1), clamp(last, 0, grid.width - 1)];
}

/**
 * The tiles a viewport covers, or `null` when the whole plane is the better
 * answer — the view spans every tile, the bundle has no tiling, or the view
 * misses a cropped grid altogether (nothing to draw, and a whole-plane decode
 * is the one path that needs no coverage tracking).
 *
 * `pad` widens the answer by that many tiles on every side, so a small pan
 * lands inside what is already decoded instead of blanking the new edge.
 */
export function viewportTileRects(
  metadata: BundleMetadata,
  geometry: TileGeometry,
  bounds: ViewportBounds,
  pad = 1,
): TileRect[] | null {
  const grid = geoGrid(metadata);
  if (grid.width <= 0 || grid.height <= 0 || grid.longitudeStep === 0 || grid.latitudeStep === 0) {
    return null;
  }
  const rows = rowRange(grid, bounds.south, bounds.north);
  if (!rows) return null;
  const firstRow = clamp(Math.floor(rows[0] / geometry.tileHeight) - pad, 0, geometry.rows - 1);
  const lastRow = clamp(Math.floor(rows[1] / geometry.tileHeight) + pad, 0, geometry.rows - 1);

  const span = bounds.east - bounds.west;
  if (!Number.isFinite(span) || span < 0) return null;

  if (!grid.wraps) {
    const columns = croppedColumnRange(grid, bounds.west, span);
    if (!columns) return null;
    return rectsOrNull(geometry, [
      {
        firstColumn: clamp(Math.floor(columns[0] / geometry.tileWidth) - pad, 0, geometry.columns - 1),
        firstRow,
        lastColumn: clamp(Math.floor(columns[1] / geometry.tileWidth) + pad, 0, geometry.columns - 1),
        lastRow,
      },
    ]);
  }

  // A wrapping grid puts every longitude on some column, so only the width of
  // the view matters: an origin tile plus how many tiles the span reaches.
  // Counting rather than comparing wrapped endpoints is what keeps a view one
  // degree short of the whole world from reading as a two-tile sliver.
  const origin = wrap(bounds.west - grid.firstLongitude, 360) / grid.longitudeStep;
  const first = Math.floor(origin / geometry.tileWidth) - pad;
  const reach =
    Math.floor((origin + span / grid.longitudeStep) / geometry.tileWidth) -
    Math.floor(origin / geometry.tileWidth) +
    1 +
    2 * pad;
  if (reach >= geometry.columns) {
    return rectsOrNull(geometry, [
      { firstColumn: 0, firstRow, lastColumn: geometry.columns - 1, lastRow },
    ]);
  }
  const firstColumn = wrap(first, geometry.columns);
  const lastColumn = wrap(first + reach - 1, geometry.columns);
  if (firstColumn <= lastColumn) {
    return rectsOrNull(geometry, [{ firstColumn, firstRow, lastColumn, lastRow }]);
  }
  return rectsOrNull(geometry, [
    { firstColumn, firstRow, lastColumn: geometry.columns - 1, lastRow },
    { firstColumn: 0, firstRow, lastColumn, lastRow },
  ]);
}

/** Collapse a rectangle list that already spans the grid to `null`, so the
 * caller takes the whole-plane path rather than a rectangle that happens to
 * be everything. */
function rectsOrNull(geometry: TileGeometry, rects: TileRect[]): TileRect[] | null {
  const total = geometry.columns * geometry.rows;
  return tileCount(rects) >= total ? null : rects;
}

/** How many tiles a rectangle list names. The lists this module builds never
 * overlap, so a plain sum is exact. */
export function tileCount(rects: readonly TileRect[]): number {
  return rects.reduce(
    (total, rect) =>
      total + (rect.lastColumn - rect.firstColumn + 1) * (rect.lastRow - rect.firstRow + 1),
    0,
  );
}

/** Fraction of the grid a rectangle list covers; `null` (whole plane) is 1. */
export function tileFraction(geometry: TileGeometry, rects: readonly TileRect[] | null): number {
  if (!rects) return 1;
  return Math.min(1, tileCount(rects) / (geometry.columns * geometry.rows));
}

function containsRect(outer: TileRect, inner: TileRect): boolean {
  return (
    outer.firstColumn <= inner.firstColumn &&
    outer.lastColumn >= inner.lastColumn &&
    outer.firstRow <= inner.firstRow &&
    outer.lastRow >= inner.lastRow
  );
}

/**
 * Whether a plane decoded for `have` can be shown for `want`.
 *
 * `null` is the whole plane and covers everything. Otherwise every wanted
 * rectangle must sit inside one held rectangle: the lists this module builds
 * split only at the antimeridian, so a wanted rectangle never needs two held
 * ones stitched together, and a false negative only costs a re-decode.
 */
export function coversTiles(
  have: readonly TileRect[] | null,
  want: readonly TileRect[] | null,
): boolean {
  if (!have) return true;
  if (!want) return false;
  return want.every((rect) => have.some((held) => containsRect(held, rect)));
}

/**
 * A coverage box in texture coordinates: what part of the data texture a
 * partially decoded plane actually filled.
 *
 * `uStart > uEnd` means the box wraps the antimeridian, which is exactly the
 * two-rectangle case above — one box covers it because the u axis is
 * periodic. A whole plane is `{0, 1, 0, 1}`, which passes every sample, so
 * the shader needs no separate "no coverage" flag.
 */
export interface CoverageBox {
  uStart: number;
  uEnd: number;
  vStart: number;
  vEnd: number;
}

export const WHOLE_PLANE_COVERAGE: CoverageBox = { uStart: 0, uEnd: 1, vStart: 0, vEnd: 1 };

/**
 * The texture-space box a rectangle list covers on a grid of `width` x
 * `height` cells. `null` rectangles (the whole plane) give the whole box.
 *
 * The bounds are cell edges, not centers: a tile column starting at cell c
 * begins at `c / width`, and one ending at cell c ends at `(c + 1) / width`.
 */
export function coverageBox(
  geometry: TileGeometry,
  width: number,
  height: number,
  rects: readonly TileRect[] | null,
): CoverageBox {
  if (!rects || rects.length === 0) return WHOLE_PLANE_COVERAGE;
  const firstRow = Math.min(...rects.map((rect) => rect.firstRow));
  const lastRow = Math.max(...rects.map((rect) => rect.lastRow));
  const vStart = (firstRow * geometry.tileHeight) / height;
  const vEnd = Math.min(1, ((lastRow + 1) * geometry.tileHeight) / height);
  // One rectangle is a plain span; two are the halves of a span that wrapped,
  // and the wrapped box is written by taking the western half's start and the
  // eastern half's end.
  const wrapped = rects.length > 1;
  const west = wrapped ? rects.reduce((a, b) => (a.firstColumn > b.firstColumn ? a : b)) : rects[0]!;
  const east = wrapped ? rects.reduce((a, b) => (a.lastColumn < b.lastColumn ? a : b)) : rects[0]!;
  return {
    uStart: (west.firstColumn * geometry.tileWidth) / width,
    uEnd: Math.min(1, ((east.lastColumn + 1) * geometry.tileWidth) / width),
    vStart,
    vEnd,
  };
}

/** Whether two rectangle lists name the same tiles. */
export function sameTileRects(
  a: readonly TileRect[] | null,
  b: readonly TileRect[] | null,
): boolean {
  if (!a || !b) return a === b;
  return (
    a.length === b.length &&
    a.every((rect, index) => {
      const other = b[index]!;
      return (
        rect.firstColumn === other.firstColumn &&
        rect.lastColumn === other.lastColumn &&
        rect.firstRow === other.firstRow &&
        rect.lastRow === other.lastRow
      );
    })
  );
}
