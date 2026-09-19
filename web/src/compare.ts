import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import {
  COLUMN_PX,
  DEFAULT_SPAN,
  SPANS,
  buildAxis,
  dayBands,
  hourColumns,
  hourLabel,
  layoutFrames,
  layoutReports,
  xOf,
  type Axis,
  type FrameCell,
  type SpanHours,
} from "./compare/axis";
import {
  comparableModels,
  fetchModelRun,
  modelCoversPoint,
  publishedOf,
  readPointSeries,
  seriesDirections,
  seriesValues,
  type PointSeries,
} from "./compare/series";
import {
  applyStaticMessages,
  htmlLang,
  locale,
  localeHtmlLang,
  LOCALES,
  onLocaleChange,
  setLocale,
  t,
  type Locale,
} from "./i18n";
import { vectorMaxMagnitude } from "./levels";
import { FORECAST_MODELS, type ForecastBundleId, type ForecastModelId, type LoadedManifest } from "./manifest";
import { applyPageMeta } from "./pagemeta";
import { buildPalette, precipitationColor } from "./palettes";
import { geoGrid } from "./probe";
import { createSheet, fillLanguageList } from "./sheet";
import { fetchAirportIndex, fetchAirportStation } from "./stations/fetch";
import { nearestAirport } from "./stations/nearest";
import { cloudCoverPercent } from "./stations/observations";
import type { AirportStationHistory, Metar } from "./stations/schema";
import { applyTheme, onThemeChange, toggleTheme } from "./theme";
import { browserZone } from "./timezone";
import { displayUnit, displayValue } from "./units";
import { variableSpec } from "./variables";

/**
 * The comparison page.
 *
 * One point, every live forecast model, one clock. Each model is a block:
 * its run in the header, a row of local hours, and a row per field the
 * page compares, each cell the model's value at that frame, tinted by the
 * viewer's own palette for the field. The nearest airport's reports are a
 * block of the same shape above them, over the hours the runs reach back
 * to, so a forecast can be read against what was measured.
 *
 * No map and no playback: the data behind the page is one series per
 * field per model, read out of each bundle's store at the point's cell
 * (`compare/series.ts`), and the layout is arithmetic on real time
 * (`compare/axis.ts`). The state is the query string — `lat`, `lon`,
 * `span`, `fields` — so a comparison is a link; which models are hidden or
 * pinned is this device's preference and stays in localStorage.
 */

applyStaticMessages();
applyTheme();
applyPageMeta({ path: "/compare.html" });

// ---------------------------------------------------------------------------
// The fields the page can compare, in row order. Each names the bundle a
// model would publish it under and how a METAR reports the same quantity.

type FieldId = "tmp2m" | "dpt2m" | "prate" | "wind10m" | "gust" | "tcdc" | "prmsl";

interface FieldSpec {
  id: FieldId;
  /** The quantity off a report, null where a METAR does not carry it (a
   * rate is never reported). */
  observed: ((metar: Metar) => number | null) | null;
  /** Written beside the observed value; the model's unit comes off its
   * bundle. */
  observedUnit: string;
}

const FIELDS: readonly FieldSpec[] = [
  { id: "tmp2m", observed: (metar) => metar.t, observedUnit: "°C" },
  { id: "dpt2m", observed: (metar) => metar.td, observedUnit: "°C" },
  { id: "prate", observed: null, observedUnit: "mm/h" },
  { id: "wind10m", observed: (metar) => metar.ws, observedUnit: "m/s" },
  { id: "gust", observed: (metar) => metar.gust, observedUnit: "m/s" },
  { id: "tcdc", observed: (metar) => cloudCoverPercent(metar.cloud), observedUnit: "%" },
  { id: "prmsl", observed: (metar) => metar.slp ?? metar.qnh, observedUnit: "hPa" },
];
const FIELD_IDS: readonly FieldId[] = FIELDS.map((field) => field.id);
const DEFAULT_FIELDS: readonly FieldId[] = ["tmp2m", "prate", "wind10m"];

const DEFAULT_POINT = { latitude: 35.68, longitude: 139.69 };
const MODEL_PREFERENCES_KEY = "xue-compare-models";

// ---------------------------------------------------------------------------
// State.

interface PageState {
  latitude: number;
  longitude: number;
  span: SpanHours;
  fields: FieldId[];
}

