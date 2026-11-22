import { describe, expect, it } from "vitest";

import { freezingHeight, lapseRate, modelColumn, surfacePressure } from "../../web/src/sounding/column";
import type { ColumnSurface } from "../../web/src/sounding/column";
import {
  modelColumnBundles,
  modelColumnPossible,
  modelColumnSurfaceIds,
  modelProfileBundles,
} from "../../web/src/sounding/model";
import { profileFromModel } from "../../web/src/sounding/profile";

/** Six levels of a run at one cell: a mild, moist column over low ground. */
const LEVELS = [
  { p: 1000, t: 18, rh: 80, z: 110, u: 2, v: 1 },
  { p: 925, t: 13, rh: 75, z: 780, u: 4, v: 2 },
  { p: 850, t: 8, rh: 60, z: 1490, u: 6, v: 2 },
  { p: 700, t: -1, rh: 50, z: 3050, u: 10, v: 3 },
  { p: 500, t: -18, rh: 30, z: 5650, u: 20, v: 4 },
  { p: 300, t: -44, rh: 20, z: 9300, u: 35, v: 5 },
];

const SEA: ColumnSurface = { t: 20, td: 16, u: 1, v: 0, prmsl: 1013, orog: 30 };

describe("surfacePressure", () => {
  it("is the sea-level pressure on terrain at height zero", () => {
    expect(surfacePressure(LEVELS, 1013, 0)).toBeCloseTo(1013, 6);
  });

  it("is a level's own pressure on terrain at that level's height", () => {
    expect(surfacePressure(LEVELS, 1013, 1490)).toBeCloseTo(850, 6);
  });

  it("falls between the two levels that bracket the terrain", () => {
    // 800 m is just above the 925 hPa surface (780 m) on the way to 850.
    const p = surfacePressure(LEVELS, 1013, 800)!;
    expect(p).toBeLessThan(925);
    expect(p).toBeGreaterThan(920);
  });

  it("extrapolates under the lowest pair and over the highest", () => {
    const below = surfacePressure(LEVELS, 1013, -50)!;
    expect(below).toBeGreaterThan(1013);
    expect(below).toBeLessThan(1025);
    const above = surfacePressure(LEVELS, 1013, 9800)!;
    expect(above).toBeLessThan(300);
    expect(above).toBeGreaterThan(250);
  });

  it("keeps its order in a low, where the 1000 hPa surface is under the sea", () => {
    const low = LEVELS.map((level) => (level.p === 1000 ? { ...level, z: -80 } : level));
    expect(surfacePressure(low, 990, 0)).toBeCloseTo(990, 6);
    const inside = surfacePressure(low, 990, -40)!;
    expect(inside).toBeGreaterThan(990);
    expect(inside).toBeLessThan(1000);
  });

  it("says nothing without a terrain height or with one pair", () => {
    expect(surfacePressure(LEVELS, 1013, null)).toBeNull();
    expect(surfacePressure(LEVELS, 1013, Number.NaN)).toBeNull();
    expect(surfacePressure([{ p: 500 }], 1013, 100)).toBeNull();
    expect(surfacePressure([{ p: 500, z: 5650 }], null, 100)).toBeNull();
  });
});

