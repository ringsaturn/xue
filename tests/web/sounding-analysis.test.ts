import { describe, expect, it } from "vitest";

import {
  analyseProfile,
  inversions,
  liftParcel,
  maxWind,
  moistLayers,
  precipitationReading,
  surfaceLevel,
} from "../../web/src/sounding/analysis";
import type { LiftedParcel } from "../../web/src/sounding/analysis";
import {
  dryAdiabat,
  mixingRatioLine,
  moistAdiabatFrom,
  profileFromSounding,
} from "../../web/src/sounding/profile";
import type { Profile } from "../../web/src/sounding/profile";
import { SAMPLE_URUMQI, SAMPLE_WAKKANAI } from "../../web/src/sounding/sample";

const RD = 287.04;
const KAPPA = RD / 1005.7;
const NAN = Number.NaN;

/** One level of a synthetic ascent; anything left out is unreported. */
interface Row {
  p: number;
  t?: number;
  td?: number;
  z?: number;
  ws?: number;
  wd?: number;
}

/** A `Profile` from rows, in the order given (callers list them by
 * descending pressure, as the product does). */
function column(rows: readonly Row[]): Profile {
  const field = (pick: (row: Row) => number | undefined): Float64Array =>
    Float64Array.from(rows, (row) => pick(row) ?? NAN);
  return {
    n: rows.length,
    p: field((row) => row.p),
    z: field((row) => row.z),
    t: field((row) => row.t),
    td: field((row) => row.td),
    wd: field((row) => row.wd),
    ws: field((row) => row.ws),
    sig: field(() => undefined),
    derived: null,
  };
}

/** Pressures from `bottom` to `top` inclusive every `step` hPa. */
function pressures(bottom: number, top: number, step: number): number[] {
  const out: number[] = [];
  for (let p = bottom; p >= top - 1e-9; p -= step) out.push(p);
  return out;
}

function expectDescending(values: Float64Array): void {
  for (let index = 1; index < values.length; index += 1) {
    expect(values[index]!).toBeLessThan(values[index - 1]!);
  }
}

/** The invariants every lifted parcel holds, whatever the ascent. */
function expectWellFormed(parcel: LiftedParcel): void {
  expect(parcel.path.p.length).toBe(parcel.path.t.length);
  expect(parcel.environment.length).toBe(parcel.path.p.length);
  expect(parcel.path.p[0]).toBe(parcel.p0);
  expectDescending(parcel.path.p);
  for (const t of parcel.path.t) expect(Number.isFinite(t)).toBe(true);
  expect(Number.isFinite(parcel.t0)).toBe(true);
  expect(Number.isFinite(parcel.td0)).toBe(true);
  expect(parcel.td0).toBeLessThanOrEqual(parcel.t0);
  expect(Number.isFinite(parcel.cape)).toBe(true);
  expect(parcel.cape).toBeGreaterThanOrEqual(0);
  const points = [...parcel.path.p];
  if (parcel.lfc === null) {
    expect(parcel.el).toBeNull();
    expect(parcel.cin).toBeNull();
    expect(parcel.cape).toBe(0);
  } else {
    expect(parcel.el).not.toBeNull();
    expect(parcel.cin).not.toBeNull();
    expect(parcel.cin!).toBeLessThanOrEqual(0);
    expect(Number.isFinite(parcel.cin!)).toBe(true);
    expect(parcel.p0).toBeGreaterThanOrEqual(parcel.lfc);
    expect(parcel.lfc).toBeGreaterThan(parcel.el!);
    expect(points).toContain(parcel.lfc);
    expect(points).toContain(parcel.el!);
  }
  if (parcel.path.lcl.p > parcel.path.p[parcel.path.p.length - 1]!) {
    expect(points).toContain(parcel.path.lcl.p);
  }
}

/** A saturated 20 °C parcel at 1000 hPa (its LCL is where it stands), in an
 * environment 1 K warmer than it up to 905 hPa, `excess` K colder from 900 to
 * 400 hPa and 5 K warmer above — a constant-buoyancy layer whose CAPE is
 * `Rd·excess·ln(900/400)` plus two thin triangles where the environment
 * swings across the parcel. */
function buoyantColumn(excess: number): Profile {
  return column(
    pressures(1000, 300, 5).map((p) => {
      const parcel = moistAdiabatFrom(1000, 20, p);
      const t = p > 900 ? parcel + 1 : p >= 400 ? parcel - excess : parcel + 5;
      return { p, t: p === 1000 ? 20 : t, td: p === 1000 ? 20 : t - 10 };
    }),
  );
}

