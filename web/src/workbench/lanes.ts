/**
 * The surface under the charts, a lane each: temperature and dew point, the
 * wind and its gusts, the pressure, and through time the rain. A lane's range
 * is of what is on it and written at its ends; the numbers at the probe are in
 * the column between the two, in elements.
 */

import { element } from "../dom";
import { convertPrecipitation, convertPressure, convertTemperature, convertWind } from "../units";
import type { LaneValues } from "./engine";
import { LANES, ROWS } from "./layout";
import { Pad, type PadAxis, type PadHandlers } from "./pad";

/** One lane's series in the reader's units, a value a place of the axis. */
interface Trace {
  values: ArrayLike<number>;
  color: string;
  dash?: number[];
}

export interface LaneExtra {
  /** m/s and mm/h a frame, NaN where there is none; null until read. */
  gust: ArrayLike<number> | null;
  rain: ArrayLike<number> | null;
}

/** The least span a lane is stretched over, in SI: a flat line is flat. */
const LEAST_SPAN = { temperature: 4, wind: 5, pressure: 4, rain: 1 } as const;
/** mm/h a dot is drawn from: under it the rate's code is none. */
const DRY = 0.05;

function map(values: ArrayLike<number>, convert: (value: number) => number): Float64Array {
  return Float64Array.from(values, (value) => (value === value ? convert(value) : Number.NaN));
}

function range(traces: readonly ArrayLike<number>[], least: number, floor: number | null): [number, number] | null {
  let low = Number.POSITIVE_INFINITY;
  let high = Number.NEGATIVE_INFINITY;
  for (const values of traces) {
    for (let at = 0; at < values.length; at += 1) {
      const value = values[at]!;
      if (value !== value) continue;
      low = Math.min(low, value);
      high = Math.max(high, value);
    }
  }
  if (!(high >= low)) return null;
  if (floor !== null) low = Math.min(low, floor);
  const short = least - (high - low);
  if (short > 0) {
    if (floor === null) low -= short / 2;
    high = low + least;
  }
  return [low, high];
}

export class LaneView {
  readonly root = element("div", "wb-lanes");
  readonly pad: Pad;
  private readonly canvas = element("canvas", "wb-lanes-canvas");
  private data: LaneValues | null = null;
  private extra: LaneExtra = { gust: null, rain: null };
  private axis: PadAxis | null = null;

  constructor(
    className: string,
    handlers: PadHandlers,
    /** How many of the lanes there are. */
    private readonly lanes: number = LANES,
  ) {
    this.canvas.setAttribute("aria-hidden", "true");
    this.pad = new Pad(className, { ...handlers, resized: () => this.paint() });
    this.pad.root.prepend(this.canvas);
    this.root.append(this.pad.root);
  }

  setAxis(axis: PadAxis | null): void {
    this.axis = axis;
    this.pad.setAxis(axis);
    this.paint();
  }

  show(data: LaneValues | null, extra: LaneExtra = { gust: null, rain: null }): void {
    this.data = data;
    this.extra = extra;
    this.paint();
  }

  paint(): void {
    const { canvas, data, axis, extra } = this;
    const { width } = this.pad;
    const height = LANES * ROWS.lane;
    if (width === 0) return;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const style = getComputedStyle(this.root);
    const ink = (name: string, fallback: string): string => style.getPropertyValue(name).trim() || fallback;
    const text = ink("--text", "#1b1a17");
    const muted = ink("--muted", "#6e6a60");
    context.strokeStyle = ink("--line", "rgba(27, 26, 23, 0.16)");
    context.lineWidth = 1;
    for (let lane = 0; lane <= this.lanes; lane += 1) {
      const y = Math.round(lane * ROWS.lane) + 0.5;
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }
    if (!data || !axis) return;
    const { tmp2m, dpt2m, ugrd10m, vgrd10m, prmsl } = data.values;
    const speed = Float32Array.from(ugrd10m, (u, at) => Math.hypot(u, vgrd10m[at]!));
    const scale = (least: number, convert: (value: number) => number): number => Math.abs(convert(least) - convert(0));
    this.lane(context, 0, [
      { values: map(tmp2m, (value) => convertTemperature(value)), color: ink("--skewt-temperature", "#b4441a") },
      { values: map(dpt2m, (value) => convertTemperature(value)), color: ink("--skewt-dew", "#0e7c72") },
    ], scale(LEAST_SPAN.temperature, (value) => convertTemperature(value)), null, muted, 0);
    this.lane(context, 1, [
      { values: map(speed, (value) => convertWind(value)), color: text },
      ...(extra.gust ? [{ values: map(extra.gust, (value) => convertWind(value)), color: muted, dash: [3, 2] }] : []),
    ], scale(LEAST_SPAN.wind, (value) => convertWind(value)), 0, muted, 0);
    this.lane(context, 2, [{ values: map(prmsl, (value) => convertPressure(value)), color: text }],
      scale(LEAST_SPAN.pressure, (value) => convertPressure(value)), null, muted, convertPressure(1) < 0.1 ? 2 : 0);
    if (this.lanes > 3 && extra.rain) {
      const values = map(extra.rain, (value) => convertPrecipitation(value));
      this.rain(context, values, ink("--wb-rain", "#2c68a8"), muted);
    }
  }

