/** A showcase case's context lines (casecontext.ts): the catalog block,
 * the features drawn from the file, the layers they are drawn in and where
 * those go in the style. */

import { describe, expect, it, vi } from "vitest";

import {
  CONTEXT_KINDS,
  contextBeforeLayer,
  contextFeatures,
  contextInk,
  contextLayerSpecs,
} from "../../web/src/casecontext";
import { contextBlock, MAX_CONTEXT_BYTES, validateCatalog } from "../../web/src/showcase-catalog";

const BLOCK = {
  file: "context/context.geojson",
  credit: "ROADS, RIVERS, BORDER AND RESERVE © OPENSTREETMAP CONTRIBUTORS (ODbL)",
  byteLength: 123_456,
  crc32: "0a1b2c3d",
};

function caseFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "arxan-2026",
    title: { zh: "阿尔山火", en: "Arxan fire" },
    summary: { zh: "示例", en: "Demo" },
    modelId: "gfs",
    model: "GFS",
    product: "pgrb2.0p25",
    run: "2026100800",
    runTime: "2026-10-08T00:00:00Z",
    forecastHours: 72,
    bbox: [117, 45.5, 122, 48.5],
    dataBbox: [117, 45.5, 122, 48.5],
    grid: { width: 21, height: 13 },
    variables: ["wind10m"],
    defaultVariable: "wind10m",
    manifestPath: "showcase/arxan-2026/manifest.json",
    manifestCrc32: "0a1b2c3d",
    byteLength: 2_500_000,
    ...overrides,
  };
}

function line(kind: unknown, coordinates: unknown, type = "LineString") {
  return { type: "Feature", properties: { kind, name: "ignored" }, geometry: { type, coordinates } };
}

const RING = [
  [119, 46],
  [120, 46],
  [120, 47],
  [119, 46],
];

describe("the catalog's context block", () => {
  it("names the file from the data root under its crc32, with its credit", () => {
    expect(contextBlock(BLOCK, "arxan-2026")).toEqual({
      filePath: "showcase/arxan-2026/context/context.geojson",
      crc32: "0a1b2c3d",
      byteLength: 123_456,
      credit: BLOCK.credit,
    });
    expect(contextBlock(undefined, "x")).toBeUndefined();
  });

  it("keeps the credit to one line", () => {
    expect(contextBlock({ ...BLOCK, credit: "  ROADS\n© OSM  " }, "x")?.credit).toBe("ROADS © OSM");
  });

  it("drops a block it cannot read with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const bad = [
      { ...BLOCK, file: "/context/context.geojson" },
      { ...BLOCK, file: "../other/context.geojson" },
      { ...BLOCK, file: "context/context.json" },
      { ...BLOCK, credit: "   " },
      { ...BLOCK, credit: "x".repeat(201) },
      { ...BLOCK, crc32: "nope" },
      { ...BLOCK, byteLength: undefined },
      { ...BLOCK, byteLength: MAX_CONTEXT_BYTES + 1 },
      null,
    ];
    for (const block of bad) expect(contextBlock(block, "x")).toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(bad.length);
    warn.mockRestore();
  });

  it("rides on the catalog row, and a block it cannot read leaves the case playing", () => {
    const catalog = validateCatalog({
      schemaVersion: 1,
      generatedAt: "2026-10-25T00:00:00Z",
      cases: [caseFixture({ context: BLOCK })],
    });
    expect(catalog.cases[0]!.context?.filePath).toBe("showcase/arxan-2026/context/context.geojson");

    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const without = validateCatalog({
      schemaVersion: 1,
      generatedAt: "2026-10-25T00:00:00Z",
      cases: [caseFixture({ context: { ...BLOCK, file: "../context.geojson" } })],
    });
    expect(without.cases).toHaveLength(1);
    expect(without.cases[0]).not.toHaveProperty("context");
    warn.mockRestore();
  });
});

