import { describe, expect, it } from "vitest";

import { decodeWkbLineString, encodeWkbLineString } from "../../web/src/river/wkb";

describe("WKB LineString", () => {
  it("round-trips a line", () => {
    const line = [-99.32, 30.07, -99.31, 30.071, -99.3, 30.069];
    expect(Array.from(decodeWkbLineString(encodeWkbLineString(line)))).toEqual(line);
  });

  it("reads a big-endian line", () => {
    const bytes = new Uint8Array(9 + 2 * 16);
    const view = new DataView(bytes.buffer);
    view.setUint8(0, 0);
    view.setUint32(1, 2, false);
    view.setUint32(5, 2, false);
    [1.5, 2.5, 3.5, 4.5].forEach((value, index) => view.setFloat64(9 + index * 8, value, false));
    expect(Array.from(decodeWkbLineString(bytes))).toEqual([1.5, 2.5, 3.5, 4.5]);
  });

  it("reads a line from a view into a larger buffer", () => {
    const encoded = encodeWkbLineString([0, 0, 1, 1]);
    const padded = new Uint8Array(encoded.byteLength + 3);
    padded.set(encoded, 3);
    expect(Array.from(decodeWkbLineString(padded.subarray(3)))).toEqual([0, 0, 1, 1]);
  });

  it("refuses anything but a 2D LineString of the stated length", () => {
    const point = encodeWkbLineString([0, 0, 1, 1]);
    new DataView(point.buffer).setUint32(1, 1, true);
    expect(() => decodeWkbLineString(point)).toThrow(/type 1/);
    const z = encodeWkbLineString([0, 0, 1, 1]);
    new DataView(z.buffer).setUint32(1, 1002, true);
    expect(() => decodeWkbLineString(z)).toThrow(/type 1002/);
    expect(() => decodeWkbLineString(encodeWkbLineString([0, 0, 1, 1]).subarray(0, 30))).toThrow(/bytes/);
    const order = encodeWkbLineString([0, 0, 1, 1]);
    order[0] = 7;
    expect(() => decodeWkbLineString(order)).toThrow(/byte order/);
    const single = encodeWkbLineString([0, 0, 1, 1]);
    new DataView(single.buffer).setUint32(5, 1, true);
    expect(() => decodeWkbLineString(single.subarray(0, 25))).toThrow(/two points/);
    const nan = encodeWkbLineString([0, 0, 1, Number.NaN]);
    expect(() => decodeWkbLineString(nan)).toThrow(/non-finite/);
    expect(() => decodeWkbLineString(new Uint8Array(4))).toThrow(/header/);
  });
});
