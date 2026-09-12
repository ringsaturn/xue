import { describe, expect, it } from "vitest";

import {
  parseCameraFromHash,
  parseLinesFromSearch,
  parseModelFromSearch,
  parseParticlesFromSearch,
  parseResolutionFromSearch,
  parseUseH264FromSearch,
  parseVariableFromSearch,
  searchForVariable,
  searchWithLines,
  searchWithParticles,
} from "../../web/src/urlstate";

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

  it("names every isobaric field by its id and a chart reader's spellings", () => {
    expect(parseVariableFromSearch("?type=tmp850")).toBe("tmp850");
    expect(parseVariableFromSearch("?type=T850")).toBe("tmp850");
    expect(parseVariableFromSearch("?type=temp500")).toBe("tmp500");
    expect(parseVariableFromSearch("?type=rh700")).toBe("rh700");
    expect(parseVariableFromSearch("?type=humidity850")).toBe("rh850");
    expect(parseVariableFromSearch("?type=q850")).toBe("spfh850");
    expect(parseVariableFromSearch("?type=wind850")).toBe("wind850");
    expect(parseVariableFromSearch("?type=qflux850")).toBe("qflux850");
    expect(parseVariableFromSearch("?type=vapor850")).toBe("qflux850");
    expect(parseVariableFromSearch("?type=moisture700")).toBe("qflux700");
    expect(parseVariableFromSearch("?type=z500")).toBe("hgt500");
    // The rule, not a table of the eight surfaces the encoders happen to
    // register today: an alias for a level this build has never rendered
    // still resolves to the id a manifest would carry, and whether the run
    // ships it is the manifest's answer.
    expect(parseVariableFromSearch("?type=t550")).toBe("tmp550");
    expect(parseVariableFromSearch("?type=wind1")).toBe("wind1");
    // The level is the layer: the canonical spelling is the id itself.
    expect(searchForVariable("tmp850", "")).toBe("?model=gfs&type=tmp850");
    expect(searchForVariable("qflux850", "", "gfs")).toBe("?model=gfs&type=qflux850");
    // A filled isobaric field is never the lines slot.
    expect(parseLinesFromSearch("?lines=tmp850")).toBeNull();
    expect(parseLinesFromSearch("?lines=z500")).toBe("hgt500");
  });

  it("resolves types for every served model", () => {
    expect(parseVariableFromSearch("?model=ecmwf&type=wind")).toBe("wind10m");
    expect(parseVariableFromSearch("?model=ifs&type=temp")).toBe("tmp2m");
    expect(parseVariableFromSearch("?model=sflux&type=solar")).toBe("dswrf");
    expect(parseVariableFromSearch("?type=radiation")).toBe("dswrf");
    expect(parseVariableFromSearch("?type=DSWRF")).toBe("dswrf");
    // cref ships only on showcase cases, which pin their own dataset, so the
    // type alias has to resolve without a model param.
    expect(parseVariableFromSearch("?type=radar")).toBe("cref");
    expect(parseVariableFromSearch("?type=CREF")).toBe("cref");
    expect(parseVariableFromSearch("?type=reflectivity")).toBe("cref");
  });

  it("passes a well-formed name the tables do not know straight through", () => {
    // The shell is not the registry of what exists: a run may publish a
    // bundle this build has never heard of, and a link to it has to open.
    // main.ts falls back to the default when the manifest does not ship it.
    expect(parseVariableFromSearch("?model=gfs&type=vorticity")).toBe("vorticity");
    expect(parseVariableFromSearch("?type=CAPE")).toBe("cape");
    expect(parseVariableFromSearch("?type=gust10m")).toBe("gust10m");
    // A well-formed unknown name is still not a pressure surface.
    expect(parseLinesFromSearch("?lines=vorticity")).toBeNull();
    // Round-trips under its own name, since it has no canonical alias.
    expect(searchForVariable("vorticity", "")).toBe("?model=gfs&type=vorticity");
    expect(parseVariableFromSearch(searchForVariable("vorticity", ""))).toBe("vorticity");
  });

  it("falls back to null on unknown model, a malformed type, or no params", () => {
    expect(parseVariableFromSearch("?model=icon&type=wind")).toBeNull();
    // Not bundle names at all: a manifest could never carry these.
    expect(parseVariableFromSearch("?type=tmp-2m")).toBeNull();
    expect(parseVariableFromSearch("?type=2mtemp")).toBeNull();
    expect(parseVariableFromSearch("?type=")).toBeNull();
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

describe("parseUseH264FromSearch", () => {
  it("is off unless the URL opts in", () => {
    expect(parseUseH264FromSearch("")).toBe(false);
    expect(parseUseH264FromSearch("?model=gfs&type=temp")).toBe(false);
  });

  it("accepts true and 1, case-insensitively", () => {
    expect(parseUseH264FromSearch("?use_h264=true")).toBe(true);
    expect(parseUseH264FromSearch("?use_h264=TRUE")).toBe(true);
    expect(parseUseH264FromSearch("?use_h264=1")).toBe(true);
    expect(parseUseH264FromSearch("?model=gfs&use_h264=true&type=temp")).toBe(true);
  });

  it("treats any other value as off rather than erroring", () => {
    expect(parseUseH264FromSearch("?use_h264=false")).toBe(false);
    expect(parseUseH264FromSearch("?use_h264=0")).toBe(false);
    expect(parseUseH264FromSearch("?use_h264=")).toBe(false);
    expect(parseUseH264FromSearch("?use_h264=yes%20please")).toBe(false);
  });

  it("survives a layer switch, which preserves unrelated params", () => {
    expect(parseUseH264FromSearch(searchForVariable("prate", "?use_h264=true"))).toBe(true);
  });
});

describe("parseParticlesFromSearch", () => {
  it("says nothing when the URL does not, so the stored choice can decide", () => {
    expect(parseParticlesFromSearch("")).toBeNull();
    expect(parseParticlesFromSearch("?model=gfs&type=wind")).toBeNull();
  });

  it("accepts the usual on and off spellings, case-insensitively", () => {
    for (const value of ["on", "1", "true", "TRUE", "yes"]) {
      expect(parseParticlesFromSearch(`?particles=${value}`)).toBe(true);
    }
    for (const value of ["off", "0", "false", "No"]) {
      expect(parseParticlesFromSearch(`?particles=${value}`)).toBe(false);
    }
    expect(parseParticlesFromSearch("?model=gfs&particles=off&type=wind")).toBe(false);
  });

  it("treats an unrecognized value as unsaid rather than erroring", () => {
    expect(parseParticlesFromSearch("?particles=maybe")).toBeNull();
    expect(parseParticlesFromSearch("?particles=")).toBeNull();
    expect(parseParticlesFromSearch("?particles=2")).toBeNull();
  });

  it("survives a layer switch, which preserves unrelated params", () => {
    expect(parseParticlesFromSearch(searchForVariable("prate", "?particles=off"))).toBe(false);
  });
});

describe("searchWithParticles", () => {
  it("writes only the switched-off state, and clears it again", () => {
    expect(searchWithParticles("?model=gfs&type=wind", false)).toBe("?model=gfs&type=wind&particles=off");
    expect(searchWithParticles("?model=gfs&type=wind&particles=off", true)).toBe("?model=gfs&type=wind");
    expect(searchWithParticles("", true)).toBe("?");
  });

  it("round-trips through parseParticlesFromSearch", () => {
    expect(parseParticlesFromSearch(searchWithParticles("?type=wind", false))).toBe(false);
    // On is the default, so an on link carries nothing and reads as unsaid.
    expect(parseParticlesFromSearch(searchWithParticles("?type=wind", true))).toBeNull();
  });
});

describe("parseResolutionFromSearch", () => {
  it("is automatic unless the URL pins a tier", () => {
    expect(parseResolutionFromSearch("")).toBe("auto");
    expect(parseResolutionFromSearch("?model=gfs&type=temp")).toBe("auto");
  });

  it("accepts both ends of the ladder and their aliases, case-insensitively", () => {
    expect(parseResolutionFromSearch("?res=half")).toBe("half");
    expect(parseResolutionFromSearch("?res=LOW")).toBe("half");
    expect(parseResolutionFromSearch("?res=full")).toBe("full");
    expect(parseResolutionFromSearch("?res=High")).toBe("full");
    expect(parseResolutionFromSearch("?res=auto")).toBe("auto");
    expect(parseResolutionFromSearch("?model=gfs&res=half&type=temp")).toBe("half");
  });

  it("treats an unknown value as automatic rather than erroring", () => {
    expect(parseResolutionFromSearch("?res=quarter")).toBe("auto");
    expect(parseResolutionFromSearch("?res=")).toBe("auto");
    expect(parseResolutionFromSearch("?res=720")).toBe("auto");
  });

  it("survives a layer switch, which preserves unrelated params", () => {
    expect(parseResolutionFromSearch(searchForVariable("prate", "?res=half"))).toBe("half");
  });
});

describe("parseLinesFromSearch", () => {
  it("says nothing when the URL names no lines", () => {
    expect(parseLinesFromSearch("")).toBeNull();
    expect(parseLinesFromSearch("?model=gfs&type=precip")).toBeNull();
  });

  it("accepts every spelling ?type= takes for a pressure surface", () => {
    expect(parseLinesFromSearch("?lines=pressure")).toBe("prmsl");
    expect(parseLinesFromSearch("?lines=MSLP")).toBe("prmsl");
    expect(parseLinesFromSearch("?lines=prmsl")).toBe("prmsl");
    expect(parseLinesFromSearch("?lines=hgt500")).toBe("hgt500");
    expect(parseLinesFromSearch("?lines=subtropicalhigh")).toBe("hgt500");
    expect(parseLinesFromSearch("?type=precip&lines=hgt850")).toBe("hgt850");
  });

  it("refuses a filled field as lines, and anything unknown, rather than erroring", () => {
    expect(parseLinesFromSearch("?lines=temp")).toBeNull();
    expect(parseLinesFromSearch("?lines=wind")).toBeNull();
    expect(parseLinesFromSearch("?lines=vorticity")).toBeNull();
    expect(parseLinesFromSearch("?lines=")).toBeNull();
  });
});

describe("searchWithLines", () => {
  it("writes the canonical name over a field and clears it again", () => {
    expect(searchWithLines("?model=gfs&type=precip", "prmsl")).toBe("?model=gfs&type=precip&lines=pressure");
    expect(searchWithLines("?model=gfs&type=temp", "hgt500")).toBe("?model=gfs&type=temp&lines=hgt500");
    expect(searchWithLines("?model=gfs&type=precip&lines=pressure", null)).toBe("?model=gfs&type=precip");
  });

  it("round-trips through parseLinesFromSearch and leaves the field alone", () => {
    const search = searchWithLines(searchForVariable("prate", ""), "hgt500");
    expect(parseLinesFromSearch(search)).toBe("hgt500");
    expect(parseVariableFromSearch(search)).toBe("prate");
    // A layer switch preserves the lines, which is what keeps them sticky.
    expect(parseLinesFromSearch(searchForVariable("tmp2m", search))).toBe("hgt500");
  });
});

describe("parseCameraFromHash", () => {
  it("reads MapLibre's named hash, zoom then latitude then longitude", () => {
    expect(parseCameraFromHash("#map=4.5/38.5/-97.5")).toEqual({ center: [-97.5, 38.5], zoom: 4.5 });
    // Bearing and pitch may follow; the camera is still the first three.
    expect(parseCameraFromHash("#map=4.5/38.5/-97.5/30/45")).toEqual({ center: [-97.5, 38.5], zoom: 4.5 });
    // Other fragment params beside it are left to whoever owns them.
    expect(parseCameraFromHash("#other=1&map=2/28/128")).toEqual({ center: [128, 28], zoom: 2 });
  });

  it("reads no camera from an empty, foreign or malformed fragment", () => {
    expect(parseCameraFromHash("")).toBeNull();
    expect(parseCameraFromHash("#")).toBeNull();
    expect(parseCameraFromHash("#4.5/38.5/-97.5")).toBeNull();
    expect(parseCameraFromHash("#map=4.5/38.5")).toBeNull();
    expect(parseCameraFromHash("#map=zoom/lat/lon")).toBeNull();
    expect(parseCameraFromHash("#map=4.5/91/0")).toBeNull();
    expect(parseCameraFromHash("#map=-1/38.5/-97.5")).toBeNull();
  });
});
