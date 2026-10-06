import { describe, expect, it } from "vitest";

import {
  latitudeAt,
  marchLit,
  mercatorX,
  metresPerPixel,
  planMosaic,
  refraction,
  refractedElevation,
  solarPosition,
  worldPixels,
  type MosaicGeometry,
} from "../../web/src/shadow";
import { MAX_MARCH_STEPS, marchStepBound, marchUniforms } from "../../web/src/shadowgl";

describe("solarPosition", () => {
  it("matches NREL's solar position reference case", () => {
    // Reda & Andreas (2004), Table A5.1: Golden, Colorado, 2003-10-17
    // 12:30:30 MST. Topocentric zenith 50.11162° (refraction of ~0.02°
    // included), azimuth 194.34024°.
    const sun = solarPosition(Date.UTC(2003, 9, 17, 19, 30, 30), 39.742476, -105.1786);
    expect(Math.abs(sun.elevation - (90 - 50.11162 - 0.02))).toBeLessThan(0.2);
    expect(Math.abs(sun.azimuth - 194.34024)).toBeLessThan(0.5);
  });

  it("culminates at 90° - |lat| at the equinox", () => {
    const day = Date.UTC(2024, 2, 20);
    for (const lat of [0, 35, -50]) {
      let highest = -90;
      for (let minute = 0; minute < 1440; minute++) {
        highest = Math.max(highest, solarPosition(day + minute * 60000, lat, 0).elevation);
      }
      expect(Math.abs(highest - (90 - Math.abs(lat)))).toBeLessThan(0.5);
    }
  });

  it("rises in the east and sets in the west", () => {
    const morning = solarPosition(Date.UTC(2024, 5, 21, 21, 0), 35.68, 139.69); // 06:00 JST
    const evening = solarPosition(Date.UTC(2024, 5, 21, 9, 0), 35.68, 139.69); // 18:00 JST
    expect(morning.azimuth).toBeGreaterThan(45);
    expect(morning.azimuth).toBeLessThan(90);
    expect(evening.azimuth).toBeGreaterThan(270);
    expect(evening.azimuth).toBeLessThan(315);
  });
});

describe("refraction", () => {
  it("lifts the horizon by about half a degree and vanishes overhead", () => {
    expect(refraction(0)).toBeCloseTo(0.48, 1);
    expect(refraction(45)).toBeLessThan(0.02);
    expect(refraction(89)).toBe(0);
    expect(refractedElevation(0)).toBeGreaterThan(0.4);
  });

  it("is continuous across its branches", () => {
    for (const edge of [5, -0.575]) {
      expect(Math.abs(refraction(edge + 1e-6) - refraction(edge - 1e-6))).toBeLessThan(0.002);
    }
  });
});

/** A flat mosaic on the equator at one DEM level, the view its middle. */
function flatMosaic(demZoom: number, size: number, margin: number): { geometry: MosaicGeometry; elevation: Float32Array } {
  const world = worldPixels(demZoom);
  return {
    geometry: {
      demZoom,
      originX: world / 2,
      originY: world / 2 - size / 2,
      width: size,
      height: size,
      inner: { x: margin, y: margin, width: size - 2 * margin, height: size - 2 * margin },
    },
    elevation: new Float32Array(size * size),
  };
}

