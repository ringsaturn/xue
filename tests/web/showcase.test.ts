import { describe, expect, it } from "vitest";

import { validateManifest } from "../../web/src/manifest";
import { spawnRectangle } from "../../web/src/particles";
import {
  boundsWidth,
  caseCameraLimits,
  fitBoundsCorners,
  localizedText,
  validateCatalog,
} from "../../web/src/showcase-catalog";
import { parseCaseFromSearch, searchForCaseVariable } from "../../web/src/urlstate";

function caseFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "zhengzhou-2021",
    title: { zh: "郑州暴雨", en: "Zhengzhou rainstorm" },
    summary: { zh: "示例", en: "Demo" },
    modelId: "gfs",
    model: "GFS",
    product: "pgrb2.0p25",
    run: "2021071800",
    runTime: "2021-07-18T00:00:00Z",
    forecastHours: 120,
    bbox: [105, 28, 122, 42],
    dataBbox: [105, 28, 122, 42],
    grid: { width: 69, height: 57 },
    variables: ["prate", "wind10m"],
    defaultVariable: "prate",
    manifestPath: "showcase/zhengzhou-2021/manifest.json",
    manifestCrc32: "0a1b2c3d",
    byteLength: 2_500_000,
    eventTime: "2021-07-20T08:00:00Z",
    tags: ["rainstorm"],
    ...overrides,
  };
}

function catalogFixture(cases: unknown[] = [caseFixture()]) {
  return { schemaVersion: 1, generatedAt: "2026-08-30T00:00:00Z", cases };
}

describe("showcase catalog", () => {
  it("accepts a well-formed catalog", () => {
    const catalog = validateCatalog(catalogFixture());
    expect(catalog.cases).toHaveLength(1);
    expect(catalog.cases[0]!.defaultVariable).toBe("prate");
  });

  it("accepts an observation case", () => {
    // The radar archive is a dataset like any other in the catalog; it just
    // has no live feed behind it.
    const catalog = validateCatalog(
      catalogFixture([
        caseFixture({
          id: "shadel-2026",
          modelId: "radar",
          model: "CMA-RADAR",
          product: "l3-mst-cref",
          run: "2026082516",
          runTime: "2026-08-25T16:00:00Z",
          forecastHours: 212,
          variables: ["cref"],
          defaultVariable: "cref",
          manifestPath: "showcase/shadel-2026/manifest.json",
        }),
      ]),
    );
    expect(catalog.cases[0]!.modelId).toBe("radar");
    expect(catalog.cases[0]!.variables).toEqual(["cref"]);
  });

  it("rejects a manifest path outside the case's own directory", () => {
    expect(() =>
      validateCatalog(catalogFixture([caseFixture({ manifestPath: "showcase/other/manifest.json" })])),
    ).toThrow();
    expect(() =>
      validateCatalog(catalogFixture([caseFixture({ manifestPath: "../latest.json" })])),
    ).toThrow();
  });

  it("rejects a dataset it cannot serve", () => {
    expect(() => validateCatalog(catalogFixture([caseFixture({ model: "ICON" })]))).toThrow();
    expect(() => validateCatalog(catalogFixture([caseFixture({ modelId: "ecmwf" })]))).toThrow();
    expect(() => validateCatalog(catalogFixture([caseFixture({ product: "pgrb2.0p50" })]))).toThrow();
  });

  it("rejects a default variable the case does not ship", () => {
    expect(() => validateCatalog(catalogFixture([caseFixture({ defaultVariable: "tmp2m" })]))).toThrow();
  });

  it("rejects duplicate case ids", () => {
    expect(() => validateCatalog(catalogFixture([caseFixture(), caseFixture()]))).toThrow();
  });

  it("rejects an unknown schema version", () => {
    expect(() => validateCatalog({ ...catalogFixture(), schemaVersion: 2 })).toThrow();
  });

  it("falls back across locales for card text", () => {
    expect(localizedText({ zh: "郑州", en: "Zhengzhou" }, "zh")).toBe("郑州");
    expect(localizedText({ en: "Zhengzhou" }, "zh")).toBe("Zhengzhou");
    expect(localizedText({ fr: "Zhengzhou" }, "zh")).toBe("Zhengzhou");
  });
});

describe("case framing", () => {
  it("measures an ordinary box", () => {
    expect(boundsWidth([105, 28, 122, 42])).toBe(17);
  });

  it("measures a box across the antimeridian", () => {
    expect(boundsWidth([170, -20, -170, 10])).toBe(20);
  });

  it("treats equal longitudes as the whole world, matching the encoder", () => {
    expect(boundsWidth([-180, -30, 180, 30])).toBe(360);
  });

  it("keeps an antimeridian fit contiguous by running east past +180", () => {
    expect(fitBoundsCorners([170, -20, -170, 10])).toEqual([
      [170, -20],
      [190, 10],
    ]);
  });
});

describe("case URL state", () => {
  it("reads a case id", () => {
    expect(parseCaseFromSearch("?case=zhengzhou-2021&type=precip")).toBe("zhengzhou-2021");
    expect(parseCaseFromSearch("?type=precip")).toBeNull();
    expect(parseCaseFromSearch("?case=../secrets")).toBeNull();
  });

  it("drops the model param, which a case pins itself", () => {
    const search = searchForCaseVariable("wind10m", "?model=ecmwf&type=precip&lang=zh", "doksuri-2023");
    const params = new URLSearchParams(search);
    expect(params.get("model")).toBeNull();
    expect(params.get("case")).toBe("doksuri-2023");
    expect(params.get("type")).toBe("wind");
    expect(params.get("lang")).toBe("zh");
  });
});

