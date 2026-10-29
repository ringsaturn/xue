import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import { Map as MaplibreMap, NavigationControl, Popup, setWorkerUrl, type StyleSpecification } from "maplibre-gl";
// See main.ts: the worker has to be its own chunk once Vite has bundled.
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

import { element } from "./dom";
import type { GifHost } from "./gifexport";
import { deliverMp4, exportMp4, isMp4ExportSupported, MP4_MAX_FRAMES, mp4FileName } from "./mp4export";
import { formatUtcHour } from "./format";
import { applyStaticMessages, basemapLang, locale, onLocaleChange, t } from "./i18n";
import { applyPageMeta } from "./pagemeta";
import { mountLanguagePicker } from "./sheet";
import { dataBaseUrl } from "./site";
import { applyTheme, isDark, onThemeChange, toggleTheme } from "./theme";
import { agencyCode, TC_AGENCIES, TC_MODELS } from "./tc/agencies";
import {
  archiveBounds,
  isArchiveStormId,
  forecastsAt,
  storyTimes,
  seasonOfStorm,
  sourcesOf,
  stormAt,
  stormCode,
  TcArchive,
  trackErrors,
  truthsOf,
  type ArchiveRecord,
  type BestRecord,
  type SeasonIndex,
  type SeasonStorm,
  type StormErrors,
} from "./tc/archive";
import { buildTcCard } from "./tc/card";
import { pointDataOf, StormLayers, TC_CLICKABLE_LAYERS } from "./tc/layers";
import { RADII_COLORS, SSHS_CLASSES } from "./tc/intensity";
import { localStormName } from "./tc/names";
import { seasonPagePath, stormPagePath } from "./tc/pages";

/**
 * Past tropical cyclones, from the archive (`docs/tc.md` §8–9), on one map.
 *
 * The panel picks the storm — the archive's seasons, the season's basins,
 * one row per storm with forecasts — and the map draws it in place. The
 * map draws the centres' best tracks and, for one issue time, the
 * forecast each source started from it — the live product's own layers,
 * fed a storm put together from the archive, with the playhead held at the
 * issue time so each forecast is drawn from its start. The slider steps
 * through the issue times; the table beside it is how far each source
 * missed, over the storm and for the forecast on the map.
 *
 * The address names the storm and the issue time (`?storm=WP062023&
 * issue=2023080100`); the page writes both back as they change. Without a
 * storm it opens on the latest one of the season running now.
 */

setWorkerUrl(maplibreWorkerUrl);
applyStaticMessages();
applyTheme();

const LEADS = [0, 24, 48, 72, 96, 120] as const;
const SOURCE_ORDER = [...Object.keys(TC_AGENCIES), ...Object.keys(TC_MODELS)];
const PROTOMAPS_KEY = import.meta.env.VITE_PROTOMAPS_KEY || "249bb192fefe0a77";
const KNOT = 0.514444;

const status = document.getElementById("typhoon-status") as HTMLParagraphElement;
const stormSection = document.getElementById("typhoon-storm") as HTMLElement;
const seasonsGroup = document.getElementById("typhoon-seasons") as HTMLDivElement;
const basinsGroup = document.getElementById("typhoon-basins") as HTMLDivElement;
const stormList = document.getElementById("typhoon-list") as HTMLUListElement;
const seasonNote = document.getElementById("typhoon-season-note") as HTMLParagraphElement;
const legend = document.getElementById("typhoon-legend") as HTMLDivElement;
const seasonLink = document.getElementById("typhoon-season-link") as HTMLAnchorElement;
const stormLink = document.getElementById("typhoon-storm-link") as HTMLAnchorElement;
const titleNode = document.getElementById("typhoon-title") as HTMLSpanElement;
const codeNode = document.getElementById("typhoon-code") as HTMLSpanElement;
const facts = document.getElementById("typhoon-facts") as HTMLDListElement;
const slider = document.getElementById("typhoon-slider") as HTMLInputElement;
const issueLabel = document.getElementById("typhoon-issue-time") as HTMLOutputElement;
const sourcesGroup = document.getElementById("typhoon-sources") as HTMLDivElement;
const truthsGroup = document.getElementById("typhoon-truths") as HTMLDivElement;
const table = document.getElementById("typhoon-errors") as HTMLTableElement;
document.getElementById("theme-toggle")?.addEventListener("click", () => toggleTheme());

