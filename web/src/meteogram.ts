/**
 * The meteogram: the pinned point's forecast as stacked rows, one quantity
 * per row, the whole axis across.
 *
 * A row is a fixed set of bundles read at the same cell — temperature with
 * the dew point under it, precipitation, wind with the gust, the cloud
 * layers, sea level pressure — and a run publishes whichever of them it
 * publishes: `meteogramRows` keeps the rows the manifest can fill and drops
 * the rest, so an ECMWF run with three surface bundles draws three rows and
 * a GFS run draws five, and nothing here knows which model it is looking at.
 *
 * Every row reads the *primary* session's axis: a bundle whose own axis
 * lacks a lead time (ECMWF prate has no analysis frame) leaves a gap there,
 * which `alignSeries` does by lead seconds rather than by index. Columns are
 * placed by index, the way the transport track places its ticks, so the
 * playhead here and the one on the capsule agree to the pixel.
 *
 * The drawing is one canvas; the row labels and the readout at the playhead
 * are DOM text beside it (main.ts), so the numbers are selectable and
 * readable to assistive technology, and the canvas only carries the shape.
 * Every series is drawn in the theme's own ink — the shell's charts are
 * instrument traces, not colored plots — and a second series in a row is
 * told apart by its dashes and named by the row's code, never by hue.
 */

import type { ProbeValue } from "./probe";

export type MeteogramRowId = "temperature" | "precipitation" | "wind" | "cloud" | "pressure";

/** How a row draws its series: traces, bars from a baseline, or one shaded
 * band per series (the cloud layers, high over middle over low). */
export type MeteogramRowKind = "line" | "bars" | "bands";

export interface MeteogramRowSpec {
  id: MeteogramRowId;
  kind: MeteogramRowKind;
  /** The bundles the row reads, in drawing order: the first is the trace
   * the row is named for, the second (dew point, gust) rides under it
   * dashed. For bands the order is top to bottom. */
  bundles: readonly string[];
  /** A fixed value range, or null to fit the series with `baseline` held. */
  range: readonly [number, number] | null;
  /** A value the fitted range must include — precipitation and wind start
   * at zero however small the series is. */
  baseline: number | null;
}

interface RowTemplate {
  id: MeteogramRowId;
  kind: MeteogramRowKind;
  /** Candidate bundles in drawing order; the row exists when any is
   * published. */
  bundles: readonly string[];
  /** Bundles used only when none of `bundles` is published — the total cloud
   * cover stands in for the three layers. */
  fallback?: readonly string[];
  range: readonly [number, number] | null;
  baseline: number | null;
}

const ROW_TEMPLATES: readonly RowTemplate[] = [
  { id: "temperature", kind: "line", bundles: ["tmp2m", "dpt2m"], range: null, baseline: null },
  { id: "precipitation", kind: "bars", bundles: ["prate"], range: null, baseline: 0 },
  { id: "wind", kind: "line", bundles: ["wind10m", "gust"], range: null, baseline: 0 },
  { id: "cloud", kind: "bands", bundles: ["hcdc", "mcdc", "lcdc"], fallback: ["tcdc"], range: [0, 100], baseline: null },
  { id: "pressure", kind: "line", bundles: ["prmsl"], range: null, baseline: null },
];

/** The instrument code each bundle is labelled by in a row. */
const BUNDLE_CODES: Record<string, string> = {
  tmp2m: "TMP",
  dpt2m: "DPT",
  prate: "PRATE",
  wind10m: "WIND",
  gust: "GUST",
  hcdc: "HIGH",
  mcdc: "MID",
  lcdc: "LOW",
  tcdc: "TCDC",
  prmsl: "PRMSL",
};

/** Every bundle any row could read; what a pinned point opens sessions for
 * when the run publishes it. */
export const METEOGRAM_BUNDLE_IDS: readonly string[] = ROW_TEMPLATES.flatMap((row) => [
  ...row.bundles,
  ...(row.fallback ?? []),
]);

/** The rows a run can fill, in display order, each restricted to the bundles
 * it publishes. */
export function meteogramRows(published: (bundleId: string) => boolean): MeteogramRowSpec[] {
  const rows: MeteogramRowSpec[] = [];
  for (const template of ROW_TEMPLATES) {
    let bundles = template.bundles.filter(published);
    if (bundles.length === 0 && template.fallback) bundles = template.fallback.filter(published);
    if (bundles.length === 0) continue;
    rows.push({ id: template.id, kind: template.kind, bundles, range: template.range, baseline: template.baseline });
  }
  return rows;
}

/** The row's label: its bundles' codes joined, the surface once at the end
 * where every bundle shares it ("TMP · DPT 2M", "WIND · GUST 10M"). */
export function meteogramRowCode(spec: MeteogramRowSpec): string {
  const codes = spec.bundles.map((id) => BUNDLE_CODES[id] ?? id.toUpperCase());
  const surface = spec.id === "temperature" ? "2M" : spec.id === "wind" ? "10M" : null;
  return surface ? `${codes.join(" · ")} ${surface}` : codes.join(" · ");
}

