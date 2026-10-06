import { describe, expect, it } from "vitest";
import golden from "../fixtures/lapse-golden.json";
import {
  freeAtmosphere,
  LAPSE_RATE,
  SITE_TEMPERATURE_GLSL,
  siteTemperature,
  sortedColumn,
  SURFACE_ANOMALY_DECAY_M,
} from "../../web/src/lapse";

describe("site temperature", () => {
  it("is the evaluated method on every golden cell", () => {
    expect(golden.length).toBe(20);
    for (const item of golden) {
      const { value } = siteTemperature(item.t2m, item.model, item.site, sortedColumn(item.levels));
      expect(value, item.name).toBeCloseTo(item.siteTemperature, 9);
    }
  });

  it("reads Mount Fuji off its GFS column", () => {
    const fuji = golden.find((item) => item.name === "fuji gfs")!;
    expect(fuji.siteTemperature).toBeCloseTo(5.81, 2);
  });

  it("keeps the standard lapse rate below the model ground, column or not", () => {
    const column = sortedColumn([
      { height: 100, temperature: 20 },
      { height: 3000, temperature: 0 },
    ]);
    expect(siteTemperature(15, 1400, 600, null)).toEqual({ value: 15 + LAPSE_RATE * 800, fallback: false });
    expect(siteTemperature(15, 1400, 600, column).value).toBeCloseTo(15 + LAPSE_RATE * 800, 12);
  });

  it("falls back to the standard rate above the model ground without a column", () => {
    expect(siteTemperature(17, 643, 3748, null)).toEqual({ value: 17 - LAPSE_RATE * 3105, fallback: true });
    expect(sortedColumn([{ height: 1500, temperature: 4 }])).toBeNull();
    expect(sortedColumn([{ height: 1500, temperature: 4 }, { height: Number.NaN, temperature: 1 }])).toBeNull();
  });

  it("is continuous at the model ground", () => {
    const column = sortedColumn([
      { height: 0, temperature: 10 },
      { height: 800, temperature: 14 },
      { height: 3000, temperature: 0 },
    ]);
    expect(siteTemperature(10, 500, 500.001, column).value).toBeCloseTo(10, 4);
    expect(siteTemperature(10, 500, 499.999, column).value).toBeCloseTo(10, 4);
  });

  it("interpolates between surfaces and carries on past the ends", () => {
    const column = sortedColumn([
      { height: 3000, temperature: 0 },
      { height: 1000, temperature: 10 },
    ])!;
    expect(freeAtmosphere(column, 2000)).toBeCloseTo(5, 12);
    expect(freeAtmosphere(column, 4000)).toBeCloseTo(-LAPSE_RATE * 1000, 12);
    expect(freeAtmosphere(column, 0)).toBeCloseTo(10 + LAPSE_RATE * 1000, 12);
  });

  it("gives the shader the same constants", () => {
    expect(SITE_TEMPERATURE_GLSL).toContain(LAPSE_RATE.toFixed(4));
    expect(SITE_TEMPERATURE_GLSL).toContain(SURFACE_ANOMALY_DECAY_M.toFixed(1));
  });
});
