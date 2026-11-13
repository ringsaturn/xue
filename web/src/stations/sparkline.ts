/** A surface station's 24-hour window as the card draws it: one small
 * chart per quantity, all on the same time axis, so a front's passage
 * reads across the temperature, the wind and the pressure at once.
 *
 * A gap in the record is a gap in the line: a station that stopped
 * reporting for two hours is not drawn as if it had held steady. The
 * precipitation is bars, one per report, because each value is a total
 * over its own period rather than a reading at an instant. */

import { element } from "../dom";
import { t } from "../i18n";
import {
  convertPrecipitation,
  convertPressure,
  convertTemperature,
  convertWind,
  formatPrecipitation,
  formatPressure,
  formatTemperature,
  formatWind,
} from "../units";
import { displayZone } from "../timezone";
import type { SynopStationSeries } from "./schema";

const SVG = "http://www.w3.org/2000/svg";
const WIDTH = 240;
const HEIGHT = 34;
/** Room above and below the line, so its extremes are not cut by the box. */
const PAD = 2;

/** A pair of reports further apart than this is a gap: three times the
 * station's usual spacing, and never under an hour (an hourly station
 * that misses one report keeps its line). */
export function gapSeconds(time: readonly number[]): number {
  const steps: number[] = [];
  for (let index = 1; index < time.length; index += 1) steps.push(time[index]! - time[index - 1]!);
  if (!steps.length) return 3600;
  steps.sort((a, b) => a - b);
  return Math.max(3600, 3 * steps[Math.floor(steps.length / 2)]!);
}

/** The value range a chart spans: the values' own, widened to `minSpan`
 * around their middle so a flat record is not drawn as noise. */
export function valueRange(values: readonly number[], minSpan: number): [number, number] {
  let low = Math.min(...values);
  let high = Math.max(...values);
  if (high - low < minSpan) {
    const middle = (low + high) / 2;
    low = middle - minSpan / 2;
    high = middle + minSpan / 2;
  }
  return [low, high];
}

/** The SVG path of one series: `M` where a run starts, `L` along it. A
 * point with no neighbour inside the gap is drawn as a dot by the caller. */
export function linePath(
  time: readonly number[],
  values: readonly (number | null)[],
  span: readonly [number, number],
  range: readonly [number, number],
  gap: number,
  width = WIDTH,
  height = HEIGHT,
): string {
  const x = (seconds: number) => ((seconds - span[0]) / Math.max(1, span[1] - span[0])) * width;
  const y = (value: number) => PAD + (1 - (value - range[0]) / (range[1] - range[0])) * (height - 2 * PAD);
  let path = "";
  let previous: number | null = null;
  time.forEach((seconds, index) => {
    const value = values[index];
    if (value === null || value === undefined) {
      previous = null;
      return;
    }
    const joined = previous !== null && seconds - previous <= gap;
    path += `${joined ? "L" : "M"}${x(seconds).toFixed(1)} ${y(value).toFixed(1)}`;
    previous = seconds;
  });
  return path;
}

/** Every six hours of the display zone's clock inside the span: where the
 * charts draw their grid and the axis its labels. Stepped by the half
 * hour so a half-hour zone still lands on its own whole hours. */
export function axisTicks(span: readonly [number, number], zone: string): { seconds: number; hour: number; day: string }[] {
  const format = new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit", hourCycle: "h23" });
  const ticks: { seconds: number; hour: number; day: string }[] = [];
  for (let seconds = Math.ceil(span[0] / 1800) * 1800; seconds <= span[1]; seconds += 1800) {
    const parts = Object.fromEntries(format.formatToParts(new Date(seconds * 1000)).map((part) => [part.type, part.value]));
    const hour = Number(parts.hour);
    if (parts.minute !== "00" || hour % 6 !== 0) continue;
    ticks.push({ seconds, hour, day: `${parts.month}/${parts.day}` });
  }
  return ticks;
}

interface Trace {
  values: (number | null)[];
  className: string;
}

