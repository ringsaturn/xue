import { describe, expect, it } from "vitest";

import {
  SIG,
  dryAdiabat,
  dewPointFromRelativeHumidity,
  hasSig,
  interpolateAtPressure,
  lcl,
  mixingRatioLine,
  moistAdiabat,
  parcelPath,
  profileFromModel,
  profileFromSounding,
  windFromComponents,
} from "../../web/src/sounding/profile";
import { SAMPLE_URUMQI, SAMPLE_WAKKANAI } from "../../web/src/sounding/sample";
import {
  barbLevels,
  drawSkewT,
  fromXY,
  insideChart,
  knots,
  pressureAtHeight,
  readoutAt,
  skewTLayout,
  toXY,
} from "../../web/src/sounding/skewt";
import type { SkewTInk } from "../../web/src/sounding/skewt";

const LAYOUT = skewTLayout({ x: 40, y: 20, width: 600, height: 500 });
const WAKKANAI = SAMPLE_WAKKANAI.soundings[0]!;
const URUMQI = SAMPLE_URUMQI.soundings[0]!;

describe("the skew-T projection", () => {
  it("round-trips a pressure and a temperature through the canvas", () => {
    for (const [p, t] of [
      [1000, 20],
      [850, 0],
      [500, -18.5],
      [137.4, -63.25],
    ] as const) {
      const { x, y } = toXY(LAYOUT, p, t);
      const back = fromXY(LAYOUT, x, y);
      expect(back.p).toBeCloseTo(p, 6);
      expect(back.t).toBeCloseTo(t, 9);
    }
  });

  it("puts the pressure range on the box's edges, bottom first", () => {
    expect(toXY(LAYOUT, LAYOUT.pBottom, 0).y).toBeCloseTo(LAYOUT.y + LAYOUT.height, 9);
    expect(toXY(LAYOUT, LAYOUT.pTop, 0).y).toBeCloseTo(LAYOUT.y, 9);
    // Log-p: the half-pressure point is not the half-height point.
    const middle = toXY(LAYOUT, (LAYOUT.pBottom + LAYOUT.pTop) / 2, 0).y;
    expect(middle).toBeGreaterThan(LAYOUT.y + LAYOUT.height / 2);
  });

  it("skews the isotherms to the right as they rise", () => {
    const low = toXY(LAYOUT, 1000, -10);
    const high = toXY(LAYOUT, 300, -10);
    expect(high.x).toBeGreaterThan(low.x);
    expect(high.y).toBeLessThan(low.y);
    // 45° by default: the rightward shift matches the rise, pixel for pixel.
    expect(high.x - low.x).toBeCloseTo(low.y - high.y, 6);
  });

  it("takes a skew of zero as an emagram", () => {
    const emagram = skewTLayout({ x: 0, y: 0, width: 100, height: 100 }, { skew: 0 });
    expect(toXY(emagram, 1000, 10).x).toBeCloseTo(toXY(emagram, 200, 10).x, 9);
  });

  it("knows what is inside the box", () => {
    expect(insideChart(LAYOUT, 41, 21)).toBe(true);
    expect(insideChart(LAYOUT, LAYOUT.x + LAYOUT.width + 10, 100)).toBe(false);
  });
});

