import { describe, expect, it } from "vitest";

import { gridMapFor, parseGrid } from "../../../web/src/gridmap";
import type { SectionGrid } from "../../../web/src/crosssection/line";
import { planPoint } from "../../../web/src/workbench/plan";
import { globalGrid } from "../helpers";

function store(): SectionGrid {
  const grid = parseGrid(globalGrid().grid);
  return { grid: gridMapFor(grid), width: grid.width, height: grid.height, blockHeight: 32, blockWidth: 32 };
}

describe("the plan of a point", () => {
  it("is one sample on the nearest cell", () => {
    const on = store();
    const plan = planPoint([139.69, 35.68], on);
    expect(plan.points).toHaveLength(1);
    expect(plan.cells).toHaveLength(2);
    expect([...plan.weights]).toEqual([1, 0, 0, 0]);
    expect(plan.blocks).toBe(1);
    const { lon, lat } = plan.points[0]!;
    expect(((lon % 360) + 360) % 360).toBeCloseTo(139.75, 6);
    expect(lat).toBeCloseTo(35.75, 6);
    const [row, column] = plan.cells;
    const [cellLon, cellLat] = on.grid.cellToLonLat(column!, row!);
    expect([cellLon, cellLat]).toEqual([lon, lat]);
  });

  it("goes round the antimeridian and stays on the poles", () => {
    const on = store();
    for (const place of [[179.95, 10], [-179.95, 10], [0, 90], [12, -90], [-0.05, 0]] as [number, number][]) {
      const plan = planPoint(place, on);
      expect(plan.cells[0]).toBeGreaterThanOrEqual(0);
      expect(plan.cells[0]).toBeLessThan(on.height);
      expect(plan.cells[1]).toBeLessThan(on.width);
      const turn = ((((plan.points[0]!.lon - place[0] + 180) % 360) + 360) % 360) - 180;
      expect(Math.abs(turn)).toBeLessThanOrEqual(0.126);
    }
  });
});
