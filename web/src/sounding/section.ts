/**
 * The sounding section of the probe panel: the nearest radiosonde ascent,
 * drawn as a skew-T under the meteogram rows.
 *
 * `skewt.ts` draws into a context a caller owns and knows nothing about the
 * shell; this is that caller for the panel, as `dev.ts` is for the
 * workbench. It owns the section's DOM, the station whose line it has
 * fetched, which of that station's nominal times is selected, and the ink
 * it resolves from the panel's own custom properties at every draw — so a
 * theme switch repaints the chart in place, like everything else the shell
 * repaints rather than reloads.
 *
 * What it deliberately does not own: the model column. That comes from the
 * run on screen, through probe sessions main.ts opens and reads, and is
 * handed in through `modelProfile()` at draw time. The section asks; it
 * never fetches a forecast, and never moves the playhead — the selected
 * ascent is a time of its own, and the transport below is untouched by it.
 *
 * Opening is what costs anything: the station's whole window is one range
 * request, made once, on the first open. A section that stays closed —
 * which is what a phone-width viewport starts as, where a 320 px chart
 * would be the whole screen — fetches nothing and opens no session.
 */

import { t } from "../i18n";
import { fetchSoundingStation, type LoadedSoundingIndex } from "../stations/fetch";
import type { SoundingStation, SoundingStationEntry } from "../stations/schema";
import { parcelPath, profileFromSounding, type Profile } from "./profile";
import { drawSkewT, readoutAt, skewTLayout, toXY, type SkewTInk, type SkewTLayout } from "./skewt";

/** The chart's height in the panel, CSS pixels: tall enough for the
 * troposphere to read at a glance, short enough that the panel over the
 * capsule stays a panel. */
const CHART_HEIGHT = 320;
/** Room around the plot for the pressure labels (left), the wind column
 * (right) and the temperature labels (bottom). */
const MARGIN = { left: 32, top: 12, right: 44, bottom: 22 } as const;
/** The chart's instrument face, the panel's own. */
const FONT = '9px "IBM Plex Mono", ui-monospace, monospace';
/** Below this the panel is a phone's, and the section starts closed. */
const PHONE_WIDTH = 720;
/** Below this viewport height the chart would take the panel past the top
 * of the screen, so the section starts closed there too. */
const SHORT_HEIGHT = 760;

export interface SoundingSectionOptions {
  /** The model column at the selected ascent's time, with the run it came
   * from, or null when the run publishes no isobaric levels or has no
   * frame near enough. Called at draw time, never cached. */
  modelProfile(): { profile: Profile; run: string; validTime: number } | null;
  /** A nominal time, formatted the way the panel formats a valid time. */
  formatTime(time: string): string;
  /** Whether the dataset on screen could supply a model column at all: a
   * forecast run with isobaric levels. A radar mosaic cannot, and the
   * legend then says nothing about a model rather than that none is near. */
  modelPossible(): boolean;
  /** Whether a fresh pin should open the chart on its own: true while the
   * sounding marks are switched on, since a viewer who asked to see the
   * stations wants the ascent; false otherwise, when the section waits
   * folded under the rows and a card's button or the header opens it. */
  wantsOpen(): boolean;
  /** Something the caller has to react to changed: the station, the
   * selected time, or whether the section is open. main.ts opens or drops
   * the model sessions on this and redraws. */
  onChange(): void;
}