const langTrigger = document.getElementById("lang-toggle");
const langSheet = document.getElementById("lang-sheet");
const langList = document.getElementById("lang-list");
if (langTrigger && langSheet && langList) onLocaleChange(mountLanguagePicker(langTrigger, langSheet, langList));

// --- the address ----------------------------------------------------------

interface PageState {
  storm: string | null;
  /** `YYYYMMDDHH`, or null for the storm's first issue time. */
  issue: string | null;
}

function parseState(search: string): PageState {
  const params = new URLSearchParams(search);
  const storm = params.get("storm")?.toUpperCase() ?? null;
  const issue = params.get("issue");
  return {
    storm: isArchiveStormId(storm) ? storm : null,
    issue: issue !== null && /^\d{10}$/.test(issue) ? issue : null,
  };
}

function issueKey(iso: string): string {
  return iso.slice(0, 13).replace(/[-T]/g, "");
}

function writeState(storm: string, issue: string | null): void {
  const url = new URL(window.location.href);
  url.searchParams.set("storm", storm);
  if (issue === null) url.searchParams.delete("issue");
  else url.searchParams.set("issue", issueKey(issue));
  if (url.href !== window.location.href) history.replaceState(null, "", url);
}

// --- what is loaded ----------------------------------------------------------

interface Loaded {
  entry: SeasonStorm;
  best: BestRecord | null;
  records: ArchiveRecord[];
  errors: StormErrors | null;
  /** The slider's stops: every best-track synoptic time and every issue
   * time (`storyTimes`), so a storm is followed past its last forecast. */
  issues: string[];
  sources: { agencies: string[]; models: string[] };
}

let loaded: Loaded | null = null;
let issueIndex = 0;
/** Sources switched off, by key; the best tracks under `best` and the
 * ensemble spaghetti under `members`. Members start off: fifty-one lines a
 * source hide the forecasts the page is about. */
const hidden = new Set<string>(["members"]);
let truth = "usa";

/** The storm's name in the reader's language where its basin has local
 * names (`tc/names.ts`), as issued otherwise. */
function stormName(entry: SeasonStorm): string {
  return localStormName(entry.name, entry.basin) ?? t("tcUnnamed");
}

// --- the map ------------------------------------------------------------------

function basemapStyle(): StyleSpecification {
  const flavorName = isDark ? "dark" : "white";
  return {
    version: 8,
    glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
    sprite: `https://protomaps.github.io/basemaps-assets/sprites/v4/${flavorName}`,
    sources: {
      protomaps: {
        type: "vector",
        tiles: [`https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=${PROTOMAPS_KEY}`],
        maxzoom: 15,
        attribution: "Protomaps © OpenStreetMap contributors",
      },
    },
    layers: basemapLayers("protomaps", namedFlavor(flavorName), { lang: basemapLang }).filter(
      (layer) => layer.id !== "landcover" && layer.id !== "buildings",
    ),
  };
}

let map: MaplibreMap | null = null;
let storms: StormLayers | null = null;
let card: Popup | null = null;

function ensureMap(): MaplibreMap {
  if (map) return map;
  const created = new MaplibreMap({
    container: "typhoon-map",
    center: [130, 20],
    zoom: 2,
    attributionControl: { compact: true },
    style: basemapStyle(),
  });
  created.addControl(new NavigationControl({ showCompass: false }), "top-right");
  // A style swap (theme, basemap language) drops every layer added to the
  // map; the storm layers are made again on the style it loads. The page
  // has no WebGL layers of its own, which is what the viewer's never-
  // setStyle rule protects.
  created.on("style.load", () => {
    storms = new StormLayers(created);
    const before = (created.getStyle().layers ?? []).find((layer) => layer.type === "symbol")?.id;
    storms.ensure(before);
    storms.setInk(isDark);
    draw();
  });
  created.on("click", (event) => {
    card?.remove();
    card = null;
    const layers = TC_CLICKABLE_LAYERS.filter((id) => created.getLayer(id) !== undefined);
    if (!layers.length) return;
    const box: [[number, number], [number, number]] = [
      [event.point.x - 6, event.point.y - 6],
      [event.point.x + 6, event.point.y + 6],
    ];
    for (const feature of created.queryRenderedFeatures(box, { layers })) {
      const data = pointDataOf(feature);
      if (!data) continue;
      card = new Popup({ closeButton: false, closeOnClick: false, className: "tc-popup", maxWidth: "300px", offset: 12 })
        .setLngLat([data.lon, data.lat])
        .setDOMContent(buildTcCard(data, { formatTime: formatUtcHour }))
        .addTo(created);
      return;
    }
  });
  map = created;
  return created;
}