interface ModelPreferences {
  hidden: ForecastModelId[];
  /** Models pinned to the top, in pin order. */
  pinned: ForecastModelId[];
}

interface ModelResult {
  model: ForecastModelId;
  run: LoadedManifest | null;
  error: string | null;
  /** Null: the point is off this bundle's grid. Absent: not read yet. */
  series: Map<ForecastBundleId, PointSeries | null>;
  /** Bundles still being read. */
  reading: Set<ForecastBundleId>;
  /** Bundles whose store could not be read, with the decoder's message. */
  failed: Map<ForecastBundleId, string>;
}

function parseState(search: string): PageState {
  const params = new URLSearchParams(search);
  const number = (name: string, min: number, max: number, fallback: number) => {
    const value = Number(params.get(name));
    return params.has(name) && Number.isFinite(value) && value >= min && value <= max ? value : fallback;
  };
  const span = Number(params.get("span"));
  const fields = (params.get("fields") ?? "")
    .split(",")
    .filter((id): id is FieldId => (FIELD_IDS as readonly string[]).includes(id));
  return {
    latitude: number("lat", -90, 90, DEFAULT_POINT.latitude),
    longitude: number("lon", -180, 180, DEFAULT_POINT.longitude),
    span: (SPANS as readonly number[]).includes(span) ? (span as SpanHours) : DEFAULT_SPAN,
    fields: fields.length > 0 ? FIELD_IDS.filter((id) => fields.includes(id)) : [...DEFAULT_FIELDS],
  };
}

function searchForState(state: PageState): string {
  const params = new URLSearchParams(window.location.search);
  params.set("lat", state.latitude.toFixed(2));
  params.set("lon", state.longitude.toFixed(2));
  if (state.span === DEFAULT_SPAN) params.delete("span");
  else params.set("span", String(state.span));
  const fields = state.fields.join(",");
  if (fields === DEFAULT_FIELDS.join(",")) params.delete("fields");
  else params.set("fields", fields);
  return `?${params.toString()}`;
}

function readPreferences(): ModelPreferences {
  try {
    const raw = localStorage.getItem(MODEL_PREFERENCES_KEY);
    if (!raw) return { hidden: [], pinned: [] };
    const parsed = JSON.parse(raw) as Partial<ModelPreferences>;
    const known = (list: unknown): ForecastModelId[] =>
      Array.isArray(list) ? list.filter((id): id is ForecastModelId => typeof id === "string" && id in FORECAST_MODELS) : [];
    return { hidden: known(parsed.hidden), pinned: known(parsed.pinned) };
  } catch {
    return { hidden: [], pinned: [] };
  }
}

function writePreferences(preferences: ModelPreferences): void {
  try {
    localStorage.setItem(MODEL_PREFERENCES_KEY, JSON.stringify(preferences));
  } catch {
    // Without storage the choice lasts this page load only.
  }
}

let state = parseState(window.location.search);
let preferences = readPreferences();
const results = new Map<ForecastModelId, ModelResult>();
let airport: { station: AirportStationHistory; distanceKm: number } | null | undefined;
let axis: Axis | null = null;
let loadController: AbortController | null = null;
let scrolledToNow = false;

// ---------------------------------------------------------------------------
// The DOM.

const controls = document.getElementById("compare-controls") as HTMLFormElement;
const latInput = document.getElementById("compare-lat") as HTMLInputElement;
const lonInput = document.getElementById("compare-lon") as HTMLInputElement;
const locateButton = document.getElementById("compare-locate") as HTMLButtonElement;
const spanGroup = document.getElementById("compare-spans") as HTMLDivElement;
const fieldGroup = document.getElementById("compare-fields") as HTMLDivElement;
const status = document.getElementById("compare-status") as HTMLParagraphElement;
const scroll = document.getElementById("compare-scroll") as HTMLDivElement;
const blocks = document.getElementById("compare-blocks") as HTMLDivElement;
const hiddenSection = document.getElementById("compare-hidden") as HTMLElement;
const hiddenList = document.getElementById("compare-hidden-list") as HTMLUListElement;
const mapLink = document.getElementById("compare-map-link") as HTMLAnchorElement;

document.getElementById("theme-toggle")?.addEventListener("click", () => toggleTheme());

