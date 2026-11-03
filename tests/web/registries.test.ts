import { describe, expect, it } from "vitest";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId } from "../../web/src/identity";
import { isobaricLegend, scalarLegendRange } from "../../web/src/levels";
import { COMPOSITE_BUNDLES, FORECAST_MODELS, OCEAN_IDS, SOIL_IDS, SURFACE_DIAGNOSTIC_IDS, VECTOR_BUNDLES, VOLUME_BUNDLE_LEVELS } from "../../web/src/manifest";
import { decodeValue } from "../../web/src/palettes";
import { REGISTRY } from "../../web/src/registry";
import { variableSpec } from "../../web/src/variables";
import { REGISTRY_DOCUMENT, publishedBundleIds, registryEntries, registryVariable } from "./helpers";

/** Bundles a source publishes that have no row in the variable table on
 * purpose: the radar quality index draws generically, and the 3D
 * reflectivity is the volume layer's (volume.ts), never a rail field. */
const GENERIC_BUNDLE_IDS = new Set(["rqi", "refl3d"]);

describe("the shell's registry", () => {
  it("is the registry document, trimmed", () => {
    for (const [id, entry] of Object.entries(REGISTRY.variables)) {
      const whole = REGISTRY_DOCUMENT.variables[id]!;
      expect(entry.parameter).toEqual(whole.parameter);
      expect(entry.quality).toEqual(whole.quantization!.quality);
    }
    for (const [id, entry] of Object.entries(REGISTRY_DOCUMENT.variables)) {
      if (entry.quantization) expect(REGISTRY.variables).toHaveProperty(id);
    }
  });

  it("names a dataset for every source, under the source's own label and product", () => {
    for (const id of Object.keys(REGISTRY_DOCUMENT.sources)) {
      const info = FORECAST_MODELS[id as keyof typeof FORECAST_MODELS];
      const source = REGISTRY.sources[id as keyof typeof REGISTRY.sources];
      expect(info).toBeDefined();
      expect(info.label).toBe(source.label);
      expect(info.product).toBe(source.product);
      expect(info.latestFilename).toBe(source.latestFilename ?? undefined);
      expect(info.observation === true).toBe(source.observation);
    }
  });

  it("carries the members of every vector, composite and volume bundle", () => {
    for (const [id, bundle] of Object.entries(REGISTRY_DOCUMENT.bundles)) {
      if (bundle.kind === "vector") expect(VECTOR_BUNDLES).toHaveProperty(id, bundle.members);
      if (bundle.kind === "composite" && bundle.members!.length === 3) expect(COMPOSITE_BUNDLES).toHaveProperty(id, bundle.members);
      if (bundle.kind === "volume") expect(VOLUME_BUNDLE_LEVELS.get(id)).toBe(bundle.members!.length);
    }
  });

  it("has chart knowledge for every bundle a source publishes", () => {
    // A new bundle cannot ship without a row in variables.ts (and so a
    // palette, a legend and a ground) unless it is listed as generic.
    for (const id of publishedBundleIds()) {
      if (GENERIC_BUNDLE_IDS.has(id)) continue;
      expect(variableSpec(id), id).not.toBeNull();
    }
  });
});

/** The single-surface scalar families, read off the registry document.
 * The contract below is the same for each; what is particular to one
 * lives in its own test file. */
const REGISTRIES = [
  {
    name: "surface diagnostic",
    registry: registryEntries(SURFACE_DIAGNOSTIC_IDS),
    ids: SURFACE_DIAGNOSTIC_IDS as readonly string[],
    // Precipitation type is categorical: its legend is a swatch key, not a
    // ramp, so it has no numeric span (`isobaricLegend` is null too).
    withoutLegend: ["ptype"],
    legendPastCodebook: [] as string[],
  },
  {
    name: "ocean",
    registry: registryEntries(OCEAN_IDS),
    ids: OCEAN_IDS as readonly string[],
    withoutLegend: [] as string[],
    // The directions' legend closes the circle at 360, which the codebook
    // stops one code short of; every other span sits inside its codebook.
    legendPastCodebook: ["dirpw", "wvdir", "swdir"],
  },
  {
    name: "soil",
    registry: registryEntries(SOIL_IDS),
    ids: SOIL_IDS as readonly string[],
    withoutLegend: [] as string[],
    legendPastCodebook: [] as string[],
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
