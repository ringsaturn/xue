import { describe, expect, it } from "vitest";

import { parseLinearGradient } from "../../web/src/stillexport";

describe("parseLinearGradient", () => {
  it("spreads unplaced stops top to bottom", () => {
    const parsed = parseLinearGradient("linear-gradient(rgb(255, 0, 0), rgb(0, 255, 0), rgb(0, 0, 255))");
    expect(parsed?.angle).toBe(180);
    expect(parsed?.stops).toEqual([
      { color: "rgb(255, 0, 0)", at: 0 },
      { color: "rgb(0, 255, 0)", at: 0.5 },
      { color: "rgb(0, 0, 255)", at: 1 },
    ]);
  });

  it("reads a direction, placed stops and hard steps", () => {
    const parsed = parseLinearGradient("linear-gradient(to right, red 0%, red 25%, rgba(0, 0, 0, 0.5) 25% 50%, blue)");
    expect(parsed?.angle).toBe(90);
    expect(parsed?.stops.map((stop) => stop.at)).toEqual([0, 0.25, 0.25, 0.5, 1]);
    expect(parsed?.stops[2]?.color).toBe("rgba(0, 0, 0, 0.5)");
  });

  it("reads an angle and gives null for no gradient", () => {
    expect(parseLinearGradient("linear-gradient(0deg, #000, #fff)")?.angle).toBe(0);
    expect(parseLinearGradient("none")).toBeNull();
  });
});
