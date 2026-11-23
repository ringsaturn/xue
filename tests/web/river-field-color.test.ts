import { describe, expect, it } from "vitest";

import { ForecastLayer } from "../../web/src/layer";
import type { BundleMetadata } from "../../web/src/manifest";
import type { GridForm, GridTable } from "../../web/src/gridmap";

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
    form: { first: [10, 50], step: [0.25, -0.25], cone: [0, 0, 0, 0] },
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
    expect(source.form).toEqual({ first: [10, 50], step: [0.25, -0.25], cone: [0, 0, 0, 0] });
    expect(source.table).toBeNull();
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

  it("shares the native projected grid and its uploaded table, never a stale table", () => {
    const { layer } = field();
    const form: GridForm = { first: [-2700000, -1500000], step: [3000, 3000], cone: [0.6, 12000000, 8000000, -1.7] };
    const table = { box: [0.1, 0.2, 0.4, 0.5], cone: form.cone } as GridTable;
    const texture = {} as WebGLTexture;
    Object.assign(layer, { form, gridTable: table, gridTableTexture: texture });
    expect(layer.scalarColorTextures()).toBeNull();
    Object.assign(layer, { gridTableUploaded: table });
    expect(layer.scalarColorTextures()!.form).toBe(form);
    expect(layer.scalarColorTextures()!.table).toEqual({ texture, box: table.box, cone: table.cone });
    Object.assign(layer, { gridTable: { ...table } });
    expect(layer.scalarColorTextures()).toBeNull();
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