const langTrigger = document.getElementById("lang-toggle");
const langSheet = document.getElementById("lang-sheet");
const langList = document.getElementById("lang-list");
if (langTrigger && langSheet && langList) {
  const langSheetControl = createSheet({
    trigger: langTrigger,
    sheet: langSheet,
    initialFocus: (sheet) => sheet.querySelector<HTMLButtonElement>("button[aria-current]"),
  });
  const renderLanguageList = () =>
    fillLanguageList(langList, LOCALES, {
      current: locale,
      htmlLang: localeHtmlLang,
      onPick: (next: Locale) => {
        langSheetControl.close();
        setLocale(next);
      },
    });
  renderLanguageList();
  onLocaleChange(renderLanguageList);
}

function dataBaseUrl(): string {
  return import.meta.env.VITE_DATA_BASE_URL || "data/";
}

function say(text: string | null, error = false): void {
  status.hidden = text === null;
  status.textContent = text ?? "";
  status.classList.toggle("is-error", error);
}

// ---------------------------------------------------------------------------
// Formatting.

function formatRun(runTime: string): string {
  const parsed = new Date(runTime);
  if (Number.isNaN(parsed.getTime())) return "--";
  const pad = (item: number) => String(item).padStart(2, "0");
  return `${parsed.getUTCFullYear()}-${pad(parsed.getUTCMonth() + 1)}-${pad(parsed.getUTCDate())} ${pad(parsed.getUTCHours())}Z`;
}

function formatDegrees(value: number, axisName: "NS" | "EW"): string {
  const hemisphere = axisName === "NS" ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W";
  return `${Math.abs(value).toFixed(2)}°${hemisphere}`;
}

function formatGridStep(step: number): string {
  const text = Math.abs(step).toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  return `${text}°`;
}

/** The text a cell shows for a field's value. A dry hour is blank, as on
 * every such table: a column of zeros hides the rain among them. */
function formatCell(field: FieldId, value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "";
  switch (field) {
    case "tmp2m":
    case "dpt2m":
      return `${Math.round(value)}°`;
    case "prate":
      return value < 0.05 ? "" : value < 10 ? value.toFixed(1) : String(Math.round(value));
    case "prmsl":
      return String(Math.round(value));
    default:
      return String(Math.round(value));
  }
}

function fieldLabel(field: FieldId): string {
  return variableSpec(field)?.label() ?? field.toUpperCase();
}

function fieldCode(field: FieldId): string {
  const spec = variableSpec(field);
  return spec?.meteogramCode ?? spec?.code ?? field.toUpperCase();
}

// ---------------------------------------------------------------------------
// Tints: the viewer's own palette for the field, at an ink-light alpha, so
// a warm afternoon reads in the map's orange and heavy rain in its blue.

const paletteCache = new WeakMap<PointSeries, Uint8Array>();

function paletteOf(series: PointSeries): Uint8Array {
  let palette = paletteCache.get(series);
  if (!palette) {
    palette = buildPalette(series.variables[0]!, series.identity?.identity ?? null);
    paletteCache.set(series, palette);
  }
  return palette;
}

function rgba(palette: Uint8Array, index: number, alpha: number): string {
  const at = Math.max(0, Math.min(255, index)) * 4;
  return `rgba(${palette[at]}, ${palette[at + 1]}, ${palette[at + 2]}, ${alpha})`;
}

const TINT_ALPHA = 0.34;

function tintFor(field: FieldId, series: PointSeries, frame: number, value: number | null): string | null {
  if (value === null) return null;
  if (field === "prate") {
    if (value < 0.05) return null;
    const [r, g, b] = precipitationColor(value);
    return `rgba(${r}, ${g}, ${b}, ${TINT_ALPHA})`;
  }
  if (field === "wind10m" || field === "gust") {
    const identity = series.identity?.identity ?? null;
    const max = field === "gust" ? 40 : vectorMaxMagnitude(identity?.family ?? "wind", identity?.level ?? null);
    return rgba(paletteOf(series), Math.round((255 * value) / max), TINT_ALPHA);
  }
  if (field === "prmsl") return null;
  return rgba(paletteOf(series), series.codes[0]![frame]!, TINT_ALPHA);
}

// ---------------------------------------------------------------------------
// Rendering.

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function arrowSvg(fromDegrees: number): SVGSVGElement {
  // The arrow points where the wind goes: the direction it comes from, turned about.
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 12 12");
  svg.setAttribute("width", "11");
  svg.setAttribute("height", "11");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("cmp-arrow");
  svg.style.transform = `rotate(${Math.round(fromDegrees + 180)}deg)`;
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M6 1.2L6 10.8M6 1.2L2.6 4.6M6 1.2L9.4 4.6");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.6");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

