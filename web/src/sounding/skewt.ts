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
 * What is deliberately absent: CAPE and CIN, the shaded areas between the
 * parcel and the environment. They are a later phase; `parcelPath` gives the
 * curve, and the areas want a sounding-quality integration and a story about
 * which parcel, which is more than a chart.
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
export interface SkewTRect {
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

export interface SkewTLayoutOptions {
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
export const LABELLED_ISOBARS: readonly number[] = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100, 70, 50];

/** Isotherms every 10 °C, the 0 °C line emphasised. */
export const ISOTHERM_STEP = 10;
/** Dry adiabats every 10 K of potential temperature. */
export const DRY_ADIABAT_STEP = 10;
/** The pseudoadiabats drawn, by wet-bulb potential temperature at 1000 hPa
 * (°C). A few, wide apart: they are orientation, not a reading. */
export const MOIST_ADIABATS: readonly number[] = [-20, -10, 0, 8, 16, 24, 32, 40];
/** The saturation mixing-ratio lines drawn, g/kg. */
export const MIXING_RATIO_LINES: readonly number[] = [1, 2, 4, 8, 16];
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
  /** A second, model profile. */
  model: string;
  /** The lifted parcel. */
  parcel: string;
}

export interface SkewTOptions {
  /** The observed ascent: `t` solid, `td` dashed. */
  profile?: Profile | null;
  /** A second profile — a model column at the same point — in the same ink,
   * dotted and thinner, because the chart tells series apart by dash and
   * weight rather than by hue. */
  model?: Profile | null;
  /** A lifted parcel, thin and muted. */
  parcel?: ParcelPath | null;
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

/** One profile field as a trace: its own levels, gaps left as gaps. */
function strokeProfile(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  profile: Profile,
  field: Float64Array,
): void {
  context.beginPath();
  let pen = false;
  for (let index = 0; index < profile.n; index += 1) {
    const p = profile.p[index]!;
    const value = field[index]!;
    if (!Number.isFinite(p) || !Number.isFinite(value) || p > layout.pBottom || p < layout.pTop) {
      pen = false;
      continue;
    }
    const point = toXY(layout, p, value);
    if (pen) context.lineTo(point.x, point.y);
    else context.moveTo(point.x, point.y);
    pen = true;
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

/**
 * Paint the whole chart: the background families, the profiles over them, the
 * wind column beside them, and the derived heights as ticks on the left axis.
 * The context is left with its transform, font and dash reset to what it came
 * in with, so a caller can draw two charts into one canvas.
 */
export function drawSkewT(
  context: CanvasRenderingContext2D,
  layout: SkewTLayout,
  ink: SkewTInk,
  options: SkewTOptions = {},
): void {
  const { profile, model, parcel } = options;
  const font = options.font ?? "10px ui-monospace, monospace";
  context.save();
  context.font = font;
  context.lineJoin = "round";
  context.lineCap = "butt";
  context.textBaseline = "middle";

  context.fillStyle = ink.paper;
  context.fillRect(layout.x, layout.y, layout.width, layout.height);

  // --- the background families, inside the box -----------------------------
  context.save();
  context.beginPath();
  context.rect(layout.x, layout.y, layout.width, layout.height);
  context.clip();

  // Dry adiabats: every 10 K of potential temperature that crosses the box.
  context.strokeStyle = ink.grid;
  context.lineWidth = 0.75;
  context.globalAlpha = 0.85;
  for (let theta = 230; theta <= 500; theta += DRY_ADIABAT_STEP) {
    strokeSamples(
      context,
      layout,
      sampleCurve(layout, (p) => dryAdiabat(theta, p)),
    );
  }

  // Pseudoadiabats: fainter still, and dashed, so they never read as data.
  context.globalAlpha = 0.5;
  context.setLineDash([1, 3]);
  for (const thetaW of MOIST_ADIABATS) {
    strokeSamples(context, layout, sampleMoistAdiabat(layout, thetaW));
  }
  context.setLineDash([]);

  // Saturation mixing ratio: the lower troposphere only, dashed.
  context.globalAlpha = 0.55;
  context.setLineDash([4, 4]);
  for (const w of MIXING_RATIO_LINES) {
    const samples: { p: number; t: number }[] = [];
    for (let p = layout.pBottom; p >= MIXING_RATIO_TOP_HPA; p -= CURVE_SAMPLE_HPA) {
      samples.push({ p, t: mixingRatioLine(w, p) });
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
    context.lineTo(layout.x + layout.width, y);
    context.stroke();
  }

  // --- the data ------------------------------------------------------------
  if (model) {
    context.strokeStyle = ink.model;
    context.lineWidth = 1;
    context.setLineDash([1.5, 2.5]);
    strokeProfile(context, layout, model, model.t);
    strokeProfile(context, layout, model, model.td);
    context.setLineDash([]);
  }
  if (parcel) {
    context.strokeStyle = ink.parcel;
    context.lineWidth = 1;
    context.setLineDash([5, 3]);
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
    context.strokeStyle = ink.ink;
    context.lineWidth = 1;
    context.setLineDash([4, 3]);
    strokeProfile(context, layout, profile, profile.td);
    context.setLineDash([]);
    context.lineWidth = 1.8;
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
  context.textAlign = "center";
  context.textBaseline = "top";
  for (let t = first; t <= last; t += ISOTHERM_STEP) {
    const x = toXY(layout, layout.pBottom, t).x;
    if (x < layout.x + 6 || x > layout.x + layout.width - 6) continue;
    context.fillText(String(t), x, layout.y + layout.height + 4);
  }
  context.textBaseline = "middle";

  // The isohumes carry their value where they leave the top of their range,
  // which is the only place they are not under a profile.
  context.fillStyle = ink.inkMuted;
  context.globalAlpha = 0.8;
  context.textAlign = "center";
  for (const w of MIXING_RATIO_LINES) {
    const point = toXY(layout, MIXING_RATIO_TOP_HPA, mixingRatioLine(w, MIXING_RATIO_TOP_HPA));
    if (point.x < layout.x + 8 || point.x > layout.x + layout.width - 8) continue;
    context.fillText(String(w), point.x, point.y - 6);
  }
  context.globalAlpha = 1;

  // --- the derived heights, as ticks on the left axis ----------------------
  if (profile?.derived) {
    context.strokeStyle = ink.ink;
    context.fillStyle = ink.inkMuted;
    context.lineWidth = 1;
    context.textAlign = "left";
    const marks: [number | null, string][] = [
      [profile.derived.freezingLevel, "0°"],
      [profile.derived.tropopause, "TRP"],
    ];
    for (const [height, label] of marks) {
      if (height === null) continue;
      const p = pressureAtHeight(profile, height);
      if (!Number.isFinite(p) || p > layout.pBottom || p < layout.pTop) continue;
      const y = Math.round(toXY(layout, p, layout.tMin).y) + 0.5;
      context.beginPath();
      context.moveTo(layout.x, y);
      context.lineTo(layout.x + 7, y);
      context.stroke();
      context.globalAlpha = 0.8;
      context.fillText(label, layout.x + 9, y - 5);
      context.globalAlpha = 1;
    }
  }

  // --- the wind column -----------------------------------------------------
  const drawBarbs = options.barbs ?? true;
  if (profile && drawBarbs && layout.barbWidth > 0) {
    const cx = layout.x + layout.width + layout.barbWidth / 2;
    context.strokeStyle = ink.grid;
    context.lineWidth = 0.75;
    context.beginPath();
    context.moveTo(Math.round(cx) + 0.5, layout.y);
    context.lineTo(Math.round(cx) + 0.5, layout.y + layout.height);
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
export interface SkewTProfileReadout {
  t: number | null;
  td: number | null;
  wd: number | null;
  ws: number | null;
  z: number | null;
}

/** What the chart says at a point: the pressure and temperature the point
 * itself projects from, and each profile's values there. */
export interface SkewTReadout {
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
