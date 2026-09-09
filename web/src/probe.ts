/**
 * Point probe: one grid cell, read across the whole time axis.
 *
 * A click gives a longitude and latitude; the bundle's own grid turns that
 * into a cell index, and every plane the app decodes anyway — the frame on
 * screen, the playback prefetch, a scrub — contributes that cell's code to a
 * series. Nothing is fetched for the probe itself: the series fills in as
 * frames arrive, and a frame that was never decoded is simply a gap. That is
 * what keeps the probe compatible with windowed streaming, where the client
 * deliberately holds only the frames around the playhead.
 *
 * The cell lookup mirrors the fragment shader in layer.ts, with one
 * difference the UI has to be honest about: the shader paints a bicubic
 * reconstruction of the code field, while a probe reads the single nearest
 * cell — the model's own value, not an interpolation of it.
 */

import { decodeValue } from "./palettes";
import type { BundleMetadata, BundleVariable } from "./manifest";

/** The grid cell a probe reads, and the coordinates of its center. */
export interface ProbeCell {
  column: number;
  row: number;
  /** Offset into a decoded plane (row-major from the grid's first corner). */
  index: number;
  longitude: number;
  latitude: number;
}

/** The geographic grid a bundle declares. Metadata carries these beside
 * width/height (docs/format.md); the defaults are the global quarter-degree
 * grid, matching ForecastLayer.configureGrid. */
export interface GeoGrid {
  width: number;
  height: number;
  firstLongitude: number;
  firstLatitude: number;
  longitudeStep: number;
  latitudeStep: number;
  /** The columns cover the full 360 degrees, so they wrap at the
   * antimeridian; a cropped grid's do not. */
  wraps: boolean;
}

export function geoGrid(metadata: BundleMetadata): GeoGrid {
  const grid = metadata.grid as unknown as Record<string, number | boolean | undefined>;
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

/** Positive remainder, the way GLSL's mod() and the spec's degree wrap work. */
export function wrap(value: number, modulus: number): number {
  return ((value % modulus) + modulus) % modulus;
}

/** Longitude in [-180, 180), the form the readout shows. */
export function normalizeLongitude(longitude: number): number {
  return wrap(longitude + 180, 360) - 180;
}

/** The cell containing a point, or null when the point is off the grid — a
 * cropped showcase grid covers one region, and everything outside it has no
 * data, exactly as the shader discards it. */
export function probeCell(
  metadata: BundleMetadata,
  longitude: number,
  latitude: number,
): ProbeCell | null {
  const grid = geoGrid(metadata);
  if (grid.width <= 0 || grid.height <= 0 || grid.longitudeStep === 0 || grid.latitudeStep === 0) {
    return null;
  }
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null;
  // Degrees east of the grid origin, wrapped into [0, 360): a window that
  // crosses the antimeridian stays contiguous instead of splitting in two.
  const delta = wrap(longitude - grid.firstLongitude, 360);
  let column = Math.round(delta / grid.longitudeStep);
  if (grid.wraps) {
    // The last column's east half rounds up to `width`, which is column 0
    // again on a global grid.
    column = wrap(column, grid.width);
  } else if (column < 0 || column >= grid.width) {
    return null;
  }
  // Adding zero folds Math.round's negative zero (a point exactly on the
  // first row) back to zero, so neither the index nor the readout carries it.
  const row = Math.round((latitude - grid.firstLatitude) / grid.latitudeStep) + 0;
  if (row < 0 || row >= grid.height) return null;
  return {
    column,
    row,
    index: row * grid.width + column,
    longitude: normalizeLongitude(grid.firstLongitude + column * grid.longitudeStep),
    latitude: grid.firstLatitude + row * grid.latitudeStep,
  };
}

function seriesKey(variableId: number, frameOffset: number): string {
  return `${variableId}:${frameOffset}`;
}

/**
 * The codes seen so far at one pinned point, keyed by variable and frame
 * offset. Only codes are kept, never planes: a sampled plane is free to be
 * evicted from the frame cache and recycled into the worker right after.
 *
 * Grids differ per session (resolution tiers, and per-variable crops), so the
 * cell is resolved against the metadata of whichever bundle a plane came
 * from, memoized on that metadata's identity.
 */
export class ProbeSeries {
  private readonly codes = new Map<string, number>();
  private cellSource: BundleMetadata | null = null;
  private cell: ProbeCell | null = null;

  constructor(
    readonly longitude: number,
    readonly latitude: number,
  ) {}

  /** The cell this point falls in on one bundle's grid, or null when it
   * falls outside that grid. */
  cellFor(metadata: BundleMetadata): ProbeCell | null {
    if (metadata !== this.cellSource) {
      this.cellSource = metadata;
      this.cell = probeCell(metadata, this.longitude, this.latitude);
    }
    return this.cell;
  }

  /** Record this plane's code at the pinned point. Returns false when the
   * point is off the bundle's grid or the plane is short of the cell. */
  sample(metadata: BundleMetadata, variableId: number, frameOffset: number, plane: Uint8Array): boolean {
    const cell = this.cellFor(metadata);
    if (!cell || cell.index >= plane.length) return false;
    this.codes.set(seriesKey(variableId, frameOffset), plane[cell.index]!);
    return true;
  }

  /** The code sampled for one frame, or undefined when that frame has not
   * been decoded since the point was pinned. */
  code(variableId: number, frameOffset: number): number | undefined {
    return this.codes.get(seriesKey(variableId, frameOffset));
  }

  /** Drop every sample, keeping the pin — what a new run or a new model
   * needs, since their planes are a different dataset. */
  clear(): void {
    this.codes.clear();
    this.cellSource = null;
    this.cell = null;
  }
}

/** One frame of a probed series: a physical value, `null` where the codebook
 * says no data, `undefined` where the frame has not been decoded yet. */
export type ProbeValue = number | null | undefined;

/**
 * The series a set of variables reads at the pinned point, over the given
 * frame offsets. A scalar bundle has one variable; the wind bundle has the
 * u/v pair, and its series is the speed, so a frame counts only once both
 * components are sampled.
 */
export function probeSeriesValues(
  series: ProbeSeries,
  variables: readonly BundleVariable[],
  frameOffsets: readonly number[],
): ProbeValue[] {
  return frameOffsets.map((offset) => {
    const values: (number | null)[] = [];
    for (const variable of variables) {
      const code = series.code(variable.numericId, offset);
      if (code === undefined) return undefined;
      values.push(decodeValue(variable, code));
    }
    if (values.some((value) => value === null)) return null;
    if (values.length === 1) return values[0]!;
    return Math.hypot(...(values as number[]));
  });
}

/** Meteorological wind direction (degrees the wind comes FROM) at one frame,
 * or null when either component is missing at that frame. */
export function probeWindDirection(
  series: ProbeSeries,
  variables: readonly BundleVariable[],
  frameOffset: number,
): number | null {
  if (variables.length < 2) return null;
  const [u, v] = variables;
  const uCode = series.code(u!.numericId, frameOffset);
  const vCode = series.code(v!.numericId, frameOffset);
  if (uCode === undefined || vCode === undefined) return null;
  const uValue = decodeValue(u!, uCode);
  const vValue = decodeValue(v!, vCode);
  if (uValue === null || vValue === null) return null;
  return wrap((Math.atan2(-uValue, -vValue) * 180) / Math.PI, 360);
}
