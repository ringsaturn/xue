/**
 * Terrain cast shadows: where the sun is, and which ground it reaches.
 *
 * Pure and DOM-free so the arithmetic is testable; `shadow.worker.ts` fetches
 * the relief and calls in here. Everything works on a mosaic of Terrarium
 * pixels laid out in Web Mercator at one DEM level: Mercator is conformal, so
 * a compass bearing is the same direction in pixel space everywhere and a
 * pixel is square on the ground, `metresPerPixel` across in both axes.
 *
 * The sun is placed per pixel, not once per view: a view can span half the
 * globe, and the terminator only shows when each pixel has its own sun.
 */

import { TERRAIN_MAX_ZOOM, TERRAIN_TILE_SIZE } from "./terrain";

const DEG = Math.PI / 180;
/** Web Mercator's latitude limit, where the square world ends. */
export const MERCATOR_MAX_LAT = 85.05112878;
const EQUATOR_METRES = 40075016.7;
const EARTH_RADIUS = 6371000;
/** Terrestrial refraction bends sight lines over the curve; surveyors fold it
 * into an effective radius R / (1 - k), k = 0.13. */
const EFFECTIVE_RADIUS = EARTH_RADIUS / (1 - 0.13);
/** The sun's apparent radius: the penumbra's half-width. */
const SUN_RADIUS = 0.267;
/** Civil twilight: below this the ground gets no light worth drawing. */
const NIGHT = -6;
const MIN_ELEVATION = 0.5;
export const MAX_SHADOW_METRES = 120000;
/** The relief assumed before any tile is read, sizing the margin casters can
 * stand in. */
export const DEFAULT_RELIEF = 4000;
export const DEFAULT_MAX_PIXELS = 2048;
const MARGIN_PIXELS = 64;
const STEP_GROWTH = 1.06;

export interface SunPosition {
  /** Degrees clockwise from north. */
  azimuth: number;
  /** Degrees above the horizon. */
  elevation: number;
}

/** The part of the solar position that depends only on the instant: the
 * declination and the equation of time. Computed once per request; each
 * pixel then needs only its own hour angle. */
export interface SolarEphemeris {
  /** Radians. */
  declination: number;
  /** Minutes of true solar time past UTC midnight at longitude 0. */
  solarMinutes: number;
}

/** NOAA's general solar position algorithm (the one behind its solar
 * calculator spreadsheet), good to ~0.01° for this century. */
export function solarEphemeris(timeMs: number): SolarEphemeris {
  const jd = timeMs / 86400000 + 2440587.5;
  const t = (jd - 2451545) / 36525;
  const l0 = mod360(280.46646 + t * (36000.76983 + t * 0.0003032));
  const m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
  const ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const mr = m * DEG;
  const centre =
    Math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
    Math.sin(2 * mr) * (0.019993 - 0.000101 * t) +
    Math.sin(3 * mr) * 0.000289;
  const omega = (125.04 - 1934.136 * t) * DEG;
  const apparentLong = (l0 + centre - 0.00569 - 0.00478 * Math.sin(omega)) * DEG;
  const meanObliquity = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60;
  const obliquity = (meanObliquity + 0.00256 * Math.cos(omega)) * DEG;
  const declination = Math.asin(Math.sin(obliquity) * Math.sin(apparentLong));
  const y = Math.tan(obliquity / 2) ** 2;
  const l0r = l0 * DEG;
  const equationOfTime =
    (4 / DEG) *
    (y * Math.sin(2 * l0r) -
      2 * ecc * Math.sin(mr) +
      4 * ecc * y * Math.sin(mr) * Math.cos(2 * l0r) -
      0.5 * y * y * Math.sin(4 * l0r) -
      1.25 * ecc * ecc * Math.sin(2 * mr));
  const utcMinutes = (((timeMs % 86400000) + 86400000) % 86400000) / 60000;
  return { declination, solarMinutes: utcMinutes + equationOfTime };
}

