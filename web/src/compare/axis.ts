/**
 * The comparison page's clock: one time axis every model block is laid on.
 *
 * Runs start at different cycles (a 06Z GFS beside a 00Z ECMWF) and step at
 * different rates (hourly, 3-hourly, 6-hourly, and a change of step along
 * the way), so nothing is compared by frame index. The axis is real time:
 * a column is one hour (or three, on the long horizon), a frame is placed
 * at its valid time and spans until the model's next frame, and every
 * block shares the same origin, so a value at 15:00 sits under 15:00 in
 * every block, whatever run it came from.
 *
 * Everything here is arithmetic on epoch milliseconds and pixels, with no
 * DOM, so it is unit-tested outright.
 */

export const HOUR_MS = 3600 * 1000;

/** The two horizons a chip picks. The short one shows every hour; the long
 * one shows every third, since 360 hourly columns is a very long scroll
 * and an hourly value 12 px wide cannot be read. */
export type SpanHours = 120 | 360;
export const SPANS: readonly SpanHours[] = [120, 360];
export const DEFAULT_SPAN: SpanHours = 120;

/** One column's width. Wide enough for `−12°`, `12.5`, a wind arrow beside
 * two digits, and a three-digit local hour is never needed. */
export const COLUMN_PX = 40;

/** How far back the axis reaches behind the present, at most: a day of
 * airport reports to lay the runs' first frames beside. */
export const LOOKBACK_HOURS = 24;

export interface Axis {
  /** Epoch milliseconds of the left edge, on a column boundary. */
  startMs: number;
  /** Epoch milliseconds of the right edge. */
  endMs: number;
  /** Hours per column: 1 or 3. */
  columnHours: 1 | 3;
  /** The present, as the page was opened. */
  nowMs: number;
  /** The whole track's width in pixels. */
  widthPx: number;
}

export function columnHoursFor(span: SpanHours): 1 | 3 {
  return span === 120 ? 1 : 3;
}

/** The axis for a set of runs: it begins at the earliest run's cycle, so
 * every run's first frame is on it, but never more than `LOOKBACK_HOURS`
 * before now, and always on a column boundary of the UTC clock — the
 * cycles are 00/06/12/18Z and every model's frames are whole hours from
 * them, so a 3-hour grid on UTC hours divisible by three catches a frame
 * of every step the sources have. With no run loaded yet it begins at the
 * present hour. */
export function buildAxis(runTimesMs: readonly number[], nowMs: number, span: SpanHours): Axis {
  const columnHours = columnHoursFor(span);
  const columnMs = columnHours * HOUR_MS;
  const earliestRun = runTimesMs.length > 0 ? Math.min(...runTimesMs) : nowMs;
  const floor = Math.max(earliestRun, nowMs - LOOKBACK_HOURS * HOUR_MS);
  const startMs = Math.floor(Math.min(floor, nowMs) / columnMs) * columnMs;
  const endMs = startMs + span * HOUR_MS;
  return { startMs, endMs, columnHours, nowMs, widthPx: (span / columnHours) * COLUMN_PX };
}

/** Pixels from the left edge for an instant; may fall outside the track. */
export function xOf(axis: Axis, timeMs: number): number {
  return ((timeMs - axis.startMs) / (axis.columnHours * HOUR_MS)) * COLUMN_PX;
}

/** One frame kept on the axis: its index into the series, the span it
 * fills, and where that lands. */
export interface FrameCell {
  index: number;
  startMs: number;
  endMs: number;
  x: number;
  width: number;
}

/**
 * The frames of a series to draw, in order. A frame is kept when it is on
 * the axis and on the column grid — an hourly model on the 3-hour axis
 * shows every third frame — or when the model's own step is coarser than
 * the column, in which case every frame is kept and each spans its step.
 * A cell ends where the next kept frame begins; the last spans the step
 * before it (or one column), and every cell is clipped to the axis.
 */
