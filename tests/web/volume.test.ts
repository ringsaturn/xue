import { describe, expect, it } from "vitest";
import type { BundleVariable } from "../../web/src/manifest";
import {
  cameraFromMatrix,
  levelLookup,
  maxPool2,
  VOLUME_BUNDLE_LEVELS,
  volumeBox,
  volumeLevels,
  volumeTop,
} from "../../web/src/volume";

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
