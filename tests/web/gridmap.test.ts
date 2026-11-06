import { describe, expect, it } from "vitest";

import { HRRR_DOMAIN, lambertCone, lambertForward, lambertInverse, lambertMapFactor } from "../../web/src/domain";
import {
  axesAlign,
  clipDomain,
  cropGrid,
  forEachCell,
  gaussianLatitudes,
  geoGrid,
  gridForm,
  gridTable,
  gridMapFor,
  parseGrid,
  sameGrid,
  type LambertGrid,
  type RegularGrid,
} from "../../web/src/gridmap";
import { parseBundleMetadata, visibleGridShare, type BundleMetadata } from "../../web/src/manifest";
import { DERIVED_MAX_CODE, nearestCell, ramp, thermalFrontZone } from "../../web/src/composite";
import { dayMask } from "../../web/src/hotspot";
import { buildPhaseField } from "../../web/src/precipphase";
import { bilinearCells, probeCell } from "../../web/src/probe";
import { viewportTileRects } from "../../web/src/tiles";
import { cellOf, parseGrid as parseCellGrid } from "../../web/src/apiconsole/locate";
import { parseGroupMetadata } from "../../web/src/zarr/shard";
import { globalGrid } from "./helpers";

const PROJECTION = {
  kind: "lambert-conformal" as const,
  radius: 6371229,
  standardParallel1: 38.5,
  standardParallel2: 38.5,
  latitudeOfOrigin: 38.5,
  centralMeridian: -97.5,
  x0: -2697520.142,
  y0: 1586693.847,
  dx: 3000,
  dy: -3000,
};
const LAMBERT_BLOCK = { width: 1799, height: 1059, wrapLongitude: false, projection: PROJECTION };
const LAMBERT = parseGrid(LAMBERT_BLOCK) as LambertGrid;
const GFS: RegularGrid = { width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25, wraps: true };
const SFLUX_BLOCK = {
  width: 3072,
  height: 1536,
  firstLongitude: -180,
  firstLatitude: 89.91032453466268,
  longitudeStep: 0.1171875,
  // GDAL's even spacing between the first and last rows.
  latitudeStep: -(2 * 89.91032453466268) / 1535,
  wrapLongitude: true,
  gaussian: { n: 768, firstRow: 0, rowStride: 1 },
};
const SFLUX = parseGrid(SFLUX_BLOCK) as RegularGrid;

function legendre(degree: number, x: number): number {
  let previous = 1;
  let value = x;
  for (let k = 2; k <= degree; k += 1) [previous, value] = [value, ((2 * k - 1) * x * value - (k - 1) * previous) / k];
  return value;
}

