/**
 * The Ordnance Survey's British National Grid: WGS84 ↔ OSGB36 ↔ National
 * Grid eastings and northings ↔ grid references (`TQ 3009 8047`). Pure
 * arithmetic, no DOM.
 *
 * The datum shift is OS's published seven-parameter Helmert transform
 * between ETRS89 (taken as WGS84) and OSGB36. It is good to about ±5 m
 * across Great Britain, not the centimetre of OSTN15's rubber sheet, which
 * is why the references stop at 10 m (8 digits) where a point is named for
 * reading. The projection is the National Grid's transverse Mercator on
 * the Airy 1830 ellipsoid (true origin 49°N 2°W, scale 0.9996012717, false
 * origin 400 km west and 100 km north of it), in Krüger's series to the
 * fourth order in n: sub-millimetre across the grid.
 */

import type { LatLon } from "./mgrs";

const DEG = Math.PI / 180;
const ARCSEC = DEG / 3600;

/** Airy 1830. */
const AIRY_A = 6377563.396;
const AIRY_B = 6356256.909;
/** GRS80, which ETRS89 and (to this transform's accuracy) WGS84 share. */
const GRS80_A = 6378137;
const GRS80_F = 1 / 298.257222101;

const F0 = 0.9996012717;
const LAT0 = 49;
const LON0 = -2;
const E0 = 400_000;
const N0 = -100_000;

/** The National Grid's extent, metres. */
export const BNG_MAX_EASTING = 700_000;
export const BNG_MAX_NORTHING = 1_300_000;

/** The box in degrees a point must fall in to be named on the National
 * Grid: Great Britain and its waters, so a point in France or Ireland,
 * though the projection reaches it, gets no reference. */
export const BNG_LIMITS = { west: -9, east: 3, south: 49, north: 61.5 } as const;

/** A box in degrees that holds the whole 0–700 km × 0–1300 km grid. */
export const BNG_ENVELOPE = { west: -9.6, east: 3.7, south: 49.7, north: 61.65 } as const;

/** ETRS89 → OSGB36, OS's parameters (position-vector rotations). */
const HELMERT = { tx: -446.448, ty: 125.157, tz: -542.06, s: 20.4894e-6, rx: -0.1502 * ARCSEC, ry: -0.247 * ARCSEC, rz: -0.8421 * ARCSEC };

interface Ellipsoid {
  a: number;
  e2: number;
}

const AIRY: Ellipsoid = { a: AIRY_A, e2: 1 - (AIRY_B * AIRY_B) / (AIRY_A * AIRY_A) };
const GRS80: Ellipsoid = { a: GRS80_A, e2: GRS80_F * (2 - GRS80_F) };

function toCartesian(lat: number, lon: number, ellipsoid: Ellipsoid): [number, number, number] {
  const phi = lat * DEG;
  const lambda = lon * DEG;
  const sinPhi = Math.sin(phi);
  const nu = ellipsoid.a / Math.sqrt(1 - ellipsoid.e2 * sinPhi * sinPhi);
  return [nu * Math.cos(phi) * Math.cos(lambda), nu * Math.cos(phi) * Math.sin(lambda), nu * (1 - ellipsoid.e2) * sinPhi];
}

function fromCartesian(x: number, y: number, z: number, ellipsoid: Ellipsoid): LatLon {
  const p = Math.hypot(x, y);
  let phi = Math.atan2(z, p * (1 - ellipsoid.e2));
  for (let step = 0; step < 10; step += 1) {
    const sinPhi = Math.sin(phi);
    const nu = ellipsoid.a / Math.sqrt(1 - ellipsoid.e2 * sinPhi * sinPhi);
    const next = Math.atan2(z + ellipsoid.e2 * nu * sinPhi, p);
    if (Math.abs(next - phi) < 1e-13) {
      phi = next;
      break;
    }
    phi = next;
  }
  return { lat: phi / DEG, lon: Math.atan2(y, x) / DEG };
}

/** The Helmert transform, forward (ETRS89 → OSGB36) or by its negated
 * parameters back, which is how OS states the inverse to this accuracy. */
