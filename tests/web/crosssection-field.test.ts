import { describe, expect, it } from "vitest";

import { SectionField, fillAvailable, frameAt, sectionArrays, type FieldArray, type FieldSpec } from "../../web/src/crosssection/field";
import type { SectionArray } from "../../web/src/crosssection/types";

/** A codebook of `offset + scale * code`, 255 no value, per member. */
function tables(members: number, offset = 0, scale = 1): Float32Array {
  return Float32Array.from({ length: members * 256 }, (_, at) => (at % 256 === 255 ? Number.NaN : offset + scale * (at % 256)));
}

function spec(arrays: FieldArray[], cells = 4): FieldSpec {
  return {
    arrays,
    points: 1,
    cells,
    corners: new Uint32Array([0, 1, 2, 3]),
    weights: new Float32Array([0.5, 0.25, 0.25, 0]),
    bearings: new Float64Array(1),
    latitudes: new Float64Array(1),
  };
}

const TMP: FieldArray = { id: "tmp", levels: [1000, 500], offsets: [0, 3600, 7200], tables: tables(2) };

describe("frameAt", () => {
  const offsets = [0, 3600, 7200, 18000];

  it("is the frame at the lead time", () => {
    expect(frameAt(offsets, 0)).toBe(0);
    expect(frameAt(offsets, 7200)).toBe(2);
    expect(frameAt(offsets, 18000)).toBe(3);
  });

  it("is the earlier of the two around it, and never a later one", () => {
    expect(frameAt(offsets, 10800)).toBe(2);
    expect(frameAt(offsets, 17999)).toBe(2);
  });

  it("is none past either end", () => {
    expect(frameAt(offsets, -1)).toBe(-1);
    expect(frameAt(offsets, 18001)).toBe(-1);
    expect(frameAt([], 0)).toBe(-1);
  });
});

describe("SectionField", () => {
  it("blends a frame by the plan's weights, cells × frames × levels", () => {
    const field = new SectionField(spec([TMP]));
    const codes = new Uint8Array(3 * 3 * 2);
    for (let cell = 0; cell < 3; cell += 1) {
      for (let frame = 0; frame < 3; frame += 1) {
        for (let level = 0; level < 2; level += 1) codes[(cell * 3 + frame) * 2 + level] = cell * 20 + frame * 4 + level;
      }
    }
    field.add("tmp", new Uint32Array([0, 1, 2]), codes);
    const out = new Float32Array(2);
    field.blend("tmp", 1, out);
    // 0.5 × 4 + 0.25 × 24 + 0.25 × 44.
    expect(out[0]).toBeCloseTo(19, 5);
    expect(out[1]).toBeCloseTo(20, 5);
  });

  it("leaves a corner without a reading out, and has none where there is none", () => {
    const array: FieldArray = { id: "tmp", levels: [1000], offsets: [0], tables: tables(1) };
    const field = new SectionField(spec([array]));
    const out = new Float32Array(1);
    field.add("tmp", new Uint32Array([0, 1, 2, 3]), new Uint8Array([10, 40, 255, 255]));
    field.blend("tmp", 0, out);
    // (0.5 × 10 + 0.25 × 40) / 0.75.
    expect(out[0]).toBeCloseTo(20, 5);
    field.add("tmp", new Uint32Array([0, 1]), new Uint8Array([255, 255]));
    field.blend("tmp", 0, out);
    expect(out[0]).toBeNaN();
  });

  it("is ready at a point once every corner that counts has arrived", () => {
    const field = new SectionField(spec([TMP]));
    expect([...field.ready("tmp")!]).toEqual([0]);
    field.add("tmp", new Uint32Array([0, 2]), new Uint8Array(12));
    expect([...field.ready("tmp")!]).toEqual([0]);
    expect(field.complete).toBeCloseTo(0.5, 6);
    // The fourth corner weighs nothing.
    field.add("tmp", new Uint32Array([1]), new Uint8Array(6));
    expect([...field.ready("tmp")!]).toEqual([1]);
    expect(field.complete).toBeCloseTo(0.75, 6);
  });

  it("has nothing at a point off the grid, whatever its corners hold", () => {
    const held = { ...spec([TMP]), points: 2, corners: new Uint32Array(8), weights: new Float32Array([1, 0, 0, 0, 0, 0, 0, 0]), bearings: new Float64Array(2), latitudes: new Float64Array(2) };
    const field = new SectionField(held);
    field.add("tmp", new Uint32Array([0]), new Uint8Array([7, 8, 7, 8, 7, 8]));
    const out = new Float32Array(4);
    field.blend("tmp", 0, out);
    expect([...out]).toEqual([7, 8, Number.NaN, Number.NaN]);
    expect([...field.ready("tmp")!]).toEqual([1, 0]);
    // One point at a time, as a reading is taken.
    const one = new Float32Array(2);
    field.blend("tmp", 0, one, 1, 2);
    expect([...one]).toEqual([Number.NaN, Number.NaN]);
  });

  it("refuses codes that are not its cells'", () => {
    const field = new SectionField(spec([TMP]));
    expect(() => field.add("tmp", new Uint32Array([0]), new Uint8Array(5))).toThrow();
    expect(() => field.add("tmp", new Uint32Array([4]), new Uint8Array(6))).toThrow();
    // An array the section has no use for is not its business.
    expect(() => field.add("pwat", new Uint32Array([0]), new Uint8Array(1))).not.toThrow();
  });

  it("holds a surface field of one frame at every lead time", () => {
    const orog: FieldArray = { id: "orog", levels: null, offsets: [0], tables: tables(1) };
    const field = new SectionField(spec([TMP, orog]));
    expect(field.frame("orog", 7200)).toBe(0);
    expect(field.frame("tmp", 7200)).toBe(2);
    expect(field.frame("tmp", 7201)).toBe(-1);
    expect(field.leadOf(5000)).toBe(3600);
    expect(field.leadOf(7201)).toBeNull();
  });
});

describe("sectionArrays", () => {
  const array = (id: string, levels: number[] | null, quantity = id): [string, SectionArray] => [
    id,
    { id, quantity: quantity as SectionArray["quantity"], levels, offsets: [0], members: [] },
  ];

  it("keeps the arrays a section reads, in the shape it reads them", () => {
    const arrays = new Map([array("tmp", [500]), array("orog", null), array("vvel", [500]), array("tmp2m", [2])]);
    expect([...sectionArrays({ arrays }).keys()]).toEqual(["orog", "tmp"]);
  });

  it("goes by what an array is of, not by its name", () => {
    const arrays = new Map([array("tmp", [500], "rh"), array("t", [500], "tmp"), array("tmp_again", [850], "tmp")]);
    expect([...sectionArrays({ arrays })].map(([quantity, { id }]) => [quantity, id])).toEqual([
      ["tmp", "t"],
      ["rh", "tmp"],
    ]);
  });

  it("says which fills a store can draw", () => {
    const arrays = new Map([array("tmp", [500]), array("hgt", [500])]);
    expect(fillAvailable("tmp", { arrays })).toBe(true);
    expect(fillAvailable("thetae", { arrays })).toBe(false);
    expect(fillAvailable("cross", { arrays })).toBe(false);
  });
});
