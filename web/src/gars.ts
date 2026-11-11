/**
 * The Global Area Reference System (NGA): a lat/lon grid named in three
 * nested steps. Pure arithmetic, no DOM.
 *
 * - a 30′ × 30′ cell: three digits for the longitude band (001–720,
 *   eastward from 180°W) and two letters for the latitude band (AA–QZ,
 *   northward from 90°S; the letters skip I and O, so the second letter
 *   runs A–Z in 24 steps and the first A–Q);
 * - a 15′ quadrant, 1–4: 1 NW, 2 NE, 3 SW, 4 SE;
 * - a 5′ keypad cell, 1–9 within the quadrant, laid out as a telephone
 *   keypad: 1 NW, 2 N, 3 NE, … 9 SE.
 *
 * `006AG39` is band 6 (177°30′W–177°W), band AG (87°S–86°30′S), quadrant
 * 3 (the SW 15′), keypad 9 (its SE 5′). A point on a line belongs to the
 * cell east or north of it, except on 180°E and 90°N, which belong to the
 * last cell.
 */

import { wrapLongitude } from "./mgrs";

const LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ";
const LON_BANDS = 720;
const LAT_BANDS = 360;
/** Points this close (in minutes) under a line count as on it: a decimal
 * degree times 60 can land a hair short of the minute it names. */
const SNAP = 1e-9;

/** The cell sizes GARS names, in minutes of arc. */
export type GarsPrecision = 30 | 15 | 5;

export interface GarsCell {
  /** The south-west corner, degrees. */
  south: number;
  west: number;
  /** The cell's side, degrees (0.5, 0.25 or 1/12). */
  size: number;
}

/** The longitude band (1–720) and latitude band index (0–359) of a point,
 * and its offset into that 30′ cell in minutes (0–30). */
function locate(lat: number, lon: number): { lonBand: number; latBand: number; dx: number; dy: number } {
  const x = lon === 180 ? 180 : wrapLongitude(lon);
  const mx = (x + 180) * 60;
  const my = (lat + 90) * 60;
  const lonBand = Math.min(LON_BANDS - 1, Math.floor((mx + SNAP) / 30));
  const latBand = Math.min(LAT_BANDS - 1, Math.floor((my + SNAP) / 30));
  return {
    lonBand: lonBand + 1,
    latBand,
    dx: Math.min(30, Math.max(0, mx - lonBand * 30)),
    dy: Math.min(30, Math.max(0, my - latBand * 30)),
  };
}

/** The five characters of a 30′ cell, `006AG`. */
function cellName(lonBand: number, latBand: number): string {
  return String(lonBand).padStart(3, "0") + LETTERS.charAt(Math.floor(latBand / 24)) + LETTERS.charAt(latBand % 24);
}

/** The index (0, 1, 2) of a step along an axis, the last one taking the
 * cell's far edge. */
function part(offset: number, size: number, count: number): number {
  return Math.min(count - 1, Math.floor((offset + SNAP) / size));
}

/** The GARS reference of a point at a precision: `006AG` (30′), `006AG3`
 * (15′) or `006AG39` (5′). `null` for a point that is not a coordinate. */
export function toGars(lat: number, lon: number, precision: GarsPrecision = 5): string | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90) return null;
  const { lonBand, latBand, dx, dy } = locate(lat, lon);
  let out = cellName(lonBand, latBand);
  if (precision === 30) return out;
  const east = part(dx, 15, 2);
  const north = part(dy, 15, 2);
  // 1 and 2 are the northern pair.
  out += String((1 - north) * 2 + east + 1);
  if (precision === 15) return out;
  const column = part(dx - east * 15, 5, 3);
  const row = part(dy - north * 15, 5, 3);
  return out + String((2 - row) * 3 + column + 1);
}

/** The cell a GARS reference names (case and spaces ignored), or `null`
 * for one that does not parse. */
export function parseGars(text: string): GarsCell | null {
  const match = /^(\d{3})([A-HJ-NP-Q])([A-HJ-NP-Z])([1-4])?([1-9])?$/.exec(text.replace(/\s+/g, "").toUpperCase());
  if (!match) return null;
  const lonBand = Number(match[1]);
  const latBand = LETTERS.indexOf(match[2] ?? "") * 24 + LETTERS.indexOf(match[3] ?? "");
  if (lonBand < 1 || lonBand > LON_BANDS || latBand < 0 || latBand >= LAT_BANDS) return null;
  let west = -180 + (lonBand - 1) / 2;
  let south = -90 + latBand / 2;
  let size = 0.5;
  const quadrant = match[4];
  const keypad = match[5];
  if (keypad !== undefined && quadrant === undefined) return null;
  if (quadrant !== undefined) {
    const q = Number(quadrant) - 1;
    size = 0.25;
    west += (q % 2) * size;
    south += (q < 2 ? 1 : 0) * size;
  }
  if (keypad !== undefined) {
    const k = Number(keypad) - 1;
    size = 1 / 12;
    west += (k % 3) * size;
    south += (2 - Math.floor(k / 3)) * size;
  }
  return { south, west, size };
}