describe("regional manifests", () => {
  const manifest = {
    schemaVersion: 5,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2021-07-18T00:00:00Z",
    forecastHours: 120,
    bundles: [{ variable: "prate", path: "prate.xue", byteLength: 32, crc32: "00000000" }],
  };

  it("accepts a case manifest without the core pair", () => {
    expect(validateManifest(manifest, "gfs", { requireCoreVariables: false }).bundles).toHaveLength(1);
  });

  it("still requires the core pair of a live run", () => {
    expect(() => validateManifest(manifest, "gfs")).toThrow();
  });
});

describe("particle spawn window", () => {
  it("seeds the whole world square on a global grid", () => {
    const [x, y, width, height] = spawnRectangle(-180, 90, 0.25, -0.25, 1440, 721, true);
    expect([x, width]).toEqual([0, 1]);
    expect(y).toBeCloseTo(0, 6);
    expect(height).toBeCloseTo(1, 6);
  });

  it("seeds only the window of a regional grid", () => {
    const [x, y, width, height] = spawnRectangle(105, 42, 0.25, -0.25, 69, 57, false);
    expect(x).toBeCloseTo((105 + 180) / 360, 6);
    expect(width).toBeCloseTo(17 / 360, 6);
    expect(y).toBeGreaterThan(0);
    expect(height).toBeGreaterThan(0);
    expect(y + height).toBeLessThan(1);
  });

  it("wraps the origin of a window that crosses the antimeridian", () => {
    const [x, , width] = spawnRectangle(170, 10, 0.25, -0.25, 81, 121, false);
    expect(x).toBeCloseTo(350 / 360, 6);
    expect(x + width).toBeGreaterThan(1);
  });
});

describe("case camera limits", () => {
  const HEAT_DOME: [number, number, number, number] = [-142, 32, -96, 60];
  const DESKTOP = { width: 1440, height: 860 };

  /** Does the viewport at `minZoom` hold the whole region? `bounds` is in
   * MapLibre's [south-west, north-east] order. */
  function coversBox(box: [number, number, number, number], viewport: { width: number; height: number }): boolean {
    const limits = caseCameraLimits(box, viewport);
    const [[west, south], [east, north]] = limits.bounds;
    return west <= box[0] && east >= box[2] && south <= box[1] && north >= box[3];
  }

  it("frames the region and holds panning to what that shows", () => {
    const limits = caseCameraLimits(HEAT_DOME, DESKTOP);
    expect(limits.center[0]).toBeCloseTo(-119, 6);
    expect(limits.minZoom).toBeGreaterThan(3);
    expect(limits.minZoom).toBeLessThan(5);
    expect(coversBox(HEAT_DOME, DESKTOP)).toBe(true);
  });

  it("covers the region on every viewport shape", () => {
    for (const viewport of [
      { width: 1920, height: 1080 },
      { width: 1440, height: 860 },
      { width: 390, height: 780 },
      { width: 780, height: 390 },
      { width: 320, height: 480 },
    ]) {
      expect(coversBox(HEAT_DOME, viewport)).toBe(true);
    }
  });

  it("zooms in further when there are more pixels to fill", () => {
    // Doubling every dimension is exactly one zoom level, once the fixed
    // padding is out of the way.
    const small = caseCameraLimits(HEAT_DOME, { width: 720, height: 430 }, 0);
    const large = caseCameraLimits(HEAT_DOME, { width: 1440, height: 860 }, 0);
    expect(large.minZoom).toBeCloseTo(small.minZoom + 1, 6);
    // With padding held fixed, a bigger viewport still gains more than that.
    const padded = caseCameraLimits(HEAT_DOME, { width: 1440, height: 860 });
    expect(padded.minZoom).toBeGreaterThan(caseCameraLimits(HEAT_DOME, { width: 720, height: 430 }).minZoom);
  });

  it("puts the region's edges inside the viewport, not against it", () => {
    const padded = caseCameraLimits(HEAT_DOME, DESKTOP);
    const unpadded = caseCameraLimits(HEAT_DOME, DESKTOP, 0);
    expect(padded.minZoom).toBeLessThan(unpadded.minZoom);
  });

  it("keeps an antimeridian region contiguous", () => {
    const box: [number, number, number, number] = [170, -20, -170, 10];
    const limits = caseCameraLimits(box, DESKTOP);
    expect(limits.center[0]).toBeCloseTo(180, 6);
    const [[west], [east]] = limits.bounds;
    expect(west).toBeLessThanOrEqual(170);
    expect(east).toBeGreaterThanOrEqual(190);
  });

  it("never asks to zoom out past the world", () => {
    const limits = caseCameraLimits([-180, -60, 180, 60], { width: 320, height: 200 });
    expect(limits.minZoom).toBe(0);
  });

  it("survives a padding wider than the viewport", () => {
    const limits = caseCameraLimits(HEAT_DOME, { width: 40, height: 40 }, 200);
    expect(Number.isFinite(limits.minZoom)).toBe(true);
    expect(limits.minZoom).toBeGreaterThanOrEqual(0);
  });
});
