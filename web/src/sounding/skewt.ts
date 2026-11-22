/**
 * The skew-T log-p chart: one ascent drawn the way a forecaster reads it.
 *
 * Two conventions make the diagram, and both are in `toXY`. Pressure is the
 * vertical axis, linear in its logarithm, so equal fractions of the
 * atmosphere's mass take equal room and the stratosphere does not swallow the
 * page. Temperature is the horizontal axis, *skewed*: an isotherm leans to
 * the right as it rises, by default at 45°, which turns the two families a
 * reader compares — the dry adiabat and the isotherm — from nearly parallel
 * lines into lines that cross at a wide angle. Everything else on the chart
 * is a curve in that projection: the dry adiabats bend left, the
 * pseudoadiabats bend left harder and then straighten as they run out of
 * vapour, and the saturation mixing-ratio lines lean right a little more than
 * the isotherms.
 *
 * The module draws and nothing else. It takes a 2D context, a rectangle, an
 * ink and the profiles; it never reads the DOM, never asks what the theme is,
 * never touches `devicePixelRatio` — the caller scales the context and passes
 * the two or three colors it wants, exactly as `meteogram.ts` is called, so
 * the same code paints the light panel, the dark panel and a test's stub
 * context. Every length is in the rectangle's own pixels and scales with it.
 *
 * The chart also carries what a reader who is not a forecaster needs in order
 * to read it: each trace named where it ends, the height in kilometres beside
 * the pressure, the layers where the air is near saturation, the inversions,
 * and the areas between the lifted parcel and its surroundings. Which layers,
 * which parcel and every word of it are the caller's — `analysis.ts` finds
 * them and the panel formats them — so the module still only draws.
 *
 * The pointer's crosshair is a second, cheap pass (`drawSkewTCursor`) over a
 * finished chart, so a caller can keep the chart's pixels and repaint only
 * the crosshair as the pointer moves.
 */

import type { ParcelPath, Profile } from "./profile";
import {
  MOIST_STEP_HPA,
  SIG,
  dryAdiabat,
  hasSig,
  interpolateAtPressure,
  mixingRatioLine,
  moistAdiabatFrom,
} from "./profile";

/** A rectangle in canvas pixels. */
interface SkewTRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * Where the chart is and what it spans. `x`/`y`/`width`/`height` are the plot
 * rectangle — the box the isobars and isotherms fill — and `barbWidth` is the
 * column reserved to its right for the wind, so `toXY` never has to know
 * about the barbs and the barbs never land on the profile.
 */
export interface SkewTLayout extends SkewTRect {
  /** Pressure at the bottom edge, hPa. */
  pBottom: number;
  /** Pressure at the top edge, hPa. */
  pTop: number;
  /** Temperature at the bottom-left corner, °C. */
  tMin: number;
  /** Temperature at the bottom-right corner, °C. */
  tMax: number;
  /** The skew, as pixels of rightward shift per pixel of rise. 1 is the
   * classical 45°; 0 would be an emagram. */
  skew: number;
  /** Pixels reserved to the right of the rectangle for the wind barbs. */
  barbWidth: number;
}

interface SkewTLayoutOptions {
  /** 100 hPa by default; 50 for an ascent worth following into the
   * stratosphere. */
  pressureTop?: number;
  pressureBottom?: number;
  tMin?: number;
  tMax?: number;
  skew?: number;
  barbWidth?: number;
}

/** The defaults: 1050 → 100 hPa, −40 … +45 °C along the bottom, 45° of skew.
 * The temperature range is the bottom edge's, not the whole chart's — the
 * skew carries the top of the box far to the left of −40 °C, which is what
 * makes a −60 °C tropopause visible on a scale that also holds a +40 °C
 * surface. */
export function skewTLayout(rect: SkewTRect, options: SkewTLayoutOptions = {}): SkewTLayout {
  return {
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
    pBottom: options.pressureBottom ?? 1050,
    pTop: options.pressureTop ?? 100,
    tMin: options.tMin ?? -40,
    tMax: options.tMax ?? 45,
    skew: options.skew ?? 1,
    barbWidth: options.barbWidth ?? 34,
  };
}

/** The warmest temperature the top edge has to hold, °C: a polar summer's
 * stratosphere at 100 hPa, which is the warmest air a chart's top ever
 * shows. */
const WARMEST_AT_TOP = -35;

/**
 * The skew a box of this shape can afford.
 *
 * A skew of 1 leans the isotherms at 45°, and carries the top edge to the
 * cold side by the box's own height. In a box wider than it is tall that is
 * what makes the diagram; in one taller than it is wide it carries the top
 * edge past the air it has to show, and the temperature trace leaves through
 * the right-hand side long before the tropopause. So the skew is 1 where
 * the box allows it and otherwise as much as keeps `WARMEST_AT_TOP` on the
 * top edge.
 */
export function fittedSkew(width: number, height: number, tMin: number, tMax: number): number {
  if (!(width > 0) || !(height > 0) || !(tMax > tMin)) return 1;
  const degreesPerPixel = (tMax - tMin) / width;
  return Math.max(0, Math.min(1, (tMax - WARMEST_AT_TOP) / (height * degreesPerPixel)));
}

/** The height fraction of a pressure: 0 at the bottom edge, 1 at the top. */
function depth(layout: SkewTLayout, p: number): number {
  return (Math.log(layout.pBottom) - Math.log(p)) / (Math.log(layout.pBottom) - Math.log(layout.pTop));
}

/** Project a (pressure, temperature) pair onto the canvas. */
export function toXY(layout: SkewTLayout, p: number, t: number): { x: number; y: number } {
  const up = depth(layout, p);
  return {
    x: layout.x + ((t - layout.tMin) / (layout.tMax - layout.tMin)) * layout.width + layout.skew * layout.height * up,
    y: layout.y + layout.height * (1 - up),
  };
}

/** The inverse: what a point on the canvas reads as. Valid outside the
 * rectangle too — the caller decides whether a pointer is in the chart. */
export function fromXY(layout: SkewTLayout, x: number, y: number): { p: number; t: number } {
  const up = 1 - (y - layout.y) / layout.height;
  const p = Math.exp(Math.log(layout.pBottom) - up * (Math.log(layout.pBottom) - Math.log(layout.pTop)));
  const t =
    layout.tMin +
    ((x - layout.x - layout.skew * layout.height * up) / layout.width) * (layout.tMax - layout.tMin);
  return { p, t };
}

