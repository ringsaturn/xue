/**
 * A gauge's hydrograph in the probe panel: what the gauge measured, what
 * the model carried at it, and the peaks the operators reported, on the
 * meteogram's own axis so the playhead here and on the capsule agree to
 * the pixel (`meteogram.ts::columnX`, `axisPosition`).
 *
 * The flow axis is logarithmic, by whole decades: a flood is three orders
 * of magnitude over the dry bed it rose from, and on a linear axis the
 * days before it are a flat line on the floor. Everything is placed by its
 * own clock — a five-minute gauge series and an hourly model both — and
 * what falls off the case's axis is not drawn, as the meteogram does with
 * an airport's reports. A missing reading is a gap, never bridged.
 */

import { axisPosition, columnX } from "../meteogram";

/** A series by its own clock: epoch milliseconds and m³/s, null a gap. */
export interface TimedSeries {
  times: readonly number[];
  values: readonly (number | null)[];
}

export interface HydrographPeak {
  timeMs: number;
  value: number;
  /** The peak's number in the list under the chart. */
  label: string;
}

export interface HydrographData {
  /** The case axis: its run time and each frame's lead seconds. */
  runTimeMs: number;
  leadSeconds: readonly number[];
  selected: number;
  observed: TimedSeries | null;
  /** A dam gauge's outflow, under the inflow. */
  outflow: TimedSeries | null;
  modelled: TimedSeries | null;
  /** The reported peaks in the chart's unit that carry a time. */
  peaks: readonly HydrographPeak[];
}

export interface HydrographInk {
  ink: string;
  muted: string;
}

const PADDING = { top: 6, bottom: 6, left: 0, right: 0 } as const;
/** The least flow the axis reaches down to: under it is a dry bed. */
const FLOOR = 0.1;

/** The decades a set of flows spans: `[10^a, 10^b]` with `b > a`, the
 * lowest positive value's decade to the highest's, at least one decade,
 * never under `FLOOR`. With nothing positive, 1 to 1000. */
export function logRange(series: readonly (readonly (number | null)[])[]): [number, number] {
  let lowest = Infinity;
  let highest = -Infinity;
  for (const values of series) {
    for (const value of values) {
      if (typeof value !== "number" || !(value > 0) || !Number.isFinite(value)) continue;
      lowest = Math.min(lowest, value);
      highest = Math.max(highest, value);
    }
  }
  if (lowest === Infinity) return [1, 1000];
  const low = Math.floor(Math.log10(Math.max(lowest, FLOOR)));
  let high = Math.ceil(Math.log10(Math.max(highest, FLOOR)));
  if (high <= low) high = low + 1;
  return [10 ** low, 10 ** high];
}

/** A decade's label: `0.1`, `1`, `10`, `100`, `1k`, `10k`. */
export function decadeLabel(value: number): string {
  if (value >= 1000) return `${value / 1000}k`;
  return String(Number(value.toPrecision(1)));
}

/** The value of a series at an instant: the nearest reading within
 * `toleranceMs`, or null. */
export function valueNear(series: TimedSeries | null, time: number, toleranceMs: number): number | null {
  if (!series || series.times.length === 0) return null;
  let low = 0;
  let high = series.times.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (series.times[middle]! < time) low = middle + 1;
    else high = middle;
  }
  let best: number | null = null;
  let distance = Infinity;
  for (const index of [low - 1, low]) {
    const at = series.times[index];
    const value = series.values[index];
    if (at === undefined || value === null || value === undefined) continue;
    const gap = Math.abs(at - time);
    if (gap <= toleranceMs && gap < distance) {
      best = value;
      distance = gap;
    }
  }
  return best;
}

export function drawHydrograph(
  context: CanvasRenderingContext2D,
  size: { width: number; height: number },
  data: HydrographData,
  options: { ink: HydrographInk; font: string },
): void {
  const { width, height } = size;
  const { ink } = options;
  context.clearRect(0, 0, width, height);
  const count = data.leadSeconds.length;
  if (count === 0 || width <= 0 || height <= 0) return;
  const [low, high] = logRange([
    data.observed?.values ?? [],
    data.outflow?.values ?? [],
    data.modelled?.values ?? [],
    data.peaks.map((peak) => peak.value),
  ]);
  const top = PADDING.top;
  const bottom = height - PADDING.bottom;
  const logLow = Math.log10(low);
  const logHigh = Math.log10(high);
  const y = (value: number): number =>
    bottom - ((Math.log10(Math.max(value, low)) - logLow) / (logHigh - logLow)) * (bottom - top);
  const xAt = (time: number): number | null => {
    const position = axisPosition(data.leadSeconds, (time - data.runTimeMs) / 1000);
    return position === null ? null : columnX(position, count, width);
  };

  context.font = options.font;
  context.textBaseline = "bottom";
  context.textAlign = "left";
  context.lineJoin = "round";
  context.lineCap = "round";
  // Decade lines, labelled at the left edge.
  context.strokeStyle = ink.muted;
  context.fillStyle = ink.muted;
  context.lineWidth = 1;
  for (let decade = Math.round(logLow); decade <= Math.round(logHigh); decade += 1) {
    const py = Math.round(y(10 ** decade)) + 0.5;
    context.globalAlpha = 0.22;
    context.beginPath();
    context.moveTo(0, py);
    context.lineTo(width, py);
    context.stroke();
    context.globalAlpha = 0.8;
    if (py - 9 >= 0) context.fillText(decadeLabel(10 ** decade), 2, py - 1);
  }
  context.globalAlpha = 1;

  const trace = (series: TimedSeries | null, style: { color: string; width: number; dash: number[] }): void => {
    if (!series) return;
    context.strokeStyle = style.color;
    context.lineWidth = style.width;
    context.setLineDash(style.dash);
    context.beginPath();
    let pen = false;
    for (const [index, time] of series.times.entries()) {
      const value = series.values[index];
      const px = xAt(time);
      if (px === null || value === null || value === undefined || !Number.isFinite(value)) {
        pen = false;
        continue;
      }
      const py = y(value);
      if (pen) context.lineTo(px, py);
      else context.moveTo(px, py);
      pen = true;
    }
    context.stroke();
    context.setLineDash([]);
  };
  trace(data.modelled, { color: ink.muted, width: 1.2, dash: [4, 3] });
  trace(data.outflow, { color: ink.ink, width: 1, dash: [1, 3] });
  trace(data.observed, { color: ink.ink, width: 1.5, dash: [] });

  // The reported peaks: a small downward triangle over the value, numbered
  // as the list under the chart numbers them.
  context.fillStyle = ink.ink;
  context.textAlign = "center";
  for (const peak of data.peaks) {
    const px = xAt(peak.timeMs);
    if (px === null) continue;
    const py = y(peak.value);
    context.beginPath();
    context.moveTo(px - 4, py - 7);
    context.lineTo(px + 4, py - 7);
    context.lineTo(px, py - 2);
    context.closePath();
    context.fill();
    if (py - 9 >= 8) context.fillText(peak.label, px, py - 8);
  }

  // The playhead, over everything.
  context.strokeStyle = ink.ink;
  context.globalAlpha = 0.55;
  context.lineWidth = 1;
  const px = Math.round(columnX(data.selected, count, width)) + 0.5;
  context.beginPath();
  context.moveTo(px, 0);
  context.lineTo(px, height);
  context.stroke();
  context.globalAlpha = 1;
}
