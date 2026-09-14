import { describe, expect, it } from "vitest";

import {
  DERIVED_MAX_CODE,
  FRONT_ZONE_GRADIENT,
  WARM_MOIST_INFLOW,
  gaussianSmooth,
  PRECIPITATION_STEPS,
  PRECIPITATION_STEP_BOUNDS,
  nearestCell,
  precipitationStep,
  ramp,
  sameGrid,
  steppedPrecipitationLegend,
  steppedPrecipitationPalette,
  thermalFrontZone,
  warmMoistInflow,
} from "../../web/src/composite";
import type { BundleVariable, LogQuantization } from "../../web/src/manifest";
import { decodeValue, precipitationColor } from "../../web/src/palettes";
import type { GeoGrid } from "../../web/src/probe";

function linear(id: string, offset: number, scale: number, unit = ""): BundleVariable {
  return {
    numericId: 1,
    id,
    label: id,
    unit,
    quantization: { type: "linear", offset, scale, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
  };
}

/** A code of a linear variable for a value, rounded to the nearest. */
function encode(variable: BundleVariable, value: number): number {
  const q = variable.quantization;
  if (q.type !== "linear") throw new Error("linear only");
  return Math.round((value - q.offset) / q.scale);
}

function grid(width: number, height: number, firstLongitude = 130, firstLatitude = 40, step = 0.25): GeoGrid {
  return {
    width,
    height,
    firstLongitude,
    firstLatitude,
    longitudeStep: step,
    latitudeStep: -step,
    wraps: Math.abs(width * step - 360) < 1e-6,
  };
}

const FLUX_U = linear("uqflx850", -63.5, 0.5);
const FLUX_V = linear("vqflx850", -63.5, 0.5);
const THETA_E = linear("thetae850", 230, 0.5, "K");

describe("ramp", () => {
  it("is clamped and smooth between the two ends", () => {
    expect(ramp(0, 10, 20)).toBe(0);
    expect(ramp(10, 10, 20)).toBe(0);
    expect(ramp(15, 10, 20)).toBeCloseTo(0.5);
    expect(ramp(20, 10, 20)).toBe(1);
    expect(ramp(50, 10, 20)).toBe(1);
    expect(ramp(Number.NaN, 10, 20)).toBe(0);
  });
});

describe("nearestCell", () => {
  it("rounds to the nearest cell of a cropped grid and refuses the outside", () => {
    const g = grid(4, 3);
    expect(nearestCell(g, 130, 40)).toBe(0);
    expect(nearestCell(g, 130.6, 39.6)).toBe(2 * 4 + 2);
    expect(nearestCell(g, 129, 40)).toBe(-1);
    expect(nearestCell(g, 130, 38)).toBe(-1);
  });

  it("wraps the columns of a global grid", () => {
    const g = grid(1440, 721, -180, 90);
    expect(nearestCell(g, 180, 90)).toBe(0);
    expect(nearestCell(g, -180.25, 90)).toBe(1439);
    expect(sameGrid(g, grid(1440, 721, -180, 90))).toBe(true);
    expect(sameGrid(g, grid(720, 361, -180, 90, 0.5))).toBe(false);
  });
});

describe("warmMoistInflow", () => {
  it("lights where the flux is strong and the air warm, on the flux grid", () => {
    const g = grid(3, 1);
    const cold = encode(THETA_E, 320);
    const warm = encode(THETA_E, 350);
    // Cell 0: strong flux, warm. Cell 1: strong flux, cold. Cell 2: calm, warm.
    const uPlane = Uint8Array.of(encode(FLUX_U, 30), encode(FLUX_U, 30), encode(FLUX_U, 0));
    const vPlane = Uint8Array.of(encode(FLUX_V, 30), encode(FLUX_V, 30), encode(FLUX_V, 1));
    const plane = Uint8Array.of(warm, cold, warm);
    const out = warmMoistInflow(
      { grid: g, u: FLUX_U, v: FLUX_V, uPlane, vPlane },
      { grid: g, variable: THETA_E, plane },
      WARM_MOIST_INFLOW,
      0,
    );
    expect(out[0]).toBe(DERIVED_MAX_CODE);
    expect(out[1]).toBe(0);
    expect(out[2]).toBe(0);
  });

  it("reads the θe off its own, coarser grid and treats no-data as nothing", () => {
    const fine = grid(4, 2, 130, 40, 0.25);
    const coarse = grid(2, 1, 130, 40, 0.5);
    const strong = encode(FLUX_U, 40);
    const uPlane = new Uint8Array(8).fill(strong);
    const vPlane = new Uint8Array(8).fill(encode(FLUX_V, 0));
    uPlane[3] = 255;
    // West half warm, east half cold, on the coarse grid.
    const plane = Uint8Array.of(encode(THETA_E, 350), encode(THETA_E, 320));
    const out = warmMoistInflow(
      { grid: fine, u: FLUX_U, v: FLUX_V, uPlane, vPlane },
      { grid: coarse, variable: THETA_E, plane },
      WARM_MOIST_INFLOW,
      0,
    );
    expect(out[0]).toBe(DERIVED_MAX_CODE);
    expect(out[2]).toBe(0);
    expect(out[3]).toBe(0);
  });
});

describe("gaussianSmooth", () => {
  it("leaves a uniform field alone and spreads a spike without losing its mass", () => {
    const width = 9;
    const height = 3;
    const flat = new Float32Array(width * height).fill(7);
    expect([...gaussianSmooth(flat, width, height, false, 1)].every((v) => Math.abs(v - 7) < 1e-5)).toBe(true);
    const spike = new Float32Array(width * height);
    spike[width + 4] = 1;
    const out = gaussianSmooth(spike, width, height, false, 1);
    expect(out[width + 4]).toBeLessThan(1);
    expect(out[width + 3]).toBeGreaterThan(0);
    expect(out[width + 3]).toBeCloseTo(out[width + 5]!);
  });

  it("ignores no-data cells instead of spreading them", () => {
    const width = 5;
    const values = Float32Array.of(1, 1, Number.NaN, 1, 1);
    const out = gaussianSmooth(values, width, 1, false, 1);
    expect(out[1]).toBeCloseTo(1);
    expect(out[2]).toBeCloseTo(1);
    const gone = new Float32Array(width).fill(Number.NaN);
    expect(Number.isNaN(gaussianSmooth(gone, width, 1, false, 1)[2]!)).toBe(true);
  });
});

describe("thermalFrontZone", () => {
  it("draws a band along a sharp θe step and nothing over uniform air", () => {
    const width = 21;
    const height = 5;
    const g = grid(width, height, 130, 36);
    const plane = new Uint8Array(width * height);
    for (let row = 0; row < height; row += 1) {
      for (let column = 0; column < width; column += 1) {
        plane[row * width + column] = encode(THETA_E, column < 10 ? 345 : 325);
      }
    }
    const middle = 2 * width;
    const raw = thermalFrontZone({ grid: g, variable: THETA_E, plane }, FRONT_ZONE_GRADIENT, 0);
    expect(raw[middle + 10]).toBeGreaterThan(0);
    expect(raw[middle + 9]).toBeGreaterThan(0);
    expect(raw[middle + 8]).toBe(0);
    expect(raw[middle + 11]).toBe(0);
    // Smoothed, the band widens over the step but stays a band.
    const out = thermalFrontZone({ grid: g, variable: THETA_E, plane });
    expect(out[middle + 10]).toBeGreaterThan(0);
    expect(out[middle + 7]).toBeGreaterThan(0);
    expect(out[middle + 1]).toBe(0);
    expect(out[middle + 19]).toBe(0);
    // The edge rows and columns of a cropped grid carry no difference.
    expect(out[10]).toBe(0);
    expect(out[middle]).toBe(0);
  });

  it("is silent on a plane of no data", () => {
    const g = grid(5, 5);
    const out = thermalFrontZone({ grid: g, variable: THETA_E, plane: new Uint8Array(25).fill(255) });
    expect(out.every((code) => code === 0)).toBe(true);
  });
});

describe("stepped precipitation key", () => {
  const prate: BundleVariable = {
    numericId: 1,
    id: "prate",
    label: "Precipitation rate",
    unit: "mm/h",
    quantization: {
      type: "log1p",
      trace: 0.01,
      scale: 0.05,
      maximum: 128,
      minimumCode: 1,
      maximumCode: 253,
      zeroCode: 0,
      overflowCode: 254,
      nodataCode: 255,
    } satisfies LogQuantization,
  };

  it("bands a rate by its lower bound", () => {
    expect(precipitationStep(0)).toBe(-1);
    expect(precipitationStep(0.05)).toBe(-1);
    expect(precipitationStep(0.1)).toBe(0);
    expect(precipitationStep(3.9)).toBe(2);
    expect(precipitationStep(64)).toBe(PRECIPITATION_STEPS.length - 1);
  });

  it("takes each band's colour from the viewer's own precipitation ramp", () => {
    for (const [step, [from, tone]] of PRECIPITATION_STEPS.entries()) {
      const to = PRECIPITATION_STEP_BOUNDS[step + 1] ?? from * 2;
      const [r, g, b] = precipitationColor(Math.sqrt(from * to));
      expect(tone).toEqual([r, g, b]);
    }
  });

  it("colours every code by its band, opaque, and leaves dry and reserved codes clear", () => {
    const palette = steppedPrecipitationPalette(prate);
    expect(palette[0 * 4 + 3]).toBe(0);
    expect(palette[255 * 4 + 3]).toBe(0);
    // The overflow code is a rate past the codebook's ceiling: the top band.
    for (let code = 1; code <= 254; code += 1) {
      const rate = decodeValue(prate, code)!;
      const step = precipitationStep(rate);
      const alpha = palette[code * 4 + 3];
      if (step < 0) {
        expect(alpha).toBe(0);
      } else {
        expect(alpha).toBe(255);
        expect([palette[code * 4], palette[code * 4 + 1], palette[code * 4 + 2]]).toEqual([...PRECIPITATION_STEPS[step]![1]]);
      }
    }
  });

  it("lays the legend out as equal bands, strongest at the top", () => {
    const key = steppedPrecipitationLegend();
    expect(key.labels).toEqual(["50", "20", "10", "5", "2", "1", "0.1"]);
    const top = PRECIPITATION_STEPS[PRECIPITATION_STEPS.length - 1]![1];
    const bottom = PRECIPITATION_STEPS[0]![1];
    expect(key.gradient.startsWith(`linear-gradient(to bottom, rgb(${top.join(", ")}) 0.00% 14.29%`)).toBe(true);
    expect(key.gradient.endsWith(`rgb(${bottom.join(", ")}) 85.71% 100.00%)`)).toBe(true);
  });
});
