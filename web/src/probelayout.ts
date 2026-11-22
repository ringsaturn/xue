/**
 * How the pinned point's panel shares the screen with the map.
 *
 * The panel holds two kinds of thing. The meteogram is wide and short and
 * belongs over the transport, on its time axis. A sounding is a square, and
 * stacked under the meteogram it made a slab over the middle of the map —
 * where the pinned point and the weather around it are. So the two do not
 * stack:
 *
 * - `side`, on a wide window: the sounding's body is docked in a card beside
 *   the map and the panel keeps the rows, so the two are read together and
 *   the middle of the map stays clear. Where the centred capsule would reach
 *   under the card, the capsule and the panel move over together
 *   (`--dock-right`), which keeps the rows on the transport's axis.
 * - `tabs`, on a phone or a narrow window: the panel shows one of the
 *   meteogram, the observations and the sounding at a time, so it is never
 *   taller than the tallest of them. A phone's panel also takes only half
 *   the screen until its reader asks for all of it.
 *
 * Whichever it is, a pin the panel or the card has come to cover is brought
 * back into the map that is left (`aim`).
 */

import type { Map as MaplibreMap } from "maplibre-gl";
import { t } from "./i18n";
import type { SoundingSection } from "./sounding/section";
import type { ObservationSection } from "./stations/obssection";

export type ProbeLayoutMode = "side" | "tabs";
export type ProbePage = "rows" | "obs" | "sounding";

/** At and under this width the panel is a phone's: half the screen's height
 * until asked for more. The stylesheet's phone rules start at the same one. */
const PHONE_WIDTH = 720;
/** Under this width there is no room for a card and a panel worth reading
 * beside it, and the panel takes the tabs. */
const SIDE_MIN_WIDTH = 1120;
/** The card's width, and where it stands: under the row of buttons at the
 * top, inside the column of controls on the right edge. The stylesheet has
 * the same three numbers. */
const CARD_WIDTH = 388;
const CARD_TOP = 72;
const CARD_RIGHT = 76;
/** What the card takes of the screen's width, with the gap beside it. */
const CARD_DOCK = CARD_WIDTH + CARD_RIGHT + 12;
/** The transport capsule's widest, which the panel shares (`--capsule-width`). */
const CAPSULE_WIDTH = 960;
/** How near an edge of the clear map a pin may sit before it is moved. */
const CLEARANCE = 24;
/** The least clear map, either way, worth moving a pin into. */
const MIN_CLEAR = 80;

/** Which layout a window of this width takes. */
export function probeLayoutFor(width: number): ProbeLayoutMode {
  return width < SIDE_MIN_WIDTH ? "tabs" : "side";
}

/** Whether a card shown in a window of this width stands where the centred
 * capsule and panel would reach, so that they have to move over for it. */
export function cardCrowdsCapsule(width: number): boolean {
  return width < CAPSULE_WIDTH + 2 * CARD_DOCK;
}

/**
 * The page the tabs show, from the one they showed and what the sections
 * have. The sounding opens itself for a pin beside a station while the
 * marks are on, and for a station card's own button, so an open sounding is
 * the page whatever was showing; a page whose section has gone falls back
 * to the rows.
 */
export function probePageFor(
  page: ProbePage,
  has: { observations: boolean; sounding: boolean; soundingOpen: boolean },
): ProbePage {
  if (has.sounding && has.soundingOpen) return "sounding";
  if (page === "sounding") return "rows";
  if (page === "obs" && !has.observations) return "rows";
  return page;
}

export interface ScreenBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/**
 * Where a covered pin goes: the offset from the map's centre of the middle
 * of the map left clear — above the panel and left of the card — or null
 * when the pin is in the clear already, or there is too little clear map to
 * move it into. A pin that is merely near the screen's edge is left alone:
 * the reader put it there.
 */
export function aimOffset(
  point: { x: number; y: number },
  map: ScreenBox,
  panel: ScreenBox,
  card: ScreenBox | null,
): [number, number] | null {
  const under = (box: ScreenBox): boolean =>
    point.x > box.left - CLEARANCE &&
    point.x < box.right + CLEARANCE &&
    point.y > box.top - CLEARANCE &&
    point.y < box.bottom + CLEARANCE;
  const offScreen = point.x < map.left || point.x > map.right || point.y < map.top || point.y > map.bottom;
  if (!offScreen && !under(panel) && !(card && under(card))) return null;
  const clear = {
    left: map.left + CLEARANCE,
    right: (card ? card.left : map.right) - CLEARANCE,
    top: map.top + CARD_TOP,
    bottom: panel.top - CLEARANCE,
  };
  if (clear.right - clear.left < MIN_CLEAR || clear.bottom - clear.top < MIN_CLEAR) return null;
  return [
    (clear.left + clear.right) / 2 - (map.left + map.right) / 2,
    (clear.top + clear.bottom) / 2 - (map.top + map.bottom) / 2,
  ];
}

