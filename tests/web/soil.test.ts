import { describe, expect, it } from "vitest";

import registryJson from "../fixtures/soil-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import {
  FAMILIES,
  SOIL_MOISTURE_CHART_MAX,
  SOIL_TEMPERATURE_CHART_RANGE,
  familyLabel,
  familyLevels,
  familyMembers,
  familyOf,
  familyVariants,
  levelCode,
  scalarLegendRange,
} from "../../web/src/levels";
import { KNOWN_BUNDLE_IDS, SOIL_IDS, isVectorBundle, type BundleVariable } from "../../web/src/manifest";
import { buildPalette, decodeValue } from "../../web/src/palettes";
import { variableSpec } from "../../web/src/variables";
import { registryVariable, rgba, type RegistryEntry } from "./helpers";

/** The committed registry both encoders are held to (`tests/test_soil.py`,
 * and the Rust encoder's unit tests). */
const registry = registryJson as unknown as Record<string, RegistryEntry>;

function bundleVariable(id: string, profile: "quality" | "compact" = "quality"): BundleVariable {
  return registryVariable(id, registry[id]!, registry[id]![profile]);
}

const TEMPERATURE_LAYERS = ["tsoil1", "tsoil2", "tsoil3", "tsoil4"];
const MOISTURE_LAYERS = ["soilw1", "soilw2", "soilw3", "soilw4"];

