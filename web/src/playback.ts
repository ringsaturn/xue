/** Playback pacing: the animation's frame rate ladder, the per-frame dwell a
 * mixed-step axis needs, and the speed a given time axis should open at. Kept
 * apart from main.ts so it is testable without a DOM. */

/** Selectable frame rates, ascending — the transport's speed button cycles
 * through them in this order. */
export const FPS_LADDER = [3, 6, 12, 24] as const;

export type PlaybackFps = (typeof FPS_LADDER)[number];

/** The rate the format is designed around: a 240-hour run (161 frames) loops
 * in about 13 seconds. */
export const DEFAULT_FPS: PlaybackFps = 12;

/** Shortest a full loop may last before the opening speed steps down a rung. */
const MIN_LOOP_SECONDS = 4;

/** How long each frame holds, relative to the frame rate's own interval.
 *
 * Forecast axes are mixed-step — GFS and sflux turn 3-hourly after f120,
 * ECMWF 6-hourly after f144 — so a constant frame rate makes forecast time
 * lurch to triple speed halfway through the loop. Pacing the selected rate on
 * the axis's shortest step and holding longer steps proportionally longer
 * keeps the weather moving at one apparent speed from f000 to f240.
 *
 * A frame's dwell is the step leading out of it (that is the transit the
 * blend between it and the next frame renders); the last frame inherits the
 * step leading into it, since the loop seam is a restart, not a transition.
 * A uniform axis yields all ones, i.e. exactly the selected frame rate. */
export function frameDwellScales(hours: readonly number[]): number[] {
  if (hours.length === 0) return [];
  const steps = hours.map((hour, index) =>
    index + 1 < hours.length ? (hours[index + 1] ?? hour) - hour : 0,
  );
  if (steps.length > 1) steps[steps.length - 1] = steps[steps.length - 2] ?? 0;
  const positive = steps.filter((step) => step > 0);
  // A degenerate axis (one frame, or repeated hours) simply plays flat.
  if (positive.length === 0) return steps.map(() => 1);
  const base = Math.min(...positive);
  return steps.map((step) => (step > 0 ? step / base : 1));
}

/** A loop's length in frame-interval units: the dwell scales summed, so a
 * uniform axis counts its frames and a mixed-step one counts what it really
 * costs (a 240-hour GFS run is 161 frames but 240 units). */
export function loopDwellUnits(scales: readonly number[]): number {
  return scales.reduce((total, scale) => total + scale, 0);
}

/** Speed a dataset opens at, from the loop length `loopDwellUnits` reports.
 * The live feed runs 20 seconds at 12 fps, but a showcase case can be two
 * dozen frames — the whole event flashes past in two seconds. Take the
 * fastest rung whose loop still lasts long enough to watch, never above the
 * default (the ceiling for anything long enough to need no help) and never
 * below the slowest rung. */
export function defaultFpsForLoop(dwellUnits: number): PlaybackFps {
  let choice: PlaybackFps = FPS_LADDER[0];
  for (const fps of FPS_LADDER) {
    if (fps > DEFAULT_FPS) break;
    if (dwellUnits / fps >= MIN_LOOP_SECONDS) choice = fps;
  }
  return choice;
}

/** Next rung of the ladder, wrapping at the top. An unknown rate (a stale
 * stored preference) restarts at the slowest. */
export function nextFps(current: number): PlaybackFps {
  const index = FPS_LADDER.indexOf(current as PlaybackFps);
  return FPS_LADDER[(index + 1) % FPS_LADDER.length] ?? FPS_LADDER[0];
}

/** A stored speed preference, or null when absent or off the ladder. */
export function parseStoredFps(value: string | null): PlaybackFps | null {
  const fps = Number(value);
  return FPS_LADDER.includes(fps as PlaybackFps) ? (fps as PlaybackFps) : null;
}