/** Whether a canvas point is inside the plot rectangle. */
export function insideChart(layout: SkewTLayout, x: number, y: number): boolean {
  return x >= layout.x && x <= layout.x + layout.width && y >= layout.y && y <= layout.y + layout.height;
}

/** The isobars that carry a label — the mandatory levels a reader looks for,
 * and nothing between them. */
const LABELLED_ISOBARS: readonly number[] = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 70, 50];

/** Isotherms every 10 °C, the 0 °C line emphasised. */
const ISOTHERM_STEP = 10;
/** Dry adiabats every 20 K of potential temperature: with the isotherms
 * every 10 °C they are the second grid on the page, and at the isotherms'
 * own density the two together are a hatching the profiles get lost in. */
const DRY_ADIABAT_STEP = 20;
/** The pseudoadiabats drawn, by wet-bulb potential temperature at 1000 hPa
 * (°C). A few, wide apart: they are orientation, not a reading. */
const MOIST_ADIABATS: readonly number[] = [-8, 0, 8, 16, 24, 32];
/** The saturation mixing-ratio lines drawn, g/kg. */
const MIXING_RATIO_LINES: readonly number[] = [1, 2, 4, 8, 16];
/** Isohumes are only drawn in the lower troposphere; above it they crowd into
 * the left margin and mean nothing. */
const MIXING_RATIO_TOP_HPA = 600;
/** Background curves are sampled every this many hectopascals; the
 * pseudoadiabats integrate `MOIST_STEP_HPA` inside each sample. */
const CURVE_SAMPLE_HPA = 25;

/**
 * The chart's colors. Nothing here is read from a stylesheet: the panel
 * resolves its theme once and hands the ink down, the way the meteogram's is
 * handed down, so the same layout can be painted light and dark side by side.
 */
export interface SkewTInk {
  /** The observed profile, the frame, the labels that carry a reading. */
  ink: string;
  /** Axis labels and the dew-point trace's companion text. */
  inkMuted: string;
  /** Isobars and isotherms. */
  grid: string;
  /** The 0 °C isotherm, the frame, an emphasised isobar. */
  gridStrong: string;
  /** The chart's ground. */
  paper: string;
  /** A second, model profile: its temperature. */
  model: string;
  /** The lifted parcel. */
  parcel: string;
  /** The observed temperature trace, when it has a hue of its own; the
   * plain ink otherwise. Hue tells the quantities apart (temperature warm,
   * dew point cool) and dash tells the sources apart (the sonde solid, the
   * model dotted), so a reader has two cues and a colour-blind one still
   * has the second. */
  temperature?: string;
  /** The observed dew-point trace. */
  dew?: string;
  /** The model's dew-point trace. */
  modelDew?: string;
  /** The background families, each in a hue of its own so a reader can tell
   * which line to follow: the dry adiabats, the pseudoadiabats and the
   * saturation mixing-ratio lines. Without them all three are `grid` and
   * told apart by dash and weight alone. */
  dryAdiabat?: string;
  moistAdiabat?: string;
  mixingRatio?: string;
  /** The crosshair's value tags: their ground and their text. */
  tag?: string;
  tagText?: string;
  /** An opaque ground the chart's own lettering is haloed in, so it reads
   * over the lines under it. */
  halo?: string;
  /** Where the lifted parcel is warmer than the air around it (the area is
   * CAPE), and where it is colder below its level of free convection (CIN). */
  buoyant?: string;
  inhibited?: string;
  /** A layer near saturation: the bar on the left edge and the wash between
   * the two traces. */
  moistLayer?: string;
  /** The underlay of a stretch of the temperature trace that warms upward. */
  inversion?: string;
}

/** A layer between two pressures, hPa. */
export interface SkewTLayer {
  pBottom: number;
  pTop: number;
}

/** A level worth naming, and what to call it. The caller picks the levels
 * and writes the words, in the viewer's language and units. */
export interface SkewTMark {
  p: number;
  label: string;
}

/** What a lifted parcel is compared with: the environment's temperature at
 * each of the parcel path's own levels, and the two levels the positive area
 * lies between (`analysis.ts` computes all three). */
export interface SkewTBuoyancy {
  environment: Float64Array;
  /** hPa, or null for a parcel that is never warmer than its surroundings. */
  lfc: number | null;
  el: number | null;
}

/** The chart's own lettering. Every string is the caller's; one left out is
 * not drawn. */
export interface SkewTLabels {
  /** Written where the observed temperature trace ends. */
  temperature?: string;
  /** Written where the observed dew-point trace ends. */
  dew?: string;
  /** Written where the lifted parcel's path ends. */
  parcel?: string;
  /** The pressure axis's unit, over its labels. */
  pressureUnit?: string;
  /** The temperature axis's unit, after its labels. */
  temperatureUnit?: string;
  /** The isohumes' unit, after the last of their values. */
  mixingRatioUnit?: string;
}

interface SkewTOptions {
  /** The observed ascent: `t` solid, `td` dashed. */
  profile?: Profile | null;
  /** A second profile — a model column at the same point — dotted, in the
   * model's own hues: dash says which source, hue says which quantity. */
  model?: Profile | null;
  /** A lifted parcel, dashed in its own hue. */
  parcel?: ParcelPath | null;
  /** The areas between that parcel and the environment, shaded. */
  buoyancy?: SkewTBuoyancy | null;
  /** Layers near saturation, marked on the left edge and between the traces. */
  moistLayers?: readonly SkewTLayer[];
  /** Layers where the temperature rises with height, underlaid on its trace. */
  inversions?: readonly SkewTLayer[];
  /** Named levels — the freezing level, a condensation level, the
   * tropopause — as ticks on the left edge. */
  marks?: readonly SkewTMark[];
  /** Heights on the right edge: the pressure each falls at
   * (`pressureAtHeight`) and its label. */
  heights?: readonly SkewTMark[];
  labels?: SkewTLabels;
  /** The font for every label; the caller's instrument face. */
  font?: string;
  /** Draw the wind column (default true when the profile carries wind). */
  barbs?: boolean;
}

