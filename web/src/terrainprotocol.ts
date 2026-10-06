import { AJAXError, addProtocol } from "maplibre-gl";
import type { GetResourceResponse, RequestParameters } from "maplibre-gl";
import { TERRAIN_MAX_ZOOM, terrainTileUrl } from "./terrain";
import { TERRAIN_COVERAGE_CELL_ZOOM, TERRAIN_COVERAGE_CELLS } from "./terraincoverage";

/**
 * The relief the map draws, past the planet's z12 wherever Mapterhorn has a
 * regional archive (Japan, Europe, North America, New Zealand … to z16–18).
 *
 * The sources ask for tiles through this protocol instead of the HTTPS
 * template. A tile inside a covered zoom-6 cell, or any tile at z12 and
 * above it, is fetched as is; anywhere else the handler answers 404 without
 * touching the network. A 404 is the one error MapLibre takes quietly: no
 * error event, and the tile manager draws the nearest loaded parent in its
 * place — the overzoomed z12 tile the map showed before. Inside a covered
 * cell a tile the archive lacks (open sea, the edge of a national survey)
 * comes back 404 from Mapterhorn itself and falls back the same way.
 *
 * Only the map's sources go through here. The pin's readout and the shadow
 * worker read the global z12 tiles directly (`terrain.ts`), so their numbers
 * do not change with where a regional survey happens to end.
 */

const TERRAIN_PROTOCOL = "mapterhorn";
export const TERRAIN_DETAIL_TILES = [`${TERRAIN_PROTOCOL}://{z}/{x}/{y}`];

let deepestZoomByCell: Map<string, number> | null = null;

function coverage(): Map<string, number> {
  if (deepestZoomByCell) return deepestZoomByCell;
  deepestZoomByCell = new Map();
  for (const entry of TERRAIN_COVERAGE_CELLS.split(",")) {
    const [x, y, zoom] = entry.split("/");
    deepestZoomByCell.set(`${x}/${y}`, Number(zoom));
  }
  return deepestZoomByCell;
}

/** The deepest zoom any cell reaches: the sources' `maxzoom`. MapLibre asks
 * no deeper than the camera needs, so the ceiling costs nothing where the
 * view stops short of it. */
export const TERRAIN_DETAIL_MAX_ZOOM = Math.max(
  TERRAIN_MAX_ZOOM,
  ...TERRAIN_COVERAGE_CELLS.split(",").map((entry) => Number(entry.split("/")[2])),
);

/** Whether Mapterhorn publishes a tile at this address: everywhere to the
 * planet's ceiling, past it only inside a cell whose archive goes that deep. */
export function terrainTileCovered(zoom: number, x: number, y: number): boolean {
  if (zoom <= TERRAIN_MAX_ZOOM) return true;
  const shift = zoom - TERRAIN_COVERAGE_CELL_ZOOM;
  const deepest = coverage().get(`${x >> shift}/${y >> shift}`);
  return deepest !== undefined && zoom <= deepest;
}

const ADDRESS = new RegExp(`^${TERRAIN_PROTOCOL}://(\\d+)/(\\d+)/(\\d+)$`);

async function loadTerrainTile(
  params: RequestParameters,
  abortController: AbortController,
): Promise<GetResourceResponse<ArrayBuffer>> {
  const match = ADDRESS.exec(params.url);
  if (!match) throw new Error(`not a terrain tile address: ${params.url}`);
  const [zoom, x, y] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const url = terrainTileUrl(zoom, x, y);
  if (!terrainTileCovered(zoom, x, y)) throw new AJAXError(404, "Not Found", url, new Blob());
  const response = await fetch(url, { signal: abortController.signal });
  if (!response.ok) throw new AJAXError(response.status, response.statusText, url, await response.blob());
  return {
    data: await response.arrayBuffer(),
    cacheControl: response.headers.get("cache-control"),
    expires: response.headers.get("expires"),
  };
}

export function registerTerrainProtocol(): void {
  addProtocol(TERRAIN_PROTOCOL, loadTerrainTile);
}
