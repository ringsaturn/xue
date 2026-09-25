/** Geographic grid math, mirroring `web/src/probe.ts::geoGrid` / `probeCell`.
 * Kept inline so the API does not pull the frontend's i18n / palette chain;
 * if the two ever disagree, `probe.ts` is the reference. */

export interface GeoGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
  wraps: boolean;
}

export interface ProbeCell {
  column: number;
  row: number;
  longitude: number;
  latitude: number;
}

export function geoGrid(metadata: { grid?: Record<string, unknown> }): GeoGrid {
  const grid = (metadata.grid ?? {}) as Record<string, number | boolean | undefined>;
  const width = (grid.width as number) ?? 0;
  const longitudeStep = (grid.longitudeStep as number) ?? 0.25;
  return {
    width,
    height: (grid.height as number) ?? 0,
    firstLongitude: (grid.firstLongitude as number) ?? -180,
    firstLatitude: (grid.firstLatitude as number) ?? 90,
    longitudeStep,
    latitudeStep: (grid.latitudeStep as number) ?? -0.25,
    wraps: (grid.wrapLongitude as boolean) ?? Math.abs(width * longitudeStep - 360) < 1e-6,
  };
}

export function wrap(value: number, modulus: number): number {
  return ((value % modulus) + modulus) % modulus;
}

export function normalizeLongitude(longitude: number): number {
  return wrap(longitude + 180, 360) - 180;
}

/** The cell a point reads, or null when it is off the grid (a regional
 * model's rectangle, a satellite disk). */
export function probeCell(
  metadata: { grid?: Record<string, unknown> },
  longitude: number,
  latitude: number,
): ProbeCell | null {
  const grid = geoGrid(metadata);
  if (grid.width <= 0 || grid.height <= 0 || grid.longitudeStep === 0 || grid.latitudeStep === 0) return null;
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
  const delta = wrap(longitude - grid.firstLongitude, 360);
  let column = Math.round(delta / grid.longitudeStep);
  if (grid.wraps) {
    column = wrap(column, grid.width);
  } else if (column < 0 || column >= grid.width) {
    return null;
  }
  const row = Math.round((latitude - grid.firstLatitude) / grid.latitudeStep) + 0;
  if (row < 0 || row >= grid.height) return null;
  return {
    column,
    row,
    longitude: normalizeLongitude(grid.firstLongitude + column * grid.longitudeStep),
    latitude: grid.firstLatitude + row * grid.latitudeStep,
  };
}
