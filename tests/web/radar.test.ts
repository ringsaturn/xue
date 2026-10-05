/** The single-site radar in the shell (`docs/nexrad.md`): the window
 * manifest and pointer readers on the committed golden, the URL state, the
 * case block, the disc mesh and the site marks. */

import { describe, expect, it, vi } from "vitest";

import pointerJson from "../fixtures/nexrad/expected/latest-nexrad.json";
import windowJson from "../fixtures/nexrad/expected/nexrad.202303250145/index.json";

import { discMesh } from "../../web/src/radar/layer";
import { siteFeatures } from "../../web/src/radar/overlay";
import { parseNexradPointer, parseRadarWindow } from "../../web/src/radar/schema";
import { validateCatalog } from "../../web/src/showcase-catalog";
import { parseRadarFromSearch, searchWithRadar } from "../../web/src/urlstate";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

describe("the window manifest", () => {
  const window = parseRadarWindow(windowJson);

  it("reads the golden", () => {
    expect(parseNexradPointer(pointerJson).path).toBe("nexrad.202303250145/index.json");
    expect(window.sites.map((site) => site.id)).toEqual(["DGX", "GWX"]);
    expect(window.rounds).toHaveLength(2);
    const first = window.rounds[0]!.products.n0g!;
    // GWX's two MESO-SAILS sweeps share the round, so the store pads to two.
    expect(first.scans).toBe(2);
    expect(first.chunks[1]!.times).toEqual([1679708183000, 1679708280000]);
    expect(window.rounds[0]!.path).toBe("../nexrad.202303250140/");
  });

  it("refuses spans a reader could not trust", () => {
    const overlap = clone(windowJson) as { rounds: { n0g: { chunks: number[][] } }[] };
    overlap.rounds[0]!.n0g.chunks[1]![1] = 0;
    expect(() => parseRadarWindow(overlap)).toThrow(/in order/);
    const past = clone(windowJson) as { rounds: { n0g: { chunks: number[][] } }[] };
    past.rounds[0]!.n0g.chunks[1]![2] = 10 ** 9;
    expect(() => parseRadarWindow(past)).toThrow(/in order/);
    const stray = clone(windowJson) as { rounds: { n0b: { chunks: number[][] } }[] };
    stray.rounds[0]!.n0b.chunks[0]![0] = 9;
    expect(() => parseRadarWindow(stray)).toThrow(/past the table/);
    const repeat = clone(windowJson) as unknown as { rounds: { n0g: { scans: [number, number[]][] } }[] };
    repeat.rounds[1]!.n0g.scans[1]![1] = [1679708280];
    expect(() => parseRadarWindow(repeat)).toThrow(/strictly follow/);
  });
});

describe("the ?radar= parameter", () => {
  it("is off unless named", () => {
    expect(parseRadarFromSearch("")).toEqual({ on: false, site: null, product: "n0b" });
    expect(parseRadarFromSearch("?radar=off")).toEqual({ on: false, site: null, product: "n0b" });
  });

  it("names a site and a product", () => {
    expect(parseRadarFromSearch("?radar=gwx&radarproduct=vel")).toEqual({ on: true, site: "GWX", product: "n0g" });
    expect(parseRadarFromSearch("?radar=on")).toEqual({ on: true, site: null, product: "n0b" });
    expect(parseRadarFromSearch("?radar=KGWX")).toEqual({ on: true, site: null, product: "n0b" });
  });

  it("round-trips, writing only what differs from the default", () => {
    expect(searchWithRadar("?type=cref", { on: false, site: null, product: "n0g" })).toBe("?type=cref");
    expect(searchWithRadar("?type=cref", { on: true, site: "GWX", product: "n0b" })).toBe("?type=cref&radar=GWX");
    for (const state of [
      { on: true, site: "GWX", product: "n0g" as const },
      { on: true, site: null, product: "n0b" as const },
      { on: false, site: null, product: "n0b" as const },
    ]) {
      expect(parseRadarFromSearch(searchWithRadar("", state))).toEqual(state);
    }
  });
});

describe("a case's radar block", () => {
  const base = {
    id: "rolling-fork-2023",
    title: { en: "Rolling Fork" },
    summary: { en: "Tornado" },
    modelId: "mrms",
    model: "NOAA-MRMS",
    product: "conus-cref",
    run: "2023032423",
    runTime: "2023-03-24T23:00:00Z",
    forecastHours: 4,
    bbox: [-93.5, 30.5, -86, 36.5],
    dataBbox: [-93.5, 30.5, -86, 36.5],
    grid: { width: 376, height: 301 },
    variables: ["cref"],
    defaultVariable: "cref",
    manifestPath: "showcase/rolling-fork-2023/manifest.json",
    manifestCrc32: "0123abcd",
    byteLength: 1,
  };
  const radar = {
    window: "radar/nexrad.202303250230/index.json",
    defaultSite: "GWX",
    defaultProduct: "n0g",
    byteLength: 21501,
    crc32: "36144726",
  };

  it("is read from the data root, beside the case", () => {
    const catalog = validateCatalog({ schemaVersion: 1, generatedAt: "", cases: [{ ...base, radar }] });
    expect(catalog.cases).toHaveLength(1);
    expect(catalog.cases[0]!.radar).toEqual({
      windowPath: "showcase/rolling-fork-2023/radar/nexrad.202303250230/index.json",
      crc32: "36144726",
      byteLength: 21501,
      defaultSite: "GWX",
      defaultProduct: "n0g",
    });
  });

  it("is dropped, not the case, when this shell cannot read it", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const catalog = validateCatalog({
      schemaVersion: 1,
      generatedAt: "",
      cases: [{ ...base, radar: { ...radar, window: "../elsewhere/index.json" } }],
    });
    expect(catalog.cases).toHaveLength(1);
    expect(catalog.cases[0]!.radar).toBeUndefined();
    warn.mockRestore();
  });
});

describe("the map pieces", () => {
  it("covers the disc with a mesh around the antenna", () => {
    const mesh = discMesh(33.896917, -88.329194, 300);
    let west = Infinity, east = -Infinity;
    for (let at = 0; at < mesh.length; at += 2) {
      west = Math.min(west, mesh[at]!);
      east = Math.max(east, mesh[at]!);
    }
    const antenna = (-88.329194 + 180) / 360;
    expect(west).toBeLessThan(antenna);
    expect(east).toBeGreaterThan(antenna);
    // 300 km at 34° N is about 3.2° of longitude each way.
    expect((east - west) * 360).toBeGreaterThan(6);
    expect((east - west) * 360).toBeLessThan(7.5);
  });

  it("marks every site, the chosen one filled", () => {
    const window = parseRadarWindow(windowJson);
    const features = siteFeatures(window, "GWX").features;
    expect(features.map((feature) => feature.properties!.id)).toEqual(["DGX", "GWX"]);
    expect(features.map((feature) => feature.properties!.selected)).toEqual([false, true]);
  });
});
