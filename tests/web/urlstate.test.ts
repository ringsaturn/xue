import { describe, expect, it } from "vitest";

import { parseModelFromSearch, parseVariableFromSearch, searchForVariable } from "../../web/src/urlstate";

describe("parseVariableFromSearch", () => {
  it("resolves the canonical type names", () => {
    expect(parseVariableFromSearch("?model=gfs&type=wind")).toBe("wind10m");
    expect(parseVariableFromSearch("?model=gfs&type=temp")).toBe("tmp2m");
    expect(parseVariableFromSearch("?model=gfs&type=precip")).toBe("prate");
  });

  it("accepts bundle ids and common aliases, case-insensitively", () => {
    expect(parseVariableFromSearch("?type=wind10m")).toBe("wind10m");
    expect(parseVariableFromSearch("?type=tmp2m")).toBe("tmp2m");
    expect(parseVariableFromSearch("?type=TEMPERATURE")).toBe("tmp2m");
    expect(parseVariableFromSearch("?type=rain")).toBe("prate");
    expect(parseVariableFromSearch("?type=Wind")).toBe("wind10m");
  });

  it("works without a model param and tolerates model casing", () => {
    expect(parseVariableFromSearch("?type=wind")).toBe("wind10m");
    expect(parseVariableFromSearch("?model=GFS&type=wind")).toBe("wind10m");
  });

  it("resolves types for every served model", () => {
    expect(parseVariableFromSearch("?model=ecmwf&type=wind")).toBe("wind10m");
    expect(parseVariableFromSearch("?model=ifs&type=temp")).toBe("tmp2m");
    expect(parseVariableFromSearch("?model=sflux&type=solar")).toBe("dswrf");
    expect(parseVariableFromSearch("?type=radiation")).toBe("dswrf");
    expect(parseVariableFromSearch("?type=DSWRF")).toBe("dswrf");
  });

  it("falls back to null on unknown model, unknown type, or no params", () => {
    expect(parseVariableFromSearch("?model=icon&type=wind")).toBeNull();
    expect(parseVariableFromSearch("?model=gfs&type=vorticity")).toBeNull();
    expect(parseVariableFromSearch("?model=gfs")).toBeNull();
    expect(parseVariableFromSearch("")).toBeNull();
  });
});

describe("parseModelFromSearch", () => {
  it("resolves served models and their aliases, case-insensitively", () => {
    expect(parseModelFromSearch("?model=gfs")).toBe("gfs");
    expect(parseModelFromSearch("?model=ECMWF")).toBe("ecmwf");
    expect(parseModelFromSearch("?model=ifs")).toBe("ecmwf");
    expect(parseModelFromSearch("?model=sflux")).toBe("sflux");
    expect(parseModelFromSearch("?model=GFS-SFLUX")).toBe("sflux");
  });

  it("falls back to the default on unknown or missing model", () => {
    expect(parseModelFromSearch("?model=icon")).toBe("gfs");
    expect(parseModelFromSearch("")).toBe("gfs");
  });
});

describe("searchForVariable", () => {
  it("writes the canonical model and type", () => {
    expect(searchForVariable("wind10m", "")).toBe("?model=gfs&type=wind");
    expect(searchForVariable("tmp2m", "")).toBe("?model=gfs&type=temp");
    expect(searchForVariable("prate", "")).toBe("?model=gfs&type=precip");
  });

  it("rewrites an existing type while preserving unrelated params", () => {
    expect(searchForVariable("wind10m", "?debug=1&type=temp")).toBe("?debug=1&type=wind&model=gfs");
  });

  it("round-trips through parseVariableFromSearch", () => {
    for (const id of ["tmp2m", "prate", "dswrf", "wind10m"] as const) {
      expect(parseVariableFromSearch(searchForVariable(id, ""))).toBe(id);
    }
  });

  it("writes the requested model and round-trips it", () => {
    expect(searchForVariable("prate", "", "ecmwf")).toBe("?model=ecmwf&type=precip");
    expect(parseModelFromSearch(searchForVariable("prate", "", "ecmwf"))).toBe("ecmwf");
    expect(searchForVariable("dswrf", "", "sflux")).toBe("?model=sflux&type=solar");
    expect(parseModelFromSearch(searchForVariable("dswrf", "", "sflux"))).toBe("sflux");
  });
});