function cell(frame: FrameCell, text: string, tint: string | null, direction: number | null = null): HTMLSpanElement {
  const node = element("span", "cmp-cell");
  node.style.left = `${frame.x}px`;
  node.style.width = `${frame.width}px`;
  if (tint) node.style.background = tint;
  if (direction !== null) node.append(arrowSvg(direction));
  node.append(document.createTextNode(text));
  return node;
}

/** A row: the sticky label and the track the cells sit on. */
function row(className: string, label: Node | string, cells: readonly HTMLElement[], width: number): HTMLDivElement {
  const node = element("div", `cmp-row ${className}`);
  const labelNode = element("div", "cmp-label");
  labelNode.append(label);
  const track = element("div", "cmp-track");
  track.style.width = `${width}px`;
  track.append(...cells);
  node.append(labelNode, track);
  return node;
}

function fieldLabelNode(field: FieldId, unit: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  fragment.append(element("span", "cmp-label-name", fieldLabel(field)), element("span", "cmp-label-unit", unit));
  return fragment;
}

/** The rows every block opens with: the days, then the columns' local
 * hours. The hours are the block's own frames, so a six-hourly model shows
 * six-hourly readings and an hourly one every column. */
function clockRows(ax: Axis, frames: readonly FrameCell[], zone: string): HTMLDivElement[] {
  const days = dayBands(ax, zone, htmlLang).map((band) => {
    const node = element("span", "cmp-day", band.label);
    node.style.left = `${band.x}px`;
    node.style.width = `${band.width}px`;
    return node;
  });
  const hours = frames.map((frame) => {
    const hour = hourColumns({ ...ax, startMs: frame.startMs, endMs: frame.startMs + 1 }, zone)[0]!.hour;
    return cell(frame, hourLabel(hour), null);
  });
  return [row("cmp-days", "", days, ax.widthPx), row("cmp-hours", t("compareHours"), hours, ax.widthPx)];
}

/** The night columns and the present, behind a block's rows. */
function shading(ax: Axis, zone: string): HTMLDivElement {
  const layer = element("div", "cmp-shade");
  layer.style.width = `${ax.widthPx}px`;
  for (const column of hourColumns(ax, zone)) {
    if (!column.night) continue;
    const night = element("span", "cmp-night");
    night.style.left = `${column.x}px`;
    night.style.width = `${COLUMN_PX}px`;
    layer.append(night);
  }
  const now = element("span", "cmp-now");
  now.style.left = `${xOf(ax, ax.nowMs)}px`;
  now.title = t("compareNow");
  layer.append(now);
  return layer;
}

function blockShell(id: string, title: string, meta: readonly (HTMLElement | string)[]): { section: HTMLElement; head: HTMLElement; grid: HTMLDivElement } {
  const section = element("section", "cmp-block");
  section.dataset.block = id;
  const head = element("header", "cmp-head");
  const name = element("h2", "cmp-name", title);
  const details = element("span", "cmp-meta");
  details.append(...meta);
  head.append(name, details);
  const grid = element("div", "cmp-grid");
  section.append(head, grid);
  return { section, head, grid };
}

