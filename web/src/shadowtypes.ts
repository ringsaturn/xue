/**
 * The terrain-shadow worker's messages (`shadow.worker.ts`), shared by the
 * worker and the main-thread layer that drives it (`shadowlayer.ts`).
 *
 * The worker answers each request with a sunlit mask over a rectangle of
 * Web Mercator space: 0 where the ground is in a cast shadow or the sun is
 * down, 255 where it is fully lit. The rectangle is in Mercator units, [0, 1]
 * across (west to east) and down (north to south); `x0`/`x1` may run past
 * [0, 1] when the view crosses the antimeridian (unwrapped coordinates).
 */

export interface ShadowRequest {
  type: "shadow";
  /** Increasing per request; a result whose id is older than the newest
   * request sent is stale and dropped. */
  id: number;
  /** The instant the sun is placed at, milliseconds since the epoch. */
  time: number;
  /** The view's bounds in degrees; `west` may exceed `east` numerically only
   * after unwrapping, so callers pass unwrapped longitudes (east > west). */
  bounds: { west: number; south: number; east: number; north: number };
  /** The map's zoom, used to pick the DEM level. */
  zoom: number;
  /** The widest mosaic side in pixels the worker may build (default 2048). */
  maxPixels?: number;
}

export interface ShadowResult {
  type: "shadow";
  id: number;
  time: number;
  /** The DEM level the mosaic was built from. */
  demZoom: number;
  /** The rectangle the mask covers, in Mercator units (see above). */
  rect: { x0: number; y0: number; x1: number; y1: number };
  width: number;
  height: number;
  /** Row-major, north row first, `width * height` bytes; transferred. */
  lit: Uint8Array;
  /** The sun at the rectangle's centre, degrees: azimuth clockwise from
   * north, elevation above the horizon (refraction included). */
  sun: { azimuth: number; elevation: number };
}

export interface ShadowError {
  type: "error";
  id: number;
  message: string;
}

export type ShadowResponse = ShadowResult | ShadowError;
