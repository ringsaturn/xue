/**
 * The sounding section of the probe panel: the nearest radiosonde ascent,
 * drawn as a skew-T under the meteogram rows, with everything a reader needs
 * beside it to read the chart — which line is which, the values under the
 * pointer, what the profile means in a few sentences, and the handful of
 * numbers a forecaster takes off it.
 *
 * `skewt.ts` draws into a context a caller owns and knows nothing about the
 * shell; this is that caller for the panel, as `dev.ts` is for the
 * workbench. It owns the section's DOM, the station whose line it has
 * fetched, which of that station's nominal times is selected, and the ink
 * it resolves from the panel's own custom properties at every draw — so a
 * theme switch repaints the chart in place, like everything else the shell
 * repaints rather than reloads. `analysis.ts` finds the layers and lifts the
 * parcel; the words and the units are put on them here.
 *
 * What it deliberately does not own: the model column. That comes from the
 * run on screen, through probe sessions main.ts opens and reads, and is
 * handed in through `modelProfile()` at draw time. The section asks; it
 * never fetches a forecast, and never moves the playhead — the selected
 * ascent is a time of its own, and the transport below is untouched by it.
 *
 * The full chart is not a second chart. The section's body — the times, the
 * chart and everything beside it — is one subtree, and opening the sheet
 * moves that subtree into it (`enterSheet`) and closing moves it back, so
 * the two can never disagree and nothing is built twice.
 *
 * Opening is what costs anything: the station's whole window is one range
 * request, made once, on the first open. A section that stays closed —
 * which is what a phone-width viewport starts as, where a 320 px chart
 * would be the whole screen — fetches nothing and opens no session.
 */

import { element } from "../dom";
import { intlLocale, t } from "../i18n";
import {
  UNIT_LABELS,
  convertHeight,
  convertTemperatureDelta,
  formatDistance,
  formatPrecipitation,
  formatTemperature,
  formatWind,
  units,
} from "../units";
import { fetchSoundingStation, type LoadedSoundingIndex } from "../stations/fetch";
import type { SoundingStation, SoundingStationEntry } from "../stations/schema";
import { analyseProfile, liftParcel, type ProfileAnalysis } from "./analysis";
import type { ModelColumn } from "./column";
import {
  SIG,
  clipBelowSurface,
  convectiveCondensation,
  hasSig,
  interpolateAtPressure,
  profileFromSounding,
  type ConvectiveCondensation,
  type Profile,
} from "./profile";
import {
  drawSkewT,
  drawSkewTCursor,
  fittedSkew,
  pressureAtHeight,
  readoutAt,
  skewTLayout,
  toXY,
  type SkewTCursor,
  type SkewTInk,
  type SkewTLayout,
  type SkewTMark,
} from "./skewt";

/** The chart's height in the panel, CSS pixels: tall enough for the
 * troposphere to read at a glance, short enough that the panel over the
 * capsule stays a panel. */
const CHART_HEIGHT = 320;
/** Room around the plot for the pressure labels (left), the wind column
 * (right), the temperature labels (bottom) and, above it, a barb at the top
 * level, whose shaft and pennants lean out of the plot by their own length. */
const MARGIN = { left: 32, top: 20, right: 44, bottom: 22 } as const;
/** The chart's instrument face, the panel's own. */
const FONT = '9px "IBM Plex Mono", ui-monospace, monospace';
/** Below this the panel is a phone's, and the section starts closed. */
const PHONE_WIDTH = 720;
/** Below this viewport height the chart would take the panel past the top
 * of the screen, so the section starts closed there too. */
const SHORT_HEIGHT = 760;
/** A model frame this far from the ascent is still drawn, but the legend
 * says what the gap means: past it the two curves differ by the weather
 * having moved on as much as by the forecast being wrong. */
const GAP_WARNING_MS = 6 * 3600 * 1000;
/** The spread the moist layers are found at, °C (`analysis.ts`'s default). */
const MOIST_SPREAD = 3;
/** A wind at or above this, m/s, is called a jet. */
const JET_SPEED = 30;
/** One full barb, in m/s: ten knots. */
const BARB_MS = 10 / 1.9438444924406046;

interface SoundingSectionOptions {
  /** The model column at the selected ascent's time, with the run it came
   * from, or null when the run publishes no isobaric levels or has no
   * frame near enough. Called at draw time, never cached. */
  modelProfile(): { profile: Profile; run: string; validTime: number } | null;
  /** The model column at the playhead, stood on the model's ground, with
   * the run and the frame it is for and the run's own convective energy —
   * or null while its series are still being read. This is the profile
   * itself where there is no ascent, and it follows the transport. */
  modelColumn(): {
    column: ModelColumn;
    run: string;
    stamp: string;
    frame: number;
    cape: number | null;
    cin: number | null;
  } | null;
  /** Whether the run on screen publishes levels enough to be a profile of
   * its own (`model.ts::modelColumnPossible`). */
  columnPossible(): boolean;
  /** The run's model, as the source switch names it. */
  modelName(): string;
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
  /** How tall the chart is drawn outside the full-chart sheet, for the
   * width it has: the place the body is docked in decides (a half-height
   * panel on a phone has less room than a card). 320 px when absent. */
  chartHeight?(width: number): number;
}

