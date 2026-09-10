import { describe, expect, it } from "vitest";

import {
  coverageBox,
  coversTiles,
  flattenTileRects,
  parseTileGeometry,
  sameTileRects,
  tileCount,
  tileFraction,
  viewportTileRects,
  WHOLE_PLANE_COVERAGE,
  type TileGeometry,
  type TileRect,
} from "../../web/src/tiles";
import type { BundleMetadata } from "../../web/src/manifest";

/** The global quarter-degree production grid, as a bundle declares it. */
function globalGrid(overrides: Record<string, unknown> = {}): BundleMetadata {
  return {
    schemaVersion: 3,
    model: "GFS",
    runTime: "2026-08-15T06:00:00Z",
    time: { frameCount: 3, unitSeconds: 3600, firstFrameOffset: 0, frameStep: 1 },
    grid: {
      width: 1440,
      height: 721,
      firstLongitude: -180,
      firstLatitude: 90,
      longitudeStep: 0.25,
      latitudeStep: -0.25,
      wrapLongitude: true,
      ...overrides,
    },
    variables: [],
  } as unknown as BundleMetadata;
}

/** A showcase crop over the western Pacific: 10 degrees square, no wrap. */
function croppedGrid(): BundleMetadata {
  return globalGrid({
    width: 40,
    height: 40,
    firstLongitude: 110,
    firstLatitude: 30,
    wrapLongitude: false,
  });
}

/** The production tiling of the quarter-degree grid: 30 x 14 tiles. */
const GFS_TILES: TileGeometry = { tileWidth: 48, tileHeight: 52, columns: 30, rows: 14 };
/** The crop above, cut by the same policy: tiles clamped to the grid. */
const CROP_TILES: TileGeometry = { tileWidth: 40, tileHeight: 40, columns: 1, rows: 1 };

describe("parseTileGeometry", () => {
  it("reads the four numbers the decoder reports", () => {
    expect(parseTileGeometry(new Uint32Array([48, 52, 30, 14]))).toEqual(GFS_TILES);
  });

  it("is null for a v1 bundle and for anything degenerate", () => {
    expect(parseTileGeometry(undefined)).toBeNull();
    expect(parseTileGeometry(new Uint32Array([48, 52]))).toBeNull();
    expect(parseTileGeometry(new Uint32Array([48, 0, 30, 14]))).toBeNull();
  });
});

describe("viewportTileRects", () => {
  it("is null when the view spans the world, so the caller takes the whole plane", () => {
    const rects = viewportTileRects(globalGrid(), GFS_TILES, {
      west: -180,
      east: 180,
      south: -85,
      north: 85,
    });
    expect(rects).toBeNull();
  });

  it("is null one degree short of the world too — a near-global view is not a sliver", () => {
    // Wrapped endpoints alone would read this as two tiles at the seam.
    const rects = viewportTileRects(globalGrid(), GFS_TILES, {
      west: -179.5,
      east: 179.5,
      south: -85,
      north: 85,
    });
    expect(rects).toBeNull();
  });

  it("names the tiles a regional view touches, padded by one on each side", () => {
    // 100..120 E, 20..40 N: cells 1120..1200 across and 200..280 down, so
    // tile columns 23..25 and tile rows 3..5 before the one-tile padding.
    const rects = viewportTileRects(globalGrid(), GFS_TILES, {
      west: 100,
      east: 120,
      south: 20,
      north: 40,
    });
    expect(rects).toEqual([{ firstColumn: 22, firstRow: 2, lastColumn: 26, lastRow: 6 }]);
    expect(tileCount(rects!)).toBe(25);
    expect(tileFraction(GFS_TILES, rects)).toBeCloseTo(25 / 420);
  });

  it("splits a view straddling the antimeridian into two column ranges", () => {
    // 170 E .. 190 E (= 170 W): the western columns and the eastern ones,
    // with no tiles in between.
    const rects = viewportTileRects(globalGrid(), GFS_TILES, {
      west: 170,
      east: 190,
      south: 20,
      north: 40,
    });
    expect(rects).toHaveLength(2);
    expect(rects![0]!.lastColumn).toBe(GFS_TILES.columns - 1);
    expect(rects![1]!.firstColumn).toBe(0);
    expect(rects![0]!.firstColumn).toBeGreaterThan(rects![1]!.lastColumn + 1);
    for (const rect of rects!) {
      expect(rect.firstRow).toBe(2);
      expect(rect.lastRow).toBe(6);
    }
  });

  it("clamps rows at the poles rather than running off the grid", () => {
    const rects = viewportTileRects(globalGrid(), GFS_TILES, {
      west: 0,
      east: 20,
      south: 80,
      north: 90,
    });
    expect(rects![0]!.firstRow).toBe(0);
  });

  it("keeps a cropped grid's columns unwrapped: a view to its west still hits it", () => {
    // The crop starts at 110 E; a view from 100 E to 130 E covers all of it,
    // which a 360-degree wrap would have placed most of the world away.
    expect(
      viewportTileRects(croppedGrid(), CROP_TILES, { west: 100, east: 130, south: 15, north: 35 }),
    ).toBeNull();
  });

  it("is null when the view misses a cropped grid altogether", () => {
    expect(
      viewportTileRects(croppedGrid(), CROP_TILES, { west: -60, east: -40, south: 20, north: 30 }),
    ).toBeNull();
    expect(
      viewportTileRects(croppedGrid(), CROP_TILES, { west: 112, east: 118, south: 60, north: 70 }),
    ).toBeNull();
  });

  it("is null for a degenerate grid or an unusable span", () => {
    const noGrid = globalGrid({ width: 0 });
    expect(viewportTileRects(noGrid, GFS_TILES, { west: 0, east: 1, south: 0, north: 1 })).toBeNull();
    expect(
      viewportTileRects(globalGrid(), GFS_TILES, { west: 0, east: Number.NaN, south: 0, north: 1 }),
    ).toBeNull();
  });
});

