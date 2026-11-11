import { describe, expect, it } from "vitest";

import {
  ACCUMULATION_BOUNDS,
  ACCUMULATION_QUANTIZATION,
  RangeSum,
  WindowTotals,
  accumulationLegend,
  accumulationParam,
  accumulationPalette,
  accumulationPlan,
  accumulationVariable,
  accumulationWindows,
  encodeTotal,
  parseAccumulation,
  parseWindow,
  planTotal,
  rangeIndices,
  rangePlan,
  rateKind,
  runningTotals,
  seriesTotals,
  windowCode,
  windowFits,
  windowParam,
} from "../../web/src/accumulation";
import type { BundleVariable } from "../../web/src/manifest";
import { decodeValue } from "../../web/src/palettes";

const HOUR = 3600;

/** A rate in mm/h on a linear codebook of 0.1 mm/h steps, so a code is ten
 * times its rate and sums are easy to state. */
function rate(statistical: boolean): BundleVariable {
  return {
    numericId: 1,
    id: "prate",
    label: "Precipitation rate",
    unit: "mm/h",
    parameter: {
      discipline: 0,
      parameterCategory: 1,
      parameterNumber: 7,
      typeOfFirstFixedSurface: 1,
      scaledValueOfFirstFixedSurface: 0,
      scaleFactorOfFirstFixedSurface: 0,
      ...(statistical ? { typeOfStatisticalProcessing: 0 } : {}),
    },
    quantization: { type: "linear", offset: 0, scale: 0.1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
  } as BundleVariable;
}

function hours(plan: ReturnType<typeof accumulationPlan>): [number, number][] {
  return plan.terms.map((term) => [term.index, Number(term.hours.toFixed(6))]);
}

describe("rateKind", () => {
  it("reads a statistic over the step as an interval mean and anything else as a sample", () => {
    expect(rateKind(rate(true))).toBe("interval");
    expect(rateKind(rate(false))).toBe("instant");
    expect(rateKind({ ...rate(false), unit: "K" })).toBeNull();
  });
});

describe("accumulationWindows", () => {
  it("offers the forecast windows no shorter than the step and no longer than the axis", () => {
    const hourly = Array.from({ length: 121 }, (_, index) => index * HOUR);
    expect(accumulationWindows(hourly, "instant", false).map(windowCode)).toEqual(["3H", "6H", "12H", "24H", "48H", "72H"]);
    const sixHourly = Array.from({ length: 5 }, (_, index) => (index + 1) * 6 * HOUR);
    // An interval axis from F006 covers the run's first six hours too.
    expect(accumulationWindows(sixHourly, "interval", false).map(windowCode)).toEqual(["6H", "12H", "24H"]);
  });

  it("offers the observation windows the radar window spans", () => {
    const twoMinutes = Array.from({ length: 61 }, (_, index) => index * 120);
    expect(accumulationWindows(twoMinutes, "instant", true).map(windowCode)).toEqual(["10M", "30M", "1H"]);
  });
});

describe("window spellings", () => {
  it("round-trips the URL's spelling", () => {
    for (const seconds of [600, 1800, HOUR, 24 * HOUR, 72 * HOUR]) expect(parseWindow(windowParam(seconds))).toBe(seconds);
    expect(parseWindow("24H")).toBe(24 * HOUR);
    expect(parseWindow("30min")).toBe(1800);
    expect(parseWindow("0h")).toBeNull();
    expect(parseWindow("day")).toBeNull();
  });
});

describe("accumulationPlan", () => {
  it("takes the steps of an interval axis that fall inside the window", () => {
    // ECMWF-like: F003, F006 … then six-hourly past F012.
    const leads = [3, 6, 9, 12, 18, 24].map((hour) => hour * HOUR);
    expect(hours(accumulationPlan(leads, 3, 6 * HOUR, "interval"))).toEqual([
      [2, 3],
      [3, 3],
    ]);
    // A three-hour window over a six-hour step takes half of that step.
    expect(hours(accumulationPlan(leads, 5, 3 * HOUR, "interval"))).toEqual([[5, 3]]);
    // The first frame covers the run's first three hours.
    const start = accumulationPlan(leads, 1, 24 * HOUR, "interval");
    expect(hours(start)).toEqual([
      [0, 3],
      [1, 3],
    ]);
    expect(start.covered).toBe(6 * HOUR);
  });

  it("integrates an instantaneous axis by the trapezoid rule", () => {
    const leads = [0, 1, 2, 3, 4].map((hour) => hour * HOUR);
    expect(hours(accumulationPlan(leads, 4, 3 * HOUR, "instant"))).toEqual([
      [1, 0.5],
      [2, 1],
      [3, 1],
      [4, 0.5],
    ]);
    // A window starting mid-step weights the line over the part inside.
    const half = accumulationPlan(leads, 4, 1.5 * HOUR, "instant");
    expect(hours(half)).toEqual([
      [2, 0.125],
      [3, 0.875],
      [4, 0.5],
    ]);
    expect(half.terms.reduce((sum, term) => sum + term.hours, 0)).toBeCloseTo(1.5);
  });

  it("reads a window ending at an instantaneous axis's first frame as dry, not unknown", () => {
    const leads = [0, 1, 2].map((hour) => hour * HOUR);
    const plan = accumulationPlan(leads, 0, 48 * HOUR, "instant");
    expect(hours(plan)).toEqual([[0, 0]]);
    expect(plan.covered).toBe(0);
    expect(planTotal([44.7, 6.5, 1.7], plan)).toBe(0);
  });

  it("draws no line across a hole in an observation window", () => {
    const leads = [0, 120, 240, 360, 2400, 2520].map(Number);
    const plan = accumulationPlan(leads, 5, 3600, "instant");
    expect(plan.terms.map((term) => term.index)).toEqual([0, 1, 2, 3, 4, 5]);
    expect(plan.covered).toBe(480);
  });
});

describe("windowFits", () => {
  it("takes any window from one step to the axis's span", () => {
    const hourly = [...Array(25).keys()].map((hour) => hour * HOUR);
    expect(windowFits(hourly, "instant", 9 * HOUR)).toBe(true);
    expect(windowFits(hourly, "instant", 2 * HOUR)).toBe(true);
    expect(windowFits(hourly, "instant", 30 * 60)).toBe(false);
    expect(windowFits(hourly, "instant", 25 * HOUR)).toBe(false);
    // ECMWF-like three-hourly: a two-hour window is under one step.
    const threeHourly = [3, 6, 9, 12].map((hour) => hour * HOUR);
    expect(windowFits(threeHourly, "interval", 2 * HOUR)).toBe(false);
    expect(windowFits(threeHourly, "interval", 12 * HOUR)).toBe(true);
  });
});

describe("a range", () => {
  it("is spelled by UTC valid times, to the minute only off the hour", () => {
    const range = { type: "range" as const, from: Date.UTC(2026, 10, 11, 6), to: Date.UTC(2026, 10, 14, 0, 30) };
    expect(accumulationParam(range)).toBe("2026111106-202611140030");
    expect(parseAccumulation("2026111106-202611140030")).toEqual(range);
    expect(parseAccumulation("24h")).toEqual({ type: "window", seconds: 24 * HOUR });
    expect(parseAccumulation("2026023106-2026030100")).toBeNull();
    expect(parseAccumulation("2026111106-2026111106")).toBeNull();
  });

  it("snaps its ends to the nearest frames, and is none when they meet", () => {
    const times = [0, 1, 2, 3, 6].map((hour) => hour * HOUR * 1000);
    expect(rangeIndices(times, { from: 0.4 * HOUR * 1000, to: 5 * HOUR * 1000 })).toEqual({ from: 0, to: 4 });
    expect(rangeIndices(times, { from: 10 * HOUR * 1000, to: 12 * HOUR * 1000 })).toBeNull();
  });

  it("weights the frames between its two valid times", () => {
    const interval = [3, 6, 9, 12, 18, 24].map((hour) => hour * HOUR);
    expect(hours(rangePlan(interval, 1, 4, "interval"))).toEqual([
      [2, 3],
      [3, 3],
      [4, 6],
    ]);
    const instant = [0, 1, 2, 3].map((hour) => hour * HOUR);
    const plan = rangePlan(instant, 1, 3, "instant");
    expect(hours(plan)).toEqual([
      [1, 0.5],
      [2, 1],
      [3, 0.5],
    ]);
    expect(planTotal([9, 2, 4, 2], plan)).toBe(6);
    expect(planTotal([9, 2, undefined, 2], plan)).toBeUndefined();
  });

  it("sums the frames added one at a time, each once", () => {
    const variable = rate(false);
    const sum = new RangeSum(variable, "range", 2);
    // Codes are ten times the rate: 2 mm/h for an hour, no data beside it.
    const plane = new Uint8Array([20, 255]);
    sum.add("a", plane, 1);
    sum.add("a", plane, 1);
    expect(sum.count).toBe(1);
    expect([...sum.plane()]).toEqual([encodeTotal(2), ACCUMULATION_QUANTIZATION.nodataCode]);
    sum.add("b", new Uint8Array([10, 0]), 2);
    expect([...sum.plane()]).toEqual([encodeTotal(4), ACCUMULATION_QUANTIZATION.zeroCode]);
  });
});

describe("seriesTotals", () => {
  it("sums a probe series over the window, waiting on unread frames", () => {
    const leads = [0, 1, 2, 3].map((hour) => hour * HOUR);
    // The first frame of an instantaneous axis has nothing before it: dry, not unknown.
    expect(seriesTotals([2, 2, 4, undefined], leads, 2 * HOUR, "instant")).toEqual([0, 2, 5, undefined]);
    expect(seriesTotals([null, 1, null, 3], leads, HOUR, "interval")).toEqual([null, 1, null, 3]);
  });
});

describe("runningTotals", () => {
  it("adds up from the axis's start, an interval frame counting its own step", () => {
    const leads = [3, 6, 9].map((hour) => hour * HOUR);
    expect(runningTotals([1, 2, 3], leads, "interval")).toEqual([3, 9, 18]);
    expect(runningTotals([2, 2, 4, 4], [0, 1, 2, 3].map((hour) => hour * HOUR), "instant")).toEqual([0, 2, 5, 9]);
  });

  it("reads a range as the sum the range plan makes at every frame", () => {
    const leads = [1, 2, 3, 4, 6].map((hour) => hour * HOUR);
    const values = [1, 2, 3, 4, 5];
    for (const kind of ["interval", "instant"] as const) {
      const totals = runningTotals(values, leads, kind, { from: 1, to: 3 });
      expect(totals[0]).toBeNull();
      expect(totals[4]).toBeNull();
      for (const index of [1, 2, 3]) {
        const planned = index === 1 ? 0 : planTotal(values, rangePlan(leads, 1, index, kind));
        expect(totals[index]).toBeCloseTo(planned as number);
      }
    }
  });

  it("waits from the first unread frame on and passes over missing ones", () => {
    const leads = [1, 2, 3, 4].map((hour) => hour * HOUR);
    expect(runningTotals([1, undefined, 1, 1], leads, "interval")).toEqual([1, undefined, undefined, undefined]);
    expect(runningTotals([null, null, 2, 1], leads, "interval")).toEqual([null, null, 2, 3]);
  });
});

describe("the total codebook", () => {
  it("encodes a total to the code that decodes nearest it", () => {
    const variable = accumulationVariable(rate(false));
    expect(variable.unit).toBe("mm");
    expect(encodeTotal(0)).toBe(ACCUMULATION_QUANTIZATION.zeroCode);
    expect(encodeTotal(0.05)).toBe(ACCUMULATION_QUANTIZATION.zeroCode);
    // Half a code step on the log1p grid: (log1p(1000) - log1p(0.1)) / 252 / 2.
    for (const total of [0.1, 1, 12.5, 87, 400]) {
      const decoded = decodeValue(variable, encodeTotal(total))!;
      expect(Math.abs(Math.log1p(decoded) - Math.log1p(total))).toBeLessThan(0.0136);
    }
    expect(encodeTotal(5000)).toBe(ACCUMULATION_QUANTIZATION.overflowCode);
  });

  it("colours every band and leaves dry and no data clear", () => {
    const palette = accumulationPalette();
    expect(palette[ACCUMULATION_QUANTIZATION.zeroCode * 4 + 3]).toBe(0);
    expect(palette[ACCUMULATION_QUANTIZATION.nodataCode * 4 + 3]).toBe(0);
    for (const bound of ACCUMULATION_BOUNDS) expect(palette[encodeTotal(bound * 1.05) * 4 + 3]).toBeGreaterThan(0);
    const key = accumulationLegend();
    expect(key.labels).toEqual(ACCUMULATION_BOUNDS.map(String).reverse());
  });
});

describe("WindowTotals", () => {
  const variable = rate(false);
  const plane = (...codes: number[]) => Uint8Array.from(codes);

  it("sums the weighted planes and steps the window exactly", () => {
    const totals = new WindowTotals(variable);
    const total = accumulationVariable(variable);
    const a = plane(10, 0, 255);
    const b = plane(20, 0, 255);
    const c = plane(30, 255, 255);
    const first = totals.plane("1", [
      { key: "a", plane: a, hours: 0.5 },
      { key: "b", plane: b, hours: 0.5 },
    ], 3);
    expect(decodeValue(total, first[0]!)).toBeCloseTo(1.5, 1);
    expect(first[1]).toBe(ACCUMULATION_QUANTIZATION.zeroCode);
    expect(first[2]).toBe(ACCUMULATION_QUANTIZATION.nodataCode);
    const second = totals.plane("2", [
      { key: "b", plane: b, hours: 0.5 },
      { key: "c", plane: c, hours: 0.5 },
    ], 3);
    expect(decodeValue(total, second[0]!)).toBeCloseTo(2.5, 1);
    // One frame of two read no data there: the other's dry reading stands.
    expect(second[1]).toBe(ACCUMULATION_QUANTIZATION.zeroCode);
    // The same terms again are the plane already made.
    expect(totals.plane("1", [
      { key: "a", plane: a, hours: 0.5 },
      { key: "b", plane: b, hours: 0.5 },
    ], 3)).toBe(first);
  });
});
