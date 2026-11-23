import { describe, expect, it } from "vitest";

import { ForecastLayer } from "../../web/src/layer";
import type { BundleMetadata } from "../../web/src/manifest";

function field() {
  const layer = new ForecastLayer(() => {});
  const a = {} as WebGLTexture;
  const b = {} as WebGLTexture;
  const palette = {} as WebGLTexture;
  // The GPU handles are borrowed, not copied or uploaded by a consumer.
  Object.assign(layer, {
    hasFrame: true,
    slots: [{ raw: a }, { raw: b }],
    paletteTexture: palette,
    width: 4,
    height: 3,
    firstLongitude: 10,
    firstLatitude: 50,
    longitudeStep: 0.25,
    latitudeStep: -0.25,
    wraps: false,
  });
  return { layer, a, b, palette };
}

describe("river colours from the displayed scalar field", () => {
  it("shares the field's textures, palette, grid, coverage and frame blend", () => {
    const { layer, a, b, palette } = field();
    const coverage = { uStart: 0.25, uEnd: 0.75, vStart: 0, vEnd: 1 };
    layer.setBlend(new Uint8Array(12), new Uint8Array(12), 0.4, coverage);
    const source = layer.scalarColorTextures()!;
    expect(source.a).toBe(a);
    expect(source.b).toBe(b);
    expect(source.palette).toBe(palette);
    expect(source.first).toEqual([10, 50]);
    expect(source.step).toEqual([0.25, -0.25]);
    expect(source.size).toEqual([4, 3]);
    expect(source.wraps).toBe(false);
    expect(source.mix).toBe(0.4);
    expect(source.coverage).toBe(coverage);
  });

  it("does not sample an uninitialised next texture on a single frame", () => {
    const { layer, a } = field();
    layer.setFrame(new Uint8Array(12));
    expect(layer.scalarColorTextures()!.b).toBe(a);
    expect(layer.scalarColorTextures()!.mix).toBe(0);
  });

  it("hides stale colours while switching grids or hiding the field", () => {
    const { layer } = field();
    expect(layer.scalarColorTextures()).not.toBeNull();
    layer.setSuppressed(true);
    expect(layer.scalarColorTextures()).toBeNull();
    layer.setSuppressed(false);
    expect(layer.scalarColorTextures()).not.toBeNull();
    layer.configureGrid({ grid: { width: 2, height: 2 } } as BundleMetadata);
    expect(layer.scalarColorTextures()).toBeNull();
  });
});
