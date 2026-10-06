import { describe, expect, it } from "vitest";

import { worldPixels, zoomForSpan } from "../../web/src/mercator";

describe("zoomForSpan", () => {
  it("is the zoom at which a span fills the given pixels", () => {
    // The whole world is one 512 px tile at zoom 0; half of it fills that
    // width one level deeper.
    expect(zoomForSpan(1, 512)).toBe(0);
    expect(zoomForSpan(0.5, 512)).toBe(1);
    const zoom = zoomForSpan(0.02 / 360, 16);
    expect(worldPixels(zoom) * (0.02 / 360)).toBeCloseTo(16, 9);
  });
});