/** Sample a background curve over the chart's pressure range. */
function sampleCurve(layout: SkewTLayout, at: (p: number) => number): { p: number; t: number }[] {
  const samples: { p: number; t: number }[] = [];
  for (let p = layout.pBottom; p > layout.pTop - CURVE_SAMPLE_HPA; p -= CURVE_SAMPLE_HPA) {
    const level = Math.max(p, layout.pTop);
    samples.push({ p: level, t: at(level) });
    if (level === layout.pTop) break;
  }
  return samples;
}

/** One pseudoadiabat's samples. The integration is carried from sample to
 * sample rather than restarted at each, so the whole curve costs the same
 * `MOIST_STEP_HPA` walk once. */
function sampleMoistAdiabat(layout: SkewTLayout, thetaW: number): { p: number; t: number }[] {
  const samples: { p: number; t: number }[] = [];
  let p = layout.pBottom;
  let t = moistAdiabatFrom(1000, thetaW, p);
  samples.push({ p, t });
  while (p > layout.pTop) {
    const next = Math.max(layout.pTop, p - CURVE_SAMPLE_HPA);
    t = moistAdiabatFrom(p, t, next);
    p = next;
    samples.push({ p, t });
  }
  return samples;
}

function strokeSamples(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  samples: readonly { p: number; t: number }[],
): void {
  context.beginPath();
  let pen = false;
  for (const sample of samples) {
    if (!Number.isFinite(sample.t)) {
      pen = false;
      continue;
    }
    const point = toXY(layout, sample.p, sample.t);
    if (pen) context.lineTo(point.x, point.y);
    else context.moveTo(point.x, point.y);
    pen = true;
  }
  context.stroke();
}

/** Two levels that report a field, with silent levels between them, are a
 * hole in the record once they are further apart than this pressure ratio.
 * An ascent's levels are a few percent apart, so a quarter of the pressure
 * with nothing said is a sensor that stopped, not the ascent's spacing. */
const TRACE_GAP_RATIO = 1.25;

/**
 * One profile field as a trace through the levels that report it.
 *
 * A level that does not report the field is not a break in it: many centres
 * file significant wind levels with no temperature between the temperature
 * levels, and lifting the pen at each would leave a dashed line where the
 * sonde measured a continuous one. The pen is lifted only over a real hole —
 * silent levels across more than `TRACE_GAP_RATIO` — and where the trace
 * leaves the chart's pressure range.
 */
function strokeProfile(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  profile: Profile,
  field: Float64Array,
): void {
  context.beginPath();
  /** The pressure of the last point drawn, or NaN with the pen up. */
  let last = Number.NaN;
  let silent = false;
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const value = field[index]!;
    if (!Number.isFinite(p)) continue;
    if (!Number.isFinite(value)) {
      silent = true;
      continue;
    }
    if (p > layout.pBottom || p < layout.pTop) {
      last = Number.NaN;
      silent = false;
      continue;
    }
    const point = toXY(layout, p, value);
    if (Number.isFinite(last) && !(silent && last / p > TRACE_GAP_RATIO)) context.lineTo(point.x, point.y);
    else context.moveTo(point.x, point.y);
    last = p;
    silent = false;
  }
  context.stroke();
}

/** The isotherms that reach the plot rectangle. The skew carries the top edge
 * far to the left of `tMin`, so the set runs well below it: the leftmost
 * isotherm to touch the box is the one at the top-left corner. */
function isothermRange(layout: SkewTLayout): { first: number; last: number } {
  const perPixel = (layout.tMax - layout.tMin) / layout.width;
  const shift = layout.skew * layout.height * perPixel;
  const first = Math.floor((layout.tMin - shift) / ISOTHERM_STEP) * ISOTHERM_STEP;
  const last = Math.ceil(layout.tMax / ISOTHERM_STEP) * ISOTHERM_STEP;
  return { first, last };
}

/**
 * The levels the wind column draws a barb at.
 *
 * A properly flagged TEMP already says which levels matter — the surface, the
 * mandatory levels, the tropopause and the maximum wind — and drawing those
 * is both the convention and a tenth of the levels of a 200-level ascent. An
 * ascent whose `sig` is missing or unflagged (a model column, a centre that
 * files none) has no such set, so the column falls back to every k-th level
 * with a wind, k chosen to land near `limit` barbs.
 */
export function barbLevels(profile: Profile, limit = 30): number[] {
  const wind = (index: number): boolean =>
    Number.isFinite(profile.ws[index]!) && Number.isFinite(profile.wd[index]!);
  const flagged: number[] = [];
  const available: number[] = [];
  const wanted = SIG.surface | SIG.standard | SIG.tropopause | SIG.maxWind;
  for (let index = 0; index < profile.n; index += 1) {
    if (!wind(index)) continue;
    available.push(index);
    if (hasSig(profile.sig[index]!, wanted)) flagged.push(index);
  }
  if (flagged.length > 0) return flagged;
  if (available.length <= limit) return available;
  const stride = Math.ceil(available.length / limit);
  return available.filter((_, position) => position % stride === 0);
}

/** Knots, rounded to the nearest five, which is the resolution a barb can
 * say. */
export function knots(metresPerSecond: number): number {
  return Math.round((metresPerSecond * 1.9438444924406046) / 5) * 5;
}

/** The least vertical room between two barbs, in pixels: closer than this and
 * the column is a smear. */
const BARB_SPACING = 13;
/** The shaft, and the barbs on it. */
const BARB_SHAFT = 14;
const BARB_LENGTH = 7;
const BARB_STEP = 3.4;

/**
 * One WMO wind barb at a point: the shaft points *into* the wind — from the
 * level toward where the air is coming from — and carries a pennant per
 * 50 kt, a full barb per 10 and a half barb per 5, counted from the far end
 * inward. Barbs lie to one side of the shaft, the northern hemisphere's: a
 * westerly (shaft to the left) carries its barbs upward.
 */