describe("modelColumn", () => {
  it("stands the levels on the ground with the 2 m point under them", () => {
    const column = modelColumn(LEVELS, SEA);
    // 30 m of terrain under a 1013 hPa sea-level pressure is a few
    // hectopascals short of it, and every level is above that.
    expect(column.pSurface!).toBeGreaterThan(1005);
    expect(column.pSurface!).toBeLessThan(1013);
    expect(column.levels).toBe(6);
    expect(column.top).toBe(300);
    const { profile } = column;
    expect(profile.n).toBe(7);
    // The ground comes first, at its own pressure and the terrain's height.
    expect(profile.p[0]).toBeCloseTo(column.pSurface!, 9);
    expect(profile.z[0]).toBe(30);
    expect(profile.t[0]).toBe(20);
    expect(profile.td[0]).toBe(16);
    expect(profile.ws[0]).toBeCloseTo(1, 9);
    expect(Array.from(profile.p.slice(1))).toEqual([1000, 925, 850, 700, 500, 300]);
    // A level's dew point comes from its humidity, and is below its temperature.
    expect(profile.td[1]!).toBeLessThan(18);
    expect(Number.isFinite(profile.td[1]!)).toBe(true);
  });

  it("derives where it freezes and the lapse between 850 and 500 hPa", () => {
    const { profile } = modelColumn(LEVELS, SEA);
    // 8 °C at 1490 m and −1 °C at 3050 m: eight ninths of the way up.
    expect(profile.derived!.freezingLevel!).toBeCloseTo(1490 + (8 / 9) * 1560, 6);
    expect(profile.derived!.lapse850_500!).toBeCloseTo(26 / 4.16, 6);
    expect(profile.derived!.tropopause).toBeNull();
    expect(profile.derived!.pw).toBeNull();
  });

  it("drops the levels a run extrapolated under a plateau", () => {
    const column = modelColumn(LEVELS, { ...SEA, t: 12, td: 2, orog: 2000 });
    // 2000 m lies between the 850 and 700 hPa surfaces.
    expect(column.pSurface!).toBeLessThan(850);
    expect(column.pSurface!).toBeGreaterThan(700);
    expect(column.levels).toBe(3);
    expect(Array.from(column.profile.p.slice(1))).toEqual([700, 500, 300]);
    expect(column.profile.z[0]).toBe(2000);
    // 12 °C at the ground, −1 °C at 3050 m.
    expect(column.profile.derived!.freezingLevel!).toBeCloseTo(2000 + (12 / 13) * 1050, 6);
    // The 850 hPa surface is under the ground: no lapse to it.
    expect(column.profile.derived!.lapse850_500).toBeNull();
  });

  it("starts at its lowest level until the 2 m temperature has been read", () => {
    const column = modelColumn(LEVELS, { ...SEA, t: null, td: null });
    expect(column.profile.n).toBe(6);
    expect(column.profile.p[0]).toBe(1000);
  });

  it("keeps every level when the ground cannot be placed", () => {
    const column = modelColumn(LEVELS, { ...SEA, orog: null });
    expect(column.pSurface).toBeNull();
    expect(column.levels).toBe(6);
    expect(column.profile.n).toBe(6);
  });

  it("has no freezing level over frozen ground", () => {
    const column = modelColumn(LEVELS, { ...SEA, t: -5, td: -8 });
    expect(column.profile.derived!.freezingLevel).toBeNull();
  });

  it("is an empty profile for a run with no levels and no ground", () => {
    const column = modelColumn([], { t: null, td: null, u: null, v: null, prmsl: null, orog: null });
    expect(column.profile.n).toBe(0);
    expect(column.levels).toBe(0);
    expect(column.top).toBeNull();
  });
});

describe("the two derived numbers", () => {
  it("find nothing in a profile without heights", () => {
    const bare = profileFromModel([
      { p: 850, t: 8 },
      { p: 500, t: -18 },
    ]);
    expect(freezingHeight(bare)).toBeNull();
    expect(lapseRate(bare, 850, 500)).toBeNull();
  });

  it("want both levels of a lapse", () => {
    const profile = profileFromModel(LEVELS);
    expect(lapseRate(profile, 850, 600)).toBeNull();
    expect(lapseRate(profile, 925, 700)!).toBeCloseTo(14 / 2.27, 6);
  });
});

describe("which runs can be a profile of their own", () => {
  const levels = [1000, 925, 850, 700, 500, 300, 250, 200];
  const ground = ["tmp2m", "dpt2m", "wind10m", "prmsl", "orog", "cape"];
  const full = [
    ...ground,
    "prate",
    ...levels.flatMap((level) => [`tmp${level}`, `rh${level}`, `wind${level}`, `hgt${level}`]),
  ];

  it("takes a run with the four families on every level", () => {
    expect(modelColumnPossible(full)).toBe(true);
    const bundles = modelColumnBundles(full);
    expect(bundles).toHaveLength(32);
    // From the ground up, and within a level temperature, humidity, wind, height.
    expect(bundles.slice(0, 4).map((bundle) => bundle.id)).toEqual(["tmp1000", "rh1000", "wind1000", "hgt1000"]);
    // The overlay beside an ascent still reads no heights.
    expect(modelProfileBundles(full).some((bundle) => bundle.field === "hgt")).toBe(false);
    expect(modelColumnSurfaceIds(full)).toEqual(["tmp2m", "dpt2m", "prmsl", "orog", "wind10m", "cape"]);
  });

  it("still takes one whose upper levels carry no humidity", () => {
    const dryTop = full.filter((id) => id !== "rh250" && id !== "rh200");
    expect(modelColumnPossible(dryTop)).toBe(true);
  });

  it("refuses three temperatures with no humidity, and a run with no ground", () => {
    const sparse = [...ground, "tmp925", "tmp850", "tmp500", "wind925", "wind850", "wind250", "hgt850", "hgt700", "hgt500"];
    expect(modelColumnPossible(sparse)).toBe(false);
    expect(modelColumnPossible(full.filter((id) => id !== "orog"))).toBe(false);
    expect(modelColumnPossible(full.filter((id) => !id.startsWith("rh")))).toBe(false);
    expect(modelColumnPossible([])).toBe(false);
  });
});