describe("surfaceLevel", () => {
  it("is the lowest level with a pressure, a temperature and a dew point", () => {
    const profile = column([
      { p: 1010, t: 25 },
      { p: 1000, t: 24, td: 18, z: 80 },
      { p: 900, t: 18, td: 10, z: 980 },
    ]);
    expect(surfaceLevel(profile)).toEqual({
      index: 1,
      p: 1000,
      z: 80,
      t: 24,
      td: 18,
    });
  });

  it("keeps a missing height as NaN and answers null without dew points", () => {
    const surface = surfaceLevel(
      profileFromSounding(SAMPLE_WAKKANAI.soundings[0]!),
    )!;
    expect(surface.index).toBe(0);
    expect(surface.p).toBeCloseTo(1008, 9);
    expect(surface.z).toBeNaN();
    expect(
      surfaceLevel(
        column([
          { p: 1000, t: 20 },
          { p: 900, t: 12 },
        ]),
      ),
    ).toBeNull();
    expect(surfaceLevel(column([]))).toBeNull();
  });
});

describe("liftParcel's starting state", () => {
  // A well-mixed layer: one potential temperature, one mixing ratio.
  const theta = 300;
  const w = 12;
  const mixed = column(
    pressures(1000, 500, 25).map((p) => ({
      p,
      t: dryAdiabat(theta, p),
      td: mixingRatioLine(w, p),
    })),
  );

  it("lifts the surface level's own temperature and dew point", () => {
    const parcel = liftParcel(mixed, "surface")!;
    expect(parcel.kind).toBe("surface");
    expect(parcel.p0).toBe(1000);
    expect(parcel.t0).toBe(mixed.t[0]);
    expect(parcel.td0).toBe(mixed.td[0]);
  });

  it("averages a well-mixed layer back to its own surface state", () => {
    const parcel = liftParcel(mixed)!;
    expect(parcel.kind).toBe("mixed");
    expect(parcel.p0).toBe(1000);
    expect(parcel.t0).toBeCloseTo(mixed.t[0]!, 6);
    expect(parcel.td0).toBeCloseTo(mixed.td[0]!, 3);
  });

  it("mixes a superadiabatic surface layer cooler and a moist one drier", () => {
    // Hot, humid air at the ground under the same mixed layer as above.
    const rows: Row[] = pressures(1000, 500, 25).map((p) => ({
      p,
      t: dryAdiabat(theta, p),
      td: mixingRatioLine(w, p),
    }));
    rows[0] = { p: 1000, t: rows[0]!.t! + 4, td: rows[0]!.td! + 3 };
    const profile = column(rows);
    const parcel = liftParcel(profile)!;
    const surface = liftParcel(profile, "surface")!;
    expect(parcel.kind).toBe("mixed");
    expect(parcel.t0).toBeLessThan(surface.t0);
    expect(parcel.t0).toBeGreaterThan(dryAdiabat(theta, 1000) - 1e-9);
    expect(parcel.td0).toBeLessThan(surface.td0);
    expect(parcel.td0).toBeGreaterThan(mixingRatioLine(w, 1000) - 1e-9);
  });

  it("falls back to the surface parcel when the lowest 100 hPa has one usable level", () => {
    const profile = column([
      { p: 1000, t: 20, td: 15 },
      { p: 950, t: 17 },
      { p: 850, t: 10, td: 2 },
      { p: 700, t: 0, td: -10 },
    ]);
    const parcel = liftParcel(profile)!;
    expect(parcel.kind).toBe("surface");
    expect(parcel.t0).toBe(20);
    expect(parcel.td0).toBe(15);
  });
});