describe("marchLit", () => {
  it("casts a wall's shadow h / tan(e) long on flat ground", () => {
    const { geometry, elevation } = flatMosaic(12, 400, 0);
    const wall = 300;
    const height = 200;
    // Ten pixels thick: the march's growing steps sample relief, not
    // one-pixel spikes.
    for (let y = 0; y < 400; y++) elevation.fill(height, y * 400 + wall, y * 400 + wall + 10);
    const sunElevation = 10;
    const lit = marchLit(elevation, geometry, 0, { fixedSun: { azimuth: 90, elevation: sunElevation }, curvature: false });
    const mpp = metresPerPixel(12, 0);
    const expected = height / Math.tan((sunElevation * Math.PI) / 180) / mpp;
    const row = 200 * 400;
    // Walk west from the wall to the first lit pixel.
    let length = 0;
    while ((lit[row + wall - 1 - length] ?? 255) < 128) length++;
    expect(Math.abs(length - expected)).toBeLessThanOrEqual(2);
    // The sunlit side of the wall is open.
    expect(lit[row + wall + 15]).toBe(255);
  });

  it("drops a distant obstacle below the line of sight over the curve", () => {
    const demZoom = 8;
    const { geometry, elevation } = flatMosaic(demZoom, 400, 0);
    const distance = 300;
    for (let y = 0; y < 400; y++) elevation.fill(2000, y * 400 + 50 + distance, y * 400 + 60 + distance);
    const d = distance * metresPerPixel(demZoom, 0);
    const flatAngle = (Math.atan(2000 / d) * 180) / Math.PI;
    const sun = { azimuth: 90, elevation: flatAngle - 0.18 };
    const at = 200 * 400 + 50;
    const flat = marchLit(elevation, geometry, 0, { fixedSun: sun, curvature: false });
    const curved = marchLit(elevation, geometry, 0, { fixedSun: sun });
    expect(flat[at]).toBeLessThan(40);
    expect(curved[at]).toBeGreaterThan(215);
  });

  it("is dark at night and full on flat ground under a high sun", () => {
    const { geometry, elevation } = flatMosaic(10, 64, 8);
    const noon = Date.UTC(2024, 2, 20, 12, 7);
    const midnight = Date.UTC(2024, 2, 20, 0, 7);
    expect(marchLit(elevation, geometry, noon).every((value) => value === 255)).toBe(true);
    expect(marchLit(elevation, geometry, midnight).every((value) => value === 0)).toBe(true);
  });

  it("fades monotonically through civil twilight", () => {
    const { geometry, elevation } = flatMosaic(10, 32, 8);
    let previous = -1;
    for (let e = -7; e <= 2; e += 0.25) {
      const lit = marchLit(elevation, geometry, 0, { fixedSun: { azimuth: 270, elevation: e } });
      const value = lit[0] as number;
      expect(value).toBeGreaterThanOrEqual(previous);
      previous = value;
    }
    expect(previous).toBe(255);
    const civil = marchLit(elevation, geometry, 0, { fixedSun: { azimuth: 270, elevation: -3 } })[0] as number;
    expect(civil).toBeGreaterThan(0);
    expect(civil).toBeLessThan(255);
  });

  it("follows each pixel's own sun across a wide view", () => {
    // A global strip at the equinox, 12:00 UTC: day around the prime
    // meridian, night past ±90°.
    const world = worldPixels(2);
    const geometry: MosaicGeometry = {
      demZoom: 2,
      originX: 0,
      originY: world / 2 - 128,
      width: world,
      height: 256,
      inner: { x: 0, y: 0, width: world, height: 256 },
    };
    const lit = marchLit(new Float32Array(world * 256), geometry, Date.UTC(2024, 2, 20, 12, 0));
    const row = 128 * geometry.width;
    const pixel = (lon: number) => lit[row + Math.floor(mercatorX(lon) * geometry.width)] as number;
    expect(pixel(0)).toBe(255);
    expect(pixel(-150)).toBe(0);
    expect(pixel(150)).toBe(0);
  });
});

