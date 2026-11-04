import { describe, expect, it, vi } from "vitest";

import { parseBasemapHidden, withoutHiddenLayers } from "../../web/src/basemap";
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
