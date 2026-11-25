/**
 * Where things are on the section's chart, for the canvas that draws the field
 * and for the axes, the crosshair and the pointer around it. The plot is the
 * whole canvas: pressure runs from `P_BOTTOM` at its lower edge to the chart's
 * top at its upper one, logarithmically, and the plan's points are evenly
 * spaced from its left edge to its right.
 */

/** hPa at the lower edge: below the highest sea-level pressure there is. */
export const P_BOTTOM = 1050;

/** Rows of the drawing grid, evenly spaced in ln p. */
export const SECTION_ROWS = 180;

/** Pixels from the top edge of a pressure, hPa. */
export function pressureY(pressure: number, top: number, height: number): number {
  return (Math.log(pressure / top) / Math.log(P_BOTTOM / top)) * height;
}

/** The pressure a number of pixels under the top edge, hPa. */
export function yPressure(y: number, top: number, height: number): number {
  return top * Math.pow(P_BOTTOM / top, y / height);
}

/** Pixels from the left edge of a point of the plan. */
export function pointX(point: number, points: number, width: number): number {
  return points < 2 ? 0 : (point / (points - 1)) * width;
}

/** The point of the plan nearest a number of pixels from the left edge. */
export function xPoint(x: number, points: number, width: number): number {
  return Math.max(0, Math.min(points - 1, Math.round((x / width) * (points - 1))));
}

/** The pressures an axis names, in the order they are given up when there is
 * no room for all of them: the ones a forecaster names first go last. */
const AXIS_PRIORITY = [500, 850, 200, 300, 700, 1000, 100, 925, 400, 600, 250, 150, 800, 950, 900, 975];

/** The pressures to label under a top, descending, no two of them closer than
 * `spacing` pixels. */
export function axisPressures(top: number, height: number, spacing = 15): number[] {
  const kept: number[] = top < P_BOTTOM ? [top] : [];
  for (const pressure of AXIS_PRIORITY) {
    if (pressure <= top) continue;
    const y = pressureY(pressure, top, height);
    if (kept.every((other) => Math.abs(pressureY(other, top, height) - y) >= spacing)) kept.push(pressure);
  }
  return kept.sort((a, b) => b - a);
}
