/**
 * Well-known binary LineStrings, the reaches' geometry column
 * (`zarr-profile.md`, "Network store": WKB `LineString` in WGS84 longitude
 * and latitude). Only the one geometry type the profile names is read; a
 * reach that is anything else is refused rather than guessed at.
 *
 * Layout (OGC 06-103r4 §8.2): a byte-order byte (1 little-endian, 0
 * big-endian), a `uint32` geometry type (2 for a 2D LineString), a `uint32`
 * point count, then that many (x, y) pairs of `float64`.
 */

const LINESTRING = 2;

/** The line's vertices as interleaved `[lon, lat, lon, lat, …]`. */
export function decodeWkbLineString(bytes: Uint8Array): Float64Array {
  if (bytes.byteLength < 9) throw new Error("WKB geometry is shorter than its header");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const order = view.getUint8(0);
  if (order !== 0 && order !== 1) throw new Error(`WKB byte order ${order} is neither 0 nor 1`);
  const little = order === 1;
  const type = view.getUint32(1, little);
  if (type !== LINESTRING) throw new Error(`WKB geometry type ${type} is not a 2D LineString`);
  const points = view.getUint32(5, little);
  if (points < 2) throw new Error("a WKB LineString needs at least two points");
  const expected = 9 + points * 16;
  if (bytes.byteLength !== expected) throw new Error(`WKB LineString of ${points} points is ${bytes.byteLength} bytes, not ${expected}`);
  const coordinates = new Float64Array(points * 2);
  for (let index = 0; index < points * 2; index += 1) {
    const value = view.getFloat64(9 + index * 8, little);
    if (!Number.isFinite(value)) throw new Error("WKB LineString holds a non-finite coordinate");
    coordinates[index] = value;
  }
  return coordinates;
}

/** Encode a LineString, little-endian: the tests' and fixtures' inverse. */
export function encodeWkbLineString(coordinates: ArrayLike<number>): Uint8Array {
  if (coordinates.length % 2 !== 0 || coordinates.length < 4) throw new Error("a LineString is at least two (x, y) pairs");
  const points = coordinates.length / 2;
  const bytes = new Uint8Array(9 + points * 16);
  const view = new DataView(bytes.buffer);
  view.setUint8(0, 1);
  view.setUint32(1, LINESTRING, true);
  view.setUint32(5, points, true);
  for (let index = 0; index < coordinates.length; index += 1) view.setFloat64(9 + index * 8, coordinates[index]!, true);
  return bytes;
}
