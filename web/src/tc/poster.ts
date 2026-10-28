/** A season of tropical cyclones as a picture: every storm's track from the
 * archive's overview (`docs/tc.md` §8.3) drawn on a transparent canvas in
 * the Saffir–Simpson colors of the storm map, for the showcase card. No
 * basemap, as the cases' posters have none: the tracks are the picture.
 *
 * The projection is equirectangular around the Pacific — a western
 * longitude is taken past 180°, so the Atlantic sits right of the eastern
 * Pacific and a storm across the antimeridian stays whole — fitted to the
 * tracks with a margin. A season spans three times as many degrees of
 * longitude as of latitude, a thin band in a 16:9 tile, so latitude is
 * stretched up to `MAX_STRETCH` times: the picture is of the season, not
 * a map. */

import { SSHS_CLASSES } from "./intensity";
import type { SeasonOverview } from "./archive";

const MARGIN = 0.06;
const MAX_STRETCH = 2.2;
const UNKNOWN = "#9a9a9a";

function pacificLongitude(tenths: number): number {
  const lon = tenths / 10;
  return lon < 0 ? lon + 360 : lon;
}

/** The fitted projection, or null for an overview with no point. */
export function seasonProjection(
  overview: SeasonOverview,
  width: number,
  height: number,
): ((lon: number, lat: number) => [number, number]) | null {
  let west = Infinity;
  let east = -Infinity;
  let south = Infinity;
  let north = -Infinity;
  for (const storm of overview.storms)
    for (const [lon, lat] of storm.track) {
      const x = pacificLongitude(lon);
      west = Math.min(west, x);
      east = Math.max(east, x);
      south = Math.min(south, lat / 10);
      north = Math.max(north, lat / 10);
    }
  if (!Number.isFinite(west)) return null;
  const spanX = Math.max(east - west, 1);
  const spanY = Math.max(north - south, 1);
  const fitX = (width * (1 - 2 * MARGIN)) / spanX;
  const fitY = (height * (1 - 2 * MARGIN)) / spanY;
  const scaleX = Math.min(fitX, fitY * MAX_STRETCH);
  const scaleY = Math.min(fitY, scaleX * MAX_STRETCH);
  const offsetX = (width - spanX * scaleX) / 2;
  const offsetY = (height - spanY * scaleY) / 2;
  return (lon, lat) => [offsetX + (pacificLongitude(lon) - west) * scaleX, offsetY + (north - lat / 10) * scaleY];
}

/** Paint the season onto `canvas` at its own pixel size; false when the
 * overview holds nothing to draw. */
export function paintSeason(canvas: HTMLCanvasElement, overview: SeasonOverview): boolean {
  const context = canvas.getContext("2d");
  if (!context) return false;
  const project = seasonProjection(overview, canvas.width, canvas.height);
  if (!project) return false;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.lineCap = "round";
  context.lineJoin = "round";
  context.lineWidth = Math.max(1.6, canvas.width / 300);
  // Weak storms first, so the strong segments are drawn over them.
  const segments: { from: [number, number]; to: [number, number]; grade: number }[] = [];
  for (const storm of overview.storms)
    for (let index = 0; index + 1 < storm.track.length; index += 1) {
      const [lonA, latA, grade] = storm.track[index]!;
      const [lonB, latB] = storm.track[index + 1]!;
      // A storm crossing the seam between the Atlantic and Africa would
      // draw across the picture: such a step is not drawn.
      if (Math.abs(pacificLongitude(lonA) - pacificLongitude(lonB)) > 30) continue;
      segments.push({ from: project(lonA, latA), to: project(lonB, latB), grade });
    }
  segments.sort((a, b) => a.grade - b.grade);
  for (const { from, to, grade } of segments) {
    context.strokeStyle = SSHS_CLASSES[grade]?.color ?? UNKNOWN;
    context.beginPath();
    context.moveTo(from[0], from[1]);
    context.lineTo(to[0], to[1]);
    context.stroke();
  }
  return true;
}
