import { describe, expect, it } from "vitest";

import type { LambertDomain } from "../../web/src/domain";
import { ForecastLayer, type ScalarColorTextures } from "../../web/src/layer";
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

/** The borrowed textures, failing the test on `null` or `"pending"`. */
function shared(layer: ForecastLayer): ScalarColorTextures {
  const source = layer.scalarColorTextures();
  if (source === null || source === "pending") throw new Error(`no shared field: ${source}`);
  return source;
}

describe("river colours from the displayed scalar field", () => {
  it("shares the field's textures, palette, grid, coverage and frame blend", () => {
    const { layer, a, b, palette } = field();
    const coverage = { uStart: 0.25, uEnd: 0.75, vStart: 0, vEnd: 1 };
    layer.setBlend(new Uint8Array(12), new Uint8Array(12), 0.4, coverage);
    const source = shared(layer);
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
    expect(shared(layer).b).toBe(a);
    expect(shared(layer).mix).toBe(0);
  });

  it("shares the native projected grid and its uploaded table, never a stale table", () => {
    const { layer } = field();
    const form: GridForm = { first: [-2700000, -1500000], step: [3000, 3000], cone: [0.6, 12000000, 8000000, -1.7] };
    const table = { box: [0.1, 0.2, 0.4, 0.5], cone: form.cone } as GridTable;
    const texture = {} as WebGLTexture;
    Object.assign(layer, { form, gridTable: table, gridTableTexture: texture });
    expect(layer.scalarColorTextures()).toBe("pending");
    Object.assign(layer, { gridTableUploaded: table });
    expect(shared(layer).form).toBe(form);
    expect(shared(layer).table).toEqual({ texture, box: table.box, cone: table.cone });
    Object.assign(layer, { gridTable: { ...table } });
    expect(layer.scalarColorTextures()).toBe("pending");
  });

  it("waits for a new grid's frame, and falls back when there is no scalar fill", () => {
    const { layer } = field();
    expect(layer.scalarColorTextures()).toBeTypeOf("object");
    layer.setSuppressed(true);
    expect(layer.scalarColorTextures()).toBeNull();
    layer.setSuppressed(false);
    expect(layer.scalarColorTextures()).toBeTypeOf("object");
    layer.configureGrid({ grid: { width: 2, height: 2 } } as BundleMetadata);
    expect(layer.scalarColorTextures()).toBe("pending");
    Object.assign(layer, { vector: {} });
    expect(layer.scalarColorTextures()).toBeNull();
  });

  it("steps a class field between frames at the midpoint, as the fill does", () => {
    const { layer, a, b } = field();
    Object.assign(layer, { sampling: "classes", mixWeight: 0.4 });
    const before = shared(layer);
    expect(before.a).toBe(a);
    expect(before.mix).toBe(0);
    Object.assign(layer, { mixWeight: 0.6 });
    const after = shared(layer);
    expect(after.a).toBe(b);
    expect(after.mix).toBe(0);
  });

  it("carries the field's domain and longitude band", () => {
    const { layer } = field();
    const band = { start: 90, width: 180 };
    const domain = {} as LambertDomain;
    layer.setBand(band);
    layer.setDomain(domain);
    const source = shared(layer);
    expect(source.band).toBe(band);
    expect(source.domain).toBe(domain);
  });
});
