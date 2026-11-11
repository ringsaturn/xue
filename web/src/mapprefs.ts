/**
 * The map's reading aids the settings sheet switches: the coordinate grids
 * (MGRS, parallels and meridians) and the format a pinned point's
 * coordinates are written in.
 *
 * The grids are part of what is on screen, so they ride in the address bar
 * (`?grid=`, `urlstate.ts`) as well as in storage; a link that names them
 * outranks the stored choice. The coordinate format is the reader's own
 * habit and stays in storage only. Live bindings, like the theme: read
 * `grid` and `coordinateFormat` at the point of use and redraw on
 * `onMapPrefsChange`.
 */

import { formatGrid, NO_GRID, parseGridFromSearch, searchWithGrid, type GridState } from "./urlstate";

/** How a point's position is written: degrees, or a reference in one of
 * the grids (`coordinates.ts`). A BNG reference falls back to degrees
 * outside Great Britain. */
export const COORDINATE_FORMATS = ["latlon", "mgrs", "utm", "gars", "bng"] as const;
export type CoordinateFormat = (typeof COORDINATE_FORMATS)[number];

const GRID_KEY = "xue-grid";
const COORDS_KEY = "xue-coords";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // The choice still holds for this page.
  }
}

function detectGrid(): GridState {
  if (typeof window === "undefined") return { ...NO_GRID };
  return parseGridFromSearch(window.location.search) ?? parseGridFromSearch(`?grid=${read(GRID_KEY) ?? ""}`) ?? { ...NO_GRID };
}

function detectCoordinateFormat(): CoordinateFormat {
  if (typeof window === "undefined") return "latlon";
  const stored = read(COORDS_KEY);
  return (COORDINATE_FORMATS as readonly string[]).includes(stored ?? "") ? (stored as CoordinateFormat) : "latlon";
}

export let grid: GridState = detectGrid();
export let coordinateFormat: CoordinateFormat = detectCoordinateFormat();

const listeners = new Set<() => void>();

/** Run `listener` after a grid or the coordinate format changes. */
export function onMapPrefsChange(listener: () => void): void {
  listeners.add(listener);
}

function notify(): void {
  for (const listener of listeners) listener();
}

/** Switch the grids: persist, write `?grid=` into the address bar, redraw. */
export function setGrid(next: GridState): void {
  grid = { ...next };
  write(GRID_KEY, formatGrid(grid));
  const search = searchWithGrid(window.location.search, grid);
  window.history.replaceState(null, "", `${window.location.pathname}${search === "?" ? "" : search}${window.location.hash}`);
  notify();
}

export function setCoordinateFormat(next: CoordinateFormat): void {
  coordinateFormat = next;
  write(COORDS_KEY, next);
  notify();
}
