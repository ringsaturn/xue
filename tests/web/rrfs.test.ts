import { describe, expect, it } from "vitest";

import { identityForParameter } from "../../web/src/identity";
import { aerosolChart, familyMembers, familyOf } from "../../web/src/levels";
import { FORECAST_MODELS, KNOWN_BUNDLE_IDS, SMOKE_DUST_IDS, type BundleAerosol, type BundleParameter } from "../../web/src/manifest";
import { REGISTRY } from "../../web/src/registry";

type Entry = { parameter: BundleParameter; aerosol?: BundleAerosol };
const entry = (id: (typeof SMOKE_DUST_IDS)[number]) => REGISTRY.variables[id] as unknown as Entry;

describe("RRFS", () => {
  it("is its own footprint and opens with the smoke on the rail", () => {
    // RRFS never had a resampled run to clip; HRRR keeps its domain for those.
    expect(FORECAST_MODELS.rrfs.domain).toBeUndefined();
    expect(FORECAST_MODELS.hrrr.domain).toBeDefined();
    expect(FORECAST_MODELS.rrfs.region).toEqual(FORECAST_MODELS.hrrr.region);
    expect(FORECAST_MODELS.rrfs.railCore).toContain("smokesfc");
    expect(FORECAST_MODELS.rrfs.latestFilename).toBe("latest-rrfs.json");
  });

  it("tells each smoke and dust field by its identity and charts it", () => {
    for (const id of SMOKE_DUST_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      const { parameter, aerosol } = entry(id);
      expect(identityForParameter(parameter, undefined, aerosol)).toEqual({ family: id, level: null, vector: false });
      expect(aerosolChart(id)).not.toBeNull();
    }
  });

  it("keeps the column optical thickness apart from GEFS-Aerosols' optical depth", () => {
    const aotk = entry("aotk");
    expect(aotk.aerosol).toBeUndefined();
    expect(identityForParameter(aotk.parameter)?.family).toBe("aotk");
    expect(familyOf("aotk")).toBe("aod");
  });

  it("puts the two dust cuts under the smoke's tile", () => {
    expect(familyMembers("smoke")).toEqual(["smokesfc", "dustfine", "dustcoarse"]);
  });
});