function drawBarb(context: CanvasRenderingContext2D, cx: number, cy: number, wd: number, ws: number): void {
  const speed = knots(ws);
  if (speed < 5) {
    // Calm: the open circle the station model uses, and no shaft to read a
    // direction off that the sonde did not measure.
    context.beginPath();
    context.arc(cx, cy, 2.5, 0, Math.PI * 2);
    context.stroke();
    return;
  }
  const angle = (wd * Math.PI) / 180;
  // The direction the wind comes from, on a canvas whose y axis points down.
  const dx = Math.sin(angle);
  const dy = -Math.cos(angle);
  // A quarter turn from it, the side the barbs lie on.
  const px = -dy;
  const py = dx;
  context.beginPath();
  context.moveTo(cx, cy);
  context.lineTo(cx + dx * BARB_SHAFT, cy + dy * BARB_SHAFT);
  context.stroke();

  let remaining = speed;
  let along = BARB_SHAFT;
  while (remaining >= 50) {
    const baseX = cx + dx * along;
    const baseY = cy + dy * along;
    const innerX = cx + dx * (along - BARB_STEP * 1.6);
    const innerY = cy + dy * (along - BARB_STEP * 1.6);
    context.beginPath();
    context.moveTo(baseX, baseY);
    context.lineTo(baseX + px * BARB_LENGTH, baseY + py * BARB_LENGTH);
    context.lineTo(innerX, innerY);
    context.closePath();
    context.fill();
    along -= BARB_STEP * 2;
    remaining -= 50;
  }
  while (remaining >= 10) {
    const baseX = cx + dx * along;
    const baseY = cy + dy * along;
    context.beginPath();
    context.moveTo(baseX, baseY);
    context.lineTo(baseX + px * BARB_LENGTH - dx * 2, baseY + py * BARB_LENGTH - dy * 2);
    context.stroke();
    along -= BARB_STEP;
    remaining -= 10;
  }
  if (remaining >= 5) {
    // A half barb never sits at the very end of a bare shaft, where it reads
    // as a full one: it is set in by one step.
    if (along === BARB_SHAFT) along -= BARB_STEP;
    const baseX = cx + dx * along;
    const baseY = cy + dy * along;
    context.beginPath();
    context.moveTo(baseX, baseY);
    context.lineTo(baseX + (px * BARB_LENGTH) / 2 - dx, baseY + (py * BARB_LENGTH) / 2 - dy);
    context.stroke();
  }
}

/** The pressure a geopotential height falls at, read off a profile's own
 * `z` — how a derived height (the freezing level, the tropopause) finds its
 * place on a pressure axis. `NaN` when the profile's heights do not reach it. */
export function pressureAtHeight(profile: Profile, z: number): number {
  if (!Number.isFinite(z)) return Number.NaN;
  let lower = -1;
  for (let index = 0; index < profile.n; index += 1) {
    if (!Number.isFinite(profile.p[index]!) || !Number.isFinite(profile.z[index]!)) continue;
    if (profile.z[index]! <= z) {
      lower = index;
      continue;
    }
    if (lower < 0) return Number.NaN;
    const z0 = profile.z[lower]!;
    const weight = (z - z0) / (profile.z[index]! - z0);
    // Linear in ln p between the bracketing levels, as every other
    // interpolation on this chart is.
    return Math.exp(Math.log(profile.p[lower]!) + weight * (Math.log(profile.p[index]!) - Math.log(profile.p[lower]!)));
  }
  return Number.NaN;
}

/** Lettering over the chart's lines: haloed in the ground's own color where
 * the ink has one, so a label crossing a trace stays a label. The fill style
 * is the caller's. */
function haloText(context: CanvasRenderingContext2D, ink: SkewTInk, text: string, x: number, y: number): void {
  if (ink.halo) {
    context.strokeStyle = ink.halo;
    context.lineWidth = 3;
    context.lineJoin = "round";
    context.strokeText(text, x, y);
  }
  context.fillText(text, x, y);
}

/** A font's pixel size, read back out of the shorthand the caller passed. */
function fontSize(font: string): number {
  return Number.parseFloat(/(\d+(?:\.\d+)?)px/.exec(font)?.[1] ?? "10");
}

/** One field of a profile between two pressures: the reporting levels inside
 * the layer, with both ends interpolated onto its edges, from the bottom up.
 * A layer the field does not reach on either edge comes back shorter. */
function layerSamples(profile: Profile, field: Float64Array, layer: SkewTLayer): { p: number; t: number }[] {
  const samples: { p: number; t: number }[] = [];
  const bottom = interpolateAtPressure(profile, layer.pBottom, field);
  if (Number.isFinite(bottom)) samples.push({ p: layer.pBottom, t: bottom });
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const value = field[index]!;
    if (!Number.isFinite(p) || !Number.isFinite(value)) continue;
    if (p < layer.pBottom && p > layer.pTop) samples.push({ p, t: value });
  }
  const top = interpolateAtPressure(profile, layer.pTop, field);
  if (Number.isFinite(top)) samples.push({ p: layer.pTop, t: top });
  return samples;
}

/**
 * The areas between a lifted parcel and its environment, as two paths: where
 * the parcel is the warmer between its level of free convection and its
 * equilibrium level, and where it is the colder below the former. Each
 * stretch between two of the path's levels is one quadrilateral, split where
 * the two curves cross inside it, and each family is filled once so the
 * pieces share their edges without a seam.
 */
