import { describe, expect, it } from "vitest";

import {
  COLUMN_PX,
  HOUR_MS,
  LOOKBACK_HOURS,
  buildAxis,
  dayBands,
  hourColumns,
  layoutFrames,
  layoutReports,
  xOf,
} from "../../web/src/compare/axis";

const T0 = Date.UTC(2026, 8, 19, 6); // 2026-09-19T06:00Z, a GFS cycle
const hours = (count: number) => count * HOUR_MS;

describe("buildAxis", () => {
  it("begins at the earliest run's cycle when that is within the lookback", () => {
    const now = T0 + hours(8);
    const axis = buildAxis([T0, T0 + hours(6)], now, 120);
    expect(axis.startMs).toBe(T0);
    expect(axis.endMs).toBe(T0 + hours(120));
    expect(axis.columnHours).toBe(1);
    expect(axis.widthPx).toBe(120 * COLUMN_PX);
  });

  it("reaches back no further than the lookback behind the present", () => {
    const now = T0 + hours(30);
    const axis = buildAxis([T0], now, 120);
    expect(axis.startMs).toBe(now - hours(LOOKBACK_HOURS));
  });

  it("floors the origin to the three-hour UTC grid on the long horizon", () => {
    // 07Z is not on the grid; 06Z is.
    const axis = buildAxis([T0 + hours(1)], T0 + hours(2), 360);
    expect(axis.columnHours).toBe(3);
    expect(axis.startMs).toBe(T0);
    expect(axis.widthPx).toBe(120 * COLUMN_PX);
  });

  it("starts at the present hour with no run loaded", () => {
    const now = T0 + hours(2) + 15 * 60 * 1000;
    const axis = buildAxis([], now, 120);
    expect(axis.startMs).toBe(T0 + hours(2));
  });
});

describe("layoutFrames", () => {
  const axis = buildAxis([T0], T0 + hours(1), 120);

  it("places an hourly series one column per frame", () => {
    const times = Array.from({ length: 5 }, (_, i) => T0 + hours(i));
    const cells = layoutFrames(axis, times);
    expect(cells.map((cell) => [cell.index, cell.x, cell.width])).toEqual([
      [0, 0, COLUMN_PX],
      [1, COLUMN_PX, COLUMN_PX],
      [2, 2 * COLUMN_PX, COLUMN_PX],
      [3, 3 * COLUMN_PX, COLUMN_PX],
      [4, 4 * COLUMN_PX, COLUMN_PX],
    ]);
  });

  it("lets a coarser step span its frames and follows a change of step", () => {
    // Hourly to 3 h, then 3-hourly: the last cell spans the step before it.
    const times = [0, 1, 2, 3, 6, 9].map((h) => T0 + hours(h));
    const cells = layoutFrames(axis, times);
    expect(cells.map((cell) => cell.width / COLUMN_PX)).toEqual([1, 1, 1, 3, 3, 3]);
    expect(cells.at(-1)!.endMs).toBe(T0 + hours(12));
  });

  it("thins an hourly series to the three-hour columns of the long axis", () => {
    const long = buildAxis([T0], T0 + hours(1), 360);
    const times = Array.from({ length: 10 }, (_, i) => T0 + hours(i));
    const cells = layoutFrames(long, times);
    expect(cells.map((cell) => cell.index)).toEqual([0, 3, 6, 9]);
    expect(cells.every((cell) => cell.width === COLUMN_PX)).toBe(true);
  });

  it("keeps every frame of a run whose cycle is off the column grid", () => {
    // A 3-hour axis from 06Z; a run at 07Z stepping hourly is thinned to
    // every third frame from its own start, never dropped whole.
    const long = buildAxis([T0], T0 + hours(1), 360);
    const times = Array.from({ length: 7 }, (_, i) => T0 + hours(1 + i));
    const cells = layoutFrames(long, times);
    expect(cells.map((cell) => cell.index)).toEqual([0, 2, 5]);
  });

  it("clips to the axis and drops frames outside it", () => {
    const times = [-2, -1, 0, 118, 119, 120, 121].map((h) => T0 + hours(h));
    const cells = layoutFrames(axis, times);
    expect(cells.map((cell) => cell.index)).toEqual([2, 3, 4]);
    expect(cells.at(-1)!.endMs).toBe(axis.endMs);
  });

  it("gives a lone frame one column", () => {
    const cells = layoutFrames(axis, [T0 + hours(5)]);
    expect(cells).toHaveLength(1);
    expect(cells[0]!.width).toBe(COLUMN_PX);
  });
});

describe("layoutReports", () => {
  const now = T0 + hours(10) + 20 * 60 * 1000;
  const axis = buildAxis([T0], now, 120);

  it("orders reports, spans each to the next and the newest to the present", () => {
    const times = [T0 + hours(9), T0 + hours(7), T0 + hours(8)];
    const cells = layoutReports(axis, times);
    expect(cells.map((cell) => cell.index)).toEqual([1, 2, 0]);
    expect(cells[0]!.endMs).toBe(T0 + hours(8));
    expect(cells[1]!.endMs).toBe(T0 + hours(9));
    expect(cells[2]!.endMs).toBe(now);
    expect(cells[2]!.x).toBe(xOf(axis, T0 + hours(9)));
  });

  it("thins reports closer than a column from the newest back, keeping the newest", () => {
    // Hourly reports with a special one at 08:30: on the hourly axis the
    // special is dropped; on the three-hour axis only 09:00 and 06:00 stay.
    const times = [6, 7, 8, 8.5, 9].map((h) => T0 + hours(h));
    expect(layoutReports(axis, times).map((cell) => cell.index)).toEqual([0, 1, 2, 4]);
    const long = buildAxis([T0], now, 360);
    expect(layoutReports(long, times).map((cell) => cell.index)).toEqual([0, 4]);
  });

  it("drops reports before the axis or after the present", () => {
    const cells = layoutReports(axis, [T0 - hours(1), T0 + hours(11), T0 + hours(2)]);
    expect(cells.map((cell) => cell.index)).toEqual([2]);
  });
});

describe("clock rows", () => {
  it("reads hours in the display zone and shades the night", () => {
    const axis = buildAxis([T0], T0 + hours(1), 120);
    const columns = hourColumns(axis, "Asia/Tokyo");
    // 06Z is 15:00 JST.
    expect(columns[0]!.hour).toBe(15);
    expect(columns[0]!.night).toBe(false);
    expect(columns[3]!.hour).toBe(18);
    expect(columns[3]!.night).toBe(true);
    expect(columns[15]!.hour).toBe(6);
    expect(columns[15]!.night).toBe(false);
  });

  it("bands the axis by local date with widths summing to the track", () => {
    const axis = buildAxis([T0], T0 + hours(1), 120);
    const bands = dayBands(axis, "Asia/Tokyo", "en");
    // 15:00 JST to midnight is nine columns, then whole days.
    expect(bands[0]!.width).toBe(9 * COLUMN_PX);
    expect(bands[0]!.label).toMatch(/Saturday/);
    expect(bands[0]!.label).toMatch(/19/);
    expect(bands[1]!.width).toBe(24 * COLUMN_PX);
    expect(bands.reduce((sum, band) => sum + band.width, 0)).toBe(axis.widthPx);
  });
});