describe("the background curves", () => {
  it("labels a dry adiabat by its temperature at 1000 hPa and cools it upward", () => {
    expect(dryAdiabat(300, 1000)).toBeCloseTo(300 - 273.15, 9);
    expect(dryAdiabat(300, 500)).toBeLessThan(dryAdiabat(300, 1000));
    // Poisson: 300 K dry-adiabatically to 500 hPa is 246.15 K.
    expect(dryAdiabat(300, 500)).toBeCloseTo(246.15 - 273.15, 1);
  });

  it("keeps a pseudoadiabat warmer than the dry adiabat through the same point", () => {
    for (const p of [900, 700, 500, 300]) {
      expect(moistAdiabat(20, p)).toBeGreaterThan(dryAdiabat(20 + 273.15, p));
    }
    // It starts where it is labelled and cools upward like everything else.
    expect(moistAdiabat(20, 1000)).toBeCloseTo(20, 9);
    expect(moistAdiabat(20, 500)).toBeLessThan(moistAdiabat(20, 900));
    // Far above the moisture, the two lapse rates converge on each other.
    const dryGap = dryAdiabat(20 + 273.15, 700) - dryAdiabat(20 + 273.15, 300);
    const moistGap = moistAdiabat(20, 700) - moistAdiabat(20, 300);
    expect(moistGap).toBeLessThan(dryGap);
  });

  it("puts the 10 g/kg isohume at 14 °C on the 1000 hPa line", () => {
    expect(mixingRatioLine(10, 1000)).toBeCloseTo(14.0, 0);
    expect(Math.abs(mixingRatioLine(10, 1000) - 14.0)).toBeLessThan(0.3);
    // The same mixing ratio needs a colder dew point at lower pressure.
    expect(mixingRatioLine(10, 700)).toBeLessThan(mixingRatioLine(10, 1000));
  });

  it("lifts a parcel to its condensation level", () => {
    const level = lcl(25, 15, 1000)!;
    // Bolton (1980) eq. 15 puts a 10 K dew-point depression at 1000 hPa
    // around 863 hPa — about 1 250 m, the 125 m per kelvin the rule of thumb
    // gives. (The brief's 875–880 hPa is a lighter lift than either.)
    expect(level.p).toBeGreaterThan(855);
    expect(level.p).toBeLessThan(870);
    expect(level.t).toBeLessThan(25);
    // A saturated parcel condenses where it stands.
    expect(lcl(10, 10, 900)).toEqual({ p: 900, t: 10 });
    expect(lcl(Number.NaN, 10, 900)).toBeNull();
  });

  it("follows the parcel dry to the LCL and moist above it", () => {
    const profile = profileFromSounding(WAKKANAI);
    const path = parcelPath(profile)!;
    expect(path.p.length).toBeGreaterThan(profile.n - 2);
    // Descending pressure, starting at the surface level.
    expect(path.p[0]).toBeCloseTo(1008, 6);
    for (let index = 1; index < path.p.length; index += 1) {
      expect(path.p[index]!).toBeLessThan(path.p[index - 1]!);
    }
    // The LCL is a vertex of the path, and the parcel is colder above it than
    // a dry ascent would leave it.
    expect([...path.p]).toContain(path.lcl.p);
    const theta = (profile.t[0]! + 273.15) * (1000 / profile.p[0]!) ** (287.04 / 1005.7);
    expect(path.t[path.t.length - 1]!).toBeGreaterThan(dryAdiabat(theta, path.p[path.p.length - 1]!));
  });
});

describe("profileFromSounding", () => {
  const profile = profileFromSounding(WAKKANAI);

  it("keeps every level of every array", () => {
    expect(profile.n).toBe(15);
    for (const field of [profile.p, profile.z, profile.t, profile.td, profile.wd, profile.ws, profile.sig]) {
      expect(field.length).toBe(15);
    }
  });

  it("converts the fixed point to the chart's units", () => {
    // 100 800 Pa, 295.15 K, 4.6 m/s at the surface.
    expect(profile.p[0]).toBeCloseTo(1008, 9);
    expect(profile.t[0]).toBeCloseTo(22.0, 9);
    expect(profile.ws[0]).toBeCloseTo(4.6, 9);
    expect(profile.wd[0]).toBe(210);
    expect(profile.p[1]).toBeCloseTo(1000, 9);
    expect(profile.t[1]).toBeCloseTo(20.6, 9);
    expect(profile.z[1]).toBe(70);
    // The 500 hPa level: −13.7 °C, the same number the index's headline
    // carries for this station.
    const index = [...profile.p].indexOf(500);
    expect(profile.t[index]).toBeCloseTo(-13.7, 9);
  });

  it("turns every -32768 into a NaN and leaves the rest finite", () => {
    // The surface level reports no height, and the dew point stops at 300 hPa.
    expect(profile.z[0]).toBeNaN();
    expect(Number.isFinite(profile.z[1]!)).toBe(true);
    const drySince = [...profile.td].findIndex((value) => Number.isNaN(value));
    expect(drySince).toBe(8);
    for (let index = drySince; index < profile.n; index += 1) expect(profile.td[index]).toBeNaN();
    for (let index = 0; index < drySince; index += 1) expect(Number.isFinite(profile.td[index]!)).toBe(true);
    // Every published level carries a pressure, which is the product's own
    // guarantee (docs/sounding.md §2).
    for (const p of profile.p) expect(Number.isFinite(p)).toBe(true);
  });

  it("carries the publisher's derived values through", () => {
    expect(profile.derived).toEqual({ freezingLevel: 3292, pw: 26.3, lapse850_500: 6.1, tropopause: null });
    expect(profileFromSounding(URUMQI).derived?.tropopause).toBe(12121);
  });

  it("refuses a file whose arrays are not parallel", () => {
    expect(() => profileFromSounding({ ...WAKKANAI, z: [0, 1] })).toThrow(/z has 2 levels/);
  });
});