/** Where on the chart's width a time falls, 0 … 1. */
function fraction(seconds: number, span: readonly [number, number]): number {
  return (seconds - span[0]) / Math.max(1, span[1] - span[0]);
}

function svg(span: readonly [number, number], ticks: readonly number[]): SVGSVGElement {
  const root = document.createElementNS(SVG, "svg");
  root.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  root.setAttribute("preserveAspectRatio", "none");
  root.setAttribute("aria-hidden", "true");
  root.classList.add("station-spark-plot");
  for (const seconds of ticks) {
    const x = (fraction(seconds, span) * WIDTH).toFixed(1);
    const line = document.createElementNS(SVG, "line");
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    line.setAttribute("y1", "0");
    line.setAttribute("y2", String(HEIGHT));
    line.setAttribute("class", "station-spark-grid");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    root.append(line);
  }
  return root;
}

function lineChart(
  time: readonly number[],
  traces: readonly Trace[],
  span: readonly [number, number],
  minSpan: number,
  ticks: readonly number[],
): SVGSVGElement {
  const root = svg(span, ticks);
  const all = traces.flatMap((trace) => trace.values).filter((value): value is number => value !== null);
  const range = valueRange(all, minSpan);
  const gap = gapSeconds(time);
  for (const trace of traces) {
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", linePath(time, trace.values, span, range, gap));
    path.setAttribute("class", trace.className);
    path.setAttribute("vector-effect", "non-scaling-stroke");
    root.append(path);
  }
  return root;
}

function barChart(time: readonly number[], values: readonly (number | null)[], span: readonly [number, number], ticks: readonly number[]): SVGSVGElement {
  const root = svg(span, ticks);
  const high = Math.max(1, ...values.filter((value): value is number => value !== null));
  const x = (seconds: number) => ((seconds - span[0]) / Math.max(1, span[1] - span[0])) * WIDTH;
  const bar = Math.max(1.5, WIDTH / Math.max(24, time.length) - 0.5);
  time.forEach((seconds, index) => {
    const value = values[index];
    if (value === null || value === undefined || value <= 0) return;
    const height = Math.max(1, (value / high) * (HEIGHT - PAD));
    const rect = document.createElementNS(SVG, "rect");
    rect.setAttribute("x", (x(seconds) - bar).toFixed(1));
    rect.setAttribute("y", (HEIGHT - height).toFixed(1));
    rect.setAttribute("width", bar.toFixed(1));
    rect.setAttribute("height", height.toFixed(1));
    rect.setAttribute("class", "station-spark-bar");
    root.append(rect);
  });
  return root;
}

function present(values: readonly (number | null)[] | undefined): number[] {
  return (values ?? []).filter((value): value is number => value !== null);
}

function convert(values: readonly (number | null)[] | undefined, to: (value: number) => number): (number | null)[] {
  return (values ?? []).map((value) => (value === null ? null : to(value)));
}

function row(label: string, summary: string, plot: SVGSVGElement): HTMLElement {
  const block = element("div", "station-spark");
  const head = element("div", "station-spark-head");
  head.append(element("span", "", label));
  head.append(element("span", "station-spark-range", summary));
  block.append(head, plot);
  return block;
}

/** The low–high of a series, in its display formatter. */
function spread(values: readonly number[], format: (value: number) => string): string {
  return `${format(Math.min(...values))} – ${format(Math.max(...values))}`;
}

type Column = readonly (number | null)[] | undefined;

/** A station's window as charts, row by row: each row is added only when
 * its series holds two values or more, and the block is null when no row
 * was. Values are converted to the viewer's units before they are drawn,
 * so a chart's shape and its labels agree. */
export class Sparks {
  private readonly span: [number, number];
  private readonly ticks: { seconds: number; hour: number; day: string }[];
  private readonly block = element("div", "station-sparks");

  /** `time` is epoch seconds, oldest first. */
  constructor(
    private readonly time: readonly number[],
    private readonly formatTime: (seconds: number) => string,
  ) {
    this.span = [time[0] ?? 0, time[time.length - 1] ?? 0];
    this.ticks = axisTicks(this.span, displayZone);
    this.block.append(element("div", "station-spark-title", t("stationLast24h")));
  }

