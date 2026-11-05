import { describe, expect, it, vi } from "vitest";

import { COASTLINE_LAYER, parseBasemapHidden, RIVER_LAYER, withoutHiddenLayers, withRivers } from "../../web/src/basemap";
import { loadPointerIndex, parsePointProducts } from "../../web/src/pointerindex";

describe("parsePointProducts", () => {
  it("reads every product when unset or empty", () => {
    expect(parsePointProducts(undefined)).toBeNull();
    expect(parsePointProducts(" , ")).toBeNull();
  });

  it("lists the named ones, `none` naming no product", () => {
    expect([...parsePointProducts("tc, aq")!]).toEqual(["tc", "aq"]);
    expect(parsePointProducts("none")!.has("tc")).toBe(false);
  });
});

describe("withRivers", () => {
  const layers = [
    { id: "water", type: "fill" },
    { id: RIVER_LAYER, type: "line", paint: { "line-color": "#dcdcdc" } },
    { id: COASTLINE_LAYER, type: "line" },
    { id: "boundaries_country", type: "line" },
    { id: "water_waterway_label", type: "symbol", minzoom: 13 },
  ] as unknown as Parameters<typeof withRivers>[0];

  it("puts the rivers right over the coastline, where the fields stop", () => {
    expect(withRivers(layers).map((layer) => layer.id)).toEqual([
      "water",
      COASTLINE_LAYER,
      RIVER_LAYER,
      "boundaries_country",
      "water_waterway_label",
    ]);
  });

  it("gives the rivers a color of their own, the same in every build", () => {
    const river = withRivers(layers).find((layer) => layer.id === RIVER_LAYER) as { paint: Record<string, unknown> };
    expect(river.paint["line-color"]).not.toBe("#dcdcdc");
  });

  it("draws named rivers only, which leaves out the connectors across lakes", () => {
    const flavored = layers.map((layer) =>
      layer.id === RIVER_LAYER ? { ...layer, filter: ["in", "kind", "river"] } : layer,
    ) as Parameters<typeof withRivers>[0];
    const river = withRivers(flavored).find((layer) => layer.id === RIVER_LAYER) as { filter: unknown };
    expect(river.filter).toEqual(["all", ["in", "kind", "river"], ["has", "name"]]);
  });

  it("brings the river names forward", () => {
    const label = withRivers(layers).find((layer) => layer.id === "water_waterway_label");
    expect(label?.minzoom).toBe(10);
  });

  it("refuses a flavor without a river layer", () => {
    expect(() => withRivers(layers.filter((layer) => layer.id !== RIVER_LAYER))).toThrow("no river layer");
  });
});

describe("withoutHiddenLayers", () => {
  const layers = [
    { id: "earth", type: "fill", source: "s" },
    { id: "boundaries_country", type: "line", source: "s", layout: { "line-cap": "round" } },
    { id: "places_country", type: "symbol", source: "s" },
  ] as unknown as Parameters<typeof withoutHiddenLayers>[0];
  const visibility = (hidden: string) =>
    withoutHiddenLayers(layers, parseBasemapHidden(hidden)).map(
      (layer) => (layer as { layout?: { visibility?: string } }).layout?.visibility ?? "visible",
    );

  it("leaves every layer as it is by default", () => {
    expect(visibility("")).toEqual(["visible", "visible", "visible"]);
  });

  it("hides labels and boundaries independently, keeping every layer", () => {
    expect(visibility("labels")).toEqual(["visible", "visible", "none"]);
    expect(visibility("boundaries")).toEqual(["visible", "none", "visible"]);
    expect(visibility("labels, boundaries")).toEqual(["visible", "none", "none"]);
  });

  it("keeps a hidden layer's other layout properties", () => {
    const [, boundaries] = withoutHiddenLayers(layers, parseBasemapHidden("boundaries"));
    expect((boundaries as { layout: Record<string, string> }).layout["line-cap"]).toBe("round");
  });
});

describe("loadPointerIndex", () => {
  it("asks for a product by default", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("document", { baseURI: "https://example.test/" });
    await expect(loadPointerIndex("data/", "latest-tc.json", (x) => x as never, (x) => x, "tc")).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });
});