export interface SoundingSection {
  root: HTMLElement;
  /** The button that opens the full-height copy — the sheet's trigger, so
   * `sheet.ts` owns the press, the focus and the `aria-expanded` exactly as
   * it does for the model and sources sheets. */
  expandButton: HTMLButtonElement;
  /** Show this station, or none. Passing the same station again only
   * updates the distance. */
  setStation(
    loaded: LoadedSoundingIndex | null,
    entry: SoundingStationEntry | null,
    distanceKm: number,
  ): void;
  /** The nominal time of the ascent on screen, or null when there is no
   * station, or the section is closed and has fetched nothing. */
  selectedTime(): string | null;
  /** Whether the chart is showing — what decides if a model column is
   * worth opening sessions for. */
  isOpen(): boolean;
  /** Show the chart, as a card's "open sounding" does after pinning the
   * probe at the station; a no-op when it is already showing. */
  open(): void;
  /** Repaint from the current state (a new frame, a new run, a theme or a
   * locale switch). */
  render(): void;
  /** Paint the same chart at another size — the sheet's copy. */
  drawInto(canvas: HTMLCanvasElement, width: number, height: number): void;
  /** What the sheet's heading names. */
  headline(): string;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * The chart's ink, out of the panel's own custom properties.
 *
 * `skewt.ts` takes colors rather than reading a stylesheet, which is what
 * lets it be tested and what lets the workbench hold both themes on screen
 * at once. The panel is the other kind of caller: it has a theme, the theme
 * is in CSS, and the values are read here at every draw so a switch needs
 * no state of its own. The fallbacks are the light theme's, for a context
 * where computed styles are not available.
 */
export function skewTInkFrom(node: Element): SkewTInk {
  const styles = getComputedStyle(node);
  const read = (name: string, fallback: string): string =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    ink: read("--text", "#1b1a17"),
    inkMuted: read("--muted", "#6e6a60"),
    grid: read("--skewt-grid", "rgba(27, 26, 23, 0.20)"),
    gridStrong: read("--skewt-grid-strong", "rgba(27, 26, 23, 0.45)"),
    paper: read("--skewt-paper", "#f3efe6"),
    model: read("--skewt-model", "#e8763a"),
    parcel: read("--skewt-parcel", "rgba(27, 26, 23, 0.5)"),
    temperature: read("--skewt-temperature", "#b4441a"),
    dew: read("--skewt-dew", "#0e7c72"),
    modelDew: read("--skewt-model-dew", "#3bb8a8"),
  };
}

/** Pixels per degree the chart is drawn at where it has the room — the
 * density the diagram's proportions are read at, and what the default
 * −40 … +45 °C range comes to on a chart as tall as it is wide. */
const DEGREES_PER_PIXEL = 1 / 4.4;
/** The temperature range never narrows below the classical one, and never
 * widens past what a troposphere needs. */
const SPAN_LIMITS: readonly [number, number] = [85, 190];
/** The midpoint of the default range, which every width keeps. */
const SPAN_CENTRE = 2.5;

/**
 * The plot rectangle inside a canvas of this size, and the temperature
 * range that fits it.
 *
 * The panel is as wide as the transport capsule and the chart is 320 px
 * tall, so the box the ascent is drawn in is much wider than it is tall —
 * and a skew-T drawn at a fixed −40 … +45 °C in that box has its isotherms
 * a centimetre apart and every profile lying almost flat. The range widens
 * with the width instead, about the same midpoint, so a degree is worth the
 * same few pixels whatever room the chart has and the diagram reads the
 * same in the panel and in the sheet.
 */
function layoutFor(width: number, height: number): SkewTLayout {
  const plotWidth = Math.max(40, width - MARGIN.left - MARGIN.right);
  const span = Math.min(SPAN_LIMITS[1], Math.max(SPAN_LIMITS[0], plotWidth * DEGREES_PER_PIXEL));
  return skewTLayout(
    {
      x: MARGIN.left,
      y: MARGIN.top,
      width: plotWidth,
      height: Math.max(40, height - MARGIN.top - MARGIN.bottom),
    },
    { tMin: SPAN_CENTRE - span / 2, tMax: SPAN_CENTRE + span / 2 },
  );
}

/** A legend swatch: the line's own mark in the line's own hue. */
function swatch(mark: string, token: string): HTMLElement {
  const node = document.createElement("i");
  node.className = "sounding-swatch";
  node.style.color = `var(${token})`;
  node.textContent = mark;
  return node;
}

/** The gap between the model frame and the ascent, as instrument text:
 * `+6 h` for a forecast hour after the launch, `−3 h` before it, `0 h` on
 * it. Whole hours, since both clocks are. */
