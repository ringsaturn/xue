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

interface Trace {
  values: (number | null)[];
  className: string;
}

function svg(): SVGSVGElement {
  const root = document.createElementNS(SVG, "svg");
  root.setAttribute("viewBox", `0 0 ${WIDTH} ${HEIGHT}`);
  root.setAttribute("preserveAspectRatio", "none");
  root.setAttribute("aria-hidden", "true");
  root.classList.add("station-spark-plot");
  return root;
}

function lineChart(
  time: readonly number[],
  traces: readonly Trace[],
  span: readonly [number, number],
  minSpan: number,
): SVGSVGElement {
  const root = svg();
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

function barChart(time: readonly number[], values: readonly (number | null)[], span: readonly [number, number]): SVGSVGElement {
  const root = svg();
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

/** The window's charts, or null when it holds fewer than two reports. The
 * values are converted to the viewer's units before they are drawn, so a
 * chart's shape and its labels agree. */
export function buildSynopSparklines(
  series: SynopStationSeries,
  prPeriod: number | null,
  formatTime: (seconds: number) => string,
): HTMLElement | null {
  const time = series.time;
  if (time.length < 2) return null;
  const span: [number, number] = [time[0]!, time[time.length - 1]!];
  const obs = series.obs;
  const block = element("div", "station-sparks");
  block.append(element("div", "station-spark-title", t("stationLast24h")));

  const t2 = present(obs.t);
  if (t2.length >= 2) {
    const traces: Trace[] = [{ values: convert(obs.t, (value) => convertTemperature(value)), className: "station-spark-line" }];
    if (present(obs.td).length >= 2)
      traces.push({ values: convert(obs.td, (value) => convertTemperature(value)), className: "station-spark-line station-spark-second" });
    block.append(row(t("stationTemperature"), spread(t2, (value) => formatTemperature(value)), lineChart(time, traces, span, 2)));
  }

  const ws = present(obs.ws);
  if (ws.length >= 2) {
    const traces: Trace[] = [{ values: convert(obs.ws, (value) => convertWind(value)), className: "station-spark-line" }];
    const gust = present(obs.gust);
    if (gust.length >= 2)
      traces.push({ values: convert(obs.gust, (value) => convertWind(value)), className: "station-spark-line station-spark-second" });
    const all = gust.length ? [...ws, ...gust] : ws;
    block.append(row(t("stationWind"), spread(all, (value) => formatWind(value)), lineChart(time, traces, span, convertWind(2))));
  }

  const pressureKey = present(obs.slp).length >= 2 ? "slp" : present(obs.p).length >= 2 ? "p" : null;
  if (pressureKey) {
    const values = present(obs[pressureKey]);
    block.append(
      row(
        t(pressureKey === "slp" ? "stationSeaLevelPressure" : "stationPressure"),
        spread(values, (value) => formatPressure(value)),
        lineChart(time, [{ values: convert(obs[pressureKey], (value) => convertPressure(value)), className: "station-spark-line" }], span, convertPressure(2)),
      ),
    );
  }

  // The network's own period when it reports one, else its hourly total;
  // the sum is over the window, which is what a viewer asks of a day.
  const prKey = present(obs.pr).length ? "pr" : present(obs.pr1h).length ? "pr1h" : null;
  if (prKey) {
    const values = obs[prKey]!;
    const hourly = prKey === "pr1h" && (prPeriod ?? 3600) < 3600;
    // Adding up overlapping hourly totals reported every ten minutes would
    // count the same rain six times, so the sum is only of the network's
    // own period's totals.
    const total = hourly ? null : present(values).reduce((sum, value) => sum + value, 0);
    const summary = total === null ? `↑ ${formatPrecipitation(Math.max(...present(values)))}` : `Σ ${formatPrecipitation(total)}`;
    block.append(row(t("stationPrecipitation"), summary, barChart(time, convert(values, (value) => convertPrecipitation(value)), span)));
  }

  if (block.childElementCount === 1) return null;
  const axis = element("div", "station-spark-axis");
  axis.append(element("span", "", formatTime(span[0])), element("span", "", formatTime(span[1])));
  block.append(axis);
  return block;
}
