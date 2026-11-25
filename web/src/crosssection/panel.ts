/**
 * The cross-section's panel: the probe panel's slot over the transport
 * capsule, with the chart's axes, its controls, its legend and the crosshair
 * around the canvas the renderer draws the field on. It holds no state of
 * the section's: `control.ts` hands it all of it at every change.
 *
 * The crosshair is elements of its own over the canvas, so reading the chart
 * never draws it again.
 */

import { formatCoordinate } from "../coordinates";
import { element } from "../dom";
import { formatPointDegrees } from "../format";
import { t } from "../i18n";
import { coordinateFormat } from "../mapprefs";
import { formatDistance } from "../measure";
import { formatHeight, formatTemperature, formatWind, units } from "../units";
import { axisPressures, pointX, pressureY, xPoint, yPressure } from "./chart";
import { axisDistance, axisHeight, axisHeights, legendTicks, legendUnit } from "./displayunits";
import { sectionLegend } from "./ramps";
import type { SectionDrawn, SectionDrawState, SectionFill, SectionLine, SectionPoint, SectionReading } from "./types";

/** What the panel's controls set, which is the reader's and outlives a line. */
export type SectionOptions = Pick<SectionDrawState, "fill" | "lines" | "barbs" | "top">;
export type SectionSwitch = "wind" | "zero" | "theta" | "barbs";

/** A place on the chart. */
export interface SectionCursor {
  point: number;
  pressure: number;
}

export interface SectionPanelState {
  line: SectionLine;
  /** Metres, and the points the chart is sampled at; null until the store is
   * open. */
  length: number | null;
  points: readonly SectionPoint[] | null;
  /** What is said of the section as it stands. */
  notices: readonly string[];
  /** Whether a press on the notices asks for what failed again. */
  retry: boolean;
  /** There is nothing to draw: the header's first line alone. */
  collapsed: boolean;
  /** There is no chart for now; what the canvas holds is of another. */
  blank: boolean;
  options: SectionOptions;
  /** The fills offered. */
  fills: readonly SectionFill[];
  heights: SectionDrawn["heights"];
  /** The store's grid, degrees, for the note on its terrain. */
  step: number | null;
}

export interface SectionPanelHandlers {
  onClose(): void;
  onRetry(): void;
  onFill(fill: SectionFill): void;
  onSwitch(name: SectionSwitch): void;
  onTop(top: number): void;
  /** The plot's size changed, CSS pixels. */
  onResize(width: number, height: number): void;
  /** The crosshair moved, or left the chart. */
  onCursor(cursor: SectionCursor | null): void;
}

export const SECTION_FILLS: readonly SectionFill[] = ["thetae", "tmp", "rh", "wind", "cross"];
const SWITCHES: readonly SectionSwitch[] = ["wind", "zero", "theta", "barbs"];
const TOPS: readonly number[] = [100, 500];

/** CSS pixels an arrow key moves the crosshair. */
const KEY_STEP = 6;
/** From this window's width the header is one row where it fits, and the
 * notices stand in the legend's row, so that one coming or going moves
 * nothing. The stylesheet has the same number. */
const WIDE_WINDOW = 900;

function fillLabel(fill: SectionFill): string {
  switch (fill) {
    case "thetae":
      return t("tipThetaE");
    case "tmp":
      return t("tipTemp");
    case "rh":
      return t("tipHumidity");
    case "wind":
      return t("sectionFillWind");
    case "cross":
      return t("sectionFillCross");
  }
}

function switchLabel(name: SectionSwitch): string {
  switch (name) {
    case "wind":
      return t("sectionIsotachs");
    case "zero":
      return t("sectionZeroLine");
    case "theta":
      return "θ";
    case "barbs":
      return t("sectionBarbs");
  }
}

/** A place in the reader's coordinate format: degrees, or a reference as
 * coarse as the degrees would be. */
