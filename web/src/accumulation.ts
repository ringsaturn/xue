/**
 * Sliding-window precipitation totals (`?accum=`): the depth that fell over
 * the window ending at the playhead, computed on the frontend from the
 * published precipitation rate planes. No bundle carries a total; every
 * window is a weighted sum of rate frames the session already decodes.
 *
 * What a rate frame means decides how it is weighted. A rate the encoder
 * derived by de-accumulating or de-averaging (ECMWF, GFS sflux: the
 * parameter block carries `typeOfStatisticalProcessing`) is the mean over
 * the step ending at its frame, so the frame covers that whole step and the
 * window takes the part of it that falls inside. An instantaneous rate (GFS
 * pgrb2, the radar windows) is a sample, so the window integrates the
 * straight line between neighbouring frames (the trapezoid rule) and a gap
 * in an observation window much longer than its cadence is a hole, not a
 * line. Either way a window is a list of frames with a weight in hours, and
 * mm/h × h is the total in millimetres.
 *
 * A window the axis cannot fill (the first hours of a run, the start of a
 * radar window) is the part it can: `covered` says how much that is.
 */

import type { BundleVariable, LogQuantization } from "./manifest";
import { decodeValue, precipitationColor } from "./palettes";

/** The windows a forecast offers, in seconds. */
const FORECAST_WINDOWS = [3, 6, 12, 24, 48, 72].map((hours) => hours * 3600);

/** The windows an observation window (radar, nowcast) offers, in seconds:
 * those datasets span two or three hours at minutes of cadence. */
const OBSERVATION_WINDOWS = [600, 1800, 3600, 3 * 3600];

/** How the frames of a rate bundle are weighted (see the module comment). */
export type RateKind = "interval" | "instant";

/** How a precipitation rate bundle's frames are weighted, or null when the
 * variable is not one a total can be made of (a unit other than mm/h). */
export function rateKind(variable: BundleVariable): RateKind | null {
  if (variable.unit !== "mm/h") return null;
  return variable.parameter?.typeOfStatisticalProcessing === undefined ? "instant" : "interval";
}

/** The windows a dataset offers, in seconds, over its frames' valid times
 * (`leads`, seconds, ascending): its set's windows no shorter than its
 * shortest step, since a total over less than one step would be a rate
 * relabelled, and no longer than the span its frames cover. */
export function accumulationWindows(leads: readonly number[], kind: RateKind, observations: boolean): number[] {
  if (leads.length < 2) return [];
  let step = Infinity;
  for (let index = 1; index < leads.length; index += 1) step = Math.min(step, leads[index]! - leads[index - 1]!);
  // An interval frame covers the step before it, so the first frame's own
  // step (from the run's start) counts toward the span too.
  const first = kind === "interval" ? Math.max(0, leads[0]!) : 0;
  const span = leads[leads.length - 1]! - leads[0]! + first;
  return (observations ? OBSERVATION_WINDOWS : FORECAST_WINDOWS).filter((window) => window >= step && window <= span);
}

/** A window as the level row and the instrument line spell it: "3H", "30M". */
export function windowCode(seconds: number): string {
  return seconds % 3600 === 0 ? `${seconds / 3600}H` : `${Math.round(seconds / 60)}M`;
}

/** A window as `?accum=` spells it: "3h", "30m". */
export function windowParam(seconds: number): string {
  return windowCode(seconds).toLowerCase();
}

/** `?accum=`'s value as seconds, or null for anything that is not a whole
 * number of hours or minutes. Whether the dataset offers it is the shell's
 * question, not the parser's. */
export function parseWindow(value: string): number | null {
  const match = /^(\d{1,3})\s*(h|m|min)$/.exec(value.trim().toLowerCase());
  if (!match) return null;
  const amount = Number(match[1]);
  if (amount <= 0) return null;
  return match[2] === "h" ? amount * 3600 : amount * 60;
}

/** One frame of a window and its weight: hours of its rate in the total. */
export interface AccumulationTerm {
  index: number;
  hours: number;
}

export interface AccumulationPlan {
  terms: AccumulationTerm[];
  /** Seconds of the window the frames actually cover. */
  covered: number;
}

