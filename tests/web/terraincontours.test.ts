import { describe, expect, it } from "vitest";

import { CONTOUR_MAX_ZOOM, CONTOUR_MIN_ZOOM, CONTOUR_THRESHOLDS, contourInk } from "../../web/src/terraincontours";

describe("contour thresholds", () => {
  it("add a finer tier each rung under the lines the rung before drew", () => {
    const zooms = Object.keys(CONTOUR_THRESHOLDS).map(Number).sort((a, b) => a - b);
    expect(zooms[0]).toBe(CONTOUR_MIN_ZOOM);
    expect(zooms[zooms.length - 1]).toBeLessThanOrEqual(CONTOUR_MAX_ZOOM);
    let previous: [number, number, number] | null = null;
    for (const zoom of zooms) {
      const tiers = CONTOUR_THRESHOLDS[zoom]!;
      expect(tiers).toHaveLength(3);
      const [fine, middle, index] = tiers;
      expect(index % middle).toBe(0);
      expect(middle % fine).toBe(0);
      expect(index / middle).toBe(5);
      if (previous) {
        expect(fine).toBeLessThan(previous[0]);
        expect(middle).toBe(previous[0]);
        expect(previous[2] % index).toBe(0);
      } else {
        expect(middle).toBe(fine);
      }
      previous = tiers;
    }
  });
});

describe("contour ink", () => {
  it("is a translucent brown on paper and a pale sand on the dark ground", () => {
    expect(contourInk(false).line).toMatch(/^rgba\(70, 45, 20, /);
    expect(contourInk(true).line).toMatch(/^rgba\(255, 230, 190, /);
    expect(contourInk(false).casing).not.toBe(contourInk(false).line);
    expect(contourInk(false).halo).not.toBe(contourInk(true).halo);
  });
});
