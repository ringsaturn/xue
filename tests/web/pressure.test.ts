import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/pressure-registry.json";

import { KNOWN_BUNDLE_IDS } from "../../web/src/manifest";
import type { LinearQuantization } from "../../web/src/manifest";
import { buildPalette } from "../../web/src/palettes";
import {
  PRESSURE_BUNDLE_IDS,
  PRESSURE_LEVELS,
  isPressureBundle,
  pressureCode,
  pressureLegend,
  pressureLevel,
} from "../../web/src/pressure";

/** The committed registry both encoders are held to
 * (`tests/test_pressure.py`, and the Rust encoder's unit tests). The
 * frontend's own table has to agree with it or a contour is drawn at an
 * interval the codebook was not aligned for. */
interface RegistryEntry {
  contourInterval: number;
  emphasisInterval?: number;
  emphasisContours?: number[];
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

describe("the pressure level registry", () => {
  it("names the same levels the encoders register", () => {
    expect([...PRESSURE_BUNDLE_IDS]).toEqual(Object.keys(registry));
    for (const id of PRESSURE_BUNDLE_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
    }
  });

  it("draws each level at the interval its codebook was aligned for", () => {
    for (const id of PRESSURE_BUNDLE_IDS) {
      const level = PRESSURE_LEVELS[id];
      const entry = registry[id]!;
      expect(level.contourInterval).toBe(entry.contourInterval);
      expect(level.emphasisInterval ?? undefined).toBe(entry.emphasisInterval);
      expect(level.emphasisContours ? [...level.emphasisContours] : undefined).toEqual(
        entry.emphasisContours,
      );
    }
  });

  it("shows a range that is the codebook's own coverage", () => {
    for (const id of PRESSURE_BUNDLE_IDS) {
      const { offset, scale, maximumCode } = registry[id]!.quality;
      expect(PRESSURE_LEVELS[id].range).toEqual([offset, offset + scale * maximumCode]);
    }
  });

  it("puts every contour half a code off, in both profiles' terms", () => {
    // The rule the contour shader rests on: a line that coincides with a code
    // value would light up a whole flat plateau. Restated here because the
    // frontend is where it is *used*, and it is checked against the shipped
    // codebook rather than the frontend's own numbers.
    for (const id of PRESSURE_BUNDLE_IDS) {
      const { offset, scale } = registry[id]!.quality;
      const level = PRESSURE_LEVELS[id];
      const contours = [...(level.emphasisContours ?? [])];
      for (const interval of [level.contourInterval, level.emphasisInterval ?? 0]) {
        if (interval <= 0) continue;
        for (let value = Math.ceil(level.range[0] / interval) * interval; value < level.range[1]; value += interval) {
          contours.push(value);
        }
      }
      expect(contours.length).toBeGreaterThan(8);
      for (const contour of contours) {
        const codeOffset = (contour - offset) / scale;
        expect(codeOffset - Math.floor(codeOffset)).toBeCloseTo(0.5, 9);
      }
    }
  });
});

describe("pressure presentation", () => {
  it("tells contour fields apart from filled ones", () => {
    expect(isPressureBundle("hgt500")).toBe(true);
    expect(isPressureBundle("prmsl")).toBe(true);
    expect(isPressureBundle("tmp2m")).toBe(false);
    expect(pressureLevel("wind10m")).toBeNull();
  });

  it("codes sea level pressure by name and every height by its level", () => {
    expect(pressureCode("prmsl")).toBe("PRMSL MSL");
    expect(pressureCode("hgt500")).toBe("HGT 500MB");
  });

  it("labels the legend from high to low across the codebook", () => {
    const ticks = pressureLegend("hgt500").map(Number);
    expect(ticks).toHaveLength(6);
    expect(ticks[0]!).toBeGreaterThan(ticks[5]!);
    for (const tick of ticks) {
      expect(tick).toBeGreaterThanOrEqual(PRESSURE_LEVELS.hgt500.range[0]);
      expect(tick).toBeLessThanOrEqual(PRESSURE_LEVELS.hgt500.range[1]);
    }
  });

  it("paints a height palette across the level's own range, not the temperature ramp", () => {
    // Every height is far outside the temperature ramp's -60..50, so the
    // fallback would clamp the whole plane to one colour.
    const variable = {
      numericId: 12,
      id: "hgt500" as const,
      label: "500 hPa geopotential height",
      unit: "m",
      quantization: registry.hgt500!.quality,
    };
    const palette = buildPalette(variable);
    const colorAt = (code: number) => [...palette.slice(code * 4, code * 4 + 4)];
    expect(colorAt(0)).not.toEqual(colorAt(254));
    expect(colorAt(60)).not.toEqual(colorAt(200));
    // Reserved codes stay fully transparent, as for every other variable.
    expect(colorAt(255)).toEqual([0, 0, 0, 0]);
  });
});