  private lane(context: CanvasRenderingContext2D, lane: number, traces: Trace[], least: number, floor: number | null, muted: string, decimals: number): void {
    const { axis } = this;
    const { width } = this.pad;
    const span = range(traces.map(({ values }) => values), least, floor);
    if (!axis || !span) return;
    const [low, high] = span;
    const base = (lane + 1) * ROWS.lane - 5;
    const y = (value: number): number => base - ((value - low) / (high - low)) * (ROWS.lane - 10);
    for (const { values, color, dash } of traces) {
      context.strokeStyle = color;
      context.lineWidth = 1.5;
      context.lineJoin = "round";
      context.setLineDash(dash ?? []);
      context.beginPath();
      let pen = false;
      for (let at = 0; at < values.length; at += 1) {
        const value = values[at]!;
        const x = axis.x(at, width);
        if (value !== value || x < -1 || x > width + 1) {
          pen = false;
          continue;
        }
        if (pen) context.lineTo(x, y(value));
        else context.moveTo(x, y(value));
        pen = true;
      }
      context.stroke();
    }
    context.setLineDash([]);
    this.ends(context, lane, high.toFixed(decimals), low.toFixed(decimals), muted);
  }

  /** A rate is at an instant, so it stands on it: a stem with a dot. */
  private rain(context: CanvasRenderingContext2D, values: Float64Array, color: string, muted: string): void {
    const { axis } = this;
    const { width } = this.pad;
    const span = range([values], convertPrecipitation(LEAST_SPAN.rain), 0);
    if (!axis || !span) return;
    const base = LANES * ROWS.lane - 3;
    context.strokeStyle = context.fillStyle = color;
    context.lineWidth = 1.5;
    for (let at = 0; at < values.length; at += 1) {
      const value = values[at]!;
      const x = axis.x(at, width);
      if (!(value >= convertPrecipitation(DRY)) || x < 0 || x > width) continue;
      const y = base - (value / span[1]) * (ROWS.lane - 10);
      context.beginPath();
      context.moveTo(x, base);
      context.lineTo(x, y);
      context.stroke();
      context.beginPath();
      context.arc(x, y, 2, 0, 2 * Math.PI);
      context.fill();
    }
    this.ends(context, 3, span[1].toFixed(1), "0", muted);
  }

  private ends(context: CanvasRenderingContext2D, lane: number, high: string, low: string, color: string): void {
    const { width } = this.pad;
    const paper = getComputedStyle(this.root).getPropertyValue("--sheet").trim() || "#fbf9f4";
    context.font = '10px "IBM Plex Mono", ui-monospace, monospace';
    context.textAlign = "right";
    for (const [label, y, baseline] of [[high, lane * ROWS.lane + 3, "top"], [low, (lane + 1) * ROWS.lane - 3, "bottom"]] as const) {
      context.textBaseline = baseline;
      context.lineWidth = 3;
      context.lineJoin = "round";
      context.strokeStyle = paper;
      context.strokeText(label, width - 2, y);
      context.fillStyle = color;
      context.fillText(label, width - 2, y);
    }
  }
}