describe("flattenTileRects", () => {
  it("packs rectangles the way the WASM boundary reads them", () => {
    const rects: TileRect[] = [
      { firstColumn: 1, firstRow: 2, lastColumn: 3, lastRow: 4 },
      { firstColumn: 5, firstRow: 6, lastColumn: 7, lastRow: 8 },
    ];
    expect(Array.from(flattenTileRects(rects))).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });
});

describe("coversTiles", () => {
  const inner: TileRect = { firstColumn: 4, firstRow: 4, lastColumn: 5, lastRow: 5 };
  const outer: TileRect = { firstColumn: 3, firstRow: 3, lastColumn: 6, lastRow: 6 };

  it("treats the whole plane as covering anything", () => {
    expect(coversTiles(null, [inner])).toBe(true);
    expect(coversTiles(null, null)).toBe(true);
  });

  it("does not let a rectangle stand in for the whole plane", () => {
    expect(coversTiles([outer], null)).toBe(false);
  });

  it("accepts a wanted rectangle inside a held one and rejects one that spills out", () => {
    expect(coversTiles([outer], [inner])).toBe(true);
    expect(coversTiles([inner], [outer])).toBe(false);
    expect(
      coversTiles([outer], [{ firstColumn: 6, firstRow: 3, lastColumn: 7, lastRow: 4 }]),
    ).toBe(false);
  });
});

describe("coverageBox", () => {
  it("is the whole texture for a whole plane", () => {
    expect(coverageBox(GFS_TILES, 1440, 721, null)).toEqual(WHOLE_PLANE_COVERAGE);
    expect(coverageBox(GFS_TILES, 1440, 721, [])).toEqual(WHOLE_PLANE_COVERAGE);
  });

  it("spans cell edges, not centers, and clamps at the far edge", () => {
    const box = coverageBox(GFS_TILES, 1440, 721, [
      { firstColumn: 2, firstRow: 0, lastColumn: 3, lastRow: 13 },
    ]);
    expect(box.uStart).toBeCloseTo((2 * 48) / 1440);
    expect(box.uEnd).toBeCloseTo((4 * 48) / 1440);
    // The last tile row is clipped by the grid, so v stops at 1 rather than
    // running past the pole.
    expect(box).toMatchObject({ vStart: 0, vEnd: 1 });
  });

  it("writes a straddling pair as one wrapped box", () => {
    const box = coverageBox(GFS_TILES, 1440, 721, [
      { firstColumn: 28, firstRow: 2, lastColumn: 29, lastRow: 6 },
      { firstColumn: 0, firstRow: 2, lastColumn: 1, lastRow: 6 },
    ]);
    // uStart > uEnd is what tells the shader the box wraps.
    expect(box.uStart).toBeCloseTo((28 * 48) / 1440);
    expect(box.uEnd).toBeCloseTo((2 * 48) / 1440);
    expect(box.uStart).toBeGreaterThan(box.uEnd);
    expect(box.vStart).toBeCloseTo((2 * 52) / 721);
    expect(box.vEnd).toBeCloseTo((7 * 52) / 721);
  });
});

describe("sameTileRects", () => {
  const rect: TileRect = { firstColumn: 1, firstRow: 2, lastColumn: 3, lastRow: 4 };

  it("compares by value and treats the whole plane as its own answer", () => {
    expect(sameTileRects([rect], [{ ...rect }])).toBe(true);
    expect(sameTileRects(null, null)).toBe(true);
    expect(sameTileRects(null, [rect])).toBe(false);
    expect(sameTileRects([rect], [{ ...rect, lastRow: 5 }])).toBe(false);
    expect(sameTileRects([rect], [rect, rect])).toBe(false);
  });
});
