/** The model forecast of a static airport page, the parts with no I/O: which
 * models a page reads, and the chart they draw.
 *
 * The chart is the airport's own meteogram across models: one row per
 * quantity (temperature, precipitation rate, wind speed), every model a
 * trace on the same clock, and the airport's reports as dots on the rows a
 * METAR carries. Traces are told apart by dashes, never by hue, as the
 * shell's charts are (`meteogram.ts`); the legend names each dash with its
 * model and run. SVG, as the page's 24-hour line is, so the block stays a
 * few kilobytes and prints. */

import { domainContains, lambertCone } from "../domain";
import { FORECAST_MODELS, type ForecastModelId } from "../manifest";

/** The models an airport page reads, in legend order: the three global
 * deterministic runs, then the regional one where its domain holds the
 * airport. The ECMWF 0.1° and HRES runs repeat the 0.25° one at a point,
 * and the ensembles and the seasonal run answer other questions. */
const AIRPORT_MODEL_IDS: readonly ForecastModelId[] = ["gfs", "ecmwf", "aifs", "hrrr"];

export function airportModels(longitude: number, latitude: number): ForecastModelId[] {
  return AIRPORT_MODEL_IDS.filter((id) => {
    const info = FORECAST_MODELS[id];
    if (!info || info.latestFilename === undefined) return false;
    return !info.domain || domainContains(info.domain, lambertCone(info.domain), longitude, latitude);
  });
}

export function airportModelLabel(id: ForecastModelId): string {
  return FORECAST_MODELS[id].label;
}

/** The hours the chart spans around now: the day the reports cover, then
 * five days ahead. */
export const CHART_PAST_HOURS = 24;
export const CHART_AHEAD_HOURS = 120;

export interface ChartPoint {
  /** Epoch milliseconds. */
  t: number;
  /** In the viewer's unit. */
  v: number;
}

export interface ChartTrace {
  label: string;
  points: ChartPoint[];
}

export interface ChartRow {
  title: string;
  /** The unit label the values are in. */
  unit: string;
  /** One per model, in legend order; a model without the quantity has an
   * empty trace and keeps its dash. */
  traces: ChartTrace[];
  observed: ChartPoint[];
  /** A value the range must include (zero for rates and speeds). */
  baseline: number | null;
}

export interface ChartInput {
  nowMs: number;
  tz: string;
  lang: string;
  rows: ChartRow[];
  /** Legend entries, in trace order: the model with its run. */
  legend: string[];
  observedLabel: string;
  nowLabel: string;
}

/** Dash patterns by trace index; the first is solid. */
const DASHES = ["", "6 3", "2 3", "9 3 2 3"];

const WIDTH = 640;
const ROW_HEIGHT = 96;
const PAD_LEFT = 34;
const PAD_RIGHT = 6;
const PAD_Y = 10;

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
}

function dashAttribute(index: number): string {
  const dash = DASHES[index % DASHES.length]!;
  return dash ? ` stroke-dasharray="${dash}"` : "";
}

/** A value range over the row's points with the baseline held, at least a
 * span of `minimum` so a flat series is not drawn as noise. */
function rowRange(row: ChartRow, inWindow: (point: ChartPoint) => boolean): [number, number] | null {
  const values = [...row.traces.flatMap((trace) => trace.points), ...row.observed].filter(inWindow).map((point) => point.v);
  if (values.length === 0) return null;
  let low = Math.min(...values, ...(row.baseline === null ? [] : [row.baseline]));
  let high = Math.max(...values, ...(row.baseline === null ? [] : [row.baseline]));
  const minimum = row.baseline === null ? 4 : 1;
  if (high - low < minimum) {
    if (row.baseline !== null && low === row.baseline) high = low + minimum;
    else {
      const middle = (high + low) / 2;
      low = middle - minimum / 2;
      high = middle + minimum / 2;
    }
  }
  return [low, high];
}

/** Epoch milliseconds of every local midnight inside the window, on the
 * airport's clock. */
export function localMidnights(startMs: number, endMs: number, tz: string): number[] {
  const hour = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const marks: number[] = [];
  // Every zone's offset is a whole number of quarter hours.
  const step = 15 * 60 * 1000;
  for (let t = Math.ceil(startMs / step) * step; t <= endMs; t += step) {
    if (hour.format(t) === "00:00") marks.push(t);
  }
  return marks;
}