function modelBlock(result: ModelResult, ax: Axis, zone: string): HTMLElement {
  const info = FORECAST_MODELS[result.model];
  const meta: (HTMLElement | string)[] = [];
  const first = [...result.series.values()].find((series): series is PointSeries => series !== null);
  if (first) meta.push(element("span", "cmp-grid-step", formatGridStep(geoGrid(first.metadata).longitudeStep)));
  if (result.run) meta.push(element("span", "cmp-run", `${t("compareRun")} ${formatRun(result.run.manifest.runTime)}`));
  const { section, head, grid } = blockShell(result.model, info.label, meta);

  const actions = element("div", "cmp-actions");
  const pinned = preferences.pinned.includes(result.model);
  const pin = element("button", "cmp-action", t("comparePin"));
  pin.type = "button";
  pin.setAttribute("aria-pressed", String(pinned));
  pin.addEventListener("click", () => {
    preferences.pinned = pinned
      ? preferences.pinned.filter((id) => id !== result.model)
      : [result.model, ...preferences.pinned.filter((id) => id !== result.model)];
    writePreferences(preferences);
    render();
  });
  const hide = element("button", "cmp-action", t("compareHide"));
  hide.type = "button";
  hide.addEventListener("click", () => {
    preferences.hidden = [...preferences.hidden.filter((id) => id !== result.model), result.model];
    writePreferences(preferences);
    render();
  });
  actions.append(pin, hide);
  head.append(actions);

  if (result.error) {
    const note = element("p", "cmp-note is-error", t("compareModelFailed", { model: info.label, message: result.error }));
    section.append(note);
    return section;
  }
  if (!result.run) {
    section.append(element("p", "cmp-note", t("compareLoading", { model: info.label })));
    return section;
  }
  // Every series the run has at this point read on the run's own clock:
  // the block's hours are its first field's frames, the widest axis the
  // run publishes (the de-accumulated rate may start one step late).
  const published = publishedOf(result.run, state.fields);
  const lead = published.map((id) => result.series.get(id)).find((series): series is PointSeries => series !== null && series !== undefined);
  // What could not be read is said under the block, field by field, and
  // the fields that could are still drawn.
  const failures = [...result.failed].filter(([id]) => published.includes(id));
  const failureNote = failures.length
    ? element(
        "p",
        "cmp-note is-error",
        t("compareModelFailed", { model: info.label, message: failures.map(([id, message]) => `${id}: ${message}`).join(" · ") }),
      )
    : null;
  if (!lead) {
    const offGrid = published.length > 0 && published.every((id) => result.series.get(id) === null);
    const text = offGrid ? `${info.label} ${t("compareOffGrid")}` : result.reading.size > 0 ? t("compareLoading", { model: info.label }) : "";
    if (text) section.append(element("p", "cmp-note", text));
    if (failureNote) section.append(failureNote);
    return section;
  }
  grid.append(shading(ax, zone));
  grid.append(...clockRows(ax, layoutFrames(ax, lead.validTimesMs), zone));
  for (const field of state.fields) {
    if (!published.includes(field)) continue;
    const series = result.series.get(field);
    if (series === null || result.failed.has(field)) continue;
    if (series === undefined) {
      grid.append(row("cmp-field is-reading", fieldLabelNode(field, ""), [], ax.widthPx));
      continue;
    }
    const values = seriesValues(series);
    const directions = field === "wind10m" ? seriesDirections(series) : [];
    const unit = displayUnit(series.variables[0]!.unit);
    const cells = layoutFrames(ax, series.validTimesMs).map((frame) => {
      const raw = values[frame.index] ?? null;
      const value = raw === null ? null : displayValue(series.variables[0]!.unit, raw);
      return cell(frame, formatCell(field, value), tintFor(field, series, frame.index, raw), directions[frame.index] ?? null);
    });
    grid.append(row(`cmp-field cmp-${field}`, fieldLabelNode(field, unit), cells, ax.widthPx));
  }
  if (failureNote) section.append(failureNote);
  return section;
}

function airportBlock(ax: Axis, zone: string): HTMLElement | null {
  if (!airport) return null;
  const { station, distanceKm } = airport;
  const reports = [...station.metars].reverse();
  const frames = layoutReports(ax, reports.map((metar) => Date.parse(metar.time)));
  if (frames.length === 0) return null;
  const meta: (HTMLElement | string)[] = [element("span", "cmp-grid-step", `${Math.round(distanceKm)} km`)];
  if (station.name) meta.push(element("span", "cmp-run", station.name));
  const { section, grid } = blockShell("airport", t("compareObservationsFrom", { icao: station.icao }), meta);
  section.classList.add("is-observed");
  grid.append(shading(ax, zone));
  grid.append(...clockRows(ax, frames, zone));
  for (const field of state.fields) {
    const spec = FIELDS.find((item) => item.id === field)!;
    if (!spec.observed) continue;
    const cells = frames.flatMap((frame) => {
      const metar = reports[frame.index]!;
      const value = spec.observed!(metar);
      if (value === null) return [];
      const direction = field === "wind10m" ? metar.wd : null;
      return [cell(frame, formatCell(field, value), null, direction)];
    });
    if (cells.length === 0) continue;
    grid.append(row(`cmp-field cmp-${field}`, fieldLabelNode(field, spec.observedUnit), cells, ax.widthPx));
  }
  return section;
}

