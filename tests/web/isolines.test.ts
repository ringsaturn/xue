import { describe, expect, it } from "vitest";

import {
  computeLabels,
  fieldToGeo,
  findCenters,
  reduceField,
  smoothField,
  strideFor,
  traceContours,
  type Field,
} from "../../web/src/isolines";
import type { GeoGrid } from "../../web/src/probe";
import { WHOLE_PLANE_COVERAGE } from "../../web/src/tiles";

/** A small global grid: 36 columns of ten degrees, 19 rows pole to pole. */
const GRID: GeoGrid = {
  width: 36,
  height: 19,
  firstLongitude: -180,
  firstLatitude: 90,
  longitudeStep: 10,
  latitudeStep: -10,
  wraps: true,
};

function planeOf(grid: GeoGrid, code: (column: number, row: number) => number): Uint8Array {
  const plane = new Uint8Array(grid.width * grid.height);
  for (let row = 0; row < grid.height; row += 1) {
    for (let column = 0; column < grid.width; column += 1) {
      plane[row * grid.width + column] = Math.max(0, Math.min(255, Math.round(code(column, row))));
    }
  }
  return plane;
}

function fieldOf(columns: number, rows: number, value: (x: number, y: number) => number, wraps = false): Field {
  const values = new Float32Array(columns * rows);
  for (let y = 0; y < rows; y += 1) {
    for (let x = 0; x < columns; x += 1) values[y * columns + x] = value(x, y);
  }
  return { columns, rows, values, column0: 0, row0: 0, stride: 1, wraps };
}

describe("strideFor", () => {
  it("leaves a regional view at full resolution and reduces a whole world", () => {
    expect(strideFor(50_000)).toBe(1);
    expect(strideFor(1440 * 721)).toBe(3);
  });
});

describe("reduceField", () => {
  it("averages each block and wraps columns on a global grid", () => {
    // Code equals the column, so a block's mean is the mean of its columns.
    const plane = planeOf(GRID, (column) => column);
    const field = reduceField(plane, GRID, { column0: 34, row0: 0, columns: 6, rows: 4 }, WHOLE_PLANE_COVERAGE, 2);
    expect(field.columns).toBe(3);
    expect(field.rows).toBe(2);
    expect(field.values[0]).toBeCloseTo(34.5);
    // Columns 36 and 37 are columns 0 and 1 again.
    expect(field.values[1]).toBeCloseTo(0.5);
    expect(field.values[2]).toBeCloseTo(2.5);
  });

  it("leaves uncovered blocks NaN and keeps them out of a partial block", () => {
    const plane = planeOf(GRID, () => 100);
    // Only the eastern half of the plane is covered.
    const coverage = { uStart: 0.5, uEnd: 1, vStart: 0, vEnd: 1 };
    const field = reduceField(plane, GRID, { column0: 16, row0: 0, columns: 6, rows: 2 }, coverage, 2);
    expect(field.values[0]).toBeNaN();
    expect(field.values[1]).toBe(100);
    expect(field.values[2]).toBe(100);
  });
});

describe("smoothField", () => {
  it("keeps a flat field flat and spreads a step", () => {
    const flat = fieldOf(8, 4, () => 50);
    smoothField(flat, 1);
    for (const value of flat.values) expect(value).toBeCloseTo(50);
    const step = fieldOf(8, 4, (x) => (x < 4 ? 0 : 100));
    smoothField(step, 1);
    expect(step.values[3]!).toBeGreaterThan(0);
    expect(step.values[4]!).toBeLessThan(100);
    expect(step.values[0]!).toBeCloseTo(0, 3);
  });

  it("renormalises around a NaN hole instead of poisoning its neighbours", () => {
    const field = fieldOf(8, 4, (x, y) => (x === 4 && y === 2 ? Number.NaN : 50));
    smoothField(field, 1);
    expect(field.values[2 * 8 + 4]).toBeNaN();
    expect(field.values[2 * 8 + 3]).toBeCloseTo(50);
  });
});

