import { describe, expect, it } from "vitest";

import { CONTOUR_MAX_ZOOM, CONTOUR_MIN_ZOOM, CONTOUR_THRESHOLDS, contourInk } from "../../web/src/terraincontours";

describe("contour thresholds", () => {
  it("print a major line every fifth minor one, finer as the map zooms in", () => {
    const zooms = Object.keys(CONTOUR_THRESHOLDS).map(Number).sort((a, b) => a - b);
    expect(zooms[0]).toBe(CONTOUR_MIN_ZOOM);
    expect(zooms[zooms.length - 1]).toBeLessThanOrEqual(CONTOUR_MAX_ZOOM);
    let previous = Infinity;
    for (const zoom of zooms) {
      const [minor, major] = CONTOUR_THRESHOLDS[zoom]!;
      expect(major / minor).toBe(5);
      expect(minor).toBeLessThan(previous);
      previous = minor;
    }
  });
});

describe("contour ink", () => {
  it("is a translucent brown on paper and a pale sand on the dark ground", () => {
    expect(contourInk(false).line).toMatch(/^rgba\(120, 82, 40, /);
    expect(contourInk(true).line).toMatch(/^rgba\(255, 226, 180, /);
    expect(contourInk(false).halo).not.toBe(contourInk(true).halo);
  });
});
