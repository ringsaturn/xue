import { describe, expect, it } from "vitest";

import { domainContains, domainUniforms, HRRR_DOMAIN, lambertCone, lambertForward } from "../../web/src/domain";

describe("the HRRR domain", () => {
  const cone = lambertCone(HRRR_DOMAIN);

  it("projects the first cell center where NCEP's grid definition puts it", () => {
    // The encoder reads the same numbers off the file (tests/test_hrrr.py).
    const [x, y] = lambertForward(HRRR_DOMAIN, cone, -122.719528, 21.138123);
    expect(x).toBeCloseTo(-2697520.14, 2);
    expect(y).toBeCloseTo(-1587306.15, 2);
  });

  it("contains the contiguous United States and not the rectangle's corners", () => {
    for (const [longitude, latitude] of [
      [-122.33, 47.6], // Seattle
      [-80.19, 25.76], // Miami
      [-71.06, 42.36], // Boston
      [-117.16, 32.72], // San Diego
      [-97.5, 38.5], // the origin
    ]) {
      expect(domainContains(HRRR_DOMAIN, cone, longitude!, latitude!)).toBe(true);
    }
    for (const [longitude, latitude] of [
      [-134.0, 52.5], // the north-west corner of the 0.03° grid
      [-61.0, 52.5], // the north-east corner
      [-134.0, 21.2], // the south-west corner
      [-61.0, 21.2], // the south-east corner
      [139.7, 35.7], // Tokyo, spelled east
      [-220.3, 35.7], // Tokyo, spelled west of the antimeridian
    ]) {
      expect(domainContains(HRRR_DOMAIN, cone, longitude!, latitude!)).toBe(false);
    }
  });

  it("packs the cone and the box for the shaders", () => {
    const { cone: packed, box } = domainUniforms(HRRR_DOMAIN);
    expect(packed[0]).toBeCloseTo(Math.sin((38.5 * Math.PI) / 180), 12);
    expect(packed[3]).toBeCloseTo((-97.5 * Math.PI) / 180, 12);
    expect(box[1] - box[0]).toBe(1799 * 3000);
    expect(box[3] - box[2]).toBe(1059 * 3000);
  });
});