describe("profileFromModel", () => {
  it("takes a relative humidity of 100 % as saturation", () => {
    const profile = profileFromModel([{ p: 850, t: 4.2, rh: 100 }]);
    expect(profile.td[0]).toBeCloseTo(4.2, 12);
    expect(dewPointFromRelativeHumidity(-30, 100)).toBeCloseTo(-30, 12);
    // And a drier level is colder than its temperature.
    expect(dewPointFromRelativeHumidity(20, 50)).toBeGreaterThan(8);
    expect(dewPointFromRelativeHumidity(20, 50)).toBeLessThan(10);
  });

  it("sorts the column downward and leaves the gaps as gaps", () => {
    const profile = profileFromModel([
      { p: 500, t: -18, rh: 40 },
      { p: 1000, t: 21, td: 17 },
      { p: 850, t: null },
    ]);
    expect([...profile.p]).toEqual([1000, 850, 500]);
    expect(profile.td[0]).toBe(17);
    expect(profile.t[1]).toBeNaN();
    expect(profile.td[1]).toBeNaN();
    // A model level is nobody's significant level.
    for (const word of profile.sig) expect(word).toBeNaN();
  });

  it("reads the wind in the meteorological convention", () => {
    // A westerly blows toward the east: u positive, direction 270.
    expect(windFromComponents(5, 0)).toEqual({ ws: 5, wd: 270 });
    expect(windFromComponents(0, 3).wd).toBe(180);
    const profile = profileFromModel([{ p: 700, t: 0, u: -10, v: -10 }]);
    expect(profile.ws[0]).toBeCloseTo(Math.hypot(10, 10), 9);
    expect(profile.wd[0]).toBeCloseTo(45, 9);
  });
});

describe("the wind column", () => {
  it("picks the flagged levels of a classical ascent", () => {
    const profile = profileFromSounding(WAKKANAI);
    // Every level of a mandatory-level TEMP is flagged, and every one carries
    // a wind.
    expect(barbLevels(profile)).toEqual([...Array(15).keys()]);
  });

  it("takes only the standard, surface, tropopause and maximum-wind levels of a dense one", () => {
    const profile = profileFromSounding(URUMQI);
    const picked = barbLevels(profile);
    expect(picked.length).toBeGreaterThan(5);
    expect(picked.length).toBeLessThan(profile.n / 3);
    const wanted = SIG.surface | SIG.standard | SIG.tropopause | SIG.maxWind;
    for (const index of picked) {
      expect(hasSig(profile.sig[index]!, wanted)).toBe(true);
      expect(Number.isFinite(profile.ws[index]!)).toBe(true);
    }
    // The levels it left out are the significant ones.
    const skipped = [...Array(profile.n).keys()].filter((index) => !picked.includes(index));
    expect(skipped.some((index) => hasSig(profile.sig[index]!, SIG.significantTemperature))).toBe(true);
    // The mandatory levels are among the picks.
    for (const level of [850, 700, 500, 300, 200]) {
      const index = [...profile.p].indexOf(level);
      if (index >= 0) expect(picked).toContain(index);
    }
  });

  it("falls back to a stride when nothing is flagged", () => {
    const model = profileFromModel(
      [...Array(120).keys()].map((step) => ({ p: 1000 - step * 5, t: 20 - step * 0.4, u: 3, v: 4 })),
    );
    const picked = barbLevels(model, 30);
    expect(picked.length).toBeLessThanOrEqual(30);
    expect(picked.length).toBeGreaterThan(20);
    expect(picked[0]).toBe(0);
    expect(picked[1]! - picked[0]!).toBe(4);
    // A short column is drawn whole.
    expect(barbLevels(profileFromModel([{ p: 900, t: 5, u: 1, v: 1 }]))).toEqual([0]);
  });

  it("rounds a speed to the five knots a barb can say", () => {
    expect(knots(0)).toBe(0);
    expect(knots(5.14)).toBe(10);
    expect(knots(25.7)).toBe(50);
  });
});

