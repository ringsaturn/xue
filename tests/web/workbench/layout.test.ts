import { describe, expect, it } from "vitest";

import { LANES, ROWS, workbenchLayout } from "../../../web/src/workbench/layout";

describe("the workbench's layout", () => {
  it("has both charts beside the profile where there is room, else one of them", () => {
    expect(workbenchLayout(1920, 1080, true).arrangement).toBe("both");
    expect(workbenchLayout(1400, 900, true).arrangement).toBe("both");
    expect(workbenchLayout(1399, 900, true).arrangement).toBe("one");
    expect(workbenchLayout(1280, 720, true).arrangement).toBe("one");
    // A point has one chart beside the profile.
    expect(workbenchLayout(1280, 720, false).arrangement).toBe("both");
    expect(workbenchLayout(719, 900, false).arrangement).toBe("phone");
    expect(workbenchLayout(390, 844, true).arrangement).toBe("phone");
  });

  it("gives up the map first, then the lanes, and scrolls in the end", () => {
    const tall = workbenchLayout(1920, 1080, true);
    expect(tall).toMatchObject({ locator: 292, lanes: true });
    expect(tall.plot).toBeGreaterThan(300);
    const laptop = workbenchLayout(1440, 820, true);
    expect(laptop).toMatchObject({ locator: 164, lanes: true });
    const short = workbenchLayout(1280, 620, true);
    expect(short).toMatchObject({ locator: 124, lanes: false });
    const lying = workbenchLayout(844, 390, true);
    expect(lying).toMatchObject({ locator: 110, lanes: false, plot: 220 });
  });

  it("has less for the charts under a bar of two rows", () => {
    expect(workbenchLayout(1000, 1080, true, 110).plot).toBe(workbenchLayout(1000, 1080, true).plot - 46);
  });

  it("fills the window and no more while it can", () => {
    for (const height of [760, 820, 960, 1080, 1440]) {
      const { locator, plot, lanes } = workbenchLayout(1440, height, true);
      const rows = ROWS.header + ROWS.bar + ROWS.head + 2 * ROWS.gutter + ROWS.readout + ROWS.axis + (lanes ? LANES * ROWS.lane : 0);
      expect(rows + locator + plot).toBe(height);
      expect(plot).toBeGreaterThanOrEqual(220);
    }
  });

  it("gives the map more when asked, the lanes first and the charts never below their least", () => {
    const charts = workbenchLayout(1440, 960, true);
    const even = workbenchLayout(1440, 960, true, ROWS.bar, "even");
    const map = workbenchLayout(1440, 960, true, ROWS.bar, "map");
    expect(even.locator).toBe(Math.round(0.4 * (960 - ROWS.header - ROWS.bar)));
    expect(even.locator).toBeGreaterThan(charts.locator);
    expect(map.locator).toBeGreaterThan(even.locator);
    expect(map.lanes).toBe(false);
    expect(map.plot).toBe(220);
    for (const size of ["even", "map"] as const) {
      const { locator, plot, lanes } = workbenchLayout(1440, 960, true, ROWS.bar, size);
      const rows = ROWS.header + ROWS.bar + ROWS.head + 2 * ROWS.gutter + ROWS.readout + ROWS.axis + (lanes ? LANES * ROWS.lane : 0);
      expect(rows + locator + plot).toBe(960);
    }
  });

  it("never gives the map less than the charts would, nor a phone a choice", () => {
    expect(workbenchLayout(844, 390, true, ROWS.bar, "map")).toEqual(workbenchLayout(844, 390, true));
    expect(workbenchLayout(390, 844, true, ROWS.bar, "map")).toEqual(workbenchLayout(390, 844, true));
  });
});
