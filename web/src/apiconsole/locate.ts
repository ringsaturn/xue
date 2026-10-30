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

/** A bundle's grid as the manifest declares it, defaults filled in. */
export interface CellGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
  wrapLongitude: boolean;
}

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

function finiteOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** The grid record of `docs/zarr-profile.md`, with the reference defaults
 * for missing fields. Null for an order the rule does not cover (a future
 * projected or reordered grid), so that bundle is reported, not misread. */
export function parseGrid(input: unknown): CellGrid | null {
  if (typeof input !== "object" || input === null) return null;
  const grid = input as Record<string, unknown>;
  if (grid.layout !== undefined && grid.layout !== "row-major") return null;
  if (grid.rowOrder !== undefined && grid.rowOrder !== "north-to-south") return null;
  if (grid.columnOrder !== undefined && grid.columnOrder !== "west-to-east") return null;
  const width = finiteOr(grid.width, 0);
  const longitudeStep = finiteOr(grid.longitudeStep, 0.25);
  return {
    width,
    height: finiteOr(grid.height, 0),
    firstLongitude: finiteOr(grid.firstLongitude, -180),
    firstLatitude: finiteOr(grid.firstLatitude, 90),
    longitudeStep,
    latitudeStep: finiteOr(grid.latitudeStep, -0.25),
    wrapLongitude:
      typeof grid.wrapLongitude === "boolean" ? grid.wrapLongitude : Math.abs(width * longitudeStep - 360) < 1e-6,
  };
}

function wrap(value: number, modulus: number): number {
  return ((value % modulus) + modulus) % modulus;
}

function normalizeLongitude(longitude: number): number {
  return wrap(longitude + 180, 360) - 180;
}

/** The cell holding a coordinate, or null off the grid (a regional model's
 * rectangle, a satellite disk). */
export function cellOf(grid: CellGrid, latitude: number, longitude: number): Cell | null {
  if (grid.width === 0 || grid.height === 0 || grid.longitudeStep === 0 || grid.latitudeStep === 0) return null;
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
  const delta = wrap(longitude - grid.firstLongitude, 360);
  let column = Math.floor(delta / grid.longitudeStep + 0.5);
  if (grid.wrapLongitude) column = wrap(column, grid.width);
  else if (column < 0 || column >= grid.width) return null;
  const row = Math.floor((latitude - grid.firstLatitude) / grid.latitudeStep + 0.5);
  if (row < 0 || row >= grid.height) return null;
  return {
    // `+ 0` folds a negative zero (a point on the first row or column).
    row: row + 0,
    column: column + 0,
    longitude: normalizeLongitude(grid.firstLongitude + column * grid.longitudeStep),
    latitude: grid.firstLatitude + row * grid.latitudeStep,
  };
}

/** The API's own bounds on a coordinate, checked before the rule. */
export function coordinateError(latitude: number, longitude: number): string | null {
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) return "lat must be within [-90, 90]";
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 360) return "lon must be within [-180, 360]";
  return null;
}

export function sameGrid(a: CellGrid, b: CellGrid): boolean {
  return (
    a.width === b.width &&
    a.height === b.height &&
    a.firstLongitude === b.firstLongitude &&
    a.firstLatitude === b.firstLatitude &&
    a.longitudeStep === b.longitudeStep &&
    a.latitudeStep === b.latitudeStep &&
    a.wrapLongitude === b.wrapLongitude
  );
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
    const group = groups.find((candidate) => sameGrid(candidate.grid, grid));
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
