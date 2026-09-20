/**
 * The transport track's geometry: where the ticks stand and how the strip
 * under them is divided.
 *
 * Both are read off the axis alone — the frames' lead seconds, the run time
 * and the width the strip has — so the arithmetic is testable without a
 * document, and the capsule and the meteogram place their marks from one
 * answer rather than two that have to be kept in step.
 *
 * There are two regimes, and the axis's own span picks between them:
 *
 * - **Short** (up to `LONG_AXIS_DAYS`): every frame is a tick, with the full
 *   height on a day boundary, and the strip names whole forecast days —
 *   every other one once there are more than six. This is what a 240-hour
 *   run has always looked like and nothing about it changes.
 * - **Long** (beyond that): a seasonal run is 1092 frames over 273 days.
 *   One tick per frame would be a thousand hairlines in seven hundred
 *   pixels and a day strip would be nine months of weekday names on top of
 *   each other, so the axis is read one step coarser: one tick per day,
 *   full height on the first day of each month, and a strip segmented by
 *   month rather than by day.
 *
 * Ticks are laid out evenly by index (the stylesheet spaces them out), and
 * a segment's percent is that of the frame it starts on, so the two agree
 * wherever a day's worth of frames is a fixed number of them — which every
 * axis long enough to be read in months is.
 *
 * Nothing here formats a date. A segment carries the frame its label is
 * read at and, for a month, the `YYYY-MM` it falls in; the caller turns
 * that into text through `Intl` in the locale and the display zone, which
 * is why the month a valid time falls in arrives as a function.
 */

const DAY_SECONDS = 86_400;

/** Beyond this many days the axis is read in days and months rather than in
 * frames and days. Six weeks: the longest medium-range run (360 hours) is
 * well inside it, and a seasonal one is far outside, so no live axis sits
 * near the switch and wavers across it. */
export const LONG_AXIS_DAYS = 45;

/** The room the start and horizon labels take at either end of the strip,
 * in pixels of the track: they share the band with the segment labels, so a
 * mark centred inside that room would run into one of them. */
export const TRACK_END_LABEL_PX = 48;

/** The room a month name needs. Mono at 10px: three or four letters, or a
 * month and a year where the year turns over. */
const MONTH_LABEL_PX = 42;

export interface TimelineTick {
  /** The frame the tick stands on. */
  index: number;
  /** Drawn full height: a day boundary on a short axis, the first day of a
   * month on a long one. */
  major: boolean;
}

interface SegmentBase {
  /** The frame the segment's label is read at. */
  index: number;
  /** Where the label sits along the track, in percent of its width. */
  percent: number;
  /** Whether the strip has room for the label clear of its two end labels.
   * The capsule draws the marks that fit; the meteogram, whose header has
   * the whole width to itself, draws them all. */
  labelled: boolean;
}

/** One whole forecast day from the run time. */
export interface TimelineDaySegment extends SegmentBase {
  kind: "day";
  day: number;
}

/** One calendar month of the axis, as a span rather than a boundary. */
export interface TimelineMonthSegment extends SegmentBase {
  kind: "month";
  /** `YYYY-MM` in the zone the strip is read in. */
  month: string;
  /** Whether this month starts a year the month before it was not in — the
   * label carries the year there and nowhere else. */
  yearChanged: boolean;
  startPercent: number;
  endPercent: number;
}

export type TimelineSegment = TimelineDaySegment | TimelineMonthSegment;

export interface TimelinePlan {
  /** True once the axis is past `LONG_AXIS_DAYS` and read in months. */
  long: boolean;
  ticks: TimelineTick[];
  segments: TimelineSegment[];
}

export interface TimelineInput {
  /** Seconds from the run time to each frame, in frame order. */
  leadSeconds: readonly number[];
  /** The run time as a millisecond epoch: a frame's valid time is this plus
   * its lead. */
  runTime: number;
  /** The track's width in CSS pixels — what decides which labels fit. */
  trackWidth: number;
  /** The month a valid time falls in, `YYYY-MM` in the display zone.
   * Defaults to UTC, which is what a test wants and never what the shell
   * passes. */
  monthKey?: (validTime: number) => string;
}

const EMPTY: TimelinePlan = { long: false, ticks: [], segments: [] };

function utcMonthKey(validTime: number): string {
  return new Date(validTime).toISOString().slice(0, 7);
}

