/** The bar between the map and the charts: playing through time, the three
 * coordinates with a step either way, and how the charts are drawn. */

import type { SectionFill } from "../crosssection/types";
import { legendTicks, legendUnit } from "../crosssection/displayunits";
import { sectionLegend } from "../crosssection/ramps";
import { element } from "../dom";
import { t, type MessageKey } from "../i18n";
import { MAP_SIZES, type MapSize } from "./layout";
import { FILLS, SPANS, TOPS } from "./state";

export type Coordinate = "where" | "when" | "level";
export type BarSwitch = "wind" | "zero" | "theta" | "barbs";
/** The charts of an arrangement that does not show them all. */
export type Shown = "along" | "profile" | "through";

export interface BarState {
  where: [string, string];
  when: [string, string];
  level: [string, string];
  fill: SectionFill;
  hasLine: boolean;
  fills: readonly SectionFill[];
  top: number;
  span: number;
  switches: Record<BarSwitch, boolean>;
  /** The charts to choose between, and the one chosen; none to choose. */
  choices: readonly Shown[];
  shown: Shown;
  /** The map's size; null where there is no choice of it. */
  size: MapSize | null;
  /** Whether time is being played; null for nothing to play. */
  playing: boolean | null;
}

export interface BarHandlers {
  step(coordinate: Coordinate, direction: 1 | -1): void;
  fill(fill: SectionFill): void;
  top(top: number): void;
  span(span: number): void;
  toggle(name: BarSwitch): void;
  show(shown: Shown): void;
  size(size: MapSize): void;
  play(): void;
}

const CHEVRON: Record<"left" | "right" | "up" | "down", string> = {
  left: "M8 2.5L4.5 6 8 9.5",
  right: "M4.5 2.5L8 6l-3.5 3.5",
  up: "M2.5 8L6 4.5 9.5 8",
  down: "M2.5 4.5L6 8l3.5-3.5",
};

function button(className: string): HTMLButtonElement {
  const node = element("button", className);
  node.type = "button";
  return node;
}

function arrow(direction: keyof typeof CHEVRON): HTMLButtonElement {
  const node = button("wb-step");
  node.innerHTML = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${CHEVRON[direction]}" /></svg>`;
  return node;
}

/** The map's share of the square drawn filled, from the top: rows of 14. */
const SIZE_FILL: Record<MapSize, [number, number]> = { map: [2, 9], even: [2, 7], charts: [2, 4] };
const SIZE_NAME: Record<MapSize, MessageKey> = { map: "wbMapMost", even: "wbMapEven", charts: "wbChartsMost" };

const PLAY = "M4 2l11 7-11 7z";
const PAUSE = "M4 2h4v14H4zM10 2h4v14h-4z";

function sizeIcon(size: MapSize): string {
  const [y, h] = SIZE_FILL[size];
  return `<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><rect x="1.75" y="1.75" width="14.5" height="14.5" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="2" y="${y}" width="14" height="${h}" fill="currentColor"/></svg>`;
}

export function fillLabel(fill: SectionFill): string {
  const keys: Record<SectionFill, MessageKey> = { thetae: "tipThetaE", tmp: "tipTemp", rh: "tipHumidity", wind: "sectionFillWind", cross: "sectionFillCross" };
  return t(keys[fill]);
}

const SWITCHES: readonly BarSwitch[] = ["wind", "zero", "theta", "barbs"];
const COORDINATES: readonly { id: Coordinate; name: MessageKey; back: MessageKey; on: MessageKey }[] = [
  { id: "where", name: "wbWhere", back: "wbBack", on: "wbForward" },
  { id: "when", name: "wbWhen", back: "wbEarlier", on: "wbLater" },
  { id: "level", name: "wbHeight", back: "wbLower", on: "wbHigher" },
];

export class Bar {
  readonly root = element("div", "wb-bar");
  private readonly cells = COORDINATES.map(({ id, name, back, on }) => {
    const root = element("div", `wb-coordinate wb-${id}`);
    const label = element("span", "wb-coordinate-name");
    const value = element("b", "wb-coordinate-value");
    const note = element("span", "wb-coordinate-note");
    const less = arrow(id === "level" ? "down" : "left");
    const more = arrow(id === "level" ? "up" : "right");
    less.addEventListener("click", () => this.handlers.step(id, -1));
    more.addEventListener("click", () => this.handlers.step(id, 1));
    const words = element("div", "wb-coordinate-words");
    words.append(label, value, note);
    root.append(words, less, more);
    return { id, name, back, on, root, label, value, note, less, more };
  });
  private readonly shape = element("div", "wb-group wb-choices");
  private readonly choices: { id: Shown; node: HTMLButtonElement }[] = (["along", "profile", "through"] as const).map((id) => {
    const node = button("section-chip");
    node.addEventListener("click", () => this.handlers.show(id));
    this.shape.append(node);
    return { id, node };
  });
  private readonly fill = element("select", "section-fill");
  private readonly scale = element("div", "wb-scale");
  private readonly switches = SWITCHES.map((name) => {
    const node = button("section-chip");
    node.addEventListener("click", () => this.handlers.toggle(name));
    return { name, node };
  });
  private readonly tops = TOPS.map((top) => {
    const node = button("section-chip");
    node.textContent = `${top} hPa`;
    node.addEventListener("click", () => this.handlers.top(top));
    return { top, node };
  });
  private readonly spans = SPANS.map((span) => {
    const node = button("section-chip");
    node.addEventListener("click", () => this.handlers.span(span));
    return { span, node };
  });
  private readonly sizes = MAP_SIZES.map((size) => {
    const node = button("wb-size");
    node.innerHTML = sizeIcon(size);
    node.addEventListener("click", () => this.handlers.size(size));
    return { size, node };
  });
  private readonly sizeGroup = element("div", "wb-group wb-sizes");
  private readonly topGroup = element("div", "wb-group");
  private readonly spanGroup = element("div", "wb-group");
  private legendKey = "";
  private readonly play = button("wb-play");
  private playing: boolean | null = null;

