import { describe, expect, it } from "vitest";
import { gapSeconds, linePath, valueRange } from "../../web/src/stations/sparkline";

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
});
