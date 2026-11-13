import { describe, expect, it } from "vitest";
import { axisTicks, gapSeconds, linePath, nearestIndex, valueRange } from "../../web/src/stations/sparkline";

describe("station sparklines", () => {
  it("calls a pair a gap at three steps, never under an hour", () => {
    expect(gapSeconds([0, 600, 1200, 1800])).toBe(3600);
    expect(gapSeconds([0, 3600, 7200, 10800])).toBe(10800);
    expect(gapSeconds([0])).toBe(3600);
  });

  it("widens a flat range to the minimum span around its middle", () => {
    expect(valueRange([10, 10.5], 2)).toEqual([9.25, 11.25]);
    expect(valueRange([0, 5], 2)).toEqual([0, 5]);
  });

  it("breaks the line at a missing value and at a gap", () => {
    const time = [0, 600, 1200, 1800, 9000, 9600];
    const values = [0, 1, null, 1, 0, 1];
    const path = linePath(time, values, [0, 9600], [0, 1], 3600, 96, 10);
    // Runs: [0, 600], [1800] (after the null), then the gap to 9000 starts a new run.
    expect(path.match(/M/g)).toHaveLength(3);
    expect(path.match(/L/g)).toHaveLength(2);
    expect(path.startsWith("M0.0 8.0L6.0 2.0M18.0 2.0M90.0 8.0L96.0 2.0")).toBe(true);
  });

  it("marks every six hours of the zone's clock, with the date at midnight", () => {
    // 2026-11-13 13:30 UTC → 2026-11-14 13:30 UTC is 22:30 → 22:30 in Tokyo.
    const start = Date.UTC(2026, 10, 13, 13, 30) / 1000;
    const ticks = axisTicks([start, start + 86400], "Asia/Tokyo");
    expect(ticks.map((tick) => tick.hour)).toEqual([0, 6, 12, 18]);
    expect(ticks[0]!.day).toBe("11/14");
    expect(ticks[0]!.seconds).toBe(Date.UTC(2026, 10, 13, 15) / 1000);
    // A half-hour zone lands on its own whole hours.
    const india = axisTicks([start, start + 86400], "Asia/Kolkata");
    expect(india.map((tick) => tick.hour)).toEqual([0, 6, 12, 18]);
    expect(india[0]!.seconds).toBe(Date.UTC(2026, 10, 13, 18, 30) / 1000);
  });

  it("reads the report nearest the pointer's time", () => {
    const time = [0, 600, 1200, 3600];
    expect(nearestIndex(time, -50)).toBe(0);
    expect(nearestIndex(time, 290)).toBe(0);
    expect(nearestIndex(time, 310)).toBe(1);
    expect(nearestIndex(time, 2500)).toBe(3);
    expect(nearestIndex(time, 9999)).toBe(3);
    expect(nearestIndex([42], 0)).toBe(0);
  });
});
