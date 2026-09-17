import { describe, expect, it } from "vitest";

import { FORECAST_MODELS, mosaicMembers, type ForecastModelId } from "../../web/src/manifest";
import {
  VALID_TIME_TOLERANCE_MS,
  axisCadenceMs,
  bandContains,
  mosaicBands,
  offsetNotLater,
  primaryMember,
  wrapLongitude,
} from "../../web/src/mosaic";

const HOUR = 3600 * 1000;
const TEN_MINUTES = 600 * 1000;

describe("the mosaic's bands", () => {
  it("cuts the globe at the midpoints between the four sub-satellite longitudes", () => {
    const bands = mosaicBands([
      { id: "meteosat", subLongitude: 0 },
      { id: "himawari", subLongitude: 140.7 },
      { id: "goeswest", subLongitude: 223 },
      { id: "goeseast", subLongitude: 284.8 },
    ]);
    // Meteosat: from the midpoint with GOES-East (322.4) round to the
    // midpoint with Himawari (70.35).
    expect(bands.get("meteosat")).toEqual({ start: 322.4, width: expect.closeTo(107.95, 9) });
    expect(bands.get("himawari")).toEqual({ start: expect.closeTo(70.35, 9), width: expect.closeTo(111.5, 9) });
    expect(bands.get("goeswest")).toEqual({ start: expect.closeTo(181.85, 9), width: expect.closeTo(72.05, 9) });
    expect(bands.get("goeseast")).toEqual({ start: expect.closeTo(253.9, 9), width: expect.closeTo(68.5, 9) });
    // The bands tile the globe exactly once.
    let total = 0;
    for (const band of bands.values()) total += band.width;
    expect(total).toBeCloseTo(360, 9);
  });

  it("widens the neighbours' bands when a member is absent, leaving the disk edge to end them", () => {
    const bands = mosaicBands([
      { id: "himawari", subLongitude: 140.7 },
      { id: "goeswest", subLongitude: 223 },
      { id: "goeseast", subLongitude: 284.8 },
    ]);
    // Without Meteosat, GOES-East and Himawari meet halfway round the far
    // side: (284.8 + 140.7 + 360) / 2 = 392.75 → 32.75°E.
    expect(bands.get("goeseast")).toEqual({ start: expect.closeTo(253.9, 9), width: expect.closeTo(138.85, 9) });
    expect(bands.get("himawari")).toEqual({ start: expect.closeTo(32.75, 9), width: expect.closeTo(149.1, 9) });
    expect(bandContains(bands.get("goeseast")!, 10)).toBe(true);
    expect(bandContains(bands.get("himawari")!, 40)).toBe(true);
    expect(bandContains(bands.get("himawari")!, 20)).toBe(false);
  });

  it("gives a lone member the whole world", () => {
    const bands = mosaicBands([{ id: "himawari", subLongitude: 140.7 }]);
    expect(bands.get("himawari")).toEqual({ start: expect.closeTo(320.7, 9), width: 360 });
    expect(bandContains(bands.get("himawari")!, -179.9)).toBe(true);
    expect(bandContains(bands.get("himawari")!, 500)).toBe(true);
  });

  it("reads a band the same in every copy of the world", () => {
    // Himawari's band spelled in its grid's own copy (past 180) and the
    // shader's (-180…180) agree, and a point on the far side of the
    // antimeridian is still inside.
    const band = mosaicBands([
      { id: "himawari", subLongitude: 140.7 },
      { id: "goeswest", subLongitude: 223 },
    ]).get("himawari")!;
    expect(band.start).toBeCloseTo(1.85, 9);
    expect(band.width).toBeCloseTo(180, 9);
    expect(bandContains(band, 181)).toBe(true);
    expect(bandContains(band, -179)).toBe(true);
    expect(bandContains(band, 190)).toBe(false);
    expect(bandContains(band, -170)).toBe(false);
    expect(wrapLongitude(-170)).toBe(190);
  });

  it("is empty for no members", () => {
    expect(mosaicBands([]).size).toBe(0);
  });
});