describe("parseGrid", () => {
  it("reads every published shape of regular grid, defaults included", () => {
    expect(parseGrid({ width: 1440, height: 721 })).toEqual({ ...GFS, wraps: true });
    expect(parseGrid({ width: 720, height: 361, longitudeStep: 0.5, latitudeStep: -0.5 })).toMatchObject({ wraps: true, firstLatitude: 90 });
    expect(parseGrid({ width: 10, height: 10, firstLongitude: 5, firstLatitude: 5, longitudeStep: 1, latitudeStep: -1 })).toMatchObject({ wraps: false });
  });

  it("reads a Lambert grid with no regular field", () => {
    expect(LAMBERT).toEqual({ width: 1799, height: 1059, wraps: false, projection: PROJECTION });
  });

  const malformed: [string, unknown][] = [
    ["no block", undefined],
    ["null", null],
    ["no width", { height: 10 }],
    ["zero width", { width: 0, height: 10 }],
    ["a fractional height", { width: 10, height: 1.5 }],
    ["a step of the wrong type", { width: 10, height: 10, longitudeStep: "1" }],
    ["a null first longitude", { width: 10, height: 10, firstLongitude: null }],
    ["a zero latitude step", { width: 10, height: 10, latitudeStep: 0 }],
    ["a backward column order", { width: 10, height: 10, columnOrder: "east-to-west" }],
    ["a wrap of the wrong type", { width: 10, height: 10, wrapLongitude: 1 }],
    ["an unknown kind", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, kind: "polar-stereographic" } }],
    ["a null projection", { ...LAMBERT_BLOCK, projection: null }],
    ["regular fields beside a projection", { ...LAMBERT_BLOCK, firstLongitude: -120 }],
    ["a null regular field beside a projection", { ...LAMBERT_BLOCK, latitudeStep: null }],
    ["a gaussian block beside a projection", { ...LAMBERT_BLOCK, gaussian: { n: 768, firstRow: 0, rowStride: 1 } }],
    ["a wrapping projected grid", { ...LAMBERT_BLOCK, wrapLongitude: true }],
    ["a projected grid with no wrap field", { width: 1799, height: 1059, projection: PROJECTION }],
    ["a projection field missing", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, dx: undefined } }],
    ["an unknown projection field", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, rotation: 0 } }],
    ["a non-finite projection field", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, x0: Number.NaN } }],
    ["a radius of zero", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, radius: 0 } }],
    ["a parallel at the pole", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, standardParallel1: 90 } }],
    ["an origin at the pole", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, latitudeOfOrigin: -90 } }],
    ["parallels summing to zero", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, standardParallel1: 30, standardParallel2: -30 } }],
    ["a central meridian past 180", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, centralMeridian: 200 } }],
    ["a dx that is not positive", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, dx: -3000 } }],
    ["a dy that is not negative", { ...LAMBERT_BLOCK, projection: { ...PROJECTION, dy: 3000 } }],
    ["a null gaussian block", { width: 10, height: 10, gaussian: null }],
    ["a gaussian n of zero", { width: 10, height: 10, gaussian: { n: 0, firstRow: 0, rowStride: 1 } }],
    ["a gaussian n past the limit", { width: 10, height: 10, gaussian: { n: 32769, firstRow: 0, rowStride: 1 } }],
    ["a negative first row", { width: 10, height: 10, gaussian: { n: 5, firstRow: -1, rowStride: 1 } }],
    ["a stride of zero", { width: 10, height: 10, gaussian: { n: 5, firstRow: 0, rowStride: 0 } }],
    ["rows past the last root", { width: 10, height: 11, gaussian: { n: 5, firstRow: 0, rowStride: 1 } }],
    ["an unknown gaussian field", { width: 10, height: 10, gaussian: { n: 5, firstRow: 0, rowStride: 1, weights: [] } }],
  ];

  it.each(malformed)("refuses %s", (_name, block) => {
    expect(() => parseGrid(block)).toThrow();
    expect(() => geoGrid({ grid: block })).toThrow();
    // The console's reader reports the same refusal as no grid.
    expect(parseCellGrid(block)).toBeNull();
  });

  it("is what every consumer of a metadata grid reads through", () => {
    const bad = globalGrid({ longitudeStep: 0 });
    expect(() => probeCell(bad, 0, 0)).toThrow();
    expect(() => bilinearCells(bad, 0, 0)).toThrow();
    expect(() => viewportTileRects(bad, { tileWidth: 16, tileHeight: 16, columns: 90, rows: 46 }, { west: 0, east: 1, south: 0, north: 1 })).toThrow();
  });
});

describe("gridTable", () => {
  const conus = parseGrid({
    width: 1799,
    height: 1059,
    wrapLongitude: false,
    projection: { kind: "lambert-conformal", radius: 6371229, standardParallel1: 38.5, standardParallel2: 38.5, latitudeOfOrigin: 38.5, centralMeridian: -97.5, x0: -2697520.143, y0: 1586693.847, dx: 3000, dy: -3000 },
  });
  if (!conus.projection) throw new Error("a projected grid");

  it("holds bilinear within a few metres of the forward projection", () => {
    const table = gridTable(conus);
    const map = gridMapFor(conus);
    const [west, north, east, south] = table.box;
    for (let index = 0; index < 4000; index += 1) {
      // A low-discrepancy walk, so the check covers the rectangle without chance.
      const fx = (index * 0.7548776662) % 1;
      const fy = (index * 0.5698402910) % 1;
      const atX = fx * (table.width - 1);
      const atY = fy * (table.height - 1);
      const lowX = Math.min(Math.floor(atX), table.width - 2);
      const lowY = Math.min(Math.floor(atY), table.height - 2);
      const read = (channel: number) => {
        const texel = (x: number, y: number) => table.data[(y * table.width + x) * 2 + channel]!;
        const a = texel(lowX, lowY) * (1 - (atX - lowX)) + texel(lowX + 1, lowY) * (atX - lowX);
        const b = texel(lowX, lowY + 1) * (1 - (atX - lowX)) + texel(lowX + 1, lowY + 1) * (atX - lowX);
        return a * (1 - (atY - lowY)) + b * (atY - lowY);
      };
      const mercatorX = west + fx * (east - west);
      const mercatorY = north + fy * (south - north);
      const longitude = mercatorX * 360 - 180;
      const latitude = 90 - (360 / Math.PI) * Math.atan(Math.exp((mercatorY * 2 - 1) * Math.PI));
      const cell = map.lonLatToCell(longitude, latitude);
      if (!cell) continue;
      expect(Math.abs((read(0) + 2697520.143) / 3000 - cell[0]) * 3000).toBeLessThan(5);
      expect(Math.abs((read(1) - 1586693.847) / -3000 - cell[1]) * 3000).toBeLessThan(5);
    }
  });

  it("is kept for a grid of the same cone and bounds", () => {
    expect(gridTable(conus)).toBe(gridTable(conus));
  });
});

