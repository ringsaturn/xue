/** The wind arrow over a case's fire marks (`fire/windarrow.ts`): the mean
 * wind since the newest pass, and the arrow drawn downwind at a fixed
 * ground scale. */

import { describe, expect, it } from "vitest";

import { ARROW_KM_PER_MS, arrowFeatures, meanWind } from "../../web/src/fire/windarrow";

const HOUR = 3600_000;
const T0 = Date.parse("2026-10-11T00:00:00Z");
const TIMES = [0, 1, 2, 3, 4].map((hour) => T0 + hour * HOUR);

describe("the mean wind since a pass", () => {
  const u = Float32Array.from([1, 2, 4, Number.NaN, 8]);
  const v = Float32Array.from([0, 0, 2, 0, 4]);

  it("averages the samples from the pass to the playhead as vectors", () => {
    const mean = meanWind(TIMES, u, v, T0 + HOUR, T0 + 2.5 * HOUR)!;
    expect(mean.u).toBeCloseTo(3);
    expect(mean.v).toBeCloseTo(1);
    expect(mean.count).toBe(2);
  });

  it("skips a missing sample", () => {
    const mean = meanWind(TIMES, u, v, T0 + 2 * HOUR, T0 + 4 * HOUR)!;
    expect(mean.u).toBeCloseTo(6);
    expect(mean.count).toBe(2);
  });

  it("takes the latest sample when the pass is younger than a step", () => {
    const mean = meanWind(TIMES, u, v, T0 + 1.2 * HOUR, T0 + 1.5 * HOUR)!;
    expect(mean).toMatchObject({ u: 2, v: 0, count: 1 });
  });

  it("has nothing before the first sample", () => {
    expect(meanWind(TIMES, u, v, T0 - 2 * HOUR, T0 - HOUR)).toBeNull();
  });
});

describe("the arrow", () => {
  it("points downwind, its shaft as long as the scale says", () => {
    const collection = arrowFeatures({ center: [119, 47], u: 0, v: 10, from: T0 });
    const shaft = collection.features[0]!.geometry as unknown as { coordinates: [number, number][] };
    const [tail, tip] = shaft.coordinates;
    expect(tip![0]).toBeCloseTo(tail![0]);
    expect((tip![1] - tail![1]) * 111.32).toBeCloseTo(10 * ARROW_KM_PER_MS);
  });

  it("labels the mean speed and the time it runs from", () => {
    const collection = arrowFeatures({ center: [119, 47], u: 3, v: 4, from: Date.parse("2026-10-11T04:42:00Z") });
    const label = collection.features.at(-1)!;
    expect(label.geometry.type).toBe("Point");
    // At the tip, north-east of the tail.
    const [lon, lat] = (label.geometry as unknown as { coordinates: [number, number] }).coordinates;
    expect(lon).toBeGreaterThan(119);
    expect(lat).toBeGreaterThan(47);
    expect(label.properties!.label).toBe("5.0 m/s · 04:42Z →");
  });

  it("draws only the label in a calm", () => {
    expect(arrowFeatures({ center: [119, 47], u: 0, v: 0, from: T0 }).features).toHaveLength(1);
  });
});