function helmert([x, y, z]: [number, number, number], sign: 1 | -1): [number, number, number] {
  const { tx, ty, tz, s, rx, ry, rz } = HELMERT;
  const k = 1 + sign * s;
  return [
    sign * tx + k * x - sign * rz * y + sign * ry * z,
    sign * ty + sign * rz * x + k * y - sign * rx * z,
    sign * tz - sign * ry * x + sign * rx * y + k * z,
  ];
}

/** WGS84 → OSGB36 latitude/longitude. */
export function wgs84ToOsgb36(lat: number, lon: number): LatLon {
  return fromCartesian(...helmert(toCartesian(lat, lon, GRS80), 1), AIRY);
}

/** OSGB36 → WGS84 latitude/longitude. */
export function osgb36ToWgs84(lat: number, lon: number): LatLon {
  return fromCartesian(...helmert(toCartesian(lat, lon, AIRY), -1), GRS80);
}

// Krüger's series on Airy 1830.
const N_ = (AIRY_A - AIRY_B) / (AIRY_A + AIRY_B);
const n2 = N_ * N_;
const n3 = n2 * N_;
const n4 = n3 * N_;
const RECT = (AIRY_A / (1 + N_)) * (1 + n2 / 4 + n4 / 64);
const ALPHA = [
  N_ / 2 - (2 / 3) * n2 + (5 / 16) * n3 + (41 / 180) * n4,
  (13 / 48) * n2 - (3 / 5) * n3 + (557 / 1440) * n4,
  (61 / 240) * n3 - (103 / 140) * n4,
  (49561 / 161280) * n4,
];
const BETA = [
  N_ / 2 - (2 / 3) * n2 + (37 / 96) * n3 - (1 / 360) * n4,
  (1 / 48) * n2 + (1 / 15) * n3 - (437 / 1440) * n4,
  (17 / 480) * n3 - (37 / 840) * n4,
  (4397 / 161280) * n4,
];
const DELTA = [
  2 * N_ - (2 / 3) * n2 - 2 * n3 + (116 / 45) * n4,
  (7 / 3) * n2 - (8 / 5) * n3 - (227 / 45) * n4,
  (56 / 15) * n3 - (136 / 35) * n4,
  (4279 / 630) * n4,
];
const TWO_ROOT_N = (2 * Math.sqrt(N_)) / (1 + N_);

/** ξ and η, the TM's unscaled northing and easting over the rectifying
 * radius, from the central meridian. */
function krueger(lat: number, lon: number): [number, number] {
  const phi = lat * DEG;
  const dLon = (lon - LON0) * DEG;
  const sinPhi = Math.sin(phi);
  const t = Math.sinh(Math.atanh(sinPhi) - TWO_ROOT_N * Math.atanh(TWO_ROOT_N * sinPhi));
  const xiP = Math.atan2(t, Math.cos(dLon));
  const etaP = Math.atanh(Math.sin(dLon) / Math.sqrt(1 + t * t));
  let xi = xiP;
  let eta = etaP;
  for (const [i, a] of ALPHA.entries()) {
    const j = i + 1;
    xi += a * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
    eta += a * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
  }
  return [xi, eta];
}

/** The true origin's ξ: the National Grid's northings count from 49°N. */
const XI0 = krueger(LAT0, LON0)[0];

export interface BngPoint {
  easting: number;
  northing: number;
}

/** OSGB36 latitude/longitude → National Grid metres. */
export function osgb36ToGrid(lat: number, lon: number): BngPoint {
  const [xi, eta] = krueger(lat, lon);
  return { easting: E0 + F0 * RECT * eta, northing: N0 + F0 * RECT * (xi - XI0) };
}

/** National Grid metres → OSGB36 latitude/longitude. */
export function gridToOsgb36(easting: number, northing: number): LatLon {
  const xi = (northing - N0) / (F0 * RECT) + XI0;
  const eta = (easting - E0) / (F0 * RECT);
  let xiP = xi;
  let etaP = eta;
  for (const [i, b] of BETA.entries()) {
    const j = i + 1;
    xiP -= b * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    etaP -= b * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }
  const chi = Math.asin(Math.sin(xiP) / Math.cosh(etaP));
  let phi = chi;
  for (const [i, d] of DELTA.entries()) phi += d * Math.sin(2 * (i + 1) * chi);
  return { lat: phi / DEG, lon: LON0 + Math.atan2(Math.sinh(etaP), Math.cos(xiP)) / DEG };
}

