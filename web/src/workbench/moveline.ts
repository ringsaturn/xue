/** A line moved whole: its length and the way it sets out are kept, whatever
 * the map does to its shape. */

import type { LonLat, SectionLine } from "../crosssection/types";

const RADIANS = Math.PI / 180;
/** As near a pole as a line's start is taken: past it the way it sets out
 * turns under the hand. */
const LIMIT = 85;

/** The central angle from one place to another and the bearing it sets out on,
 * radians. */
function course([lonA, latA]: LonLat, [lonB, latB]: LonLat): { angle: number; bearing: number } {
  const phiA = latA * RADIANS;
  const phiB = latB * RADIANS;
  const delta = (lonB - lonA) * RADIANS;
  const y = Math.sin(delta) * Math.cos(phiB);
  const x = Math.cos(phiA) * Math.sin(phiB) - Math.sin(phiA) * Math.cos(phiB) * Math.cos(delta);
  return { angle: Math.atan2(Math.hypot(x, y), Math.sin(phiA) * Math.sin(phiB) + Math.cos(phiA) * Math.cos(phiB) * Math.cos(delta)), bearing: Math.atan2(y, x) };
}

/** Where a course from a place ends, its longitude the nearest turn's. */
function along([lon, lat]: LonLat, angle: number, bearing: number): LonLat {
  const phi = lat * RADIANS;
  const sin = Math.sin(phi) * Math.cos(angle) + Math.cos(phi) * Math.sin(angle) * Math.cos(bearing);
  const turned = Math.atan2(Math.sin(bearing) * Math.sin(angle) * Math.cos(phi), Math.cos(angle) - Math.sin(phi) * sin);
  return [lon + turned / RADIANS, Math.asin(Math.max(-1, Math.min(1, sin))) / RADIANS];
}

/** The line with the hand that held it at `from` now at `to`. */
export function moveLine(line: SectionLine, from: LonLat, to: LonLat): SectionLine {
  const { angle, bearing } = course(line.a, line.b);
  const a: LonLat = [line.a[0] + to[0] - from[0], Math.max(-LIMIT, Math.min(LIMIT, line.a[1] + to[1] - from[1]))];
  return { a, b: along(a, angle, bearing) };
}