describe("the metadata versions", () => {
  const parameter = { discipline: 0, parameterCategory: 0, parameterNumber: 0, typeOfFirstFixedSurface: 103, scaleFactorOfFirstFixedSurface: 0, scaledValueOfFirstFixedSurface: 2 };
  const v3 = {
    schemaVersion: 3,
    model: "HRRR",
    runTime: "2026-08-15T06:00:00Z",
    time: { unitSeconds: 3600, firstFrameOffset: 0, frameCount: 3, frameStep: 1 },
    grid: { width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25, wrapLongitude: true },
    variables: [
      {
        numericId: 1,
        id: "tmp2m",
        label: "2 metre temperature",
        unit: "°C",
        parameter,
        quantization: { type: "linear", offset: -60, scale: 0.5, minimumCode: 0, maximumCode: 220, nodataCode: 255 },
      },
    ],
  };
  const v4 = { ...v3, schemaVersion: 4, grid: LAMBERT_BLOCK };

  it("takes a projection at version 4 and a gaussian block at version 3", () => {
    expect(geoGrid(parseBundleMetadata(JSON.stringify(v4))).projection).toEqual(PROJECTION);
    const gaussian = { ...v3, grid: { ...v3.grid, height: 720, gaussian: { n: 360, firstRow: 0, rowStride: 1 } } };
    expect(geoGrid(parseBundleMetadata(JSON.stringify(gaussian))).gaussian?.n).toBe(360);
  });

  it("holds the version to the lowest that fits", () => {
    expect(() => parseBundleMetadata(JSON.stringify({ ...v3, grid: LAMBERT_BLOCK }))).toThrow(/grid\.projection|schema version 4/);
    expect(() => parseBundleMetadata(JSON.stringify({ ...v3, schemaVersion: 4 }))).toThrow(/schemaVersion|schema version 3/);
    const gaussian = { ...v3.grid, gaussian: { n: 361, firstRow: 0, rowStride: 1 } };
    expect(() => parseBundleMetadata(JSON.stringify({ ...v3, schemaVersion: 4, grid: gaussian }))).toThrow();
  });

  it("refuses a malformed grid at the document, through the store's group too", () => {
    const bad = { ...v4, grid: { ...LAMBERT_BLOCK, projection: { ...PROJECTION, kind: "rotated-pole" } } };
    expect(() => parseBundleMetadata(JSON.stringify(bad))).toThrow();
    const group = { zarr_format: 3, node_type: "group", attributes: { xue_profile: 1, xue: bad } };
    expect(() => parseGroupMetadata(JSON.stringify(group))).toThrow();
    const good = { zarr_format: 3, node_type: "group", attributes: { xue_profile: 1, xue: v4 } };
    expect(parseGroupMetadata(JSON.stringify(good)).metadata.schemaVersion).toBe(4);
  });
});

describe("a regular grid", () => {
  const map = gridMapFor(GFS);

  it("looks cells up with the arithmetic it always had", () => {
    expect(map.lonLatToCell(139.75, 35.75)).toEqual([(139.75 + 180) / 0.25, (90 - 35.75) / 0.25]);
    expect(map.cellToLonLat(1279, 217)).toEqual([139.75, 35.75]);
    const metadata = globalGrid();
    expect(probeCell(metadata, 139.7, 35.7)).toMatchObject({ column: 1279, row: 217, longitude: 139.75, latitude: 35.75 });
    expect(probeCell(metadata, 179.9, 0)!.column).toBe(0);
    expect(nearestCell(GFS, 139.7, 35.7)).toBe(217 * 1440 + 1279);
  });

  it("clips a cropped grid at the half-cell the shader's nearest texel does", () => {
    const crop: RegularGrid = { width: 40, height: 30, firstLongitude: 110, firstLatitude: 35, longitudeStep: 0.5, latitudeStep: -0.5, wraps: false };
    const cropMap = gridMapFor(crop);
    expect(cropMap.lonLatToCell(129.74, 25)).not.toBeNull();
    expect(cropMap.lonLatToCell(129.75, 25)).toBeNull();
    expect(cropMap.lonLatToCell(110, 35.24)).not.toBeNull();
    expect(cropMap.lonLatToCell(110, 35.26)).toBeNull();
    expect(cropMap.bounds()).toEqual({ west: 109.75, east: 129.75, south: 20.25, north: 35.25 });
  });

  it("names its step and its form in its own degrees", () => {
    expect(map.nominalStepDegrees()).toBe(0.25);
    expect(gridForm(GFS)).toEqual({ first: [-180, 90], step: [0.25, -0.25], cone: [0, 0, 0, 0] });
  });

  it("is never clipped differently for carrying a domain or not", () => {
    expect(clipDomain(GFS, HRRR_DOMAIN)).toBe(HRRR_DOMAIN);
    expect(clipDomain(GFS, undefined)).toBeNull();
    expect(clipDomain(LAMBERT, HRRR_DOMAIN)).toBeNull();
  });
});

