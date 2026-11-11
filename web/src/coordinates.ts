/**
 * A point named in the coordinate format the viewer picked (`mapprefs.ts`):
 * the one place the probe panel and the storm cards turn a latitude and
 * longitude into an MGRS, UTM, GARS or British National Grid reference.
 */

import { toBngReference } from "./bng";
import { toGars, type GarsPrecision } from "./gars";
import type { CoordinateFormat } from "./mapprefs";
import { toMgrs, toUtmReference } from "./mgrs";

/** Digits per axis for a resolution in metres: 1 m → 5, 10 m → 4, 100 m →
 * 3, 1 km → 2, 10 km and coarser → 1. */
export function digitsFor(resolutionMetres: number): number {
  const metres = Number.isFinite(resolutionMetres) && resolutionMetres > 0 ? resolutionMetres : 1;
  return Math.max(1, Math.min(5, 5 - Math.ceil(Math.log10(metres) - 1e-9)));
}

/** The GARS cell for a resolution: the 5′ keypad (about 9 km north to
 * south, the finest GARS names) up to 10 km, the 15′ quadrant up to 30 km,
 * the 30′ cell beyond. */
export function garsPrecisionFor(resolutionMetres: number): GarsPrecision {
  if (!(resolutionMetres > 10_000)) return 5;
  return resolutionMetres <= 30_000 ? 15 : 30;
}

/** The finest the National Grid reference goes: 10 m (8 digits), as the
 * Helmert datum shift is only good to a few metres. */
const BNG_MAX_DIGITS = 4;

/** A point in `format` at about `resolutionMetres`, or `null` for
 * `latlon` (the caller formats degrees itself) and wherever the format
 * cannot name the point (the National Grid outside Great Britain), where
 * the caller falls back to degrees. */
export function formatCoordinate(format: CoordinateFormat, lat: number, lon: number, resolutionMetres: number): string | null {
  switch (format) {
    case "latlon":
      return null;
    case "mgrs":
      return toMgrs(lat, lon, digitsFor(resolutionMetres));
    case "utm":
      return toUtmReference(lat, lon, Math.max(1, resolutionMetres));
    case "gars":
      return toGars(lat, lon, garsPrecisionFor(resolutionMetres));
    case "bng":
      return toBngReference(lat, lon, Math.min(BNG_MAX_DIGITS, digitsFor(resolutionMetres)));
  }
}