/** WGS84 → National Grid metres (the projection extends past the grid;
 * nothing here is clipped). */
export function toBng(lat: number, lon: number): BngPoint {
  const osgb = wgs84ToOsgb36(lat, lon);
  return osgb36ToGrid(osgb.lat, osgb.lon);
}

/** National Grid metres → WGS84. */
export function fromBng(easting: number, northing: number): LatLon {
  const osgb = gridToOsgb36(easting, northing);
  return osgb36ToWgs84(osgb.lat, osgb.lon);
}

/** The two letters of the 100 km square holding a grid point, `TQ`, or
 * `null` off the grid. The first letter names the 500 km square (S, T, N,
 * H, O, … from a false origin 1 000 km west and 500 km south of the grid's
 * SW corner), the second the 100 km square in it; both skip I. */
export function bngSquareId(easting: number, northing: number): string | null {
  if (!(easting >= 0 && easting < BNG_MAX_EASTING && northing >= 0 && northing < BNG_MAX_NORTHING)) return null;
  const e100 = Math.floor(easting / 100_000);
  const n100 = Math.floor(northing / 100_000);
  let first = 19 - n100 - ((19 - n100) % 5) + Math.floor((e100 + 10) / 5);
  let second = (((19 - n100) * 5) % 25) + (e100 % 5);
  if (first > 7) first += 1;
  if (second > 7) second += 1;
  return String.fromCharCode(65 + first) + String.fromCharCode(65 + second);
}

/** The letter of the 500 km square holding a grid point, `S`, `T`, `N`,
 * `H`, `O` or `J`. */
export function bngMajorSquare(easting: number, northing: number): string | null {
  return bngSquareId(easting, northing)?.charAt(0) ?? null;
}

function digits(value: number, precision: number): string {
  const metres = Math.floor(((value % 100_000) + 100_000) % 100_000);
  return String(Math.floor(metres / 10 ** (5 - precision))).padStart(precision, "0");
}

/** The grid reference of a WGS84 point, `TQ 3009 8047`: `precision`
 * digits per axis (1–5, 10 km to 1 m), truncated, as a grid reference
 * names the square a point is in. `null` off the grid or outside
 * `BNG_LIMITS`. */
export function toBngReference(lat: number, lon: number, precision = 4): string | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (lat < BNG_LIMITS.south || lat > BNG_LIMITS.north || lon < BNG_LIMITS.west || lon > BNG_LIMITS.east) return null;
  const p = Math.max(1, Math.min(5, Math.round(precision)));
  const { easting, northing } = toBng(lat, lon);
  const square = bngSquareId(easting, northing);
  if (square === null) return null;
  return `${square} ${digits(easting, p)} ${digits(northing, p)}`;
}

/** The south-west corner in grid metres of the square a reference names
 * (`TQ 3009 8047`, spaces optional), or `null` for one that does not
 * parse. */
export function parseBngReference(text: string): BngPoint | null {
  const match = /^([A-HJ-Z])([A-HJ-Z])(\d*)$/.exec(text.replace(/\s+/g, "").toUpperCase());
  if (!match) return null;
  const numbers = match[3] ?? "";
  if (numbers.length % 2 !== 0 || numbers.length > 10) return null;
  const index = (letter: string) => {
    const code = letter.charCodeAt(0) - 65;
    return code > 7 ? code - 1 : code;
  };
  const first = index(match[1] ?? "");
  const second = index(match[2] ?? "");
  const e100 = ((first % 5) - 2) * 5 + (second % 5);
  const n100 = (3 - Math.floor(first / 5)) * 5 + (4 - Math.floor(second / 5));
  const p = numbers.length / 2;
  const scale = 10 ** (5 - p);
  const easting = e100 * 100_000 + (p === 0 ? 0 : Number(numbers.slice(0, p)) * scale);
  const northing = n100 * 100_000 + (p === 0 ? 0 : Number(numbers.slice(p)) * scale);
  if (bngSquareId(easting, northing) === null) return null;
  return { easting, northing };
}
