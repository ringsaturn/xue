import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/isobaric-registry.json";

import {
  FAMILIES,
  ISOBARIC_FAMILIES,
  ISOBARIC_FILL_IDS,
  bundleLevel,
  familyMembers,
  familyOf,
  isobaricCode,
  isobaricLegend,
  isobaricRange,
  levelCode,
  temperatureLegendRange,
  temperaturePaletteDomain,
  vectorMaxMagnitude,
} from "../../web/src/levels";
import { identityForBundleId } from "../../web/src/identity";
import {
  KNOWN_BUNDLE_IDS,
  ISOBARIC_LEVELS,
  VECTOR_BUNDLES,
  isVectorBundle,
  vectorComponents,
  type IsobaricScalarBundleId,
  type LinearQuantization,
} from "../../web/src/manifest";
import { buildPalette, buildVapourFluxPalette, buildWindFieldPalette, legendGradient } from "../../web/src/palettes";
import type { BundleVariable } from "../../web/src/manifest";

/** The committed registry both encoders are held to
 * (`tests/test_isobaric.py`, and the Rust encoder's unit tests). */
interface RegistryEntry {
  label: string;
  unit: string;
  quality: LinearQuantization;
  compact: LinearQuantization;
}

const registry = registryJson as unknown as Record<string, RegistryEntry>;

describe("the isobaric family registry", () => {
  it("knows every variable the encoders register, and nothing else", () => {
    const componentIds = Object.values(VECTOR_BUNDLES)
      .filter(([u]) => u !== "ugrd10m")
      .flatMap(([u, v]) => [u, v]);
    const scalarIds = ISOBARIC_FILL_IDS.filter((id) => !isVectorBundle(id));
    expect([...scalarIds, ...componentIds].sort()).toEqual(Object.keys(registry).sort());
    for (const id of ISOBARIC_FILL_IDS) expect(KNOWN_BUNDLE_IDS).toContain(id);
  });

  it("shows each filled scalar over its codebook's own coverage", () => {
    for (const id of ISOBARIC_FILL_IDS.filter((id) => !isVectorBundle(id)) as IsobaricScalarBundleId[]) {
      const { offset, scale, maximumCode } = registry[id]!.quality;
      const identity = identityForBundleId(id)!;
      const [low, high] = isobaricRange(identity.family, identity.level)!;
      expect(low).toBe(offset);
      expect(high).toBeCloseTo(offset + scale * maximumCode, 9);
    }
  });

  it("keeps the temperature ramp absolute up to 700 hPa and windowed above", () => {
    for (const level of ISOBARIC_LEVELS) {
      const [low, high] = temperaturePaletteDomain(level);
      if (level >= 700) expect([low, high]).toEqual([-60, 50]);
      else expect(high - low).toBe(60);
      // The legend never claims a value the codebook cannot hold.
      const [min, max] = isobaricRange("tmp", level)!;
      const [legendLow, legendHigh] = temperatureLegendRange(level);
      expect(legendLow).toBeGreaterThanOrEqual(Math.max(min, low));
      expect(legendHigh).toBeLessThanOrEqual(Math.min(max, high));
    }
    expect(temperatureLegendRange(850)).toEqual([-60, 45]);
    expect(temperatureLegendRange(500)).toEqual([-60, 0]);
  });

  it("places every bundle in exactly one family, with the surface member first", () => {
    expect(familyOf("tmp2m")).toBe("tmp");
    expect(familyOf("wind10m")).toBe("wind");
    expect(familyOf("prmsl")).toBe("hgt");
    expect(familyOf("prate")).toBeNull();
    expect(familyOf("dswrf")).toBeNull();
    for (const family of ISOBARIC_FAMILIES) {
      const members = familyMembers(family);
      expect(members[0]).toBe(FAMILIES[family].surface ?? `${family}1000`);
      expect(members.length).toBe(ISOBARIC_LEVELS.length + (FAMILIES[family].surface ? 1 : 0));
      for (const member of members) expect(familyOf(member)).toBe(family);
    }
    expect(bundleLevel("tmp850")).toBe(850);
    expect(bundleLevel("tmp2m")).toBeNull();
    expect(bundleLevel("qflux700")).toBe(700);
    expect(levelCode("tmp2m")).toBe("2M");
    expect(levelCode("wind10m")).toBe("10M");
    expect(levelCode("prmsl")).toBe("MSL");
    expect(levelCode("rh700")).toBe("700");
  });

  it("writes the instrument code from the family and the level", () => {
    expect(isobaricCode("tmp850")).toBe("TMP 850MB");
    expect(isobaricCode("rh700")).toBe("RH 700MB");
    expect(isobaricCode("wind850")).toBe("WIND 850MB");
    expect(isobaricCode("qflux850")).toBe("QFLUX 850MB");
  });

  it("gives every vector bundle its components and a palette ceiling", () => {
    expect(vectorComponents("wind850")).toEqual(["ugrd850", "vgrd850"]);
    expect(vectorComponents("qflux850")).toEqual(["uqflx850", "vqflx850"]);
    expect(vectorComponents("wind10m")).toEqual(["ugrd10m", "vgrd10m"]);
    expect(vectorComponents("tmp850")).toBeNull();
    expect(vectorMaxMagnitude("wind", null)).toBe(40);
    expect(vectorMaxMagnitude("wind", 850)).toBeGreaterThan(40);
    expect(vectorMaxMagnitude("wind", 250)).toBeGreaterThan(vectorMaxMagnitude("wind", 850));
    expect(vectorMaxMagnitude("qflux", 850)).toBe(50);
  });

  it("builds six legend ticks, high to low, for every member", () => {
    for (const id of ISOBARIC_FILL_IDS) {
      const legend = isobaricLegend(identityForBundleId(id)!)!;
      expect(legend).toHaveLength(6);
      const values = legend.map(Number);
      for (let index = 1; index < values.length; index += 1) expect(values[index]!).toBeLessThan(values[index - 1]!);
    }
    expect(isobaricLegend({ family: "rh", level: 850, vector: false })).toEqual(["100", "80", "60", "40", "20", "0"]);
    expect(isobaricLegend({ family: "qflux", level: 850, vector: true })![0]).toBe("50");
  });
});