describe("liftParcel's CAPE and CIN", () => {
  it("measures a constant-buoyancy layer against the closed form", () => {
    const excess = 2;
    const profile = buoyantColumn(excess);
    const parcel = liftParcel(profile, "surface")!;
    expectWellFormed(parcel);
    expect(parcel.cape).toBeGreaterThan(0);
    const closed = RD * excess * Math.log(900 / 400);
    // The two triangles at the edges add about 1 J/kg each.
    expect(parcel.cape).toBeGreaterThan(closed);
    expect(parcel.cape - closed).toBeLessThan(5);
    // The LFC sits just below 900 hPa and the EL just above 400 hPa.
    expect(parcel.lfc!).toBeGreaterThan(900);
    expect(parcel.lfc!).toBeLessThan(905);
    expect(parcel.el!).toBeLessThan(400);
    expect(parcel.el!).toBeGreaterThan(395);
    // CIN is the 1 K deficit over 1000 → 905 hPa, plus a sliver.
    const inhibition = -RD * 1 * Math.log(1000 / 905);
    expect(parcel.cin!).toBeLessThan(inhibition + 0.5);
    expect(parcel.cin!).toBeGreaterThan(inhibition - 2);
  });

  it("closes the shaded polygon on the curves at the LFC and the EL", () => {
    const parcel = liftParcel(buoyantColumn(2), "surface")!;
    const points = [...parcel.path.p];
    for (const level of [parcel.lfc!, parcel.el!]) {
      const index = points.indexOf(level);
      expect(index).toBeGreaterThan(0);
      expect(
        Math.abs(parcel.path.t[index]! - parcel.environment[index]!),
      ).toBeLessThan(1e-4);
      // The vertex is on the true pseudoadiabat, not a chord across it.
      expect(parcel.path.t[index]!).toBeCloseTo(
        moistAdiabatFrom(1000, 20, level),
        3,
      );
    }
  });

  it("scales CAPE with the buoyancy", () => {
    const one = liftParcel(buoyantColumn(1), "surface")!.cape;
    const three = liftParcel(buoyantColumn(3), "surface")!.cape;
    expect(three).toBeGreaterThan(2.5 * one);
    expect(three).toBeLessThan(3.5 * one);
  });

  it("puts the LFC at the ground when the parcel is warmer from the start", () => {
    const profile = column(
      pressures(1000, 400, 10).map((p) => {
        const parcel = moistAdiabatFrom(1000, 20, p);
        return {
          p,
          t: p === 1000 ? 20 : p > 500 ? parcel - 1 : parcel + 3,
          td: p === 1000 ? 20 : -40,
        };
      }),
    );
    // At the ground the parcel is the environment, so the walk starts on a
    // zero and finds positive area straight away.
    const parcel = liftParcel(profile, "surface")!;
    expectWellFormed(parcel);
    expect(parcel.lfc).toBe(1000);
    expect(parcel.cin).toBe(0);
    // One kelvin of buoyancy from the ground to about 500 hPa.
    expect(parcel.cape).toBeGreaterThan(0.9 * RD * Math.log(1000 / 500));
    expect(parcel.cape).toBeLessThan(RD * Math.log(1000 / 500));
  });

  it("keeps the EL at the top of the data when the parcel never cools below it", () => {
    const profile = column(
      pressures(1000, 600, 10).map((p) => {
        const parcel = moistAdiabatFrom(1000, 20, p);
        return {
          p,
          t: p === 1000 ? 20 : parcel - 2,
          td: p === 1000 ? 20 : -40,
        };
      }),
    );
    const parcel = liftParcel(profile, "surface")!;
    expectWellFormed(parcel);
    expect(parcel.el).toBe(600);
  });

  it("finds nothing to release in a stable isothermal column", () => {
    const profile = column(
      pressures(1000, 200, 50).map((p) => ({
        p,
        t: 0,
        td: p === 1000 ? -2 : -20,
      })),
    );
    for (const kind of ["mixed", "surface"] as const) {
      const parcel = liftParcel(profile, kind)!;
      expectWellFormed(parcel);
      expect(parcel.cape).toBe(0);
      expect(parcel.lfc).toBeNull();
      expect(parcel.el).toBeNull();
      expect(parcel.cin).toBeNull();
    }
  });

  it("survives gaps, a single level, no dew points and an empty ascent", () => {
    expect(liftParcel(column([]))).toBeNull();
    expect(
      liftParcel(
        column([
          { p: 1000, t: 20 },
          { p: 900, t: 12 },
        ]),
      ),
    ).toBeNull();
    const single = liftParcel(column([{ p: 1000, t: 20, td: 15 }]))!;
    expectWellFormed(single);
    expect(single.kind).toBe("surface");
    expect(single.path.p.length).toBe(1);
    expect(single.lfc).toBeNull();
    const gappy = buoyantColumn(2);
    for (let index = 3; index < gappy.n; index += 7) gappy.t[index] = NAN;
    gappy.td[5] = NAN;
    // The mixed parcel takes in the drier air above the ground and has no
    // CAPE here; the surface parcel keeps its constant-buoyancy layer.
    expectWellFormed(liftParcel(gappy, "mixed")!);
    const surface = liftParcel(gappy, "surface")!;
    expectWellFormed(surface);
    expect(surface.cape).toBeGreaterThan(400);
    // A sounding whose temperature stops short leaves the environment NaN
    // above it, and nothing else.
    const short = buoyantColumn(2);
    for (let index = 0; index < short.n; index += 1)
      if (short.p[index]! < 600) short.t[index] = NAN;
    const parcel = liftParcel(short, "surface")!;
    expectWellFormed(parcel);
    expect(parcel.el).toBe(600);
    const last = parcel.environment.length - 1;
    expect(parcel.environment[last]).toBeNaN();
  });
});

