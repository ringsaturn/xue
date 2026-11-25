/** The line of a section: what is refused, the sampling plan, the limit. */
import { describe, expect, it } from "vitest";

import {
  clampLine,
  MAX_BLOCKS,
  MAX_POINTS,
  planSection,
  validateLine,
  type SectionGrid,
} from "../../web/src/crosssection/line";
import type { LonLat, SectionPlan } from "../../web/src/crosssection/types";
import { gridMapFor, parseGrid } from "../../web/src/gridmap";
import { distance } from "../../web/src/measure";

function gridOf(block: Record<string, unknown>): SectionGrid {
  const grid = parseGrid(block);
  return { grid: gridMapFor(grid), width: grid.width, height: grid.height, blockHeight: 8, blockWidth: 8 };
}

/** The GFS quarter degree grid. */
const GLOBAL = gridOf({ width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25 });
const GAUSSIAN = gridOf({
  width: 192,
  height: 96,
  firstLongitude: -180,
  firstLatitude: 88.6,
  longitudeStep: 1.875,
  latitudeStep: -1.865,
  gaussian: { n: 48, firstRow: 0, rowStride: 1 },
});

const BEIJING: LonLat = [116.4, 39.9];
const TOKYO: LonLat = [139.7, 35.7];

/** The sum a point makes of a field given by the cells' coordinates. */
function blend(plan: SectionPlan, grid: SectionGrid, point: number, field: (lon: number, lat: number) => number): number {
  let sum = 0;
  for (let corner = point * 4; corner < point * 4 + 4; corner += 1) {
    const cell = plan.corners[corner]!;
    const [lon, lat] = grid.grid.cellToLonLat(plan.cells[cell * 2 + 1]!, plan.cells[cell * 2]!);
    sum += plan.weights[corner]! * field(lon, lat);
  }
  return sum;
}

/** The plan samples the place it says: the latitude exactly, the longitude by
 * its sine and cosine, which know no antimeridian. */
function expectSampled(plan: SectionPlan, grid: SectionGrid): void {
  for (const [index, { lon, lat }] of plan.points.entries()) {
    expect(blend(plan, grid, index, () => 1)).toBeCloseTo(1, 5);
    expect(blend(plan, grid, index, (_, value) => value)).toBeCloseTo(lat, 4);
    expect(blend(plan, grid, index, (value) => Math.sin((value * Math.PI) / 180))).toBeCloseTo(Math.sin((lon * Math.PI) / 180), 3);
    expect(blend(plan, grid, index, (value) => Math.cos((value * Math.PI) / 180))).toBeCloseTo(Math.cos((lon * Math.PI) / 180), 3);
  }
}

describe("a line", () => {
  it("is refused where there is no one great circle, or next to none of it", () => {
    expect(validateLine({ a: BEIJING, b: TOKYO })).toBeNull();
    expect(validateLine({ a: BEIJING, b: BEIJING })).toBe("coincident");
    expect(validateLine({ a: [30, 90], b: [120, 90] })).toBe("coincident");
    expect(validateLine({ a: [180, 10], b: [-180, 10] })).toBe("coincident");
    expect(validateLine({ a: [10, 40], b: [-170, -40] })).toBe("antipodal");
    expect(validateLine({ a: [10, 40], b: [190.5, -40] })).toBe("antipodal");
    expect(validateLine({ a: [0, 0], b: [1, 0] })).toBe("short");
    expect(validateLine({ a: [178, 0], b: [-178, 0] })).toBeNull();
  });
});

