import { describe, expect, it } from "vitest";

import { equivalentPotentialTemperature } from "../../../web/src/crosssection/derive";
import { sectionLegend } from "../../../web/src/crosssection/ramps";
import { fillCodes, mapFill } from "../../../web/src/workbench/mapfield";

/** A codebook of `value = offset + code * scale`, the top code none. */
function table(offset: number, scale: number): Float32Array {
  const out = new Float32Array(256);
  for (let code = 0; code < 256; code += 1) out[code] = code === 255 ? Number.NaN : offset + code * scale;
  return out;
}

/** The value a fill code stands for. */
function valueOf(fill: Parameters<typeof sectionLegend>[0], code: number): number {
  const { min, max } = sectionLegend(fill);
  return min + ((max - min) * code) / 253;
}

describe("the map's field", () => {
  it("draws the wind's speed for the wind across a line", () => {
    expect(mapFill("cross")).toBe("wind");
    expect(mapFill("rh")).toBe("rh");
  });

  it("puts a value on the fill's own scale, a cell none of whose inputs is read left as none", () => {
    const tmp = table(-85, 0.5);
    const codes = fillCodes("tmp", [Uint8Array.of(170, 255)], [tmp], 500);
    expect(valueOf("tmp", codes[0]!)).toBeCloseTo(0, 0);
    expect(codes[1]).toBe(255);
  });

  it("reads the wind's speed off its two components", () => {
    const wind = table(-100, 1);
    const codes = fillCodes("wind", [Uint8Array.of(130), Uint8Array.of(140)], [wind, wind], 500);
    expect(valueOf("wind", codes[0]!)).toBeCloseTo(50, 0);
  });

  it("has the equivalent potential temperature of a temperature and a humidity", () => {
    const tmp = table(-85, 0.5);
    const rh = table(0, 1);
    const codes = fillCodes("thetae", [Uint8Array.of(190), Uint8Array.of(100)], [tmp, rh], 850);
    expect(valueOf("thetae", codes[0]!)).toBeCloseTo(equivalentPotentialTemperature(10, 10, 850), -0.5);
  });

  it("holds a value past the ramp's ends at its ends", () => {
    const codes = fillCodes("tmp", [Uint8Array.of(0, 254)], [table(-200, 2)], 500);
    expect([...codes]).toEqual([0, 253]);
  });
});
