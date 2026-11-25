/**
 * The workbench's state: three coordinates (where, when, how high) that every
 * chart on the page shares, and how the charts are drawn. The address carries
 * all of it but the line switches, and is the page's only memory; parsing and
 * writing are inverses, and what does not parse counts as not there.
 *
 * A time is a valid time and never a lead: a link read after the next run has
 * replaced this one still means the same hour.
 */

import { distance } from "../measure";
import { FORECAST_MODELS, type ForecastModelId } from "../manifest";
import type { LonLat, SectionFill, SectionLine, SectionPoint } from "../crosssection/types";
import { parseSectionFromSearch, searchWithSection } from "../urlstate";

export const FILLS: readonly SectionFill[] = ["thetae", "tmp", "rh", "wind", "cross"];
export const TOPS: readonly number[] = [100, 500];
/** Hours the charts through time run. */
export const SPANS: readonly number[] = [120, 240];

export interface WorkbenchState {
  model: ForecastModelId;
  /** The line of the section, or null: the page is of one point. */
  line: SectionLine | null;
  /** The probe: the point, or the place on the line nearest it. Null is the
   * line's middle, which needs the plan to say where that is. */
  point: LonLat | null;
  /** Valid time, milliseconds, or null for now. */
  time: number | null;
  /** hPa. */
  level: number;
  fill: SectionFill;
  /** The charts' top, hPa. */
  top: number;
  span: number;
}

export const DEFAULT_MODEL: ForecastModelId = "gfs";
export const DEFAULT_POINT: LonLat = [139.69, 35.68];
const DEFAULT_LEVEL = 850;

/** Metres: a probe farther from the line than this is of another line. */
const OFF_LINE = 100_000;

export function defaultState(): WorkbenchState {
  return { model: DEFAULT_MODEL, line: null, point: DEFAULT_POINT, time: null, level: DEFAULT_LEVEL, fill: "thetae", top: 100, span: 120 };
}

function parsePoint(value: string | null): LonLat | null {
  const parts = value?.split(",") ?? [];
  if (parts.length !== 2 || !parts.every((part) => /^\s*-?\d+(\.\d+)?\s*$/.test(part))) return null;
  const [lat, lon] = parts.map(Number) as [number, number];
  if (Math.abs(lat) > 90 || Math.abs(lon) > 360) return null;
  return placed([lon, lat]);
}

/** A point as a link can carry it: hundredths of a degree, one turn. */
export function placed([lon, lat]: LonLat): LonLat {
  const round = (value: number): number => Math.round(value * 100) / 100 + 0;
  return [round(((((lon + 180) % 360) + 360) % 360) - 180), round(lat)];
}

function parseTime(value: string | null): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})Z$/.exec(value ?? "");
  if (!match) return null;
  const time = Date.parse(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:00:00Z`);
  // A date that rolls over (02-30) is not the one that was written.
  return Number.isFinite(time) && formatTime(time) === value ? time : null;
}

export function formatTime(time: number): string {
  return `${new Date(time).toISOString().slice(0, 13)}Z`;
}

/** The value of a parameter where it is one of some numbers. */
function oneOf(value: string | null, choices: readonly number[], fallback: number): number {
  return choices.find((choice) => String(choice) === value) ?? fallback;
}

export function parseState(search: string): WorkbenchState {
  const params = new URLSearchParams(search);
  const base = defaultState();
  const model = params.get("model");
  const line = parseSectionFromSearch(search);
  const point = parsePoint(params.get("point"));
  const level = params.get("p") ?? "";
  const fill = FILLS.find((id) => id === params.get("fill")) ?? base.fill;
  return {
    model: model !== null && Object.hasOwn(FORECAST_MODELS, model) ? (model as ForecastModelId) : base.model,
    line,
    point: point ?? (line ? null : base.point),
    time: parseTime(params.get("time")),
    level: /^\d{1,4}$/.test(level) && Number(level) > 0 ? Number(level) : base.level,
    // There is no line for the wind to be across.
    fill: fill === "cross" && !line ? "wind" : fill,
    top: oneOf(params.get("top"), TOPS, base.top),
    span: oneOf(params.get("span"), SPANS, base.span),
  };
}

/** A query string with the state's parameters set, whatever else it carries
 * kept and the defaults left out. */
export function searchForState(state: WorkbenchState, search: string): string {
  const base = defaultState();
  const params = new URLSearchParams(searchWithSection(search, state.line));
  const set = (name: string, value: string | null): void => {
    params.delete(name);
    if (value !== null) params.append(name, value);
  };
  set("model", state.model === base.model ? null : state.model);
  set("point", state.point && `${state.point[1].toFixed(2)},${state.point[0].toFixed(2)}`);
  set("time", state.time === null ? null : formatTime(state.time));
  set("p", state.level === base.level ? null : String(state.level));
  set("fill", state.fill === base.fill ? null : state.fill);
  set("top", state.top === base.top ? null : String(state.top));
  set("span", state.span === base.span ? null : String(state.span));
  // Coordinates are read by people; a comma needs no escape in a query.
  return `?${params.toString().replaceAll("%2C", ",")}`;
}

/** The point of a line's plan that a probe is at: the nearest, or the middle
 * one where the probe is on none. */
export function probeOnLine(points: readonly SectionPoint[], probe: LonLat | null): number {
  const middle = points.length >> 1;
  if (!probe) return middle;
  let best = middle;
  let nearest = OFF_LINE;
  for (const [index, { lon, lat }] of points.entries()) {
    const away = distance([lon, lat], probe);
    if (away >= nearest) continue;
    best = index;
    nearest = away;
  }
  return best;
}

/** The frame of an axis nearest a time, and whether the time is on the axis at
 * all. `offsets` are seconds from `runTime`. */
export function nearestFrame(offsets: readonly number[], runTime: number, time: number): { frame: number; outside: boolean } {
  const lead = (time - runTime) / 1000;
  let frame = 0;
  for (const [index, offset] of offsets.entries()) {
    if (Math.abs(offset - lead) < Math.abs(offsets[frame]! - lead)) frame = index;
  }
  return { frame, outside: offsets.length === 0 || lead < offsets[0]! || lead > offsets[offsets.length - 1]! };
}

/** The level of a store nearest a pressure, by ln p as the charts space them. */
export function nearestLevel(levels: readonly number[], pressure: number): number {
  return levels.reduce(
    (best, level) => (Math.abs(Math.log(level / pressure)) < Math.abs(Math.log(best / pressure)) ? level : best),
    levels[0] ?? pressure,
  );
}

/** The next level up (`direction` 1) or down (-1) that is on a chart with this
 * top, or null. The height may itself be above the chart. */
export function stepLevel(levels: readonly number[], from: number, direction: 1 | -1, top: number): number | null {
  const shown = levels.filter((level) => level >= top && (direction > 0 ? level < from : level > from));
  if (shown.length === 0) return null;
  return direction > 0 ? Math.max(...shown) : Math.min(...shown);
}
