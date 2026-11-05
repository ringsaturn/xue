import { describe, expect, it } from "vitest";

import type { BundleVariable } from "../../web/src/manifest";
import { buildPalette, buildPhasePalette, phaseLegendGradient } from "../../web/src/palettes";
import {
  buildFrostPlane,
  buildPhaseField,
  classifyPhase,
  FROST_PLANE_VARIABLE,
  phaseAnchors,
  PHASE_FREEZING_RAIN,
  PHASE_ICE_PELLETS,
  PHASE_MIX,
  PHASE_RAIN,
  PHASE_ROWS,
  PHASE_SNOW,
  type PhasePlane,
} from "../../web/src/precipphase";
import type { GeoGrid } from "../../web/src/probe";

const TYPE: BundleVariable = {
  numericId: 1,
  id: "ptype",
  label: "ptype",
  unit: "1",
  quantization: { type: "linear", offset: 0, scale: 1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};
const FROZEN: BundleVariable = {
  numericId: 1,
  id: "cpofp",
  label: "cpofp",
  unit: "%",
  quantization: { type: "linear", offset: 0, scale: 1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};
const HEIGHT: BundleVariable = {
  numericId: 1,
  id: "fzlvl",
  label: "fzlvl",
  unit: "m",
  quantization: { type: "linear", offset: 0, scale: 25, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};
const GROUND: BundleVariable = {
  numericId: 1,
  id: "orog",
  label: "orog",
  unit: "m",
  quantization: { type: "linear", offset: 0, scale: 10, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};
const RATE: BundleVariable = {
  numericId: 1,
  id: "prate",
  label: "prate",
  unit: "mm/h",
  quantization: { type: "linear", offset: 0, scale: 0.5, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
};

function grid(width: number, height: number): GeoGrid {
  return { width, height, firstLongitude: 100, firstLatitude: 40, longitudeStep: 0.25, latitudeStep: -0.25, wraps: false };
}

function plane(variable: BundleVariable, width: number, height: number, codes: number[]): PhasePlane {
  return { grid: grid(width, height), variable, plane: Uint8Array.from(codes), geometry: null, tiles: null };
}

function weightAt(weights: Uint8Array, cell: number, phase: number): number {
  return weights[cell * 4 + phase - 1]!;
}

describe("classifyPhase", () => {
  it("keeps the two classes the percent frozen cannot name", () => {
    expect(classifyPhase(3, 100)).toBe(PHASE_FREEZING_RAIN);
    expect(classifyPhase(8, 0)).toBe(PHASE_ICE_PELLETS);
  });

  it("reads ECMWF's own codes", () => {
    expect(classifyPhase(7, null)).toBe(PHASE_MIX);
    expect(classifyPhase(6, null)).toBe(PHASE_SNOW);
    expect(classifyPhase(12, null)).toBe(PHASE_FREEZING_RAIN);
  });

  it("calls a cell a mix when the model says part of it is frozen", () => {
    expect(classifyPhase(1, 50)).toBe(PHASE_MIX);
    expect(classifyPhase(5, 50)).toBe(PHASE_MIX);
    expect(classifyPhase(null, 50)).toBe(PHASE_MIX);
  });

  it("trusts the flag outside the band", () => {
    expect(classifyPhase(5, 100)).toBe(PHASE_SNOW);
    expect(classifyPhase(1, 0)).toBe(PHASE_RAIN);
    expect(classifyPhase(5, 5)).toBe(PHASE_SNOW);
  });

  it("uses the percent frozen where there is no flag", () => {
    expect(classifyPhase(0, 95)).toBe(PHASE_SNOW);
    expect(classifyPhase(0, 0)).toBe(PHASE_RAIN);
    expect(classifyPhase(null, 95)).toBe(PHASE_SNOW);
  });

  it("uses the flag alone without a percent frozen", () => {
    expect(classifyPhase(5, null)).toBe(PHASE_SNOW);
    expect(classifyPhase(1, null)).toBe(PHASE_RAIN);
    expect(classifyPhase(0, null)).toBe(PHASE_RAIN);
  });
});

describe("buildPhaseField", () => {
  it("writes one channel per class and nothing for rain", () => {
    const type = plane(TYPE, 4, 1, [1, 5, 1, 3]);
    const frozen = plane(FROZEN, 4, 1, [0, 100, 50, 0]);
    const { weights } = buildPhaseField(type, frozen);
    expect([...weights.subarray(0, 4)]).toEqual([0, 0, 0, 0]);
    expect(weightAt(weights, 1, PHASE_SNOW)).toBe(255);
    expect(weightAt(weights, 2, PHASE_MIX)).toBe(255);
    expect(weightAt(weights, 3, PHASE_FREEZING_RAIN)).toBe(255);
  });

  it("works from either field alone", () => {
    expect(weightAt(buildPhaseField(plane(TYPE, 1, 1, [5]), null).weights, 0, PHASE_SNOW)).toBe(255);
    expect(weightAt(buildPhaseField(null, plane(FROZEN, 1, 1, [50])).weights, 0, PHASE_MIX)).toBe(255);
  });

  it("leaves cells the base plane's tiles do not hold on the plain ramp", () => {
    const type: PhasePlane = {
      ...plane(TYPE, 4, 2, new Array<number>(8).fill(5)),
      geometry: { tileWidth: 2, tileHeight: 2, columns: 2, rows: 1 },
      tiles: [{ firstColumn: 1, lastColumn: 1, firstRow: 0, lastRow: 0 }],
    };
    const { weights } = buildPhaseField(type, null);
    expect(weightAt(weights, 0, PHASE_SNOW)).toBe(0);
    expect(weightAt(weights, 2, PHASE_SNOW)).toBe(255);
  });

  it("ignores a percent frozen its own tiles do not hold", () => {
    const type = plane(TYPE, 2, 1, [1, 1]);
    const frozen: PhasePlane = {
      ...plane(FROZEN, 2, 1, [50, 50]),
      geometry: { tileWidth: 1, tileHeight: 1, columns: 2, rows: 1 },
      tiles: [{ firstColumn: 1, lastColumn: 1, firstRow: 0, lastRow: 0 }],
    };
    const { weights } = buildPhaseField(type, frozen);
    expect(weightAt(weights, 0, PHASE_MIX)).toBe(0);
    expect(weightAt(weights, 1, PHASE_MIX)).toBe(255);
  });

  it("reads the second field on a coarser grid at the nearest cell", () => {
    const type = plane(TYPE, 4, 1, [1, 1, 1, 1]);
    const frozen: PhasePlane = {
      grid: { ...grid(2, 1), longitudeStep: 0.5 },
      variable: FROZEN,
      plane: Uint8Array.from([0, 50]),
      geometry: null,
      tiles: null,
    };
    const { weights } = buildPhaseField(type, frozen);
    expect(weightAt(weights, 0, PHASE_MIX)).toBe(0);
    expect(weightAt(weights, 2, PHASE_MIX)).toBe(255);
  });
});

describe("buildFrostPlane", () => {
  const value = (code: number) => -1016 + code * 8;

  it("is the isotherm's height above the ground", () => {
    // 2500 and 500 m over ground at 1000 m; the plane only has to hold the
    // crossing, and tops out at 1016 m.
    const height = plane(HEIGHT, 2, 1, [100, 20]);
    const ground = plane(GROUND, 2, 1, [100, 100]);
    const out = buildFrostPlane(height, ground);
    expect(value(out[0]!)).toBe(1016);
    expect(Math.abs(value(out[1]!) + 500)).toBeLessThanOrEqual(8);
  });

  it("reads a column frozen through as below the ground, over sea or land", () => {
    const height = plane(HEIGHT, 2, 1, [0, 0]);
    const ground = plane(GROUND, 2, 1, [0, 100]);
    const out = buildFrostPlane(height, ground);
    expect(value(out[0]!)).toBeLessThan(0);
    expect(out[0]).toBe(out[1]);
  });

  it("gives every frame its own buffer", () => {
    const height = plane(HEIGHT, 1, 1, [1]);
    const ground = plane(GROUND, 1, 1, [0]);
    expect(buildFrostPlane(height, ground)).not.toBe(buildFrostPlane(height, ground));
  });
});

describe("phaseAnchors", () => {
  it("names a zone at its middle and skips a lone cell", () => {
    const type = plane(TYPE, 8, 2, [3, 3, 3, 0, 0, 0, 3, 0, 3, 3, 3, 0, 0, 0, 0, 0]);
    const rate = plane(RATE, 8, 2, new Array<number>(16).fill(2));
    const zones = phaseAnchors(buildPhaseField(type, null), rate, null);
    expect(zones).toHaveLength(1);
    expect(zones[0]!.kind).toBe("freezing-zone");
    expect(zones[0]!.longitude).toBeCloseTo(100.25);
    expect(zones[0]!.latitude).toBeCloseTo(39.875);
  });

  it("names no zone where nothing is falling", () => {
    const type = plane(TYPE, 3, 2, [3, 3, 3, 3, 3, 3]);
    const dry = plane(RATE, 3, 2, [0, 0, 0, 0, 0, 0]);
    expect(phaseAnchors(buildPhaseField(type, null), dry, null)).toEqual([]);
    expect(phaseAnchors(buildPhaseField(type, null), null, null)).toEqual([]);
  });

  it("places the 0 °C label where the freezing line crosses", () => {
    // 200 and 400 m over ground at 300 m: the crossing is about halfway along.
    const height = plane(HEIGHT, 2, 1, [8, 16]);
    const ground = plane(GROUND, 2, 1, [30, 30]);
    const frost = { ...height, variable: FROST_PLANE_VARIABLE, plane: buildFrostPlane(height, ground) };
    const anchors = phaseAnchors(null, null, frost);
    expect(anchors).toHaveLength(1);
    expect(anchors[0]!.kind).toBe("frost-line");
    expect(anchors[0]!.longitude).toBeCloseTo(100.125, 1);
    expect(anchors[0]!.latitude).toBe(40);
  });
});

describe("phase palette", () => {
  it("paints rain with the ordinary ramp", () => {
    const palette = buildPhasePalette(RATE);
    const ordinary = buildPalette(RATE, { family: "prate", level: null } as never);
    expect(palette.length).toBe(256 * PHASE_ROWS * 4);
    for (let code = 1; code < 255; code += 1) {
      expect([...palette.subarray(code * 4, code * 4 + 4)]).toEqual([...ordinary.subarray(code * 4, code * 4 + 4)]);
    }
  });

  it("keeps dry transparent in every row", () => {
    const palette = buildPhasePalette(RATE);
    for (let row = 0; row < PHASE_ROWS; row += 1) expect(palette[row * 256 * 4 + 3]).toBe(0);
  });

  it("gives every class its own key", () => {
    const keys = new Set(Array.from({ length: PHASE_ROWS }, (_, row) => phaseLegendGradient(row)));
    expect(keys.size).toBe(PHASE_ROWS);
  });
});