/** The hour angle at a longitude, radians, west of the meridian positive. */
function hourAngle(ephemeris: SolarEphemeris, lonDeg: number): number {
  return ((ephemeris.solarMinutes + 4 * lonDeg) / 4 - 180) * DEG;
}

/** The geometric sun (no refraction) seen from a place. */
export function solarPosition(timeMs: number, latDeg: number, lonDeg: number): SunPosition {
  const ephemeris = solarEphemeris(timeMs);
  const h = hourAngle(ephemeris, lonDeg);
  return sunFrom(Math.sin(latDeg * DEG), Math.cos(latDeg * DEG), ephemeris.declination, Math.sin(h), Math.cos(h));
}

function sunFrom(sinLat: number, cosLat: number, declination: number, sinH: number, cosH: number): SunPosition {
  const sinD = Math.sin(declination);
  const cosD = Math.cos(declination);
  const sinE = sinLat * sinD + cosLat * cosD * cosH;
  const elevation = Math.asin(Math.max(-1, Math.min(1, sinE))) / DEG;
  const azimuth = mod360(Math.atan2(-sinH * cosD, cosLat * sinD - sinLat * cosD * cosH) / DEG);
  return { azimuth, elevation };
}

/** NOAA's atmospheric refraction correction, degrees to add to a geometric
 * elevation. The branches meet continuously, so it is applied below the
 * horizon too, where it shifts the twilight band by a fraction of a degree. */
export function refraction(elevationDeg: number): number {
  const e = elevationDeg;
  if (e > 85) return 0;
  const te = Math.tan(e * DEG);
  let seconds: number;
  if (e > 5) {
    const inv = 1 / te;
    const inv2 = inv * inv;
    seconds = inv * (58.1 + inv2 * (-0.07 + inv2 * 0.000086));
  }
  else if (e > -0.575) seconds = 1735 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)));
  else seconds = -20.772 / te;
  return seconds / 3600;
}

export function refractedElevation(elevationDeg: number): number {
  return elevationDeg + refraction(elevationDeg);
}

/** The sun as seen, refraction included. */
export function apparentSun(timeMs: number, latDeg: number, lonDeg: number): SunPosition {
  const sun = solarPosition(timeMs, latDeg, lonDeg);
  return { azimuth: sun.azimuth, elevation: refractedElevation(sun.elevation) };
}

export interface Bounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** Pixels across the world at a DEM level. */
export function worldPixels(demZoom: number): number {
  return TERRAIN_TILE_SIZE * 2 ** demZoom;
}

export function mercatorX(lonDeg: number): number {
  return (lonDeg + 180) / 360;
}

export function mercatorY(latDeg: number): number {
  const lat = Math.max(-MERCATOR_MAX_LAT, Math.min(MERCATOR_MAX_LAT, latDeg));
  return (1 - Math.asinh(Math.tan(lat * DEG)) / Math.PI) / 2;
}

export function latitudeAt(mercY: number): number {
  return Math.atan(Math.sinh(Math.PI * (1 - 2 * mercY))) / DEG;
}

/** Ground metres one DEM pixel spans at a latitude. */
export function metresPerPixel(demZoom: number, latDeg: number): number {
  return (EQUATOR_METRES / worldPixels(demZoom)) * Math.cos(latDeg * DEG);
}

function viewPixels(bounds: Bounds, demZoom: number): { width: number; height: number } {
  const world = worldPixels(demZoom);
  return {
    width: (mercatorX(bounds.east) - mercatorX(bounds.west)) * world,
    height: (mercatorY(bounds.south) - mercatorY(bounds.north)) * world,
  };
}

/** The DEM level a view is sampled at: the one whose pixels match the map's
 * (MapLibre's zoom is in 512 px tiles, as Terrarium's are), capped at the
 * archive's z12 and lowered until the view alone fits in `maxPixels`. */
