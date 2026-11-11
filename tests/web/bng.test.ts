import { describe, expect, it } from "vitest";

import { bngSquareId, fromBng, gridToOsgb36, osgb36ToGrid, parseBngReference, toBng, toBngReference } from "../../web/src/bng";

const dms = (d: number, m: number, s: number) => d + m / 60 + s / 3600;

describe("the National Grid projection", () => {
  it("matches the worked example in OS's guide to coordinate systems (Annex C)", () => {
    // OSGB36 52°39′27.2531″N 1°43′4.5177″E (Caister water tower) →
    // E 651409.903, N 313177.270.
    const grid = osgb36ToGrid(dms(52, 39, 27.2531), dms(1, 43, 4.5177));
    expect(grid.easting).toBeCloseTo(651409.903, 3);
    expect(grid.northing).toBeCloseTo(313177.27, 3);
  });

  it("inverts the worked example", () => {
    const back = gridToOsgb36(651409.903, 313177.27);
    expect(back.lat).toBeCloseTo(dms(52, 39, 27.2531), 8);
    expect(back.lon).toBeCloseTo(dms(1, 43, 4.5177), 8);
  });
});

// Expected values are PROJ's (pyproj 3.8, PROJ 9.8):
// +proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 +x_0=400000 +y_0=-100000
// +ellps=airy +towgs84=446.448,-125.157,542.06,0.1502,0.247,0.8421,-20.4894
// from EPSG:4326 — the same seven-parameter Helmert, not OSTN15, so these
// differ from OS's definitive coordinates by up to a few metres.
const PROJ: [string, number, number, number, number][] = [
  ["Big Ben", 51.500729, -0.124625, 530268.0875, 179643.8979],
  ["Ben Nevis", 56.796891, -5.003526, 216676.3227, 771286.1334],
  ["Edinburgh Castle", 55.948611, -3.200833, 325105.5983, 673492.884],
  ["Land's End", 50.0657, -5.7144, 134283.7568, 25009.3244],
];

describe("toBng", () => {
  it("matches PROJ's Helmert pipeline to a centimetre", () => {
    for (const [, lat, lon, easting, northing] of PROJ) {
      const grid = toBng(lat, lon);
      expect(Math.abs(grid.easting - easting)).toBeLessThan(0.02);
      expect(Math.abs(grid.northing - northing)).toBeLessThan(0.02);
    }
  });

  it("inverts", () => {
    for (const [, lat, lon, easting, northing] of PROJ) {
      const back = fromBng(easting, northing);
      expect(back.lat).toBeCloseTo(lat, 6);
      expect(back.lon).toBeCloseTo(lon, 6);
    }
  });
});

describe("bngSquareId", () => {
  it("letters the 100 km squares", () => {
    expect(bngSquareId(0, 0)).toBe("SV");
    expect(bngSquareId(530268, 179643)).toBe("TQ");
    expect(bngSquareId(651409, 313177)).toBe("TG");
    expect(bngSquareId(216676, 771286)).toBe("NN");
    expect(bngSquareId(450000, 1150000)).toBe("HU");
    expect(bngSquareId(699999, 1299999)).toBe("JM");
  });

  it("is null off the grid", () => {
    expect(bngSquareId(-1, 0)).toBeNull();
    expect(bngSquareId(700000, 0)).toBeNull();
    expect(bngSquareId(0, 1300000)).toBeNull();
  });
});

describe("toBngReference", () => {
  it("truncates to the square the point is in", () => {
    expect(toBngReference(51.500729, -0.124625, 5)).toBe("TQ 30268 79643");
    expect(toBngReference(51.500729, -0.124625, 4)).toBe("TQ 3026 7964");
    expect(toBngReference(51.500729, -0.124625, 1)).toBe("TQ 3 7");
    expect(toBngReference(56.796891, -5.003526, 3)).toBe("NN 166 712");
  });

  it("is null outside Great Britain", () => {
    expect(toBngReference(48.8566, 2.3522)).toBeNull(); // Paris
    expect(toBngReference(40.7128, -74.006)).toBeNull();
    expect(toBngReference(Number.NaN, 0)).toBeNull();
  });
});

describe("parseBngReference", () => {
  it("finds the square's south-west corner", () => {
    expect(parseBngReference("TQ 3026 7964")).toEqual({ easting: 530260, northing: 179640 });
    expect(parseBngReference("tg5113")).toEqual({ easting: 651000, northing: 313000 });
    expect(parseBngReference("HU")).toEqual({ easting: 400000, northing: 1100000 });
    expect(parseBngReference("SV")).toEqual({ easting: 0, northing: 0 });
  });

  it("round-trips every square on the grid", () => {
    for (let e = 0; e < 700_000; e += 100_000) {
      for (let n = 0; n < 1_300_000; n += 100_000) {
        const square = bngSquareId(e, n);
        expect(square).not.toBeNull();
        expect(parseBngReference(square ?? "")).toEqual({ easting: e, northing: n });
      }
    }
  });

  it("refuses malformed or off-grid references", () => {
    for (const text of ["TQ 302 79", "TI", "Q1234", "AA"]) expect(parseBngReference(text)).toBeNull();
  });
});