describe("a section's plan", () => {
  it("samples the line about half a cell apart", () => {
    const plan = planSection({ a: BEIJING, b: TOKYO }, GLOBAL);
    expect(plan.length).toBeCloseTo(distance(BEIJING, TOKYO), 3);
    expect(plan.points[0]).toMatchObject({ lon: BEIJING[0], lat: BEIJING[1], along: 0 });
    expect(plan.points.at(-1)!.along).toBeCloseTo(plan.length, 3);
    expect(plan.points.at(-1)!.lat).toBe(TOKYO[1]);
    const step = plan.length / (plan.points.length - 1);
    expect(step).toBeGreaterThan(13_000);
    expect(step).toBeLessThan(14_000);
    expect(plan.corners).toHaveLength(plan.points.length * 4);
    expectSampled(plan, GLOBAL);
    // No cell is listed twice.
    const keys = Array.from({ length: plan.cells.length / 2 }, (_, cell) => plan.cells[cell * 2]! * 1440 + plan.cells[cell * 2 + 1]!);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("is as many points as it is told, over the same line", () => {
    const plan = planSection({ a: BEIJING, b: TOKYO }, GLOBAL, 10);
    expect(plan.points).toHaveLength(10);
    expectSampled(plan, GLOBAL);
    expect(planSection({ a: [0, 0], b: [170, 5] }, GLOBAL).points).toHaveLength(MAX_POINTS);
  });

  it("counts the blocks it reads", () => {
    expect(planSection({ a: [140, 10], b: [140, 60] }, GLOBAL).blocks).toBe(26);
    expect(planSection({ a: BEIJING, b: TOKYO }, GLOBAL).blocks).toBe(16);
    expect(planSection({ a: [87.6, 43.8], b: [121.5, 31.2] }, GLOBAL).blocks).toBe(27);
  });

  it("reads a line along a row or a column off that one alone", () => {
    const plan = planSection({ a: [140, 10], b: [140, 60] }, GLOBAL);
    expect(new Set(plan.cells.filter((_, index) => index % 2 === 1))).toEqual(new Set([1280]));
    expect(plan.cells).toHaveLength(201 * 2);
  });

  it("knows which way the line runs", () => {
    const north = planSection({ a: [140, 10], b: [140, 60] }, GLOBAL);
    for (const { bearing } of north.points) expect(Math.sin(bearing)).toBeCloseTo(0, 9);
    const { points } = planSection({ a: [170, 30], b: [-165, 45] }, GLOBAL);
    for (let index = 1; index < points.length - 1; index += 1) {
      const [before, at, after] = [points[index - 1]!, points[index]!, points[index + 1]!];
      const east = (after.lon - before.lon) * Math.cos((at.lat * Math.PI) / 180);
      expect(Math.atan2(east, after.lat - before.lat)).toBeCloseTo(at.bearing, 4);
    }
  });

  it("goes over the antimeridian, whichever way its end is written", () => {
    const plan = planSection({ a: [170, 30], b: [-165, 45] }, GLOBAL);
    expect(plan.points.at(-1)!.lon).toBe(195);
    expectSampled(plan, GLOBAL);
    const other = planSection({ a: [170, 30], b: [195, 45] }, GLOBAL);
    expect(other.cells).toEqual(plan.cells);
    expect(other.blocks).toBe(plan.blocks);
    // Between the last column and the first.
    const across = planSection({ a: [179.9, 10], b: [179.9, 40] }, GLOBAL);
    expect(new Set(across.cells.filter((_, index) => index % 2 === 1))).toEqual(new Set([1439, 0]));
  });

  it("goes over a pole", () => {
    const plan = planSection({ a: [10, 80], b: [-170, 80] }, GLOBAL);
    // To within the spacing of the points, which need not fall on the pole.
    const highest = Math.max(...plan.points.map((point) => point.lat));
    expect(highest).toBeGreaterThan(90 - 0.125);
    expect(highest).toBeLessThanOrEqual(90);
    // Up one meridian and down the one opposite.
    expect(new Set(plan.points.map(({ lon }) => Math.round(Math.cos(((lon - 10) * Math.PI) / 180))))).toEqual(new Set([1, -1]));
    expect(Math.max(...plan.cells.filter((_, index) => index % 2 === 0))).toBe(40);
    for (const { bearing } of plan.points) expect(Number.isFinite(bearing)).toBe(true);
    expect(Math.cos(plan.points[0]!.bearing)).toBeCloseTo(1, 9);
    expect(Math.cos(plan.points.at(-1)!.bearing)).toBeCloseTo(-1, 9);
    for (let index = 0; index < plan.points.length; index += 1) {
      expect(blend(plan, GLOBAL, index, (_, lat) => lat)).toBeCloseTo(plan.points[index]!.lat, 4);
    }
  });

  it("reads Gaussian rows where they are", () => {
    const plan = planSection({ a: [100, -30], b: [140, 45] }, GAUSSIAN);
    expectSampled(plan, GAUSSIAN);
    // North of the first row there is only the first row.
    const polar = planSection({ a: [10, 80], b: [-170, 80] }, GAUSSIAN);
    const top = polar.points.findIndex((point) => point.lat > 89);
    expect(blend(polar, GAUSSIAN, top, () => 1)).toBeCloseTo(1, 5);
  });

  it("gives a point off the grid no weight", () => {
    const cropped = gridOf({ width: 80, height: 80, firstLongitude: 110, firstLatitude: 50, longitudeStep: 0.25, latitudeStep: -0.25, wrapLongitude: false });
    const plan = planSection({ a: BEIJING, b: TOKYO }, cropped);
    expect(blend(plan, cropped, 0, () => 1)).toBeCloseTo(1, 5);
    expect([...plan.weights.slice(-4)]).toEqual([0, 0, 0, 0]);
    expect(Math.max(...plan.cells)).toBeLessThan(80);
  });
});

describe("the limit on a line", () => {
  it("leaves a line within it as it is", () => {
    const line = { a: BEIJING, b: TOKYO };
    expect(clampLine(line, GLOBAL)).toEqual({ line, clamped: false });
  });

  it("cuts a longer one on its own great circle, as near the limit as it can", () => {
    for (const [a, b] of [
      [[100, 20], [150, 55]],
      [[0, 80], [170, 75]],
      [[170, 10], [-120, 40]],
      [[0, 0], [170, 5]],
    ] as [LonLat, LonLat][]) {
      const cut = clampLine({ a, b }, GLOBAL);
      expect(cut.clamped).toBe(true);
      expect(cut.line.a).toEqual(a);
      const { blocks, length } = planSection(cut.line, GLOBAL);
      expect(blocks).toBeLessThanOrEqual(MAX_BLOCKS);
      expect(blocks).toBeGreaterThan(MAX_BLOCKS - 4);
      // On the way to `b`.
      expect((distance(a, cut.line.b) + distance(cut.line.b, b)) / distance(a, b)).toBeLessThan(1.005);
      expect(length).toBeLessThan(distance(a, b));
      // Cutting a line that has been cut changes nothing.
      expect(clampLine(cut.line, GLOBAL).clamped).toBe(false);
    }
  });

  it("cuts at the limit it is given", () => {
    const cut = clampLine({ a: BEIJING, b: TOKYO }, GLOBAL, 5);
    expect(cut.clamped).toBe(true);
    expect(planSection(cut.line, GLOBAL).blocks).toBe(5);
  });
});
