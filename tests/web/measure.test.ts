/** The ruler and the area tool (measure.ts): lengths and areas on the
 * sphere, the great circles they are drawn along, the readings and what the
 * layers draw. */

import { describe, expect, it } from "vitest";

import {
  densify,
  distance,
  EARTH_RADIUS,
  formatArea,
  formatDistance,
  greatCircle,
  measureFeatures,
  pathLength,
  ringArea,
  unwrapLongitude,
  type LonLat,
} from "../../web/src/measure";

const DEGREE = (2 * Math.PI * EARTH_RADIUS) / 360;

describe("distance", () => {
  it("reads a degree of a meridian", () => {
    expect(distance([0, 0], [0, 1])).toBeCloseTo(DEGREE, 3);
  });

  it("reads Tokyo to Osaka at about 400 km", () => {
    const metres = distance([139.767, 35.681], [135.502, 34.694]);
    expect(metres).toBeGreaterThan(395_000);
    expect(metres).toBeLessThan(410_000);
  });

  it("goes the short way across the antimeridian", () => {
    expect(distance([179, 0], [-179, 0])).toBeCloseTo(2 * DEGREE, 3);
  });

  it("sums a path", () => {
    expect(pathLength([[0, 0], [0, 1], [0, 3]])).toBeCloseTo(3 * DEGREE, 3);
    expect(pathLength([[0, 0]])).toBe(0);
  });
});

describe("great circles", () => {
  it("unwraps a longitude to the reference's side", () => {
    expect(unwrapLongitude(-179, 179)).toBe(181);
    expect(unwrapLongitude(10, 0)).toBe(10);
    expect(unwrapLongitude(350, 0)).toBe(-10);
  });

  it("keeps a short edge as its two ends", () => {
    expect(greatCircle([0, 0], [0.1, 0.1])).toEqual([[0, 0], [0.1, 0.1]]);
  });

  it("steps a long edge with continuous longitudes and its ends exact", () => {
    const path = greatCircle([170, 10], [-170, 20]);
    expect(path.length).toBeGreaterThan(2);
    expect(path[0]).toEqual([170, 10]);
    expect(path[path.length - 1]).toEqual([190, 20]);
    for (let i = 1; i < path.length; i += 1) {
      expect(Math.abs(path[i]![0] - path[i - 1]![0])).toBeLessThan(5);
    }
  });

  it("bows a long east-west edge poleward", () => {
    const path = greatCircle([-60, 45], [60, 45]);
    const middle = path[Math.floor(path.length / 2)]!;
    expect(middle[1]).toBeGreaterThan(50);
    // Measured along the arc it is drawn as.
    expect(pathLength(path)).toBeCloseTo(distance([-60, 45], [60, 45]), -1);
  });

  it("closes a ring back to its first point", () => {
    const ring = densify([[0, 0], [0.1, 0], [0.1, 0.1]], true);
    expect(ring[0]).toEqual(ring[ring.length - 1]);
  });
});

describe("ringArea", () => {
  const square: LonLat[] = [[0, 0], [1, 0], [1, 1], [0, 1]];
  // R² Δλ (sin φ₂ − sin φ₁) for the cell between parallels.
  const cell = EARTH_RADIUS ** 2 * (Math.PI / 180) * Math.sin(Math.PI / 180);

  it("reads a one-degree cell at the equator", () => {
    expect(Math.abs(ringArea(square) / cell - 1)).toBeLessThan(0.005);
  });

  it("does not care which way the ring winds", () => {
    expect(ringArea([...square].reverse())).toBeCloseTo(ringArea(square), 0);
  });

  it("reads a cell straddling the antimeridian as one degree wide", () => {
    const across: LonLat[] = [[179.5, 0], [180.5, 0], [180.5, 1], [179.5, 1]];
    expect(Math.abs(ringArea(across) / cell - 1)).toBeLessThan(0.005);
  });

  it("is nothing under three points", () => {
    expect(ringArea([[0, 0], [1, 1]])).toBe(0);
  });
});

describe("readings", () => {
  it("formats distances", () => {
    expect(formatDistance(849.6)).toBe("850 m");
    expect(formatDistance(12_345)).toBe("12.3 km");
    expect(formatDistance(1_234_567)).toBe("1,235 km");
  });

  it("formats areas", () => {
    expect(formatArea(5_000)).toBe("5,000 m²");
    expect(formatArea(1_234_000)).toBe("1.23 km²");
    expect(formatArea(123_400_000)).toBe("123.4 km²");
    expect(formatArea(12_364_000_000)).toBe("12,364 km²");
  });
});

describe("measureFeatures", () => {
  const roles = (collection: ReturnType<typeof measureFeatures>) => collection.features.map((f) => f.properties.role);
  const labels = (collection: ReturnType<typeof measureFeatures>) =>
    collection.features.filter((f) => f.properties.role === "label").map((f) => f.properties.label);

  it("draws a lone vertex and nothing else", () => {
    expect(roles(measureFeatures("distance", [[0, 0]], null))).toEqual(["vertex"]);
  });

  it("labels a distance's running total at each vertex past the first and at the pointer", () => {
    const drawn = measureFeatures("distance", [[0, 0], [0, 1]], [0, 3]);
    expect(roles(drawn)).toEqual(["edge", "vertex", "vertex", "label", "label"]);
    expect(labels(drawn)).toEqual([formatDistance(DEGREE), formatDistance(3 * DEGREE)]);
  });

  it("fills an area and labels it once", () => {
    const drawn = measureFeatures("area", [[0, 0], [1, 0], [1, 1]], null);
    expect(roles(drawn)).toEqual(["edge", "fill", "vertex", "vertex", "vertex", "label"]);
    const ring = drawn.features[0]!.geometry as unknown as { coordinates: LonLat[] };
    expect(ring.coordinates[0]).toEqual(ring.coordinates[ring.coordinates.length - 1]);
    expect(labels(drawn)).toEqual([formatArea(ringArea([[0, 0], [1, 0], [1, 1]]))]);
  });

  it("draws an area of two points as its edge only", () => {
    expect(roles(measureFeatures("area", [[0, 0]], [1, 0]))).toEqual(["edge", "vertex"]);
  });
});
