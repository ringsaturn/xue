import { describe, expect, it } from "vitest";

import { parseCaseFromLocation } from "../../web/src/urlstate";
import {
  compositionForPrimary,
  compositionPrimary,
  parseView,
  searchForView,
  setupInView,
  volumeOverField,
  type ViewDefaults,
} from "../../web/src/viewstate";

const SETUP_LAYERS = { inflow: true, front: true };
const DEFAULTS: ViewDefaults = { field: "prate", lines: null, particles: true, setupLayers: SETUP_LAYERS };

/** Write back what was parsed, against the same query string, the way the
 * shell does after a load: the case from the path, the particles chosen
 * only if the URL chose them. */
function roundTrip(search: string, defaults: ViewDefaults = DEFAULTS, pathname = "/"): string {
  const parsed = parseView(search, defaults);
  return searchForView(
    parsed.view,
    search,
    {
      caseId: parseCaseFromLocation(pathname, search),
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
    const parsed = parseView("", { field: "prate", lines: "prmsl", particles: false, setupLayers: SETUP_LAYERS });
    expect(parsed.view).toMatchObject({ field: "prate", lines: "prmsl", particles: false });
    expect(parsed.fieldRequested).toBe(false);
    expect(parsed.particlesRequested).toBe(false);
    // A named field takes no default lines.
    expect(parseView("?type=temp", { field: "prate", lines: "prmsl", particles: true, setupLayers: SETUP_LAYERS }).view.lines).toBeNull();
  });

  it("reads a section's line, latitudes first, and writes back what it read", () => {
    const search = "?type=temp&section=39.90%2C116.40%2C35.70%2C-170.25";
    expect(parseView(search, DEFAULTS).view.section).toEqual({ a: [116.4, 39.9], b: [-170.25, 35.7] });
    expect(roundTrip(search)).toBe(search);
    // To hundredths of a degree, within one turn, and never a negative zero.
    expect(parseView("?section=10.126,190,-0.001,-180", DEFAULTS).view.section).toEqual({ a: [-170, 10.13], b: [-180, 0] });
    for (const bad of ["", "1,2,3", "1,2,3,x", "91,0,0,0", "0,361,0,0", "1,2,3,4,5"]) {
      expect(parseView(`?section=${bad}`, DEFAULTS).view.section, bad).toBeNull();
      expect(roundTrip(`?type=temp&section=${bad}`), bad).toBe("?type=temp");
    }
    // A case has none.
    expect(roundTrip(search, DEFAULTS, "/showcase/some-case")).toBe("?type=temp");
  });

  it("names no fire field in the link", () => {
    const { view } = parseView("?type=temp", DEFAULTS);
    view.derived.ffwi = true;
    expect(searchForView(view, "?type=temp", { caseId: null, particlesChosen: false }, "prate")).toBe(
      "?type=temp",
    );
  });

  it("reads the overlays and the marks", () => {
    const { view, particlesRequested } = parseView("?particles=off&stations=snd,apt&tc=EP142026&tcmembers=on", {
      ...DEFAULTS,
      setupLayers: { inflow: true, front: false },
    });
    expect(view.particles).toBe(false);
    expect(particlesRequested).toBe(true);
    expect(view.derived).toEqual({ inflow: true, front: false, hotspot: false, ffwi: false, fwalert: false });
    expect(view.marks.stations).toEqual({ soundings: true, airports: true, synop: false, fire: false, aq: false, alerts: false });
    expect(view.marks.tc).toMatchObject({ storm: "EP142026", off: false, members: true });
  });

  it("reads the precipitation setup on the rate, with the sea level pressure unless told otherwise", () => {
    const bare = parseView("?type=precipsetup", DEFAULTS);
    expect(bare.view).toMatchObject({ field: "prate", lines: "prmsl", setup: true, flow: null, accumulation: null });
    expect(bare.fieldRequested).toBe(true);
    expect(setupInView(bare.view)).toBe(true);
    expect(parseView("?type=PrecipSetup&lines=hgt500&flow=wind500", DEFAULTS).view).toMatchObject({
      field: "prate",
      lines: "hgt500",
      setup: true,
      flow: "wind500",
    });
    expect(parseView("?type=precipsetup&lines=none", DEFAULTS).view.lines).toBeNull();
    // The default flow and a flow that is no wind or flux read as the default.
    expect(parseView("?type=precipsetup&flow=qflux850", DEFAULTS).view.flow).toBeNull();
    expect(parseView("?type=precipsetup&flow=tmp2m", DEFAULTS).view.flow).toBeNull();
    // A total is the rate's other picture: the setup has none.
    expect(parseView("?type=precipsetup&accum=24h", DEFAULTS).view.accumulation).toBeNull();
    expect(parseView("?type=precip", DEFAULTS).view.setup).toBe(false);
  });
});

