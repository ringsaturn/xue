import { describe, expect, it } from "vitest";

import {
  DEFAULT_FPS,
  defaultFpsForLoop,
  FPS_LADDER,
  frameDwellScales,
  loopDwellUnits,
  nextFps,
  parseStoredFps,
} from "../../web/src/playback";

/** The live feed's three axes. */
const GFS_HOURS = [...Array(121).keys()].concat(
  [...Array(40).keys()].map((step) => 123 + step * 3),
);
const ECMWF_HOURS = [...Array(49).keys()]
  .map((step) => step * 3)
  .concat([...Array(16).keys()].map((step) => 150 + step * 6));

describe("frameDwellScales", () => {
  it("holds every frame equally on a uniform axis", () => {
    expect(frameDwellScales([0, 1, 2, 3])).toEqual([1, 1, 1, 1]);
    expect(frameDwellScales([0, 3, 6, 9])).toEqual([1, 1, 1, 1]);
  });

  it("holds a longer step proportionally longer", () => {
    // The GFS/sflux tail: hourly to f120, then 3-hourly to f240.
    const scales = frameDwellScales(GFS_HOURS);
    expect(scales.length).toBe(161);
    expect(scales[0]).toBe(1);
    expect(scales[119]).toBe(1);
    // f120 is the last hourly frame but the transit out of it is three hours.
    expect(scales[120]).toBe(3);
    expect(scales[160]).toBe(3);
    // ECMWF doubles instead: 3-hourly to 144, 6-hourly to 240.
    const ecmwf = frameDwellScales(ECMWF_HOURS);
    expect(ecmwf[0]).toBe(1);
    expect(ecmwf[48]).toBe(2);
    expect(ecmwf.at(-1)).toBe(2);
  });

  it("gives the last frame the step leading into it", () => {
    expect(frameDwellScales([0, 1, 2, 5])).toEqual([1, 1, 3, 3]);
  });

  it("plays degenerate axes flat", () => {
    expect(frameDwellScales([])).toEqual([]);
    expect(frameDwellScales([12])).toEqual([1]);
    expect(frameDwellScales([6, 6, 6])).toEqual([1, 1, 1]);
  });
});

describe("loopDwellUnits", () => {
  it("counts frames on a uniform axis and forecast span on a mixed one", () => {
    expect(loopDwellUnits(frameDwellScales([...Array(25).keys()]))).toBe(25);
    // 161 GFS frames cost 243 intervals: the 240-hour span hourly-paced,
    // plus the last frame's own hold (a loop shows it before wrapping).
    expect(loopDwellUnits(frameDwellScales(GFS_HOURS))).toBe(243);
    // ECMWF's shortest step is 3 h, so its 240-hour span is 80 units + 2.
    expect(loopDwellUnits(frameDwellScales(ECMWF_HOURS))).toBe(82);
  });
});

describe("defaultFpsForLoop", () => {
  it("keeps the default rate for the live feed's axes", () => {
    expect(defaultFpsForLoop(loopDwellUnits(frameDwellScales(GFS_HOURS)))).toBe(DEFAULT_FPS);
    expect(defaultFpsForLoop(loopDwellUnits(frameDwellScales(ECMWF_HOURS)))).toBe(DEFAULT_FPS);
    expect(defaultFpsForLoop(48)).toBe(DEFAULT_FPS); // 4 s exactly
  });

  it("steps down for short case axes", () => {
    expect(defaultFpsForLoop(41)).toBe(6); // 3.4 s at 12 fps
    expect(defaultFpsForLoop(24)).toBe(6); // 4 s at 6 fps
    expect(defaultFpsForLoop(20)).toBe(3);
  });

  it("never goes below the slowest rung, however short the loop", () => {
    expect(defaultFpsForLoop(2)).toBe(FPS_LADDER[0]);
    expect(defaultFpsForLoop(0)).toBe(FPS_LADDER[0]);
  });

  it("never opens above the default rate", () => {
    for (const units of [1, 24, 65, 240, 10_000]) {
      expect(defaultFpsForLoop(units)).toBeLessThanOrEqual(DEFAULT_FPS);
    }
  });
});

describe("nextFps", () => {
  it("cycles the ladder and wraps", () => {
    expect(FPS_LADDER.map((fps) => nextFps(fps))).toEqual([6, 12, 24, 3]);
  });

  it("restarts at the slowest rung from an unknown rate", () => {
    expect(nextFps(9)).toBe(FPS_LADDER[0]);
  });
});

describe("parseStoredFps", () => {
  it("accepts ladder rungs only", () => {
    expect(parseStoredFps("24")).toBe(24);
    expect(parseStoredFps("9")).toBeNull();
    expect(parseStoredFps("fast")).toBeNull();
    expect(parseStoredFps(null)).toBeNull();
    expect(parseStoredFps("")).toBeNull();
  });
});
