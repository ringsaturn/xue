import { describe, expect, it } from "vitest";

import {
  buildTimeline,
  commonestCycle,
  formatCycleHour,
  formatHours,
  formatMinutes,
  formatRun,
  formatUtcMinute,
  groupDeadlines,
  metricsWindow,
  orderSources,
  orderedOutcomes,
  parseLatencyMetrics,
  runTime,
  type DeadlineDay,
} from "../../web/src/metrics/latency";

const deadline = (days: DeadlineDay[]) => ({
  id: "ercot-day-ahead",
  label: "ERCOT day-ahead market close, 10:00 US Central",
  timeZone: "America/Chicago",
  localTime: "10:00",
  days,
});

const sample = () => ({
  schemaVersion: 1,
  generatedAt: "2026-10-12T16:30:04Z",
  windowDays: 3,
  repository: "ringsaturn/xue",
  sources: {
    gfs: {
      workflow: "publish-gfs.yml",
      cadenceHours: 6,
      landed: 2,
      missing: ["2026101012"],
      outcomes: { failed: 1, landed: 2 },
      latencyMinutes: { median: 335.2, p90: 346.0, max: 346.7 },
      latest: { run: "2026101206", landedAt: "2026-10-12T11:40:09Z", latencyMinutes: 340.1 },
      deadlines: [
        deadline([
          { date: "2026-10-11", cutoff: "2026-10-11T15:00:00Z", run: "2026101106", ageHours: 9.0 },
          { date: "2026-10-12", cutoff: "2026-10-12T15:00:00Z", run: "2026101206", ageHours: 9.0 },
        ]),
      ],
      arrivals: [
        { run: "2026101006", landedAt: "2026-10-10T11:46:00Z", latencyMinutes: 346.0 },
        { run: "2026101206", landedAt: "2026-10-12T11:40:09Z", latencyMinutes: 340.1 },
      ],
      backlog: 0,
      workflowRuns: [],
    },
    hrrr: {
      workflow: "publish-hrrr.yml",
      cadenceHours: 1,
      landed: 0,
      missing: [],
      outcomes: {},
      latencyMinutes: null,
      latest: null,
      deadlines: [deadline([{ date: "2026-10-12", cutoff: "2026-10-12T15:00:00Z", run: null }])],
      arrivals: [],
      backlog: 4,
      workflowRuns: [],
    },
  },
});

describe("parseLatencyMetrics", () => {
  it("reads the schema 1 file", () => {
    const metrics = parseLatencyMetrics(sample())!;
    expect(metrics.windowDays).toBe(3);
    expect(Object.keys(metrics.sources)).toEqual(["gfs", "hrrr"]);
    expect(metrics.sources.gfs!.latencyMinutes).toEqual({ median: 335.2, p90: 346.0, max: 346.7 });
    expect(metrics.sources.hrrr!.latest).toBeNull();
    expect(metrics.sources.hrrr!.backlog).toBe(4);
    expect(metrics.sources.hrrr!.deadlines[0]!.days[0]).toEqual({
      date: "2026-10-12",
      cutoff: "2026-10-12T15:00:00Z",
      run: null,
    });
  });

  it("refuses another schema version or a non-object", () => {
    expect(parseLatencyMetrics({ ...sample(), schemaVersion: 2 })).toBeNull();
    expect(parseLatencyMetrics(null)).toBeNull();
    expect(parseLatencyMetrics("{}")).toBeNull();
    expect(parseLatencyMetrics({ ...sample(), generatedAt: "soon" })).toBeNull();
  });

  it("drops malformed arrivals rather than failing", () => {
    const raw = sample();
    (raw.sources.gfs.arrivals as unknown[]).push({ run: null, landedAt: "x", latencyMinutes: 1 }, "junk");
    expect(parseLatencyMetrics(raw)!.sources.gfs!.arrivals).toHaveLength(2);
  });
});

