import { describe, expect, it } from "vitest";

import {
  METEOGRAM_BUNDLE_IDS,
  alignSeries,
  columnX,
  fitRange,
  frameIndexAtX,
  meteogramRowCode,
  meteogramRows,
  seriesState,
} from "../../web/src/meteogram";

function published(...ids: string[]): (id: string) => boolean {
  const set = new Set(ids);
  return (id) => set.has(id);
}

describe("meteogramRows", () => {
  it("keeps only the rows a run can fill, restricted to what it publishes", () => {
    const rows = meteogramRows(published("tmp2m", "prate", "wind10m", "hgt500"));
    expect(rows.map((row) => row.id)).toEqual(["temperature", "precipitation", "wind"]);
    expect(rows[0]!.bundles).toEqual(["tmp2m"]);
    expect(rows[2]!.bundles).toEqual(["wind10m"]);
  });

  it("draws the full GFS surface set in display order", () => {
    const rows = meteogramRows(
      published("tmp2m", "dpt2m", "prate", "wind10m", "gust", "tcdc", "lcdc", "mcdc", "hcdc", "prmsl"),
    );
    expect(rows.map((row) => row.id)).toEqual(["temperature", "precipitation", "wind", "cloud", "pressure"]);
    expect(rows[0]!.bundles).toEqual(["tmp2m", "dpt2m"]);
    expect(rows[2]!.bundles).toEqual(["wind10m", "gust"]);
    // The layers stand in for the total, high over middle over low.
    expect(rows[3]!.bundles).toEqual(["hcdc", "mcdc", "lcdc"]);
    expect(rows[3]!.kind).toBe("bands");
    expect(rows[3]!.range).toEqual([0, 100]);
  });

  it("falls back to the total cloud cover when no layer is published", () => {
    const rows = meteogramRows(published("tcdc"));
    expect(rows).toHaveLength(1);
    expect(rows[0]!.bundles).toEqual(["tcdc"]);
  });

  it("names nothing on a run without a surface bundle", () => {
    expect(meteogramRows(published("hgt500", "wind850"))).toEqual([]);
  });

  it("lists every bundle a row could read", () => {
    for (const id of ["tmp2m", "dpt2m", "prate", "wind10m", "gust", "hcdc", "mcdc", "lcdc", "tcdc", "prmsl"]) {
      expect(METEOGRAM_BUNDLE_IDS).toContain(id);
    }
  });
});

describe("meteogramRowCode", () => {
  it("joins the codes and names the shared surface once", () => {
    const [temperature, precipitation, wind, cloud, pressure] = meteogramRows(
      published("tmp2m", "dpt2m", "prate", "wind10m", "gust", "hcdc", "mcdc", "lcdc", "prmsl"),
    );
    expect(meteogramRowCode(temperature!)).toBe("TMP · DPT 2M");
    expect(meteogramRowCode(precipitation!)).toBe("PRATE");
    expect(meteogramRowCode(wind!)).toBe("WIND · GUST 10M");
    expect(meteogramRowCode(cloud!)).toBe("HIGH · MID · LOW");
    expect(meteogramRowCode(pressure!)).toBe("PRMSL");
  });

  it("drops the second code with the bundle", () => {
    const [wind] = meteogramRows(published("gust"));
    expect(meteogramRowCode(wind!)).toBe("GUST 10M");
  });
});

describe("alignSeries", () => {
  // A primary axis of hourly frames, and a bundle whose own axis starts at
  // three hours and steps by three — ECMWF prate under a GFS-style primary.
  const leads = [0, 3600, 7200, 10800, 14400, 18000, 21600];
  const offsetFor = (lead: number): number | null => (lead >= 10800 && lead % 10800 === 0 ? lead / 3600 : null);

  it("leaves a gap wherever the bundle's axis has no frame", () => {
    const values = alignSeries(leads, offsetFor, (offset) => offset * 10);
    expect(values).toEqual([undefined, undefined, undefined, 30, undefined, undefined, 60]);
  });

  it("passes the bundle's own nodata and undecoded frames through", () => {
    const values = alignSeries(leads, offsetFor, (offset) => (offset === 3 ? null : undefined));
    expect(values[3]).toBeNull();
    expect(values[6]).toBeUndefined();
  });
});

describe("seriesState", () => {
  const leads = [0, 3600, 7200];
  const every = (lead: number): number => lead / 3600;

  it("is complete once every frame the bundle has is in hand", () => {
    expect(seriesState(leads, every, [1, 2, 3])).toBe("complete");
    expect(seriesState(leads, every, [1, null, 3])).toBe("complete");
  });

  it("is partial while a frame the bundle has is still missing", () => {
    expect(seriesState(leads, every, [1, undefined, 3])).toBe("partial");
  });

  it("is empty with nothing in hand, and complete when a gap is the axis's own", () => {
    expect(seriesState(leads, every, [undefined, undefined, undefined])).toBe("empty");
    // The bundle has no frame at the second lead, so its absence is not a wait.
    const sparse = (lead: number): number | null => (lead === 3600 ? null : lead / 3600);
    expect(seriesState(leads, sparse, [1, undefined, 3])).toBe("complete");
    expect(seriesState(leads, () => null, [])).toBe("empty");
  });
});

describe("fitRange", () => {
  it("returns a fixed range untouched", () => {
    expect(fitRange([[50, 90]], [0, 100], null)).toEqual([0, 100]);
  });

  it("pads the series' extent on both sides", () => {
    const [low, high] = fitRange([[10, 20]], null, null);
    expect(low).toBeLessThan(10);
    expect(high).toBeGreaterThan(20);
  });

  it("holds the baseline without padding below it", () => {
    const [low, high] = fitRange([[0.2, 3]], null, 0);
    expect(low).toBe(0);
    expect(high).toBeGreaterThan(3);
  });

  it("centres a series that never moves, and spans nothing sensibly", () => {
    expect(fitRange([[5, 5, 5]], null, null)).toEqual([4.5, 5.5]);
    expect(fitRange([[undefined, null]], null, null)).toEqual([0, 1]);
    expect(fitRange([[undefined]], null, 0)).toEqual([0, 1]);
  });

  it("ignores gaps and nodata", () => {
    const [low, high] = fitRange([[undefined, 1, null, 3]], null, null);
    expect(low).toBeCloseTo(1 - 0.16);
    expect(high).toBeCloseTo(3 + 0.16);
  });
});

describe("columns", () => {
  it("place the first frame at the left edge and the last at the right", () => {
    expect(columnX(0, 161, 320)).toBe(0);
    expect(columnX(160, 161, 320)).toBe(320);
    expect(columnX(0, 1, 320)).toBe(160);
  });

  it("scrub to the nearest frame, clamped to the axis", () => {
    expect(frameIndexAtX(0, 161, 320)).toBe(0);
    expect(frameIndexAtX(320, 161, 320)).toBe(160);
    expect(frameIndexAtX(-40, 161, 320)).toBe(0);
    expect(frameIndexAtX(900, 161, 320)).toBe(160);
    expect(frameIndexAtX(160, 161, 320)).toBe(80);
    expect(frameIndexAtX(50, 1, 320)).toBe(0);
    expect(frameIndexAtX(50, 5, 0)).toBe(0);
  });
});
