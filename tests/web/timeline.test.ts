import { describe, expect, it } from "vitest";

import { LONG_AXIS_DAYS, timelinePlan, type TimelineMonthSegment } from "../../web/src/timeline";

const HOUR = 3600;
const DAY = 24 * HOUR;

/** The live medium-range axes, as lead seconds. */
const GFS = [...Array(121).keys()].concat([...Array(40).keys()].map((step) => 123 + step * 3)).map((h) => h * HOUR);
const ECMWF = [...Array(49).keys()]
  .map((step) => step * 3)
  .concat([...Array(16).keys()].map((step) => 150 + step * 6))
  .map((h) => h * HOUR);
/** AIFS: six-hourly to 360 h — the deepest axis that is still read in days. */
const AIFS = [...Array(61).keys()].map((step) => step * 6 * HOUR);
/** The seasonal axis: six-hourly from 6 h to 6552 h, 1092 frames over 273 days. */
const CFS = [...Array(1092).keys()].map((step) => (step + 1) * 6 * HOUR);

/** 2026-09-19 00Z, a CFSv2 cycle. */
const RUN = Date.parse("2026-09-19T00:00:00Z");

const plan = (leadSeconds: readonly number[], runTime = RUN, trackWidth = 680) =>
  timelinePlan({ leadSeconds, runTime, trackWidth });

const months = (segments: readonly unknown[]) => segments as readonly TimelineMonthSegment[];

describe("timelinePlan on an axis of days", () => {
  it("leaves the medium-range track as it has always been: a tick a frame", () => {
    const gfs = plan(GFS);
    expect(gfs.long).toBe(false);
    expect(gfs.ticks.length).toBe(161);
    expect(gfs.ticks.map((tick) => tick.index)).toEqual([...Array(161).keys()]);
    // Full height on every day boundary, whatever the step is there: f24 is
    // hourly, f168 sits in the three-hourly tail.
    expect(gfs.ticks[24]!.major).toBe(true);
    expect(gfs.ticks[25]!.major).toBe(false);
    expect(gfs.ticks[136]!.major).toBe(true); // f168 = 121 hourly frames + 15 × 3 h
    expect(gfs.ticks.filter((tick) => tick.major).length).toBe(11); // f0 … f240
  });

  it("names every other day past six days, and none on a short run", () => {
    const gfs = plan(GFS);
    expect(gfs.segments.every((segment) => segment.kind === "day")).toBe(true);
    expect(gfs.segments.map((segment) => "day" in segment && segment.day)).toEqual([2, 4, 6, 8, 10]);
    // A 48-hour window keeps both of its days.
    const short = plan([...Array(49).keys()].map((h) => h * HOUR));
    expect(short.segments.map((segment) => "day" in segment && segment.day)).toEqual([1, 2]);
  });

  it("drops a mark that would run into the start or the horizon", () => {
    // Day 10 is the horizon itself and is always dropped; on a phone the
    // 48 px the end labels take is a fifth of the track and day 8 goes too.
    const wide = plan(GFS).segments;
    const phone = timelinePlan({ leadSeconds: GFS, runTime: RUN, trackWidth: 260 }).segments;
    expect(wide.filter((segment) => segment.labelled).length).toBe(4);
    expect(phone.filter((segment) => segment.labelled).length).toBe(3);
    // Dropped, not removed: the meteogram draws them all.
    expect(phone.length).toBe(5);
  });

  it("stays on the short regime to the last day before the switch", () => {
    // 45 days exactly is still days; AIFS's 360-hour horizon is 15 of them.
    expect(plan(AIFS).long).toBe(false);
    const exactly = [...Array(LONG_AXIS_DAYS * 4 + 1).keys()].map((step) => step * 6 * HOUR);
    expect(plan(exactly).long).toBe(false);
    expect(plan([...exactly, (LONG_AXIS_DAYS * 24 + 6) * HOUR]).long).toBe(true);
  });

  it("answers an empty axis with an empty track", () => {
    expect(plan([])).toEqual({ long: false, ticks: [], segments: [] });
  });
});

