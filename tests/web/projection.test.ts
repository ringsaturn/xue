import { describe, expect, it } from "vitest";

import { gridMesh, mercatorYUnclamped, SKIRT_OFFSET, skirtedTileMesh, withDefines, worldRowAt } from "../../web/src/projection";
import { parseSceneFromSearch, searchWithScene, DEFAULT_TERRAIN_EXAGGERATION } from "../../web/src/urlstate";

describe("gridMesh", () => {
  it("covers the unit square with two triangles per cell", () => {
    const mesh = gridMesh(4, 2);
    expect(mesh.positions.length).toBe(5 * 3 * 2);
    expect(mesh.indices.length).toBe(4 * 2 * 6);
    expect(Math.max(...mesh.indices)).toBe(5 * 3 - 1);
    expect([...mesh.positions.slice(-2)]).toEqual([1, 1]);
  });

  it("splits a cell along MapLibre's terrain diagonal", () => {
    // Top-left to bottom-right, so a tile mesh is the terrain's triangles.
    expect([...gridMesh(1, 1).indices]).toEqual([0, 2, 3, 0, 3, 1]);
  });
});

describe("skirtedTileMesh", () => {
  it("hangs one skirt vertex from every edge vertex", () => {
    const size = 4;
    const mesh = skirtedTileMesh(size);
    const gridVertices = (size + 1) ** 2;
    const skirtVertices = mesh.positions.length / 2 - gridVertices;
    expect(skirtVertices).toBe(4 * size);
    for (let vertex = gridVertices; vertex < mesh.positions.length / 2; vertex += 1) {
      const x = mesh.positions[vertex * 2]! - SKIRT_OFFSET;
      const y = mesh.positions[vertex * 2 + 1]! - SKIRT_OFFSET;
      expect(x === 0 || x === 1 || y === 0 || y === 1).toBe(true);
    }
    expect(Math.max(...mesh.indices)).toBe(mesh.positions.length / 2 - 1);
  });
});

describe("world mesh rows", () => {
  it("run pole to pole, past the Mercator square at both ends", () => {
    const rowAt = worldRowAt(128);
    expect(rowAt(0)).toBeLessThan(0);
    expect(rowAt(128)).toBeGreaterThan(1);
    expect(rowAt(64)).toBeCloseTo(0.5, 12);
    expect(mercatorYUnclamped(0)).toBeCloseTo(0.5, 12);
  });
});

describe("withDefines", () => {
  it("puts defines right after the version line", () => {
    expect(withDefines("#version 300 es\nvoid main() {}", ["A", "B"])).toBe(
      "#version 300 es\n#define A\n#define B\nvoid main() {}",
    );
    expect(withDefines("#version 300 es\nx", [])).toBe("#version 300 es\nx");
  });
});

describe("scene URL state", () => {
  it("defaults to a flat plane", () => {
    expect(parseSceneFromSearch("")).toEqual({ globe: false, terrain: null, shadow: false, contours: false });
    expect(parseSceneFromSearch("?projection=mercator&terrain=off")).toEqual({ globe: false, terrain: null, shadow: false, contours: false });
    expect(parseSceneFromSearch("?terrain=banana")).toEqual({ globe: false, terrain: null, shadow: false, contours: false });
  });

  it("reads the globe and the relief", () => {
    expect(parseSceneFromSearch("?projection=Globe&terrain=on")).toEqual({
      globe: true,
      terrain: DEFAULT_TERRAIN_EXAGGERATION,
      shadow: false,
      contours: false,
    });
    expect(parseSceneFromSearch("?terrain=2.6").terrain).toBe(2.6);
    expect(parseSceneFromSearch("?terrain=50").terrain).toBe(10);
    expect(parseSceneFromSearch("?terrain=0").terrain).toBeNull();
  });

  it("writes only what differs from the default and round-trips", () => {
    expect(searchWithScene("?model=gfs", { globe: false, terrain: null, shadow: false, contours: false })).toBe("?model=gfs");
    expect(searchWithScene("?model=gfs&projection=globe&terrain=on", { globe: false, terrain: null, shadow: false, contours: false })).toBe("?model=gfs");
    const on = searchWithScene("?model=gfs", { globe: true, terrain: DEFAULT_TERRAIN_EXAGGERATION, shadow: false, contours: false });
    expect(on).toBe("?model=gfs&projection=globe&terrain=on");
    expect(parseSceneFromSearch(on)).toEqual({ globe: true, terrain: DEFAULT_TERRAIN_EXAGGERATION, shadow: false, contours: false });
    expect(searchWithScene("", { globe: false, terrain: 2.6, shadow: false, contours: false })).toBe("?terrain=2.6");
  });
});

describe("particle pacing and seeding", () => {
  it("keeps the speed below the reference zoom and halves it per level above", async () => {
    const { zoomPace } = await import("../../web/src/particles");
    expect(zoomPace(1.65)).toBe(1);
    expect(zoomPace(4)).toBe(1);
    expect(zoomPace(5)).toBe(0.5);
    expect(zoomPace(10)).toBe(1 / 64);
  });
});
