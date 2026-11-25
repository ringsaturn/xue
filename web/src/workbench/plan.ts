/**
 * The plan of a point: the section's, of one sample on the store's nearest
 * cell. The nearest and not a blend of four, so that a value here is the one a
 * pin on the map reads.
 */

import { wrap } from "../gridmap";
import type { LonLat, SectionPlan } from "../crosssection/types";
import type { SectionGrid } from "../crosssection/line";

/** The cell nearest a place, or null off the grid. */
function nearestCell(grid: SectionGrid, [lon, lat]: LonLat): { row: number; column: number } | null {
  const at = grid.grid.lonLatToCell(lon, lat);
  if (!at) return null;
  const { west, east } = grid.grid.bounds();
  const around = east - west > 360 - 1e-6;
  const column = Math.round(at[0]);
  return {
    row: Math.min(grid.height - 1, Math.max(0, Math.round(at[1]))),
    column: around ? wrap(column, grid.width) : Math.min(grid.width - 1, Math.max(0, column)),
  };
}

/** The plan of the cell a point lies in, its sample at the cell's centre; a
 * point off the grid has no cells, as a line off it has none. */
export function planPoint(point: LonLat, grid: SectionGrid): SectionPlan {
  const cell = nearestCell(grid, point);
  const [lon, lat] = cell ? grid.grid.cellToLonLat(cell.column, cell.row) : point;
  return {
    line: { a: point, b: point },
    length: 0,
    points: [{ lon, lat, along: 0, bearing: 0 }],
    cells: cell ? Uint32Array.of(cell.row, cell.column) : new Uint32Array(0),
    corners: new Uint32Array(4),
    weights: Float32Array.of(cell ? 1 : 0, 0, 0, 0),
    blocks: cell ? 1 : 0,
  };
}