describe("gaussianLatitudes", () => {
  it("are the roots of the Legendre polynomial, north to south", () => {
    for (const n of [1, 2, 47, 95]) {
      const table = gaussianLatitudes(n);
      expect(table).toHaveLength(2 * n);
      for (let row = 0; row < 2 * n; row += 1) {
        expect(Math.abs(legendre(2 * n, Math.sin((table[row]! * Math.PI) / 180)))).toBeLessThan(1e-9);
        expect(table[row]! + table[2 * n - 1 - row]!).toBeCloseTo(0, 10);
        if (row > 0) expect(table[row]!).toBeLessThan(table[row - 1]!);
      }
    }
  });

  it("match the closed forms", () => {
    expect(gaussianLatitudes(1)[0]).toBeCloseTo((Math.asin(1 / Math.sqrt(3)) * 180) / Math.PI, 11);
  });

  it("match the writers' goldens to the tolerance across libms", () => {
    const expected: [number, number[]][] = [
      [95, [89.27671287810583, 88.33975425118209, 87.3972623773714]],
      [768, [89.91032453466268, 89.79415738840316, 89.67730421235662]],
    ];
    for (const [n, rows] of expected) {
      const table = gaussianLatitudes(n);
      rows.forEach((latitude, row) => expect(Math.abs(table[row]! - latitude)).toBeLessThan(1e-11));
    }
  });
});

describe("a Gaussian grid", () => {
  const map = gridMapFor(SFLUX);
  const table = gaussianLatitudes(768);

  it("reports true latitudes and finds them again", () => {
    for (const row of [0, 1, 100, 767, 768, 1535]) {
      expect(map.cellToLonLat(0, row)[1]).toBe(table[row]);
      expect(map.lonLatToCell(10, table[row]!)![1]).toBeCloseTo(row, 9);
    }
    const cell = probeCell({ grid: SFLUX_BLOCK } as unknown as BundleMetadata, 10, table[100]!)!;
    expect(cell.row).toBe(100);
    expect(cell.latitude).toBe(table[100]);
  });

  it("takes the nearest row by true latitude, where equal spacing would not", () => {
    // Between two rows of the table, a hair south of their midpoint.
    const row = 700;
    const midpoint = (table[row]! + table[row + 1]!) / 2;
    expect(Math.round(map.lonLatToCell(0, midpoint + 1e-6)![1])).toBe(row);
    expect(Math.round(map.lonLatToCell(0, midpoint - 1e-6)![1])).toBe(row + 1);
  });

  it("gives the polar caps to the outermost rows on a whole grid", () => {
    expect(map.lonLatToCell(0, 90)![1]).toBe(0);
    expect(map.lonLatToCell(0, -90)![1]).toBe(1535);
  });

  it("reads a thinned or cropped tier's own rows", () => {
    const half = parseGrid({ ...SFLUX_BLOCK, width: 1536, height: 768, longitudeStep: 0.234375, latitudeStep: -0.234375, gaussian: { n: 768, firstRow: 0, rowStride: 2 } });
    expect(gridMapFor(half).cellToLonLat(0, 3)[1]).toBe(table[6]);
    const crop = cropGrid(SFLUX, 100, 200, 10, 10);
    expect(crop.gaussian).toEqual({ n: 768, firstRow: 200, rowStride: 1 });
    expect(gridMapFor(crop).cellToLonLat(0, 2)[1]).toBe(table[202]);
    // A crop has rows beyond its edge, so the point past half their gap is off it.
    expect(gridMapFor(crop).lonLatToCell(-168, table[199]!)).toBeNull();
  });

  it("keeps the regular extents and the regular shader form", () => {
    const regular: RegularGrid = { ...SFLUX };
    delete regular.gaussian;
    const box = { west: 10, east: 20, south: -5, north: 5 };
    expect(map.coverOfBounds(box)).toEqual(gridMapFor(regular).coverOfBounds(box));
    expect(map.bounds()).toEqual(gridMapFor(regular).bounds());
    expect(gridForm(SFLUX).first).toEqual([-180, 89.91032453466268]);
    expect(gridForm(SFLUX).cone[0]).toBe(0);
  });

  it("differs from its regular fields by no more than the format says", () => {
    for (let row = 0; row < 1536; row += 50) {
      const even = SFLUX.firstLatitude + row * SFLUX.latitudeStep;
      expect(Math.abs(even - table[row]!) / 0.1171875).toBeLessThan(0.02);
    }
  });
});

