/** The NASA GIBS daily underlay (gibs.ts): the layer table, the tile
 * template, the day a playhead asks for, and `?gibs=`. */

import { describe, expect, it } from "vitest";

import { GIBS_LAYERS, GIBS_MAX_LEVEL, gibsDate, gibsLayer, gibsTileUrl } from "../../web/src/gibs";
import { parseGibsFromSearch, searchWithGibs } from "../../web/src/urlstate";
import { parseView, searchForView } from "../../web/src/viewstate";

describe("the GIBS layers", () => {
  it("have unique ids, and a true and a false colour picture per platform", () => {
    const ids = GIBS_LAYERS.map((layer) => layer.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(id).toMatch(/^[a-z0-9]+(-[a-z0-9]+)*$/);
    const platforms = new Set(GIBS_LAYERS.map((layer) => layer.code));
    for (const platform of platforms) {
      const pictures = GIBS_LAYERS.filter((layer) => layer.code === platform);
      expect(pictures.map((layer) => layer.falseColor).sort()).toEqual([false, true]);
    }
    expect(gibsLayer("snpp-fc")?.layer).toBe("VIIRS_SNPP_CorrectedReflectance_BandsM11-I2-I1");
    expect(gibsLayer("terra-721")?.layer).toBe("MODIS_Terra_CorrectedReflectance_Bands721");
    expect(gibsLayer("nonsense")).toBeNull();
    expect(gibsLayer(null)).toBeNull();
  });

  it("builds the WMTS REST template for a day", () => {
    expect(gibsTileUrl(gibsLayer("noaa20")!, "2026-10-11")).toBe(
      `https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_NOAA20_CorrectedReflectance_TrueColor/default/2026-10-11/GoogleMapsCompatible_Level${GIBS_MAX_LEVEL}/{z}/{y}/{x}.jpg`,
    );
  });

  it("asks for the playhead's UTC day, never one after today", () => {
    const now = Date.parse("2026-10-12T05:00:00Z");
    expect(gibsDate(Date.parse("2026-10-10T23:30:00Z"), now)).toBe("2026-10-10");
    // A forecast frame days ahead reads today's picture.
    expect(gibsDate(Date.parse("2026-10-15T12:00:00Z"), now)).toBe("2026-10-12");
    // The UTC day, not the viewer's.
    expect(gibsDate(Date.parse("2026-10-11T20:00:00-08:00"), now)).toBe("2026-10-12");
  });
});

describe("?gibs=", () => {
  it("reads a known layer and nothing else", () => {
    expect(parseGibsFromSearch("")).toBeNull();
    expect(parseGibsFromSearch("?gibs=off")).toBeNull();
    expect(parseGibsFromSearch("?gibs=bogus")).toBeNull();
    expect(parseGibsFromSearch("?gibs=SNPP-FC")).toBe("snpp-fc");
  });

  it("writes the layer, and nothing for none", () => {
    expect(searchWithGibs("?type=temp", "aqua")).toBe("?type=temp&gibs=aqua");
    expect(searchWithGibs("?type=temp&gibs=aqua", null)).toBe("?type=temp");
  });

  it("round-trips through the view, under the experiment only", () => {
    const defaults = { field: "tmp2m", lines: null, particles: true } as const;
    expect(parseView("?model=gfs&type=temp&gibs=noaa21-fc", defaults).view.imagery).toBeNull();
    const search = "?model=gfs&type=temp&x=none&gibs=noaa21-fc";
    const { view } = parseView(search, defaults);
    expect(view.imagery).toBe("noaa21-fc");
    expect(searchForView(view, search, { model: "gfs", caseId: null, experimentEnabled: true, particlesChosen: false }, "tmp2m")).toBe(search);
  });
});
