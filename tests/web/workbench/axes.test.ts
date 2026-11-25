import { describe, expect, it } from "vitest";

import { midnights, timeAxis } from "../../../web/src/workbench/axes";

describe("the axis of time", () => {
  const leads = [0, 3600, 7200, 18_000, 28_800];

  it("has its frames by the hour, and those past it off the chart", () => {
    const axis = timeAxis(leads, 5);
    expect(axis.x(0, 500)).toBe(0);
    expect(axis.x(2, 500)).toBe(200);
    expect(axis.x(3, 500)).toBe(500);
    expect(axis.x(4, 500)).toBeGreaterThan(500);
    expect(axis.index(140, 500)).toBe(1);
    expect(axis.index(400, 500)).toBe(3);
    expect(axis.index(500, 500)).toBe(3);
  });

  it("has the midnights of a zone, over a change of its offset too", () => {
    const from = Date.UTC(2026, 10, 25, 6);
    const tokyo = midnights(from, from + 3 * 86_400_000, "Asia/Tokyo");
    expect(tokyo.map((at) => new Date(at).toISOString())).toEqual([
      "2026-11-25T15:00:00.000Z",
      "2026-11-26T15:00:00.000Z",
      "2026-11-27T15:00:00.000Z",
    ]);
    // New York's clocks go back on 1 November 2026.
    const autumn = Date.UTC(2026, 9, 31, 12);
    expect(midnights(autumn, autumn + 2.5 * 86_400_000, "America/New_York").map((at) => new Date(at).toISOString())).toEqual([
      "2026-11-01T04:00:00.000Z",
      "2026-11-02T05:00:00.000Z",
    ]);
  });
});
