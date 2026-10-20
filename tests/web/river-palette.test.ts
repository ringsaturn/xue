import { describe, expect, it } from "vitest";

import { DISRP_CLASSES } from "../../web/src/levels";
import { decadeLabel, logRange, valueNear } from "../../web/src/river/hydrograph";
import { packValues, segmentInstances } from "../../web/src/river/layer";
import { reachClass, reachRatio, reachWidthPx, riverClassColors, RIVER_NODATA_COLOR } from "../../web/src/river/palette";

describe("river classes", () => {
  it("take the ratio against the usual flow, floored at 1 m³/s", () => {
    expect(reachRatio(10, 5)).toBe(2);
    expect(reachRatio(10, 0.2)).toBe(10);
    expect(reachRatio(10, Number.NaN)).toBe(10);
  });

  it("step at 2, 10 and 100 times the usual flow", () => {
    expect(reachClass(1.9, 1)).toBe(0);
    expect(reachClass(2, 1)).toBe(1);
    expect(reachClass(9.99, 1)).toBe(1);
    expect(reachClass(10, 1)).toBe(2);
    expect(reachClass(99, 1)).toBe(2);
    expect(reachClass(100, 1)).toBe(3);
    expect(reachClass(5000, 20)).toBe(3);
    expect(reachClass(Number.NaN, 1)).toBe(-1);
  });

  it("are the EFAS return-period hues: grey, 2-year yellow, 5-year red, 20-year purple", () => {
    const hue = (code: number) => DISRP_CLASSES.find((entry) => entry.code === code)!.rgb.map((value) => value / 255);
    const colors = riverClassColors();
    expect(colors).toHaveLength(4);
    expect(colors.map((color) => color.slice(0, 3))).toEqual([hue(1), hue(3), hue(4), hue(5)]);
    expect(colors.every((color) => color[3]! > 0.5)).toBe(true);
    expect(RIVER_NODATA_COLOR[3]).toBeLessThan(colors[0]![3]!);
  });
});

describe("river widths", () => {
  it("run from 1 px at 1 m³/s to 5 px at 10 000 m³/s in log10", () => {
    expect(reachWidthPx(0.2)).toBe(1);
    expect(reachWidthPx(1)).toBe(1);
    expect(reachWidthPx(10)).toBeCloseTo(2);
    expect(reachWidthPx(100)).toBeCloseTo(3);
    expect(reachWidthPx(10_000)).toBeCloseTo(5);
    expect(reachWidthPx(1e6)).toBe(5);
    expect(reachWidthPx(Number.NaN)).toBe(1);
  });
});

describe("the line layer's buffers", () => {
  it("cut each reach into segments carrying its feature", () => {
    const instances = segmentInstances(100, [Float64Array.from([-180, 0, 0, 0, 180, 0]), Float64Array.from([0, 0, 90, 0])]);
    expect(instances.length).toBe(3 * 5);
    expect(Array.from(instances.subarray(0, 5))).toEqual([0, 0.5, 0.5, 0.5, 100]);
    expect(instances[9]).toBe(100);
    expect(instances[14]).toBe(101);
  });

  it("pack no value as a negative flow and an unknown usual flow as 0", () => {
    const out = packValues(Float32Array.from([3, Number.NaN]), Float32Array.from([Number.NaN, 2]), new Float32Array(4));
    expect(Array.from(out)).toEqual([3, 0, -1, 2]);
  });
});

describe("the hydrograph", () => {
  it("spans whole decades of what it draws", () => {
    expect(logRange([[0.14, 8919.8, null], [3]])).toEqual([0.1, 10000]);
    expect(logRange([[0, 0.001]])).toEqual([0.1, 1]);
    expect(logRange([[50, 60]])).toEqual([10, 100]);
    expect(logRange([[]])).toEqual([1, 1000]);
    expect(decadeLabel(0.1)).toBe("0.1");
    expect(decadeLabel(100)).toBe("100");
    expect(decadeLabel(10000)).toBe("10k");
  });

  it("reads the reading nearest the playhead within the tolerance", () => {
    const series = { times: [0, 300_000, 600_000], values: [1, null, 3] };
    expect(valueNear(series, 290_000, 400_000)).toBe(1);
    expect(valueNear(series, 590_000, 60_000)).toBe(3);
    expect(valueNear(series, 300_000, 60_000)).toBeNull();
    expect(valueNear(null, 0, 1)).toBeNull();
  });
});
