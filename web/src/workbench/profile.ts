/**
 * The profile between the planes: the probe's column as a skew-T on the planes'
 * own pressure axis, pixel for pixel, with the axis of pressure at its left
 * edge and of height at its right that all three are read by.
 */

import { P_BOTTOM } from "../crosssection/chart";
import { element } from "../dom";
import { t } from "../i18n";
import { analyseProfile } from "../sounding/analysis";
import { modelColumn } from "../sounding/column";
import { skewTInkFrom } from "../sounding/ink";
import { convectiveCondensation } from "../sounding/profile";
import { drawSkewT, pressureAtHeight, skewTLayout, toXY, type SkewTLayout } from "../sounding/skewt";
import { guideItems, heightScale, indexItems, levelMarks, summaryLines, type ProfileReading } from "../sounding/summary";
import type { PlaceReading } from "./engine";
import { ROWS } from "./layout";
import { Pad, type PadHandlers } from "./pad";

const FONT = '10px "IBM Plex Mono", ui-monospace, monospace';
/** Beside the plot: the pressures, and the barbs. */
const PROFILE_MARGIN = { left: 34, right: 46 } as const;
const BARB_WIDTH = 34;
const DEGREES_PER_PIXEL = 1 / 4.4;
const SPAN: readonly [number, number] = [85, 190];
const SPAN_CENTRE = 2.5;
/** The warmest air a chart's top edge has to hold, °C, by the top. */
const WARMEST_AT = (top: number): number => (top <= 100 ? -35 : 8);

function layoutFor(width: number, height: number, top: number): SkewTLayout {
  const plotWidth = Math.max(40, width - PROFILE_MARGIN.left - PROFILE_MARGIN.right);
  const span = Math.min(SPAN[1], Math.max(SPAN[0], plotWidth * DEGREES_PER_PIXEL));
  const tMin = SPAN_CENTRE - span / 2;
  const tMax = SPAN_CENTRE + span / 2;
  const degreesPerPixel = span / plotWidth;
  return skewTLayout(
    { x: PROFILE_MARGIN.left, y: ROWS.gutter, width: plotWidth, height },
    {
      tMin,
      tMax,
      skew: Math.max(0, Math.min(1, (tMax - WARMEST_AT(top)) / (height * degreesPerPixel))),
      pressureBottom: P_BOTTOM,
      pressureTop: top,
      barbWidth: BARB_WIDTH,
    },
  );
}

/** A column as the chart and the words beside it read it. The parcel is the
 * lowest 100 hPa's, lifted here: the run's own energies are not in the store. */
export function profileReading(column: NonNullable<PlaceReading["column"]>): ProfileReading | null {
  const { profile } = modelColumn(column.levels, column.surface);
  if (profile.n < 2) return null;
  return { profile, analysis: analyseProfile(profile), convective: convectiveCondensation(profile), column: null };
}

export class ProfileView {
  readonly root = element("div", "wb-profile");
  /** The summary, the indices and the guide, folded. */
  readonly notes = element("details", "wb-notes");
  readonly pad: Pad;
  private readonly canvas = element("canvas", "wb-profile-canvas");
  private readonly notesTitle = element("summary", "wb-notes-title");
  private readonly summary = element("ul", "sounding-summary");
  private readonly parcel = element("p", "wb-notes-parcel");
  private readonly indices = element("dl", "sounding-indices");
  private readonly guide = element("details", "sounding-guide");
  private readonly guideTitle = element("summary", "sounding-guide-title");
  private readonly guideList = element("ol", "sounding-guide-list");
  private reading: ProfileReading | null = null;
  private top = 100;
  private plot = 0;

  constructor(handlers: PadHandlers) {
    this.canvas.setAttribute("role", "img");
    this.pad = new Pad("wb-profile-pad", handlers);
    this.pad.root.style.left = `${PROFILE_MARGIN.left}px`;
    this.pad.root.style.right = `${PROFILE_MARGIN.right - BARB_WIDTH}px`;
    this.root.append(this.canvas, this.pad.root);
    this.guide.append(this.guideTitle, this.guideList);
    this.notes.append(this.notesTitle, this.summary, this.parcel, this.indices, this.guide);
    new ResizeObserver(() => this.paint()).observe(this.root);
  }

  show(reading: ProfileReading | null, top: number, plot: number): void {
    this.reading = reading;
    this.top = top;
    this.plot = plot;
    this.paint();
    this.write();
  }

  /** The theme, the units or the language changed. */
  refresh(): void {
    this.paint();
    this.write();
  }

  private paint(): void {
    const { canvas, reading, plot } = this;
    const width = this.root.clientWidth;
    if (width === 0 || plot === 0) return;
    const height = plot + 2 * ROWS.gutter;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const pixels = [Math.round(width * ratio), Math.round(height * ratio)] as const;
    if (canvas.width !== pixels[0] || canvas.height !== pixels[1]) [canvas.width, canvas.height] = pixels;
    canvas.style.height = `${height}px`;
    canvas.setAttribute("aria-label", t("wbProfileAria"));
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const layout = layoutFor(width, plot, this.top);
    const ink = skewTInkFrom(this.root);
    const profile = reading?.profile ?? null;
    const parcel = reading?.analysis.parcel ?? null;
    drawSkewT(context, layout, ink, {
      profile,
      parcel: parcel?.path ?? null,
      buoyancy: parcel ? { environment: parcel.environment, lfc: parcel.lfc, el: parcel.el } : null,
      nodes: true,
      moistLayers: reading?.analysis.moist ?? [],
      inversions: reading?.analysis.inversions ?? [],
      marks: reading ? levelMarks(reading) : [],
      heights: profile ? heightScale().map(({ metres, label }) => ({ p: pressureAtHeight(profile, metres), label })) : [],
      labels: {
        temperature: "T",
        dew: "Td",
        parcel: t("soundingParcel"),
        pressureUnit: "hPa",
        temperatureUnit: "°C",
        mixingRatioUnit: "g/kg",
      },
      font: FONT,
    });
    if (!profile) return;
    // A dew point past the left edge is said to be, not cut off in silence.
    context.fillStyle = ink.dew ?? ink.ink;
    for (let level = 0; level < profile.n; level += 1) {
      const p = profile.p[level]!;
      const td = profile.td[level]!;
      if (!(td === td) || p < layout.pTop || p > layout.pBottom) continue;
      const at = toXY(layout, p, td);
      if (at.x >= layout.x) continue;
      context.beginPath();
      context.moveTo(layout.x + 1, at.y);
      context.lineTo(layout.x + 7, at.y - 3.5);
      context.lineTo(layout.x + 7, at.y + 3.5);
      context.closePath();
      context.fill();
    }
  }

  private write(): void {
    const { reading } = this;
    this.notesTitle.textContent = t("wbNotes");
    this.guideTitle.textContent = t("soundingGuideTitle");
    this.guideList.replaceChildren(...guideItems());
    this.notes.hidden = reading === null;
    if (!reading) return;
    this.summary.replaceChildren(...summaryLines(reading).map((text) => element("li", "sounding-summary-line", text)));
    const { parcel } = reading.analysis;
    this.parcel.textContent = parcel
      ? `${t("soundingParcel")}: ${t(parcel.kind === "mixed" ? "soundingParcelMixed" : "soundingParcelSurface")}`
      : "";
    this.indices.replaceChildren(...indexItems(reading));
  }
}