/**
 * One bundle's series laid onto the primary axis. `offsetFor` answers the
 * bundle's own frame offset at a lead time, or null when its axis has no
 * frame there — that column is a gap, drawn as one and never interpolated.
 */
export function alignSeries(
  axisLeadSeconds: readonly number[],
  offsetFor: (leadSeconds: number) => number | null,
  read: (frameOffset: number) => ProbeValue,
): ProbeValue[] {
  return axisLeadSeconds.map((lead) => {
    const offset = offsetFor(lead);
    return offset === null ? undefined : read(offset);
  });
}

/** Whether every frame a bundle's axis has at the point is in hand: a series
 * with a column its axis lacks is still complete, one awaiting a frame is
 * not, and one with nothing at all is empty. */
export type SeriesState = "empty" | "partial" | "complete";

export function seriesState(
  axisLeadSeconds: readonly number[],
  offsetFor: (leadSeconds: number) => number | null,
  values: readonly ProbeValue[],
): SeriesState {
  let expected = 0;
  let present = 0;
  for (const [index, lead] of axisLeadSeconds.entries()) {
    if (offsetFor(lead) === null) continue;
    expected += 1;
    if (values[index] !== undefined) present += 1;
  }
  if (expected === 0 || present === 0) return "empty";
  return present === expected ? "complete" : "partial";
}

/** The value range a row draws over: the fixed one, or the series' own
 * extent held to the baseline, with a little headroom so a trace never
 * touches the row's edge. A series that never moves is centred rather than
 * divided by zero. */
export function fitRange(
  series: readonly (readonly ProbeValue[])[],
  fixed: readonly [number, number] | null,
  baseline: number | null,
): [number, number] {
  if (fixed) return [fixed[0], fixed[1]];
  let lowest = Infinity;
  let highest = -Infinity;
  for (const values of series) {
    for (const value of values) {
      if (typeof value !== "number") continue;
      lowest = Math.min(lowest, value);
      highest = Math.max(highest, value);
    }
  }
  if (lowest === Infinity) return baseline === null ? [0, 1] : [baseline, baseline + 1];
  if (baseline !== null) {
    lowest = Math.min(lowest, baseline);
    highest = Math.max(highest, baseline);
  }
  if (highest - lowest < 1e-9) {
    // A dry series sits on its baseline and grows upward from it.
    return baseline !== null && lowest === baseline ? [baseline, baseline + 1] : [lowest - 0.5, lowest + 0.5];
  }
  const padding = (highest - lowest) * 0.08;
  return [
    baseline !== null && lowest === baseline ? lowest : lowest - padding,
    baseline !== null && highest === baseline ? highest : highest + padding,
  ];
}

/** Column centres by index, the track's own placement: the first frame at
 * the left edge, the last at the right. */
export function columnX(index: number, count: number, width: number): number {
  return count > 1 ? (index / (count - 1)) * width : width / 2;
}

/** The frame nearest a horizontal position, clamped to the axis — what a
 * press on the chart scrubs to. */
export function frameIndexAtX(x: number, count: number, width: number): number {
  if (count <= 1 || width <= 0) return 0;
  return Math.max(0, Math.min(count - 1, Math.round((x / width) * (count - 1))));
}

/** A day boundary on the axis: the first frame of a UTC day and its mark. */
export interface DayMark {
  index: number;
  label: string;
}

export interface MeteogramRowData {
  spec: MeteogramRowSpec;
  /** One series per bundle of the spec, on the shared axis. */
  series: readonly (readonly ProbeValue[])[];
  /** Meteorological wind direction per frame (degrees the wind comes from),
   * on a wind row whose bundle is a component pair. */
  directions?: readonly (number | null)[];
}

export interface MeteogramGeometry {
  /** CSS pixels the canvas is drawn at (scaled by devicePixelRatio outside). */
  width: number;
  /** The day-mark strip above the first row. */
  headerHeight: number;
  rowHeight: number;
}

export interface MeteogramInk {
  /** The traces, bars and bands. */
  ink: string;
  /** Labels, and the second series of a row. */
  muted: string;
}

/** Vertical room inside a row: the trace never touches the separator. */
const ROW_PADDING = 5;
/** The strip at the foot of a wind row holding its direction arrows. */
const ARROW_STRIP = 12;
/** The least horizontal room between two direction arrows. */
const ARROW_SPACING = 16;