function fillBuoyancy(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  ink: SkewTInk,
  parcel: ParcelPath,
  buoyancy: SkewTBuoyancy,
): void {
  const { lfc, el, environment } = buoyancy;
  if (lfc === null || el === null) return;
  type Piece = [number, number, number, number, number, number];
  const warm: Piece[] = [];
  const cold: Piece[] = [];
  const add = (pA: number, parcelA: number, envA: number, pB: number, parcelB: number, envB: number): void => {
    const middle = Math.sqrt(pA * pB);
    const excess = parcelA - envA + (parcelB - envB);
    if (excess > 0 && middle <= lfc && middle >= el) warm.push([pA, parcelA, envA, pB, parcelB, envB]);
    else if (excess < 0 && middle > lfc) cold.push([pA, parcelA, envA, pB, parcelB, envB]);
  };
  const count = Math.min(parcel.p.length, environment.length);
  for (let index = 0; index + 1 < count; index += 1) {
    let pA = parcel.p[index]!;
    let pB = parcel.p[index + 1]!;
    let parcelA = parcel.t[index]!;
    let parcelB = parcel.t[index + 1]!;
    let envA = environment[index]!;
    let envB = environment[index + 1]!;
    if (![pA, pB, parcelA, parcelB, envA, envB].every(Number.isFinite)) continue;
    if (pA < layout.pTop || pB > layout.pBottom || !(pA > pB)) continue;
    // A stretch that runs past an edge of the chart is cut on that edge.
    const span = Math.log(pB) - Math.log(pA);
    if (pA > layout.pBottom) {
      const weight = (Math.log(layout.pBottom) - Math.log(pA)) / span;
      parcelA += weight * (parcelB - parcelA);
      envA += weight * (envB - envA);
      pA = layout.pBottom;
    }
    if (pB < layout.pTop) {
      const weight = (Math.log(layout.pTop) - Math.log(pA)) / (Math.log(pB) - Math.log(pA));
      parcelB = parcelA + weight * (parcelB - parcelA);
      envB = envA + weight * (envB - envA);
      pB = layout.pTop;
    }
    const excessA = parcelA - envA;
    const excessB = parcelB - envB;
    if (excessA * excessB < 0) {
      // The curves cross inside the stretch: linear in ln p, as the levels
      // on either side were interpolated.
      const weight = excessA / (excessA - excessB);
      const p = Math.exp(Math.log(pA) + weight * (Math.log(pB) - Math.log(pA)));
      const t = parcelA + weight * (parcelB - parcelA);
      add(pA, parcelA, envA, p, t, t);
      add(p, t, t, pB, parcelB, envB);
    } else {
      add(pA, parcelA, envA, pB, parcelB, envB);
    }
  }
  const fill = (pieces: readonly Piece[], color: string | undefined): void => {
    if (pieces.length === 0 || !color) return;
    context.fillStyle = color;
    context.beginPath();
    for (const [pA, parcelA, envA, pB, parcelB, envB] of pieces) {
      const corners = [toXY(layout, pA, envA), toXY(layout, pA, parcelA), toXY(layout, pB, parcelB), toXY(layout, pB, envB)];
      context.moveTo(corners[0]!.x, corners[0]!.y);
      for (const corner of corners.slice(1)) context.lineTo(corner.x, corner.y);
      context.closePath();
    }
    context.fill();
  };
  fill(cold, ink.inhibited);
  fill(warm, ink.buoyant);
}

/** Where a trace leaves the chart on its way up: the highest of its points
 * that is still inside the box, which is where its name is written. */
function traceEnd(
  layout: SkewTLayout,
  pressures: ArrayLike<number>,
  values: ArrayLike<number>,
): { x: number; y: number } | null {
  for (let index = pressures.length - 1; index >= 0; index -= 1) {
    const p = pressures[index]!;
    const value = values[index]!;
    if (!Number.isFinite(p) || !Number.isFinite(value) || p > layout.pBottom || p < layout.pTop) continue;
    const point = toXY(layout, p, value);
    if (point.x >= layout.x + 2 && point.x <= layout.x + layout.width - 2) return point;
  }
  return null;
}

/** The least room between two labels stacked on an edge, in pixels. */
const LABEL_PITCH = 11;

/**
 * Paint the whole chart: the background families, the shaded layers, the
 * profiles over them, the wind column beside them, and the named levels and
 * heights on the two edges. The context is left with its transform, font and
 * dash reset to what it came in with, so a caller can draw two charts into
 * one canvas.
 */