describe("timelinePlan on a seasonal axis", () => {
  it("thins 1092 frames to one tick a day, full height on the first of a month", () => {
    const cfs = plan(CFS);
    expect(cfs.long).toBe(true);
    // 273 days plus the day the run starts on: the first frame (6 h) stands
    // for day 0, then the frame at 24 h, 48 h, ...
    expect(cfs.ticks.length).toBe(274);
    expect(cfs.ticks.map((tick) => tick.index).slice(0, 3)).toEqual([0, 3, 7]);
    // The run is 00Z, so every tick past the first is a midnight and the
    // majors are the first of each month: October through June, nine of them.
    const majors = cfs.ticks.filter((tick) => tick.major);
    expect(majors.length).toBe(9);
    expect(majors.map((tick) => new Date(RUN + CFS[tick.index]! * 1000).toISOString().slice(0, 10))).toEqual([
      "2026-10-01",
      "2026-11-01",
      "2026-12-01",
      "2027-01-01",
      "2027-02-01",
      "2027-03-01",
      "2027-04-01",
      "2027-05-01",
      "2027-06-01",
    ]);
    // A tick per day is a tick the playhead can be compared against: they
    // are in frame order and the last one is inside the axis.
    expect(cfs.ticks.at(-1)!.index).toBeLessThan(CFS.length);
  });

  it("segments the strip by month, centred in the month's own span", () => {
    const segments = months(plan(CFS).segments);
    expect(segments.every((segment) => segment.kind === "month")).toBe(true);
    // September (the 12 days the run starts in) through June: ten months.
    expect(segments.length).toBe(10);
    expect(segments.map((segment) => segment.month)).toEqual([
      "2026-09",
      "2026-10",
      "2026-11",
      "2026-12",
      "2027-01",
      "2027-02",
      "2027-03",
      "2027-04",
      "2027-05",
      "2027-06",
    ]);
    // The segments tile the track end to end, in order.
    expect(segments[0]!.startPercent).toBe(0);
    expect(segments.at(-1)!.endPercent).toBe(100);
    for (const [position, segment] of segments.entries()) {
      expect(segment.endPercent).toBeGreaterThan(segment.startPercent);
      if (position > 0) expect(segment.startPercent).toBe(segments[position - 1]!.endPercent);
      expect(segment.percent).toBeGreaterThanOrEqual(0);
      expect(segment.percent).toBeLessThanOrEqual(100);
    }
    // October is a whole month of a nine-month run: its label sits inside it.
    const october = segments[1]!;
    expect(october.percent).toBeGreaterThan(october.startPercent);
    expect(october.percent).toBeLessThan(october.endPercent);
  });

  it("carries the year only where the strip crosses into one", () => {
    const segments = months(plan(CFS).segments);
    expect(segments.filter((segment) => segment.yearChanged).map((segment) => segment.month)).toEqual(["2027-01"]);
  });

  it("leaves a sliver of a month unnamed rather than overprinting its neighbour", () => {
    const segments = months(plan(CFS).segments);
    // The twelve days of September the run starts in are 4 % of the track
    // and sit under the `+0H` label; June is the two days the run ends in.
    expect(segments[0]!.labelled).toBe(false);
    expect(segments.at(-1)!.labelled).toBe(false);
    expect(segments.filter((segment) => segment.labelled).map((segment) => segment.month)).toEqual([
      "2026-10",
      "2026-11",
      "2026-12",
      "2027-01",
      "2027-02",
      "2027-03",
      "2027-04",
      "2027-05",
    ]);
    // A phone's track thins them the way the day strip thins to every
    // other day, and the plan still keeps every segment for the meteogram.
    const phone = months(timelinePlan({ leadSeconds: CFS, runTime: RUN, trackWidth: 260 }).segments);
    expect(phone.length).toBe(10);
    expect(phone.filter((segment) => segment.labelled).map((segment) => segment.month)).toEqual([
      "2026-11",
      "2027-01",
      "2027-03",
    ]);
    // Whatever the width, no two names are written closer together than one
    // of them is wide.
    for (const width of [680, 520, 420, 320, 260]) {
      const written = timelinePlan({ leadSeconds: CFS, runTime: RUN, trackWidth: width }).segments
        .filter((segment) => segment.labelled)
        .map((segment) => segment.percent);
      const room = (42 / width) * 100;
      for (let position = 1; position < written.length; position += 1) {
        expect(written[position]! - written[position - 1]!).toBeGreaterThanOrEqual(room);
      }
    }
  });

  it("reads the months in the zone it is given, not in UTC", () => {
    // A 12Z cycle: the first of a month is 21:00 the day before in Tokyo,
    // so the boundary a viewer there sees is one frame earlier.
    const runTime = Date.parse("2026-09-19T12:00:00Z");
    const tokyo = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit" });
    const utc = plan(CFS, runTime);
    const jst = timelinePlan({
      leadSeconds: CFS,
      runTime,
      trackWidth: 680,
      monthKey: (validTime) => {
        const parts = tokyo.formatToParts(new Date(validTime));
        const year = parts.find((part) => part.type === "year")!.value;
        const month = parts.find((part) => part.type === "month")!.value;
        return `${year}-${month}`;
      },
    });
    const utcStarts = months(utc.segments).map((segment) => segment.index);
    const jstStarts = months(jst.segments).map((segment) => segment.index);
    expect(jstStarts).not.toEqual(utcStarts);
    expect(jstStarts.length).toBe(utcStarts.length);
    // Tokyo is ahead, so each month opens earlier on the axis.
    for (const [position, index] of jstStarts.entries()) {
      if (position === 0) continue;
      expect(index).toBeLessThan(utcStarts[position]!);
    }
  });

  it("leaves exactly one tick a day on an axis that straddles midnight", () => {
    // An axis whose step does not divide the day: 5-hourly for 60 days.
    const leads = [...Array(289).keys()].map((step) => step * 5 * HOUR);
    const odd = plan(leads);
    expect(odd.long).toBe(true);
    const days = new Set(odd.ticks.map((tick) => Math.floor(leads[tick.index]! / DAY)));
    expect(days.size).toBe(odd.ticks.length);
    expect(odd.ticks.length).toBe(Math.floor((leads.at(-1)! / DAY)) + 1);
  });
});

describe("the two regimes agree about what a mark is", () => {
  it("keeps a mark on a frame the axis really has, in both", () => {
    for (const leads of [GFS, ECMWF, CFS]) {
      const built = plan(leads);
      for (const tick of built.ticks) expect(leads[tick.index]).toBeDefined();
      for (const segment of built.segments) expect(leads[segment.index]).toBeDefined();
    }
  });
});