export interface SoundingSection {
  root: HTMLElement;
  /** The button that opens the full chart — the sheet's trigger, so
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
  /** Whether the chart is showing the model column as the profile itself,
   * which reads more of the run than an overlay does. */
  wantsColumn(): boolean;
  /** Show the chart, as a card's "open sounding" does after pinning the
   * probe at the station; a no-op when it is already showing. */
  open(): void;
  /** Fold the chart away, as a press on the open header does. */
  close(): void;
  /** Keep the body somewhere other than under the section's header — a
   * card beside the map — or, with null, under the header again. The
   * full-chart sheet still borrows it from wherever it is docked. */
  dock(host: HTMLElement | null): void;
  /** The header's line: the station, its distance and the ascent's time,
   * or the run and the frame of a model column. */
  caption(): string;
  /** Repaint from the current state (a new frame, a new run, a theme, a
   * locale or a unit switch, a resized window). */
  render(): void;
  /** Move the section's body into the sheet, where the chart takes the
   * height the viewport gives it. */
  enterSheet(mount: HTMLElement): void;
  /** Bring the body back under the section's header. */
  leaveSheet(): void;
  /** What the sheet's heading names. */
  headline(): string;
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
function skewTInkFrom(node: Element): SkewTInk {
  const styles = getComputedStyle(node);
  const read = (name: string, fallback: string): string =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    ink: read("--text", "#1b1a17"),
    inkMuted: read("--muted", "#6e6a60"),
    grid: read("--skewt-grid", "rgba(27, 26, 23, 0.16)"),
    gridStrong: read("--skewt-grid-strong", "rgba(27, 26, 23, 0.45)"),
    paper: read("--skewt-paper", "#f8f5ee"),
    model: read("--skewt-model", "#e8763a"),
    parcel: read("--skewt-parcel", "#6a45a8"),
    temperature: read("--skewt-temperature", "#b4441a"),
    dew: read("--skewt-dew", "#0e7c72"),
    modelDew: read("--skewt-model-dew", "#3bb8a8"),
    dryAdiabat: read("--skewt-dry", "rgba(150, 104, 44, 0.24)"),
    moistAdiabat: read("--skewt-moist", "rgba(44, 104, 168, 0.26)"),
    mixingRatio: read("--skewt-mixing", "rgba(46, 128, 72, 0.4)"),
    tag: read("--skewt-tag", "rgba(27, 26, 23, 0.82)"),
    tagText: read("--skewt-tag-text", "#f3efe6"),
    halo: read("--skewt-halo", "#f8f5ee"),
    buoyant: read("--skewt-buoyant", "rgba(214, 84, 30, 0.2)"),
    inhibited: read("--skewt-inhibited", "rgba(44, 104, 168, 0.2)"),
    moistLayer: read("--skewt-moist-layer", "#0e7c72"),
    inversion: read("--skewt-inversion", "rgba(214, 84, 30, 0.28)"),
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
/** An emagram has no skew to carry the top of the box to the cold side, so
 * its range has to hold a −60 °C tropopause and a +35 °C surface at once:
 * wider, and centred colder. */
const EMAGRAM_SPAN_MIN = 120;
const EMAGRAM_CENTRE = -22.5;

/** The chart's bottom edge for an ascent that starts at `pSurface` hPa.
 * A station near sea level keeps the classical 1050 hPa, so two of them
 * compare at a glance; one on high ground starts just under its own
 * surface, rather than above a third of a chart of rock. */
function bottomPressure(pSurface: number | null): number {
  if (pSurface === null || !Number.isFinite(pSurface) || pSurface >= 970) return 1050;
  return Math.ceil((pSurface * 1.03) / 10) * 10;
}

/**
 * The plot rectangle inside a canvas of this size, the temperature range
 * that fits it and the skew it can afford.
 *
 * The panel is as wide as the transport capsule and the chart is 320 px
 * tall, so the box the ascent is drawn in is much wider than it is tall —
 * and a skew-T drawn at a fixed −40 … +45 °C in that box has its isotherms
 * a centimetre apart and every profile lying almost flat. The range widens
 * with the width instead, about the same midpoint, so a degree is worth the
 * same few pixels whatever room the chart has. The sheet's box is the other
 * shape, taller than it is wide, and there it is the skew that gives
 * (`fittedSkew`).
 */
function layoutFor(width: number, height: number, emagram: boolean, pSurface: number | null): SkewTLayout {
  const plotWidth = Math.max(40, width - MARGIN.left - MARGIN.right);
  const plotHeight = Math.max(40, height - MARGIN.top - MARGIN.bottom);
  const span = Math.min(
    SPAN_LIMITS[1],
    Math.max(emagram ? EMAGRAM_SPAN_MIN : SPAN_LIMITS[0], plotWidth * DEGREES_PER_PIXEL),
  );
  const centre = emagram ? EMAGRAM_CENTRE : SPAN_CENTRE;
  const tMin = centre - span / 2;
  const tMax = centre + span / 2;
  return skewTLayout(
    { x: MARGIN.left, y: MARGIN.top, width: plotWidth, height: plotHeight },
    {
      tMin,
      tMax,
      skew: emagram ? 0 : fittedSkew(plotWidth, plotHeight, tMin, tMax),
      pressureBottom: bottomPressure(pSurface),
    },
  );
}

/** The sheet's chart is as tall as the viewport lets it be, and never
 * squarer than a skew-T is read at. */
function sheetChartHeight(width: number): number {
  return Math.max(CHART_HEIGHT, Math.min(window.innerHeight - 220, Math.round(width * 1.1)));
}

type SwatchKind = "solid" | "dashed" | "dotted" | "fill" | "bar" | "glow";

/** A legend swatch: the line's own stroke in the line's own hue, drawn by
 * the stylesheet from the same custom property the chart reads. */
function swatch(kind: SwatchKind, token: string): HTMLElement {
  const node = document.createElement("i");
  node.className = "sounding-swatch";
  node.dataset.kind = kind;
  node.style.color = `var(${token})`;
  return node;
}

/** One line of the legend. */
function keyRow(...parts: (string | Node)[]): HTMLElement {
  const row = element("div", "sounding-key-row");
  row.append(...parts);
  return row;
}

/** A named item of a legend line: its swatches, then its words. */
function keyItem(text: string, ...swatches: Node[]): HTMLElement {
  const item = element("span", "sounding-key-item");
  item.append(...swatches, text);
  return item;
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

/** The synoptic hour an ascent is filed under, the way a forecaster names
 * it: `00Z`, `12Z`. */
function synopticHour(time: string): string {
  const hour = new Date(time).getUTCHours();
  return Number.isFinite(hour) ? `${String(hour).padStart(2, "0")}Z` : "";
}

/** How long ago an ascent was released, in the viewer's language: hours up
 * to two days, days past that. Empty for a time that is not in the past. */
function ageLabel(time: string): string {
  const elapsed = Date.now() - Date.parse(time);
  if (!Number.isFinite(elapsed) || elapsed < 0) return "";
  const hours = Math.round(elapsed / 3600000);
  const format = new Intl.RelativeTimeFormat(intlLocale, { numeric: "always", style: "short" });
  return hours < 48 ? format.format(-hours, "hour") : format.format(-Math.round(hours / 24), "day");
}

/** A height above sea level as the chart's right edge writes it: kilometres
 * where the viewer reads metres, feet otherwise. */
function altitude(metres: number): string {
  if (units.height === "ft") return `${Math.round(convertHeight(metres) / 100) * 100} ft`;
  return Math.abs(metres) < 1000 ? `${Math.round(metres / 10) * 10} m` : `${(metres / 1000).toFixed(1)} km`;
}

/** The two ends of a layer, written as one range: the lower end drops its
 * unit where the upper end says the same one (`3.8–4.0 km`), and keeps it
 * where they differ (`800 m–1.2 km`). */
function altitudeSpan(from: number, to: number): { from: string; to: string } {
  const upper = altitude(to);
  const lower = altitude(from);
  const unit = (text: string): string => text.slice(text.lastIndexOf(" "));
  return { from: unit(lower) === unit(upper) ? lower.slice(0, lower.lastIndexOf(" ")) : lower, to: upper };
}

/** A temperature difference in the viewer's unit. */
function temperatureDelta(kelvin: number, decimals = 1): string {
  return `${convertTemperatureDelta(kelvin).toFixed(decimals)} ${UNIT_LABELS.temperature[units.temperature]}`;
}

/** The heights the right edge of the chart names, in metres, and what it
 * calls each: whole kilometres, or thousands of feet. Close together near
 * the ground, where a reader asks how high a cloud base or an inversion is,
 * and sparse above it — the pressure axis is logarithmic, and a tick every
 * kilometre is a ladder up the side of the chart. */
function heightScale(): { metres: number; label: string }[] {
  if (units.height === "ft") {
    return [3, 6, 10, 18, 24, 30, 39, 53].map((thousands) => ({
      metres: (thousands * 1000) / 3.280839895,
      label: `${thousands} kft`,
    }));
  }
  return [1, 2, 3, 5, 7, 9, 12, 16].map((kilometres) => ({
    metres: kilometres * 1000,
    label: `${kilometres} km`,
  }));
}

/** A wind as the readout writes it, direction first: `247° 40 m/s`. */
function windText(direction: number, speed: number): string {
  return `${String(Math.round(direction) % 360).padStart(3, "0")}° ${formatWind(speed, 0)}`;
}

/** Everything the section derives from one ascent, computed once when the
 * ascent is first shown: the parcel is integrated, and a pointer moving
 * over the chart must not integrate it again. */
interface Reading {
  profile: Profile;
  analysis: ProfileAnalysis;
  convective: ConvectiveCondensation | null;
  /** Set when the profile is a model column rather than an ascent: what it
   * is a column of, how coarse it is, and the run's own CAPE and CIN. */
  column: { run: string; stamp: string; levels: number; top: number | null; cape: number | null; cin: number | null } | null;
}

/** Which profile the chart is of. */
type Source = "observed" | "model";

/** A profile's values folded into one number: enough to tell that a series
 * has arrived or a frame has changed, and cheap on a column of a dozen
 * levels. */
function profileSum(profile: Profile): number {
  let sum = 0;
  for (let index = 0; index < profile.n; index += 1) {
    for (const field of [profile.p, profile.t, profile.td, profile.ws, profile.wd]) {
      const value = field[index]!;
      if (Number.isFinite(value)) sum += value * (index + 1);
    }
  }
  return sum;
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
  // Which profile: the ascent nearby or the run's own column. Shown only
  // where there are both to choose between.
  const sources = element("div", "sounding-times sounding-sources");
  sources.setAttribute("role", "group");
  const times = element("div", "sounding-times");
  times.setAttribute("role", "group");
  const layoutNode = element("div", "sounding-layout");
  const figure = element("div", "sounding-figure");
  const canvas = element("canvas", "sounding-chart");
  canvas.setAttribute("aria-hidden", "true");
  const side = element("div", "sounding-side");
  const status = element("p", "sounding-status");
  const readout = element("output", "sounding-readout");
  const legend = element("div", "sounding-key");
  const summary = element("ul", "sounding-summary");
  const indices = element("dl", "sounding-indices");
  // How to read the chart, for a reader who has never met one: folded away,
  // since the reader who has does not want it between them and the numbers.
  const guide = element("details", "sounding-guide");
  const guideTitle = element("summary", "sounding-guide-title");
  const guideList = element("ol", "sounding-guide-list");
  guide.append(guideTitle, guideList);
  side.append(status, readout, legend, summary, indices, guide);
  layoutNode.append(figure, side);

  const footer = element("div", "sounding-footer");
  // The projection switch: the skew-T a forecaster reads by default, or the
  // unskewed emagram. Both names are the diagrams' own, in every locale, and
  // the switch is the sheet's — the panel's chart is always a skew-T.
  const projection = element("span", "sounding-projection");
  projection.setAttribute("role", "group");
  const projectionButtons = (["SKEW-T", "EMAGRAM"] as const).map((name) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sounding-projection-option";
    button.textContent = name;
    button.setAttribute("aria-pressed", String(name === "SKEW-T"));
    projection.append(button);
    return button;
  });
  const expand = document.createElement("button");
  expand.type = "button";
  expand.className = "sounding-expand";
  footer.append(projection, expand);
  figure.append(canvas, footer);
  body.append(sources, times, layoutNode);
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
  /** What the viewer last chose by hand, kept from pin to pin: whether the
   * chart is open, and which profile it shows where both are on offer. */
  let chosenOpen: boolean | null = null;
  let preferred: Source | null = null;
  /** Where the body is while the full chart is open, or null in the panel. */
  let sheetMount: HTMLElement | null = null;
  /** Where the body lives outside the sheet: under the header, or wherever
   * the panel's layout docked it. */
  let home: HTMLElement = root;
  /** Where the pointer is on the chart, or null for the default reading
   * and no crosshair. */
  let pointer: { x: number; y: number } | null = null;
  /** Whether the chart is drawn unskewed. */
  let emagram = false;
  /** The finished chart, kept so the crosshair repaints over a copy of its
   * pixels instead of integrating the background again. */
  let chart: { layout: SkewTLayout; ratio: number; base: HTMLCanvasElement } | null = null;
  /** What the canvas last had painted on it, so a repaint that would
   * produce the same picture is skipped (`signature`). */
  let painted = "";
  /** What the words beside the chart were last written for. */
  let insightKey = "";
  /** The profile on screen, read once (`reading`). */
  let cached: { key: string; reading: Reading } | null = null;
  /** The run's column for the render in progress: asked for once and kept
   * until the next render, since a render reads it several times over and
   * a pointer moving over the chart reads it again at every move. */
  let columnMemo: ReturnType<SoundingSectionOptions["modelColumn"]> | undefined;
  /** What the two rows of buttons were last built for. They are rebuilt
   * only when that changes: a button replaced between a finger going down
   * on it and coming up never hears the tap, and a panel that follows the
   * playhead renders several times a second. */
  let sourcesKey = "";
  let timesKey = "";
  /** The tallest the notes beside the chart have been for this pin and
   * source. A column that follows the playhead says a line more or a line
   * less from one frame to the next, and a panel whose height moved with it
   * would scroll under the reader's finger; the notes keep the room they
   * have needed so far. */
  let notesHeight = 0;
  let notesKey = "";

  const stationKey = (item: SoundingStationEntry): string => `${item.id}|${item.latest}`;

  /** The ascent on screen, or null before the line has arrived. */
  function sounding(): SoundingStation["soundings"][number] | null {
    if (!station) return null;
    return station.soundings[Math.min(selected, station.soundings.length - 1)] ?? null;
  }

  /** Which profile is on screen: the ascent where there is one and the
   * viewer has not asked for the model, the run's column otherwise. */
  function profileSource(): Source {
    if (!entry) return "model";
    if (!options.columnPossible()) return "observed";
    return preferred ?? "observed";
  }

  /** The profile on screen with everything derived from it. */
  function reading(): Reading | null {
    return profileSource() === "model" ? columnReading() : ascentReading();
  }

  /**
   * The run's column as the profile. It is read again whenever a series
   * arrives or the playhead moves, which is cheap on a dozen levels, and it
   * is analysed as what it is: the parcel is the 2 m air itself — there is
   * no layer of levels to average — and no inversion is looked for, since a
   * column this coarse would only ever find the ones a kilometre deep.
   */
  function currentColumn(): ReturnType<SoundingSectionOptions["modelColumn"]> {
    if (columnMemo === undefined) columnMemo = options.modelColumn();
    return columnMemo;
  }

  function columnReading(): Reading | null {
    const model = currentColumn();
    if (!model) {
      cached = null;
      return null;
    }
    const profile = model.column.profile;
    const key = `model|${model.run}|${model.frame}|${profile.n}|${profileSum(profile).toFixed(3)}|${model.cape}|${model.cin}`;
    if (cached?.key !== key) {
      cached = {
        key,
        reading: {
          profile,
          analysis: { ...analyseProfile(profile), parcel: liftParcel(profile, "surface"), inversions: [] },
          convective: null,
          column: {
            run: model.run,
            stamp: model.stamp,
            levels: model.column.levels,
            top: model.column.top,
            cape: model.cape,
            cin: model.cin,
          },
        },
      };
    }
    return cached.reading;
  }

  /** The ascent on screen with everything derived from it. */
  function ascentReading(): Reading | null {
    const ascent = sounding();
    if (!ascent || !entry) {
      cached = null;
      return null;
    }
    const key = `${entry.id}|${ascent.time}|${ascent.n}`;
    if (cached?.key !== key) {
      const profile = profileFromSounding(ascent);
      // Some centres file the surface level without its height. It is the
      // station's own elevation, and without it nothing between the ground
      // and the first level that has a height can be placed on the chart.
      for (let index = 0; index < profile.n; index += 1) {
        if (!hasSig(profile.sig[index]!, SIG.surface)) continue;
        if (!Number.isFinite(profile.z[index]!) && entry.elev !== null) profile.z[index] = entry.elev;
        break;
      }
      cached = {
        key,
        reading: {
          profile,
          analysis: analyseProfile(profile),
          convective: convectiveCondensation(profile),
          column: null,
        },
      };
    }
    return cached.reading;
  }

  /** The model column as the chart shows it: the levels the run publishes
   * under the station's own ground are extrapolations, and are not drawn. */
  function shownModel(observed: Reading | null): { profile: Profile; run: string; validTime: number } | null {
    if (observed?.column) return null;
    const model = options.modelProfile();
    if (!model) return null;
    const surface = observed?.analysis.surface?.p ?? observed?.profile.p[0];
    if (surface === undefined || !Number.isFinite(surface)) return model;
    return { ...model, profile: clipBelowSurface(model.profile, surface) };
  }

  /** Fetch the station's window, once, the first time the section is open
   * with a station in it. A failure is a note in the section rather than an
   * error over the map: the panel's other rows are unaffected by it. */
  function ensureStation(): void {
    if (!open || !entry || !loaded || profileSource() !== "observed") return;
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

  head.addEventListener("click", () => {
    chosenOpen = !open;
    setOpen(!open);
  });

  // The crosshair follows a mouse and stays where a finger left it: on a
  // touch screen the reading under a finger is only legible once the finger
  // is gone. A finger's drag is the chart's for as long as it lasts, past
  // the chart's edge too, so it is captured where it starts.
  const move = (event: PointerEvent): void => {
    const rect = canvas.getBoundingClientRect();
    pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    paintCursor();
    writeReadout();
  };
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "touch") canvas.setPointerCapture?.(event.pointerId);
    move(event);
  });
  canvas.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "touch") return;
    pointer = null;
    paintCursor();
    writeReadout();
  });

  for (const [index, button] of projectionButtons.entries()) {
    button.addEventListener("click", () => {
      const next = index === 1;
      if (emagram === next) return;
      emagram = next;
      for (const [position, other] of projectionButtons.entries()) {
        other.setAttribute("aria-pressed", String((position === 1) === emagram));
      }
      pointer = null;
      render();
    });
  }

  /** Put the finished chart back on the canvas and, if the pointer is over
   * it, the crosshair on top. */
  function paintCursor(): void {
    const context = canvas.getContext("2d");
    if (!chart || !context) return;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(chart.base, 0, 0);
    context.setTransform(chart.ratio, 0, 0, chart.ratio, 0, 0);
    if (!pointer) return;
    const cursor = cursorAt(chart.layout, pointer.x, pointer.y);
    if (cursor) drawSkewTCursor(context, chart.layout, skewTInkFrom(root), cursor, FONT);
  }

  /** The crosshair's content at a canvas point, every value in the
   * viewer's units, or null off the plot or with no ascent. */
  function cursorAt(layout: SkewTLayout, x: number, y: number): SkewTCursor | null {
    const observed = reading();
    if (!observed) return null;
    const model = shownModel(observed);
    const profiles = model ? [observed.profile, model.profile] : [observed.profile];
    const at = readoutAt(layout, profiles, x, y);
    if (!at) return null;
    const ink = skewTInkFrom(root);
    const sonde = at.profiles[0]!;
    const marks: SkewTCursor["marks"][number][] = [];
    if (sonde.td !== null) {
      marks.push({ t: sonde.td, color: ink.dew ?? ink.ink, label: formatTemperature(sonde.td, 0), side: "left" });
    }
    if (sonde.t !== null) {
      marks.push({ t: sonde.t, color: ink.temperature ?? ink.ink, label: formatTemperature(sonde.t, 0), side: "right" });
    }
    const column = at.profiles[1];
    if (column?.td != null) marks.push({ t: column.td, color: ink.modelDew ?? ink.model, side: "left" });
    if (column?.t != null) marks.push({ t: column.t, color: ink.model, side: "right" });
    const left = [`${at.p.toFixed(0)} hPa`];
    if (sonde.z !== null) left.unshift(altitude(sonde.z));
    const right = sonde.ws === null || sonde.wd === null ? null : windText(sonde.wd, sonde.ws);
    return { p: at.p, marks, left, right };
  }

  /** The reading under the pointer, or the 500 hPa one a forecaster names
   * first when there is nothing under it: the level, then one line per
   * source with each value named. */
  function writeReadout(): void {
    const layout = chart?.layout;
    const observed = reading();
    if (!layout || !observed) {
      readout.replaceChildren();
      return;
    }
    const model = shownModel(observed);
    const profiles = model ? [observed.profile, model.profile] : [observed.profile];
    // Off the plot — over an axis, the barb column, the margin — the lines
    // read 500 hPa as they do before any hover, rather than going blank.
    const home = toXY(layout, Math.min(500, layout.pBottom), 0);
    const at = pointer ?? home;
    const values = readoutAt(layout, profiles, at.x, at.y) ?? readoutAt(layout, profiles, home.x, home.y);
    if (!values) {
      readout.replaceChildren();
      return;
    }
    // The pressure is the diagram's own vertical coordinate and stays in
    // hPa, as its axis does; the values read in the viewer's units.
    const height = values.profiles[0]?.z;
    const level = element(
      "span",
      "sounding-readout-level",
      height == null ? `${values.p.toFixed(0)} hPa` : `${values.p.toFixed(0)} hPa · ${altitude(height)}`,
    );
    const rows = values.profiles.map((profile, index) => {
      const row = element("span", "sounding-readout-row");
      const parts = [
        `T ${profile.t === null ? "--" : formatTemperature(profile.t)}`,
        `Td ${profile.td === null ? "--" : formatTemperature(profile.td)}`,
      ];
      if (profile.ws !== null && profile.wd !== null) parts.push(windText(profile.wd, profile.ws));
      row.append(
        element(
          "b",
          "sounding-readout-source",
          index === 0 && !observed.column ? t("soundingObserved") : t("soundingModel"),
        ),
        parts.join(" · "),
      );
      return row;
    });
    readout.replaceChildren(level, ...rows);
  }

  /** The levels the chart names on its left edge. */
  function levelMarks(observed: Reading): SkewTMark[] {
    const { profile, analysis } = observed;
    const marks: SkewTMark[] = [];
    const derived = profile.derived;
    if (derived?.freezingLevel != null) {
      marks.push({ p: pressureAtHeight(profile, derived.freezingLevel), label: formatTemperature(0, 0) });
    }
    const parcel = analysis.parcel;
    if (parcel) {
      marks.push({ p: parcel.path.lcl.p, label: "LCL" });
      // Where the parcel breaks free and where it stops are read off the
      // environment between levels, which a model column does not have.
      if (!observed.column) {
        // A parcel free from its condensation level has one level, not two.
        if (parcel.lfc !== null && Math.abs(parcel.lfc - parcel.path.lcl.p) > 3) marks.push({ p: parcel.lfc, label: "LFC" });
        if (parcel.el !== null) marks.push({ p: parcel.el, label: "EL" });
      }
    }
    if (derived?.tropopause != null) marks.push({ p: pressureAtHeight(profile, derived.tropopause), label: "TRP" });
    return marks;
  }

  /**
   * What the chart on the canvas is currently a picture of.
   *
   * The panel repaints on every decoded frame and on every step of
   * playback, and none of that moves the ascent: it is drawn at its own
   * nominal time, and the model column over it at the frame nearest *that*.
   * Redrawing a skew-T is not free — the pseudoadiabats are integrated
   * every time — so a paint that would produce the same picture is skipped,
   * and the signature is everything a picture is made of. The model column
   * is summed rather than compared level by level, which is enough to catch
   * a series arriving and cheap on the eight levels a run publishes.
   */
  function signature(width: number, height: number): string {
    const ascent = sounding();
    const shown = reading();
    const model = shown?.column ? null : options.modelProfile();
    const ink = skewTInkFrom(root);
    return [
      cached?.key ?? "-",
      entry?.id ?? "-",
      ascent?.time ?? "-",
      ascent?.n ?? 0,
      model?.run ?? "-",
      model?.profile.n ?? -1,
      model ? profileSum(model.profile).toFixed(3) : "-",
      Math.round(width),
      Math.round(height),
      emagram ? "emagram" : "skew",
      ink.ink,
      ink.paper,
      units.height,
      units.temperature,
      t("soundingParcel"),
    ].join("|");
  }

  /** Paint the canvas at the device's own resolution. */
  function paint(width: number, height: number): boolean {
    if (width <= 0 || height <= 0) return false;
    const observed = reading();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    canvas.style.height = `${height}px`;
    const context = canvas.getContext("2d");
    if (!context) return false;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const ink = skewTInkFrom(root);
    context.clearRect(0, 0, width, height);
    const surface = observed?.analysis.surface?.p ?? null;
    const layout = layoutFor(width, height, emagram, surface);
    const model = shownModel(observed);
    const profile = observed?.profile ?? null;
    const parcel = observed?.analysis.parcel ?? null;
    drawSkewT(context, layout, ink, {
      profile,
      model: model?.profile ?? null,
      parcel: parcel?.path ?? null,
      // Between a column's levels the environment is a straight line, and an
      // area shaded against it would not be the energy the run reports.
      buoyancy: parcel && !observed?.column ? { environment: parcel.environment, lfc: parcel.lfc, el: parcel.el } : null,
      nodes: Boolean(observed?.column),
      moistLayers: observed?.analysis.moist ?? [],
      inversions: observed?.analysis.inversions ?? [],
      marks: observed ? levelMarks(observed) : [],
      heights: profile
        ? heightScale().map((height) => ({ p: pressureAtHeight(profile, height.metres), label: height.label }))
        : [],
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
    const base = chart?.base ?? document.createElement("canvas");
    base.width = pixelWidth;
    base.height = pixelHeight;
    base.getContext("2d")?.drawImage(canvas, 0, 0);
    chart = { layout, ratio, base };
    if (pointer) paintCursor();
    return true;
  }

  /** The time buttons: the station's nominal times, newest first, the one
   * on screen pressed. A station with one ascent needs no row. */
  function syncTimes(): void {
    const available = station?.soundings.map((ascent) => ascent.time) ?? entry?.times ?? [];
    times.hidden = available.length < 2 || station === null;
    const key = times.hidden ? "" : [selected, ...available.map((time) => options.formatTime(time))].join("|");
    if (key === timesKey) return;
    timesKey = key;
    if (times.hidden) {
      times.replaceChildren();
      return;
    }
    times.replaceChildren(
      ...available.map((time, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "sounding-time";
        button.append(options.formatTime(time), element("small", "sounding-time-hour", synopticHour(time)));
        if (index === selected) button.setAttribute("aria-current", "true");
        button.addEventListener("click", () => {
          if (selected === index) return;
          selected = index;
          pointer = null;
          render();
          options.onChange();
        });
        return button;
      }),
    );
  }

  /** The legend: every line and every mark on the chart, named. */
  function writeLegend(shown: Reading, time: string): void {
    const rows: HTMLElement[] = [];
    const traces = [
      keyItem(`T ${t("soundingTemperature")}`, swatch("solid", "--skewt-temperature")),
      keyItem(`Td ${t("soundingDewPoint")}`, swatch("dashed", "--skewt-dew")),
    ];
    if (shown.column) {
      // The profile is the run's: say which run, which frame, and how many
      // levels the two lines are drawn through.
      rows.push(keyRow(...traces));
      rows.push(
        element(
          "div",
          "sounding-key-row sounding-key-quiet",
          [
            `${t("soundingModel")} ${shown.column.run}`,
            shown.column.stamp,
            t("soundingColumnLevels", { count: shown.column.levels }),
          ].join(" · "),
        ),
      );
    } else {
      rows.push(
        keyRow(...traces, element("span", "sounding-key-note", `${t("soundingObserved")} ${options.formatTime(time)}`)),
      );
      const model = shownModel(shown);
      if (model) {
        const gap = model.validTime - Date.parse(time);
        rows.push(
          keyRow(
            keyItem(
              `${t("soundingModel")} ${model.run} · ${options.formatTime(new Date(model.validTime).toISOString())} (${gapLabel(gap)})`,
              swatch("dotted", "--skewt-model"),
              swatch("dotted", "--skewt-model-dew"),
            ),
          ),
        );
        if (Math.abs(gap) >= GAP_WARNING_MS) {
          rows.push(
            element(
              "div",
              "sounding-key-row sounding-key-warning",
              t("soundingGapNote", { gap: gapLabel(gap).replace(/^[+−]/, "") }),
            ),
          );
        }
      } else if (options.modelPossible()) {
        rows.push(element("div", "sounding-key-row sounding-key-quiet", t("soundingNoModel")));
      }
    }
    const parcel = shown.analysis.parcel;
    if (parcel) {
      const row = keyRow(
        keyItem(
          `${t("soundingParcel")}: ${t(parcel.kind === "mixed" ? "soundingParcelMixed" : "soundingParcelSurface")}`,
          swatch("dashed", "--skewt-parcel"),
        ),
      );
      if (parcel.lfc !== null && !shown.column) {
        row.append(keyItem("CAPE", swatch("fill", "--skewt-buoyant")));
        if (parcel.cin !== null && parcel.cin < -0.5) row.append(keyItem("CIN", swatch("fill", "--skewt-inhibited")));
      }
      rows.push(row);
    }
    const layers: HTMLElement[] = [];
    if (shown.analysis.moist.length > 0) {
      layers.push(
        keyItem(t("soundingMoistLayer", { spread: temperatureDelta(MOIST_SPREAD, 0) }), swatch("bar", "--skewt-moist-layer")),
      );
    }
    if (shown.analysis.inversions.length > 0) {
      layers.push(keyItem(t("soundingInversion"), swatch("glow", "--skewt-inversion")));
    }
    if (layers.length > 0) rows.push(keyRow(...layers));
    const families = keyRow(
      keyItem(t("soundingDryAdiabat"), swatch("solid", "--skewt-dry")),
      keyItem(t("soundingMoistAdiabat"), swatch("dashed", "--skewt-moist")),
      keyItem(`${t("soundingMixingRatio")} g/kg`, swatch("dotted", "--skewt-mixing")),
    );
    families.classList.add("sounding-key-quiet");
    rows.push(families);
    rows.push(element("div", "sounding-key-row sounding-key-quiet", t("soundingBarbKey", barbParams())));
    legend.replaceChildren(...rows);
  }

  /** What one barb, half barb and pennant are worth in the viewer's unit. */
  function barbParams(): Record<string, string> {
    return {
      full: formatWind(BARB_MS, 0),
      half: formatWind(BARB_MS / 2, 0),
      flag: formatWind(BARB_MS * 5, 0),
    };
  }

  /** The profile in a few sentences: where it is moist, where it is capped,
   * where it freezes, whether it is unstable, what that means for
   * precipitation and how hard it blows aloft. */
  function writeSummary(observed: Reading): void {
    const { profile, analysis } = observed;
    const lines: string[] = [];
    const surface = analysis.surface;
    // The two deepest moist layers, from the ground up: more than that is a
    // list, not a reading.
    const moist = [...analysis.moist]
      .sort((a, b) => b.pBottom - b.pTop - (a.pBottom - a.pTop))
      .slice(0, 2)
      .sort((a, b) => b.pBottom - a.pBottom);
    for (const layer of moist) {
      if (!Number.isFinite(layer.zTop)) continue;
      const grounded = surface !== null && layer.pBottom >= surface.p - 5;
      if (grounded) lines.push(t("soundingSayMoistSurface", { to: altitude(layer.zTop) }));
      else if (Number.isFinite(layer.zBottom)) {
        lines.push(t("soundingSayMoist", altitudeSpan(layer.zBottom, layer.zTop)));
      }
    }
    if (analysis.moist.length === 0 && surface !== null) {
      lines.push(t("soundingSayDry", { spread: temperatureDelta(MOIST_SPREAD, 0) }));
    }
    // The surface inversion if there is one, else the strongest aloft.
    const inversion =
      analysis.inversions.find((layer) => layer.surfaceBased) ??
      [...analysis.inversions].sort((a, b) => b.strength - a.strength)[0];
    if (inversion && Number.isFinite(inversion.zTop)) {
      const strength = temperatureDelta(inversion.strength);
      if (inversion.surfaceBased) {
        lines.push(t("soundingSayInversionSurface", { strength, to: altitude(inversion.zTop) }));
      } else if (Number.isFinite(inversion.zBottom)) {
        lines.push(t("soundingSayInversion", { strength, ...altitudeSpan(inversion.zBottom, inversion.zTop) }));
      }
    }
    const freezing = profile.derived?.freezingLevel;
    if (freezing != null) lines.push(t("soundingSayFreezing", { height: altitude(freezing) }));
    else if (surface !== null && surface.t < 0) lines.push(t("soundingSayFrozen"));
    // An ascent's energy is the area on the chart; a column's is the number
    // the run computed on all of its own levels, where it publishes one.
    const parcel = analysis.parcel;
    const energy = observed.column
      ? observed.column.cape
      : parcel
        ? parcel.lfc === null
          ? 0
          : parcel.cape
        : null;
    if (energy !== null) {
      const cape = Math.round(energy);
      if (cape < 100) lines.push(t("soundingSayStable"));
      else {
        const key = cape < 1000 ? "soundingSayCapeWeak" : cape < 2500 ? "soundingSayCapeModerate" : "soundingSayCapeStrong";
        lines.push(t(key, { cape }));
      }
    }
    // What the moisture, the freezing level and the stability add up to for
    // precipitation — conditions, since a profile holds no lift. A thin
    // moist layer is not called drizzle where the air is unstable: a shower
    // builds its own depth. The phase is said only where something could
    // fall and the answer is not plain rain.
    const wet = analysis.precipitation;
    const unstable = energy !== null && Math.round(energy) >= 100;
    if (wet.deep) lines.push(t("soundingSayPrecipDeep", { depth: altitude(wet.depth) }));
    else if (wet.depth > 0 && !unstable) lines.push(t("soundingSayPrecipShallow", { depth: altitude(wet.depth) }));
    if (wet.evaporates) lines.push(t("soundingSayPrecipEvaporates"));
    if (unstable) lines.push(t("soundingSayPrecipShowery"));
    if (wet.depth > 0 || unstable) {
      if (wet.phase === "snow") lines.push(t("soundingSayPhaseSnow"));
      else if (wet.phase === "mix") lines.push(t("soundingSayPhaseMix", { depth: altitude(wet.meltingDepth) }));
      else if (wet.phase === "freezing") lines.push(t("soundingSayPhaseFreezing"));
    }
    const wind = analysis.maxWind;
    if (wind && Number.isFinite(wind.z)) {
      lines.push(
        t(wind.ws >= JET_SPEED ? "soundingSayJet" : "soundingSayWind", {
          speed: formatWind(wind.ws, 0),
          height: altitude(wind.z),
        }),
      );
    }
    // What a column this coarse cannot show is said last, once.
    if (observed.column && observed.column.top !== null) {
      lines.push(t("soundingSayColumn", { count: observed.column.levels, top: `${observed.column.top} hPa` }));
    }
    summary.replaceChildren(...lines.map((text) => element("li", "sounding-summary-line", text)));
    summary.hidden = lines.length === 0;
  }

  /** The numbers a forecaster takes off the chart, each under its name. */
  function writeIndices(observed: Reading): void {
    const { profile, analysis, convective } = observed;
    const derived = profile.derived;
    const surface = analysis.surface;
    const aboveGround = (p: number): string | null => {
      if (!surface) return null;
      const z = interpolateAtPressure(profile, p, profile.z) - surface.z;
      return Number.isFinite(z) ? altitude(Math.max(0, z)) : null;
    };
    const joules = (value: number | null): string | null => (value === null ? null : `${Math.round(value)} J/kg`);
    const entries: [string, string | null][] = [
      [t("soundingIndexFreezing"), derived?.freezingLevel != null ? altitude(derived.freezingLevel) : null],
      [t("soundingIndexTropopause"), derived?.tropopause != null ? altitude(derived.tropopause) : null],
      [t("soundingIndexPw"), derived?.pw != null ? formatPrecipitation(derived.pw, 1) : null],
      [t("soundingIndexLapse"), derived?.lapse850_500 != null ? `${derived.lapse850_500.toFixed(1)} K/km` : null],
      [t("soundingIndexLcl"), analysis.parcel ? aboveGround(analysis.parcel.path.lcl.p) : null],
      ...(observed.column
        ? ([
            [t("soundingIndexModelOutput", { name: "CAPE" }), joules(observed.column.cape)],
            [t("soundingIndexModelOutput", { name: "CIN" }), joules(observed.column.cin)],
          ] satisfies [string, string | null][])
        : ([
            ["CAPE", analysis.parcel ? joules(analysis.parcel.cape) : null],
            ["CIN", joules(analysis.parcel?.cin ?? null)],
          ] satisfies [string, string | null][])),
      [t("soundingIndexCcl"), convective ? aboveGround(convective.p) : null],
      [t("soundingIndexTcon"), convective ? formatTemperature(convective.tcon, 0) : null],
    ];
    indices.replaceChildren(
      ...entries
        .filter((item): item is [string, string] => item[1] !== null)
        .map(([name, value]) => {
          const item = element("div", "sounding-index");
          item.append(element("dt", "", name), element("dd", "", value));
          return item;
        }),
    );
  }

  /** The guide's six sentences. */
  function writeGuide(): void {
    guideTitle.textContent = t("soundingGuideTitle");
    guideList.replaceChildren(
      ...(
        [
          "soundingGuideAxes",
          "soundingGuideMoisture",
          "soundingGuideInversion",
          "soundingGuideFreezing",
          "soundingGuideParcel",
          "soundingGuideWind",
        ] as const
      ).map((key) => element("li", "", t(key, barbParams()))),
    );
  }

  /** Everything beside the chart that follows from the profile, the model
   * frame, the units and the language — written when one of them changes
   * rather than on every frame the panel repaints. */
  function writeInsight(time: string): void {
    const shown = reading();
    const model = shown?.column ? null : options.modelProfile();
    const key = [
      cached?.key ?? "-",
      shown?.column?.stamp ?? "-",
      model?.run ?? "-",
      model?.validTime ?? "-",
      model?.profile.n ?? -1,
      options.modelPossible(),
      units.temperature,
      units.height,
      units.wind,
      units.precipitation,
      t("soundingGuideTitle"),
      time === "" ? "" : options.formatTime(time),
    ].join("|");
    if (key === insightKey) return;
    insightKey = key;
    writeGuide();
    if (!shown) {
      legend.replaceChildren();
      summary.replaceChildren();
      indices.replaceChildren();
      return;
    }
    writeLegend(shown, time);
    writeSummary(shown);
    writeIndices(shown);
  }

  /** The source switch: the ascent nearby or the run's own column, shown
   * only where the pin has both. */
  function syncSources(): void {
    const both = entry !== null && options.columnPossible();
    sources.hidden = !both;
    const current = profileSource();
    const choices: [Source, string][] =
      both && entry
        ? [
            [
              "observed",
              `${t("soundingObserved")} ${stationCode(entry)} · ${formatDistance(distanceKm, distanceKm < 10 ? 1 : 0)}`,
            ],
            ["model", `${t("soundingModel")} ${options.modelName()}`],
          ]
        : [];
    const key = [current, ...choices.map(([, label]) => label)].join("|");
    if (key === sourcesKey) return;
    sourcesKey = key;
    sources.replaceChildren(
      ...choices.map(([choice, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "sounding-time";
        button.textContent = label;
        if (choice === current) button.setAttribute("aria-current", "true");
        button.addEventListener("click", () => {
          if (profileSource() === choice) return;
          preferred = choice;
          pointer = null;
          ensureStation();
          render();
          // The other profile reads other bundles of the run.
          options.onChange();
        });
        return button;
      }),
    );
  }

  /** Let the notes beside the chart grow and never shrink while the pin,
   * the source and their width stay what they are (`notesHeight`). */
  function keepNotesHeight(): void {
    const key = `${profileSource()}|${entry?.id ?? "-"}|${Math.round(side.clientWidth)}`;
    if (key !== notesKey) {
      notesKey = key;
      notesHeight = 0;
      side.style.minHeight = "";
    }
    const height = side.offsetHeight;
    if (height > notesHeight) {
      notesHeight = height;
      side.style.minHeight = `${height}px`;
    }
  }

  function render(): void {
    columnMemo = undefined;
    if (!entry && !options.columnPossible()) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const model = profileSource() === "model";
    const ascent = model ? null : sounding();
    const time = model ? "" : (ascent?.time ?? entry?.latest ?? "");
    if (model) {
      // The column is the run's, at the frame on screen.
      const column = currentColumn();
      line.textContent = column ? `${column.run} · ${column.stamp}` : `${t("soundingModel")} ${options.modelName()}`;
    } else if (entry) {
      line.textContent = [
        entry.name ? `${stationCode(entry)} ${entry.name}` : stationCode(entry),
        formatDistance(distanceKm, distanceKm < 10 ? 1 : 0),
        options.formatTime(time),
        synopticHour(time),
        ageLabel(time),
      ]
        .filter((part) => part !== "")
        .join(" · ");
    }
    expand.textContent = t("soundingExpand");
    if (!open) return;
    syncSources();
    if (model) {
      times.hidden = true;
      if (timesKey !== "") {
        timesKey = "";
        times.replaceChildren();
      }
    } else {
      syncTimes();
    }
    const shown = reading();
    status.textContent = shown ? "" : failed && !model ? t("soundingUnavailable") : t("soundingLoading");
    status.hidden = status.textContent === "";
    side.dataset.ready = String(shown !== null);
    writeInsight(time);
    keepNotesHeight();
    const width = canvas.clientWidth;
    const height = sheetMount ? sheetChartHeight(width) : (options.chartHeight?.(width) ?? CHART_HEIGHT);
    const mark = signature(width, height);
    if (chart === null || painted !== mark) {
      if (paint(width, height)) painted = mark;
    }
    writeReadout();
  }

  return {
    root,
    expandButton: expand,
    setStation(nextLoaded, nextEntry, nextDistance) {
      loaded = nextLoaded;
      distanceKm = nextDistance;
      notesKey = "";
      const previous = entry;
      entry = nextEntry;
      if (!nextEntry) {
        // The pin moved off every station: drop the fetched line so a later
        // pin near the same one asks again against the index of the day.
        // What is left to show, if the run has the levels for it, is its
        // own column — folded unless the viewer has opened the chart by
        // hand, since opening it reads some forty bundles of the run.
        station = null;
        fetchKey = null;
        fetchSequence += 1;
        if (previous) {
          pointer = null;
          open = chosenOpen ?? false;
          head.setAttribute("aria-expanded", String(open));
          body.hidden = !open;
        }
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
        // whole screen. A choice made by hand outlasts both.
        const wide = window.innerWidth > PHONE_WIDTH && window.innerHeight > SHORT_HEIGHT;
        open = chosenOpen ?? (wide && options.wantsOpen());
        head.setAttribute("aria-expanded", String(open));
        body.hidden = !open;
        ensureStation();
      }
      render();
    },
    selectedTime() {
      return profileSource() === "observed" ? (sounding()?.time ?? null) : null;
    },
    isOpen() {
      return open && (entry !== null || options.columnPossible());
    },
    wantsColumn() {
      return open && profileSource() === "model" && options.columnPossible();
    },
    open() {
      chosenOpen = true;
      setOpen(true);
    },
    close() {
      chosenOpen = false;
      setOpen(false);
    },
    dock(host) {
      const next = host ?? root;
      if (home === next) return;
      home = next;
      if (sheetMount) return;
      home.append(body);
      pointer = null;
      render();
    },
    caption() {
      return line.textContent ?? "";
    },
    render,
    enterSheet(mount) {
      if (sheetMount === mount) return;
      sheetMount = mount;
      mount.append(body);
      pointer = null;
      render();
    },
    leaveSheet() {
      if (!sheetMount) return;
      sheetMount = null;
      home.append(body);
      // The switch stays with the sheet, so the panel's chart is a skew-T
      // again whatever the sheet was showing.
      emagram = false;
      for (const [position, button] of projectionButtons.entries()) {
        button.setAttribute("aria-pressed", String(position === 0));
      }
      pointer = null;
      render();
    },
    headline() {
      if (profileSource() === "model") return `${t("soundingModel")} ${options.modelName()}`;
      if (!entry) return "";
      const named = entry.name ? ` ${entry.name}` : "";
      return `${stationCode(entry)}${named}`;
    },
  };
}
