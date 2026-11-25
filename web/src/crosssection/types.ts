/**
 * What the parts of the vertical cross-section hand one another: the line
 * and its sampling plan (`line.ts`), the column store as a section reads it
 * (`source.ts`), and the state a frame is drawn from (`renderer.ts`).
 */

import type { GridMap } from "../gridmap";
import type { ColumnQuantity } from "../identity";
import type { BundleVariable } from "../manifest";

/** A longitude/latitude pair, degrees. */
export type LonLat = [number, number];

/** The line of a section: a great circle, `a` at the left of the chart. */
export interface SectionLine {
  a: LonLat;
  b: LonLat;
}

/** One sample along the line. */
export interface SectionPoint {
  lon: number;
  lat: number;
  /** Metres from `a` along the line. */
  along: number;
  /** Which way the line runs here, radians clockwise from north. */
  bearing: number;
}

/** The cells a section is sampled from: every point is a weighted sum of
 * four of them. */
export interface SectionPlan {
  line: SectionLine;
  /** Metres. */
  length: number;
  points: SectionPoint[];
  /** The distinct cells read, `[row, column, row, column, ...]`. */
  cells: Uint32Array;
  /** Four indexes into the cells per point. */
  corners: Uint32Array;
  /** The four corners' weights per point, which sum to one. */
  weights: Float32Array;
  /** How many of the store's blocks the cells lie in. */
  blocks: number;
}

/** One array of the store: a quantity on every level, or a surface field. */
export interface SectionArray {
  id: string;
  /** What it is of, by its members' parameters; null for what a section has
   * no use for. */
  quantity: ColumnQuantity | null;
  /** hPa, descending; null on a surface field, which has one member. */
  levels: number[] | null;
  /** Lead time of each of its frames, seconds. */
  offsets: number[];
  /** One variable per level, each with its own codebook. */
  members: BundleVariable[];
}

/** The open column store. */
export interface SectionStore {
  model: string;
  /** Milliseconds since the epoch. */
  runTime: number;
  grid: GridMap;
  width: number;
  height: number;
  /** The store's block, cells: what a plan counts blocks by. */
  blockHeight: number;
  blockWidth: number;
  arrays: Map<string, SectionArray>;
}

/** Why there is no section to draw. `model`: the dataset on screen has no
 * column store; `run`: this run has none (yet); `origin`: the data's origin
 * answers no ranges; `outside`: the line lies off the store's grid; `failed`:
 * the store did not open. */
export type SectionUnavailable = "model" | "run" | "origin" | "outside" | "failed";

/** What fills the section. */
export type SectionFill = "thetae" | "tmp" | "rh" | "wind" | "cross";

/** What a frame is drawn from. */
export interface SectionDrawState {
  /** The playhead, seconds of lead time from the store's run. */
  lead: number;
  fill: SectionFill;
  lines: { wind: boolean; zero: boolean; theta: boolean };
  barbs: boolean;
  /** The chart's top, hPa: 100 or 500. */
  top: number;
  /** The surfaces the map is on, drawn as lines across the section. */
  mapLevels: { fill: number | null; lines: number | null };
  dark: boolean;
  units: SectionDrawUnits;
}

/** The reader's units, as what is drawn in them needs to know them. */
export interface SectionDrawUnits {
  /** Isotachs, in the wind unit: what 1 m/s is in it, the interval, the
   * lowest drawn, and the speed from which they are drawn heavier. */
  wind: { scale: number; interval: number; first: number; heavy: number };
  /** The freezing point, written out. */
  freezing: string;
  /** Metres between the heights the right axis may label. */
  heightStep: number;
}

/** What has been drawn, for the axes and the notices around the chart. */
export interface SectionDrawn {
  /** The share of the section's samples that have arrived, 0 to 1. */
  complete: number;
  /** The store has no frame at the playhead. */
  noFrame: boolean;
  /** Pressures of the multiples of `heightStep`, averaged along the line, for the right axis. */
  heights: { metres: number; pressure: number }[];
}

/** A reading at one point of the chart. */
export interface SectionReading {
  point: number;
  /** hPa. */
  pressure: number;
  /** Below the model's ground: only `surfacePressure` is meaningful. */
  belowGround: boolean;
  surfacePressure: number;
  /** Metres. */
  height: number;
  /** °C, %, m/s, degrees the wind blows from, K, K. */
  temperature: number;
  humidity: number;
  windSpeed: number;
  windDirection: number;
  theta: number;
  thetaE: number;
}