/** The chart, every row over one axis, with its legend; empty when no row
 * has a point in the window. */
export function renderModelChart(input: ChartInput): string {
  const start = input.nowMs - CHART_PAST_HOURS * 3600_000;
  const end = input.nowMs + CHART_AHEAD_HOURS * 3600_000;
  const inWindow = (point: ChartPoint) => point.t >= start && point.t <= end;
  const x = (t: number) => (PAD_LEFT + ((t - start) / (end - start)) * (WIDTH - PAD_LEFT - PAD_RIGHT)).toFixed(1);
  const midnights = localMidnights(start, end, input.tz);
  const dayLabel = new Intl.DateTimeFormat(input.lang, { timeZone: input.tz, weekday: "short", day: "numeric" });

  const rows = input.rows
    .map((row) => ({ row, range: rowRange(row, inWindow) }))
    .filter((entry): entry is { row: ChartRow; range: [number, number] } => entry.range !== null);
  if (rows.length === 0) return "";

  const body = rows
    .map(({ row, range: [low, high] }, rowIndex) => {
      const y = (v: number) => (ROW_HEIGHT - PAD_Y - ((v - low) / (high - low)) * (ROW_HEIGHT - 2 * PAD_Y)).toFixed(1);
      const grid = midnights.map((t) => `<line class="day" x1="${x(t)}" x2="${x(t)}" y1="0" y2="${ROW_HEIGHT}"/>`).join("");
      const traces = row.traces
        .map((trace, index) => {
          const points = trace.points.filter(inWindow).sort((a, b) => a.t - b.t);
          if (points.length < 2) return "";
          const path = points.map((point, at) => `${at ? "L" : "M"}${x(point.t)} ${y(point.v)}`).join("");
          return `<path class="trace" d="${path}"${dashAttribute(index)}/>`;
        })
        .join("");
      const dots = row.observed
        .filter(inWindow)
        .map((point) => `<circle class="obs" cx="${x(point.t)}" cy="${y(point.v)}" r="1.8"/>`)
        .join("");
      const decimals = high - low < 3 ? 1 : 0;
      return `<figure class="mrow"><figcaption>${escapeHtml(row.title)} <span class="unit">${escapeHtml(row.unit)}</span></figcaption>
        <svg viewBox="0 0 ${WIDTH} ${ROW_HEIGHT}" role="img" aria-label="${escapeHtml(`${row.title} (${row.unit})`)}">${grid}
          <line class="now" x1="${x(input.nowMs)}" x2="${x(input.nowMs)}" y1="0" y2="${ROW_HEIGHT}"/>${traces}${dots}${
            rowIndex === 0 ? `<text class="nowlabel" x="${(Number(x(input.nowMs)) + 3).toFixed(1)}" y="8">${escapeHtml(input.nowLabel)}</text>` : ""
          }
          <text x="2" y="${PAD_Y + 4}">${high.toFixed(decimals)}</text><text x="2" y="${ROW_HEIGHT - PAD_Y + 4}">${low.toFixed(decimals)}</text>
        </svg></figure>`;
    })
    .join("");

  const ticks = midnights
    .map((t) => `<text x="${x(t)}" y="12">${escapeHtml(dayLabel.format(t))}</text>`)
    .join("");
  const axis = `<svg class="maxis" viewBox="0 0 ${WIDTH} 16" aria-hidden="true">${ticks}</svg>`;
  const legend = input.legend
    .map((label, index) => `<li><svg viewBox="0 0 28 8" aria-hidden="true"><line class="trace" x1="0" x2="28" y1="4" y2="4"${dashAttribute(index)}/></svg>${escapeHtml(label)}</li>`)
    .join("");
  const observed = rows.some(({ row }) => row.observed.some(inWindow))
    ? `<li><svg viewBox="0 0 28 8" aria-hidden="true"><circle class="obs" cx="14" cy="4" r="2"/></svg>${escapeHtml(input.observedLabel)}</li>`
    : "";
  return `<ul class="mlegend">${legend}${observed}</ul>${body}${axis}`;
}
