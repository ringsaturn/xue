import { describe, expect, it } from "vitest";

import { parseCaseFromSearch, parseExperimentFromSearch, parseModelFromSearch } from "../../web/src/urlstate";
import { compositionForPrimary, compositionPrimary, parseView, searchForView, type ViewDefaults } from "../../web/src/viewstate";

const DEFAULTS: ViewDefaults = { field: "prate", lines: null, particles: true };

/** Write back what was parsed, against the same query string, the way the
 * shell does after a load: the dataset from the URL, the experiment's
 * switch from it, the particles chosen only if the URL chose them. */
function roundTrip(search: string, defaults: ViewDefaults = DEFAULTS): string {
  const parsed = parseView(search, defaults);
  return searchForView(
    parsed.view,
    search,
    {
      model: parseModelFromSearch(search),
      caseId: parseCaseFromSearch(search),
      experimentEnabled: parseExperimentFromSearch(search).enabled,
      particlesChosen: parsed.particlesRequested,
    },
    defaults.field,
  );
}

describe("parseView", () => {
  it("splits the primary into the field and the lines", () => {
    expect(parseView("?type=temp&lines=hgt500", DEFAULTS).view).toMatchObject({ field: "tmp2m", lines: "hgt500" });
    expect(parseView("?type=hgt500", DEFAULTS).view).toMatchObject({ field: null, lines: "hgt500" });
    // Lines named beside a pressure primary are the primary: the view is
    // the lines alone, and `lines` is not a second copy of it.
    expect(parseView("?type=pressure&lines=hgt500", DEFAULTS).view).toMatchObject({ field: null, lines: "prmsl" });
  });

  it("falls back to the defaults where the URL says nothing", () => {
    const parsed = parseView("?model=gfs", { field: "prate", lines: "prmsl", particles: false });
    expect(parsed.view).toMatchObject({ field: "prate", lines: "prmsl", particles: false });
    expect(parsed.fieldRequested).toBe(false);
    expect(parsed.particlesRequested).toBe(false);
    // A named field takes no default lines: only the empty URL opens on
    // the experiment's composition.
    expect(parseView("?type=temp", { field: "prate", lines: "prmsl", particles: true }).view.lines).toBeNull();
  });

  it("reads the overlays and the marks", () => {
    const { view, particlesRequested } = parseView("?particles=off&x=inflow&stations=snd,apt&tc=EP142026&tcmembers=on", DEFAULTS);
    expect(view.particles).toBe(false);
    expect(particlesRequested).toBe(true);
    expect(view.derived).toEqual({ inflow: true, front: false });
    expect(view.marks.stations).toEqual({ soundings: true, airports: true });
    expect(view.marks.tc).toMatchObject({ storm: "EP142026", off: false, members: true });
  });
});

describe("searchForView", () => {
  it.each([
    "?model=gfs&type=precip",
    "?model=gfs&type=temp&lines=hgt500",
    "?model=ecmwf&type=hgt500",
    "?model=gfs&type=wind&particles=off",
    "?model=gfs&type=precip&x=true",
    "?model=gfs&type=precip&x=inflow",
    "?model=gfs&type=precip&x=none",
    "?model=gfs&type=precip&stations=snd",
    "?model=gfs&type=precip&stations=snd%2Capt",
    "?model=gfs&type=precip&tc=off",
    "?model=gfs&type=precip&tc=EP142026&tcagency=nhc%2Cjtwc&tcmodel=gfs&tcmembers=on",
    "?case=ida-2021&type=radar&lines=pressure",
    "?model=hrrr&type=radar&res=half&backend=xue",
  ])("round-trips %s", (search) => {
    expect(roundTrip(search)).toBe(search);
  });

  it("writes only what differs from the defaults", () => {
    const view = parseView("?model=gfs", DEFAULTS).view;
    expect(
      searchForView(view, "?model=gfs", { model: "gfs", caseId: null, experimentEnabled: false, particlesChosen: false }, "prate"),
    ).toBe("?model=gfs&type=precip");
    // A default that was never chosen is not written, even when it is off.
    const off = { ...view, particles: false };
    expect(
      searchForView(off, "?model=gfs", { model: "gfs", caseId: null, experimentEnabled: false, particlesChosen: false }, "prate"),
    ).toBe("?model=gfs&type=precip");
    expect(
      searchForView(off, "?model=gfs", { model: "gfs", caseId: null, experimentEnabled: false, particlesChosen: true }, "prate"),
    ).toBe("?model=gfs&type=precip&particles=off");
  });

  it("names the lines in type alone when they are the view", () => {
    const view = { ...parseView("?model=gfs", DEFAULTS).view, field: null, lines: "hgt500" as const };
    expect(
      searchForView(view, "?model=gfs&type=temp&lines=hgt500", { model: "gfs", caseId: null, experimentEnabled: false, particlesChosen: false }, "prate"),
    ).toBe("?model=gfs&type=hgt500");
  });

  it("drops the model under a case", () => {
    const view = parseView("?model=gfs&type=temp", DEFAULTS).view;
    expect(
      searchForView(view, "?model=gfs&type=temp", { model: "gfs", caseId: "ida-2021", experimentEnabled: false, particlesChosen: false }, "prate"),
    ).toBe("?type=temp&case=ida-2021");
  });
});

describe("composition helpers", () => {
  it("keeps lines under a field and drops them for a pressure primary", () => {
    expect(compositionForPrimary("tmp2m", "prmsl")).toEqual({ fill: "tmp2m", lines: "prmsl" });
    expect(compositionForPrimary("hgt500", "prmsl")).toEqual({ fill: null, lines: "hgt500" });
    expect(compositionPrimary({ fill: null, lines: null }, "prate")).toBe("prate");
    expect(compositionPrimary({ fill: null, lines: "prmsl" }, "prate")).toBe("prmsl");
  });
});