export interface ProbeLayoutOptions {
  map: MaplibreMap;
  /** The panel, and its head: the tabs are the head's last row, so they stay
   * in view with it while a tall page scrolls. */
  panel: HTMLElement;
  head: HTMLElement;
  /** The panel's close button: the phone's grow button goes before it. */
  close: HTMLElement;
  observation: ObservationSection;
  sounding: SoundingSection;
  /** The pinned point, or null with nothing pinned. */
  pin(): { longitude: number; latitude: number } | null;
}

export interface ProbeLayout {
  /** Bring the tabs, the card and the pages in line with what the sections
   * have to show. Called at every render of the panel. */
  sync(): void;
  /** Move the map, if the pin is under the panel or the card, so that it
   * sits in the map left clear. */
  aim(): void;
  /** How tall the sounding's chart is drawn where its body is now. */
  chartHeight(): number;
}

const PAGES: readonly ProbePage[] = ["rows", "obs", "sounding"];

/** A page's name on its tab. The sounding's is the word its stations go by. */
function pageLabel(page: ProbePage): string {
  if (page === "rows") return t("probeTabSeries");
  if (page === "obs") return t("probeTabObservations");
  return t("stationSounding");
}

function button(className: string): HTMLButtonElement {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  return node;
}

export function createProbeLayout(options: ProbeLayoutOptions): ProbeLayout {
  const { map, panel, head, observation, sounding } = options;
  const parent = panel.parentElement!;

  const tabs = document.createElement("div");
  tabs.className = "probe-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.hidden = true;
  const tabButtons = PAGES.map((id) => {
    const node = button("probe-tab");
    node.setAttribute("role", "tab");
    node.dataset.page = id;
    node.addEventListener("click", () => show(id));
    tabs.append(node);
    return { id, node };
  });
  head.append(tabs);

  const card = document.createElement("aside");
  card.className = "sounding-card";
  card.hidden = true;
  const cardHead = document.createElement("div");
  cardHead.className = "sounding-card-head";
  // The code the panel's own row carries, which is what opened the card.
  const cardCode = document.createElement("span");
  cardCode.className = "sounding-head-code";
  cardCode.textContent = "SOUNDING";
  const cardCaption = document.createElement("span");
  cardCaption.className = "sounding-card-caption";
  const cardClose = button("probe-close sounding-card-close");
  cardClose.innerHTML =
    '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8" /></svg>';
  cardClose.addEventListener("click", () => sounding.close());
  cardHead.append(cardCode, cardCaption, cardClose);
  const cardBody = document.createElement("div");
  cardBody.className = "sounding-card-body";
  card.append(cardHead, cardBody);
  parent.append(card);

  const grow = button("probe-grow");
  grow.innerHTML =
    '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 7.5L6 4l3.5 3.5" /></svg>';
  options.close.before(grow);

  let applied: ProbeLayoutMode | null = null;
  let page: ProbePage = "rows";
  /** Whether a phone's panel has been given the whole screen. */
  let tall = false;
  /** The room the card has, top to bottom, where it now stands. */
  let cardRoom = 0;
  /** What the panel and the card last took of the map, so the chart is
   * redrawn and the pin aimed again only when that has changed. */
  let footprint = "";

  const phone = (): boolean => window.innerWidth <= PHONE_WIDTH;

  /** The chart's height on the sounding's page of a wide panel: shorter on
   * a short screen, so that some map is left above the panel. */
  const wideChartHeight = (): number => Math.max(200, Math.min(300, window.innerHeight - 480));

  /** Turn to a page, by a press on its tab. Leaving the sounding folds it:
   * a chart nobody is looking at should not keep reading the run's levels. */
  function show(next: ProbePage): void {
    if (page === next) return;
    page = next;
    panel.dataset.page = page;
    if (next === "sounding") sounding.open();
    else sounding.close();
    sync();
  }

  /** Give the card the height it has: the screen's where it stands beside
   * the panel, and down to the panel's top where the two would overlap. */
  function placeCard(): void {
    const box = parent.getBoundingClientRect();
    const panelBox = panel.getBoundingClientRect();
    const left = box.right - CARD_RIGHT - CARD_WIDTH;
    const overlaps = !panel.hidden && left < panelBox.right + 12;
    const floor = overlaps ? panelBox.top - 12 : box.bottom - 24;
    cardRoom = Math.max(220, Math.round(floor - (box.top + CARD_TOP)));
    card.style.maxHeight = `${cardRoom}px`;
  }

  function sync(): void {
    const layout = probeLayoutFor(window.innerWidth);
    const pinned = !panel.hidden;
    if (layout !== applied) {
      applied = layout;
      panel.dataset.layout = layout;
      sounding.dock(layout === "side" ? cardBody : null);
      page = sounding.isOpen() ? "sounding" : "rows";
    }
    const hasObservations = !observation.root.hidden;
    const hasSounding = !sounding.root.hidden;
    if (layout === "tabs") {
      page = probePageFor(page, { observations: hasObservations, sounding: hasSounding, soundingOpen: sounding.isOpen() });
      observation.setOpen(page === "obs");
      card.hidden = true;
    } else {
      // Empty while the full-chart sheet has borrowed the body.
      const shown = pinned && hasSounding && sounding.isOpen() && cardBody.childElementCount > 0;
      if (shown) {
        const caption = sounding.caption();
        if (cardCaption.textContent !== caption) cardCaption.textContent = caption;
      }
      card.hidden = !shown;
    }
    if (!card.hidden && cardCrowdsCapsule(window.innerWidth)) parent.style.setProperty("--dock-right", `${CARD_DOCK}px`);
    else parent.style.removeProperty("--dock-right");

    panel.dataset.page = page;
    tabs.hidden = layout !== "tabs" || !(hasObservations || hasSounding);
    for (const { id, node } of tabButtons) {
      node.hidden = (id === "obs" && !hasObservations) || (id === "sounding" && !hasSounding);
      node.setAttribute("aria-selected", String(id === page));
      // The locale changes in place; the panel is rendered again when it does.
      const label = pageLabel(id);
      if (node.textContent !== label) node.textContent = label;
    }
    const half = phone() && !tall;
    panel.dataset.tall = String(!half);
    // The notes beside the chart run as long as the chart does.
    panel.style.setProperty("--sounding-notes-height", `${wideChartHeight() + 32}px`);
    grow.setAttribute("aria-label", t(half ? "probeGrowAria" : "probeShrinkAria"));
    grow.setAttribute("aria-pressed", String(!half));
    cardClose.setAttribute("aria-label", t("soundingCloseAria"));

    const next = pinned ? `${layout}|${page}|${card.hidden}|${half}|${window.innerWidth}x${window.innerHeight}` : "";
    if (next === footprint) return;
    footprint = next;
    if (!pinned) return;
    placeCard();
    // The chart was last drawn for the room it had before.
    sounding.render();
    aim();
  }

  function aim(): void {
    const at = options.pin();
    if (!at || panel.hidden) return;
    // After the layout this change asked for has happened.
    requestAnimationFrame(() => {
      if (panel.hidden) return;
      const box = map.getContainer().getBoundingClientRect();
      const point = map.project([at.longitude, at.latitude]);
      const offset = aimOffset(
        { x: box.left + point.x, y: box.top + point.y },
        box,
        panel.getBoundingClientRect(),
        card.hidden ? null : card.getBoundingClientRect(),
      );
      if (!offset) return;
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      map.easeTo({ center: [at.longitude, at.latitude], offset, duration: reduced ? 0 : 450 });
    });
  }

  grow.addEventListener("click", () => {
    tall = !tall;
    sync();
  });
  window.addEventListener("resize", sync);
  // The panel grows and shrinks with what is unfolded in it and with the
  // rows that arrive after the pin (an airport's, a station's): a card that
  // stands above it has what is left, and a pin it has grown over is moved.
  // A panel that shrank uncovers and never covers, so only growth aims.
  let panelHeight = 0;
  new ResizeObserver(() => {
    if (applied === "side" && !card.hidden) placeCard();
    const height = panel.offsetHeight;
    const grew = height > panelHeight + 4;
    panelHeight = height;
    if (grew) aim();
  }).observe(panel);

  return {
    sync,
    aim,
    chartHeight() {
      if (applied === "side") return Math.max(220, Math.min(320, cardRoom - 170));
      // At half height the chart and the button under it fit the panel
      // without a scroll; the full height has the panel's usual chart.
      if (phone()) return tall ? 320 : 200;
      return wideChartHeight();
    },
  };
}