export function demZoomFor(bounds: Bounds, zoom: number, maxPixels = DEFAULT_MAX_PIXELS): number {
  let demZoom = Math.max(0, Math.min(TERRAIN_MAX_ZOOM, Math.round(zoom)));
  while (demZoom > 0) {
    const { width, height } = viewPixels(bounds, demZoom);
    if (width <= maxPixels && height <= maxPixels) break;
    demZoom -= 1;
  }
  return demZoom;
}

export interface MosaicTile {
  z: number;
  /** Wrapped into [0, 2^z). */
  x: number;
  y: number;
  /** Where the tile's top-left pixel lands in the mosaic; may be negative. */
  offsetX: number;
  offsetY: number;
}

/** A pixel rectangle inside the mosaic. */
export interface PixelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface MercatorRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface MosaicGeometry {
  demZoom: number;
  /** The mosaic's top-left in world pixels at `demZoom`; x is unwrapped. */
  originX: number;
  originY: number;
  width: number;
  height: number;
  /** The view inside the mosaic: the only pixels the mask is computed for;
   * the rest is margin for casters to stand in. */
  inner: PixelRect;
}

export interface MosaicPlan extends MosaicGeometry {
  tiles: MosaicTile[];
  /** The whole mosaic and the view inside it, in Mercator units. */
  rect: MercatorRect;
  innerRect: MercatorRect;
  /** The apparent sun at the view's centre, which sets the margin. */
  sun: SunPosition;
}

export interface PlanOptions {
  maxPixels?: number;
  /** The tallest relief a caster may stand above the shadowed ground. */
  relief?: number;
}

/** The mosaic a view's shadows are computed on. The view needs margin only
 * toward the sun, where the casters of its shadows stand; its length is the
 * longest shadow the relief can cast at the centre's sun elevation. A
 * 64 px collar on every side covers the bearing's drift across the view. */
export function planMosaic(bounds: Bounds, zoom: number, timeMs: number, options: PlanOptions = {}): MosaicPlan {
  const maxPixels = options.maxPixels ?? DEFAULT_MAX_PIXELS;
  const relief = options.relief ?? DEFAULT_RELIEF;
  const north = Math.min(MERCATOR_MAX_LAT, bounds.north);
  const south = Math.max(-MERCATOR_MAX_LAT, bounds.south);
  const clamped = { west: bounds.west, east: bounds.east, north, south };
  const centreLat = latitudeAt((mercatorY(north) + mercatorY(south)) / 2);
  const centreLon = (bounds.west + bounds.east) / 2;
  const sun = apparentSun(timeMs, centreLat, centreLon);
  const shadowMetres = Math.min(relief / Math.tan(Math.max(sun.elevation, MIN_ELEVATION) * DEG), MAX_SHADOW_METRES);
  const sinA = Math.sin(sun.azimuth * DEG);
  const cosA = Math.cos(sun.azimuth * DEG);

  let demZoom = demZoomFor(clamped, zoom, maxPixels);
  for (;;) {
    const plan = layout(clamped, demZoom, shadowMetres / metresPerPixel(demZoom, centreLat), sinA, cosA);
    if ((plan.width <= maxPixels && plan.height <= maxPixels) || demZoom === 0) {
      return { ...plan, sun };
    }
    demZoom -= 1;
  }
}