function draw(): void {
  if (!storms || !loaded) return;
  const issue = loaded.issues[issueIndex];
  if (issue === undefined) {
    storms.clear();
    return;
  }
  const storm = stormAt(loaded.entry.id, loaded.entry.name, loaded.records, loaded.best, issue);
  // Between two times that draw the same forecasts only the time moves:
  // the tracks are not sent to the map's worker again.
  const signature = [
    loaded.entry.id,
    ...[...hidden].sort(),
    ...Object.entries({ ...storm.agencies, ...storm.models }).map(([key, forecast]) => `${key}@${forecast.base}`).sort(),
  ].join("|");
  if (signature === drawnSignature && storms === drawnLayers) {
    storms.setTime(Date.parse(issue));
    return;
  }
  drawnSignature = signature;
  drawnLayers = storms;
  const on = (keys: Iterable<string>) => new Set([...keys].filter((key) => !hidden.has(key)));
  storms.update(
    [
      {
        storm,
        agencies: on(Object.keys(storm.agencies)),
        models: on(Object.keys(storm.models)),
        members: !hidden.has("members"),
        best: !hidden.has("best"),
        bestRadii: !hidden.has("radii"),
        bestIntensity: true,
      },
    ],
    Date.parse(issue),
  );
}

/** What the map was last sent, so a step that changes only the time
 * moves only the time; the layers are new after a style swap. */
let drawnSignature = "";
let drawnLayers: StormLayers | null = null;

// --- the controls ----------------------------------------------------------

function chip(label: string, pressed: boolean, onPress: () => void, title?: string): HTMLButtonElement {
  const button = element("button", "", label);
  button.type = "button";
  button.setAttribute("aria-pressed", String(pressed));
  if (title) button.title = title;
  button.addEventListener("click", onPress);
  return button;
}

function renderSources(): void {
  if (!loaded) return;
  const toggle = (key: string) => () => {
    if (hidden.has(key)) hidden.delete(key);
    else hidden.add(key);
    renderSources();
    renderLegend();
    draw();
  };
  const chips = [...loaded.sources.agencies, ...loaded.sources.models].map((key) =>
    chip(agencyCode(key), !hidden.has(key), toggle(key), TC_AGENCIES[key]?.name ?? TC_MODELS[key]?.name),
  );
  const tracks = Object.values(loaded.best?.best ?? {});
  if (tracks.some((track) => track.points.some((point) => point.radii)))
    chips.unshift(chip(t("tcPageRadii"), !hidden.has("radii"), toggle("radii")));
  if (tracks.length > 0) chips.unshift(chip(t("tcBest"), !hidden.has("best"), toggle("best")));
  if (loaded.sources.models.some((key) => TC_MODELS[key]?.ensemble)) chips.push(chip(t("tcMembers"), !hidden.has("members"), toggle("members")));
  sourcesGroup.replaceChildren(...chips);
}

/** What the colors on the map mean: the best track's intensity classes
 * and, while they are drawn, the radii thresholds. */
