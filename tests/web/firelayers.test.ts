/** The fire layers the shell derives (`?derived=`): the 3.9 µm hotspot
 * test (hotspot.ts) on a synthetic disk, and the Fosberg index and the
 * dry-and-gusty alert (firewx.ts) on synthetic surface fields. */

import { describe, expect, it } from "vitest";

import { DERIVED_MAX_CODE } from "../../web/src/composite";
import {
  FFWI_MAX,
  fireWeatherAlert,
  fosbergCode,
  fosbergIndex,
  fosbergPlane,
  relativeHumidity,
} from "../../web/src/firewx";
import { HOTSPOT_THRESHOLDS, backgroundStatistics, dayMask, hotspots } from "../../web/src/hotspot";
import type { BundleVariable, LinearQuantization } from "../../web/src/manifest";
import type { GeoGrid } from "../../web/src/probe";

function linear(id: string, unit: string, offset: number, scale: number, maximumCode = 253): BundleVariable {
  const quantization: LinearQuantization = { type: "linear", offset, scale, minimumCode: 0, maximumCode, nodataCode: 255 };
  return {
    numericId: 1,
    id,
    label: id,
    unit,
    parameter: { discipline: 0, parameterCategory: 0, parameterNumber: 0, typeOfFirstFixedSurface: 1, scaleFactorOfFirstFixedSurface: null, scaledValueOfFirstFixedSurface: null },
    quantization,
  } as BundleVariable;
}

const IR039 = linear("ir039", "K", 180, 0.9);
const IR104 = linear("ir104", "K", 180, 0.6);

function grid(width: number, height: number): GeoGrid {
  return { width, height, firstLongitude: 100, firstLatitude: 48, longitudeStep: 0.04, latitudeStep: -0.04, wraps: false };
}

function encode(variable: BundleVariable, values: number[]): Uint8Array {
  const { offset, scale } = variable.quantization as LinearQuantization;
  return Uint8Array.from(values, (value) => Math.max(1, Math.min(253, Math.round((value - offset) / scale))));
}

/** 01:40 local solar time at 100° E: night over the whole synthetic grid. */
const NIGHT = Date.parse("2026-10-12T18:00:00Z");
/** 13:40 local solar time at 100° E: day. */
const DAY = Date.parse("2026-10-12T06:00:00Z");

const SIZE = 31;
const CENTRE = 15 * SIZE + 15;

/** A clear night background with a little texture, and whatever the
 * caller overrides cell by cell. */
function scene(
  shortOverrides: Record<number, number> = {},
  longOverrides: Record<number, number> = {},
  base: { short: number; long: number } = { short: 287, long: 285 },
) {
  const cells = SIZE * SIZE;
  const short = Array.from({ length: cells }, (_, cell) => shortOverrides[cell] ?? base.short + ((cell % 3) - 1) * 0.9);
  const long = Array.from({ length: cells }, (_, cell) => longOverrides[cell] ?? base.long);
  return {
    shortwave: { grid: grid(SIZE, SIZE), variable: IR039, plane: encode(IR039, short) },
    longwave: { grid: grid(SIZE, SIZE), variable: IR104, plane: encode(IR104, long) },
  };
}