describe("moistLayers", () => {
  /** Rows at the given pressures with the given dew-point depressions. */
  const spreads = (levels: readonly (readonly [number, number])[]): Profile =>
    column(levels.map(([p, s]) => ({ p, t: 0, td: -s, z: (1000 - p) * 10 })));
  const lnCrossing = (p1: number, p2: number, weight: number): number =>
    Math.exp(Math.log(p1) + weight * (Math.log(p2) - Math.log(p1)));

  it("places a layer's edges where the depression crosses the threshold", () => {
    const layers = moistLayers(
      spreads([
        [1000, 10],
        [950, 2],
        [900, 2],
        [850, 10],
      ]),
    );
    expect(layers).toHaveLength(1);
    const layer = layers[0]!;
    expect(layer.pBottom).toBeCloseTo(lnCrossing(1000, 950, 7 / 8), 9);
    expect(layer.pTop).toBeCloseTo(lnCrossing(900, 850, 1 / 8), 9);
    expect(layer.zBottom).toBeGreaterThan(0);
    expect(layer.zTop).toBeGreaterThan(layer.zBottom);
  });

  it("merges layers across a thin gap and keeps a thick gap", () => {
    const merged = moistLayers(
      spreads([
        [1000, 1],
        [960, 1],
        [955, 10],
        [950, 1],
        [900, 1],
        [850, 10],
      ]),
    );
    expect(merged).toHaveLength(1);
    expect(merged[0]!.pBottom).toBe(1000);
    expect(merged[0]!.pTop).toBeLessThan(900);
    const apart = moistLayers(
      spreads([
        [1000, 1],
        [960, 1],
        [940, 10],
        [920, 1],
        [880, 1],
        [860, 10],
      ]),
    );
    expect(apart).toHaveLength(2);
    expect(apart[0]!.pBottom).toBeGreaterThan(apart[1]!.pBottom);
    expect(apart[0]!.pTop).toBeGreaterThan(apart[1]!.pBottom);
    // Both are 40–50 hPa deep: a 50 hPa minimum drops both.
    expect(
      moistLayers(
        spreads([
          [1000, 1],
          [960, 1],
          [940, 10],
          [920, 1],
          [880, 1],
          [860, 10],
        ]),
        { minDepthHpa: 50 },
      ),
    ).toEqual([]);
  });

  it("drops a thin layer and stops at the top pressure", () => {
    expect(
      moistLayers(
        spreads([
          [1000, 10],
          [990, 1],
          [980, 10],
        ]),
      ),
    ).toEqual([]);
    const saturated = spreads(
      pressures(1000, 200, 50).map((p) => [p, 1] as const),
    );
    const layers = moistLayers(saturated);
    expect(layers).toHaveLength(1);
    expect(layers[0]!.pBottom).toBe(1000);
    expect(layers[0]!.pTop).toBe(300);
    expect(moistLayers(saturated, { pTop: 500 })[0]!.pTop).toBe(500);
    // A wider threshold finds a layer a narrower one does not.
    const damp = spreads(pressures(1000, 800, 50).map((p) => [p, 4] as const));
    expect(moistLayers(damp)).toEqual([]);
    expect(moistLayers(damp, { spread: 5 })).toHaveLength(1);
  });

  it("skips levels that report no humidity, as if they were not there", () => {
    const reporting: Row[] = [
      { p: 1000, t: 0, td: -10 },
      { p: 950, t: 0, td: -2 },
      { p: 900, t: 0, td: -2 },
      { p: 850, t: 0, td: -10 },
    ];
    // Wind-only significant levels interleaved through the layer.
    const interleaved = column([
      reporting[0]!,
      { p: 975, ws: 5, wd: 200 },
      reporting[1]!,
      { p: 930, ws: 7, wd: 210 },
      { p: 920, ws: 8, wd: 215 },
      reporting[2]!,
      { p: 875, ws: 9, wd: 220 },
      reporting[3]!,
    ]);
    const expected = moistLayers(column(reporting));
    const layers = moistLayers(interleaved);
    expect(layers).toHaveLength(1);
    expect(layers[0]!.pBottom).toBeCloseTo(expected[0]!.pBottom, 9);
    expect(layers[0]!.pTop).toBeCloseTo(expected[0]!.pTop, 9);
    // A silent level inside a moist column leaves it one layer.
    const column1 = spreads(
      pressures(1000, 700, 25).map((p) => [p, 1] as const),
    );
    column1.td[6] = NAN; // 850 hPa
    const one = moistLayers(column1);
    expect(one).toHaveLength(1);
    expect(one[0]!.pBottom).toBe(1000);
    expect(one[0]!.pTop).toBe(700);
  });

  it("splits a layer at a hole wider than a quarter of the pressure", () => {
    const profile = spreads(
      pressures(1000, 500, 25).map((p) => [p, 1] as const),
    );
    // 875 … 725 hPa report a pressure and nothing else: 900 → 700 is a ratio
    // of 1.29 across silent levels.
    for (let index = 0; index < profile.n; index += 1) {
      const p = profile.p[index]!;
      if (p < 900 && p > 700) {
        profile.t[index] = NAN;
        profile.td[index] = NAN;
      }
    }
    const layers = moistLayers(profile);
    expect(layers.map((layer) => [layer.pBottom, layer.pTop])).toEqual([
      [1000, 900],
      [700, 500],
    ]);
  });

  it("bridges an ascent filed on the standard surfaces only", () => {
    const layers = moistLayers(
      spreads([
        [1000, 1],
        [925, 1],
        [850, 1],
        [700, 1],
        [500, 1],
        [400, 1],
        [300, 1],
      ]),
    );
    expect(layers).toHaveLength(1);
    expect(layers[0]!.pBottom).toBe(1000);
    expect(layers[0]!.pTop).toBe(300);
  });

  it("answers an ascent without humidity with nothing", () => {
    expect(moistLayers(column([]))).toEqual([]);
    expect(
      moistLayers(
        column([
          { p: 1000, t: 10 },
          { p: 900, t: 5 },
        ]),
      ),
    ).toEqual([]);
  });
});