function renderLegend(): void {
  if (!loaded) return;
  const tracks = Object.values(loaded.best?.best ?? {});
  const rows: HTMLElement[] = [];
  const row = (label: string, items: { code: string; color: string; ring?: boolean }[]) => {
    const line = element("div", "typhoon-legend-row");
    line.append(element("span", "typhoon-legend-label", label));
    for (const item of items) {
      const key = element("span", "typhoon-legend-item");
      const swatch = element("span", item.ring ? "typhoon-swatch is-ring" : "typhoon-swatch");
      swatch.style.setProperty("--swatch", item.color);
      key.append(swatch, item.code);
      line.append(key);
    }
    rows.push(line);
  };
  if (!hidden.has("best") && tracks.some((track) => track.points.some((point) => point.vmax !== null)))
    row(t("tcLegendIntensity"), SSHS_CLASSES.map((item) => ({ code: item.code, color: item.color })));
  if (!hidden.has("best") && !hidden.has("radii") && tracks.some((track) => track.points.some((point) => point.radii)))
    row(
      t("tcPageRadii"),
      (["34", "50", "64"] as const).map((threshold) => ({ code: `${threshold}KT`, color: RADII_COLORS[threshold], ring: true })),
    );
  legend.replaceChildren(...rows);
  legend.hidden = rows.length === 0;
}

function renderIssue(): void {
  if (!loaded) return;
  const issue = loaded.issues[issueIndex];
  issueLabel.textContent = issue ? formatUtcHour(issue) : "--";
  // A time on the best track nobody forecast from (the storm left the
  // centre's area, or was not yet named): only the track is drawn.
  issueLabel.title = issue && !forecastsAt(loaded.records, issue) ? t("tcPageNoForecast") : "";
  issueLabel.classList.toggle("is-quiet", Boolean(issue) && !forecastsAt(loaded.records, issue!));
  slider.value = String(issueIndex);
  (document.getElementById("typhoon-prev") as HTMLButtonElement).disabled = issueIndex <= 0;
  (document.getElementById("typhoon-next") as HTMLButtonElement).disabled = issueIndex >= loaded.issues.length - 1;
}

function setIssue(index: number): void {
  if (!loaded || loaded.issues.length === 0) return;
  issueIndex = Math.max(0, Math.min(loaded.issues.length - 1, index));
  scheduleWrite();
  renderIssue();
  renderErrors();
  draw();
}

/** The address follows the time, but once the time has settled: browsers
 * throttle `history.replaceState` (Safari throws past a hundred calls in
 * thirty seconds), and a drag or playback would hit that in seconds. */
let writeTimer: number | null = null;

function scheduleWrite(): void {
  if (writeTimer !== null) window.clearTimeout(writeTimer);
  writeTimer = window.setTimeout(() => {
    writeTimer = null;
    if (!loaded) return;
    try {
      writeState(loaded.entry.id, loaded.issues[issueIndex] ?? null);
    } catch {
      // A refused write leaves the address one step behind, nothing more.
    }
  }, 400);
}

/** A drag fires `input` faster than the map draws; the latest position is
 * applied once per frame. */
let pendingSlide: number | null = null;

slider.addEventListener("input", () => {
  setPlaying(false);
  const first = pendingSlide === null;
  pendingSlide = Number(slider.value);
  if (!first) return;
  requestAnimationFrame(() => {
    const index = pendingSlide;
    pendingSlide = null;
    if (index !== null && index !== issueIndex) setIssue(index);
  });
});

// --- playback and the MP4 --------------------------------------------------

/** How long each time holds, in playback and in the MP4: four steps a
 * second, so a storm's fortnight of six-hourly times plays in about fifteen
 * seconds. */
const HOLD_MS = 250;
const playButton = document.getElementById("typhoon-play") as HTMLButtonElement;
const mp4Button = document.getElementById("typhoon-mp4") as HTMLButtonElement;
const exportStatus = document.getElementById("typhoon-export-status") as HTMLSpanElement;
let playTimer: number | null = null;
let exporting: AbortController | null = null;

function setPlaying(on: boolean): void {
  if (playTimer !== null) window.clearInterval(playTimer);
  playTimer = null;
  if (on && loaded && loaded.issues.length > 1) {
    // From the end, a press plays the storm again from its start.
    if (issueIndex >= loaded.issues.length - 1) setIssue(0);
    playTimer = window.setInterval(() => {
      if (!loaded || issueIndex >= loaded.issues.length - 1) setPlaying(false);
      else setIssue(issueIndex + 1);
    }, HOLD_MS);
  }
  const playing = playTimer !== null;
  playButton.setAttribute("aria-pressed", String(playing));
  playButton.setAttribute("aria-label", t(playing ? "pauseAnimation" : "playAnimation"));
}

