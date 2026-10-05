import { describe, expect, it } from "vitest";

import { identityForBundleId, identityForParameter } from "../../web/src/identity";
import {
  CAT_CLASSES,
  CAT_CODEBOOK_MAX,
  ISOBARIC_FILL_IDS,
  bundleLevel,
  catClass,
  familyLabel,
  familyLevels,
  familyMembers,
  familyOf,
  isobaricCode,
  isobaricRange,
  levelCode,
  scalarLegendRange,
} from "../../web/src/levels";
import { CAT_LEVELS, KNOWN_BUNDLE_IDS, type BundleVariable } from "../../web/src/manifest";
import { buildPalette } from "../../web/src/palettes";
import { displayUnit } from "../../web/src/units";
import { parseVariableFromSearch } from "../../web/src/urlstate";
import { variableSpec } from "../../web/src/variables";

/** The codebook the encoders write for `cat<level>`: 0–0.635 m^(2/3) s⁻¹
 * at 0.005 a code. */
function catVariable(id = "cat250"): BundleVariable {
  return {
    numericId: 1,
    id,
    label: "250 hPa clear-air turbulence (EDR)",
    unit: "m^(2/3)/s",
    quantization: { type: "linear", offset: 0, scale: 0.005, minimumCode: 0, maximumCode: 127, nodataCode: 255 },
  };
}

describe("the clear-air turbulence family", () => {
  it("lives on the three jet levels only", () => {
    expect(CAT_LEVELS).toEqual([300, 250, 200]);
    expect(familyLevels("cat")).toEqual(["cat300", "cat250", "cat200"]);
    expect(familyMembers("cat")).toEqual(["cat300", "cat250", "cat200"]);
    for (const id of ["cat300", "cat250", "cat200"]) {
      expect(familyOf(id)).toBe("cat");
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(ISOBARIC_FILL_IDS).toContain(id);
    }
    expect(levelCode("cat250")).toBe("250");
    expect(bundleLevel("cat250")).toBe(250);
    // A level the encoders never derive it on is no member and no level.
    expect(familyOf("cat850")).toBeNull();
    expect(bundleLevel("cat850")).toBeNull();
    expect(variableSpec("cat850")).toBeNull();
    expect(isobaricRange("cat", 850)).toBeNull();
  });

  it("reads the GRIB2 clear-air turbulence parameter on an isobaric surface", () => {
    const block = {
      discipline: 0,
      parameterCategory: 19,
      parameterNumber: 29,
      typeOfFirstFixedSurface: 100,
      scaleFactorOfFirstFixedSurface: 0,
      scaledValueOfFirstFixedSurface: 25000,
    };
    expect(identityForParameter(block)).toEqual({ family: "cat", level: 250, vector: false });
    expect(identityForParameter({ ...block, typeOfFirstFixedSurface: 1, scaledValueOfFirstFixedSurface: 0 })).toBeNull();
    expect(identityForBundleId("cat200")).toEqual({ family: "cat", level: 200, vector: false });
  });

  it("names, codes and spans every member", () => {
    expect(isobaricCode("cat250")).toBe("CAT 250MB");
    expect(familyLabel("cat300")).toBe("300 hPa clear-air turbulence (EDR)");
    expect(isobaricRange("cat", 250)).toEqual([0, CAT_CODEBOOK_MAX]);
    expect(scalarLegendRange({ family: "cat", level: 200, vector: false })).toEqual([0, CAT_CODEBOOK_MAX]);
    const spec = variableSpec("cat250")!;
    expect(spec.family).toBe("cat");
    expect(spec.group).toBe("dynamics");
    expect(spec.title).toEqual(["250 hPa", "Turbulence"]);
    expect(spec.showcaseCode).toBe("CAT 250MB");
  });

  it("classes EDR by the ICAO and WAFS thresholds", () => {
    expect(CAT_CLASSES.map((entry) => entry.from)).toEqual([0.1, 0.2, 0.35, 0.45]);
    expect(catClass(0)).toBe(-1);
    // Annex 3: nil at or under 0.10, light above it.
    expect(catClass(0.1)).toBe(-1);
    expect(catClass(0.105)).toBe(0);
    expect(catClass(0.195)).toBe(0);
    // Moderate from 0.20, the WAFS severe forecast from 0.35, severe from 0.45.
    expect(catClass(0.2)).toBe(1);
    expect(catClass(0.345)).toBe(1);
    expect(catClass(0.35)).toBe(2);
    expect(catClass(0.445)).toBe(2);
    expect(catClass(0.45)).toBe(3);
    expect(catClass(CAT_CODEBOOK_MAX)).toBe(3);
  });

  it("paints one colour per class and nothing for nil", () => {
    const variable = catVariable();
    const palette = buildPalette(variable);
    const rgba = (code: number) => [...palette.subarray(code * 4, code * 4 + 4)];
    // Every code agrees with the class its decoded value falls in.
    for (let code = 0; code <= 127; code += 1) {
      const index = catClass(code * 0.005);
      if (index < 0) {
        expect(rgba(code)[3]).toBe(0);
      } else {
        const entry = CAT_CLASSES[index]!;
        expect(rgba(code)).toEqual([...entry.rgb, entry.alpha]);
      }
    }
    // The edges, by code: 20 is 0.10 (nil), 40 is 0.20 (moderate).
    expect(rgba(20)[3]).toBe(0);
    expect(rgba(21)).toEqual(rgba(39));
    expect(rgba(39)).not.toEqual(rgba(40));
    expect(rgba(69)).not.toEqual(rgba(70));
    expect(rgba(89)).not.toEqual(rgba(90));
    // Severity reads in rising opacity.
    for (let index = 1; index < CAT_CLASSES.length; index += 1) {
      expect(CAT_CLASSES[index]!.alpha).toBeGreaterThan(CAT_CLASSES[index - 1]!.alpha);
    }
    // The no-data code stays unpainted.
    expect(rgba(255)[3]).toBe(0);
  });

  it("keys the legend by intensity class, strongest first, with the EDR unit", () => {
    const spec = variableSpec("cat250")!;
    expect(spec.legend()).toEqual([]);
    expect(spec.legendKey!().map((swatch) => swatch.label)).toEqual([
      "Severe ≥ 0.45",
      "Moderate–severe 0.35–0.45",
      "Moderate 0.20–0.35",
      "Light 0.10–0.20",
    ]);
    expect(displayUnit("m^(2/3)/s")).toBe("m⅔/s");
    expect(displayUnit("m^(2/3) s^-1")).toBe("m⅔/s");
    expect(displayUnit("m/s")).toBe("m/s");
  });

  it("resolves the turbulence spellings", () => {
    expect(parseVariableFromSearch("?type=turbulence")).toBe("cat250");
    expect(parseVariableFromSearch("?type=cat")).toBe("cat250");
    expect(parseVariableFromSearch("?type=turbulence300")).toBe("cat300");
    expect(parseVariableFromSearch("?type=edr200")).toBe("cat200");
  });
});
