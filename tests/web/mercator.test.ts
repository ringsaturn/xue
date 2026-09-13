import { describe, expect, it } from "vitest";

import { zoomCeilingForStep, zoomForSpan } from "../../web/src/mercator";

describe("zoomCeilingForStep", () => {
  it("raises the ceiling with the grid and never lowers it under the floor", () => {
    // The ceiling is where one cell spans the given width on screen: a
    // 0.02° radar mosaic earns two more zoom levels than the 0.25° models,
    // which sit under the floor and keep it.
    expect(zoomCeilingForStep(0.25, 16, 7)).toBe(7);
    expect(zoomCeilingForStep(0.02, 16, 7)).toBeCloseTo(9.14, 2);
    expect(zoomCeilingForStep(0.03, 16, 7)).toBeCloseTo(8.55, 2);
    // At the ceiling a cell is exactly the asked-for width.
    const ceiling = zoomCeilingForStep(0.02, 16, 7);
    expect(zoomForSpan(0.02 / 360, 16)).toBeCloseTo(ceiling, 9);
  });

  it("falls back to the floor on a grid with no usable step", () => {
    expect(zoomCeilingForStep(0, 16, 7)).toBe(7);
    expect(zoomCeilingForStep(Number.NaN, 16, 7)).toBe(7);
  });
});