playButton.addEventListener("click", () => setPlaying(playTimer === null));

/** The frames of the file: every time, or 240 spread evenly over them. */
function exportFrames(count: number): number[] {
  if (count <= MP4_MAX_FRAMES) return Array.from({ length: count }, (_, index) => index);
  return Array.from({ length: MP4_MAX_FRAMES }, (_, index) => Math.round((index * (count - 1)) / (MP4_MAX_FRAMES - 1)));
}

async function exportStormMp4(): Promise<void> {
  if (!loaded || !map) return;
  if (exporting) {
    exporting.abort();
    return;
  }
  setPlaying(false);
  const story = loaded;
  const created = map;
  const back = issueIndex;
  const controller = new AbortController();
  exporting = controller;
  mp4Button.textContent = t("exportCancel");
  const host: GifHost = {
    map: created,
    show(index) {
      setIssue(index);
      return true;
    },
    caption(index) {
      const time = story.issues[index] ?? "";
      const storm = stormAt(story.entry.id, story.entry.name, story.records, story.best, time);
      const sources = [...Object.keys(storm.agencies), ...Object.keys(storm.models)].filter((key) => !hidden.has(key));
      return {
        title: stormName(story.entry),
        code: [story.entry.id, ...sources.map((key) => agencyCode(key))].join(" · "),
        stamp: formatUtcHour(time),
      };
    },
    // Google's tracks carry its citation wherever they are distributed.
    credit: () =>
      story.sources.models.includes("gdm") && !hidden.has("gdm")
        ? "IBTRACS · NHC · ECMWF OPEN DATA CC BY 4.0 · GDM: © 2023-25 GOOGLE LLC CC BY 4.0, RESEARCH ONLY · © OSM"
        : "IBTRACS · NHC · JTWC · UCAR · ECMWF OPEN DATA CC BY 4.0 · © OSM",
    holdMs: () => HOLD_MS,
  };
  try {
    const blob = await exportMp4(
      host,
      exportFrames(story.issues.length),
      (progress) => {
        exportStatus.textContent =
          progress.phase === "capture"
            ? t("exportCapturing", { done: progress.done, total: progress.total })
            : t("exportEncoding", { percent: Math.round((100 * progress.done) / Math.max(1, progress.total)) });
      },
      controller.signal,
    );
    exportStatus.textContent = "";
    await deliverMp4(blob, mp4FileName(`${story.entry.id}-${story.entry.name ?? ""}`, Date.parse(story.issues[0] ?? "")));
  } catch (error) {
    exportStatus.textContent = error instanceof DOMException && error.name === "AbortError" ? "" : t("exportFailed");
    if (exportStatus.textContent) console.warn("tc mp4 export failed", error);
  } finally {
    exporting = null;
    mp4Button.textContent = "MP4";
    if (loaded === story) setIssue(back);
  }
}

mp4Button.addEventListener("click", () => void exportStormMp4());
void isMp4ExportSupported().then((supported) => {
  mp4Button.hidden = !supported;
});
document.getElementById("typhoon-prev")?.addEventListener("click", () => {
  setPlaying(false);
  setIssue(issueIndex - 1);
});
document.getElementById("typhoon-next")?.addEventListener("click", () => {
  setPlaying(false);
  setIssue(issueIndex + 1);
});
// The arrow keys step the issue time from anywhere on the page but a
// field, which has its own use for them (the slider steps itself).
window.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
  if (event.key === "ArrowLeft") setIssue(issueIndex - 1);
  if (event.key === "ArrowRight") setIssue(issueIndex + 1);
});

function renderTruths(): void {
  if (!loaded) return;
  const truths = truthsOf(loaded.errors);
  truthsGroup.hidden = truths.length < 2;
  truthsGroup.replaceChildren(
    ...truths.map((key) =>
      chip(key.toUpperCase(), key === truth, () => {
        truth = key;
        renderTruths();
        renderErrors();
      }),
    ),
  );
}

