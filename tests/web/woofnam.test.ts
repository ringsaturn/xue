import { describe, expect, it } from "vitest";

import { FORECAST_MODELS, isCaseOnlyModel, isObservationModel, modelDefaultVariable } from "../../web/src/manifest";
import { REGISTRY } from "../../web/src/registry";
import { parseModelFromSearch } from "../../web/src/urlstate";

describe("WOOF-NAM", () => {
  it("is a live forecast on its own footprint", () => {
    expect(FORECAST_MODELS.woofnam).toMatchObject({
      label: "WOOF-NAM",
      product: "awphys",
      latestFilename: "latest-woofnam.json",
    });
    expect(isObservationModel("woofnam")).toBe(false);
    expect(isCaseOnlyModel("woofnam")).toBe(false);
    // On the model's own grid: nothing to clip, only a place to fly to.
    expect(FORECAST_MODELS.woofnam.domain).toBeUndefined();
    expect(FORECAST_MODELS.woofnam.region).toEqual([-152.99, 12.12, -49.31, 61.35]);
  });

  it("has the forecast core and opens on the rain like any other forecast", () => {
    expect(REGISTRY.sources.woofnam.coreBundleIds).toEqual(["tmp2m", "prate"]);
    expect(FORECAST_MODELS.woofnam.coreBundles).toEqual(["tmp2m", "prate"]);
    expect(modelDefaultVariable("woofnam", "prate")).toBe("prate");
    // No rail of its own: the forecast core's, which carries the rate.
    expect(FORECAST_MODELS.woofnam.railCore).toBeUndefined();
  });

  it("is told apart from the WOOF nest in a link", () => {
    expect(parseModelFromSearch("?model=woofnam")).toBe("woofnam");
    expect(parseModelFromSearch("?model=WOOF-NAM")).toBe("woofnam");
    // The nest is cases only, so a bare link naming it falls back.
    expect(parseModelFromSearch("?model=woof")).toBe("gfs");
  });
});