/** A gap between two observation frames longer than this many of the
 * window's usual steps is a hole: no line is drawn across it. */
const GAP_STEPS = 4;

/** The frames, and their weights, of the window of `window` seconds ending
 * at frame `index` of an axis whose frames are valid at `leads` (seconds
 * from the run's start, ascending). */
export function accumulationPlan(
  leads: readonly number[],
  index: number,
  window: number,
  kind: RateKind,
): AccumulationPlan {
  const end = leads[index]!;
  const start = end - window;
  const weights = new Map<number, number>();
  let covered = 0;
  const add = (frame: number, seconds: number) => {
    if (seconds > 0) weights.set(frame, (weights.get(frame) ?? 0) + seconds / 3600);
  };
  if (kind === "interval") {
    for (let frame = index; frame >= 0; frame -= 1) {
      const to = leads[frame]!;
      const from = frame > 0 ? leads[frame - 1]! : Math.min(0, to);
      if (to <= start) break;
      const seconds = to - Math.max(from, start);
      add(frame, seconds);
      covered += Math.max(0, seconds);
    }
  } else {
    const gap = GAP_STEPS * medianStep(leads);
    for (let frame = index; frame >= 1; frame -= 1) {
      const b = leads[frame]!;
      const a = leads[frame - 1]!;
      if (b <= start) break;
      if (b - a > gap) continue;
      // The straight line from a to b, integrated over [c, b]: weights on
      // the two ends that sum to the overlap.
      const c = Math.max(a, start);
      const length = b - c;
      const onB = (length * ((c + b) / 2 - a)) / (b - a);
      add(frame, onB);
      add(frame - 1, length - onB);
      covered += length;
    }
  }
  const terms = [...weights].map(([frame, hours]) => ({ index: frame, hours })).sort((x, y) => x.index - y.index);
  return { terms, covered };
}

function medianStep(leads: readonly number[]): number {
  const steps: number[] = [];
  for (let index = 1; index < leads.length; index += 1) steps.push(leads[index]! - leads[index - 1]!);
  if (steps.length === 0) return Infinity;
  steps.sort((a, b) => a - b);
  return steps[Math.floor(steps.length / 2)]!;
}

/** The window total at every frame of a rate series (the probe's): a number
 * where every frame of the window is read, null where none of them has a
 * value, undefined while any is still unread. */
export function seriesTotals(
  values: readonly (number | null | undefined)[],
  leads: readonly number[],
  window: number,
  kind: RateKind,
): (number | null | undefined)[] {
  return values.map((_, index) => {
    let total = 0;
    let read = false;
    for (const term of accumulationPlan(leads, index, window, kind).terms) {
      const value = values[term.index];
      if (value === undefined) return undefined;
      if (value === null) continue;
      total += value * term.hours;
      read = true;
    }
    return read ? total : null;
  });
}

/** The codebook a total plane is written in: 0.1 mm to 1 m on a log1p grid,
 * the same shape the rate's own codebook has. Below the trace is dry (code
 * 0); 255 is no data, as in every scalar palette. */
export const ACCUMULATION_QUANTIZATION: LogQuantization = {
  type: "log1p",
  trace: 0.1,
  scale: 1,
  maximum: 1000,
  minimumCode: 1,
  maximumCode: 253,
  zeroCode: 0,
  overflowCode: 254,
  nodataCode: 255,
};

/** The rate variable restated as its total: millimetres, in the total
 * codebook — what the legend, the probe and the chart read it as. */
export function accumulationVariable(rate: BundleVariable): BundleVariable {
  return { ...rate, unit: "mm", quantization: ACCUMULATION_QUANTIZATION };
}

/** A total in millimetres as its code. */
export function encodeTotal(total: number): number {
  const q = ACCUMULATION_QUANTIZATION;
  if (!(total >= q.trace)) return q.zeroCode;
  const lo = Math.log1p(q.trace / q.scale);
  const hi = Math.log1p(q.maximum / q.scale);
  const unit = (Math.log1p(total / q.scale) - lo) / (hi - lo);
  return Math.min(q.overflowCode, Math.max(q.minimumCode, Math.round(q.minimumCode + unit * (q.maximumCode - q.minimumCode))));
}