function renderErrors(): void {
  if (!loaded) return;
  const issue = loaded.issues[issueIndex];
  const mean = trackErrors(loaded.errors, truth, LEADS);
  const one = issue === undefined ? new Map() : trackErrors(loaded.errors, truth, LEADS, issue);
  const sources = SOURCE_ORDER.filter((key) => mean.has(key)).concat([...mean.keys()].filter((key) => !SOURCE_ORDER.includes(key)));
  if (sources.length === 0) {
    const row = element("tr", "");
    const cell = element("td", "typhoon-empty", t("tcPageErrorsNone"));
    row.append(cell);
    table.replaceChildren(row);
    return;
  }
  const head = element("thead", "");
  const headRow = element("tr", "");
  headRow.append(element("th", "", t("tcPageSource")));
  for (const lead of LEADS) headRow.append(element("th", "", `${lead}h`));
  head.append(headRow);
  const group = (label: string, cells: Map<string, Map<number, { km: number; n: number }>>, withCount: boolean) => {
    const section = element("tbody", "");
    const caption = element("tr", "typhoon-group");
    const captionCell = element("th", "", label);
    captionCell.colSpan = LEADS.length + 1;
    caption.append(captionCell);
    section.append(caption);
    for (const source of sources) {
      const row = element("tr", "");
      const name = element("th", "", agencyCode(source));
      name.scope = "row";
      const ink = TC_AGENCIES[source]?.color;
      if (ink) name.style.setProperty("--source-ink", ink);
      row.append(name);
      for (const lead of LEADS) {
        const value = cells.get(source)?.get(lead);
        const cell = element("td", "", value ? String(Math.round(value.km)) : "—");
        if (value && withCount) cell.append(element("span", "typhoon-n", String(value.n)));
        row.append(cell);
      }
      section.append(row);
    }
    return section;
  };
  table.replaceChildren(head, group(t("tcPageErrorsMean"), mean, true), group(t("tcPageErrorsThis"), one, false));
}

function renderFacts(): void {
  if (!loaded) return;
  const { entry } = loaded;
  titleNode.textContent = stormName(entry);
  // The name as the centres issue it stays beside a local one, so the
  // storm can be found in their bulletins.
  const issued = entry.name && stormName(entry) !== entry.name ? `${entry.name} · ` : "";
  codeNode.textContent = `${issued}${stormCode(entry.id)} · ${entry.id.slice(-4)}`;
  const definition = (label: string, value: string) => {
    const item = element("div", "");
    item.append(element("dt", "", label), element("dd", "", value));
    return item;
  };
  const items = [definition(t("tcPageId"), entry.id)];
  if (entry.first && entry.last) items.push(definition(t("tcPageLife"), `${formatUtcHour(entry.first)} → ${formatUtcHour(entry.last)}`));
  if (entry.vmax !== null) items.push(definition(t("tcPagePeak"), `${Math.round(entry.vmax / KNOT)} kt`));
  items.push(definition(t("tcPageForecasts"), String(loaded.records.length)));
  facts.replaceChildren(...items);
  // A storm with a best track has a static page of its own.
  stormLink.hidden = loaded.best === null;
  stormLink.href = stormPagePath(locale, seasonOfStorm(entry.id), entry);
  stormLink.textContent = `${t("tcPageDetails")} →`;
  applyPageMeta({
    path: `/typhoon.html?storm=${entry.id}`,
    title: `${stormName(entry)} (${entry.id})`,
    description: t("tcPageMetaDescription"),
  });
}

function renderAll(): void {
  renderFacts();
  renderSources();
  renderLegend();
  renderTruths();
  renderIssue();
  renderErrors();
}

/** Where a link without an issue time opens: the first forecast that
 * verified, since a storm's earliest ones often start before it was a
 * tropical cyclone and have nothing in the table. */
function firstVerifiedIssue(issues: readonly string[], errors: StormErrors | null, against: string): number {
  const verified = new Set<number>();
  for (const columns of Object.values(errors?.truths[against] ?? {})) for (const base of columns.base) verified.add(Date.parse(base));
  const found = issues.findIndex((issue) => verified.has(Date.parse(issue)));
  return found >= 0 ? found : 0;
}