describe("searchForView", () => {
  it.each([
    "?type=precip",
    "?type=temp&lines=hgt500",
    "?type=hgt500",
    "?type=wind&particles=off",
    "?type=precipsetup",
    "?type=precipsetup&lines=none",
    "?type=precipsetup&lines=hgt500&flow=wind850",
    "?type=precipsetup&phase=on",
    "?type=precip&stations=snd",
    "?type=precip&stations=snd%2Capt",
    "?type=precip&tc=off",
    "?type=precip&tc=EP142026&tcagency=nhc%2Cjtwc&tcmodel=gfs&tcmembers=on",
    "?type=radar&lines=pressure",
    "?type=radar&res=half&backend=xue",
    "?type=sst&volume=cloud3d",
    "?type=precip&accum=24h",
    "?type=precip&accum=30m",
  ])("round-trips %s", (search) => {
    expect(roundTrip(search)).toBe(search);
  });

  it.each([
    ["/showcase/ida-2021", "?type=radar&lines=pressure"],
    ["/ja/showcase/fuji-woof-2026-10-10", "?type=sst&volume=cloud3d"],
  ])("round-trips a case's view on its path %s%s", (pathname, search) => {
    expect(roundTrip(search, DEFAULTS, pathname)).toBe(search);
  });

  it("writes only what differs from the defaults", () => {
    const view = parseView("", DEFAULTS).view;
    expect(
      searchForView(view, "", { caseId: null, particlesChosen: false }, "prate"),
    ).toBe("?type=precip");
    // A default that was never chosen is not written, even when it is off.
    const off = { ...view, particles: false };
    expect(
      searchForView(off, "", { caseId: null, particlesChosen: false }, "prate"),
    ).toBe("?type=precip");
    expect(
      searchForView(off, "", { caseId: null, particlesChosen: true }, "prate"),
    ).toBe("?type=precip&particles=off");
  });

  it("names the lines in type alone when they are the view", () => {
    const view = { ...parseView("", DEFAULTS).view, field: null, lines: "hgt500" as const };
    expect(
      searchForView(view, "?type=temp&lines=hgt500", { caseId: null, particlesChosen: false }, "prate"),
    ).toBe("?type=hgt500");
  });

  it("drops the model, and a case param the path now says, under a case", () => {
    const view = parseView("?type=temp", DEFAULTS).view;
    expect(
      searchForView(view, "?model=gfs&case=ida-2021&type=temp", { caseId: "ida-2021", particlesChosen: false }, "prate"),
    ).toBe("?type=temp");
  });

  it("drops an old model param on a live view: the dataset is the path's", () => {
    const view = parseView("?type=temp", DEFAULTS).view;
    expect(searchForView(view, "?model=ecmwf&type=temp", { caseId: null, particlesChosen: false }, "prate")).toBe("?type=temp");
  });
});