describe("readoutAt", () => {
  const observed = profileFromSounding(WAKKANAI);
  const model = profileFromModel([
    { p: 1000, t: 19, rh: 80, u: 2, v: -4 },
    { p: 850, t: 12, rh: 70, u: 6, v: -6 },
    { p: 700, t: 3, rh: 55, u: 9, v: -3 },
    { p: 500, t: -14, rh: 40, u: 16, v: 2 },
  ]);

  it("gives back the pressure a point was projected from", () => {
    for (const [p, t] of [
      [925, 12],
      [700, -1.5],
      [300, -45],
    ] as const) {
      const { x, y } = toXY(LAYOUT, p, t);
      const readout = readoutAt(LAYOUT, [observed], x, y)!;
      expect(readout.p).toBeCloseTo(p, 6);
      expect(readout.t).toBeCloseTo(t, 9);
    }
  });

  it("interpolates each profile at that pressure", () => {
    const { x, y } = toXY(LAYOUT, 700, 0);
    const readout = readoutAt(LAYOUT, [observed, model], x, y)!;
    // 700 hPa is a reported level of the ascent: 1.4 °C, 3 048 gpm.
    expect(readout.profiles[0]!.t).toBeCloseTo(1.4, 6);
    expect(readout.profiles[0]!.z).toBeCloseTo(3048, 6);
    expect(readout.profiles[0]!.ws).toBeCloseTo(15.9, 6);
    expect(readout.profiles[0]!.wd).toBeCloseTo(250, 4);
    expect(readout.profiles[1]!.t).toBeCloseTo(3, 6);
    // A pressure between two model levels lands between their values.
    const between = readoutAt(LAYOUT, [model], ...pointAt(600, 0))!;
    expect(between.profiles[0]!.t).toBeLessThan(3);
    expect(between.profiles[0]!.t).toBeGreaterThan(-14);
  });

  it("crosses north the short way", () => {
    // 350° below, 010° above: the interpolation reads north between them, not
    // a sweep back through the south.
    const wrapping = profileFromModel([
      { p: 900, t: 5, u: -Math.sin((350 * Math.PI) / 180) * 10, v: -Math.cos((350 * Math.PI) / 180) * 10 },
      { p: 800, t: 0, u: -Math.sin((10 * Math.PI) / 180) * 10, v: -Math.cos((10 * Math.PI) / 180) * 10 },
    ]);
    const readout = readoutAt(LAYOUT, [wrapping], ...pointAt(Math.sqrt(900 * 800), 0))!;
    expect(readout.profiles[0]!.wd).toBeCloseTo(0, 4);
  });

  it("says nothing outside the box", () => {
    expect(readoutAt(LAYOUT, [observed], LAYOUT.x - 5, LAYOUT.y + 10)).toBeNull();
    // The wind column is beside the chart, not on it.
    expect(readoutAt(LAYOUT, [observed], LAYOUT.x + LAYOUT.width + LAYOUT.barbWidth / 2, LAYOUT.y + 10)).toBeNull();
  });

  it("reports a missing field as null rather than a number", () => {
    const readout = readoutAt(LAYOUT, [observed], ...pointAt(150, -60))!;
    // The dew point stops at 300 hPa; the temperature does not.
    expect(readout.profiles[0]!.td).toBeNull();
    expect(readout.profiles[0]!.t).not.toBeNull();
  });

  function pointAt(p: number, t: number): [number, number] {
    const point = toXY(LAYOUT, p, t);
    return [point.x, point.y];
  }
});