describe("the hotspot test", () => {
  it("finds a fire pixel that stands out of a clear night", () => {
    const { shortwave, longwave } = scene({ [CENTRE]: 330 });
    const out = hotspots(shortwave, longwave, NIGHT);
    expect(out[CENTRE]).toBeGreaterThan(0);
    expect([...out].filter((code) => code > 0)).toHaveLength(1);
  });

  it("grades a stronger fire higher, up to the top code", () => {
    const at = (kelvin: number) => {
      const { shortwave, longwave } = scene({ [CENTRE]: kelvin });
      return hotspots(shortwave, longwave, NIGHT)[CENTRE]!;
    };
    const weak = at(305);
    const strong = at(400);
    expect(weak).toBeGreaterThan(0);
    expect(strong).toBeGreaterThan(weak);
    expect(strong).toBeLessThanOrEqual(DERIVED_MAX_CODE);
  });

  it("ignores a cell less than the minimum anomaly above its background", () => {
    const { shortwave, longwave } = scene({ [CENTRE]: 287 + HOTSPOT_THRESHOLDS.minAnomaly - 2 });
    expect(hotspots(shortwave, longwave, NIGHT)[CENTRE]).toBe(0);
  });

  it("does not call a uniformly hot desert a fire", () => {
    const { shortwave, longwave } = scene({}, {}, { short: 325, long: 305 });
    expect([...hotspots(shortwave, longwave, DAY)].every((code) => code === 0)).toBe(true);
  });

  it("drops a cell the day cloud test calls cloud", () => {
    // A sunlit cloud edge: bright at 3.9 µm, cold at 10.4 µm.
    const { shortwave, longwave } = scene({ [CENTRE]: 330 }, { [CENTRE]: 260 }, { short: 300, long: 295 });
    expect(hotspots(shortwave, longwave, DAY)[CENTRE]).toBe(0);
    // The same cell clear of cloud is a fire.
    const clear = scene({ [CENTRE]: 330 }, {}, { short: 300, long: 295 });
    expect(hotspots(clear.shortwave, clear.longwave, DAY)[CENTRE]).toBeGreaterThan(0);
  });

  it("reads code 0 as no data, not as a 180 K cell", () => {
    const { shortwave, longwave } = scene({ [CENTRE]: 330 });
    shortwave.plane[CENTRE] = 0;
    expect(hotspots(shortwave, longwave, NIGHT)[CENTRE]).toBe(0);
  });

  it("places the terminator by the sun", () => {
    const g = grid(4, 2);
    expect([...dayMask(g, DAY, 5)].every((flag) => flag === 1)).toBe(true);
    expect([...dayMask(g, NIGHT, 5)].every((flag) => flag === 0)).toBe(true);
  });

  it("sums the background around each cell, the cell itself left out", () => {
    const values = Float32Array.from([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    const background = Uint8Array.from([1, 1, 1, 1, 1, 1, 1, 1, 0]);
    const { count, sum, sumSquares } = backgroundStatistics(values, background, 3, 3, false, 3);
    // The centre: every neighbour but the one that is not background.
    expect(count[4]).toBe(7);
    expect(sum[4]).toBe(1 + 2 + 3 + 4 + 6 + 7 + 8);
    expect(sumSquares[4]).toBe(1 + 4 + 9 + 16 + 36 + 49 + 64);
    // A corner: its three neighbours inside the grid.
    expect(count[0]).toBe(3);
    expect(sum[0]).toBe(2 + 4 + 5);
  });
});

describe("fire weather", () => {
  it("takes the relative humidity from temperature and dew point", () => {
    expect(relativeHumidity(20, 20)).toBeCloseTo(100, 6);
    expect(relativeHumidity(30, 10)).toBeCloseTo(28.7, 0);
    expect(relativeHumidity(10, 12)).toBe(100);
  });

  it("follows Fosberg's index", () => {
    // Dry fuel under a 30 mph wind is the scale's top.
    expect(fosbergIndex(30, 0, 30 / 2.2369362920544)).toBeCloseTo(FFWI_MAX, 0);
    // Calm and humid is near nothing; dry and windy is high.
    expect(fosbergIndex(15, 90, 0)).toBeLessThan(5);
    expect(fosbergIndex(30, 15, 10)).toBeGreaterThan(50);
    // More wind, or drier air, never lowers it.
    expect(fosbergIndex(25, 20, 12)).toBeGreaterThan(fosbergIndex(25, 20, 6));
    expect(fosbergIndex(25, 15, 8)).toBeGreaterThan(fosbergIndex(25, 40, 8));
    // Capped at the top.
    expect(fosbergIndex(40, 0, 40)).toBe(FFWI_MAX);
  });

  it("codes the index with nothing at zero", () => {
    expect(fosbergCode(0)).toBe(0);
    expect(fosbergCode(0.1)).toBe(1);
    expect(fosbergCode(FFWI_MAX)).toBe(DERIVED_MAX_CODE);
    expect(fosbergCode(500)).toBe(DERIVED_MAX_CODE);
  });

  const T = linear("tmp2m", "°C", -70, 0.5, 254);
  const TD = linear("dpt2m", "°C", -70, 0.5, 254);
  const GUST = linear("gust", "m/s", 0, 0.25, 254);
  const U = linear("ugrd10m", "m/s", -50, 0.5, 254);
  const V = linear("vgrd10m", "m/s", -50, 0.5, 254);
  const g = grid(2, 1);

  it("raises the alert where it is dry and gusty together", () => {
    // Cell 0: 30 °C over 5 °C (RH ≈ 20 %) with 12 m/s gusts; cell 1: the
    // same air in a 6 m/s gust.
    const out = fireWeatherAlert(
      { grid: g, variable: T, plane: encode(T, [30, 30]) },
      { grid: g, variable: TD, plane: encode(TD, [5, 5]) },
      { grid: g, variable: GUST, plane: encode(GUST, [12, 6]) },
    );
    expect([...out]).toEqual([DERIVED_MAX_CODE, 0]);
    // Moist air in the same gust: no alert.
    const moist = fireWeatherAlert(
      { grid: g, variable: T, plane: encode(T, [30, 30]) },
      { grid: g, variable: TD, plane: encode(TD, [25, 25]) },
      { grid: g, variable: GUST, plane: encode(GUST, [12, 12]) },
    );
    expect([...moist]).toEqual([0, 0]);
  });

  it("puts the index on the temperature's grid", () => {
    const out = fosbergPlane(
      { grid: g, variable: T, plane: encode(T, [30, 15]) },
      { grid: g, variable: TD, plane: encode(TD, [0, 14]) },
      { grid: g, u: U, v: V, uPlane: encode(U, [8, 0]), vPlane: encode(V, [6, 0]) },
    );
    expect(out[0]).toBeGreaterThan(out[1]!);
    expect(out[0]).toBe(fosbergCode(fosbergIndex(30, relativeHumidity(30, 0), 10)));
  });
});
