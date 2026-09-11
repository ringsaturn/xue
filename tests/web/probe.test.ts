import { describe, expect, it } from "vitest";

import {
  normalizeLongitude,
  probeCell,
  ProbeSeries,
  probeSeriesValues,
  probeWindDirection,
} from "../../web/src/probe";
import type { BundleMetadata, BundleVariable } from "../../web/src/manifest";

/** The global quarter-degree production grid, as a bundle declares it. */
function globalGrid(overrides: Record<string, unknown> = {}): BundleMetadata {
  return {
    schemaVersion: 3,
    model: "GFS",
    runTime: "2026-08-15T06:00:00Z",
    time: { frameCount: 3, unitSeconds: 3600, firstFrameOffset: 0, frameStep: 1 },
    grid: {
      width: 1440,
      height: 721,
      firstLongitude: -180,
      firstLatitude: 90,
      longitudeStep: 0.25,
      latitudeStep: -0.25,
      wrapLongitude: true,
      ...overrides,
    },
    variables: [],
  } as unknown as BundleMetadata;
}

/** A showcase crop over the western Pacific: no wrap, and everything outside
 * it has no data. */
function croppedGrid(): BundleMetadata {
  return globalGrid({
    width: 40,
    height: 30,
    firstLongitude: 110,
    firstLatitude: 35,
    longitudeStep: 0.5,
    latitudeStep: -0.5,
    wrapLongitude: false,
  });
}

function temperature(numericId = 1): BundleVariable {
  return {
    numericId,
    id: "tmp2m",
    label: "2 meter temperature",
    unit: "°C",
    quantization: { type: "linear", offset: -60, scale: 0.5, minimumCode: 0, maximumCode: 220, nodataCode: 255 },
  } as BundleVariable;
}

function windComponent(numericId: number, id: string): BundleVariable {
  return {
    numericId,
    id,
    label: id,
    unit: "m/s",
    quantization: { type: "linear", offset: -60, scale: 0.5, minimumCode: 0, maximumCode: 240, nodataCode: 255 },
  } as BundleVariable;
}

describe("probeCell", () => {
  it("resolves a point to the nearest cell of a global grid", () => {
    const cell = probeCell(globalGrid(), 116.4, 39.9)!;
    expect(cell.column).toBe(Math.round((116.4 + 180) / 0.25));
    expect(cell.row).toBe(Math.round((39.9 - 90) / -0.25));
    expect(cell.index).toBe(cell.row * 1440 + cell.column);
    // The cell center, which is what the readout labels the value with.
    expect(cell.longitude).toBeCloseTo(116.5, 6);
    expect(cell.latitude).toBeCloseTo(40, 6);
  });

  it("wraps the antimeridian back onto column zero", () => {
    // Half a cell east of the last column's center rounds up to `width`,
    // which is column 0 again on a grid whose columns cover 360 degrees.
    const cell = probeCell(globalGrid(), 179.9, 0)!;
    expect(cell.column).toBe(0);
    const west = probeCell(globalGrid(), -180, 0)!;
    expect(west.column).toBe(0);
  });

  it("keeps the poles inside the grid", () => {
    expect(probeCell(globalGrid(), 0, 90)!.row).toBe(0);
    expect(probeCell(globalGrid(), 0, -90)!.row).toBe(720);
  });

  it("returns null outside a cropped grid, in both directions", () => {
    const grid = croppedGrid();
    // The crop runs 110°E to 129.5°E and 35°N down to 20.5°N.
    expect(probeCell(grid, 120, 25)).not.toBeNull();
    expect(probeCell(grid, 100, 25)).toBeNull();
    expect(probeCell(grid, 140, 25)).toBeNull();
    expect(probeCell(grid, 120, 40)).toBeNull();
    expect(probeCell(grid, 120, 10)).toBeNull();
  });

  it("rejects a grid with no extent", () => {
    expect(probeCell(globalGrid({ width: 0 }), 0, 0)).toBeNull();
    expect(probeCell(globalGrid({ longitudeStep: 0 }), 0, 0)).toBeNull();
  });
});

describe("normalizeLongitude", () => {
  it("wraps into [-180, 180)", () => {
    expect(normalizeLongitude(190)).toBeCloseTo(-170, 9);
    expect(normalizeLongitude(-190)).toBeCloseTo(170, 9);
    expect(normalizeLongitude(180)).toBeCloseTo(-180, 9);
    expect(normalizeLongitude(0)).toBe(0);
  });
});

