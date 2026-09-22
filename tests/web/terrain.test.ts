import { describe, expect, it } from "vitest";

import {
  demSampleFor,
  elevationFromRgba,
  terrariumElevation,
  terrainTileUrl,
  TERRAIN_TILE_SIZE,
} from "../../web/src/terrain";

/** Pack one elevation the way a Terrarium tile carries it. */
function packed(elevation: number): [number, number, number] {
  const metres = elevation + 32768;
  const red = Math.floor(metres / 256);
  const green = Math.floor(metres - red * 256);
  const blue = Math.round((metres - red * 256 - green) * 256);
  return [red, green, blue];
}

describe("terrariumElevation", () => {
  it("reads the three bytes back as metres", () => {
    // The encoding is metres + 32768 across a high byte, a low byte and a
    // 1/256 m fraction; sea level is therefore 128/0/0, not 0/0/0.
    expect(terrariumElevation(128, 0, 0)).toBe(0);
    expect(terrariumElevation(...packed(1000))).toBeCloseTo(1000, 6);
    expect(terrariumElevation(...packed(-430))).toBeCloseTo(-430, 6);
    expect(terrariumElevation(...packed(8849))).toBeCloseTo(8849, 6);
    // The blue byte is a fraction of a metre, not another byte.
    expect(terrariumElevation(128, 0, 128)).toBeCloseTo(0.5, 6);
  });
});

describe("elevationFromRgba", () => {
  it("picks the pixel the index names", () => {
    // A 2x2 tile of elevations 0, 1000, 2000, 3000 in row-major order.
    const points = [0, 1000, 2000, 3000];
    const data = new Uint8ClampedArray(points.length * 4);
    points.forEach((elevation, index) => {
      const [red, green, blue] = packed(elevation);
      data.set([red, green, blue, 255], index * 4);
    });
    expect(elevationFromRgba(data, 2, 0, 0)).toBeCloseTo(0, 6);
    expect(elevationFromRgba(data, 2, 1, 0)).toBeCloseTo(1000, 6);
    expect(elevationFromRgba(data, 2, 0, 1)).toBeCloseTo(2000, 6);
    expect(elevationFromRgba(data, 2, 1, 1)).toBeCloseTo(3000, 6);
  });
});

describe("demSampleFor", () => {
  it("puts the equator and prime meridian at the middle pixel of the world", () => {
    // At z0 the whole world is one tile, and 0/0° is its centre.
    expect(demSampleFor(0, 0, 0)).toEqual({
      zoom: 0,
      x: 0,
      y: 0,
      pixelX: TERRAIN_TILE_SIZE / 2,
      pixelY: TERRAIN_TILE_SIZE / 2,
    });
  });

  it("names the tile and pixel of a point in the northern hemisphere", () => {
    // Beijing's neighbourhood at the archive's ceiling: the tile MapLibre
    // asks for, and where in it the point falls.
    const sample = demSampleFor(39.9, 116.4, 12);
    expect(sample).toMatchObject({ zoom: 12, x: 3372, y: 1552 });
    expect(sample.pixelX).toBeGreaterThanOrEqual(0);
    expect(sample.pixelX).toBeLessThan(TERRAIN_TILE_SIZE);
    expect(sample.pixelY).toBeGreaterThanOrEqual(0);
    expect(sample.pixelY).toBeLessThan(TERRAIN_TILE_SIZE);
  });

  it("clamps a point at the poles onto the edge tile rather than past it", () => {
    for (const latitude of [90, -90, 91, 85.0511287]) {
      const sample = demSampleFor(latitude, 0, 2);
      expect(sample.y).toBeGreaterThanOrEqual(0);
      expect(sample.y).toBeLessThan(2 ** 2);
      expect(sample.pixelY).toBeGreaterThanOrEqual(0);
      expect(sample.pixelY).toBeLessThan(TERRAIN_TILE_SIZE);
    }
  });

  it("keeps longitude inside the world's own wrap", () => {
    // 180° is the last tile's far edge, not a tile beyond it.
    const sample = demSampleFor(0, 180, 3);
    expect(sample.x).toBe(2 ** 3 - 1);
    expect(sample.pixelX).toBe(TERRAIN_TILE_SIZE - 1);
  });
});

describe("terrainTileUrl", () => {
  it("fills every placeholder", () => {
    const url = terrainTileUrl(12, 3372, 1552);
    expect(url).toBe("https://tiles.mapterhorn.com/12/3372/1552.webp");
    expect(url).not.toMatch(/\{[xyz]\}/);
  });
});
