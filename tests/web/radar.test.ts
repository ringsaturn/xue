/** The single-site radar in the shell (`docs/nexrad.md`): the window
 * manifest and pointer readers on the committed goldens (a live round's and
 * a case's window stores), the feeds over them and over the live bucket,
 * the URL state, the case block, the disc mesh and the site marks. */

import { describe, expect, it, vi } from "vitest";

import pointerJson from "../fixtures/nexrad/expected/latest-nexrad.json";
import caseJson from "../fixtures/nexrad/expected/case/index.json";
import windowJson from "../fixtures/nexrad/expected/nexrad.202303250145/index.json";

import { discMesh } from "../../web/src/radar/layer";
import { windowFeed } from "../../web/src/radar/feed";
import { hourPrefix, keyTime, LIVE_SITES, parseListing } from "../../web/src/radar/live";
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

describe("a case's window stores", () => {
  const window = parseRadarWindow(caseJson);

  it("reads every round out of the one store beside the manifest", () => {
    expect(window.rounds.map((round) => round.path)).toEqual(["./", "./"]);
    const [first, second] = window.rounds.map((round) => round.products.n0g!);
    // The store pads every chunk to its busiest round, so a round with one
    // GWX sweep still decompresses to two slots.
    expect(first!.scans).toBe(2);
    expect(second!.scans).toBe(2);
    expect(second!.chunks.find((chunk) => chunk.site === 1)!.sweeps).toBe(1);
    expect(first!.shardCrc32).toBe(second!.shardCrc32);
    expect(second!.chunks[0]!.offset).toBeGreaterThan(first!.chunks[0]!.offset);
  });

  it("refuses a round that would read another store or another depth", () => {
    type Case = { rounds: { path: string; n0g: { depth?: number; shard: { crc32: string } } }[] };
    const missing = clone(caseJson) as unknown as Case;
    delete missing.rounds[0]!.n0g.depth;
    expect(() => parseRadarWindow(missing)).toThrow(/depth/);
    const other = clone(caseJson) as unknown as Case;
    other.rounds[1]!.n0g.shard.crc32 = "00000000";
    expect(() => parseRadarWindow(other)).toThrow(/another window store/);
    const mixed = clone(caseJson) as unknown as Case;
    mixed.rounds[1]!.path = "../nexrad.202303250145/";
    expect(() => parseRadarWindow(mixed)).toThrow(/all in round stores or all in window stores/);
    const live = clone(windowJson) as unknown as Case;
    live.rounds[0]!.n0g.depth = 2;
    expect(() => parseRadarWindow(live)).toThrow(/belongs to a window store/);
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
    window: "radar/index.json",
    defaultSite: "GWX",
    defaultProduct: "n0g",
    byteLength: 21501,
    crc32: "36144726",
  };

  it("is read from the data root, beside the case", () => {
    const catalog = validateCatalog({ schemaVersion: 1, generatedAt: "", cases: [{ ...base, radar }] });
    expect(catalog.cases).toHaveLength(1);
    expect(catalog.cases[0]!.radar).toEqual({
      windowPath: "showcase/rolling-fork-2023/radar/index.json",
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
    const features = siteFeatures(window.sites, "GWX").features;
    expect(features.map((feature) => feature.properties!.id)).toEqual(["DGX", "GWX"]);
    expect(features.map((feature) => feature.properties!.selected)).toEqual([false, true]);
  });
});

describe("the feeds", () => {
  it("reads a case's site as one unit per round", () => {
    const window = parseRadarWindow(caseJson);
    const gwx = window.sites.findIndex((site) => site.id === "GWX");
    const units = windowFeed(window, "https://data.example/showcase/c/radar/index.json?v=1").site(gwx, () => {}).units("n0g");
    expect(units.map((unit) => unit.times.length)).toEqual(
      window.rounds.map((round) => round.products.n0g!.chunks.find((chunk) => chunk.site === gwx)!.sweeps),
    );
    const first = units[0]!.request;
    expect(first.kind).toBe("chunk");
    expect(first.url).toMatch(/^https:\/\/data\.example\/showcase\/c\/radar\/n0g\.zarr\/n0g\/c\/0\/0\/0\/0\?v=[0-9a-f]{8}$/);
  });

  it("reads the bucket's keys and listings", () => {
    expect(keyTime("GWX_N0G_2023_03_25_01_36_23")).toBe(Date.UTC(2023, 2, 25, 1, 36, 23));
    expect(keyTime("GWX_N0G_2023_03_25_01_36")).toBeNull();
    expect(hourPrefix("GWX", "n0g", Date.UTC(2023, 2, 5, 7, 59))).toBe("GWX_N0G_2023_03_05_07");
    const page = parseListing(
      "<ListBucketResult><IsTruncated>true</IsTruncated><Contents><Key>GWX_N0B_2026_10_05_00_00_28</Key></Contents>" +
        "<Contents><Key>GWX_N0B_2026_10_05_00_03_07</Key></Contents><NextContinuationToken>abc=</NextContinuationToken></ListBucketResult>",
    );
    expect(page).toEqual({ keys: ["GWX_N0B_2026_10_05_00_00_28", "GWX_N0B_2026_10_05_00_03_07"], next: "abc=" });
    expect(parseListing("<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")).toEqual({ keys: [], next: null });
  });

  it("knows the live sites as the case's manifest does", () => {
    const window = parseRadarWindow(caseJson);
    expect(LIVE_SITES.length).toBeGreaterThan(140);
    expect(new Set(LIVE_SITES.map((site) => site.id)).size).toBe(LIVE_SITES.length);
    for (const site of window.sites) expect(LIVE_SITES.find((entry) => entry.id === site.id)).toEqual(site);
  });
});