describe("the upper-air palettes", () => {
  // Variables are numbered 1..n *per file*, so a registry entry has no
  // numericId of its own to lend: a bundle of one variable numbers it 1.
  function variable(id: IsobaricScalarBundleId): BundleVariable {
    const entry = registry[id]!;
    return { numericId: 1, id, label: entry.label, unit: entry.unit, quantization: entry.quality };
  }

  it("paints the isobaric temperature with the surface ramp where the codebooks overlap", () => {
    const surface: BundleVariable = {
      numericId: 1,
      id: "tmp2m",
      label: "2 m temperature",
      unit: "°C",
      quantization: { type: "linear", offset: -60, scale: 0.5, minimumCode: 0, maximumCode: 220, nodataCode: 255 },
    };
    const surfacePalette = buildPalette(surface);
    const upperPalette = buildPalette(variable("tmp850"));
    // 0 °C is code 120 at 2 m and code 140 at 850 hPa: the same colour.
    expect([...upperPalette.slice(140 * 4, 140 * 4 + 4)]).toEqual([...surfacePalette.slice(120 * 4, 120 * 4 + 4)]);
    // The reserved code stays unpainted.
    expect(upperPalette[255 * 4 + 3]).toBe(0);
  });

  it("paints relative humidity opaque and specific humidity transparent when dry", () => {
    const humidity = buildPalette(variable("rh850"));
    expect(humidity[3]).toBeGreaterThan(200);
    expect(humidity[200 * 4 + 3]).toBe(255);
    const specific = buildPalette(variable("spfh850"));
    expect(specific[3]).toBe(0);
    expect(specific[254 * 4 + 3]).toBe(255);
  });

  it("spreads the temperature ramp over an unrecognized field's own codebook", () => {
    // A layer this build has no chart knowledge for still has to be legible:
    // the ramp is laid over what the file says its codes mean, not over
    // -60..50 °C, which a wind gust in m/s would clamp to one colour.
    const gust: BundleVariable = {
      numericId: 1,
      id: "gust10m",
      label: "10 m wind gust",
      unit: "m/s",
      quantization: { type: "linear", offset: 0, scale: 0.4, minimumCode: 0, maximumCode: 250, nodataCode: 255 },
    };
    const palette = buildPalette(gust, null);
    const colorAt = (code: number) => [...palette.slice(code * 4, code * 4 + 4)];
    // The whole codebook is used, end to end, and is not one flat colour.
    expect(colorAt(0)).not.toEqual(colorAt(250));
    expect(colorAt(60)).not.toEqual(colorAt(180));
    // The temperature ramp's ends, at the codebook's ends.
    expect(colorAt(0)).toEqual([39, 25, 89, 255]);
    expect(colorAt(250)).toEqual([112, 20, 65, 255]);
    // Reserved codes stay unpainted, as for every other variable.
    expect(colorAt(255)).toEqual([0, 0, 0, 0]);
    // Without the identity argument the id is read as the naming convention,
    // which says nothing about this one either.
    expect([...buildPalette(gust)]).toEqual([...palette]);
  });

  it("stretches the wind ramp to a higher ceiling and keeps the flux ramp's own", () => {
    const surface = buildWindFieldPalette(40);
    const upper = buildWindFieldPalette(80);
    // The same fraction of the ceiling is the same colour.
    expect([...upper.slice(128 * 4, 128 * 4 + 3)]).toEqual([...surface.slice(128 * 4, 128 * 4 + 3)]);
    const flux = buildVapourFluxPalette(50);
    expect(flux[3]).toBe(0);
    expect(flux[255 * 4 + 3]).toBe(255);
    const gradient = legendGradient(flux);
    expect(gradient.startsWith("linear-gradient(to bottom, rgba(")).toBe(true);
    expect(gradient.split("rgba(").length - 1).toBe(12);
  });
});
