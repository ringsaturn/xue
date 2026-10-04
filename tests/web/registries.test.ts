import { describe, expect, it } from "vitest";

import oceanJson from "../fixtures/ocean-registry.json";
import surfaceJson from "../fixtures/surface-registry.json";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import { isobaricLegend, scalarLegendRange } from "../../web/src/levels";
import { OCEAN_IDS, SURFACE_DIAGNOSTIC_IDS } from "../../web/src/manifest";
import { decodeValue } from "../../web/src/palettes";
import { registryVariable, type RegistryEntry } from "./helpers";

/** The single-surface scalar registries, each the committed fixture both
 * encoders are held to (`tests/test_surface.py`, `tests/test_ocean.py`, and
 * the Rust encoder's unit tests). The contract below is the same for each;
 * what is particular to one lives in its own test file. */
const REGISTRIES = [
  {
    name: "surface diagnostic",
    registry: surfaceJson as unknown as Record<string, RegistryEntry>,
    ids: SURFACE_DIAGNOSTIC_IDS as readonly string[],
    // Precipitation type is categorical: its legend is a swatch key, not a
    // ramp, so it has no numeric span (`isobaricLegend` is null too).
    withoutLegend: ["ptype"],
    legendPastCodebook: [] as string[],
  },
  {
    name: "ocean",
    registry: oceanJson as unknown as Record<string, RegistryEntry>,
    ids: OCEAN_IDS as readonly string[],
    withoutLegend: [] as string[],
    // The direction's legend closes the circle at 360, which the codebook
    // stops one code short of; every other span sits inside its codebook.
    legendPastCodebook: ["dirpw"],
  },
];

describe.each(REGISTRIES)("the $name registry", ({ registry, ids, withoutLegend, legendPastCodebook }) => {
  it("identifies each field from its parameter block, and from its name before the file is open", () => {
    for (const id of ids) {
      const identity = identityForParameter(registry[id]!.parameter);
      expect(identity).toEqual({ family: id, level: null, vector: false });
      expect(identityForBundleId(id)).toEqual(identity);
      expect(registeredBundleId(identity)).toBe(id);
      // A file named anything at all still reads as the field it carries.
      const placed = identifyBundle([{ ...registryVariable(id, registry[id]!, registry[id]!.quality), id: "x" }]);
      expect(placed?.identity).toEqual(identity);
    }
  });

  it("reads the legend over a span the codebook can hold", () => {
    for (const id of ids) {
      if (withoutLegend.includes(id)) continue;
      const { offset, scale, maximumCode } = registry[id]!.quality;
      const identity = identityForBundleId(id)!;
      const [low, high] = scalarLegendRange(identity)!;
      expect(low).toBeGreaterThanOrEqual(offset);
      if (!legendPastCodebook.includes(id)) expect(high).toBeLessThanOrEqual(offset + scale * maximumCode);
      // Six ticks, high to low, the top one at the chart ceiling.
      const legend = isobaricLegend(identity)!;
      expect(legend).toHaveLength(6);
      expect(Number(legend[0])).toBe(high);
      expect(Number(legend[5])).toBe(low);
      for (let index = 1; index < legend.length; index += 1) {
        expect(Number(legend[index])).toBeLessThan(Number(legend[index - 1]));
      }
    }
  });

  it("decodes every valid code and no reserved one", () => {
    for (const id of ids) {
      const variable = registryVariable(id, registry[id]!, registry[id]!.quality);
      const { offset, scale, maximumCode, nodataCode } = registry[id]!.quality;
      expect(decodeValue(variable, 0)).toBe(offset);
      expect(decodeValue(variable, maximumCode)).toBeCloseTo(offset + scale * maximumCode, 9);
      expect(decodeValue(variable, nodataCode)).toBeNull();
    }
  });
});