export function drawMeteogram(
  context: CanvasRenderingContext2D,
  geometry: MeteogramGeometry,
  rows: readonly MeteogramRowData[],
  options: { count: number; selected: number; dayMarks: readonly DayMark[]; ink: MeteogramInk; font: string },
): void {
  const { width, headerHeight, rowHeight } = geometry;
  const { count, selected, dayMarks, ink } = options;
  const height = headerHeight + rows.length * rowHeight;
  context.clearRect(0, 0, width, height);
  context.font = options.font;
  context.textBaseline = "middle";
  context.lineJoin = "round";
  context.lineCap = "round";
  const x = (index: number): number => columnX(index, count, width);
  const column = count > 1 ? width / (count - 1) : width;

  // Day boundaries run the whole height, labelled in the strip.
  context.strokeStyle = ink.muted;
  context.fillStyle = ink.muted;
  context.lineWidth = 1;
  context.textAlign = "left";
  for (const mark of dayMarks) {
    const px = Math.round(x(mark.index)) + 0.5;
    context.globalAlpha = 0.25;
    context.beginPath();
    context.moveTo(px, headerHeight - 3);
    context.lineTo(px, height);
    context.stroke();
    context.globalAlpha = 1;
    if (px + 30 <= width) context.fillText(mark.label, px + 3, headerHeight / 2);
  }

  // Row separators.
  context.globalAlpha = 0.22;
  for (let row = 0; row <= rows.length; row += 1) {
    const py = headerHeight + row * rowHeight + 0.5;
    context.beginPath();
    context.moveTo(0, py);
    context.lineTo(width, py);
    context.stroke();
  }
  context.globalAlpha = 1;

  for (const [rowIndex, row] of rows.entries()) {
    const top = headerHeight + rowIndex * rowHeight;
    const arrows = row.spec.id === "wind" && row.directions !== undefined;
    const plotTop = top + ROW_PADDING;
    const plotBottom = top + rowHeight - ROW_PADDING - (arrows ? ARROW_STRIP : 0);
    const [low, high] = fitRange(row.series, row.spec.range, row.spec.baseline);
    const y = (value: number): number => plotBottom - ((value - low) / (high - low)) * (plotBottom - plotTop);

    if (row.spec.kind === "bands") {
      // One band per layer, shaded by its cover: nothing at clear sky, the
      // ink at overcast.
      const bandHeight = (plotBottom - plotTop) / row.series.length;
      for (const [band, values] of row.series.entries()) {
        const bandTop = plotTop + band * bandHeight;
        for (const [index, value] of values.entries()) {
          if (typeof value !== "number" || value <= 0) continue;
          context.globalAlpha = Math.min(1, (value - low) / (high - low)) * 0.7;
          context.fillStyle = ink.ink;
          context.fillRect(x(index) - column / 2, bandTop, column + 0.5, bandHeight);
        }
      }
      context.globalAlpha = 1;
    } else if (row.spec.kind === "bars") {
      context.fillStyle = ink.ink;
      context.globalAlpha = 0.7;
      const base = y(low);
      for (const [index, value] of row.series[0]!.entries()) {
        if (typeof value !== "number" || value <= low) continue;
        const barTop = y(value);
        context.fillRect(x(index) - column / 2, barTop, Math.max(1, column - 0.5), base - barTop);
      }
      context.globalAlpha = 1;
    } else {
      for (const [position, values] of row.series.entries()) {
        context.strokeStyle = position === 0 ? ink.ink : ink.muted;
        context.lineWidth = position === 0 ? 1.5 : 1;
        context.setLineDash(position === 0 ? [] : [3, 3]);
        context.beginPath();
        let pen = false;
        for (const [index, value] of values.entries()) {
          if (typeof value !== "number") {
            pen = false;
            continue;
          }
          if (pen) context.lineTo(x(index), y(value));
          else context.moveTo(x(index), y(value));
          pen = true;
        }
        context.stroke();
      }
      context.setLineDash([]);
    }

    if (arrows) {
      // The wind's direction of travel, one arrow per few columns.
      const stride = Math.max(1, Math.ceil(ARROW_SPACING / column));
      const centerY = top + rowHeight - ROW_PADDING - ARROW_STRIP / 2;
      context.strokeStyle = ink.muted;
      context.fillStyle = ink.muted;
      context.lineWidth = 1;
      for (let index = 0; index < count; index += stride) {
        const from = row.directions![index];
        if (from === null || from === undefined) continue;
        // Meteorological direction is where the wind comes from, measured
        // clockwise from north; the arrow flies the other way, on a canvas
        // whose y axis points down.
        const heading = ((from + 180) * Math.PI) / 180;
        const dx = Math.sin(heading);
        const dy = -Math.cos(heading);
        const cx = x(index);
        const length = 4;
        context.beginPath();
        context.moveTo(cx - dx * length, centerY - dy * length);
        context.lineTo(cx + dx * length, centerY + dy * length);
        context.stroke();
        context.beginPath();
        context.moveTo(cx + dx * length, centerY + dy * length);
        context.lineTo(cx + dx * length - dx * 3 + dy * 2, centerY + dy * length - dy * 3 - dx * 2);
        context.lineTo(cx + dx * length - dx * 3 - dy * 2, centerY + dy * length - dy * 3 + dx * 2);
        context.closePath();
        context.fill();
      }
    }
  }

  // The playhead, over everything, so the frame on screen is findable in
  // every row at once.
  context.strokeStyle = ink.ink;
  context.globalAlpha = 0.55;
  context.lineWidth = 1;
  const px = Math.round(x(selected)) + 0.5;
  context.beginPath();
  context.moveTo(px, headerHeight - 3);
  context.lineTo(px, height);
  context.stroke();
  context.globalAlpha = 1;
}
