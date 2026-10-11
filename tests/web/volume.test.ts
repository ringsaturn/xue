import { describe, expect, it } from "vitest";
import type { BundleVariable } from "../../web/src/manifest";
import {
  cameraFromMatrix,
  DEFAULT_VERTICAL_EXAGGERATION,
  defaultVerticalExaggeration,
  GLOBE_RADIUS,
  globeSectionStrip,
  globeShellMesh,
  levelLookup,
  maxPool2,
  regionGrid,
  spherePoint,
  tileRegion,
  VOLUME_BUNDLE_LEVELS,
  volumeBox,
  volumeLevels,
  volumeTop,
  volumeTransfer,
} from "../../web/src/volume";
import { boxBounds, dragSelects } from "../../web/src/volumetool";

const LEVELS_M = [
  500, 750, 1000, 1250, 1500, 1750, 2000, 2250, 2500, 2750, 3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500, 7000,
  7500, 8000, 8500, 9000, 10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000,
];

function level(metres: number, index: number, overrides: Partial<BundleVariable["parameter"]> = {}): BundleVariable {
  return {
    numericId: index + 1,
    id: `refl${metres}` as BundleVariable["id"],
    label: `Radar reflectivity at ${metres / 1000} km MSL`,
    unit: "dBZ",
    parameter: {
      discipline: 209,
      parameterCategory: 9,
      parameterNumber: 0,
      typeOfFirstFixedSurface: 102,
      scaleFactorOfFirstFixedSurface: 0,
      scaledValueOfFirstFixedSurface: metres,
      ...overrides,
    },
    quantization: { type: "linear", offset: 0, scale: 0.5, minimumCode: 0, maximumCode: 160, nodataCode: 255 },
  } as BundleVariable;
}

const volume = LEVELS_M.map((metres, index) => level(metres, index));

describe("volume levels", () => {
  it("reads the refl3d bundle's 33 altitudes in bundle order", () => {
    const levels = volumeLevels(volume);
    expect(levels?.altitudes).toEqual(LEVELS_M);
    expect(levels?.variables.map((variable) => variable.id)).toEqual(volume.map((variable) => variable.id));
    expect(VOLUME_BUNDLE_LEVELS.get("refl3d")).toBe(LEVELS_M.length);
  });

  it("refuses anything that is not one quantity stacked on altitudes", () => {
    expect(volumeLevels([volume[0]!])).toBeNull();
    expect(volumeLevels([volume[0]!, level(750, 1, { parameterNumber: 1 })])).toBeNull();
    expect(volumeLevels([volume[0]!, level(750, 1, { typeOfFirstFixedSurface: 100 })])).toBeNull();
    expect(volumeLevels([volume[1]!, volume[0]!])).toBeNull();
    const logCoded = { ...volume[1]!, quantization: { ...volume[1]!.quantization, type: "log1p" } } as BundleVariable;
    expect(volumeLevels([volume[0]!, logCoded])).toBeNull();
    const unparametered = { ...volume[1]!, parameter: undefined };
    expect(volumeLevels([volume[0]!, unparametered])).toBeNull();
  });

  it("tops the box half a level step above the highest level", () => {
    expect(volumeTop(LEVELS_M)).toBe(19500);
  });
});

describe("level lookup", () => {
  const top = volumeTop(LEVELS_M);
  const size = 19500; // one bin per metre, so a level sits on a bin
  const table = levelLookup(LEVELS_M, top, size);
  const at = (metres: number): number => table[Math.floor((metres / top) * size)]!;

  it("lands on each level's texel centre at its altitude", () => {
    for (const [index, metres] of LEVELS_M.entries()) {
      expect(at(metres)).toBeCloseTo((index + 0.5) / LEVELS_M.length, 3);
    }
  });

  it("is linear between unevenly spaced levels and clamped outside them", () => {
    // Halfway between 9 km (index 22) and 10 km (index 23).
    expect(at(9500)).toBeCloseTo((22.5 + 0.5) / LEVELS_M.length, 3);
    expect(at(100)).toBeCloseTo(0.5 / LEVELS_M.length, 6);
    expect(at(19400)).toBeCloseTo((LEVELS_M.length - 0.5) / LEVELS_M.length, 6);
  });
});

