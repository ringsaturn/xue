import { describe, expect, it } from "vitest";
import { TERRAIN_DETAIL_MAX_ZOOM, terrainTileCovered } from "../../web/src/terrainprotocol";

/** The tile holding a point at a zoom, in the Web Mercator scheme. */
function tileAt(latitude: number, longitude: number, zoom: number): [number, number] {
  const tiles = 2 ** zoom;
  const x = Math.floor(((longitude + 180) / 360) * tiles);
  const y = Math.floor(((1 - Math.asinh(Math.tan((latitude * Math.PI) / 180)) / Math.PI) / 2) * tiles);
  return [x, y];
}

describe("terrainTileCovered", () => {
  it("covers the whole planet to z12", () => {
    expect(terrainTileCovered(0, 0, 0)).toBe(true);
    expect(terrainTileCovered(12, ...tileAt(29.5, 103.33, 12))).toBe(true);
  });

  it("goes deeper where a regional archive does", () => {
    // Fuji: Japan's archive reaches z16 (checked against tiles.mapterhorn.com).
    expect(terrainTileCovered(16, ...tileAt(35.36, 138.73, 16))).toBe(true);
    expect(terrainTileCovered(13, ...tileAt(35.36, 138.73, 13))).toBe(true);
  });

  it("stops at z12 where none does", () => {
    // Emei Shan: tiles.mapterhorn.com answers 404 at z13.
    expect(terrainTileCovered(13, ...tileAt(29.5, 103.33, 13))).toBe(false);
  });

  it("stops at the cell's own deepest zoom", () => {
    expect(terrainTileCovered(TERRAIN_DETAIL_MAX_ZOOM + 1, ...tileAt(35.36, 138.73, TERRAIN_DETAIL_MAX_ZOOM + 1))).toBe(
      false,
    );
    expect(TERRAIN_DETAIL_MAX_ZOOM).toBeGreaterThan(12);
  });
});