describe("a Lambert grid", () => {
  const map = gridMapFor(LAMBERT);
  const cone = lambertCone(PROJECTION);

  it("agrees with the footprint test's forward formulas", () => {
    const [x, y] = lambertForward(PROJECTION, cone, -100, 40);
    const [column, row] = map.lonLatToCell(-100, 40)!;
    expect(PROJECTION.x0 + column * PROJECTION.dx).toBeCloseTo(x, 6);
    expect(PROJECTION.y0 + row * PROJECTION.dy).toBeCloseTo(y, 6);
  });

  it("round trips every part of the grid", () => {
    for (const [column, row] of [[0, 0], [1798, 0], [0, 1058], [1798, 1058], [899, 529], [17.25, 901.5]] as const) {
      const [longitude, latitude] = map.cellToLonLat(column, row);
      const back = map.lonLatToCell(longitude, latitude)!;
      expect(back[0]).toBeCloseTo(column, 7);
      expect(back[1]).toBeCloseTo(row, 7);
    }
  });

  it("round trips the projection on a southern cone", () => {
    const south = { ...PROJECTION, standardParallel1: -30, standardParallel2: -60, latitudeOfOrigin: -45, centralMeridian: 135 };
    const southern = lambertCone(south);
    expect(southern.n).toBeLessThan(0);
    const [x, y] = lambertForward(south, southern, 150, -40);
    const [longitude, latitude] = lambertInverse(south, southern, x, y);
    expect(longitude).toBeCloseTo(150, 9);
    expect(latitude).toBeCloseTo(-40, 9);
  });

  it("lays its first cell where NCEP does", () => {
    const [longitude, latitude] = map.cellToLonLat(0, 1058);
    expect(longitude).toBeCloseTo(-122.719528, 2);
    expect(latitude).toBeCloseTo(21.138123, 2);
  });

  it("is off the grid past half a cell, and nowhere NaN", () => {
    expect(map.lonLatToCell(139.77, 35.68)).toBeNull();
    expect(map.lonLatToCell(-97.5, 89.99)).toBeNull();
    expect(map.lonLatToCell(-97.5, -90)).toBeNull();
    expect(map.lonLatToCell(Number.NaN, 40)).toBeNull();
    const [edgeLon, edgeLat] = map.cellToLonLat(-0.4, 500);
    expect(map.lonLatToCell(edgeLon, edgeLat)).not.toBeNull();
    const [pastLon, pastLat] = map.cellToLonLat(-0.6, 500);
    expect(map.lonLatToCell(pastLon, pastLat)).toBeNull();
  });

  it("names the probe's cell and normalises its longitude", () => {
    const metadata = { grid: LAMBERT_BLOCK } as unknown as BundleMetadata;
    const cell = probeCell(metadata, -97.5, 38.5)!;
    expect(cell.index).toBe(cell.row * 1799 + cell.column);
    expect(Object.is(cell.column, -0)).toBe(false);
    expect(probeCell(metadata, 139.77, 35.68)).toBeNull();
    const around = bilinearCells(metadata, -97.5, 38.5)!;
    expect(around.weights.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 12);
    for (const corner of around.cells) expect(probeCell(metadata, corner.longitude, corner.latitude)).not.toBeNull();
    const at = map.lonLatToCell(-97.5, 38.5)!;
    expect(cell.column).toBe(Math.round(at[0]));
    expect(cellOf(LAMBERT, 38.5, -97.5)).toMatchObject({ column: cell.column, row: cell.row });
  });

  it("is a box around its corners, the widest latitude on the north edge's ends", () => {
    const box = map.bounds();
    expect(box.west).toBeLessThan(-130);
    expect(box.east).toBeGreaterThan(-65);
    expect(box.south).toBeCloseTo(21.13, 0);
    expect(box.north).toBeGreaterThan(52);
    expect(map.nominalStepDegrees()).toBeCloseTo(3000 / ((6371229 * Math.PI) / 180), 10);
  });

  it("is not clipped to any domain and maps cells of two tiers one axis at a time", () => {
    const half = parseGrid({ width: 900, height: 530, wrapLongitude: false, projection: { ...PROJECTION, dx: 6000, dy: -6000 } });
    expect(sameGrid(LAMBERT, half)).toBe(false);
    expect(axesAlign(LAMBERT, half)).toBe(true);
    expect(axesAlign(LAMBERT, GFS)).toBe(false);
    expect(axesAlign(SFLUX, GFS)).toBe(false);
    expect(axesAlign(SFLUX, SFLUX)).toBe(true);
    expect(sameGrid(LAMBERT, parseGrid(LAMBERT_BLOCK))).toBe(true);
  });

  it("crops by first cell and steps", () => {
    const crop = cropGrid(LAMBERT, 100, 200, 8, 6) as LambertGrid;
    expect(crop.projection.x0).toBe(PROJECTION.x0 + 100 * 3000);
    expect(crop.projection.y0).toBe(PROJECTION.y0 - 200 * 3000);
    const [longitude, latitude] = map.cellToLonLat(103, 202);
    const back = gridMapFor(crop).lonLatToCell(longitude, latitude)!;
    expect(back[0]).toBeCloseTo(3, 7);
    expect(back[1]).toBeCloseTo(2, 7);
  });

  it("gives the cone's uniforms for the shader", () => {
    const form = gridForm(LAMBERT);
    expect(form.first).toEqual([PROJECTION.x0, PROJECTION.y0]);
    expect(form.step).toEqual([3000, -3000]);
    expect(form.cone).toEqual([cone.n, cone.scale, cone.originRadius, (-97.5 * Math.PI) / 180]);
    expect(form.cone[0]).not.toBe(0);
  });
});