export function drawSkewT(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  ink: SkewTInk,
  options: SkewTOptions = {},
): void {
  const { profile, model, parcel } = options;
  const labels = options.labels ?? {};
  const font = options.font ?? "10px ui-monospace, monospace";
  const size = fontSize(font);
  const right = layout.x + layout.width;
  const bottom = layout.y + layout.height;
  context.save();
  context.font = font;
  context.lineJoin = "round";
  context.lineCap = "butt";
  context.textBaseline = "middle";

  context.fillStyle = ink.paper;
  context.fillRect(layout.x, layout.y, layout.width, layout.height);

  // --- inside the box --------------------------------------------------------
  context.save();
  context.beginPath();
  context.rect(layout.x, layout.y, layout.width, layout.height);
  context.clip();

  // Dry adiabats, the faintest solid family. A family with a hue of its own
  // carries its weight in that color's alpha.
  context.strokeStyle = ink.dryAdiabat ?? ink.grid;
  context.lineWidth = 0.75;
  context.globalAlpha = ink.dryAdiabat ? 1 : 0.7;
  for (let theta = 230; theta <= 500; theta += DRY_ADIABAT_STEP) {
    strokeSamples(
      context,
      layout,
      sampleCurve(layout, (p) => dryAdiabat(theta, p)),
    );
  }

  // Pseudoadiabats: dashed, so they never read as data.
  context.strokeStyle = ink.moistAdiabat ?? ink.grid;
  context.globalAlpha = ink.moistAdiabat ? 1 : 0.5;
  context.setLineDash([5, 4]);
  for (const thetaW of MOIST_ADIABATS) {
    strokeSamples(context, layout, sampleMoistAdiabat(layout, thetaW));
  }
  context.setLineDash([]);

  // Saturation mixing ratio: the lower troposphere only, short dashes.
  context.strokeStyle = ink.mixingRatio ?? ink.grid;
  context.globalAlpha = ink.mixingRatio ? 1 : 0.55;
  context.setLineDash([2, 3]);
  for (const w of MIXING_RATIO_LINES) {
    const samples: { p: number; t: number }[] = [];
    for (let p = layout.pBottom; p >= MIXING_RATIO_TOP_HPA; p -= CURVE_SAMPLE_HPA) {
      samples.push({ p, t: mixingRatioLine(w, p) });
    }
    if (samples.length > 0 && samples[samples.length - 1]!.p > MIXING_RATIO_TOP_HPA) {
      samples.push({ p: MIXING_RATIO_TOP_HPA, t: mixingRatioLine(w, MIXING_RATIO_TOP_HPA) });
    }
    strokeSamples(context, layout, samples);
  }
  context.setLineDash([]);
  context.globalAlpha = 1;

  // Isotherms, the 0 °C line emphasised.
  const { first, last } = isothermRange(layout);
  for (let t = first; t <= last; t += ISOTHERM_STEP) {
    context.strokeStyle = t === 0 ? ink.gridStrong : ink.grid;
    context.lineWidth = t === 0 ? 1.1 : 0.75;
    strokeSamples(context, layout, [
      { p: layout.pBottom, t },
      { p: layout.pTop, t },
    ]);
  }

  // Isobars.
  context.strokeStyle = ink.grid;
  context.lineWidth = 0.75;
  for (const p of LABELLED_ISOBARS) {
    if (p > layout.pBottom || p < layout.pTop) continue;
    const y = Math.round(toXY(layout, p, layout.tMin).y) + 0.5;
    context.beginPath();
    context.moveTo(layout.x, y);
    context.lineTo(right, y);
    context.stroke();
  }

  // The isohumes carry their value where they end, under the profiles: a
  // number a trace crosses is still a number. The family's own hue is a
  // faint one, meant for lines, so the lettering takes the axis labels'.
  if (layout.pBottom > MIXING_RATIO_TOP_HPA && layout.pTop < MIXING_RATIO_TOP_HPA) {
    context.fillStyle = ink.inkMuted;
    context.globalAlpha = 0.75;
    context.textAlign = "center";
    const drawn: { text: string; x: number; y: number }[] = [];
    for (const w of MIXING_RATIO_LINES) {
      const point = toXY(layout, MIXING_RATIO_TOP_HPA, mixingRatioLine(w, MIXING_RATIO_TOP_HPA));
      if (point.x < layout.x + 10 || point.x > right - 10) continue;
      drawn.push({ text: String(w), x: point.x, y: point.y - size * 0.75 });
    }
    // The unit goes after the last value that has the room for it.
    const unit = labels.mixingRatioUnit;
    const lastDrawn = drawn[drawn.length - 1];
    if (unit && lastDrawn) {
      const withUnit = `${lastDrawn.text} ${unit}`;
      if (lastDrawn.x + context.measureText(withUnit).width / 2 < right - 4) lastDrawn.text = withUnit;
    }
    for (const label of drawn) haloText(context, ink, label.text, label.x, label.y);
    context.globalAlpha = 1;
  }

  // --- the layers ------------------------------------------------------------
  if (profile && ink.moistLayer) {
    // A layer near saturation: a bar on the left edge, where the eye finds
    // it at a glance, and a wash between the two traces, where it is.
    context.fillStyle = ink.moistLayer;
    for (const layer of options.moistLayers ?? []) {
      const pBottom = Math.min(layer.pBottom, layout.pBottom);
      const pTop = Math.max(layer.pTop, layout.pTop);
      if (!(pBottom > pTop)) continue;
      const yTop = toXY(layout, pTop, layout.tMin).y;
      const yBottom = toXY(layout, pBottom, layout.tMin).y;
      context.globalAlpha = 0.85;
      context.fillRect(layout.x, yTop, 4, yBottom - yTop);
      const warm = layerSamples(profile, profile.t, { pBottom, pTop });
      const cool = layerSamples(profile, profile.td, { pBottom, pTop });
      if (warm.length < 2 || cool.length < 2) continue;
      context.globalAlpha = 0.3;
      context.beginPath();
      for (const [index, sample] of warm.entries()) {
        const point = toXY(layout, sample.p, sample.t);
        if (index === 0) context.moveTo(point.x, point.y);
        else context.lineTo(point.x, point.y);
      }
      for (const sample of cool.reverse()) {
        const point = toXY(layout, sample.p, sample.t);
        context.lineTo(point.x, point.y);
      }
      context.closePath();
      context.fill();
    }
    context.globalAlpha = 1;
  }
  if (parcel && options.buoyancy) fillBuoyancy(context, layout, ink, parcel, options.buoyancy);

  // --- the data ------------------------------------------------------------
  if (model) {
    context.lineWidth = 1.4;
    context.setLineDash([1.5, 3]);
    context.strokeStyle = ink.modelDew ?? ink.model;
    strokeProfile(context, layout, model, model.td);
    context.strokeStyle = ink.model;
    strokeProfile(context, layout, model, model.t);
    context.setLineDash([]);
  }
  if (parcel) {
    context.strokeStyle = ink.parcel;
    context.lineWidth = 1.3;
    context.setLineDash([7, 4]);
    context.beginPath();
    let pen = false;
    for (let index = 0; index < parcel.p.length; index += 1) {
      const p = parcel.p[index]!;
      const t = parcel.t[index]!;
      // A sonde that reached 5 hPa carries the parcel there too; the chart
      // stops where its axis does.
      if (!Number.isFinite(t) || p > layout.pBottom || p < layout.pTop) {
        pen = false;
        continue;
      }
      const point = toXY(layout, p, t);
      if (pen) context.lineTo(point.x, point.y);
      else context.moveTo(point.x, point.y);
      pen = true;
    }
    context.stroke();
    context.setLineDash([]);
  }
  if (profile) {
    if (ink.inversion) {
      // An inversion is a stretch of the temperature trace itself, so it is
      // marked on the trace: a wide underlay the trace is then drawn over.
      context.strokeStyle = ink.inversion;
      context.lineWidth = 7;
      for (const layer of options.inversions ?? []) {
        const samples = layerSamples(profile, profile.t, layer).filter(
          (sample) => sample.p <= layout.pBottom && sample.p >= layout.pTop,
        );
        if (samples.length >= 2) strokeSamples(context, layout, samples);
      }
    }
    context.strokeStyle = ink.dew ?? ink.ink;
    context.lineWidth = 1.5;
    context.setLineDash([5, 3]);
    strokeProfile(context, layout, profile, profile.td);
    context.setLineDash([]);
    context.strokeStyle = ink.temperature ?? ink.ink;
    context.lineWidth = 2;
    strokeProfile(context, layout, profile, profile.t);
  }
  context.restore();

  // --- the frame and its labels -------------------------------------------
  context.strokeStyle = ink.gridStrong;
  context.lineWidth = 1;
  context.strokeRect(layout.x + 0.5, layout.y + 0.5, layout.width - 1, layout.height - 1);

  context.fillStyle = ink.inkMuted;
  context.textAlign = "right";
  for (const p of LABELLED_ISOBARS) {
    if (p > layout.pBottom || p < layout.pTop) continue;
    context.fillText(String(p), layout.x - 4, toXY(layout, p, layout.tMin).y);
  }
  // The unit sits over the column of pressures, in the margin a caller
  // leaves above the box for the top level's barb.
  if (labels.pressureUnit) context.fillText(labels.pressureUnit, layout.x - 4, layout.y - size * 1.3);
  context.textAlign = "center";
  context.textBaseline = "top";
  let lastTemperatureLabel = layout.x;
  for (let t = first; t <= last; t += ISOTHERM_STEP) {
    const x = toXY(layout, layout.pBottom, t).x;
    if (x < layout.x + 6 || x > right - 6) continue;
    context.fillText(String(t), x, bottom + 4);
    lastTemperatureLabel = x;
  }
  if (labels.temperatureUnit) {
    context.textAlign = "left";
    context.fillText(labels.temperatureUnit, Math.max(lastTemperatureLabel + size * 1.6, right + 2), bottom + 4);
  }
  context.textBaseline = "middle";

  // --- heights, on the right edge --------------------------------------------
  {
    context.textAlign = "right";
    let lastY = Number.POSITIVE_INFINITY;
    const heights = [...(options.heights ?? [])]
      .filter((mark) => Number.isFinite(mark.p) && mark.p <= layout.pBottom && mark.p >= layout.pTop)
      .sort((a, b) => b.p - a.p);
    for (const mark of heights) {
      const y = Math.round(toXY(layout, mark.p, layout.tMin).y) + 0.5;
      // The frame's corners and a label already drawn just below both crowd
      // a tick out.
      if (y < layout.y + size * 0.7 || y > bottom - size * 0.7 || lastY - y < LABEL_PITCH) continue;
      lastY = y;
      context.strokeStyle = ink.gridStrong;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(right - 5, y);
      context.lineTo(right, y);
      context.stroke();
      context.fillStyle = ink.inkMuted;
      haloText(context, ink, mark.label, right - 8, y);
    }
  }

  // --- named levels, on the left edge ----------------------------------------
  {
    const marks = [...(options.marks ?? [])]
      .filter((mark) => Number.isFinite(mark.p) && mark.p <= layout.pBottom && mark.p >= layout.pTop)
      .map((mark) => ({ label: mark.label, y: toXY(layout, mark.p, layout.tMin).y, at: 0 }))
      .sort((a, b) => a.y - b.y);
    // Each label wants to sit at its own level; two levels close together
    // push the lower label down, and a stack that reaches the floor is then
    // pushed back up from it, so every label keeps its pitch and its order.
    let floor = layout.y + size * 0.7;
    for (const mark of marks) {
      mark.at = Math.max(mark.y, floor);
      floor = mark.at + LABEL_PITCH;
    }
    let ceiling = bottom - size * 0.7;
    for (const mark of [...marks].reverse()) {
      mark.at = Math.min(mark.at, ceiling);
      ceiling = mark.at - LABEL_PITCH;
    }
    context.textAlign = "left";
    for (const mark of marks) {
      const y = Math.round(mark.y) + 0.5;
      context.strokeStyle = ink.ink;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(layout.x, y);
      context.lineTo(layout.x + 8, y);
      // A label moved off its level is tied back to its tick.
      if (Math.abs(mark.at - mark.y) > 1) context.lineTo(layout.x + 12, mark.at);
      context.stroke();
      context.fillStyle = ink.ink;
      haloText(context, ink, mark.label, layout.x + 14, mark.at);
    }
  }

  // --- each trace's name, where it ends ----------------------------------------
  {
    const placed: { x: number; y: number }[] = [];
    const name = (text: string | undefined, end: { x: number; y: number } | null, color: string, side: -1 | 1): void => {
      if (!text || !end) return;
      const width = context.measureText(text).width;
      let x = end.x + side * 6;
      let align: "left" | "right" = side > 0 ? "left" : "right";
      // Toward the chart when the preferred side would run off it.
      if (side > 0 && x + width > right - 4) {
        x = end.x - 6;
        align = "right";
      } else if (side < 0 && x - width < layout.x + 4) {
        x = end.x + 6;
        align = "left";
      }
      const y = Math.min(bottom - size, Math.max(layout.y + size, end.y + size * 0.9));
      const centre = align === "left" ? x + width / 2 : x - width / 2;
      if (placed.some((other) => Math.abs(other.y - y) < size && Math.abs(other.x - centre) < width + 6)) return;
      placed.push({ x: centre, y });
      context.textAlign = align;
      context.fillStyle = color;
      haloText(context, ink, text, x, y);
    };
    if (profile) {
      name(labels.temperature, traceEnd(layout, profile.p, profile.t), ink.temperature ?? ink.ink, 1);
      name(labels.dew, traceEnd(layout, profile.p, profile.td), ink.dew ?? ink.ink, -1);
    }
    if (parcel) name(labels.parcel, traceEnd(layout, parcel.p, parcel.t), ink.parcel, -1);
  }

  // --- the wind column -----------------------------------------------------
  const drawBarbs = options.barbs ?? true;
  if (profile && drawBarbs && layout.barbWidth > 0) {
    const cx = right + layout.barbWidth / 2;
    context.strokeStyle = ink.grid;
    context.lineWidth = 0.75;
    context.beginPath();
    context.moveTo(Math.round(cx) + 0.5, layout.y);
    context.lineTo(Math.round(cx) + 0.5, bottom);
    context.stroke();

    context.strokeStyle = ink.ink;
    context.fillStyle = ink.ink;
    context.lineWidth = 1;
    let lastY = Number.POSITIVE_INFINITY;
    for (const index of barbLevels(profile)) {
      const p = profile.p[index]!;
      if (p > layout.pBottom || p < layout.pTop) continue;
      const y = toXY(layout, p, layout.tMin).y;
      if (lastY - y < BARB_SPACING) continue;
      lastY = y;
      drawBarb(context, cx, y, profile.wd[index]!, profile.ws[index]!);
    }
  }

  context.restore();
}