  constructor(private readonly handlers: BarHandlers) {
    for (const fill of FILLS) {
      const option = document.createElement("option");
      option.value = fill;
      this.fill.append(option);
    }
    this.fill.addEventListener("change", () => handlers.fill(this.fill.value as SectionFill));
    for (const group of [this.shape, this.topGroup, this.spanGroup, this.sizeGroup]) group.setAttribute("role", "group");
    this.topGroup.append(...this.tops.map(({ node }) => node));
    this.spanGroup.append(...this.spans.map(({ node }) => node));
    this.sizeGroup.append(...this.sizes.map(({ node }) => node));
    const switches = element("div", "wb-group");
    switches.append(...this.switches.map(({ node }) => node));
    const controls = element("div", "wb-controls");
    controls.append(this.shape, this.fill, this.scale, switches, this.spanGroup, this.topGroup, this.sizeGroup);
    this.play.addEventListener("click", () => handlers.play());
    const coordinates = element("div", "wb-coordinates");
    coordinates.append(this.play, ...this.cells.map(({ root }) => root));
    this.root.append(coordinates, controls);
  }

  render(state: BarState): void {
    this.play.disabled = state.playing === null;
    if (this.playing !== state.playing) {
      this.playing = state.playing;
      this.play.innerHTML = `<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="${state.playing ? PAUSE : PLAY}" fill="currentColor"/></svg>`;
    }
    this.play.setAttribute("aria-label", t(state.playing ? "pauseAnimation" : "playAnimation"));
    this.play.title = this.play.getAttribute("aria-label")!;
    for (const cell of this.cells) {
      const [value, note] = state[cell.id];
      cell.label.textContent = t(cell.name);
      cell.value.textContent = value;
      cell.note.textContent = note;
      cell.less.setAttribute("aria-label", t(cell.back));
      cell.more.setAttribute("aria-label", t(cell.on));
    }
    this.shape.hidden = state.choices.length < 2;
    const names: Record<Shown, MessageKey> = { along: "wbAlong", profile: "wbHere", through: "wbThrough" };
    for (const { id, node } of this.choices) {
      node.hidden = !state.choices.includes(id);
      node.textContent = t(names[id]);
      node.setAttribute("aria-pressed", String(id === state.shown));
    }
    this.fill.setAttribute("aria-label", t("sectionFillAria"));
    for (const [index, fill] of FILLS.entries()) {
      const option = this.fill.options[index]!;
      option.textContent = fillLabel(fill);
      option.hidden = fill === "cross" && !state.hasLine;
      option.disabled = option.hidden || !state.fills.includes(fill);
    }
    if (this.fill.value !== state.fill) this.fill.value = state.fill;
    for (const { name, node } of this.switches) {
      node.textContent =
        name === "wind" ? t("sectionIsotachs") : name === "zero" ? t("sectionZeroLine") : name === "theta" ? "θ" : t("sectionBarbs");
      if (name === "theta") node.setAttribute("aria-label", t("sectionThetaAria"));
      node.setAttribute("aria-pressed", String(state.switches[name]));
    }
    this.topGroup.setAttribute("aria-label", t("sectionTopAria"));
    for (const { top, node } of this.tops) node.setAttribute("aria-pressed", String(top === state.top));
    this.spanGroup.setAttribute("aria-label", t("wbSpanAria"));
    for (const { span, node } of this.spans) {
      node.textContent = t("wbDays", { count: span / 24 });
      node.setAttribute("aria-pressed", String(span === state.span));
    }
    this.sizeGroup.hidden = state.size === null;
    this.sizeGroup.setAttribute("aria-label", t("wbMapSizeAria"));
    for (const { size, node } of this.sizes) {
      node.setAttribute("aria-label", t(SIZE_NAME[size]));
      node.title = t(SIZE_NAME[size]);
      node.setAttribute("aria-pressed", String(size === state.size));
    }
    this.renderScale(state.fill);
  }

  private renderScale(fill: SectionFill): void {
    const legend = sectionLegend(fill);
    const ticks = legendTicks(legend);
    const unit = legendUnit(legend.unit);
    const key = `${fill} ${unit} ${ticks.map(({ label }) => label).join()}`;
    if (key === this.legendKey) return;
    this.legendKey = key;
    const share = (value: number): string => `${(((value - legend.min) / (legend.max - legend.min)) * 100).toFixed(2)}%`;
    const band = element("div", "wb-scale-band");
    band.style.background = `linear-gradient(to right, ${legend.stops.map(({ from, to, color }) => `${color} ${share(from)} ${share(to)}`).join(", ")})`;
    const low = element("span", "wb-scale-end", ticks[0]?.label ?? "");
    const high = element("span", "wb-scale-end", `${ticks[ticks.length - 1]?.label ?? ""} ${unit}`);
    this.scale.replaceChildren(low, band, high);
    this.scale.title = ticks.map(({ label }) => label).join(" · ");
  }
}
