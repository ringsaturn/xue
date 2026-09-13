/**
 * Web Mercator conversions in world units — the [0, 1] square MapLibre and
 * the layer shaders both work in, where x = 0 is 180°W and y = 0 is the north
 * edge of the projection.
 *
 * Two places need this outside a shader: the wind layer, which seeds
 * particles inside a grid's footprint, and the showcase viewer, which works
 * out the camera limits that keep a case's region on screen.
 */

/** Latitudes beyond this are infinitely far away in Mercator. */
const LATITUDE_LIMIT = 89.999;

export function mercatorX(longitude: number): number {
  return (longitude + 180) / 360;
}

export function mercatorY(latitude: number): number {
  const clamped = Math.max(-LATITUDE_LIMIT, Math.min(LATITUDE_LIMIT, latitude));
  return Math.min(1, Math.max(0, 0.5 - Math.log(Math.tan(Math.PI / 4 + (clamped * Math.PI) / 360)) / (2 * Math.PI)));
}

export function longitudeOf(x: number): number {
  return x * 360 - 180;
}

export function latitudeOf(y: number): number {
  return ((2 * Math.atan(Math.exp((0.5 - y) * 2 * Math.PI)) - Math.PI / 2) * 180) / Math.PI;
}

/** Pixels one world unit spans at a zoom level (MapLibre's 512 px tiles). */
export function worldPixels(zoom: number): number {
  return 512 * 2 ** zoom;
}

/** Zoom at which `worldSize` world units span `pixels` pixels. */
export function zoomForSpan(worldSize: number, pixels: number): number {
  return Math.log2(pixels / worldSize / 512);
}

/** The zoom at which one grid cell of `longitudeStep` degrees spans
 * `cellPixels` CSS pixels along the equator — how far a dataset is worth
 * zooming into before it is only the interpolation being magnified. Never
 * below `floor`: the global 0.25° models reach it at zoom 5.5, and a
 * ceiling under the basemap's own detail would take the map away from
 * the viewer for nothing. */
export function zoomCeilingForStep(longitudeStep: number, cellPixels: number, floor: number): number {
  if (!(longitudeStep > 0)) return floor;
  return Math.max(floor, zoomForSpan(longitudeStep / 360, cellPixels));
}
