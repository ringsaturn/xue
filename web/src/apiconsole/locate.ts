/**
 * Latitude and longitude to a `/v1/cell` request, in the browser.
 *
 * The data API's `/v1/cell` takes a run and a grid cell, so a client that
 * holds a coordinate finds the cell itself: the run's manifest names each
 * bundle's grid, and the cell rule below turns the coordinate into a row and
 * a column. The rule is the server's (`xue::zarr::probe_cell`), operation for
 * operation, so a cell found here is the one `/v1/point` would answer for the
 * same coordinate, boundaries included: `floor(x + 0.5)`, not `Math.round`,
 * which disagrees with it just below a half (0.49999999999999994).
 *
 * The page reads the metadata from the dataset origin the API names in
 * `/v1/catalog`; only the request itself reaches the API.
 */

import { gridMapFor, parseGrid as parseBundleGrid, sameGrid, wrap, type GeoGrid } from "../gridmap";

/** A bundle's grid as the manifest declares it. */
export type CellGrid = GeoGrid;

export interface Cell {
  row: number;
  column: number;
  /** The cell centre, as the API reports it in `cell`. */
  longitude: number;
  latitude: number;
}

/** Bundles of one run that share a grid, and so one `/v1/cell` request. */
export interface GridGroup {
  grid: CellGrid;
  bundles: string[];
}

/** One `/v1/cell` request's parameters, in the API's order. */
export interface CellRequest {
  source: string;
  run: string;
  /** A string while the form is incomplete: the preview shows what is typed. */
  row: number | string;
  column: number | string;
  variables: readonly string[];
  time?: string;
}

/** The grid record of `docs/zarr-profile.md`, or null for one the rule does
 * not cover or that is malformed, so that bundle is reported, not misread. */
export function parseGrid(input: unknown): CellGrid | null {
  try {
    return parseBundleGrid(input);
  } catch {
    return null;
  }
}

function normalizeLongitude(longitude: number): number {
  return wrap(longitude + 180, 360) - 180;
}

/** The cell holding a coordinate, or null off the grid (a regional model's
 * rectangle, a satellite disk). */
export function cellOf(grid: CellGrid, latitude: number, longitude: number): Cell | null {
  const map = gridMapFor(grid);
  const at = map.lonLatToCell(longitude, latitude);
  if (!at) return null;
  let column = Math.floor(at[0] + 0.5);
  if (grid.wraps) column = wrap(column, grid.width);
  else if (column < 0 || column >= grid.width) return null;
  const row = Math.floor(at[1] + 0.5);
  if (row < 0 || row >= grid.height) return null;
  const [cellLongitude, cellLatitude] = map.cellToLonLat(column, row);
  return {
    // `+ 0` folds a negative zero (a point on the first row or column).
    row: row + 0,
    column: column + 0,
    longitude: normalizeLongitude(cellLongitude),
    latitude: cellLatitude,
  };
}

/** The API's own bounds on a coordinate, checked before the rule. */
export function coordinateError(latitude: number, longitude: number): string | null {
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) return "lat must be within [-90, 90]";
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 360) return "lon must be within [-180, 360]";
  return null;
}

export function sameCellGrid(a: CellGrid, b: CellGrid): boolean {
  return sameGrid(a, b) && a.wraps === b.wraps;
}

/** Each bundle's grid from a manifest's `bundles[]`, the first entry of a
 * repeated id kept; `null` for a grid the rule does not cover. */
export function manifestGrids(manifest: unknown): Map<string, CellGrid | null> {
  const grids = new Map<string, CellGrid | null>();
  const bundles = (manifest as { bundles?: unknown } | null)?.bundles;
  if (!Array.isArray(bundles)) return grids;
  for (const bundle of bundles) {
    const id = (bundle as { variable?: unknown } | null)?.variable;
    if (typeof id !== "string" || grids.has(id)) continue;
    grids.set(id, parseGrid((bundle as { grid?: unknown }).grid));
  }
  return grids;
}

/** The bundles asked for, grouped by grid in first-seen order. */
export function groupByGrid(grids: ReadonlyMap<string, CellGrid | null>, bundles: readonly string[]): GridGroup[] {
  const groups: GridGroup[] = [];
  for (const bundle of bundles) {
    const grid = grids.get(bundle);
    if (!grid) continue;
    const group = groups.find((candidate) => sameCellGrid(candidate.grid, grid));
    if (group) {
      if (!group.bundles.includes(bundle)) group.bundles.push(bundle);
    } else {
      groups.push({ grid, bundles: [bundle] });
    }
  }
  return groups;
}

/** The manifest directory a run name addresses (`xue::zarr::run_directory`):
 * `mrms.2026092509.1455` → `mrms.2026092509/1455`; null for a name the API
 * refuses. */
export function runDirectory(run: string): string | null {
  if (run === "" || !/^[A-Za-z0-9._/-]+$/.test(run)) return null;
  if (run.split("/").some((segment) => /^\.*$/.test(segment))) return null;
  if (run.includes("/")) return run;
  const parts = run.split(".");
  const last = parts[parts.length - 1] ?? "";
  if (parts.length >= 2 && /^[0-9]{4}$/.test(last)) return `${parts.slice(0, -1).join(".")}/${last}`;
  return run;
}

/** A query value spelled the one way every client spells it, so two callers
 * share one cached answer: `A–Z a–z 0–9 - . _ ~` and `, / :` stay as they
 * are, every other UTF-8 byte is `%XX`. */
export function encodeQueryValue(value: string): string {
  let out = "";
  for (const byte of new TextEncoder().encode(value)) {
    const char = String.fromCharCode(byte);
    out += /[A-Za-z0-9\-._~,/:]/.test(char) ? char : `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return out;
}

/** The canonical query of a `/v1/cell` request: parameters in the API's
 * order, `variables` sorted and deduplicated. */
export function cellQuery(request: CellRequest): string {
  const variables = [...new Set(request.variables)].sort();
  const pairs: [string, string][] = [
    ["source", request.source],
    ["run", request.run],
    ["row", String(request.row)],
    ["column", String(request.column)],
  ];
  if (variables.length > 0) pairs.push(["variables", variables.join(",")]);
  if (request.time) pairs.push(["time", request.time]);
  return pairs.map(([key, value]) => `${key}=${encodeQueryValue(value)}`).join("&");
}