export function sectionPosition([lon, lat]: readonly [number, number], decimals: number): string {
  const reference = formatCoordinate(coordinateFormat, lat, lon, decimals >= 2 ? 1_000 : 10_000);
  if (reference !== null) return reference;
  return `${formatPointDegrees(lat, "NS", decimals)} ${formatPointDegrees(turned(lon), "EW", decimals)}`;
}

/** A longitude within one turn. */
function turned(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/** The crosshair's reading, line by line. Pressure is in hPa whatever the
 * pressure unit: the axis is. */
export function readingLines(reading: SectionReading, place: string): string[] {
  const known = (value: number, text: (value: number) => string): string => (Number.isFinite(value) ? text(value) : "--");
  const level = `${Math.round(reading.pressure)} hPa`;
  if (reading.belowGround) {
    return [level, place, t("sectionBelowGround", { pressure: `${Math.round(reading.surfacePressure)} hPa` })];
  }
  const kelvin = (value: number): string => `${value.toFixed(1)} K`;
  return [
    `${level} · ${known(reading.height, (value) => formatHeight(value))}`,
    place,
    `${known(reading.temperature, (value) => formatTemperature(value))} · ${t("stationHumidity")} ${known(reading.humidity, (value) => `${Math.round(value)} %`)}`,
    `${t("tipWind")} ${known(reading.windDirection, (value) => `${Math.round(value) % 360}°`)} ${known(reading.windSpeed, (value) => formatWind(value))}`,
    `θ ${known(reading.theta, kelvin)} · θe ${known(reading.thetaE, kelvin)}`,
  ];
}

/** The least room between two labels of the distance axis. */
const LABEL_GAP = 14;
/** About as dense as the distance axis is labelled, pixels a label. */
const LABEL_PITCH = 120;

/** Whether labels of these widths, evenly spaced from one end of a plot to
 * the other with the first and the last inside it, keep `LABEL_GAP` apart. */
export function labelsFit(widths: readonly number[], width: number): boolean {
  let before = Number.NEGATIVE_INFINITY;
  return widths.every((label, index) => {
    const at = (index / (widths.length - 1)) * width;
    const left = index === 0 ? 0 : index === widths.length - 1 ? width - label : at - label / 2;
    const clear = left - before >= LABEL_GAP;
    before = left + label;
    return clear;
  });
}

/** How many steps the distance axis is labelled in, with labels as wide as
 * `widthAt(share)`: as many as there is room for, or 0 where not even the two
 * ends fit. */
export function axisSteps(width: number, widthAt: (share: number) => number): number {
  for (let steps = Math.max(1, Math.floor(width / LABEL_PITCH)); steps > 0; steps -= 1) {
    const widths = Array.from({ length: steps + 1 }, (_, index) => widthAt(index / steps));
    if (labelsFit(widths, width)) return steps;
  }
  return 0;
}

/** The coordinate that says more of where a point of the line is. */
export function varyingCoordinate(points: readonly { lon: number; lat: number }[]): "lon" | "lat" {
  const span = (values: number[]): number => Math.max(...values) - Math.min(...values);
  return span(points.map(({ lat }) => lat)) > span(points.map(({ lon }) => lon)) ? "lat" : "lon";
}

function button(className: string): HTMLButtonElement {
  const node = element("button", className);
  node.type = "button";
  return node;
}

export class SectionPanel {
  readonly root = element("section", "section-panel");

  private readonly title = element("b", "section-title");
  private readonly ends = element("span", "section-ends");
  private readonly length = element("span", "section-length");
  private readonly headline = element("div", "section-headline");
  private readonly legend = element("div", "section-legend");
  /** The notices and the retry beside them. */
  private readonly status = element("div", "section-status");
  private readonly notice = element("span", "section-notice");
  private readonly retry = button("section-chip section-retry");
  private readonly close = button("probe-close section-close");
  private canvas: HTMLCanvasElement | null = null;
  private readonly fill = element("select", "section-fill");
  private readonly switches: { name: SectionSwitch; node: HTMLButtonElement }[];
  private readonly tops: { top: number; node: HTMLButtonElement }[];
  private readonly topGroup = element("div", "section-tops");
  private readonly plot = element("div", "section-plot");
  private readonly pressures = element("div", "section-pressures");
  private readonly heights = element("div", "section-heights");
  private readonly distances = element("div", "section-distances");
  /** A label never seen, which the others are measured by. */
  private readonly ruler = element("span", "section-distance section-ruler");
  private readonly bar = element("div", "section-bar");
  private readonly barTicks = element("div", "section-bar-ticks");
  private readonly barUnit = element("span", "section-bar-unit");
  private readonly terrain = element("span", "section-terrain");
  private readonly vertical = element("div", "section-cross section-cross-v");
  private readonly horizontal = element("div", "section-cross section-cross-h");
  private readonly tip = element("div", "section-tip");
  /** What is said aloud, which is less than what is written: the notices count
   * a read up, and the reading at the crosshair changes with every frame. */
  private readonly spoken = element("div", "sr-only");

  private state: SectionPanelState | null = null;
  private cursor: SectionCursor | null = null;
  private width = 0;
  private height = 0;
  /** What each part was last written from, so that a frame drawn during
   * playback rewrites only what it changed. */
  private readonly written = new Map<string, string>();

  constructor(private readonly handlers: SectionPanelHandlers) {
    this.root.id = "section-panel";
    this.root.hidden = true;
    this.close.innerHTML =
      '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" /></svg>';
    this.close.addEventListener("click", () => handlers.onClose());
    this.retry.addEventListener("click", () => handlers.onRetry());
    this.spoken.setAttribute("role", "status");

    for (const fill of SECTION_FILLS) {
      const option = document.createElement("option");
      option.value = fill;
      this.fill.append(option);
    }
    // Picked by pointer, the list hands the focus to the chart: an Escape on a
    // focused list is the list's own. Arrow keys on a closed list keep it.
    let picking = false;
    this.fill.addEventListener("pointerdown", () => (picking = true));
    this.fill.addEventListener("keydown", () => (picking = false));
    this.fill.addEventListener("change", () => {
      handlers.onFill(this.fill.value as SectionFill);
      if (picking) this.plot.focus({ preventScroll: true });
      picking = false;
    });
    this.switches = SWITCHES.map((name) => {
      const node = button("section-chip");
      node.addEventListener("click", () => handlers.onSwitch(name));
      return { name, node };
    });
    this.topGroup.setAttribute("role", "group");
    this.tops = TOPS.map((top) => {
      const node = button("section-chip");
      node.textContent = `${top} hPa`;
      node.dataset.top = String(top);
      node.addEventListener("click", () => handlers.onTop(top));
      this.topGroup.append(node);
      return { top, node };
    });

    this.status.append(this.notice, this.retry);
    this.headline.append(this.title, this.ends, this.length, this.status);
    const controls = element("div", "section-controls");
    controls.append(this.fill, ...this.switches.map(({ node }) => node), this.topGroup);
    const head = element("div", "section-head");
    head.append(this.headline, controls, this.close);

    this.plot.tabIndex = 0;
    this.plot.setAttribute("role", "group");
    this.plot.append(this.vertical, this.horizontal, this.tip);
    const body = element("div", "section-body");
    body.append(this.pressures, this.plot, this.heights, this.distances);

    const scale = element("div", "section-scale");
    scale.append(this.bar, this.barTicks);
    this.legend.append(scale, this.barUnit, this.terrain);
    this.root.append(head, body, this.legend, this.spoken);

    this.plot.addEventListener("pointermove", (event) => this.point(event));
    this.plot.addEventListener("pointerdown", (event) => {
      // A finger that leaves the plot keeps reading its edge.
      this.plot.setPointerCapture(event.pointerId);
      this.point(event);
    });
    this.plot.addEventListener("pointerleave", (event) => {
      // A lifted finger leaves its reading; a mouse takes it along.
      if (event.pointerType === "mouse") this.setCursor(null);
    });
    this.plot.addEventListener("keydown", (event) => this.key(event));
    this.plot.addEventListener("blur", () => this.setCursor(null));
    new ResizeObserver(() => this.measure()).observe(this.plot);
    this.watchDensity();
    window.addEventListener("resize", () => {
      this.placeStatus();
      this.measure();
    });
    this.showCursor();
  }

  /** A canvas for a renderer, which it is then no one else's. */
  freshCanvas(): HTMLCanvasElement {
    const made = element("canvas", "section-canvas");
    made.setAttribute("aria-hidden", "true");
    this.canvas?.remove();
    this.canvas = made;
    this.plot.prepend(made);
    return made;
  }

  /** Bring the panel in line with the section. */
  render(state: SectionPanelState): void {
    const points = this.state?.points;
    this.state = state;
    if (this.canvas) this.canvas.style.visibility = state.blank ? "hidden" : "visible";
    if (points !== state.points) this.setCursor(null);
    this.root.setAttribute("aria-label", t("sectionTitle"));
    this.root.dataset.collapsed = String(state.collapsed);
    this.text(this.title, t("sectionTitle"));
    this.text(this.ends, `A ${sectionPosition(state.line.a, 2)} – B ${sectionPosition(state.line.b, 2)}`);
    this.text(this.length, state.length === null ? "" : formatDistance(state.length));
    this.text(this.notice, state.notices.join(" · "));
    this.retry.hidden = !state.retry;
    this.placeStatus();
    this.text(this.retry, t("retry"));
    this.close.setAttribute("aria-label", t("sectionCloseAria"));
    this.fill.setAttribute("aria-label", t("sectionFillAria"));
    for (const [index, fill] of SECTION_FILLS.entries()) {
      const option = this.fill.options[index]!;
      this.text(option, fillLabel(fill));
      option.disabled = !state.fills.includes(fill);
    }
    // An open list is closed by being written to.
    if (this.fill.value !== state.options.fill) this.fill.value = state.options.fill;
    for (const { name, node } of this.switches) {
      this.text(node, switchLabel(name));
      node.setAttribute("aria-pressed", String(name === "barbs" ? state.options.barbs : state.options.lines[name]));
      if (name === "theta") node.setAttribute("aria-label", t("sectionThetaAria"));
    }
    this.topGroup.setAttribute("aria-label", t("sectionTopAria"));
    for (const { top, node } of this.tops) node.setAttribute("aria-pressed", String(top === state.options.top));
    this.plot.setAttribute("aria-label", t("sectionPlotAria"));
    this.text(this.terrain, state.step === null ? "" : t("sectionTerrainNote", { step: `${Number(state.step.toFixed(2))}°` }));
    this.renderLegend(state.options.fill);
    this.renderAxes();
    this.showCursor();
  }

  /** Say something to who cannot see the panel. */
  announce(text: string): void {
    this.text(this.spoken, text);
  }

  /** What the panel shows, in a line. */
  summary(): string {
    return [this.title, this.ends, this.length].map(({ textContent }) => textContent).join(" · ");
  }

  /** What is read at the crosshair, said aloud too with `speak`; null clears
   * the words and leaves the crosshair. */
  showReading(reading: SectionReading | null, speak: boolean): void {
    const points = this.state?.points;
    const at = this.cursor && points?.[this.cursor.point];
    if (!reading || !at || this.vertical.hidden) {
      this.tip.hidden = true;
      return;
    }
    const lines = readingLines(reading, sectionPosition([at.lon, at.lat], 2));
    this.write("tip", lines.join("\n"), () => {
      this.tip.replaceChildren(...lines.map((line) => element("span", "section-tip-line", line)));
    });
    this.tip.hidden = false;
    this.placeTip();
    if (speak) this.announce(lines.join(", "));
  }

  /** Take the crosshair off the chart. */
  clearCursor(): void {
    this.setCursor(null);
  }

  private text(node: HTMLElement, text: string): void {
    if (node.textContent !== text) node.textContent = text;
  }

  /** Do `make` where a part's contents are to be other than they are. */
  private write(part: string, key: string, make: () => void): void {
    if (this.written.get(part) === key) return;
    this.written.set(part, key);
    make();
  }

  private placeStatus(): void {
    const inHeadline = this.state?.collapsed !== false || window.innerWidth < WIDE_WINDOW;
    const host = inHeadline ? this.headline : this.legend;
    if (this.status.parentElement === host) return;
    if (inHeadline) host.append(this.status);
    else this.terrain.before(this.status);
  }

  /** How wide a label of the distance axis is. */
  private labelWidth(place: string, distance: string): number {
    if (!this.ruler.isConnected) this.distances.append(this.ruler);
    this.ruler.replaceChildren(element("span", "section-distance-place", place), element("span", "section-distance-length", distance));
    return this.ruler.offsetWidth;
  }

  /** A window moved to a screen of another density keeps its size in CSS
   * pixels, and nothing else says so. */
  private watchDensity(): void {
    if (typeof window.matchMedia !== "function") return;
    window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener(
      "change",
      () => {
        this.measure();
        this.watchDensity();
      },
      { once: true },
    );
  }

  private measure(): void {
    const { clientWidth, clientHeight } = this.plot;
    if (clientWidth === 0 || clientHeight === 0) return;
    this.width = clientWidth;
    this.height = clientHeight;
    this.handlers.onResize(clientWidth, clientHeight);
    this.renderAxes();
    this.showCursor();
  }

  private renderAxes(): void {
    const { state, width, height } = this;
    if (!state || width === 0) return;
    const { top } = state.options;
    this.write("pressures", `${top} ${height}`, () => {
      this.pressures.replaceChildren(
        ...axisPressures(top, height).map((pressure) => {
          const label = element("span", "section-tick", String(pressure));
          label.style.top = `${pressureY(pressure, top, height)}px`;
          return label;
        }),
      );
    });
    const heights = axisHeights(state.heights, top, height);
    this.write(
      "heights",
      `${top} ${height} ${units.height} ${heights.map(({ metres, pressure }) => `${metres}:${pressure.toFixed(1)}`).join()}`,
      () => {
        this.heights.replaceChildren(
          ...heights.map(({ metres, pressure }) => {
            const label = element("span", "section-tick", axisHeight(metres));
            label.style.top = `${pressureY(pressure, top, height)}px`;
            return label;
          }),
        );
      },
    );
    const { points, length } = state;
    if (!points || length === null) {
      this.write("distances", "", () => this.distances.replaceChildren());
      return;
    }
    this.write("distances", `${width} ${units.distance} ${coordinateFormat} ${state.line.a} ${state.line.b} ${length}`, () => {
      const pointAt = (share: number): SectionPoint => points[Math.round(share * (points.length - 1))]!;
      const full = (share: number): string => sectionPosition([pointAt(share).lon, pointAt(share).lat], 1);
      const along = (share: number): string => axisDistance(share * length);
      const varying = varyingCoordinate(points);
      // Where there is no room for a mark between the ends, the coordinate
      // that changes is enough: the ends are written out in the header.
      const short = (share: number): string =>
        varying === "lat" ? formatPointDegrees(pointAt(share).lat, "NS", 1) : formatPointDegrees(turned(pointAt(share).lon), "EW", 1);
      const steps = (place: (share: number) => string): number => axisSteps(width, (share) => this.labelWidth(place(share), along(share)));
      let place = full;
      let count = steps(full);
      if (count < 2) {
        const fewer = steps(short);
        if (fewer > count) {
          place = short;
          count = fewer;
        }
      }
      const shares = Array.from({ length: Math.max(1, count) + 1 }, (_, index) => index / Math.max(1, count));
      this.distances.replaceChildren(
        ...shares.map((share) => {
          const label = element("span", "section-distance");
          label.style.left = `${share * 100}%`;
          label.dataset.side = share === 0 ? "start" : share === 1 ? "end" : "middle";
          label.append(
            element("span", "section-distance-place", place(share)),
            element("span", "section-distance-length", along(share)),
          );
          return label;
        }),
      );
    });
  }

  private renderLegend(fill: SectionFill): void {
    this.write("legend", `${fill} ${units.temperature} ${units.wind}`, () => {
      const legend = sectionLegend(fill);
      const { min, max, stops } = legend;
      const share = (value: number): string => `${(((value - min) / (max - min)) * 100).toFixed(2)}%`;
      this.bar.style.background = `linear-gradient(to right, ${stops
        .map(({ from, to, color }) => `${color} ${share(from)} ${share(to)}`)
        .join(", ")})`;
      this.barTicks.replaceChildren(
        ...legendTicks(legend).map(({ value, label: text }) => {
          const label = element("span", "section-bar-tick", text);
          label.style.left = share(value);
          return label;
        }),
      );
      this.barUnit.dataset.fill = fill;
    });
    const unit = legendUnit(sectionLegend(fill).unit);
    this.text(this.barUnit, fill === "cross" ? `${unit} · ${t("sectionCrossNote")}` : unit);
  }

  private point(event: PointerEvent): void {
    const points = this.state?.points;
    if (!points || this.width === 0) return;
    const box = this.plot.getBoundingClientRect();
    this.moveTo(event.clientX - box.left, event.clientY - box.top);
  }

  private moveTo(x: number, y: number): void {
    const { state } = this;
    if (!state?.points) return;
    const inX = Math.min(this.width, Math.max(0, x));
    const inY = Math.min(this.height, Math.max(0, y));
    this.setCursor({
      point: xPoint(inX, state.points.length, this.width),
      pressure: yPressure(inY, state.options.top, this.height),
    });
  }

  private key(event: KeyboardEvent): void {
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    const { state } = this;
    if (!step || !state?.points) return;
    event.preventDefault();
    const count = state.points.length;
    const at = this.cursor
      ? { x: pointX(this.cursor.point, count, this.width), y: pressureY(this.cursor.pressure, state.options.top, this.height) }
      : { x: this.width / 2, y: this.height / 2 };
    // Never less than one point: a step that rounds back would not move.
    const across = Math.max(KEY_STEP, this.width / Math.max(1, count - 1));
    this.moveTo(at.x + step[0]! * across, at.y + step[1]! * KEY_STEP);
  }

  private setCursor(cursor: SectionCursor | null): void {
    if (!cursor && !this.cursor) return;
    this.cursor = cursor;
    if (!cursor) this.tip.hidden = true;
    this.showCursor();
    this.handlers.onCursor(cursor);
  }

  private showCursor(): void {
    const { cursor, state } = this;
    const shown = cursor !== null && state?.points != null && this.width > 0 && cursor.pressure >= state.options.top;
    this.vertical.hidden = !shown;
    this.horizontal.hidden = !shown;
    if (!shown) {
      this.tip.hidden = true;
      return;
    }
    this.vertical.style.left = `${pointX(cursor.point, state.points!.length, this.width)}px`;
    this.horizontal.style.top = `${pressureY(cursor.pressure, state.options.top, this.height)}px`;
    this.placeTip();
  }

  /** The reading beside the crosshair, on the side with more room. */
  private placeTip(): void {
    const { cursor, state } = this;
    if (this.tip.hidden || !cursor || !state?.points) return;
    const x = pointX(cursor.point, state.points.length, this.width);
    const y = pressureY(cursor.pressure, state.options.top, this.height);
    const left = x > this.width / 2 ? x - this.tip.offsetWidth - 10 : x + 10;
    const top = Math.max(2, Math.min(this.height - this.tip.offsetHeight - 2, y - this.tip.offsetHeight / 2));
    this.tip.style.left = `${Math.max(0, left)}px`;
    this.tip.style.top = `${top}px`;
  }
}