describe("Lambert coverOfBounds", () => {
  const map = gridMapFor(LAMBERT);
  const WHOLE = { firstColumn: 0, lastColumn: 1798, firstRow: 0, lastRow: 1058 };

  /** Every sampled cell whose centre lies in the box must be inside the cover. */
  function expectNoMiss(box: { west: number; east: number; south: number; north: number }): void {
    const cover = map.coverOfBounds(box);
    for (let row = 0; row < 1059; row += 7) {
      for (let column = 0; column < 1799; column += 7) {
        const [longitude, latitude] = map.cellToLonLat(column, row);
        const shifted = longitude + 360 * Math.round((box.west + box.east) / 720 - longitude / 360);
        if (shifted < box.west || shifted > box.east || latitude < box.south || latitude > box.north) continue;
        expect(cover).not.toBeNull();
        expect(column).toBeGreaterThanOrEqual(cover!.firstColumn);
        expect(column).toBeLessThanOrEqual(cover!.lastColumn);
        expect(row).toBeGreaterThanOrEqual(cover!.firstRow);
        expect(row).toBeLessThanOrEqual(cover!.lastRow);
      }
    }
  }

  it("misses no cell of a view in the middle, a tile's worth at most round it", () => {
    const box = { west: -100, east: -95, south: 36, north: 40 };
    expectNoMiss(box);
    const cover = map.coverOfBounds(box)!;
    expect(cover.lastColumn - cover.firstColumn).toBeLessThan(200);
  });

  it.each([
    ["the north-west corner", { west: -140, east: -120, south: 40, north: 55 }],
    ["the north-east corner", { west: -75, east: -55, south: 40, north: 55 }],
    ["the south-west corner", { west: -130, east: -110, south: 15, north: 28 }],
    ["the south-east corner", { west: -85, east: -65, south: 15, north: 28 }],
    ["the northern edge's arc", { west: -110, east: -85, south: 45, north: 60 }],
    ["the southern edge's arc", { west: -110, east: -85, south: 10, north: 24 }],
    ["a thin sliver along a meridian", { west: -97.6, east: -97.4, south: 20, north: 55 }],
    ["a thin sliver along a parallel", { west: -135, east: -60, south: 38.4, north: 38.6 }],
  ])("misses no cell at %s", (_name, box) => expectNoMiss(box));

  it("covers the whole grid from a view holding it, in any copy of the world", () => {
    expect(map.coverOfBounds({ west: -180, east: 180, south: -85, north: 85 })).toEqual(WHOLE);
    expect(map.coverOfBounds({ west: -540, east: -180, south: -85, north: 85 })).toEqual(WHOLE);
    expect(map.coverOfBounds({ west: -400, east: 400, south: -85, north: 85 })).toEqual(WHOLE);
  });

  it("handles bounds that hold a pole, as a globe view's do", () => {
    const northern = { west: -180, east: 180, south: 30, north: 90 };
    expectNoMiss(northern);
    expect(map.coverOfBounds(northern)!.firstRow).toBe(0);
    const southern = { west: -180, east: 180, south: -90, north: 30 };
    expectNoMiss(southern);
    expect(map.coverOfBounds(southern)!.lastRow).toBe(1058);
    for (const value of Object.values(map.coverOfBounds(southern)!)) expect(Number.isFinite(value)).toBe(true);
  });

  it("wraps a view spelled in another copy of the world, and across the projection's cut", () => {
    const spelled = map.coverOfBounds({ west: -100 + 360, east: -95 + 360, south: 36, north: 40 });
    expect(spelled).toEqual(map.coverOfBounds({ west: -100, east: -95, south: 36, north: 40 }));
    // Round the back of the globe from the grid: both sides of the cut at 82.5 E.
    expect(map.coverOfBounds({ west: 60, east: 110, south: 20, north: 40 })).toBeNull();
  });

  it("is null for a view far away or an unusable box", () => {
    expect(map.coverOfBounds({ west: 130, east: 150, south: 30, north: 45 })).toBeNull();
    expect(map.coverOfBounds({ west: 0, east: Number.NaN, south: 0, north: 1 })).toBeNull();
    expect(map.coverOfBounds({ west: 10, east: 0, south: 0, north: 1 })).toBeNull();
  });

  it("turns into tiles that reach the footprint's edges and shares that follow", () => {
    const metadata = { grid: LAMBERT_BLOCK } as unknown as BundleMetadata;
    const geometry = { tileWidth: 64, tileHeight: 64, columns: 29, rows: 17 };
    expect(viewportTileRects(metadata, geometry, { west: -180, east: 180, south: -85, north: 85 })).toBeNull();
    const rects = viewportTileRects(metadata, geometry, { west: -99, east: -96, south: 37, north: 39 })!;
    expect(rects).toHaveLength(1);
    const at = map.lonLatToCell(-97.5, 38)!;
    expect(rects[0]!.firstColumn).toBeLessThanOrEqual(Math.floor(at[0] / 64));
    expect(rects[0]!.lastColumn).toBeGreaterThanOrEqual(Math.floor(at[0] / 64));
    expect(rects[0]!.firstRow).toBeLessThanOrEqual(Math.floor(at[1] / 64));
    expect(rects[0]!.lastRow).toBeGreaterThanOrEqual(Math.floor(at[1] / 64));
    const corner = viewportTileRects(metadata, geometry, { west: -125, east: -122, south: 21, north: 23 })!;
    expect(corner[0]!.lastRow).toBe(16);
    expect(corner[0]!.firstColumn).toBe(0);
    expect(viewportTileRects(metadata, geometry, { west: 130, east: 150, south: 30, north: 45 })).toBeNull();
    expect(visibleGridShare(LAMBERT, { west: -180, east: 180, south: -85, north: 85 })).toBe(1);
    expect(visibleGridShare(LAMBERT, { west: 130, east: 150, south: 30, north: 45 })).toBe(0);
    const share = visibleGridShare(LAMBERT, { west: -99, east: -96, south: 37, north: 39 });
    expect(share).toBeGreaterThan(0);
    expect(share).toBeLessThan(0.05);
  });
});