/** Visible models in display order: pinned first in pin order, then the
 * registry's order; hidden ones listed below the blocks. */
function orderedModels(): { shown: ForecastModelId[]; hidden: ForecastModelId[] } {
  const all = [...results.keys()];
  const hidden = all.filter((id) => preferences.hidden.includes(id));
  const visible = all.filter((id) => !hidden.includes(id));
  const pinned = preferences.pinned.filter((id) => visible.includes(id));
  return { shown: [...pinned, ...visible.filter((id) => !pinned.includes(id))], hidden };
}

function render(): void {
  const zone = browserZone();
  const ax = axis;
  const { shown, hidden } = orderedModels();
  const nodes: HTMLElement[] = [];
  if (ax) {
    const observed = airportBlock(ax, zone);
    if (observed) nodes.push(observed);
    for (const id of shown) nodes.push(modelBlock(results.get(id)!, ax, zone));
  }
  blocks.replaceChildren(...nodes);
  hiddenSection.hidden = hidden.length === 0;
  hiddenList.replaceChildren(
    ...hidden.map((id) => {
      const item = element("li");
      const show = element("button", "cmp-action", `${FORECAST_MODELS[id].label} · ${t("compareShow")}`);
      show.type = "button";
      show.addEventListener("click", () => {
        preferences.hidden = preferences.hidden.filter((other) => other !== id);
        writePreferences(preferences);
        render();
      });
      item.append(show);
      return item;
    }),
  );
  // Once the first grid is on the page (before that the track has no
  // width and the scroll would clamp to zero), start the view a couple of
  // columns before the present.
  if (ax && !scrolledToNow && nodes.some((node) => node.querySelector(".cmp-track"))) {
    scrolledToNow = true;
    scroll.scrollLeft = Math.max(0, xOf(ax, ax.nowMs) - 2 * COLUMN_PX);
  }
}

function renderControls(): void {
  latInput.value = state.latitude.toFixed(2);
  lonInput.value = state.longitude.toFixed(2);
  for (const button of spanGroup.querySelectorAll<HTMLButtonElement>("button[data-span]")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.span) === state.span));
  }
  fieldGroup.replaceChildren(
    ...FIELDS.map((field) => {
      const button = element("button", undefined, fieldCode(field.id));
      button.type = "button";
      button.dataset.field = field.id;
      button.title = fieldLabel(field.id);
      button.setAttribute("aria-pressed", String(state.fields.includes(field.id)));
      return button;
    }),
  );
  const url = new URL("./", window.location.href);
  url.hash = `map=7/${state.latitude.toFixed(2)}/${state.longitude.toFixed(2)}`;
  mapLink.href = url.href;
}

function syncUrl(): void {
  const search = searchForState(state);
  if (search !== window.location.search) {
    window.history.replaceState(null, "", `${window.location.pathname}${search}${window.location.hash}`);
  }
}

function documentTitle(): void {
  applyPageMeta({
    path: "/compare.html",
    title: `${t("compareTitle")} · ${formatDegrees(state.latitude, "NS")} ${formatDegrees(state.longitude, "EW")}`,
  });
}

// ---------------------------------------------------------------------------
// Loading.

/** A pool over the bundles of one model: two stores open at a time, so a
 * page of six models holds a dozen workers at most. */
async function readModel(result: ModelResult, signal: AbortSignal): Promise<void> {
  const run = result.run;
  if (!run) return;
  const queue = publishedOf(run, state.fields).filter((id) => !result.series.has(id) && !result.reading.has(id) && !result.failed.has(id));
  for (const id of queue) result.reading.add(id);
  const worker = async () => {
    for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
      try {
        const series = await readPointSeries(run, id, state.longitude, state.latitude, signal);
        if (signal.aborted) return;
        result.series.set(id, series);
      } catch (error) {
        if (signal.aborted) return;
        // Diagnostics stay English; the row is simply absent, the note names why.
        console.warn(`compare: ${result.model} ${id} not read:`, error instanceof Error ? error.message : error);
        result.failed.set(id, error instanceof Error ? error.message : String(error));
      } finally {
        result.reading.delete(id);
      }
      render();
    }
  };
  await Promise.all([worker(), worker()]);
}