/** One profile's reading under the pointer. */
interface SkewTProfileReadout {
  t: number | null;
  td: number | null;
  wd: number | null;
  ws: number | null;
  z: number | null;
}

/** What the chart says at a point: the pressure and temperature the point
 * itself projects from, and each profile's values there. */
interface SkewTReadout {
  /** hPa. */
  p: number;
  /** °C at the pointer, not on any profile. */
  t: number;
  profiles: SkewTProfileReadout[];
}

function finiteOrNull(value: number): number | null {
  return Number.isFinite(value) ? value : null;
}

/**
 * Read the chart at a canvas point: the pressure it sits at, and every
 * profile's temperature, dew point, wind and height interpolated to that
 * pressure. The wind is interpolated through its components, so a level at
 * 350° and one at 010° read as north between them rather than as a sweep the
 * whole way around the compass.
 *
 * `null` outside the plot rectangle — the wind column is beside the chart,
 * not on it, and a pointer there is not over a reading.
 */
export function readoutAt(
  layout: SkewTLayout,
  profiles: readonly Profile[],
  x: number,
  y: number,
): SkewTReadout | null {
  if (!insideChart(layout, x, y)) return null;
  const { p, t } = fromXY(layout, x, y);
  return {
    p,
    t,
    profiles: profiles.map((profile) => {
      const ws = interpolateAtPressure(profile, p, profile.ws);
      const wd = interpolateAtPressure(profile, p, profile.wd);
      let direction = wd;
      if (Number.isFinite(ws) && Number.isFinite(wd)) {
        // Components, so the interpolation crosses north the short way.
        const u = new Float64Array(profile.n);
        const v = new Float64Array(profile.n);
        for (let index = 0; index < profile.n; index += 1) {
          const speed = profile.ws[index]!;
          const from = profile.wd[index]!;
          const angle = (from * Math.PI) / 180;
          u[index] = -speed * Math.sin(angle);
          v[index] = -speed * Math.cos(angle);
        }
        const uAt = interpolateAtPressure(profile, p, u);
        const vAt = interpolateAtPressure(profile, p, v);
        direction =
          uAt === 0 && vAt === 0 ? Number.NaN : (270 - (Math.atan2(vAt, uAt) * 180) / Math.PI + 360) % 360;
      }
      return {
        t: finiteOrNull(interpolateAtPressure(profile, p, profile.t)),
        td: finiteOrNull(interpolateAtPressure(profile, p, profile.td)),
        wd: finiteOrNull(direction),
        ws: finiteOrNull(ws),
        z: finiteOrNull(interpolateAtPressure(profile, p, profile.z)),
      };
    }),
  };
}