describe("planMosaic", () => {
  const view = { west: 138.5, south: 35.2, east: 138.9, north: 35.5 };

  it("adds margin toward the sun", () => {
    // Early morning near Mount Fuji: the sun is low in the east, so the
    // casters stand east of the view.
    const plan = planMosaic(view, 11, Date.UTC(2024, 5, 21, 20, 0));
    expect(plan.sun.azimuth).toBeGreaterThan(45);
    expect(plan.sun.azimuth).toBeLessThan(90);
    const east = plan.width - plan.inner.x - plan.inner.width;
    expect(east).toBeGreaterThan(plan.inner.x);
    expect(plan.inner.x).toBeGreaterThanOrEqual(64);
    const evening = planMosaic(view, 11, Date.UTC(2024, 5, 21, 9, 30));
    expect(evening.inner.x).toBeGreaterThan(evening.width - evening.inner.x - evening.inner.width);
  });

  it("keeps the mosaic within maxPixels by lowering the DEM level", () => {
    const plan = planMosaic(view, 14, Date.UTC(2024, 5, 21, 20, 0), { maxPixels: 1024 });
    expect(plan.demZoom).toBeLessThanOrEqual(12);
    expect(plan.width).toBeLessThanOrEqual(1024);
    expect(plan.height).toBeLessThanOrEqual(1024);
    const roomy = planMosaic(view, 14, Date.UTC(2024, 5, 21, 20, 0));
    expect(roomy.demZoom).toBeGreaterThanOrEqual(plan.demZoom);
    expect(roomy.width).toBeLessThanOrEqual(2048);
  });

  it("places tiles so they cover the mosaic and the view sits inside it", () => {
    const plan = planMosaic(view, 10, Date.UTC(2024, 5, 21, 3, 0));
    const world = worldPixels(plan.demZoom);
    expect(plan.innerRect.x0).toBeCloseTo((plan.originX + plan.inner.x) / world, 12);
    expect(plan.rect.x0).toBeLessThanOrEqual(plan.innerRect.x0);
    expect(plan.rect.x1).toBeGreaterThanOrEqual(plan.innerRect.x1);
    expect(plan.innerRect.x0).toBeLessThanOrEqual(mercatorX(view.west));
    expect(plan.innerRect.x1).toBeGreaterThanOrEqual(mercatorX(view.east));
    for (const tile of plan.tiles) {
      expect(tile.offsetX).toBeGreaterThan(-512);
      expect(tile.offsetX).toBeLessThan(plan.width);
    }
  });

  it("wraps tile columns across the antimeridian", () => {
    const plan = planMosaic({ west: 179.5, south: -17, east: 180.5, north: -16 }, 9, Date.UTC(2024, 5, 21, 0, 0));
    const count = 2 ** plan.demZoom;
    const columns = new Set(plan.tiles.map((tile) => tile.x));
    expect([...columns].every((x) => x >= 0 && x < count)).toBe(true);
    expect(columns.has(0)).toBe(true);
    expect(columns.has(count - 1)).toBe(true);
    expect(plan.innerRect.x1).toBeGreaterThan(1);
  });

  it("clamps latitude to Web Mercator's square", () => {
    const plan = planMosaic({ west: -10, south: 80, east: 10, north: 89.9 }, 4, Date.UTC(2024, 5, 21, 12, 0));
    expect(plan.rect.y0).toBeGreaterThanOrEqual(0);
    expect(plan.originY).toBeGreaterThanOrEqual(0);
    expect(plan.tiles.every((tile) => tile.y >= 0)).toBe(true);
  });
});

describe("GPU march inputs", () => {
  const geometry: MosaicGeometry = {
    demZoom: 10,
    originX: 232000,
    originY: 103000,
    width: 1280,
    height: 900,
    inner: { x: 170, y: 64, width: 948, height: 573 },
  };
  const time = Date.UTC(2026, 9, 6, 8, 0);

  it("reproduces each pixel's sun from the packed uniforms", () => {
    const u = marchUniforms(geometry, time);
    expect(Math.abs(u.hourAngle0)).toBeLessThanOrEqual(Math.PI);
    const world = worldPixels(geometry.demZoom);
    for (const [col, row] of [
      [0, 0],
      [947, 0],
      [400, 300],
      [947, 572],
    ] as const) {
      // The shader's arithmetic, in double precision.
      const lat = Math.atan(Math.sinh(u.mercatorArg0 - row * u.mercatorArgStep));
      const h = u.hourAngle0 + col * u.hourAngleStep;
      const sinE =
        Math.sin(lat) * u.sinDeclination + Math.cos(lat) * u.cosDeclination * Math.cos(h);
      const elevation = (Math.asin(sinE) * 180) / Math.PI;
      const latDeg = latitudeAt((geometry.originY + geometry.inner.y + row + 0.5) / world);
      const lonDeg = ((geometry.originX + geometry.inner.x + col + 0.5) / world) * 360 - 180;
      expect((lat * 180) / Math.PI).toBeCloseTo(latDeg, 9);
      expect(elevation).toBeCloseTo(solarPosition(time, latDeg, lonDeg).elevation, 6);
      expect(u.metresPerPixel * Math.cos(lat)).toBeCloseTo(metresPerPixel(geometry.demZoom, latDeg), 6);
    }
  });

  it("bounds the march loop beyond any mosaic's diagonal", () => {
    for (const side of [64, 1280, 2048, 4096]) {
      const bound = marchStepBound(side, side);
      expect(bound).toBeLessThanOrEqual(MAX_MARCH_STEPS);
      // The march has left the mosaic by then.
      let travelled = 0;
      let step = 1;
      for (let i = 0; i < bound - 2; i++) {
        travelled += step;
        step *= 1.06;
      }
      expect(travelled).toBeGreaterThan(Math.hypot(side, side));
    }
    expect(marchStepBound(40000, 40000)).toBeGreaterThan(MAX_MARCH_STEPS);
  });
});