function layout(
  bounds: Bounds,
  demZoom: number,
  shadowPixels: number,
  sinA: number,
  cosA: number,
): Omit<MosaicPlan, "sun"> {
  const world = worldPixels(demZoom);
  const vx0 = Math.floor(mercatorX(bounds.west) * world);
  const vx1 = Math.ceil(mercatorX(bounds.east) * world);
  const vy0 = Math.max(0, Math.floor(mercatorY(bounds.north) * world));
  const vy1 = Math.min(world, Math.ceil(mercatorY(bounds.south) * world));
  const east = Math.ceil(MARGIN_PIXELS + Math.max(0, sinA) * shadowPixels);
  const west = Math.ceil(MARGIN_PIXELS + Math.max(0, -sinA) * shadowPixels);
  const up = Math.ceil(MARGIN_PIXELS + Math.max(0, cosA) * shadowPixels);
  const down = Math.ceil(MARGIN_PIXELS + Math.max(0, -cosA) * shadowPixels);
  const originX = vx0 - west;
  const originY = Math.max(0, vy0 - up);
  const endX = vx1 + east;
  const endY = Math.min(world, vy1 + down);
  const width = Math.max(1, endX - originX);
  const height = Math.max(1, endY - originY);
  const inner = { x: vx0 - originX, y: vy0 - originY, width: Math.max(1, vx1 - vx0), height: Math.max(1, vy1 - vy0) };

  const tiles: MosaicTile[] = [];
  const count = 2 ** demZoom;
  const size = TERRAIN_TILE_SIZE;
  for (let ty = Math.floor(originY / size); ty * size < endY; ty++) {
    for (let tx = Math.floor(originX / size); tx * size < endX; tx++) {
      tiles.push({
        z: demZoom,
        x: ((tx % count) + count) % count,
        y: ty,
        offsetX: tx * size - originX,
        offsetY: ty * size - originY,
      });
    }
  }
  return {
    demZoom,
    originX,
    originY,
    width,
    height,
    inner,
    tiles,
    rect: { x0: originX / world, y0: originY / world, x1: endX / world, y1: endY / world },
    innerRect: {
      x0: vx0 / world,
      y0: vy0 / world,
      x1: (vx0 + inner.width) / world,
      y1: (vy0 + inner.height) / world,
    },
  };
}

export interface MarchOptions {
  /** One sun for every pixel, elevation already refracted (tests). */
  fixedSun?: SunPosition;
  /** Off flattens the earth (tests). */
  curvature?: boolean;
  maxDistance?: number;
}

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

/**
 * The sunlit mask over the mosaic's inner rectangle, 0 (cast shadow or night)
 * to 255 (full sun), north row first.
 *
 * Each pixel marches toward its own sun, steps growing ×1.06 from one pixel,
 * and keeps the steepest terrain angle it meets (bilinear heights, less the
 * earth's drop d²/2R′). The march stops at the mosaic's edge, at the longest
 * shadow the relief allows, once the sun is fully hidden, or once even the
 * mosaic's highest point could no longer rise above the angle that matters.
 *
 * A sun below the horizon is tested as if it stood on it (plus its radius):
 * ridges above the local horizontal still darken valleys first, while open
 * ground fades through civil twilight instead of snapping dark at 0°.
 */