describe("the precipitation setup", () => {
  it("is written only while its field is the rate, and its flow goes with it", () => {
    const { view } = parseView("?type=precipsetup&flow=wind500", DEFAULTS);
    const context = { caseId: null, particlesChosen: false };
    expect(searchForView(view, "?type=precipsetup&flow=wind500", context, "prate")).toBe("?type=precipsetup&flow=wind500");
    const other = { ...view, field: "tmp2m" as const };
    expect(setupInView(other)).toBe(false);
    // The lines it drew stay over the next field, as over any.
    expect(searchForView(other, "?type=precipsetup&flow=wind500", context, "prate")).toBe("?type=temp&lines=pressure");
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

describe("the volume overlay", () => {
  it("is parsed from ?volume=, unknown values reading as none", () => {
    expect(parseView("?type=sst&volume=cloud3d", DEFAULTS).view.volume).toBe("cloud3d");
    expect(parseView("?type=sst&volume=CLOUD3D", DEFAULTS).view.volume).toBe("cloud3d");
    expect(parseView("?type=sst&volume=tmp2m", DEFAULTS).view.volume).toBeNull();
    expect(parseView("?type=sst&volume=off", DEFAULTS).view.volume).toBeNull();
    expect(parseView("?type=sst", DEFAULTS).view.volume).toBeNull();
  });

  it("remembers whether the link spoke of the volume at all", () => {
    // What lets `?volume=off` outrank a case that opens with its cloud.
    expect(parseView("?type=sst&volume=cloud3d", DEFAULTS).volumeRequested).toBe(true);
    expect(parseView("?type=sst&volume=off", DEFAULTS).volumeRequested).toBe(true);
    expect(parseView("?type=sst&volume=", DEFAULTS).volumeRequested).toBe(true);
    expect(parseView("?type=sst", DEFAULTS).volumeRequested).toBe(false);
  });

  it("is drawn and written only over a 2D field", () => {
    expect(volumeOverField({ field: "tmpsfc", volume: "cloud3d" })).toBe("cloud3d");
    expect(volumeOverField({ field: null, volume: "cloud3d" })).toBeNull();
    expect(volumeOverField({ field: "refl3d", volume: "cloud3d" })).toBeNull();
    // A volume as the field is named by `type`, and `volume` is dropped.
    expect(roundTrip("?type=refl3d&volume=cloud3d")).toBe("?type=refl3d");
  });
});

describe("an old day-night link", () => {
  it("is the true colour alone: ?blend=night is not part of the view", () => {
    // The true colour carries its own night side, so the day-night blend
    // is gone; a link that names it opens the same view as one that does
    // not, and the parameter is left as an unrelated one.
    const withBlend = parseView("?type=truecolor&blend=night", DEFAULTS);
    const without = parseView("?type=truecolor", DEFAULTS);
    expect(withBlend).toEqual(without);
    expect(withBlend.view.field).toBe("truecolor");
    expect(withBlend.view).not.toHaveProperty("blend");
    expect(roundTrip("?type=truecolor")).toBe("?type=truecolor");
  });
});

describe("the precipitation total", () => {
  it("is parsed from ?accum=, a spelling that is no window reading as the rate", () => {
    expect(parseView("?type=precip&accum=24h", DEFAULTS).view.accumulation).toEqual({ type: "window", seconds: 24 * 3600 });
    expect(parseView("?type=precip&accum=30m", DEFAULTS).view.accumulation).toEqual({ type: "window", seconds: 1800 });
    expect(parseView("?type=precip&accum=2026111106-2026111400", DEFAULTS).view.accumulation).toEqual({
      type: "range",
      from: Date.UTC(2026, 10, 11, 6),
      to: Date.UTC(2026, 10, 14, 0),
    });
    expect(parseView("?type=precip&accum=2026111400-2026111106", DEFAULTS).view.accumulation).toBeNull();
    expect(parseView("?type=precip&accum=soon", DEFAULTS).view.accumulation).toBeNull();
    expect(parseView("?type=precip", DEFAULTS).view.accumulation).toBeNull();
  });

  it("is written only while the precipitation is the field", () => {
    const view = { ...parseView("?type=temp", DEFAULTS).view, accumulation: { type: "window" as const, seconds: 6 * 3600 } };
    const context = { caseId: null, particlesChosen: false };
    expect(searchForView(view, "?type=temp", context, "prate")).toBe("?type=temp");
    expect(searchForView({ ...view, field: "prate" }, "?type=temp", context, "prate")).toBe(
      "?type=precip&accum=6h",
    );
    const range = { type: "range" as const, from: Date.UTC(2026, 10, 11, 6, 30), to: Date.UTC(2026, 10, 11, 9) };
    expect(searchForView({ ...view, field: "prate", accumulation: range }, "?type=temp", context, "prate")).toBe(
      "?type=precip&accum=202611110630-2026111109",
    );
  });
});
