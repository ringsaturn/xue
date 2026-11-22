import { describe, expect, it } from "vitest";

import { analyseProfile, liftParcel } from "../../web/src/sounding/analysis";
import {
  SIG,
  clipBelowSurface,
  convectiveCondensation,
  dryAdiabat,
  dewPointFromRelativeHumidity,
  hasSig,
  interpolateAtPressure,
  lcl,
  mixingRatioLine,
  moistAdiabat,
  profileFromModel,
  profileFromSounding,
  windFromComponents,
} from "../../web/src/sounding/profile";
import { SAMPLE_URUMQI, SAMPLE_WAKKANAI } from "../../web/src/sounding/sample";
import {
  barbLevels,
  drawSkewT,
  drawSkewTCursor,
  fittedSkew,
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

  it("leans as far as a tall box can afford and no further", () => {
    // A box wider than it is tall keeps the classical 45°.
    expect(fittedSkew(464, 280, -50, 55)).toBe(1);
    // One taller than it is wide would carry its top edge past the air it
    // has to show: at 45° a −60 °C level at 150 hPa is off the right side.
    const box = { x: 0, y: 0, width: 480, height: 660 };
    const range = { tMin: -52.5, tMax: 57.5 };
    const leaning = skewTLayout(box, { ...range, skew: 1 });
    expect(toXY(leaning, 150, -60).x).toBeGreaterThan(box.width);
    const skew = fittedSkew(box.width, box.height, range.tMin, range.tMax);
    expect(skew).toBeGreaterThan(0.4);
    expect(skew).toBeLessThan(1);
    const fitted = skewTLayout(box, { ...range, skew });
    const tropopause = toXY(fitted, 150, -60);
    expect(insideChart(fitted, tropopause.x, tropopause.y)).toBe(true);
    // And the warmest air the top edge ever shows is still on it.
    expect(toXY(fitted, 100, -45).x).toBeLessThanOrEqual(box.width);
    // A degenerate box is answered rather than divided by.
    expect(fittedSkew(0, 0, -40, 45)).toBe(1);
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
    const path = liftParcel(profile, "surface")!.path;
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

describe("convectiveCondensation", () => {
  it("meets the temperature trace on the surface dew point's isohume", () => {
    const profile = profileFromSounding(WAKKANAI);
    const ccl = convectiveCondensation(profile)!;
    expect(ccl.pSurface).toBeCloseTo(1008, 6);
    expect(ccl.p).toBeLessThan(ccl.pSurface);
    // On the isohume, on the environment within the interpolation's slack,
    // and on its own dry adiabat at both ends.
    expect(ccl.t).toBeCloseTo(mixingRatioLine(ccl.w, ccl.p), 6);
    expect(Math.abs(interpolateAtPressure(profile, ccl.p, profile.t) - ccl.t)).toBeLessThan(0.5);
    expect(dryAdiabat(ccl.theta, ccl.p)).toBeCloseTo(ccl.t, 6);
    expect(dryAdiabat(ccl.theta, ccl.pSurface)).toBeCloseTo(ccl.tcon, 6);
    // Heating the ground is what it takes: the convective temperature is
    // warmer than the CCL by the dry lapse over the depth.
    expect(ccl.tcon).toBeGreaterThan(ccl.t);
  });

  it("says nothing for an ascent without a dew point", () => {
    const dry = profileFromModel([
      { p: 1000, t: 20, u: 0, v: 0 },
      { p: 850, t: 10, u: 0, v: 0 },
    ]);
    expect(convectiveCondensation(dry)).toBeNull();
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

  it("cuts a column at the station's own ground", () => {
    const column = profileFromModel([
      { p: 1000, t: 27 },
      { p: 850, t: 18 },
      { p: 700, t: 8 },
      { p: 500, t: -8, u: 10, v: 0 },
    ]);
    // A plateau station whose ascent starts at 652 hPa: every level the run
    // extrapolated under the terrain goes, and the rest keep their values.
    const cut = clipBelowSurface(column, 652);
    expect(Array.from(cut.p)).toEqual([500]);
    expect(cut.n).toBe(1);
    expect(cut.t[0]).toBe(-8);
    expect(cut.ws[0]).toBeCloseTo(10, 6);
    // Nothing to cut, and nothing to cut by, hand the column back as it is.
    expect(clipBelowSurface(column, 1013)).toBe(column);
    expect(clipBelowSurface(column, Number.NaN)).toBe(column);
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
    halo: "#fff",
    buoyant: "#f80",
    inhibited: "#08f",
    moistLayer: "#0aa",
    inversion: "#fa0",
  };

  /** A 2D context that records the calls instead of rasterising: jsdom has no
   * canvas backend, and what matters here is that a real ascent draws without
   * throwing and touches the box it was given. */
  function stubContext(): {
    context: CanvasRenderingContext2D;
    calls: string[];
    points: [number, number][];
    texts: [string, number, number][];
  } {
    const calls: string[] = [];
    const points: [number, number][] = [];
    const texts: [string, number, number][] = [];
    const record =
      (name: string) =>
      (...args: unknown[]): void => {
        calls.push(name);
        if ((name === "moveTo" || name === "lineTo") && typeof args[0] === "number" && typeof args[1] === "number") {
          points.push([args[0], args[1]]);
        }
        if (name === "fillText" && typeof args[0] === "string" && typeof args[1] === "number" && typeof args[2] === "number") {
          texts.push([args[0], args[1], args[2]]);
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
      strokeText: record("strokeText"),
      measureText: (text: string) => ({ width: text.length * 6 }),
    } as unknown as CanvasRenderingContext2D;
    return { context, calls, points, texts };
  }

  it("draws a real ascent, its model and its parcel without touching the DOM", () => {
    const profile = profileFromSounding(URUMQI);
    const model = profileFromModel([
      { p: 925, t: 18, rh: 60, u: 3, v: 1 },
      { p: 850, t: 14, rh: 55, u: 5, v: 2 },
      { p: 500, t: -12, rh: 30, u: 14, v: 4 },
      { p: 250, t: -48, rh: 15, u: 28, v: 6 },
    ]);
    const analysis = analyseProfile(profile);
    const { context, calls, points, texts } = stubContext();
    drawSkewT(context, LAYOUT, ink, {
      profile,
      model,
      parcel: analysis.parcel?.path ?? null,
      buoyancy: analysis.parcel
        ? { environment: analysis.parcel.environment, lfc: analysis.parcel.lfc, el: analysis.parcel.el }
        : null,
      moistLayers: analysis.moist,
      inversions: analysis.inversions,
      // Two levels three hectopascals apart: both are named, one under the other.
      marks: [
        { p: 700, label: "LCL" },
        { p: 697, label: "LFC" },
      ],
      heights: [
        { p: pressureAtHeight(profile, 3000), label: "3 km" },
        { p: pressureAtHeight(profile, 9000), label: "9 km" },
      ],
      labels: {
        temperature: "T",
        dew: "Td",
        parcel: "Parcel",
        pressureUnit: "hPa",
        temperatureUnit: "°C",
        mixingRatioUnit: "g/kg",
      },
      font: "10px mono",
    });
    expect(calls.filter((call) => call === "stroke").length).toBeGreaterThan(40);
    expect(calls).toContain("fillText");
    const written = texts.map(([text]) => text);
    for (const text of ["LCL", "LFC", "3 km", "9 km", "T", "Td", "hPa", "°C"]) expect(written).toContain(text);
    // The two close levels keep their order and a label's height apart.
    const lcl = texts.find(([text]) => text === "LCL")!;
    const lfc = texts.find(([text]) => text === "LFC")!;
    expect(lcl[2] - lfc[2]).toBeGreaterThanOrEqual(10);
    // The unit follows one of the isohumes' values, once.
    expect(written.filter((text) => text.endsWith("g/kg")).length).toBe(1);
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

  it("draws a trace through the levels that do not report it, and stops at a real hole", () => {
    const points = (levels: Parameters<typeof profileFromModel>[0]): ReturnType<typeof stubContext> => {
      const stub = stubContext();
      drawSkewT(stub.context, LAYOUT, ink, { profile: profileFromModel(levels), barbs: false });
      return stub;
    };
    // Significant wind levels filed between the temperature levels carry no
    // temperature: the trace is the same line with them as without them.
    const dense = points([
      { p: 1000, t: 20, td: 10 },
      { p: 900, t: 14, td: 8 },
      { p: 800, t: 8, td: 2 },
    ]);
    const interleaved = points([
      { p: 1000, t: 20, td: 10 },
      { p: 950, t: null },
      { p: 900, t: 14, td: 8 },
      { p: 850, t: null },
      { p: 800, t: 8, td: 2 },
    ]);
    expect(interleaved.points).toEqual(dense.points);
    // Silent levels across more than a quarter of the pressure are a hole,
    // and each of the two traces starts again above it; the same two levels
    // with nothing filed between them are simply an ascent's spacing.
    const moves = (stub: ReturnType<typeof stubContext>): number => stub.calls.filter((call) => call === "moveTo").length;
    const whole = points([
      { p: 1000, t: 20, td: 10 },
      { p: 700, t: 0, td: -6 },
      { p: 600, t: -8, td: -14 },
    ]);
    const holed = points([
      { p: 1000, t: 20, td: 10 },
      { p: 900, t: null },
      { p: 800, t: null },
      { p: 700, t: 0, td: -6 },
      { p: 600, t: -8, td: -14 },
    ]);
    expect(moves(holed)).toBe(moves(whole) + 2);
  });

  it("shades the areas between a lifted parcel and its surroundings inside the box", () => {
    const profile = profileFromSounding(WAKKANAI);
    // The surface parcel of this ascent is free between two levels, so
    // there is an area to shade.
    const parcel = liftParcel(profile, "surface")!;
    expect(parcel.lfc).not.toBeNull();
    const shaded = stubContext();
    drawSkewT(shaded.context, LAYOUT, ink, {
      profile,
      parcel: parcel.path,
      buoyancy: { environment: parcel.environment, lfc: parcel.lfc, el: parcel.el },
    });
    const plain = stubContext();
    drawSkewT(plain.context, LAYOUT, ink, { profile, parcel: parcel.path });
    const fills = (calls: string[]): number => calls.filter((call) => call === "fill").length;
    expect(fills(shaded.calls)).toBeGreaterThan(fills(plain.calls));
    // The path runs far above the chart's top; no corner of the shading does.
    for (const [, y] of shaded.points) {
      expect(y).toBeGreaterThanOrEqual(LAYOUT.y - 16);
      expect(y).toBeLessThanOrEqual(LAYOUT.y + LAYOUT.height + 16);
    }
  });

  it("draws the crosshair inside the chart and its tags beside it", () => {
    const { context, calls, points } = stubContext();
    drawSkewTCursor(context, LAYOUT, ink, {
      p: 700,
      marks: [
        { t: -10, color: "#0aa", label: "-10 °C", side: "left" },
        { t: 2, color: "#a50", label: "2 °C", side: "right" },
      ],
      left: ["3048 m", "700 hPa"],
      right: "16 m/s 250°",
    });
    expect(calls.filter((call) => call === "arc").length).toBe(2);
    expect(calls.filter((call) => call === "fillText").length).toBe(5);
    expect(calls.filter((call) => call === "save").length).toBe(calls.filter((call) => call === "restore").length);
    const y = Math.round(toXY(LAYOUT, 700, 0).y) + 0.5;
    expect(points).toContainEqual([LAYOUT.x, y]);
    // A reading whose own side would put it under the wind's tag is turned
    // to the other side of its dot.
    const edge = stubContext();
    drawSkewTCursor(edge.context, LAYOUT, ink, {
      p: 700,
      marks: [{ t: 30, color: "#a50", label: "30 °C", side: "right" }],
      left: [],
      right: "16 m/s 250°",
    });
    const reading = edge.texts.find(([text]) => text === "30 °C")!;
    expect(reading[1]).toBeLessThan(toXY(LAYOUT, 700, 30).x);
    // Above the top of the chart there is nothing to point at.
    const above = stubContext();
    drawSkewTCursor(above.context, LAYOUT, ink, { p: 50, marks: [], left: [], right: null });
    expect(above.calls).toEqual([]);
  });

  it("draws an empty chart when there is nothing to plot", () => {
    const { context, calls } = stubContext();
    drawSkewT(context, skewTLayout({ x: 0, y: 0, width: 300, height: 260 }, { pressureTop: 50 }), ink, {});
    expect(calls).toContain("strokeRect");
    expect(calls.filter((call) => call === "save").length).toBe(calls.filter((call) => call === "restore").length);
  });
});