async function loadAirport(signal: AbortSignal): Promise<void> {
  try {
    const index = await fetchAirportIndex(dataBaseUrl());
    if (signal.aborted || !index) {
      airport = null;
      return;
    }
    const nearest = nearestAirport(index.index, state.longitude, state.latitude);
    if (!nearest) {
      airport = null;
      return;
    }
    const station = await fetchAirportStation(index, nearest.station);
    if (signal.aborted) return;
    airport = { station, distanceKm: nearest.distanceKm };
  } catch (error) {
    if (signal.aborted) return;
    console.warn("compare: airport reports not read:", error instanceof Error ? error.message : error);
    airport = null;
  }
  render();
}

async function load(): Promise<void> {
  loadController?.abort();
  const controller = new AbortController();
  loadController = controller;
  const { signal } = controller;
  results.clear();
  airport = undefined;
  axis = null;
  scrolledToNow = false;
  say(null);
  documentTitle();

  const models = comparableModels().filter((id) => modelCoversPoint(id, state.longitude, state.latitude));
  for (const id of models) results.set(id, { model: id, run: null, error: null, series: new Map(), reading: new Set(), failed: new Map() });
  if (models.length === 0) {
    say(t("compareNoModels"), true);
    render();
    return;
  }
  render();
  void loadAirport(signal);

  // Every pointer and manifest first: the axis begins at the earliest run.
  await Promise.all(
    models.map(async (id) => {
      const result = results.get(id)!;
      try {
        result.run = await fetchModelRun(dataBaseUrl(), id);
      } catch (error) {
        result.error = error instanceof Error ? error.message : String(error);
      }
    }),
  );
  if (signal.aborted) return;
  const runTimes = [...results.values()]
    .map((result) => (result.run ? Date.parse(result.run.manifest.runTime) : NaN))
    .filter((time) => Number.isFinite(time));
  axis = buildAxis(runTimes, Date.now(), state.span);
  render();
  await Promise.all([...results.values()].map((result) => readModel(result, signal)));
  if (signal.aborted) return;
  render();
}

/** The horizon changed: the same series, on a new axis. */
function relayout(): void {
  const runTimes = [...results.values()]
    .map((result) => (result.run ? Date.parse(result.run.manifest.runTime) : NaN))
    .filter((time) => Number.isFinite(time));
  if (axis) axis = buildAxis(runTimes, axis.nowMs, state.span);
  scrolledToNow = false;
  render();
}

/** A field was added: read it where it is not in hand yet. */
function readMissing(): void {
  if (!loadController) return;
  const { signal } = loadController;
  for (const result of results.values()) {
    if (result.run) void readModel(result, signal);
  }
}

// ---------------------------------------------------------------------------
// Controls.

controls.addEventListener("submit", (event) => {
  event.preventDefault();
  const latitude = Number(latInput.value);
  const longitude = Number(lonInput.value);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
  if (latitude === state.latitude && longitude === state.longitude) return;
  state = { ...state, latitude: Math.max(-90, Math.min(90, latitude)), longitude: Math.max(-180, Math.min(180, longitude)) };
  renderControls();
  syncUrl();
  void load();
});
for (const input of [latInput, lonInput]) input.addEventListener("change", () => controls.requestSubmit());

locateButton.addEventListener("click", () => {
  if (!navigator.geolocation) {
    say(t("compareLocateFailed"), true);
    return;
  }
  locateButton.disabled = true;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      locateButton.disabled = false;
      latInput.value = position.coords.latitude.toFixed(2);
      lonInput.value = position.coords.longitude.toFixed(2);
      controls.requestSubmit();
    },
    () => {
      locateButton.disabled = false;
      say(t("compareLocateFailed"), true);
    },
    { maximumAge: 600_000, timeout: 15_000 },
  );
});

spanGroup.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-span]");
  if (!button) return;
  const span = Number(button.dataset.span) as SpanHours;
  if (span === state.span) return;
  state = { ...state, span };
  renderControls();
  syncUrl();
  relayout();
});

fieldGroup.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-field]");
  if (!button) return;
  const field = button.dataset.field as FieldId;
  const fields = state.fields.includes(field) ? state.fields.filter((id) => id !== field) : FIELD_IDS.filter((id) => id === field || state.fields.includes(id));
  if (fields.length === 0) return;
  state = { ...state, fields };
  renderControls();
  syncUrl();
  render();
  readMissing();
});

onLocaleChange(() => {
  renderControls();
  documentTitle();
  render();
});
onThemeChange(() => render());

renderControls();
syncUrl();
void load();