/** The pseudoadiabats' integration step, re-exported so a caller documenting
 * the chart does not have to reach into `profile.ts` for it. */
export { MOIST_STEP_HPA };

/** One value pinned to the crosshair: a dot on a trace and its reading. */
interface SkewTCursorMark {
  /** °C on the trace at the cursor's pressure. */
  t: number;
  color: string;
  /** The text beside the dot; none draws the dot alone. */
  label?: string;
  /** Which side of the dot the text goes. */
  side: "left" | "right";
}

/** What the crosshair says at one pressure. */
export interface SkewTCursor {
  /** hPa. */
  p: number;
  marks: readonly SkewTCursorMark[];
  /** The lines of the tag on the left axis — height and pressure. */
  left: readonly string[];
  /** The tag over the wind column, or null for none. */
  right: string | null;
}

/**
 * The pointer's crosshair over a finished chart: an isobar at the pointer,
 * a dot where it crosses each trace with the reading beside it, the height
 * and pressure in a tag on the left axis and the wind in one over the barb
 * column. The caller has already formatted every string in the viewer's
 * units, so the chart still never asks what a unit is.
 */
export function drawSkewTCursor(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  ink: SkewTInk,
  cursor: SkewTCursor,
  font = "10px ui-monospace, monospace",
): void {
  if (!(cursor.p <= layout.pBottom && cursor.p >= layout.pTop)) return;
  context.save();
  context.font = font;
  context.textBaseline = "middle";
  const y = Math.round(toXY(layout, cursor.p, layout.tMin).y) + 0.5;
  const right = layout.x + layout.width + layout.barbWidth;
  context.strokeStyle = ink.ink;
  context.globalAlpha = 0.55;
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(layout.x, y);
  context.lineTo(right, y);
  context.stroke();
  context.globalAlpha = 1;

  const size = fontSize(font);
  // The two tags sit on the chart's edges at the isobar's own height, so a
  // reading beside a dot near either edge is turned to the dot's other side
  // rather than written under a tag.
  const tagWidth = (lines: readonly string[]): number =>
    lines.length === 0 ? 0 : Math.max(...lines.map((text) => context.measureText(text).width)) + 8;
  const clearLeft = layout.x + Math.max(0, tagWidth(cursor.left) - 2);
  const clearRight = layout.x + layout.width - (cursor.right ? Math.max(0, tagWidth([cursor.right]) - layout.barbWidth) : 0);
  for (const mark of cursor.marks) {
    if (!Number.isFinite(mark.t)) continue;
    const point = toXY(layout, cursor.p, mark.t);
    if (point.x < layout.x || point.x > layout.x + layout.width) continue;
    context.fillStyle = mark.color;
    context.beginPath();
    context.arc(point.x, y, 3, 0, Math.PI * 2);
    context.fill();
    if (!mark.label) continue;
    const width = context.measureText(mark.label).width;
    let side = mark.side;
    if (side === "right" && point.x + 6 + width > clearRight) side = "left";
    else if (side === "left" && point.x - 6 - width < clearLeft) side = "right";
    context.textAlign = side === "left" ? "right" : "left";
    const textX = point.x + (side === "left" ? -6 : 6);
    // Just above the isobar, haloed, so it reads over the lines under it.
    context.fillStyle = mark.color;
    haloText(context, ink, mark.label, textX, y - size * 0.8);
  }

  const tag = (lines: readonly string[], anchor: number, align: "left" | "right"): void => {
    if (lines.length === 0) return;
    const lineHeight = Math.round(size * 1.3);
    const width = tagWidth(lines);
    const height = lines.length * lineHeight + 4;
    const left = align === "left" ? anchor : anchor - width;
    const top = Math.min(layout.y + layout.height - height, Math.max(layout.y, y - height / 2));
    context.fillStyle = ink.tag ?? ink.ink;
    context.fillRect(left, top, width, height);
    context.fillStyle = ink.tagText ?? ink.paper;
    context.textAlign = "left";
    for (const [index, text] of lines.entries()) {
      context.fillText(text, left + 4, top + 2 + lineHeight * (index + 0.5));
    }
  };
  tag(cursor.left, Math.max(0, layout.x - 2), "left");
  if (cursor.right) tag([cursor.right], right, "right");
  context.restore();
}