/** The bands of the totals' key, in millimetres: their lower bounds. A
 * banded key is how totals are read — "over 50 mm" — and one key serves
 * every window, the short radar ones at its foot. */
export const ACCUMULATION_BOUNDS: readonly number[] = [0.1, 1, 2, 5, 10, 25, 50, 100, 200];

/** The rate ramp sampled for a band: a total of `mm` takes the colour of a
 * rate of half that, so the key's top band (200 mm and over) lands where
 * the rate ramp ends and its foot is the ramp's faint drizzle. */
const RAMP_PER_MM = 0.5;

function bandColor(band: number): [number, number, number, number] {
  const from = ACCUMULATION_BOUNDS[band]!;
  const to = ACCUMULATION_BOUNDS[band + 1] ?? from * 2;
  return precipitationColor(Math.sqrt(from * to) * RAMP_PER_MM);
}

function bandOf(total: number): number {
  let band = -1;
  for (const from of ACCUMULATION_BOUNDS) {
    if (total >= from) band += 1;
    else break;
  }
  return band;
}

/** The 256×1 RGBA palette over the total codebook: each code its band's
 * colour; dry, no data and the codes under the first band stay clear. */
export function accumulationPalette(): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  const variable = { quantization: ACCUMULATION_QUANTIZATION } as BundleVariable;
  for (let code = 0; code < 256; code += 1) {
    const total = decodeValue(variable, code);
    if (total === null || code === ACCUMULATION_QUANTIZATION.zeroCode) continue;
    const band = bandOf(total);
    if (band >= 0) palette.set(bandColor(band), code * 4);
  }
  return palette;
}

/** The key: one equal band per step, the heaviest at the top, each labelled
 * with its lower bound. */
export function accumulationLegend(): { gradient: string; labels: string[] } {
  const bands = ACCUMULATION_BOUNDS.length;
  const stops: string[] = [];
  for (let at = 0; at < bands; at += 1) {
    const [r, g, b, a] = bandColor(bands - 1 - at);
    const from = ((at / bands) * 100).toFixed(2);
    const to = (((at + 1) / bands) * 100).toFixed(2);
    stops.push(`rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)}) ${from}% ${to}%`);
  }
  return {
    gradient: `linear-gradient(to bottom, ${stops.join(", ")})`,
    labels: ACCUMULATION_BOUNDS.map(String).reverse(),
  };
}

/** One frame of a window as the plane sum takes it: its decoded plane and
 * weight, and the key the plane is cached under. */
export interface TermPlane {
  key: string;
  plane: Uint8Array;
  hours: number;
}

/** A rate codebook read once: mm/h per code (0 where no data) and whether
 * the code is a reading at all. */
interface RateTable {
  rate: Float64Array;
  valid: Uint8Array;
}

function rateTable(variable: BundleVariable): RateTable {
  const rate = new Float64Array(256);
  const valid = new Uint8Array(256);
  for (let code = 0; code < 256; code += 1) {
    const value = decodeValue(variable, code);
    if (value === null || !Number.isFinite(value)) continue;
    rate[code] = Math.max(0, value);
    valid[code] = 1;
  }
  return { rate, valid };
}

/** Rebuild from scratch after this many incremental updates, so the
 * rounding of adding and taking back the same terms never accumulates. */
const REBUILD_EVERY = 64;

/**
 * The running sum behind the total planes. Stepping the playhead one frame
 * changes the weight of two or three frames of the window, not all of
 * them, so an update applies only the change in each frame's weight to the
 * sum it holds — a 72-hour window plays at the cost of a 3-hour one. It
 * starts over whenever that would not be exact: another plane under a key
 * it summed (the view moved and the frame was decoded again), a plane given
 * back to its worker, another grid, or a change touching most of the terms.
 */
class PlaneSum {
  private sum = new Float64Array(0);
  private valid = new Uint16Array(0);
  private terms = new Map<string, TermPlane>();
  private updates = 0;

  constructor(private readonly table: RateTable) {}