// --- the picker ---------------------------------------------------------------

const archive = new TcArchive(dataBaseUrl());
const BASIN_ORDER = ["WP", "EP", "AL", "CP", "IO", "SH"];
let seasons: number[] = [];
let season: number | null = null;
let basin: string | null = null;
let seasonIndex: SeasonIndex | null = null;

/** The season's storms with forecasts, in the order they formed (by id
 * where an index predates the summary). */
function seasonStorms(): SeasonStorm[] {
  return (seasonIndex?.storms ?? [])
    .filter((storm) => storm.months.length > 0)
    .sort((a, b) => (a.first ?? "9").localeCompare(b.first ?? "9") || a.id.localeCompare(b.id));
}

/** `25–26`: the July-to-June southern season a `SH..2026` storm is
 * numbered in. */
function southernSeason(id: string): string {
  const year = seasonOfStorm(id);
  return `${String(year - 1).slice(-2)}–${String(year).slice(-2)}`;
}

/** `07-27 → 08-10`, the year left to the season chip. */
function stormSpan(storm: SeasonStorm): string {
  if (!storm.first || !storm.last) return "";
  const day = (iso: string) => iso.slice(5, 10);
  return `${day(storm.first)} → ${storm.first.slice(0, 4) === storm.last.slice(0, 4) ? day(storm.last) : storm.last.slice(0, 10)}`;
}

function renderPicker(): void {
  seasonsGroup.replaceChildren(
    ...seasons.map((year) =>
      chip(String(year), year === season, () => {
        if (year === season) return;
        season = year;
        basin = null;
        void loadSeason(null);
      }),
    ),
  );
  const storms = seasonStorms();
  const basins = BASIN_ORDER.filter((code) => storms.some((storm) => storm.basin === code));
  basinsGroup.replaceChildren(
    chip(t("tcHistoryAll"), basin === null, () => {
      basin = null;
      renderPicker();
    }),
    ...basins.map((code) =>
      chip(`${code} ${storms.filter((storm) => storm.basin === code).length}`, code === basin, () => {
        basin = code;
        renderPicker();
      }),
    ),
  );
  // The southern hemisphere numbers its storms by a July-to-June season
  // named after the year it ends in (ATCF, JTWC): say so wherever such a
  // season is on screen, or "2027" reads as a storm from the future.
  const southern = season !== null && storms.some((storm) => storm.basin === "SH");
  seasonNote.hidden = !southern;
  if (southern && season !== null)
    seasonNote.textContent = t("tcHistorySouthernNote", { season, from: `${season - 1}-07`, to: `${season}-06` });
  seasonLink.hidden = season === null || storms.length === 0;
  if (season !== null) {
    seasonLink.href = seasonPagePath(locale, season);
    seasonLink.textContent = `${t("tcPageSeasonList")} →`;
  }
  const selected = loaded?.entry.id ?? pending;
  stormList.replaceChildren(
    ...storms
      .filter((storm) => basin === null || storm.basin === basin)
      .map((storm) => {
        const item = element("li", "");
        const button = element("button", "typhoon-row");
        button.type = "button";
        button.setAttribute("aria-current", String(storm.id === selected));
        const name = stormName(storm);
        button.setAttribute("aria-label", t("tcHistoryStormAria", { name: `${name} (${storm.id})` }));
        button.append(
          element("span", "typhoon-row-code", storm.basin === "SH" ? `${stormCode(storm.id)} ${southernSeason(storm.id)}` : stormCode(storm.id)),
          element("span", "typhoon-row-name", name),
          element("span", "typhoon-row-span", stormSpan(storm)),
          element("span", "typhoon-row-peak", storm.vmax === null ? "" : `${Math.round(storm.vmax / KNOT)} kt`),
        );
        button.addEventListener("click", () => void selectStorm(storm, null));
        item.append(button);
        return item;
      }),
  );
  if (storms.length === 0 && seasonIndex) {
    stormList.replaceChildren(element("li", "typhoon-empty", t("tcHistoryEmpty")));
  }
  stormList.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
}

// --- loading -----------------------------------------------------------------

