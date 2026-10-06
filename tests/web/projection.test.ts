import { describe, expect, it } from "vitest";

import type { Map as MaplibreMap } from "maplibre-gl";

import { gridMesh, keepTerrainCameraStill, mercatorYUnclamped, moveCenterOnRay, SKIRT_OFFSET, skirtedTileMesh, withDefines, worldRowAt } from "../../web/src/projection";
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
    expect(parseSceneFromSearch("")).toEqual({ globe: false, terrain: null, shadow: false });
    expect(parseSceneFromSearch("?projection=mercator&terrain=off")).toEqual({ globe: false, terrain: null, shadow: false });
    expect(parseSceneFromSearch("?terrain=banana")).toEqual({ globe: false, terrain: null, shadow: false });
  });

  it("reads the globe and the relief", () => {
    expect(parseSceneFromSearch("?projection=Globe&terrain=on")).toEqual({
      globe: true,
      terrain: DEFAULT_TERRAIN_EXAGGERATION,
      shadow: false,
    });
    expect(parseSceneFromSearch("?terrain=2.6").terrain).toBe(2.6);
    expect(parseSceneFromSearch("?terrain=50").terrain).toBe(10);
    expect(parseSceneFromSearch("?terrain=0").terrain).toBeNull();
  });

  it("writes only what differs from the default and round-trips", () => {
    expect(searchWithScene("?model=gfs", { globe: false, terrain: null, shadow: false })).toBe("?model=gfs");
    expect(searchWithScene("?model=gfs&projection=globe&terrain=on", { globe: false, terrain: null, shadow: false })).toBe("?model=gfs");
    const on = searchWithScene("?model=gfs", { globe: true, terrain: DEFAULT_TERRAIN_EXAGGERATION, shadow: false });
    expect(on).toBe("?model=gfs&projection=globe&terrain=on");
    expect(parseSceneFromSearch(on)).toEqual({ globe: true, terrain: DEFAULT_TERRAIN_EXAGGERATION, shadow: false });
    expect(searchWithScene("", { globe: false, terrain: 2.6, shadow: false })).toBe("?terrain=2.6");
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

describe("moveCenterOnRay", () => {
  /** A camera over Fuji's flank: z13, pitch 60, looking north, 1,000 px to the center. */
  function readout(over: Partial<Parameters<typeof moveCenterOnRay>[0]> = {}) {
    return {
      center: { lng: 138.74, lat: 35.34 },
      elevation: 2000,
      zoom: 13,
      pitch: 60,
      bearing: 0,
      fovInRadians: 0.6435,
      height: 800,
      cameraToCenterDistance: 800 / 2 / Math.tan(0.6435 / 2),
      worldSize: 512 * 2 ** 13,
      tileSize: 512,
      minZoom: 0,
      maxZoom: 16,
      tileZoom: 13,
      centerPoint: null,
      ...over,
    };
  }
  /** The camera's altitude and ground position, from the transform's own quantities. */
  function camera(t: ReturnType<typeof readout>) {
    const metersPerPixel = (40075016.7 * Math.cos((t.center.lat * Math.PI) / 180)) / t.worldSize;
    const distance = t.cameraToCenterDistance * metersPerPixel;
    const pitch = (t.pitch * Math.PI) / 180;
    const back = distance * Math.sin(pitch);
    return {
      altitude: t.elevation + distance * Math.cos(pitch),
      lat: t.center.lat - (back * Math.cos((t.bearing * Math.PI) / 180)) / 111320,
    };
  }
  function slide(t: ReturnType<typeof readout>, elevation: number) {
    moveCenterOnRay(
      t,
      elevation,
      (e) => (t.elevation = e),
      (c) => (t.center = c),
      (z) => {
        t.zoom = z;
        t.worldSize = 512 * 2 ** z;
      },
    );
    return t;
  }

  it("lands the center on the plane with the camera where it was", () => {
    const before = readout();
    const was = camera(before);
    const after = slide(readout(), 1400);
    expect(after.elevation).toBeCloseTo(1400, 6);
    expect(after.center.lat).toBeGreaterThan(before.center.lat);
    expect(after.zoom).toBeLessThan(before.zoom);
    const is = camera(after);
    expect(is.altitude).toBeCloseTo(was.altitude, 0);
    expect(is.lat).toBeCloseTo(was.lat, 4);
  });

  it("leaves the transform alone when the plane is nearer than the zoom ceiling allows", () => {
    // The camera is at about 6,700 m; ground 200 m under it wants zoom 17.6.
    const t = readout();
    const moved = moveCenterOnRay(t, 6500, () => { throw new Error("elevation"); }, () => { throw new Error("center"); }, () => { throw new Error("zoom"); });
    expect(moved).toBe(false);
    expect(t.zoom).toBe(13);
  });

  it("keeps the camera still when it looks almost level", () => {
    const before = readout({ pitch: 85 });
    const after = slide(readout({ pitch: 85 }), 1900);
    expect(camera(after).altitude).toBeCloseTo(camera(before).altitude, 0);
    expect(after.zoom).toBeLessThanOrEqual(16);
  });
});

describe("keepTerrainCameraStill", () => {
  /** A map whose transform is one instance of a one-off prototype. */
  function fakeMap() {
    const calls: string[] = [];
    const prototype = {
      center: { lng: 138.74, lat: 35.34 },
      elevation: 100,
      zoom: 13,
      pitch: 60,
      bearing: 0,
      fovInRadians: 0.6435,
      height: 800,
      cameraToCenterDistance: 800 / 2 / Math.tan(0.6435 / 2),
      worldSize: 512 * 2 ** 13,
      tileSize: 512,
      minZoom: 0,
      maxZoom: 16,
      tileZoom: 13,
      centerPoint: null,
      setElevation(elevation: number) {
        calls.push(`elevation ${elevation.toFixed(0)}`);
        this.elevation = elevation;
      },
      setCenter(center: { lng: number; lat: number }) {
        calls.push("center");
        this.center = center;
      },
      setZoom(zoom: number) {
        calls.push("zoom");
        this.zoom = zoom;
      },
      screenPointToLocation(_point: unknown, _terrain?: unknown) {
        return this.center;
      },
      recalculateZoomAndCenter(_terrain?: unknown) {
        calls.push("maplibre landing");
      },
    };
    const transform = Object.create(prototype) as typeof prototype;
    const camera = { transform, elevationFreeze: false, easing: false, isEasing: () => camera.easing };
    const map = {
      _camera: camera,
      terrain: { getElevationForLngLatZoom: () => 130 } as unknown,
      on() {},
      setTerrain(options: unknown) {
        transform.setElevation(options ? 300 : 0);
        return map;
      },
    };
    keepTerrainCameraStill(map as unknown as MaplibreMap);
    return { map, camera, transform, calls };
  }

  it("slides the center along the ray when the ground re-samples at rest", () => {
    const { transform, calls } = fakeMap();
    transform.setElevation(130);
    expect(calls).toEqual(["elevation 130", "center", "zoom"]);
  });

  it("leaves a settled center alone, and one the ray cannot reach", () => {
    const { transform, calls } = fakeMap();
    transform.setElevation(100.5);
    expect(calls).toEqual([]);
    transform.setElevation(130);
    transform.setElevation(130);
    expect(calls).toEqual(["elevation 130", "center", "zoom"]);
  });

  it("moves the camera during a gesture, an ease, a toggle, and without terrain", () => {
    const { map, camera, transform, calls } = fakeMap();
    camera.elevationFreeze = true;
    transform.setElevation(130);
    camera.elevationFreeze = false;
    camera.easing = true;
    transform.setElevation(160);
    camera.easing = false;
    map.setTerrain({});
    map.terrain = null;
    transform.setElevation(0);
    expect(calls).toEqual(["elevation 130", "elevation 160", "elevation 300", "elevation 0"]);
  });

  it("lands a gesture on the sampled ground along the ray", () => {
    const { map, transform, calls } = fakeMap();
    transform.recalculateZoomAndCenter(map.terrain);
    expect(calls).toEqual(["elevation 130", "center", "zoom"]);
    transform.setElevation(130);
    expect(calls.length).toBe(3);
    map.terrain = null;
    transform.recalculateZoomAndCenter(undefined);
    expect(calls.at(-1)).toBe("maplibre landing");
  });
});