describe("camera from the projection matrix", () => {
  // A column-major perspective * look-at, the shape MapLibre hands a
  // custom layer, built for a known eye.
  function lookAt(eye: number[], target: number[], up: number[]): number[] {
    const sub = (a: number[], b: number[]) => a.map((value, i) => value - b[i]!);
    const norm = (a: number[]) => a.map((value) => value / Math.hypot(...a));
    const cross = (a: number[], b: number[]) => [
      a[1]! * b[2]! - a[2]! * b[1]!,
      a[2]! * b[0]! - a[0]! * b[2]!,
      a[0]! * b[1]! - a[1]! * b[0]!,
    ];
    const dot = (a: number[], b: number[]) => a.reduce((sum, value, i) => sum + value * b[i]!, 0);
    const z = norm(sub(eye, target));
    const x = norm(cross(up, z));
    const y = cross(z, x);
    return [x[0]!, y[0]!, z[0]!, 0, x[1]!, y[1]!, z[1]!, 0, x[2]!, y[2]!, z[2]!, 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1];
  }
  function perspective(fov: number, aspect: number, near: number, far: number): number[] {
    const f = 1 / Math.tan(fov / 2);
    return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, (2 * far * near) / (near - far), 0];
  }
  function multiply(a: number[], b: number[]): number[] {
    const out = new Array<number>(16).fill(0);
    for (let column = 0; column < 4; column += 1) {
      for (let row = 0; row < 4; row += 1) {
        for (let k = 0; k < 4; k += 1) out[column * 4 + row]! += a[k * 4 + row]! * b[column * 4 + k]!;
      }
    }
    return out;
  }

  it("recovers a pitched camera's eye in Mercator units", () => {
    const eye = [0.25, 0.4, 0.0005];
    const matrix = multiply(perspective(0.6435, 1.5, 1e-6, 1), lookAt(eye, [0.251, 0.395, 0], [0, 0, 1]));
    const camera = cameraFromMatrix(matrix);
    expect(camera).not.toBeNull();
    camera!.forEach((value, index) => expect(value).toBeCloseTo(eye[index]!, 9));
  });

  it("gives up on a degenerate matrix", () => {
    expect(cameraFromMatrix(new Array<number>(16).fill(0))).toBeNull();
  });
});

describe("volume box", () => {
  const grid = {
    width: 1400,
    height: 700,
    firstLongitude: -129.975,
    firstLatitude: 54.975,
    longitudeStep: 0.05,
    latitudeStep: -0.05,
  };

  it("spans the grid's cell edges, north at the smaller Mercator y", () => {
    const box = volumeBox(grid, 19500, 25);
    expect(box.west).toBeCloseTo(-130, 9);
    expect(box.east).toBeCloseTo(-60, 9);
    expect(box.north).toBeCloseTo(55, 9);
    expect(box.south).toBeCloseTo(20, 9);
    expect(box.min[0]).toBeCloseTo((-130 + 180) / 360, 9);
    expect(box.min[1]).toBeLessThan(box.max[1]);
    expect(box.max[2]).toBeCloseTo(19500 * box.zScale, 12);
  });

  it("scales its height with the exaggeration", () => {
    expect(volumeBox(grid, 19500, 50).max[2]).toBeCloseTo(2 * volumeBox(grid, 19500, 25).max[2], 12);
  });
});

describe("max pool", () => {
  it("keeps each block's strongest code, odd edges included", () => {
    const plane = Uint8Array.from([1, 9, 2, 3, 4, 5, 7, 0, 6, 8, 1, 1, 2, 3, 4]);
    // 5 x 3 → 3 x 2
    expect(Array.from(maxPool2(plane, 5, 3))).toEqual([9, 6, 8, 1, 3, 4]);
  });
});

