/** The ruler and the area tool (measure.ts): lengths on the WGS84
 * ellipsoid (expected values from PROJ's geodesic, pyproj `Geod`), areas on
 * the authalic sphere, the great circles they are drawn along, the ground
 * profile, the readings and what the layers draw. */

import { describe, expect, it } from "vitest";

import { AUTHALIC_RADIUS, straightLineDistance } from "../../web/src/geodesy";
import {
  densify,
  distance,
  EARTH_RADIUS,
  formatArea,
  formatDistance,
  greatCircle,
  measureFeatures,
  pathLength,
  profileFromHeights,
  profilePlan,
  ringArea,
  unwrapLongitude,
  type LonLat,
} from "../../web/src/measure";

describe("distance", () => {
  it("reads a degree of a meridian at the equator on the ellipsoid, not the sphere", () => {
    expect(distance([0, 0], [0, 1])).toBeCloseTo(110574.3886, 2);
    // The sphere's degree is 111,195 m: half a percent longer.
    expect((2 * Math.PI * EARTH_RADIUS) / 360 - distance([0, 0], [0, 1])).toBeGreaterThan(600);
  });

  it("reads Tokyo to Osaka and London to Paris as PROJ does", () => {
    expect(distance([139.767, 35.681], [135.502, 34.694])).toBeCloseTo(403552.4166, 2);
    expect(distance([-0.1278, 51.5074], [2.3522, 48.8566])).toBeCloseTo(343923.1201, 2);
  });

  it("goes the short way across the antimeridian", () => {
    expect(distance([179, 0], [-179, 0])).toBeCloseTo(222638.9816, 2);
  });

  it("reads a long east-west line and a few metres", () => {
    expect(distance([-60, 45], [60, 45])).toBeCloseTo(8420705.4096, 1);
    expect(distance([0, 0], [0.0001, 0])).toBeCloseTo(11.1319, 3);
    expect(distance([10, 10], [10, 10])).toBe(0);
  });

  it("falls back to the sphere for nearly antipodal points rather than failing", () => {
    const metres = distance([0, 0], [179.9, 0.1]);
    expect(metres).toBeGreaterThan(19_900_000);
    expect(metres).toBeLessThan(20_100_000);
  });

  it("sums a path", () => {
    expect(pathLength([[0, 0], [0, 1], [0, 3]])).toBeCloseTo(331725.8699, 1);
    expect(pathLength([[0, 0]])).toBe(0);
  });
});

describe("straight line", () => {
  it("is the horizontal distance with the rise for a short step", () => {
    const run = distance([138.7, 35.3], [138.73, 35.36]);
    const chord = straightLineDistance({ lon: 138.7, lat: 35.3, height: 500 }, { lon: 138.73, lat: 35.36, height: 3776 });
    expect(chord).toBeGreaterThan(Math.hypot(run, 3276) - 5);
    expect(chord).toBeLessThan(Math.hypot(run, 3276) + 5);
  });

  it("runs under the curve between far points", () => {
    expect(straightLineDistance({ lon: 0, lat: 0, height: 0 }, { lon: 90, lat: 0, height: 0 })).toBeCloseTo(6378137 * Math.SQRT2, 0);
  });
});

describe("ground profile", () => {
  it("adds each step's rise to its run, and sums the climb and the descent", () => {
    const samples: LonLat[] = [[0, 0], [0, 0.001], [0, 0.002], [0, 0.003]];
    const profile = profileFromHeights(samples, [0, 30, 10, 10], 110);
    const runs = [1, 2, 3].map((i) => distance(samples[i - 1]!, samples[i]!));
    expect(profile.surface).toBeCloseTo(Math.hypot(runs[0]!, 30) + Math.hypot(runs[1]!, 20) + runs[2]!, 6);
    expect(profile.climb).toBe(30);
    expect(profile.descent).toBe(20);
    expect(profile.straight).toBeCloseTo(Math.hypot(pathLength(samples), 10), 0);
    expect(profile.resolution).toBe(110);
  });

  it("reads a hole in the DEM as level ground, and no straight line without both ends", () => {
    const samples: LonLat[] = [[0, 0], [0, 0.001], [0, 0.002]];
    const profile = profileFromHeights(samples, [100, null, 100], 110);
    expect(profile.surface).toBeCloseTo(pathLength(samples), 6);
    expect(profile.climb).toBe(0);
    expect(profileFromHeights(samples, [null, 5, 5], 110).straight).toBeNull();
  });

  it("samples a short line at the DEM's finest pixel and a long one coarser", () => {
    const short = profilePlan([[138.7, 35.3], [138.73, 35.36]]);
    expect(short.zoom).toBe(12);
    expect(short.resolution).toBeLessThan(30);
    expect(short.samples[0]).toEqual([138.7, 35.3]);
    expect(short.samples[short.samples.length - 1]).toEqual([138.73, 35.36]);
    const long = profilePlan([[139.767, 35.681], [135.502, 34.694]]);
    expect(long.zoom).toBeLessThan(12);
    expect(long.samples.length).toBeLessThanOrEqual(4001);
    expect(long.resolution).toBeGreaterThan(short.resolution);
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
    // Measured along the arc it is drawn as: the great circle is within a
    // hair of the ellipsoid's geodesic.
    expect(Math.abs(pathLength(path) / distance([-60, 45], [60, 45]) - 1)).toBeLessThan(1e-3);
  });

  it("closes a ring back to its first point", () => {
    const ring = densify([[0, 0], [0.1, 0], [0.1, 0.1]], true);
    expect(ring[0]).toEqual(ring[ring.length - 1]);
  });
});

describe("ringArea", () => {
  const square: LonLat[] = [[0, 0], [1, 0], [1, 1], [0, 1]];
  // R² Δλ (sin φ₂ − sin φ₁) for the cell between parallels, on the
  // authalic sphere.
  const cell = AUTHALIC_RADIUS ** 2 * (Math.PI / 180) * Math.sin(Math.PI / 180);

  it("reads a one-degree cell at the equator", () => {
    expect(Math.abs(ringArea(square) / cell - 1)).toBeLessThan(0.005);
    // PROJ's ellipsoidal area for the same cell: within half a percent.
    expect(Math.abs(ringArea(square) / 12308778361.469 - 1)).toBeLessThan(0.005);
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
    expect(labels(drawn)).toEqual([formatDistance(distance([0, 0], [0, 1])), formatDistance(pathLength([[0, 0], [0, 1], [0, 3]]))]);
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