  update(next: readonly TermPlane[], cells: number): void {
    if (cells !== this.sum.length || this.updates >= REBUILD_EVERY) {
      this.rebuild(next, cells);
      return;
    }
    const nextByKey = new Map(next.map((term) => [term.key, term]));
    const changes: [TermPlane, number, number][] = [];
    for (const [key, held] of this.terms) {
      const incoming = nextByKey.get(key);
      if (incoming && incoming.plane !== held.plane) {
        this.rebuild(next, cells);
        return;
      }
      if (!incoming) changes.push([held, -held.hours, -1]);
      else if (incoming.hours !== held.hours) changes.push([held, incoming.hours - held.hours, 0]);
    }
    for (const term of next) if (!this.terms.has(term.key)) changes.push([term, term.hours, 1]);
    if (changes.length * 2 > Math.max(next.length, 1) || changes.some(([term]) => term.plane.length !== cells)) {
      this.rebuild(next, cells);
      return;
    }
    for (const [term, hours, count] of changes) this.apply(term.plane, hours, count);
    this.terms = nextByKey;
    this.updates += 1;
  }

  private rebuild(next: readonly TermPlane[], cells: number): void {
    if (this.sum.length !== cells) {
      this.sum = new Float64Array(cells);
      this.valid = new Uint16Array(cells);
    } else {
      this.sum.fill(0);
      this.valid.fill(0);
    }
    this.terms = new Map();
    for (const term of next) {
      if (term.plane.length !== cells) continue;
      this.apply(term.plane, term.hours, 1);
      this.terms.set(term.key, term);
    }
    this.updates = 0;
  }

  /** Add `hours` of a plane's rate to the sum, and `count` to the number of
   * frames reading at each cell (+1 entering the window, -1 leaving it). */
  private apply(plane: Uint8Array, hours: number, count: number): void {
    const { rate, valid } = this.table;
    const sum = this.sum;
    const readings = this.valid;
    for (let cell = 0; cell < plane.length; cell += 1) {
      const code = plane[cell]!;
      sum[cell] = sum[cell]! + rate[code]! * hours;
      if (count !== 0) readings[cell] = readings[cell]! + count * valid[code]!;
    }
  }

  encode(): Uint8Array {
    const out = new Uint8Array(this.sum.length);
    const q = ACCUMULATION_QUANTIZATION;
    const lo = Math.log1p(q.trace / q.scale);
    const span = Math.log1p(q.maximum / q.scale) - lo;
    const codes = q.maximumCode - q.minimumCode;
    for (let cell = 0; cell < out.length; cell += 1) {
      if (this.valid[cell] === 0) {
        out[cell] = q.nodataCode;
        continue;
      }
      const total = this.sum[cell]!;
      if (!(total >= q.trace)) continue;
      const code = Math.round(q.minimumCode + ((Math.log1p(total / q.scale) - lo) / span) * codes);
      out[cell] = code > q.overflowCode ? q.overflowCode : code;
    }
    return out;
  }
}

/** How many total planes are kept: the blend's two and the step onto the
 * next pair. */
const HELD_PLANES = 3;

/**
 * The total planes of one rate session: the plane for a frame given the
 * window's terms, kept while those terms are the very planes it was made
 * from, so a blend sweep asking for the same two frames on every animation
 * frame sums nothing twice.
 */
export class WindowTotals {
  private readonly sum: PlaneSum;
  private readonly held = new Map<string, { terms: readonly TermPlane[]; plane: Uint8Array }>();

  constructor(variable: BundleVariable) {
    this.sum = new PlaneSum(rateTable(variable));
  }

  plane(id: string, terms: readonly TermPlane[], cells: number): Uint8Array {
    const held = this.held.get(id);
    if (held && sameTerms(held.terms, terms)) {
      this.held.delete(id);
      this.held.set(id, held);
      return held.plane;
    }
    this.sum.update(terms, cells);
    const plane = this.sum.encode();
    this.held.delete(id);
    this.held.set(id, { terms, plane });
    while (this.held.size > HELD_PLANES) {
      const oldest = this.held.keys().next().value;
      if (oldest === undefined) break;
      this.held.delete(oldest);
    }
    return plane;
  }
}

function sameTerms(a: readonly TermPlane[], b: readonly TermPlane[]): boolean {
  return a.length === b.length && a.every((term, at) => term.plane === b[at]!.plane && term.hours === b[at]!.hours && term.key === b[at]!.key);
}