function gapLabel(deltaMs: number): string {
  const hours = Math.round(deltaMs / 3600000);
  if (hours === 0) return "0 h";
  return `${hours > 0 ? "+" : "−"}${Math.abs(hours)} h`;
}

/** A station's name for the header: the WMO number a sounding is called by
 * on a chart, and the WIGOS id where the product has no number for it. */
function stationCode(entry: SoundingStationEntry): string {
  return entry.wmo ?? entry.id;
}

export function createSoundingSection(options: SoundingSectionOptions): SoundingSection {
  const root = element("section", "sounding-section");
  root.hidden = true;

  const head = document.createElement("button");
  head.type = "button";
  head.className = "sounding-head";
  head.setAttribute("aria-expanded", "false");
  const code = element("span", "sounding-head-code", "SOUNDING");
  const line = element("span", "sounding-head-line");
  const chevron = element("span", "sounding-chevron");
  chevron.setAttribute("aria-hidden", "true");
  chevron.innerHTML =
    '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5L5 6.5l2.5-3" /></svg>';
  head.append(code, line, chevron);

  const body = element("div", "sounding-body");
  body.hidden = true;
  const times = element("div", "sounding-times");
  times.setAttribute("role", "group");
  const canvas = element("canvas", "sounding-chart");
  canvas.setAttribute("aria-hidden", "true");
  const readout = element("output", "sounding-readout");
  const footer = element("div", "sounding-footer");
  const legend = element("span", "sounding-legend");
  const expand = document.createElement("button");
  expand.type = "button";
  expand.className = "sounding-expand";
  footer.append(legend, expand);
  body.append(times, canvas, readout, footer);
  root.append(head, body);

  let loaded: LoadedSoundingIndex | null = null;
  let entry: SoundingStationEntry | null = null;
  let distanceKm = 0;
  let station: SoundingStation | null = null;
  /** The station and ascent the fetch in flight (or done) is for, so a
   * station replaced while its bytes are on the wire never lands. */
  let fetchKey: string | null = null;
  let fetchSequence = 0;
  let failed = false;
  let selected = 0;
  let open = false;
  /** The layout the panel's canvas was last drawn at — what the hover
   * readout reads against. */
  let panelLayout: SkewTLayout | null = null;
  /** Where the pointer is on the chart, or null for the default reading. */
  let pointer: { x: number; y: number } | null = null;
  /** What each canvas last had painted on it, so a repaint that would
   * produce the same picture is skipped (`signature`). */
  const painted = new WeakMap<HTMLCanvasElement, string>();

  const stationKey = (item: SoundingStationEntry): string => `${item.id}|${item.latest}`;

  /** The ascent on screen, or null before the line has arrived. */
  function sounding(): SoundingStation["soundings"][number] | null {
    if (!station) return null;
    return station.soundings[Math.min(selected, station.soundings.length - 1)] ?? null;
  }

  function observedProfile(): Profile | null {
    const ascent = sounding();
    return ascent ? profileFromSounding(ascent) : null;
  }

  /** Fetch the station's window, once, the first time the section is open
   * with a station in it. A failure is a note in the section rather than an
   * error over the map: the panel's other rows are unaffected by it. */
  function ensureStation(): void {
    if (!open || !entry || !loaded) return;
    const key = stationKey(entry);
    if (fetchKey === key) return;
    fetchKey = key;
    failed = false;
    station = null;
    selected = 0;
    const sequence = ++fetchSequence;
    const source = loaded;
    const wanted = entry;
    void fetchSoundingStation(source, wanted)
      .then((result) => {
        if (sequence !== fetchSequence) return;
        station = result;
        selected = 0;
        render();
        options.onChange();
      })
      .catch((error: unknown) => {
        if (sequence !== fetchSequence) return;
        failed = true;
        // Diagnostics stay English and stay in the console; the section
        // says only that it has nothing to draw.
        console.warn("sounding: station not read:", error instanceof Error ? error.message : error);
        render();
      });
  }

  function setOpen(next: boolean): void {
    if (open === next) return;
    open = next;
    head.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    ensureStation();
    render();
    options.onChange();
  }

  head.addEventListener("click", () => setOpen(!open));

  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect();
    pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    writeReadout();
  });
  canvas.addEventListener("pointerleave", () => {
    pointer = null;
    writeReadout();
  });

  /** The reading under the pointer, or the 500 hPa one a forecaster names
   * first when there is nothing under it. */
  function writeReadout(): void {
    const layout = panelLayout;
    const observed = observedProfile();
    if (!layout || !observed) {
      readout.value = "";
      return;
    }
    const model = options.modelProfile();
    const profiles = model ? [observed, model.profile] : [observed];
    // Off the plot — over an axis, the barb column, the margin — the line
    // reads 500 hPa as it does before any hover, rather than going blank:
    // an empty line collapses and the panel under it jumps.
    const home = toXY(layout, 500, 0);
    const at = pointer ?? home;
    const reading = readoutAt(layout, profiles, at.x, at.y) ?? readoutAt(layout, profiles, home.x, home.y);
    if (!reading) {
      readout.value = "";
      return;
    }
    const parts = [`${reading.p.toFixed(0)} hPa`];
    for (const [index, values] of reading.profiles.entries()) {
      const name = index === 0 ? t("soundingObserved") : t("soundingModel");
      const temperature = values.t === null ? "--" : `${values.t.toFixed(1)} °C`;
      const dew = values.td === null ? "--" : `${values.td.toFixed(1)} °C`;
      const wind =
        values.ws === null || values.wd === null
          ? "--"
          : `${String(Math.round(values.wd)).padStart(3, "0")}° ${values.ws.toFixed(0)} m/s`;
      parts.push(`${name} ${temperature} / ${dew} · ${wind}`);
    }
    readout.value = parts.join("   ");
  }

  /**
   * What the chart on a canvas of this size is currently a picture of.
   *
   * The panel repaints on every decoded frame and on every step of
   * playback, and none of that moves the ascent: it is drawn at its own
   * nominal time, and the model column over it at the frame nearest *that*.
   * Redrawing a skew-T is not free — a dozen pseudoadiabats are integrated
   * every time — so a paint that would produce the same picture is skipped,
   * and the signature is everything a picture is made of. The model column
   * is summed rather than compared level by level, which is enough to catch
   * a series arriving and cheap on the eight levels a run publishes.
   */
  function signature(width: number, height: number): string {
    const ascent = sounding();
    const model = options.modelProfile();
    let column = 0;
    if (model) {
      for (let index = 0; index < model.profile.n; index += 1) {
        for (const field of [model.profile.p, model.profile.t, model.profile.td, model.profile.ws, model.profile.wd]) {
          const value = field[index]!;
          if (Number.isFinite(value)) column += value * (index + 1);
        }
      }
    }
    const ink = skewTInkFrom(root);
    return [
      entry?.id ?? "-",
      ascent?.time ?? "-",
      ascent?.n ?? 0,
      model?.run ?? "-",
      model?.profile.n ?? -1,
      column.toFixed(3),
      Math.round(width),
      Math.round(height),
      ink.ink,
      ink.paper,
    ].join("|");
  }

  /** Paint one canvas at the device's own resolution. */
  function paint(target: HTMLCanvasElement, width: number, height: number): SkewTLayout | null {
    const observed = observedProfile();
    if (width <= 0 || height <= 0) return null;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    if (target.width !== pixelWidth || target.height !== pixelHeight) {
      target.width = pixelWidth;
      target.height = pixelHeight;
    }
    target.style.height = `${height}px`;
    const context = target.getContext("2d");
    if (!context) return null;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const ink = skewTInkFrom(root);
    context.clearRect(0, 0, width, height);
    context.fillStyle = ink.paper;
    context.fillRect(0, 0, width, height);
    const layout = layoutFor(width, height);
    const model = options.modelProfile();
    drawSkewT(context, layout, ink, {
      profile: observed,
      model: model?.profile ?? null,
      parcel: observed ? parcelPath(observed) : null,
      font: FONT,
    });
    return layout;
  }

  /** The time buttons: the station's nominal times, newest first, the one
   * on screen pressed. A station with one ascent needs no row. */
  function syncTimes(): void {
    const available = station?.soundings.map((ascent) => ascent.time) ?? entry?.times ?? [];
    times.hidden = available.length < 2 || station === null;
    if (times.hidden) {
      times.replaceChildren();
      return;
    }
    times.replaceChildren(
      ...available.map((time, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "sounding-time";
        button.textContent = options.formatTime(time);
        if (index === selected) button.setAttribute("aria-current", "true");
        button.addEventListener("click", () => {
          if (selected === index) return;
          selected = index;
          render();
          options.onChange();
        });
        return button;
      }),
    );
  }

  function render(): void {
    if (!entry) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const ascent = sounding();
    const time = ascent?.time ?? entry.latest;
    line.textContent = [
      stationCode(entry),
      `${distanceKm < 10 ? distanceKm.toFixed(1) : Math.round(distanceKm)} km`,
      options.formatTime(time),
    ].join(" · ");
    expand.textContent = t("soundingExpand");
    if (!open) return;
    syncTimes();
    const model = options.modelProfile();
    if (failed) legend.textContent = t("soundingUnavailable");
    else if (station === null) legend.textContent = t("soundingLoading");
    else {
      // Each source's words sit beside a swatch in its own two hues, so the
      // legend reads the way the chart does: T warm, Td cool, the sonde
      // solid and the model dotted.
      const items: (string | Node)[] = [
        swatch("—", "--skewt-temperature"),
        swatch("- -", "--skewt-dew"),
        ` ${t("soundingObserved")} ${options.formatTime(time)}`,
      ];
      if (model) {
        items.push(
          "   ",
          swatch("···", "--skewt-model"),
          swatch("···", "--skewt-model-dew"),
          ` ${t("soundingModel")} ${model.run} ${options.formatTime(new Date(model.validTime).toISOString())} (${gapLabel(model.validTime - Date.parse(time))})`,
        );
      } else if (options.modelPossible()) {
        items.push(`   ${t("soundingNoModel")}`);
      }
      legend.replaceChildren(...items);
    }
    const width = canvas.clientWidth;
    const mark = signature(width, CHART_HEIGHT);
    if (panelLayout === null || painted.get(canvas) !== mark) {
      panelLayout = paint(canvas, width, CHART_HEIGHT);
      if (panelLayout) painted.set(canvas, mark);
    }
    writeReadout();
  }

  return {
    root,
    expandButton: expand,
    setStation(nextLoaded, nextEntry, nextDistance) {
      loaded = nextLoaded;
      distanceKm = nextDistance;
      const previous = entry;
      entry = nextEntry;
      if (!nextEntry) {
        // The pin moved off every station: drop the fetched line so a later
        // pin near the same one asks again against the index of the day.
        station = null;
        fetchKey = null;
        fetchSequence += 1;
        render();
        return;
      }
      if (!previous || stationKey(previous) !== stationKey(nextEntry)) {
        station = null;
        selected = 0;
        pointer = null;
        // A section is open by default where there is room for it and the
        // viewer has the sounding marks on: then the chart is the reason
        // the point was pinned. With the marks off it starts folded, and on
        // a phone always, since 320 px of chart over the capsule is the
        // whole screen.
        const wide = window.innerWidth > PHONE_WIDTH && window.innerHeight > SHORT_HEIGHT;
        open = wide && options.wantsOpen();
        head.setAttribute("aria-expanded", String(open));
        body.hidden = !open;
        ensureStation();
      }
      render();
    },
    selectedTime() {
      return sounding()?.time ?? null;
    },
    isOpen() {
      return open && entry !== null;
    },
    open() {
      setOpen(true);
    },
    render,
    drawInto(target, width, height) {
      const mark = signature(width, height);
      if (painted.get(target) === mark) return;
      if (paint(target, width, height)) painted.set(target, mark);
    },
    headline() {
      if (!entry) return "";
      const named = entry.name ? ` ${entry.name}` : "";
      return `${stationCode(entry)}${named}`;
    },
  };
}