export function layoutFrames(axis: Axis, validTimesMs: readonly number[]): FrameCell[] {
  const columnMs = axis.columnHours * HOUR_MS;
  const kept: { index: number; timeMs: number }[] = [];
  let lastKept = -Infinity;
  for (const [index, timeMs] of validTimesMs.entries()) {
    if (timeMs < axis.startMs || timeMs >= axis.endMs) continue;
    const aligned = (timeMs - axis.startMs) % columnMs === 0;
    if (!aligned && timeMs - lastKept < columnMs) continue;
    kept.push({ index, timeMs });
    lastKept = timeMs;
  }
  return kept.map((frame, position) => {
    const next = kept[position + 1];
    let endMs: number;
    if (next) {
      endMs = next.timeMs;
    } else {
      const previous = kept[position - 1];
      const step = previous ? frame.timeMs - previous.timeMs : columnMs;
      endMs = frame.timeMs + Math.max(step, columnMs);
    }
    endMs = Math.min(endMs, axis.endMs);
    const x = xOf(axis, frame.timeMs);
    return { index: frame.index, startMs: frame.timeMs, endMs, x, width: xOf(axis, endMs) - x };
  });
}

/** Observations are reports at their own instants, not frames of a step:
 * each fills the time to the next report, and the newest reaches to the
 * present. Reports before the axis or after `nowMs` are dropped, and
 * reports closer together than a column are thinned from the newest
 * backwards — the last report always stays, a special report between two
 * hourly ones does not — so no two cells overlap. Input in any order;
 * output ascending. */
export function layoutReports(axis: Axis, timesMs: readonly number[]): FrameCell[] {
  const columnMs = axis.columnHours * HOUR_MS;
  const newestFirst = timesMs
    .map((timeMs, index) => ({ index, timeMs }))
    .filter((report) => report.timeMs >= axis.startMs && report.timeMs <= axis.nowMs)
    .sort((a, b) => b.timeMs - a.timeMs);
  const kept: { index: number; timeMs: number }[] = [];
  let lastKept = Infinity;
  for (const report of newestFirst) {
    if (lastKept - report.timeMs < columnMs) continue;
    kept.push(report);
    lastKept = report.timeMs;
  }
  kept.reverse();
  return kept.map((report, position) => {
    const next = kept[position + 1];
    const endMs = Math.min(next ? next.timeMs : Math.max(axis.nowMs, report.timeMs + columnMs), axis.endMs);
    const x = xOf(axis, report.timeMs);
    return { index: report.index, startMs: report.timeMs, endMs, x, width: xOf(axis, endMs) - x };
  });
}

/** A column of the axis with its clock reading in the display zone. */
export interface HourColumn {
  timeMs: number;
  x: number;
  /** Local hour, 0–23. */
  hour: number;
  /** Between 18:00 and 06:00 local: the shaded columns. */
  night: boolean;
}

/** A run of columns falling on one local date. */
export interface DayBand {
  label: string;
  x: number;
  width: number;
}

function localParts(timeMs: number, zone: string): { hour: number; dayKey: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
  }).formatToParts(new Date(timeMs));
  const read = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return { hour: Number(read("hour")) % 24, dayKey: `${read("year")}-${read("month")}-${read("day")}` };
}

export function hourColumns(axis: Axis, zone: string): HourColumn[] {
  const columnMs = axis.columnHours * HOUR_MS;
  const columns: HourColumn[] = [];
  for (let timeMs = axis.startMs; timeMs < axis.endMs; timeMs += columnMs) {
    const { hour } = localParts(timeMs, zone);
    columns.push({ timeMs, x: xOf(axis, timeMs), hour, night: hour < 6 || hour >= 18 });
  }
  return columns;
}

/** The days the axis crosses, each labelled in the page's language
 * ("Saturday 19"), widths in pixels. */
export function dayBands(axis: Axis, zone: string, lang: string): DayBand[] {
  const columnMs = axis.columnHours * HOUR_MS;
  const format = new Intl.DateTimeFormat(lang, { timeZone: zone, weekday: "long", day: "numeric" });
  const bands: DayBand[] = [];
  let current: { key: string; startMs: number } | null = null;
  const close = (endMs: number) => {
    if (!current) return;
    const x = xOf(axis, current.startMs);
    bands.push({ label: format.format(new Date(current.startMs)), x, width: xOf(axis, endMs) - x });
  };
  for (let timeMs = axis.startMs; timeMs < axis.endMs; timeMs += columnMs) {
    const { dayKey } = localParts(timeMs, zone);
    if (!current || current.key !== dayKey) {
      close(timeMs);
      current = { key: dayKey, startMs: timeMs };
    }
  }
  close(axis.endMs);
  return bands;
}

/** The hour label a column shows: two digits in the display zone. */
export function hourLabel(hour: number): string {
  return String(hour).padStart(2, "0");
}