/** The track's ticks and strip segments for one axis. */
export function timelinePlan(input: TimelineInput): TimelinePlan {
  const leads = input.leadSeconds;
  if (leads.length === 0) return EMPTY;
  const span = (leads[leads.length - 1]! - leads[0]!) / DAY_SECONDS;
  return span > LONG_AXIS_DAYS ? longPlan(input) : shortPlan(input);
}

/** Where a frame sits along the track: the ticks are laid out evenly by
 * index, so the strip is too. */
function percentOf(index: number, count: number): number {
  return count > 1 ? (index / (count - 1)) * 100 : 0;
}

/** The share of the track one label needs, floored so a width that has not
 * been measured yet cannot make everything fit. */
function labelPercent(pixels: number, trackWidth: number): number {
  return (pixels / Math.max(1, trackWidth)) * 100;
}

function shortPlan(input: TimelineInput): TimelinePlan {
  const leads = input.leadSeconds;
  const count = leads.length;
  const ticks = leads.map((lead, index) => ({ index, major: lead % DAY_SECONDS === 0 }));

  // A day mark stands on the frame whose lead is exactly that many days
  // out; a step that never lands on the boundary leaves the day unmarked.
  const byLead = new Map<number, number>();
  leads.forEach((lead, index) => {
    if (!byLead.has(lead)) byLead.set(lead, index);
  });
  const days = Math.floor(leads[count - 1]! / DAY_SECONDS);
  // Ten marks on a 240-hour run would collide, so a long axis names every
  // other day.
  const stride = days > 6 ? 2 : 1;
  const edge = Math.max(4, labelPercent(TRACK_END_LABEL_PX, input.trackWidth));
  const segments: TimelineSegment[] = [];
  for (let day = stride; day <= days; day += stride) {
    const index = byLead.get(day * DAY_SECONDS);
    if (index === undefined) continue;
    const percent = percentOf(index, count);
    segments.push({
      kind: "day",
      index,
      day,
      percent,
      // The ends of the strip already name the start and the horizon.
      labelled: percent >= edge && percent <= 100 - edge,
    });
  }
  return { long: false, ticks, segments };
}

function longPlan(input: TimelineInput): TimelinePlan {
  const leads = input.leadSeconds;
  const count = leads.length;
  const monthKey = input.monthKey ?? utcMonthKey;
  const keyAt = (index: number): string => monthKey(input.runTime + leads[index]! * 1000);

  // One tick a day: the first frame of each day the axis covers, so a step
  // that straddles midnight still leaves exactly one.
  const ticks: TimelineTick[] = [];
  let lastDay: number | null = null;
  let lastTickMonth: string | null = null;
  for (let index = 0; index < count; index += 1) {
    const day = Math.floor(leads[index]! / DAY_SECONDS);
    if (day === lastDay) continue;
    lastDay = day;
    const month = keyAt(index);
    ticks.push({ index, major: lastTickMonth !== null && month !== lastTickMonth });
    lastTickMonth = month;
  }

  // The strip's segments: each month's own share of the track, from the
  // first frame that falls in it to the first frame of the next.
  const starts: { index: number; month: string }[] = [];
  let lastMonth: string | null = null;
  for (let index = 0; index < count; index += 1) {
    const month = keyAt(index);
    if (month === lastMonth) continue;
    lastMonth = month;
    starts.push({ index, month });
  }
  const edge = Math.max(4, labelPercent(TRACK_END_LABEL_PX, input.trackWidth));
  const room = labelPercent(MONTH_LABEL_PX, input.trackWidth);
  // Left to right, naming a month whose label fits inside its own span once
  // the end labels have taken their room, and only where it clears the last
  // name written. So a desktop strip carries all nine months of a seasonal
  // run and a phone carries every second or third, the way the day strip
  // thins to every other day — and a sliver of a month the run merely
  // reaches into goes unnamed rather than sitting over its neighbour.
  let written = -Infinity;
  const segments: TimelineSegment[] = starts.map((start, position) => {
    const next = starts[position + 1];
    const startPercent = percentOf(start.index, count);
    const endPercent = next ? percentOf(next.index, count) : 100;
    const from = Math.max(startPercent, edge);
    const to = Math.min(endPercent, 100 - edge);
    const percent = (from + to) / 2;
    const labelled = to > from && percent - written >= room;
    if (labelled) written = percent;
    const previous = starts[position - 1];
    return {
      kind: "month",
      index: start.index,
      month: start.month,
      yearChanged: previous !== undefined && start.month.slice(0, 4) !== previous.month.slice(0, 4),
      startPercent,
      endPercent,
      percent,
      labelled,
    };
  });
  return { long: true, ticks, segments };
}
