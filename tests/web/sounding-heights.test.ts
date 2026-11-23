import { describe, expect, it } from "vitest";

import { analyseProfile } from "../../web/src/sounding/analysis";
import { fillHeights } from "../../web/src/sounding/heights";
import { SIG, profileFromSounding } from "../../web/src/sounding/profile";
import type { Profile } from "../../web/src/sounding/profile";
import { SAMPLE_URUMQI } from "../../web/src/sounding/sample";

const NAN = Number.NaN;

interface Row {
  p: number;
  t?: number;
  td?: number;
  z?: number;
  ws?: number;
  sig?: number;
}

function column(rows: readonly Row[]): Profile {
  const field = (pick: (row: Row) => number | undefined): Float64Array =>
    Float64Array.from(rows, (row) => pick(row) ?? NAN);
  return {
    n: rows.length,
    p: field((row) => row.p),
    z: field((row) => row.z),
    t: field((row) => row.t),
    td: field((row) => row.td),
    wd: field(() => undefined),
    ws: field((row) => row.ws),
    sig: field((row) => row.sig),
    derived: null,
  };
}

describe("fillHeights", () => {
  it("counts up from the elevation by the hypsometric equation", () => {
    const profile = column([
      { p: 1000, t: 10, sig: SIG.surface },
      { p: 850, t: 10 },
      { p: 700, t: 10 },
    ]);
    expect(fillHeights(profile, 100)).toBe(true);
    const scale = (287.04 * 283.15) / 9.80665;
    expect(profile.z[0]).toBe(100);
    expect(profile.z[1]).toBeCloseTo(100 + scale * Math.log(1000 / 850), 6);
    expect(profile.z[2]).toBeCloseTo(100 + scale * Math.log(1000 / 700), 6);
  });

  it("makes moist air deeper than dry", () => {
    const rows = [
      { p: 1000, t: 28, td: 26, sig: SIG.surface },
      { p: 850, t: 18, td: 17 },
    ];
    const moist = column(rows);
    const dry = column(rows.map(({ td: _, ...row }) => row));
    fillHeights(moist, 0);
    fillHeights(dry, 0);
    expect(moist.z[1]! - dry.z[1]!).toBeGreaterThan(10);
    expect(moist.z[1]! - dry.z[1]!).toBeLessThan(25);
  });

  it("gives a wind-only level its height and leaves one above the last temperature without", () => {
    const profile = column([
      { p: 1000, t: 10, sig: SIG.surface },
      { p: 900, ws: 8 },
      { p: 800, t: 10 },
      { p: 600, ws: 30 },
    ]);
    fillHeights(profile, 0);
    const scale = (287.04 * 283.15) / 9.80665;
    expect(profile.z[1]).toBeCloseTo(scale * Math.log(1000 / 900), 6);
    expect(profile.z[3]).toBeNaN();
  });

  it("ends at a hole in the temperatures but bridges the standard surfaces", () => {
    const holed = column([
      { p: 1000, t: 10, sig: SIG.surface },
      { p: 850, t: 5 },
      { p: 400, t: -30 },
      { p: 300, t: -45 },
    ]);
    expect(fillHeights(holed, 0)).toBe(true);
    expect(holed.z[1]).toBeGreaterThan(1300);
    expect(holed.z[2]).toBeNaN();
    expect(holed.z[3]).toBeNaN();
    const standard = column([
      { p: 1000, t: 10, sig: SIG.surface },
      { p: 850, t: 5 },
      { p: 700, t: -5 },
      { p: 500, t: -20 },
      { p: 300, t: -45 },
    ]);
    fillHeights(standard, 0);
    expect(standard.z[4]).toBeGreaterThan(8500);
  });

  it("derives the freezing level, the lapse rate and the tropopause from them", () => {
    const profile = column([
      { p: 1000, t: 15, sig: SIG.surface },
      { p: 850, t: 5 },
      { p: 700, t: -5 },
      { p: 500, t: -20 },
      { p: 300, t: -45, sig: SIG.tropopause },
    ]);
    fillHeights(profile, 0);
    const derived = profile.derived!;
    expect(derived.freezingLevel).toBeCloseTo((profile.z[1]! + profile.z[2]!) / 2, 6);
    expect(derived.lapse850_500).toBeCloseTo(25 / ((profile.z[3]! - profile.z[1]!) / 1000), 6);
    expect(derived.tropopause).toBe(profile.z[4]);
    expect(derived.pw).toBeNull();
  });

  it("leaves an ascent alone that reports a height, starts aloft or has no elevation", () => {
    const ground = { p: 1000, t: 10, sig: SIG.surface };
    const reported = column([ground, { p: 850, t: 5, z: 1500 }, { p: 700, t: -5 }]);
    expect(fillHeights(reported, 100)).toBe(false);
    expect(reported.z[2]).toBeNaN();
    expect(fillHeights(column([{ p: 98, t: -60 }, { p: 70, t: -58 }]), 100)).toBe(false);
    expect(fillHeights(column([ground, { p: 850, t: 5 }]), null)).toBe(false);
    expect(fillHeights(column([{ p: 1000, sig: SIG.surface }, { p: 850, t: 5 }]), 100)).toBe(false);
    expect(fillHeights(column([]), 100)).toBe(false);
  });

  it("recovers a real ascent's reported heights, and its readings with them", () => {
    const reported = profileFromSounding(SAMPLE_URUMQI.soundings[0]!);
    const stripped = profileFromSounding(SAMPLE_URUMQI.soundings[0]!);
    const ground = reported.z[0]!;
    stripped.z.fill(NAN);
    stripped.derived = null;
    expect(analyseProfile(stripped).inversions[0]!.zTop).toBeNaN();
    expect(fillHeights(stripped, Number.isFinite(ground) ? ground : SAMPLE_URUMQI.elev)).toBe(true);
    let compared = 0;
    for (let index = 0; index < reported.n; index += 1) {
      // Higher up the sonde's own heights drift from its pressures.
      if (reported.p[index]! < 100) break;
      if (!Number.isFinite(reported.z[index]!) || !Number.isFinite(stripped.z[index]!)) continue;
      expect(Math.abs(stripped.z[index]! - reported.z[index]!)).toBeLessThan(20);
      compared += 1;
    }
    expect(compared).toBeGreaterThan(50);
    expect(analyseProfile(stripped).inversions[0]!.zTop).toBeGreaterThan(ground);
  });
});