describe("context features", () => {
  it("keeps every drawable feature, carrying only its kind", () => {
    const collection = contextFeatures({
      type: "FeatureCollection",
      features: [
        line("route", [
          [119.4, 46.9],
          [119.5, 47.0],
        ]),
        line("protected-area", [RING], "Polygon"),
        line("border", [[[119, 47], [120, 47.3]], [[120, 47.3], [121, 47.1]]], "MultiLineString"),
        line("protected-area", [[RING]], "MultiPolygon"),
      ],
    });
    expect(collection.features.map((feature) => feature.properties)).toEqual([
      { kind: "route" },
      { kind: "protected-area" },
      { kind: "border" },
      { kind: "protected-area" },
    ]);
    expect(collection.features[0]!.geometry).toEqual({
      type: "LineString",
      coordinates: [
        [119.4, 46.9],
        [119.5, 47.0],
      ],
    });
  });

  it("drops a feature it cannot draw, never the file", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const good = line("road", [
      [119, 46],
      [119.1, 46.1],
    ]);
    const collection = contextFeatures({
      type: "FeatureCollection",
      features: [
        good,
        line("canal", [
          [119, 46],
          [119.1, 46.1],
        ]),
        line(undefined, [
          [119, 46],
          [119.1, 46.1],
        ]),
        { type: "Feature", properties: { kind: "road" }, geometry: { type: "Point", coordinates: [119, 46] } },
        line("road", [[119, 46]]),
        line("road", []),
        line("road", [
          [119, 46],
          [Number.NaN, 46.1],
        ]),
        line("road", [
          [119, 46],
          [119.1, 91],
        ]),
        line("road", [
          [119, "46"],
          [119.1, 46.1],
        ]),
        line("protected-area", [RING.slice(0, 3)], "Polygon"),
        line("road", [[119, 46], [119.1, 46.1]], "constructor"),
        { type: "Feature", properties: null, geometry: null },
        { ...good, type: "Thing" },
        null,
      ],
    });
    expect(collection.features).toHaveLength(1);
    expect(collection.features[0]!.properties).toEqual({ kind: "road" });
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });

  it("refuses a file that is no FeatureCollection", () => {
    expect(() => contextFeatures({ type: "Feature" })).toThrow();
    expect(() => contextFeatures({ type: "FeatureCollection" })).toThrow();
    expect(() => contextFeatures(null)).toThrow();
  });
});

/** The kinds a layer's filter admits. */
function filterKinds(filter: unknown): string[] {
  const [operator, , operand] = filter as [string, unknown, unknown];
  if (operator === "==") return [operand as string];
  if (operator === "in") return (operand as [string, string[]])[1];
  throw new Error(`unexpected filter ${JSON.stringify(filter)}`);
}

describe("context layers", () => {
  it("draws every kind, the route last over its casing", () => {
    const layers = contextLayerSpecs(false);
    const drawn = new Set(layers.flatMap((layer) => filterKinds(layer.filter)));
    expect([...drawn].sort()).toEqual([...CONTEXT_KINDS].sort());
    expect(layers.map((layer) => layer.id).slice(-2)).toEqual(["case-context-route-casing", "case-context-route"]);
    expect(layers[0]!.type).toBe("fill");
    expect(filterKinds(layers[0]!.filter)).toEqual(["protected-area"]);
  });

  it("widens the lines with zoom", () => {
    for (const layer of contextLayerSpecs(false)) {
      if (layer.type !== "line") continue;
      const width = layer.paint?.["line-width"] as unknown[];
      expect(width.slice(0, 3)).toEqual(["interpolate", ["linear"], ["zoom"]]);
    }
  });

  it("inks the lines for the ground", () => {
    const light = contextInk(false);
    const dark = contextInk(true);
    expect(light.border).not.toBe(dark.border);
    expect(light.casing).not.toBe(dark.casing);
    const border = (darkGround: boolean) =>
      (contextLayerSpecs(darkGround).find((layer) => layer.id === "case-context-border")!.paint ?? {}) as Record<string, unknown>;
    expect(border(false)["line-color"]).toBe(light.border);
    expect(border(true)["line-color"]).toBe(dark.border);
  });
});

describe("context placement", () => {
  const basemap = new Set(["background", "earth", "water", "roads_major", "coastline", "boundaries_country", "boundaries", "places_city"]);
  const isBasemap = (id: string) => basemap.has(id);
  const style = (...ids: string[]) =>
    ids.map((id) => ({ id, type: id.startsWith("places") || id === "case-markers" ? "symbol" : "line" }));

  it("goes over the field and the basemap's lines, under the first label", () => {
    const layers = style("background", "earth", "water", "roads_major", "field-fill", "coastline", "boundaries_country", "boundaries", "places_city");
    expect(contextBeforeLayer(layers, "coastline", isBasemap)).toBe("places_city");
  });

  it("goes under whatever the app put over the field already", () => {
    const layers = style("earth", "field-fill", "coastline", "boundaries", "fire-cluster", "fire-point", "places_city", "case-markers");
    expect(contextBeforeLayer(layers, "coastline", isBasemap)).toBe("fire-cluster");
    const river = style("earth", "coastline", "boundaries", "river-reaches", "river-gauges", "places_city");
    expect(contextBeforeLayer(river, "coastline", isBasemap)).toBe("river-reaches");
  });

  it("falls back to the first label without the anchor, and to the top without one", () => {
    expect(contextBeforeLayer(style("earth", "boundaries", "places_city"), "coastline", isBasemap)).toBe("places_city");
    expect(contextBeforeLayer(style("earth", "coastline", "boundaries"), "coastline", isBasemap)).toBeUndefined();
  });
});
