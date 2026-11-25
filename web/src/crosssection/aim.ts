/**
 * Where the map goes when the section's panel has come to cover its line: so
 * that the line lies in the map left clear, at the zoom there is where it
 * fits and just far enough out where it does not. All of it on the screen,
 * which is the same question on the plane, on the globe and under a tilt.
 */

import type { ScreenBox } from "../probelayout";

export interface ScreenPoint {
  x: number;
  y: number;
}

/** How near the panel the line may come. */
const CLEARANCE = 24;
/** The buttons along the top, and the column of controls on the right. */
const TOP = 72;
const RIGHT = 76;
/** The least clear map, either way, worth moving a line into. */
const MIN_CLEAR = 80;

export interface LineAim {
  /** The screen point to bring to the clear map, and where, from the middle
   * of the map. */
  from: ScreenPoint;
  offset: [number, number];
  /** What the line is to be drawn at, of its size now: 1 or less. */
  scale: number;
}

/**
 * The move for a line at `points`, or null where the panel does not cover any
 * of it (a line off the side of the screen is where the reader left it) or
 * there is too little clear map. The line keeps its place across as far as it
 * can, and goes to the middle of the height. `slack` is the share of the room
 * taken where the move is not exact.
 */
export function lineAim(points: readonly ScreenPoint[], map: ScreenBox, panel: ScreenBox, slack = 1): LineAim | null {
  const covered = points.some(
    ({ x, y }) => x > panel.left - CLEARANCE && x < panel.right + CLEARANCE && y > panel.top - CLEARANCE,
  );
  if (!covered) return null;
  const clear = {
    left: map.left + CLEARANCE,
    right: map.right - RIGHT,
    top: map.top + TOP,
    bottom: panel.top - CLEARANCE,
  };
  const room = { width: clear.right - clear.left, height: clear.bottom - clear.top };
  if (room.width < MIN_CLEAR || room.height < MIN_CLEAR) return null;
  const xs = points.map(({ x }) => x);
  const ys = points.map(({ y }) => y);
  const left = Math.min(...xs);
  const right = Math.max(...xs);
  const top = Math.min(...ys);
  const bottom = Math.max(...ys);
  const scale = Math.min(1, (room.width * slack) / Math.max(1, right - left), (room.height * slack) / Math.max(1, bottom - top));
  const half = ((right - left) * scale) / 2;
  const from = { x: (left + right) / 2, y: (top + bottom) / 2 };
  const to = {
    x: Math.min(clear.right - half, Math.max(clear.left + half, from.x)),
    y: (clear.top + clear.bottom) / 2,
  };
  return { from, offset: [to.x - (map.left + map.right) / 2, to.y - (map.top + map.bottom) / 2], scale };
}