describe("the soil registry", () => {
  it("knows exactly the variables the encoders register", () => {
    expect([...SOIL_IDS].sort()).toEqual(Object.keys(registry).sort());
    for (const id of SOIL_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(isVectorBundle(id)).toBe(false);
    }
  });

  it("identifies each layer by the surface's value, from the file and from the name", () => {
    for (const id of SOIL_IDS) {
      const identity = identityForParameter(registry[id]!.parameter);
      expect(identity).toEqual({ family: id, level: null, vector: false });
      expect(identityForBundleId(id)).toEqual(identity);
      expect(registeredBundleId(identity)).toBe(id);
      const placed = identifyBundle([{ ...bundleVariable(id), id: "x" }]);
      expect(placed?.identity).toEqual(identity);
    }
    // The layer is the value, however the encoder scaled it: 0.1 m written
    // as 1 × 10⁻¹ or 10 × 10⁻², 1 m as 1 × 10⁰ or 10 × 10⁻¹.
    const base = registry.tsoil1!.parameter;
    const at = (scaleFactorOfFirstFixedSurface: number, scaledValueOfFirstFixedSurface: number) =>
      identityForParameter({ ...base, scaleFactorOfFirstFixedSurface, scaledValueOfFirstFixedSurface })?.family ?? null;
    expect(at(2, 10)).toBe("tsoil2");
    expect(at(1, 4)).toBe("tsoil3");
    expect(at(1, 10)).toBe("tsoil4");
    // A depth no layer starts at, the IFS's own soil level surface (written
    // on input only), and the parameter on another surface are unknown.
    expect(at(0, 2)).toBeNull();
    expect(identityForParameter({ ...base, typeOfFirstFixedSurface: 151 })).toBeNull();
    expect(identityForParameter({ ...base, typeOfFirstFixedSurface: 1 })).toBeNull();
    // The temperature and the water are told apart by the number alone.
    expect(identityForParameter({ ...registry.soilw3!.parameter, parameterNumber: 2 })?.family).toBe("tsoil3");
  });

  it("puts each quantity behind one tile, the level row picking the layer", () => {
    for (const id of TEMPERATURE_LAYERS) expect(familyOf(id)).toBe("tsoil");
    for (const id of MOISTURE_LAYERS) expect(familyOf(id)).toBe("soilw");
    expect(familyOf("tsoil5")).toBeNull();
    expect(FAMILIES.tsoil.surface).toBe("tsoil1");
    expect(FAMILIES.soilw.surface).toBe("soilw1");
    expect(FAMILIES.tsoil.code).toBe("TSOIL");
    expect(FAMILIES.soilw.code).toBe("SOILW");
    expect(familyLevels("tsoil")).toEqual(TEMPERATURE_LAYERS);
    expect(familyLevels("soilw")).toEqual(MOISTURE_LAYERS);
    expect(familyVariants("tsoil")).toEqual([]);
    expect(familyMembers("soilw")).toEqual(MOISTURE_LAYERS);
    // AIFS ships the top two layers only: the row offers what the run has.
    const aifs = new Set(["tsoil1", "tsoil2", "soilw1", "soilw2"]);
    expect(familyLevels("soilw", (id) => aifs.has(id)).filter((id) => aifs.has(id))).toEqual(["soilw1", "soilw2"]);
    // The row reads the layer's index, never a depth: the sources cut the
    // soil at different depths under the same ids.
    expect(SOIL_IDS.map(levelCode)).toEqual(["L1", "L2", "L3", "L4", "L1", "L2", "L3", "L4"]);
    expect(familyLabel("tsoil2")).toBe("Soil temperature, layer 2");
    expect(familyLabel("soilw4")).toBe("Volumetric soil moisture, layer 4");
  });

  it("files the temperature with the temperatures and the water with the moisture", () => {
    for (const id of TEMPERATURE_LAYERS) {
      const spec = variableSpec(id)!;
      expect(spec.group).toBe("temperature");
      expect(spec.family).toBe("tsoil");
      expect(spec.floorIsNoData).toBe(true);
    }
    for (const id of MOISTURE_LAYERS) {
      const spec = variableSpec(id)!;
      expect(spec.group).toBe("moisture");
      expect(spec.family).toBe("soilw");
      expect(spec.floorIsNoData).toBe(true);
    }
    expect(variableSpec("tsoil1")!.code).toBe("TSOIL L1");
    expect(variableSpec("soilw2")!.urlAliases).toContain("soilmoist2");
    expect(variableSpec("tsoil3")!.urlAliases).toContain("soiltemp3");
    expect(variableSpec("tsoil1")!.legend()).toEqual(["50", "30", "10", "-10", "-30", "-50"]);
    expect(variableSpec("soilw1")!.legend()).toEqual(["0.5", "0.4", "0.3", "0.2", "0.1", "0"]);
  });

  it("reads each legend over its chart span", () => {
    for (const id of TEMPERATURE_LAYERS) expect(scalarLegendRange(identityForBundleId(id)!)).toEqual(SOIL_TEMPERATURE_CHART_RANGE);
    for (const id of MOISTURE_LAYERS) expect(scalarLegendRange(identityForBundleId(id)!)).toEqual([0, SOIL_MOISTURE_CHART_MAX]);
  });

  it("paints no soil as nothing and the soil temperature on the air's ramp", () => {
    for (const profile of ["quality", "compact"] as const) {
      const soil = buildPalette(bundleVariable("tsoil1", profile));
      // Code 0, −61 °C, is open water: the map. The next code is ground.
      expect(decodeValue(bundleVariable("tsoil1", profile), 0)).toBe(-61);
      expect(rgba(soil, 0)[3]).toBe(0);
      expect(rgba(soil, 1)[3]).toBe(255);
    }
    const soil = buildPalette(bundleVariable("tsoil2"));
    const air = buildPalette({
      ...bundleVariable("tsoil2"),
      id: "tmp2m",
      parameter: undefined,
      quantization: { type: "linear", offset: -60, scale: 0.5, minimumCode: 0, maximumCode: 220, nodataCode: 255 },
    });
    // The same value is the same colour as the 2 m temperature's.
    for (const celsius of [-30, 0, 15, 35]) {
      const soilCode = Math.round((celsius + 61) / 0.5);
      const airCode = Math.round((celsius + 60) / 0.5);
      expect(rgba(soil, soilCode)).toEqual(rgba(air, airCode));
    }
  });

  it("paints no soil and the IFS's zero over the sea as nothing, wet ground blue", () => {
    for (const profile of ["quality", "compact"] as const) {
      const variable = bundleVariable("soilw1", profile);
      const moisture = buildPalette(variable);
      const { offset, scale } = registry.soilw1![profile];
      const codeOf = (value: number) => Math.round((value - offset) / scale);
      expect(rgba(moisture, 0)[3]).toBe(0);
      expect(decodeValue(variable, codeOf(0))).toBeCloseTo(0, 9);
      expect(rgba(moisture, codeOf(0))[3]).toBe(0);
      // The first code above zero is ground, however dry.
      expect(rgba(moisture, codeOf(0) + 1)[3]).toBeGreaterThan(150);
      // Dry is brown, wet is blue.
      const [rDry, , bDry] = rgba(moisture, codeOf(0.05));
      expect(rDry).toBeGreaterThan(bDry);
      const [rWet, , bWet] = rgba(moisture, codeOf(SOIL_MOISTURE_CHART_MAX));
      expect(bWet).toBeGreaterThan(rWet);
    }
  });
});