describe("inversions", () => {
  const rows: Row[] = [
    { p: 1000, t: 10, td: 5, z: 100 },
    { p: 950, t: 12, td: 5, z: 550 },
    { p: 900, t: 13, td: 5, z: 1000 },
    { p: 850, t: 10, td: 0, z: 1500 },
    { p: 800, t: 7, td: 0, z: 2000 },
    { p: 750, t: 7.5, td: 0, z: 2500 },
    { p: 700, t: 4, td: -5, z: 3000 },
    { p: 650, t: 6, z: 3500 },
    { p: 600, t: 3, td: -20, z: 4100 },
    { p: 400, t: -15, z: 7000 },
    { p: 300, t: -10, z: 9000 },
  ];

  it("finds the rises from the ground up, and which one sits on the ground", () => {
    const found = inversions(column(rows));
    expect(found.map((layer) => [layer.pBottom, layer.pTop])).toEqual([
      [1000, 900],
      [700, 650],
    ]);
    expect(found[0]!.strength).toBeCloseTo(3, 9);
    expect(found[0]!.surfaceBased).toBe(true);
    expect(found[0]!.zBottom).toBe(100);
    expect(found[0]!.zTop).toBe(1000);
    // A level missing only its dew point does not interrupt the rise.
    expect(found[1]!.strength).toBeCloseTo(2, 9);
    expect(found[1]!.surfaceBased).toBe(false);
  });

  it("applies the strength threshold and the top pressure", () => {
    expect(inversions(column(rows), { minStrength: 0.4 })).toHaveLength(3);
    expect(inversions(column(rows), { minStrength: 2.5 })).toHaveLength(1);
    // The warming above 400 hPa only counts once the search reaches it.
    expect(
      inversions(column(rows), { pTop: 300 }).map((layer) => layer.pBottom),
    ).toEqual([1000, 700, 400]);
  });

  it("skips levels that report no temperature, as if they were not there", () => {
    const interleaved = column([
      rows[0]!,
      { p: 975, ws: 5, wd: 200 },
      rows[1]!,
      { p: 925, ws: 6, wd: 205 },
      ...rows.slice(2, 7),
      { p: 675, ws: 9, wd: 230 },
      ...rows.slice(7),
    ]);
    const expected = inversions(column(rows));
    const found = inversions(interleaved);
    expect(found).toHaveLength(expected.length);
    for (const [index, layer] of found.entries()) {
      expect(layer.pBottom).toBe(expected[index]!.pBottom);
      expect(layer.pTop).toBe(expected[index]!.pTop);
      expect(layer.strength).toBeCloseTo(expected[index]!.strength, 9);
      expect(layer.surfaceBased).toBe(expected[index]!.surfaceBased);
    }
    // A level with a pressure and no temperature inside the rise is skipped too.
    const gappy = column(rows);
    gappy.t[1] = NAN; // 950 hPa
    const bridged = inversions(gappy);
    expect(bridged[0]!.pBottom).toBe(1000);
    expect(bridged[0]!.pTop).toBe(900);
    expect(bridged[0]!.strength).toBeCloseTo(3, 9);
    expect(bridged[0]!.surfaceBased).toBe(true);
  });

  it("ends a run at a hole wider than a quarter of the pressure, and only there", () => {
    const sparse: Row[] = [
      { p: 1000, t: 10 },
      { p: 790, t: 14 },
      { p: 750, t: 12 },
    ];
    // Nothing between 1000 and 790 hPa: a sparse ascent, bridged.
    const kept = inversions(column(sparse));
    expect(kept).toHaveLength(1);
    expect(kept[0]!.strength).toBeCloseTo(4, 9);
    // The same two levels with silent ones between them (a ratio of 1.27):
    // a hole, so there is no rise to report.
    const holed = column([
      sparse[0]!,
      { p: 950, ws: 5 },
      { p: 900, ws: 6 },
      { p: 850, ws: 7 },
      ...sparse.slice(1),
    ]);
    expect(inversions(holed)).toEqual([]);
    expect(inversions(column([]))).toEqual([]);
    expect(inversions(column([{ p: 1000, t: 10 }]))).toEqual([]);
  });
});