describe("close-up region", () => {
  const tile = { tileWidth: 50, tileHeight: 50 };

  it("is the union of the decoded tiles in cells, clipped to the grid", () => {
    expect(tileRegion([{ firstColumn: 3, firstRow: 2, lastColumn: 5, lastRow: 4 }], tile, 1400, 700)).toEqual({
      x: 150,
      y: 100,
      width: 150,
      height: 150,
    });
    // The last tile row of a 700-row grid at 50 is whole; a 690-row grid clips it.
    expect(tileRegion([{ firstColumn: 27, firstRow: 13, lastColumn: 27, lastRow: 13 }], tile, 1400, 690)).toEqual({
      x: 1350,
      y: 650,
      width: 50,
      height: 40,
    });
    expect(tileRegion(null, tile, 1400, 700)).toBeNull();
  });

  it("moves the grid's origin to the region's first cell", () => {
    const grid = { width: 1400, height: 700, firstLongitude: -129.975, firstLatitude: 54.975, longitudeStep: 0.05, latitudeStep: -0.05 };
    const sub = regionGrid(grid, { x: 150, y: 100, width: 150, height: 150 });
    expect(sub.firstLongitude).toBeCloseTo(-122.475, 9);
    expect(sub.firstLatitude).toBeCloseTo(49.975, 9);
    expect([sub.width, sub.height]).toEqual([150, 150]);
    const box = volumeBox(sub, 19500, 10);
    expect(box.west).toBeCloseTo(-122.5, 9);
    expect(box.north).toBeCloseTo(50, 9);
  });
});

describe("volume drag tools", () => {
  it("orders a box's corners whichever way it was dragged", () => {
    expect(boxBounds([-80, 30], [-84, 33])).toEqual({ west: -84, east: -80, south: 30, north: 33 });
  });

  it("takes a tap for nothing and a drag for a selection", () => {
    expect(dragSelects("box", 0, 0, 40, 5)).toBe(false);
    expect(dragSelects("box", 0, 0, 40, 30)).toBe(true);
    expect(dragSelects("section", 0, 0, 9, 9)).toBe(true);
    expect(dragSelects("section", 0, 0, 5, 5)).toBe(false);
  });
});

describe("globe shell", () => {
  it("puts a point where MapLibre's projectToSphere does, lifted by its height", () => {
    // lon 0 lat 0 → +z; lon 90 → +x; the north pole → +y.
    expect(spherePoint(0, 0).map((v) => +v.toFixed(12))).toEqual([0, 0, 1]);
    expect(spherePoint(90, 0).map((v) => +v.toFixed(12))).toEqual([1, 0, 0]);
    expect(spherePoint(0, 90).map((v) => +v.toFixed(12))).toEqual([0, 1, 0]);
    const lifted = spherePoint(-83, 32, GLOBE_RADIUS);
    expect(Math.hypot(...lifted)).toBeCloseTo(2, 12);
  });

  it("is closed around the box with outward normals", () => {
    const box = { west: -130, east: -60, south: 20, north: 55 };
    const lift = 19500 * 10;
    const mesh = globeShellMesh(box, lift, 8, 4);
    const count = mesh.positions.length / 3;
    expect(mesh.normals.length).toBe(mesh.positions.length);
    expect(Math.max(...mesh.indices)).toBeLessThan(count);
    // Two triangles a cell: top and floor 8 x 4, east/west walls 4 x 1,
    // north/south walls 8 x 1.
    expect(mesh.indices.length).toBe(3 * 2 * (2 * 8 * 4 + 2 * 4 + 2 * 8));
    // Each triangle's normal leads out of the box: a step along it from the
    // centroid leaves, a step against it stays in. The step (0.01 of the
    // radius, 0.57°) is larger than a chord's sag, smaller than the box.
    const inside = ([x, y, z]: number[]) => {
      const r = Math.hypot(x!, y!, z!);
      const lat = (Math.asin(y! / r) * 180) / Math.PI;
      const lon = (Math.atan2(x!, z!) * 180) / Math.PI;
      return r > 1 && r < 1 + lift / GLOBE_RADIUS && lat > box.south && lat < box.north && lon > box.west && lon < box.east;
    };
    for (let t = 0; t < mesh.indices.length; t += 3) {
      const corners = [0, 1, 2].map((k) => mesh.indices[t + k]!);
      const centroid = [0, 1, 2].map((axis) => corners.reduce((sum, v) => sum + mesh.positions[v * 3 + axis]!, 0) / 3);
      const n = [0, 1, 2].map((axis) => mesh.normals[corners[0]! * 3 + axis]!);
      expect(inside(centroid.map((value, axis) => value + 0.01 * n[axis]!))).toBe(false);
      expect(inside(centroid.map((value, axis) => value - 0.01 * n[axis]!))).toBe(true);
    }
  });

  it("stands a section on the great circle between its ends", () => {
    const strip = globeSectionStrip([-84, 31], [-81, 33], 195000, 4);
    expect(strip.length).toBe((4 + 1) * 2 * 3);
    // Ground vertices on the sphere, top ones lifted.
    expect(Math.hypot(strip[0]!, strip[1]!, strip[2]!)).toBeCloseTo(1, 6);
    expect(Math.hypot(strip[3]!, strip[4]!, strip[5]!)).toBeCloseTo(1 + 195000 / GLOBE_RADIUS, 6);
    const start = spherePoint(-84, 31);
    const end = spherePoint(-81, 33);
    [0, 1, 2].forEach((axis) => expect(strip[axis]!).toBeCloseTo(start[axis]!, 6));
    [0, 1, 2].forEach((axis) => expect(strip[24 + axis]!).toBeCloseTo(end[axis]!, 6));
  });
});

