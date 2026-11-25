import { describe, expect, it } from "vitest";

import type { SectionLine } from "../../../web/src/crosssection/types";
import { distance } from "../../../web/src/measure";
import { moveLine } from "../../../web/src/workbench/moveline";

const LINE: SectionLine = { a: [116.4, 39.9], b: [139.7, 35.7] };

describe("a line moved whole", () => {
  it("is where it was when nothing moved", () => {
    const { a, b } = moveLine(LINE, [120, 30], [120, 30]);
    expect(a[0]).toBeCloseTo(LINE.a[0], 8);
    expect(a[1]).toBeCloseTo(LINE.a[1], 8);
    expect(b[0]).toBeCloseTo(LINE.b[0], 8);
    expect(b[1]).toBeCloseTo(LINE.b[1], 8);
  });

  it("keeps its length", () => {
    const length = distance(LINE.a, LINE.b);
    for (const to of [[130, 30], [120, 55], [120, -20], [-60, 10]] as [number, number][]) {
      const { a, b } = moveLine(LINE, [120, 30], to);
      expect(a[0]).toBeCloseTo(LINE.a[0] + to[0] - 120, 8);
      // The ellipsoid's length of the same angle.
      expect(Math.abs(distance(a, b) / length - 1)).toBeLessThan(0.006);
    }
  });

  it("goes on past the antimeridian and stops short of a pole", () => {
    expect(moveLine(LINE, [120, 30], [175, 30]).b[0]).toBeGreaterThan(180);
    expect(moveLine(LINE, [120, 30], [120, 89]).a[1]).toBe(85);
  });
});