describe("formatting", () => {
  it("writes minutes as h:mm, rounded, without wrapping at a day", () => {
    expect(formatMinutes(335.2)).toBe("5:35");
    expect(formatMinutes(59.6)).toBe("1:00");
    expect(formatMinutes(50)).toBe("0:50");
    expect(formatMinutes(2950)).toBe("49:10");
    expect(formatMinutes(null)).toBe("--");
    expect(formatMinutes(undefined)).toBe("--");
  });

  it("writes cycle codes and instants in UTC", () => {
    expect(formatRun("2026101206")).toBe("2026-10-12 06Z");
    expect(formatRun("latest")).toBe("latest");
    expect(formatCycleHour("2026101218")).toBe("18Z");
    expect(formatUtcMinute("2026-10-12T11:40:09Z")).toBe("2026-10-12 11:40Z");
    expect(formatUtcMinute("never")).toBe("--");
    expect(formatHours(9)).toBe("9.0 h");
    expect(formatHours(undefined)).toBe("--");
  });

  it("parses only well-formed cycle codes", () => {
    expect(runTime("2026101206")).toBe(Date.UTC(2026, 9, 12, 6));
    expect(runTime("2026101224")).toBeNull();
    expect(runTime("20261012")).toBeNull();
  });
});

describe("ordering", () => {
  it("puts known sources first in their order, then the rest alphabetically", () => {
    expect(orderSources(["zeta", "hrrr", "gfs", "alpha"], ["gfs", "ecmwf", "hrrr"])).toEqual([
      "gfs",
      "hrrr",
      "alpha",
      "zeta",
    ]);
  });

  it("lists nonzero outcomes in the fixed order", () => {
    expect(orderedOutcomes({ failed: 1, landed: 11, skipped: 0, retried: 2 })).toEqual([
      ["landed", 11],
      ["failed", 1],
      ["retried", 2],
    ]);
  });
});

describe("buildTimeline", () => {
  const metrics = parseLatencyMetrics(sample())!;
  const window = metricsWindow(metrics);

  it("ends the window at the build time", () => {
    expect(window[1] - window[0]).toBe(3 * 24 * 3_600_000);
    expect(window[1]).toBe(Date.parse("2026-10-12T16:30:04Z"));
  });

  it("places bars at cycle times, scaled to the source's own longest", () => {
    const timeline = buildTimeline(metrics.sources.gfs!, window);
    expect(timeline.scaleMinutes).toBe(346.0);
    expect(timeline.bars).toHaveLength(2);
    expect(timeline.bars[0]!.height).toBe(1);
    expect(timeline.bars[1]!.height).toBeCloseTo(340.1 / 346.0);
    const span = window[1] - window[0];
    expect(timeline.bars[1]!.x).toBeCloseTo((Date.UTC(2026, 9, 12, 6) - window[0]) / span);
    expect(timeline.missing.map((mark) => mark.run)).toEqual(["2026101012"]);
    // 10-10, 10-11 and 10-12 midnights fall inside a window from 10-09 16:30.
    expect(timeline.days).toHaveLength(3);
  });

  it("pins a cycle from before the window to its left edge", () => {
    const timeline = buildTimeline(
      { arrivals: [{ run: "2026100912", landedAt: "2026-10-09T17:35:00Z", latencyMinutes: 335 }], missing: [] },
      window,
    );
    expect(timeline.bars[0]!.x).toBe(0);
  });
});

describe("deadlines", () => {
  it("finds the commonest cycle, ties going to the most recent day", () => {
    const days: DeadlineDay[] = [
      { date: "2026-10-09", cutoff: "", run: "2026100900", ageHours: 15 },
      { date: "2026-10-10", cutoff: "", run: "2026101006", ageHours: 9 },
      { date: "2026-10-11", cutoff: "", run: null },
    ];
    expect(commonestCycle(days)).toEqual({ cycle: "06Z", count: 1, of: 2 });
    expect(commonestCycle([...days, { date: "2026-10-12", cutoff: "", run: "2026101200", ageHours: 15 }])).toEqual({
      cycle: "00Z",
      count: 2,
      of: 3,
    });
    expect(commonestCycle([{ date: "2026-10-11", cutoff: "", run: null }])).toBeNull();
  });

  it("groups each deadline across sources, newest day first", () => {
    const metrics = parseLatencyMetrics(sample())!;
    const groups = groupDeadlines(metrics.sources, ["gfs", "hrrr"]);
    expect(groups).toHaveLength(1);
    expect(groups[0]!.id).toBe("ercot-day-ahead");
    expect(groups[0]!.dates).toEqual(["2026-10-12", "2026-10-11"]);
    expect(groups[0]!.rows.map((row) => row.source)).toEqual(["gfs", "hrrr"]);
    expect(groups[0]!.rows[0]!.commonest).toEqual({ cycle: "06Z", count: 2, of: 2 });
    expect(groups[0]!.rows[1]!.commonest).toBeNull();
  });
});