describe("ProbeSeries", () => {
  it("keeps only the pinned cell's code, so the plane can be recycled", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(116.4, 39.9);
    const cell = series.cellFor(metadata)!;
    const plane = new Uint8Array(1440 * 721);
    plane[cell.index] = 170;
    expect(series.sample(metadata, "a:1", 0, plane)).toBe(true);
    plane.fill(0);
    expect(series.code("a:1", 0)).toBe(170);
    expect(series.code("a:1", 1)).toBeUndefined();
  });

  it("adopts a whole series read out of a tiled container", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(116.4, 39.9);
    expect(series.adopt(metadata, "a:1", [0, 1, 2], Uint8Array.from([10, 20, 30]))).toBe(true);
    expect([0, 1, 2].map((offset) => series.code("a:1", offset))).toEqual([10, 20, 30]);
  });

  it("declines a series that does not match the axis, or a point off the grid", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(116.4, 39.9);
    expect(series.adopt(metadata, "a:1", [0, 1, 2], Uint8Array.from([10, 20]))).toBe(false);
    expect(series.code("a:1", 0)).toBeUndefined();
    expect(new ProbeSeries(0, 0).adopt(croppedGrid(), "a:1", [0], Uint8Array.from([10]))).toBe(false);
  });

  it("declines to sample a point off the bundle's grid", () => {
    const metadata = croppedGrid();
    const series = new ProbeSeries(0, 0);
    expect(series.cellFor(metadata)).toBeNull();
    expect(series.sample(metadata, "a:1", 0, new Uint8Array(40 * 30))).toBe(false);
  });

  it("declines to sample a plane that is short of the cell", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(116.4, 39.9);
    expect(series.sample(metadata, "a:1", 0, new Uint8Array(16))).toBe(false);
  });

  it("re-resolves the cell when the grid changes", () => {
    const series = new ProbeSeries(116.4, 39.9);
    const full = series.cellFor(globalGrid())!;
    const half = series.cellFor(globalGrid({ width: 720, height: 361, longitudeStep: 0.5, latitudeStep: -0.5 }))!;
    expect(half.column).toBe(Math.round(full.column / 2));
  });

  it("clears its samples but keeps the pin", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(116.4, 39.9);
    const plane = new Uint8Array(1440 * 721).fill(150);
    series.sample(metadata, "a:1", 0, plane);
    series.clear();
    expect(series.code("a:1", 0)).toBeUndefined();
    expect(series.longitude).toBe(116.4);
    expect(series.cellFor(metadata)).not.toBeNull();
  });
});

describe("probeSeriesValues", () => {
  it("distinguishes an undecoded frame from a no-data one", () => {
    const metadata = globalGrid();
    const series = new ProbeSeries(0, 0);
    const cell = series.cellFor(metadata)!;
    const plane = new Uint8Array(1440 * 721);
    plane[cell.index] = 170;
    series.sample(metadata, "a:1", 0, plane);
    plane[cell.index] = 255; // the codebook's no-data code
    series.sample(metadata, "a:1", 2, plane);
    const values = probeSeriesValues(series, [{ key: "a:1", variable: temperature() }], [0, 1, 2]);
    expect(values[0]).toBeCloseTo(25, 9);
    expect(values[1]).toBeUndefined();
    expect(values[2]).toBeNull();
  });

  it("reads the wind pair as a speed, and only once both components landed", () => {
    const metadata = globalGrid();
    const variables = [
      { key: "a:3", variable: windComponent(3, "ugrd10m") },
      { key: "a:4", variable: windComponent(4, "vgrd10m") },
    ];
    const series = new ProbeSeries(0, 0);
    const cell = series.cellFor(metadata)!;
    const plane = new Uint8Array(1440 * 721);
    // u = -3 m/s, v = -4 m/s: a 5 m/s northeasterly.
    plane[cell.index] = (-3 + 60) / 0.5;
    series.sample(metadata, "a:3", 0, plane);
    expect(probeSeriesValues(series, variables, [0])[0]).toBeUndefined();
    plane[cell.index] = (-4 + 60) / 0.5;
    series.sample(metadata, "a:4", 0, plane);
    expect(probeSeriesValues(series, variables, [0])[0]).toBeCloseTo(5, 9);
    expect(probeWindDirection(series, variables, 0)).toBeCloseTo(36.8699, 3);
  });

  it("has no direction before both components are sampled", () => {
    const series = new ProbeSeries(0, 0);
    const variables = [
      { key: "a:3", variable: windComponent(3, "ugrd10m") },
      { key: "a:4", variable: windComponent(4, "vgrd10m") },
    ];
    expect(probeWindDirection(series, variables, 0)).toBeNull();
    expect(probeWindDirection(series, [{ key: "a:1", variable: temperature() }], 0)).toBeNull();
  });
});
