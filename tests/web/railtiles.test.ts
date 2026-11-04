import { describe, expect, it } from "vitest";

import { genericRailGlyph, genericRailTileIds } from "../../web/src/railtiles";

describe("genericRailTileIds", () => {
  const written = (id: string) => id === "tmp2m";

  it("lists every published core field without a written tile, in core order", () => {
    expect(genericRailTileIds(["b", "a", "c"], () => true, "a", written)).toEqual(["b", "a", "c"]);
  });

  it("skips what the run does not publish and what a written tile stands for", () => {
    expect(genericRailTileIds(["tmp2m", "a", "b"], (id) => id !== "b", null, written)).toEqual(["a"]);
  });

  it("adds a field on screen that is outside the core, after the core", () => {
    expect(genericRailTileIds(["a"], () => true, "z", written)).toEqual(["a", "z"]);
  });

  it("writes nothing for a field on screen with a written tile", () => {
    expect(genericRailTileIds([], () => true, "tmp2m", written)).toEqual([]);
  });
});

describe("genericRailGlyph", () => {
  it("is the initial when no sibling shares it", () => {
    expect(genericRailGlyph("alpha", ["alpha", "beta"])).toBe("A");
  });

  it("adds the first differing letter when a sibling shares the initial", () => {
    const ids = ["alpha", "bravo", "brick"];
    expect(ids.map((id) => genericRailGlyph(id, ids))).toEqual(["A", "BA", "BI"]);
  });

  it("settles on two letters when one id is a prefix of the other", () => {
    expect(genericRailGlyph("ab", ["ab", "abc"])).toBe("AB");
  });
});