describe("maxWind", () => {
  it("is the strongest reported wind, the lowest on a tie", () => {
    const profile = column([
      { p: 1000, ws: 5, wd: 180, z: 100 },
      { p: 850, wd: 200, z: 1500 },
      { p: 500, ws: 20, wd: 250 },
      { p: 300, ws: 20, wd: 260, z: 9000 },
      { p: 200, ws: 10, wd: 270, z: 12000 },
    ]);
    const wind = maxWind(profile)!;
    expect(wind.p).toBe(500);
    expect(wind.ws).toBe(20);
    expect(wind.wd).toBe(250);
    // No height at the level itself: interpolated between its neighbours.
    expect(wind.z).toBeGreaterThan(1500);
    expect(wind.z).toBeLessThan(9000);
  });

  it("answers null with no wind at all", () => {
    expect(maxWind(column([{ p: 1000, t: 20 }]))).toBeNull();
    expect(maxWind(column([]))).toBeNull();
  });
});

describe("precipitationReading", () => {
  it("takes a moist layer two kilometres deep and more as enough to rain from", () => {
    // Saturated from the ground to 600 hPa and dry above: the layer ends
    // where the depression crosses 3 °C, a fifth of the way to 500 hPa.
    const reading = precipitationReading(
      column([
        { p: 1000, z: 100, t: 15, td: 14 },
        { p: 900, z: 1000, t: 9, td: 8 },
        { p: 800, z: 2000, t: 3, td: 2 },
        { p: 700, z: 3100, t: -3, td: -4 },
        { p: 600, z: 4300, t: -10, td: -11 },
        { p: 500, z: 5700, t: -19, td: -30 },
        { p: 400, z: 7300, t: -30, td: -45 },
      ]),
    );
    expect(reading.depth).toBeCloseTo(4480, 6);
    expect(reading.deep).toBe(true);
    // It stands on the ground: nothing under it to evaporate into.
    expect(reading.evaporates).toBe(false);
    // The air is above freezing up to half-way between 800 and 700 hPa.
    expect(reading.phase).toBe("rain");
    expect(reading.meltingDepth).toBeCloseTo(2450, 6);
  });

  it("does not take a layer of stratus for one", () => {
    const reading = precipitationReading(
      column([
        { p: 1000, z: 100, t: 5, td: 4.5 },
        { p: 925, z: 750, t: 1, td: 0.5 },
        { p: 850, z: 1450, t: 0, td: -15 },
        { p: 700, z: 3000, t: -8, td: -30 },
        { p: 500, z: 5500, t: -25, td: -45 },
      ]),
    );
    expect(reading.depth).toBeGreaterThan(700);
    expect(reading.depth).toBeLessThan(800);
    expect(reading.deep).toBe(false);
    expect(reading.evaporates).toBe(false);
    // The phase is read under the cloud's top: its last level, 650 m up,
    // is still above freezing, and what the air does above it is not on
    // the way down.
    expect(reading.phase).toBe("rain");
    expect(reading.meltingDepth).toBeCloseTo(650, 6);
  });

  it("says a deep layer over dry air loses what falls from it", () => {
    const rows = [
      { p: 1000, z: 100, t: 20, td: 5 },
      { p: 900, z: 1000, t: 13, td: 0 },
      { p: 800, z: 2000, t: 6, td: 5 },
      { p: 700, z: 3100, t: 0, td: -1 },
      { p: 600, z: 4300, t: -7, td: -8 },
      { p: 500, z: 5700, t: -16, td: -17 },
      { p: 400, z: 7300, t: -28, td: -45 },
    ];
    const reading = precipitationReading(column(rows));
    // From five sixths of the way to 800 hPa up to an eighth past 500 hPa.
    expect(reading.depth).toBeCloseTo(5900 - (1000 + 1000 * (10 / 12)), 6);
    expect(reading.deep).toBe(true);
    expect(reading.evaporates).toBe(true);
    // The same layer over air 5 °C from saturation keeps its rain.
    const damp = rows.map((row, index) => (index < 2 ? { ...row, td: row.t - 5 } : row));
    expect(precipitationReading(column(damp)).evaporates).toBe(false);
  });

  it("leaves middle and high cloud out, however thick", () => {
    // Dry to 700 hPa and saturated from five kilometres up.
    const profile = column([
      { p: 1000, z: 100, t: 20, td: 5 },
      { p: 850, z: 1500, t: 10, td: -5 },
      { p: 700, z: 3100, t: 0, td: -15 },
      { p: 500, z: 5700, t: -18, td: -19 },
      { p: 400, z: 7300, t: -30, td: -31 },
      { p: 300, z: 9300, t: -45, td: -46 },
    ]);
    expect(moistLayers(profile)).toHaveLength(1);
    expect(precipitationReading(profile)).toMatchObject({ depth: 0, deep: false, evaporates: false });
  });

  it("measures from a ground that was filed without a height", () => {
    // The surface level of a classical ascent: ten hectopascals under the
    // 1000 hPa surface, some eighty metres by the hypsometric equation.
    const reading = precipitationReading(
      column([
        { p: 1010, t: 2, td: 1.5 },
        { p: 1000, z: 110, t: 1.4, td: 1 },
        { p: 950, z: 520, t: -1, td: -1.5 },
        { p: 850, z: 1400, t: -7, td: -8 },
        { p: 700, z: 2900, t: -16, td: -17 },
        { p: 500, z: 5400, t: -33, td: -45 },
      ]),
    );
    expect(reading.phase).toBe("mix");
    expect(reading.meltingDepth).toBeGreaterThan(300);
    expect(reading.meltingDepth).toBeLessThan(340);
    // The moist layer starts at that ground and is measured from it too.
    expect(reading.deep).toBe(true);
    expect(reading.depth).toBeGreaterThan(3000);
  });

  it("reads the phase off the temperatures on the way down", () => {
    const phase = (rows: readonly Row[]) => precipitationReading(column(rows));
    // Frozen all the way: snow.
    expect(
      phase([
        { p: 1000, z: 100, t: -5, td: -6 },
        { p: 850, z: 1400, t: -12, td: -13 },
        { p: 700, z: 2900, t: -20, td: -21 },
        { p: 500, z: 5400, t: -35, td: -40 },
      ]),
    ).toMatchObject({ phase: "snow", meltingDepth: 0 });
    // A warm nose over a frozen surface melts it first.
    const nose = (t: number): Row[] => [
      { p: 1000, z: 100, t: -3, td: -4 },
      { p: 925, z: 750, t: -1, td: -2 },
      { p: 850, z: 1450, t, td: t - 1 },
      { p: 700, z: 3000, t: -4, td: -5 },
      { p: 500, z: 5500, t: -20, td: -25 },
    ];
    expect(phase(nose(3))).toMatchObject({ phase: "freezing", meltingDepth: 0 });
    // Half a degree aloft is not a warm nose.
    expect(phase(nose(0.5)).phase).toBe("snow");
    // Under a warm nose a ground at −0.2 °C gets rain.
    expect(phase(nose(3).map((row, index) => (index === 0 ? { ...row, t: -0.2, td: -1 } : row))).phase).toBe("rain");
    // Three hundred metres of air above freezing do not melt it all.
    const mix = phase([
      { p: 1000, z: 100, t: 1.5, td: 1 },
      { p: 950, z: 500, t: -0.5, td: -1 },
      { p: 850, z: 1400, t: -6, td: -7 },
      { p: 700, z: 2900, t: -15, td: -16 },
      { p: 500, z: 5400, t: -32, td: -40 },
    ]);
    expect(mix.phase).toBe("mix");
    expect(mix.meltingDepth).toBeCloseTo(300, 6);
  });

  it("says nothing of the phase where the profile cannot", () => {
    // An ascent that ends warm is rain once it has shown warm air enough.
    expect(
      precipitationReading(
        column([
          { p: 1000, z: 100, t: 25, td: 20 },
          { p: 900, z: 1000, t: 19, td: 15 },
        ]),
      ),
    ).toMatchObject({ phase: "rain", meltingDepth: 900 });
    // Two hundred metres of it are not.
    expect(
      precipitationReading(
        column([
          { p: 1000, z: 100, t: 2, td: 1 },
          { p: 975, z: 300, t: 1, td: 0.5 },
        ]),
      ).phase,
    ).toBeNull();
    // Without heights the depth of the warm air is unknown; a frozen
    // surface needs none.
    expect(
      precipitationReading(
        column([
          { p: 1000, t: 4, td: 3 },
          { p: 850, t: -3, td: -4 },
        ]),
      ).phase,
    ).toBeNull();
    expect(
      precipitationReading(
        column([
          { p: 1000, t: -4, td: -5 },
          { p: 850, t: -9, td: -10 },
        ]),
      ).phase,
    ).toBe("snow");
    // One level is the ground and nothing of the way down.
    expect(precipitationReading(column([{ p: 1000, z: 100, t: -4, td: -5 }])).phase).toBeNull();
  });
});