function fail(message: string): void {
  // The message is this load's, not the markup's: a language switch must
  // not put "Loading" back over it.
  status.removeAttribute("data-i18n");
  status.hidden = false;
  status.classList.add("is-error");
  status.textContent = message;
}

/** The storm being loaded, so a second pick made before the first has
 * arrived wins, and the list marks it at once. */
let pending: string | null = null;

async function selectStorm(entry: SeasonStorm, issue: string | null): Promise<void> {
  if (!seasonIndex || loaded?.entry.id === entry.id || exporting) return;
  setPlaying(false);
  pending = entry.id;
  renderPicker();
  status.hidden = false;
  status.classList.remove("is-error");
  status.textContent = t("tcPageLoading");
  const index = seasonIndex;
  const [best, records, errors] = await Promise.all([
    archive.best(index, entry),
    archive.forecasts(entry),
    archive.errors(index.season, entry.id).catch(() => null),
  ]);
  if (pending !== entry.id) return;
  pending = null;
  const issues = storyTimes(records, best);
  loaded = { entry, best, records, errors, issues, sources: sourcesOf(records, SOURCE_ORDER) };
  card?.remove();
  if (truthsOf(errors).length > 0 && !truthsOf(errors).includes(truth)) truth = truthsOf(errors)[0]!;
  const wanted = issue === null ? -1 : issues.findIndex((item) => issueKey(item) === issue);
  issueIndex = wanted >= 0 ? wanted : firstVerifiedIssue(issues, errors, truth);
  slider.max = String(Math.max(0, issues.length - 1));
  writeState(entry.id, issues[issueIndex] ?? null);
  status.hidden = true;
  stormSection.hidden = false;
  renderAll();
  renderPicker();
  const created = ensureMap();
  const bounds = archiveBounds(best, records);
  // The track and its widest winds, with a narrow margin: the radii are
  // in the bounds already, so the padding is only room to breathe.
  if (bounds) created.fitBounds(bounds, { padding: 24, maxZoom: 6, duration: 600 });
  draw();
}

/** Show the season's storms; open `wanted` when given and listed, else the
 * latest storm when nothing is open yet. */
async function loadSeason(wanted: { id: string; issue: string | null } | null): Promise<void> {
  if (season === null) return;
  const year = season;
  seasonIndex = null;
  renderPicker();
  const index = await archive.season(year);
  if (year !== season) return;
  seasonIndex = index;
  renderPicker();
  const storms = seasonStorms();
  if (wanted) {
    const entry = index?.storms.find((storm) => storm.id === wanted.id || storm.atcf.includes(wanted.id));
    if (!entry) {
      fail(t("tcPageMissing", { id: wanted.id }));
      return;
    }
    await selectStorm(entry, wanted.issue);
  } else if (!loaded && storms.length > 0) {
    await selectStorm(storms[storms.length - 1]!, null);
  }
}

async function load(): Promise<void> {
  const state = parseState(window.location.search);
  applyPageMeta({ path: state.storm ? `/typhoon.html?storm=${state.storm}` : "/typhoon.html" });
  ensureMap();
  const catalog = await archive.catalog();
  seasons = catalog.seasons.map((entry) => entry.season).sort((a, b) => b - a);
  if (seasons.length === 0) {
    fail(t("tcHistoryEmpty"));
    return;
  }
  // The season running now, not the southern one already named after next
  // year — unless the link names a storm of another.
  const year = new Date().getUTCFullYear();
  season = state.storm ? seasonOfStorm(state.storm) : (seasons.find((item) => item <= year) ?? seasons[0]!);
  if (!seasons.includes(season)) {
    fail(t("tcPageMissing", { id: state.storm ?? "" }));
    season = seasons.find((item) => item <= year) ?? seasons[0]!;
    await loadSeason(null);
    return;
  }
  await loadSeason(state.storm ? { id: state.storm, issue: state.issue } : null);
}

onThemeChange(() => {
  map?.setStyle(basemapStyle());
});

onLocaleChange(() => {
  map?.setStyle(basemapStyle());
  if (loaded) renderAll();
  renderPicker();
});

load().catch((error: unknown) => fail(t("tcPageFailed", { message: error instanceof Error ? error.message : String(error) })));