  /** One line, with a quieter second (dew point, gust) where given. */
  line(
    label: string,
    values: Column,
    options: { second?: Column; to?: (value: number) => number; format: (value: number) => string; minSpan: number },
  ): this {
    const first = present(values);
    if (this.time.length < 2 || first.length < 2) return this;
    const to = options.to ?? ((value: number) => value);
    const traces: Trace[] = [{ values: convert(values, to), className: "station-spark-line" }];
    const second = present(options.second);
    if (second.length >= 2) traces.push({ values: convert(options.second, to), className: "station-spark-line station-spark-second" });
    const all = second.length >= 2 ? [...first, ...second] : first;
    this.block.append(row(label, spread(all, options.format), lineChart(this.time, traces, this.span, to(options.minSpan) - to(0), this.gridTimes())));
    return this;
  }

  /** Totals over each report's own period, as bars; `summary` from the
   * values in canonical units. */
  bars(label: string, values: Column, to: (value: number) => number, summary: (values: number[]) => string): this {
    const present_ = present(values);
    if (this.time.length < 2 || !present_.length) return this;
    this.block.append(row(label, summary(present_), barChart(this.time, convert(values, to), this.span, this.gridTimes())));
    return this;
  }

  private gridTimes(): number[] {
    return this.ticks.map((tick) => tick.seconds);
  }

  /** The axis under the charts: each six-hour mark at its place, the
   * date at midnight, and the window's two ends spelled out beneath. */
  finish(): HTMLElement | null {
    if (this.block.childElementCount === 1) return null;
    const axis = element("div", "station-spark-axis");
    for (const tick of this.ticks) {
      const at = fraction(tick.seconds, this.span);
      const label = element("span", "station-spark-tick", tick.hour === 0 ? tick.day : `${String(tick.hour).padStart(2, "0")}:00`);
      label.style.left = `${(at * 100).toFixed(2)}%`;
      // A mark at either end stays inside the card.
      if (at < 0.08) label.style.transform = "none";
      else if (at > 0.92) label.style.transform = "translateX(-100%)";
      if (tick.hour === 0) label.classList.add("station-spark-day");
      axis.append(label);
    }
    const ends = element("div", "station-spark-ends");
    ends.append(element("span", "", this.formatTime(this.span[0])), element("span", "", this.formatTime(this.span[1])));
    this.block.append(axis, ends);
    return this.block;
  }
}

/** A surface station's window (docs/synop.md §6). */
export function buildSynopSparklines(
  series: SynopStationSeries,
  prPeriod: number | null,
  formatTime: (seconds: number) => string,
): HTMLElement | null {
  const obs = series.obs;
  const sparks = new Sparks(series.time, formatTime)
    .line(t("stationTemperature"), obs.t, { second: obs.td, to: (value) => convertTemperature(value), format: (value) => formatTemperature(value), minSpan: 2 })
    .line(t("stationWind"), obs.ws, { second: obs.gust, to: (value) => convertWind(value), format: (value) => formatWind(value), minSpan: 2 });
  const pressureKey = present(obs.slp).length >= 2 ? "slp" : "p";
  sparks.line(t(pressureKey === "slp" ? "stationSeaLevelPressure" : "stationPressure"), obs[pressureKey], {
    to: (value) => convertPressure(value),
    format: (value) => formatPressure(value),
    minSpan: 2,
  });
  // The network's own period when it reports one, else its hourly total.
  // Adding up hourly totals reported every ten minutes would count the
  // same rain six times, so those show their peak instead of a sum.
  const prKey = present(obs.pr).length ? "pr" : "pr1h";
  const overlapping = prKey === "pr1h" && (prPeriod ?? 3600) < 3600;
  sparks.bars(t("stationPrecipitation"), obs[prKey], (value) => convertPrecipitation(value), (values) =>
    overlapping ? `↑ ${formatPrecipitation(Math.max(...values))}` : `Σ ${formatPrecipitation(values.reduce((sum, value) => sum + value, 0))}`,
  );
  return sparks.finish();
}