describe("following the playhead by valid time", () => {
  const base = Date.UTC(2026, 8, 18, 6);
  const hourly = [0, 1, 2, 3].map((hour) => base + hour * HOUR);
  const hourlyOffsets = [0, 1, 2, 3];

  it("holds an hourly member's frame under a ten-minute playhead for the hour", () => {
    const cadence = axisCadenceMs(hourly, HOUR);
    expect(cadence).toBe(HOUR);
    expect(offsetNotLater(hourly, hourlyOffsets, base + 50 * 60 * 1000, cadence)).toBe(0);
    expect(offsetNotLater(hourly, hourlyOffsets, base + HOUR, cadence)).toBe(1);
    expect(offsetNotLater(hourly, hourlyOffsets, base + HOUR + TEN_MINUTES, cadence)).toBe(1);
  });

  it("shows nothing for a member whose window has not reached the playhead", () => {
    const cadence = axisCadenceMs(hourly, HOUR);
    expect(offsetNotLater(hourly, hourlyOffsets, base - TEN_MINUTES, cadence)).toBeNull();
  });

  it("drops a frame once the playhead is a whole step past it", () => {
    // The last frame is 09:00; a whole hour and the tolerance past it the
    // member shows nothing rather than a stale hour, but at 09:50 — and
    // at 10:00 itself, the moment its next frame is due — it still holds.
    const cadence = axisCadenceMs(hourly, HOUR);
    expect(offsetNotLater(hourly, hourlyOffsets, base + 3 * HOUR + 50 * 60 * 1000, cadence)).toBe(3);
    expect(offsetNotLater(hourly, hourlyOffsets, base + 4 * HOUR, cadence)).toBe(3);
    expect(offsetNotLater(hourly, hourlyOffsets, base + 4 * HOUR + VALID_TIME_TOLERANCE_MS + 1000, cadence)).toBeNull();
  });

  it("tolerates a slot a few seconds off the playhead's", () => {
    const tenMinute = [0, 1, 2].map((step) => base + step * TEN_MINUTES);
    const cadence = axisCadenceMs(tenMinute, TEN_MINUTES);
    // A member twenty seconds ahead of the playhead's slot is the same slot.
    expect(offsetNotLater(tenMinute, [0, 1, 2], base + TEN_MINUTES - 20 * 1000, cadence)).toBe(1);
    // Beyond the tolerance it is the next slot, not yet reached.
    expect(offsetNotLater(tenMinute, [0, 1, 2], base + TEN_MINUTES - VALID_TIME_TOLERANCE_MS - 1000, cadence)).toBe(0);
  });

  it("takes a one-frame axis at the fallback step", () => {
    expect(axisCadenceMs([base], TEN_MINUTES)).toBe(TEN_MINUTES);
    expect(offsetNotLater([base], [7], base + 5 * 60 * 1000, TEN_MINUTES)).toBe(7);
    expect(offsetNotLater([base], [7], base + 2 * TEN_MINUTES, TEN_MINUTES)).toBeNull();
  });
});

describe("the primary member", () => {
  it("is the finest cadence, then the newest window", () => {
    expect(
      primaryMember([
        { id: "meteosat", cadenceSeconds: 3600, runTime: 30 },
        { id: "himawari", cadenceSeconds: 600, runTime: 10 },
        { id: "goeswest", cadenceSeconds: 600, runTime: 20 },
      ]),
    ).toBe("goeswest");
    expect(primaryMember([{ id: "meteosat", cadenceSeconds: 3600, runTime: 30 }])).toBe("meteosat");
    expect(primaryMember([])).toBeNull();
  });
});

describe("the geo entry", () => {
  it("is a view over the imagers, with no feed of its own", () => {
    const geo = FORECAST_MODELS.geo;
    expect(geo.mosaic).toBe(true);
    expect(geo.latestFilename).toBeUndefined();
    expect(geo.observation).toBe(true);
    expect(geo.region).toEqual([-180, -60, 180, 60]);
    expect(geo.members).toEqual(["meteosat", "himawari", "goeswest", "goeseast"]);
  });

  it("opens the members this build knows and skips one it predates", () => {
    // Meteosat is listed first so it takes its band when it is published;
    // a build without its entry simply has three members.
    const ids = mosaicMembers("geo").map((member) => member.id);
    const known: ForecastModelId[] = ["himawari", "goeswest", "goeseast"];
    for (const id of known) expect(ids).toContain(id);
    for (const id of ids) {
      expect(FORECAST_MODELS[id].subLongitude).toBeDefined();
      expect(FORECAST_MODELS[id].cadenceSeconds).toBe(600);
      expect(FORECAST_MODELS[id].latestFilename).toBeDefined();
    }
    expect(mosaicMembers("himawari")).toEqual([]);
  });

  it("puts the four sub-satellite points where the spacecraft are", () => {
    expect(FORECAST_MODELS.himawari.subLongitude).toBe(140.7);
    expect(FORECAST_MODELS.goeswest.subLongitude).toBe(223);
    expect(FORECAST_MODELS.goeseast.subLongitude).toBeCloseTo(284.8, 9);
  });
});