describe("the vertical helpers", () => {
  const profile = profileFromSounding(WAKKANAI);

  it("finds the pressure a derived height sits at", () => {
    // The freezing level, 3 292 gpm, is between 850 (1 452 m) and 700
    // (3 048 m)… and above 700, which reports −1.45 °C.
    const p = pressureAtHeight(profile, profile.derived!.freezingLevel!);
    expect(p).toBeLessThan(700);
    expect(p).toBeGreaterThan(600);
    expect(pressureAtHeight(profile, 70)).toBeCloseTo(1000, 6);
    expect(pressureAtHeight(profile, 99999)).toBeNaN();
  });

  it("interpolates in the logarithm of pressure", () => {
    expect(interpolateAtPressure(profile, 1000, profile.t)).toBeCloseTo(20.6, 9);
    const between = interpolateAtPressure(profile, Math.sqrt(1000 * 925), profile.t);
    expect(between).toBeCloseTo((20.6 + 16.2) / 2, 6);
    expect(interpolateAtPressure(profile, 5, profile.t)).toBeNaN();
  });
});

describe("drawSkewT", () => {
  const ink: SkewTInk = {
    ink: "#111",
    inkMuted: "#666",
    grid: "#ddd",
    gridStrong: "#999",
    paper: "#fff",
    model: "#444",
    parcel: "#888",
  };

  /** A 2D context that records the calls instead of rasterising: jsdom has no
   * canvas backend, and what matters here is that a real ascent draws without
   * throwing and touches the box it was given. */
  function stubContext(): { context: CanvasRenderingContext2D; calls: string[]; points: [number, number][] } {
    const calls: string[] = [];
    const points: [number, number][] = [];
    const record =
      (name: string) =>
      (...args: unknown[]): void => {
        calls.push(name);
        if ((name === "moveTo" || name === "lineTo") && typeof args[0] === "number" && typeof args[1] === "number") {
          points.push([args[0], args[1]]);
        }
      };
    const context = {
      save: record("save"),
      restore: record("restore"),
      beginPath: record("beginPath"),
      closePath: record("closePath"),
      moveTo: record("moveTo"),
      lineTo: record("lineTo"),
      arc: record("arc"),
      rect: record("rect"),
      clip: record("clip"),
      stroke: record("stroke"),
      fill: record("fill"),
      fillRect: record("fillRect"),
      strokeRect: record("strokeRect"),
      fillText: record("fillText"),
      setLineDash: record("setLineDash"),
    } as unknown as CanvasRenderingContext2D;
    return { context, calls, points };
  }

  it("draws a real ascent, its model and its parcel without touching the DOM", () => {
    const profile = profileFromSounding(URUMQI);
    const model = profileFromModel([
      { p: 925, t: 18, rh: 60, u: 3, v: 1 },
      { p: 850, t: 14, rh: 55, u: 5, v: 2 },
      { p: 500, t: -12, rh: 30, u: 14, v: 4 },
      { p: 250, t: -48, rh: 15, u: 28, v: 6 },
    ]);
    const { context, calls, points } = stubContext();
    drawSkewT(context, LAYOUT, ink, { profile, model, parcel: parcelPath(profile), font: "10px mono" });
    expect(calls.filter((call) => call === "stroke").length).toBeGreaterThan(40);
    expect(calls).toContain("fillText");
    // Every save is matched, so a caller's own state survives the chart.
    expect(calls.filter((call) => call === "save").length).toBe(calls.filter((call) => call === "restore").length);
    // Every point is a number, and every one of them sits between the box's
    // two pressure edges — give or take a barb, whose shaft leans out of the
    // column by its own length at the top and bottom levels, which is why a
    // caller leaves that much margin. The background families run out of the
    // box sideways, where the clip takes them.
    const margin = 16;
    for (const [x, y] of points) {
      expect(Number.isFinite(x)).toBe(true);
      expect(y).toBeGreaterThanOrEqual(LAYOUT.y - margin);
      expect(y).toBeLessThanOrEqual(LAYOUT.y + LAYOUT.height + margin);
    }
    // The wind column was drawn: some points sit to the right of the box.
    expect(points.some(([x]) => x > LAYOUT.x + LAYOUT.width)).toBe(true);
  });

  it("draws an empty chart when there is nothing to plot", () => {
    const { context, calls } = stubContext();
    drawSkewT(context, skewTLayout({ x: 0, y: 0, width: 300, height: 260 }, { pressureTop: 50 }), ink, {});
    expect(calls).toContain("strokeRect");
    expect(calls.filter((call) => call === "save").length).toBe(calls.filter((call) => call === "restore").length);
  });
});