export function marchLit(
  elevation: Float32Array,
  geometry: MosaicGeometry,
  timeMs: number,
  options: MarchOptions = {},
): Uint8Array {
  const { demZoom, originX, originY, width, height, inner } = geometry;
  const world = worldPixels(demZoom);
  const maxDistance = options.maxDistance ?? MAX_SHADOW_METRES;
  const inverse2R = options.curvature === false ? 0 : 1 / (2 * EFFECTIVE_RADIUS);
  const out = new Uint8Array(inner.width * inner.height);

  let zMax = -Infinity;
  for (let i = 0; i < elevation.length; i++) {
    const z = elevation[i] as number;
    if (z > zMax) zMax = z;
  }

  const ephemeris = solarEphemeris(timeMs);
  const sinD = Math.sin(ephemeris.declination);
  const cosD = Math.cos(ephemeris.declination);
  const columnSinH = new Float64Array(inner.width);
  const columnCosH = new Float64Array(inner.width);
  for (let i = 0; i < inner.width; i++) {
    const lon = ((originX + inner.x + i + 0.5) / world) * 360 - 180;
    const h = hourAngle(ephemeris, lon);
    columnSinH[i] = Math.sin(h);
    columnCosH[i] = Math.cos(h);
  }
  const fixed = options.fixedSun;
  const fixedDx = fixed ? Math.sin(fixed.azimuth * DEG) : 0;
  const fixedDy = fixed ? -Math.cos(fixed.azimuth * DEG) : 0;
  const lastX = width - 1;
  const lastY = height - 1;

  for (let row = 0; row < inner.height; row++) {
    const my = inner.y + row;
    const lat = latitudeAt((originY + my + 0.5) / world);
    const sinLat = Math.sin(lat * DEG);
    const cosLat = Math.cos(lat * DEG);
    const mpp = metresPerPixel(demZoom, lat);
    const rowBase = row * inner.width;
    for (let col = 0; col < inner.width; col++) {
      let e: number;
      let dx: number;
      let dy: number;
      if (fixed) {
        e = fixed.elevation;
        dx = fixedDx;
        dy = fixedDy;
      } else {
        const sinH = columnSinH[col] as number;
        const cosH = columnCosH[col] as number;
        const sinE = sinLat * sinD + cosLat * cosD * cosH;
        const geometric = Math.asin(sinE > 1 ? 1 : sinE < -1 ? -1 : sinE) / DEG;
        e = geometric + refraction(geometric);
        // The azimuth as a pixel-space unit vector (north is -y), without
        // the trigonometry of an angle in between.
        const east = -sinH * cosD;
        const northward = cosLat * sinD - sinLat * cosD * cosH;
        const norm = Math.sqrt(east * east + northward * northward) || 1;
        dx = east / norm;
        dy = -northward / norm;
      }
      if (e < NIGHT) {
        out[rowBase + col] = 0;
        continue;
      }
      const twilight = e < 0 ? smoothstep(NIGHT, 0, e) : 1;
      const sunE = Math.max(e, SUN_RADIUS);
      const mx = inner.x + col;
      const z0 = elevation[my * width + mx] as number;
      const relief = zMax - z0;
      let horizonTan = -Infinity;
      if (relief > 0) {
        const limit = Math.min(relief / Math.tan(Math.max(sunE, MIN_ELEVATION) * DEG), maxDistance);
        const hiddenTan = sunE + SUN_RADIUS >= 90 ? Infinity : Math.tan((sunE + SUN_RADIUS) * DEG);
        const mattersTan = Math.tan((sunE - SUN_RADIUS) * DEG);
        let step = 1;
        let travelled = 0;
        for (;;) {
          travelled += step;
          step *= STEP_GROWTH;
          const d = travelled * mpp;
          if (d > limit) break;
          const x = mx + dx * travelled;
          const y = my + dy * travelled;
          if (x < 0 || y < 0 || x > lastX || y > lastY) break;
          const drop = d * d * inverse2R;
          const x0 = x | 0;
          const y0 = y | 0;
          const fx = x - x0;
          const fy = y - y0;
          const i0 = y0 * width + x0;
          const x1 = x0 < lastX ? 1 : 0;
          const i1 = y0 < lastY ? i0 + width : i0;
          const a = elevation[i0] as number;
          const b = elevation[i0 + x1] as number;
          const c = elevation[i1] as number;
          const g = elevation[i1 + x1] as number;
          const top = a + (b - a) * fx;
          const z = top + (c + (g - c) * fx - top) * fy;
          const tan = (z - drop - z0) / d;
          if (tan > horizonTan) {
            horizonTan = tan;
            if (horizonTan >= hiddenTan) break;
          }
          // Nothing farther can rise above both the horizon so far and the
          // lowest angle that still dims the sun.
          if ((zMax - drop - z0) / d <= Math.max(horizonTan, mattersTan)) break;
        }
      }
      const horizon = horizonTan === -Infinity ? -90 : Math.atan(horizonTan) / DEG;
      const lit = smoothstep(-SUN_RADIUS, SUN_RADIUS, sunE - horizon) * twilight;
      out[rowBase + col] = Math.round(lit * 255);
    }
  }
  return out;
}

function mod360(degrees: number): number {
  return ((degrees % 360) + 360) % 360;
}