describe("analyseProfile on real ascents", () => {
  for (const station of [SAMPLE_WAKKANAI, SAMPLE_URUMQI]) {
    it(`holds its invariants for ${station.wmo}`, () => {
      const profile = profileFromSounding(station.soundings[0]!);
      const analysis = analyseProfile(profile);
      expect(analysis.surface).not.toBeNull();
      expect(analysis.parcel).not.toBeNull();
      expectWellFormed(analysis.parcel!);
      expect(analysis.parcel!.p0).toBe(analysis.surface!.p);
      expectWellFormed(liftParcel(profile, "surface")!);
      // The parcel starts at its own potential temperature.
      const theta =
        (analysis.parcel!.t0 + 273.15) * (1000 / analysis.parcel!.p0) ** KAPPA;
      expect(dryAdiabat(theta, analysis.parcel!.p0)).toBeCloseTo(
        analysis.parcel!.t0,
        9,
      );

      let below = Number.POSITIVE_INFINITY;
      for (const layer of analysis.moist) {
        expect(layer.pBottom).toBeLessThanOrEqual(below);
        expect(layer.pBottom - layer.pTop).toBeGreaterThanOrEqual(30);
        expect(layer.pTop).toBeGreaterThanOrEqual(300);
        below = layer.pTop;
      }
      below = Number.POSITIVE_INFINITY;
      for (const layer of analysis.inversions) {
        expect(layer.pBottom).toBeLessThanOrEqual(below);
        expect(layer.pBottom).toBeGreaterThan(layer.pTop);
        expect(layer.pTop).toBeGreaterThanOrEqual(500);
        expect(layer.strength).toBeGreaterThanOrEqual(1);
        below = layer.pTop;
      }
      const wind = analysis.maxWind!;
      for (const ws of profile.ws)
        if (Number.isFinite(ws)) expect(ws).toBeLessThanOrEqual(wind.ws);
    });
  }

  it("reads the ascents the way the chart shows them", () => {
    // Wakkanai: a moist surface parcel with some CAPE, and the jet at the
    // 237 hPa maximum-wind level.
    const wakkanai = profileFromSounding(SAMPLE_WAKKANAI.soundings[0]!);
    expect(liftParcel(wakkanai, "surface")!.cape).toBeGreaterThan(100);
    expect(analyseProfile(wakkanai).maxWind!.p).toBe(237);
    // Urumqi: the night's surface inversion.
    const urumqi = analyseProfile(
      profileFromSounding(SAMPLE_URUMQI.soundings[0]!),
    );
    expect(urumqi.inversions[0]!.surfaceBased).toBe(true);
    expect(urumqi.parcel!.cape).toBe(0);
  });

  it("answers an empty ascent with nothing", () => {
    expect(analyseProfile(column([]))).toEqual({
      surface: null,
      parcel: null,
      moist: [],
      inversions: [],
      maxWind: null,
      precipitation: { depth: 0, deep: false, evaporates: false, phase: null, meltingDepth: NAN },
    });
  });
});