describe("what a row and a column are not separable on", () => {
  /** A small Lambert grid the whole of whose cells are walked. */
  const small = parseGrid({ width: 30, height: 20, wrapLongitude: false, projection: { ...PROJECTION, x0: -870000, y0: 570000, dx: 60000, dy: -60000 } }) as LambertGrid;
  const smallMap = gridMapFor(small);

  it("walks each cell at its own coordinates", () => {
    const seen = new Map<number, [number, number]>();
    forEachCell(small, (cell, longitude, latitude) => seen.set(cell, [longitude, latitude]));
    expect(seen.size).toBe(600);
    expect(seen.get(5 * 30 + 7)).toEqual(smallMap.cellToLonLat(7, 5));
    // A row of a cone is no parallel.
    expect(seen.get(0)![1]).not.toBeCloseTo(seen.get(15)![1], 3);
  });

  it("finds a day and night on a projected grid cell by cell", () => {
    // Noon at 97.5 W at the equinox: the sun is high over the grid's middle.
    const noon = Date.UTC(2026, 2, 20, 18, 30);
    const mask = dayMask(small, noon, 0);
    expect(mask.every((value) => value === 1)).toBe(true);
    const midnight = Date.UTC(2026, 2, 20, 6, 30);
    expect(dayMask(small, midnight, 0).every((value) => value === 0)).toBe(true);
    // The same sun on a regular grid over the same place agrees at a shared point.
    const regular: RegularGrid = { width: 2, height: 1, firstLongitude: -97.5, firstLatitude: 38.5, longitudeStep: 1, latitudeStep: -1, wraps: false };
    expect(dayMask(regular, noon, 0)[0]).toBe(1);
  });

  it("measures a gradient over the ground, not over the projected metres", () => {
    expect(lambertMapFactor(PROJECTION, lambertCone(PROJECTION), 38.5)).toBeCloseTo(1, 10);
    expect(lambertMapFactor(PROJECTION, lambertCone(PROJECTION), 21)).toBeGreaterThan(1.02);
    const variable = {
      numericId: 1,
      id: "theta",
      label: "θe",
      unit: "K",
      quantization: { type: "linear" as const, offset: 0, scale: 1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
    };
    // One code per column: 1 K per 60 km of projected metres, well south of the
    // standard parallel, where a projected metre is less than a metre of ground.
    const south = parseGrid({ width: 30, height: 20, wrapLongitude: false, projection: { ...small.projection, y0: -1000000 } }) as LambertGrid;
    const plane = new Uint8Array(600);
    for (let row = 0; row < 20; row += 1) for (let column = 0; column < 30; column += 1) plane[row * 30 + column] = column + 100;
    const out = thermalFrontZone({ grid: south, variable, plane }, [0, 3.34], 0);
    const factor = lambertMapFactor(PROJECTION, lambertCone(PROJECTION), gridMapFor(south).cellToLonLat(15, 10)[1]);
    expect(factor).toBeGreaterThan(1.01);
    expect(out[10 * 30 + 15]).toBe(Math.round(ramp((100 / 60) * factor, 0, 3.34) * DERIVED_MAX_CODE));
    expect(out[10 * 30 + 15]).not.toBe(Math.round(ramp(100 / 60, 0, 3.34) * DERIVED_MAX_CODE));
  });

  it("reads a nearest cell by the projection and by the true latitude", () => {
    const [longitude, latitude] = smallMap.cellToLonLat(7, 5);
    expect(nearestCell(small, longitude, latitude)).toBe(5 * 30 + 7);
    expect(nearestCell(small, 139, 35)).toBe(-1);
    const table = gaussianLatitudes(768);
    expect(nearestCell(SFLUX, 0, table[100]!)).toBe(100 * 3072 + 1536);
  });

  const phaseVariable = (id: string) => ({
    numericId: 1,
    id,
    label: id,
    unit: "%",
    quantization: { type: "linear" as const, offset: 0, scale: 1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
  });

  it("reads a second field across tiers of a projection, and across kinds of grid", () => {
    const half = parseGrid({ width: 16, height: 11, wrapLongitude: false, projection: { ...small.projection, dx: 120000, dy: -120000 } });
    const type = { grid: small, variable: phaseVariable("ptype"), plane: new Uint8Array(600), geometry: null, tiles: null };
    const halfFrozen = { grid: half, variable: phaseVariable("cpofp"), plane: new Uint8Array(176).fill(50), geometry: null, tiles: null };
    const tiers = buildPhaseField(type, halfFrozen).weights;
    const box = smallMap.bounds();
    const regular: RegularGrid = {
      width: 60,
      height: 40,
      firstLongitude: box.west,
      firstLatitude: box.north,
      longitudeStep: (box.east - box.west) / 59,
      latitudeStep: -(box.north - box.south) / 39,
      wraps: false,
    };
    const regularFrozen = { grid: regular, variable: phaseVariable("cpofp"), plane: new Uint8Array(2400).fill(50), geometry: null, tiles: null };
    const kinds = buildPhaseField(type, regularFrozen).weights;
    // A flat percent frozen over the whole grid classifies every cell alike,
    // whichever way the second grid was reached.
    expect(Array.from(kinds)).toEqual(Array.from(tiers));
  });
});