describe("traceContours", () => {
  it("draws one straight line per level across a ramp and joins it up", () => {
    // Value climbs 10 per column: the contours at 25, 35, ... are vertical
    // lines at x = 2.5, 3.5, ...
    const field = fieldOf(8, 5, (x) => x * 10);
    const lines = traceContours(field, 10, 5);
    expect(lines.map((line) => line.level).sort((a, b) => a - b)).toEqual([5, 15, 25, 35, 45, 55, 65]);
    for (const line of lines) {
      // One polyline spanning every row, not four loose segments.
      expect(line.points.length / 2).toBe(5);
      const expectedX = line.level / 10;
      for (let position = 0; position < line.points.length; position += 2) {
        expect(line.points[position]).toBeCloseTo(expectedX);
      }
      expect(line.closed).toBe(false);
    }
  });

  it("closes a loop around a bump", () => {
    const field = fieldOf(11, 11, (x, y) => 100 - 4 * Math.hypot(x - 5, y - 5));
    const lines = traceContours(field, 100, 90);
    expect(lines).toHaveLength(1);
    const [ring] = lines;
    expect(ring!.closed).toBe(true);
    // Every point sits about 2.5 blocks from the center.
    for (let position = 0; position < ring!.points.length; position += 2) {
      const distance = Math.hypot(ring!.points[position]! - 5, ring!.points[position + 1]! - 5);
      expect(distance).toBeGreaterThan(2);
      expect(distance).toBeLessThan(3);
    }
  });

  it("skips cells touching a NaN block", () => {
    const field = fieldOf(8, 5, (x, y) => (y === 2 ? Number.NaN : x * 10));
    const lines = traceContours(field, 10, 5);
    // The NaN row cuts each line in two.
    const at25 = lines.filter((line) => line.level === 25);
    expect(at25).toHaveLength(2);
  });

  it("carries a line across the antimeridian of a wrapping field", () => {
    // A ramp in y, so contours are horizontal and run right round the
    // world: a wrapping field yields one closed ring per level.
    const field = fieldOf(12, 6, (_x, y) => y * 10, true);
    const lines = traceContours(field, 10, 5);
    for (const line of lines) {
      expect(line.closed).toBe(true);
      expect(line.points.length / 2).toBeGreaterThanOrEqual(13);
    }
  });
});

describe("findCenters", () => {
  it("marks a high and a low once each and ignores a slope", () => {
    const field = fieldOf(
      30,
      12,
      (x, y) => 100 + 30 * Math.exp(-(((x - 8) ** 2 + (y - 6) ** 2) / 8)) - 30 * Math.exp(-(((x - 22) ** 2 + (y - 6) ** 2) / 8)),
    );
    const centers = findCenters(field, 3, 5);
    expect(centers).toHaveLength(2);
    const high = centers.find((center) => center.kind === "high")!;
    const low = centers.find((center) => center.kind === "low")!;
    expect([high.x, high.y]).toEqual([8, 6]);
    expect([low.x, low.y]).toEqual([22, 6]);
  });

  it("drops a bump that does not stand out by the prominence asked for", () => {
    const field = fieldOf(20, 10, (x, y) => 100 + 2 * Math.exp(-(((x - 10) ** 2 + (y - 5) ** 2) / 8)));
    expect(findCenters(field, 3, 5)).toHaveLength(0);
    expect(findCenters(field, 3, 1)).toHaveLength(1);
  });

  it("never marks a block whose window runs into unknown data", () => {
    const field = fieldOf(20, 10, (x, y) => (x > 13 ? Number.NaN : 100 + 30 * Math.exp(-(((x - 12) ** 2 + (y - 5) ** 2) / 8))));
    expect(findCenters(field, 3, 5)).toHaveLength(0);
  });
});

describe("fieldToGeo", () => {
  it("puts a block's center on the middle of the cells it spans", () => {
    const field: Field = { columns: 1, rows: 1, values: new Float32Array(1), column0: 4, row0: 2, stride: 2, wraps: false };
    // Cells 4 and 5 have centers at -140 and -130; the block sits between.
    expect(fieldToGeo(field, GRID, 0, 0)).toEqual([-135, 65]);
  });
});

describe("computeLabels", () => {
  it("returns lines in degrees at the codebook's contour values and the centers found", () => {
    // Codes that read as 1000 + row hPa under offset 970.5 / scale 1 (every
    // contour half a code off, as the registry rule wants), with a 12 hPa
    // high on the equator that out-climbs the ramp within its window.
    const offset = 970.5;
    const plane = planeOf(GRID, (column, row) => {
      const bump = 12 * Math.exp(-(((column - 18) ** 2 + (row - 9) ** 2) / 6));
      return 1000 + row + bump - offset;
    });
    const result = computeLabels(plane, {
      grid: GRID,
      window: { column0: 0, row0: 0, columns: 36, rows: 19 },
      coverage: WHOLE_PLANE_COVERAGE,
      stride: 1,
      smoothingCells: 0,
      offset,
      scale: 1,
      interval: 4,
      centerRadiusDegrees: 30,
      prominence: 2,
    });
    const values = new Set(result.lines.map((line) => line.value));
    for (const value of values) expect(value % 4).toBe(0);
    expect(values.size).toBeGreaterThanOrEqual(4);
    for (const line of result.lines) {
      for (const [longitude, latitude] of line.coordinates) {
        expect(Number.isFinite(longitude)).toBe(true);
        expect(Math.abs(latitude)).toBeLessThanOrEqual(90);
      }
    }
    const highs = result.centers.filter((center) => center.kind === "high");
    expect(highs).toHaveLength(1);
    expect(highs[0]!.longitude).toBe(0);
    expect(highs[0]!.latitude).toBe(0);
    expect(highs[0]!.value).toBeGreaterThanOrEqual(1020);
    expect(highs[0]!.value).toBeLessThanOrEqual(1022);
  });
});