/** The WRF nest's cloud water volume (`cloud3d`): cloud water mixing ratio
 * (0/1/22) on 24 altitudes above mean sea level, 250 m apart to 3 km,
 * 500 m to 6 km and 1 km to 12 km, on the linear g/kg codebook. */
const CLW_LEVELS_M = [
  250, 500, 750, 1000, 1250, 1500, 1750, 2000, 2250, 2500, 2750, 3000, 3500, 4000, 4500, 5000, 5500, 6000, 7000, 8000,
  9000, 10000, 11000, 12000,
];

function cloudLevel(metres: number, index: number, compact = false): BundleVariable {
  return {
    numericId: index + 1,
    id: `clw${metres}` as BundleVariable["id"],
    label: `Cloud water at ${metres / 1000} km MSL`,
    unit: "g/kg",
    parameter: {
      discipline: 0,
      parameterCategory: 1,
      parameterNumber: 22,
      typeOfFirstFixedSurface: 102,
      scaleFactorOfFirstFixedSurface: 0,
      scaledValueOfFirstFixedSurface: metres,
    },
    quantization: compact
      ? { type: "linear", offset: 0, scale: 0.02, minimumCode: 0, maximumCode: 126, nodataCode: 255 }
      : { type: "linear", offset: 0, scale: 0.01, minimumCode: 0, maximumCode: 253, nodataCode: 255 },
  } as BundleVariable;
}

describe("the cloud water volume", () => {
  const cloud = CLW_LEVELS_M.map((metres, index) => cloudLevel(metres, index));

  it("reads cloud3d's 24 altitudes in bundle order, on either codebook", () => {
    const levels = volumeLevels(cloud);
    expect(levels?.altitudes).toEqual(CLW_LEVELS_M);
    expect(levels?.variables.map((variable) => variable.id)).toEqual(cloud.map((variable) => variable.id));
    expect(VOLUME_BUNDLE_LEVELS.get("cloud3d")).toBe(CLW_LEVELS_M.length);
    expect(volumeLevels(CLW_LEVELS_M.map((metres, index) => cloudLevel(metres, index, true)))?.altitudes).toEqual(CLW_LEVELS_M);
    expect(volumeTop(CLW_LEVELS_M)).toBe(12500);
  });

  it("chooses the transfer function by the levels' parameter", () => {
    expect(volumeTransfer(cloud[0]!.parameter)).toBe("cloud");
    expect(volumeTransfer(volume[0]!.parameter)).toBe("reflectivity");
    // Cloud ice (0/1/23) is not cloud water; nor is a missing block.
    expect(volumeTransfer({ ...cloud[0]!.parameter!, parameterNumber: 23 })).toBe("reflectivity");
    expect(volumeTransfer(undefined)).toBe("reflectivity");
  });
});

describe("default vertical exaggeration", () => {
  it("is true scale under two degrees of longitude and the wide-area value over it", () => {
    // The Fuji nest: 79 cells of 0.005°.
    expect(defaultVerticalExaggeration({ width: 79, longitudeStep: 0.005 })).toBe(1);
    expect(defaultVerticalExaggeration({ width: 399, longitudeStep: 0.005 })).toBe(1);
    // CONUS at 0.05°, and a box exactly two degrees wide.
    expect(defaultVerticalExaggeration({ width: 1400, longitudeStep: 0.05 })).toBe(DEFAULT_VERTICAL_EXAGGERATION);
    expect(defaultVerticalExaggeration({ width: 400, longitudeStep: 0.005 })).toBe(DEFAULT_VERTICAL_EXAGGERATION);
    expect(DEFAULT_VERTICAL_EXAGGERATION).toBe(10);
  });
});
