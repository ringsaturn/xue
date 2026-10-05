import { describe, expect, it } from "vitest";

import { GIF_MAX_FRAMES, gifFileName, gifFrameWindow, gifSize } from "../../web/src/gifexport";

describe("gifFrameWindow", () => {
  it("runs from the frame on screen onward", () => {
    expect(gifFrameWindow(121, 10, 4)).toEqual([10, 11, 12, 13]);
  });

  it("reaches back when the axis ends first, keeping the loop's length", () => {
    expect(gifFrameWindow(121, 119, 4)).toEqual([117, 118, 119, 120]);
  });

  it("takes the whole axis when it is shorter than the cap", () => {
    expect(gifFrameWindow(3, 2)).toEqual([0, 1, 2]);
  });

  it("caps a long axis", () => {
    expect(gifFrameWindow(209, 0)).toHaveLength(GIF_MAX_FRAMES);
  });
});

describe("gifFileName", () => {
  it("names the field and the first frame's valid time", () => {
    expect(gifFileName("GFS / PRATE SFC", Date.UTC(2026, 9, 5, 12, 0))).toBe("xue-gfs-prate-sfc-20261005t1200z.gif");
  });

  it("falls back when the code line is empty", () => {
    expect(gifFileName("", Date.UTC(2026, 0, 1))).toBe("xue-map-20260101t0000z.gif");
  });
});

describe("gifSize", () => {
  it("scales a wide canvas down to the cap", () => {
    expect(gifSize(2880, 1800, 720)).toEqual({ width: 720, height: 450 });
  });

  it("never scales up", () => {
    expect(gifSize(390, 844, 720)).toEqual({ width: 390, height: 844 });
  });
});
