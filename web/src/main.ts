import "@fontsource/barlow-condensed/500.css";
import "@fontsource/barlow-condensed/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "maplibre-gl/dist/maplibre-gl.css";
import "./style.css";

import { layers as basemapLayers, namedFlavor } from "@protomaps/basemaps";
import maplibregl from "maplibre-gl";

import { CRC32_INITIAL, crc32Hex, crc32Update } from "./crc32";
import { applyStaticMessages, basemapLang, t, toggleLocale } from "./i18n";
import { ForecastLayer } from "./layer";
import {
  FORECAST_BUNDLE_IDS,
  FORECAST_MODEL_IDS,
  FORECAST_MODELS,
  fetchLatestPointer,
  fetchManifest,
  hasBundle,
  parseBundleMetadata,
  pickBundleVariant,
  WIND_COMPONENT_IDS,
  type BundleMetadata,
  type BundleVariable,
  type ForecastBundleId,
  type ForecastManifest,
  type ForecastModelId,
  type VideoBundleDescriptor,
} from "./manifest";
import { buildPalette } from "./palettes";
import { parseModelFromSearch, parseVariableFromSearch, searchForVariable } from "./urlstate";
import { WindParticleLayer } from "./particles";
import { fetchPoster, isPosterSupported } from "./poster";
import {
  createVideoDecodeChannel,
  isWebCodecsSupported,
  type DecodeChannel,
  type VideoFrameIndexEntry,
  type VideoStreamSource,
} from "./webcodecs";

// Rewrite the static shell into the detected locale before anything renders.
applyStaticMessages();

const FRAME_INTERVAL_MS = 1000 / 12;
const FRAME_COUNT = 121;
/** How often the latest.json live pointer is re-checked for a new run. */
const LATEST_POLL_MS = 5 * 60_000;

/** True on connections where downloads should be frugal (Save-Data or 2G). */
function constrainedConnection(): boolean {
  const connection = (navigator as { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
  if (!connection) return false;
  return connection.saveData === true || /(^|-)2g$/.test(connection.effectiveType ?? "");
}

/** Connections where the smallest resolution tier should win outright —
 * one notch wider than constrainedConnection(), since 3G can render the app
 * fine but should not pay for full-resolution bundles. */
function slowConnection(): boolean {
  const connection = (navigator as { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
  if (!connection) return false;
  return connection.saveData === true || /(^|-)[23]g$/.test(connection.effectiveType ?? "");
}

/** Horizontal grid samples the current view can actually display — the
 * world's CSS pixel width at the current zoom times devicePixelRatio (capped:
 * beyond 2x extra grid columns are invisible). A global 0.25 degree grid is
 * 1440 columns over 360 degrees, so this is directly comparable to variant
 * widths. */
function neededGridWidth(): number {
  const worldCssWidth = 512 * 2 ** map.getZoom();
  return worldCssWidth * Math.min(2, window.devicePixelRatio || 1);
}

// The plane cache is byte-budgeted (not frame-count-limited) and the
// background prefetch is windowed — a handful of frames ahead of the
// playhead, a few fetches at a time, narrower on constrained connections.
function planeCacheBudgetBytes(): number {
  return (constrainedConnection() ? 24 : 64) * 1024 * 1024;
}
function prefetchWindowFrames(): number {
  return constrainedConnection() ? 4 : 10;
}
function prefetchConcurrency(): number {
  return constrainedConnection() ? 1 : 3;
}

/** Per-variable UI copy; the `code` is prefixed with the active model's label
 * ("GFS / TMP 2M", "ECMWF / TMP 2M"). */
const VARIABLE_UI = {
  tmp2m: {
    code: "TMP 2M",
    title: ["Surface", "Temperature"],
    bufferTitle: "Temperature buffer",
    label: t("varLabelTmp2m"),
    legend: ["50", "30", "10", "-10", "-30", "-60"],
  },
  prate: {
    code: "PRATE SFC",
    title: ["Precipitation", "Rate"],
    bufferTitle: "Precipitation buffer",
    label: t("varLabelPrate"),
    legend: ["128", "40", "20", "5", "1", "0"],
  },
  dswrf: {
    code: "DSWRF SFC",
    title: ["Solar", "Radiation"],
    bufferTitle: "Radiation buffer",
    label: t("varLabelDswrf"),
    legend: ["1200", "900", "600", "300", "100", "0"],
  },
  wind10m: {
    code: "WIND 10M",
    title: ["Surface", "Wind"],
    bufferTitle: "Wind buffer",
    label: t("varLabelWind10m"),
    legend: ["40", "30", "20", "10", "5", "0"],
  },
} as const satisfies Record<ForecastBundleId, unknown>;

/** Basemap tones per variable. tmp2m paints an opaque field so its base is
 * nearly invisible; prate and wind composite semi-transparent data over the
 * base, so those get a lighter ocean and a visible landmass to keep the page
 * from reading as a black void. */
const BASEMAP_THEME = {
  tmp2m: { ocean: "#0b1826", land: "#182c3d" },
  prate: { ocean: "#16344a", land: "#28495f" },
  dswrf: { ocean: "#0d1b2b", land: "#1c3242" },
  wind10m: { ocean: "#0e2131", land: "#1d3849" },
} as const satisfies Record<ForecastBundleId, { ocean: string; land: string }>;

function currentBasemapTheme(): { ocean: string; land: string } {
  const id = document.body.dataset.variable as ForecastBundleId | undefined;
  return BASEMAP_THEME[id ?? "tmp2m"] ?? BASEMAP_THEME.tmp2m;
}

function applyBasemapTheme(): void {
  const theme = currentBasemapTheme();
  if (map.getLayer("background")) map.setPaintProperty("background", "background-color", theme.ocean);
  if (map.getLayer("water")) map.setPaintProperty("water", "fill-color", theme.ocean);
  if (map.getLayer("earth")) map.setPaintProperty("earth", "fill-color", theme.land);
}

/** Protomaps hosted basemap (real coastlines, waterways, boundaries and
 * labels). The forecast layers insert themselves before this layer, keeping
 * boundaries and place labels legible above the data. */
const FORECAST_ANCHOR_LAYER = "boundaries_country";
const PROTOMAPS_KEY = "249bb192fefe0a77";

function buildBasemapStyle(): maplibregl.StyleSpecification {
  const theme = currentBasemapTheme();
  const flavor = { ...namedFlavor("dark"), background: theme.ocean, water: theme.ocean, earth: theme.land };
  return {
    version: 8,
    glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
    sprite: "https://protomaps.github.io/basemaps-assets/sprites/v4/dark",
    sources: {
      // Inline tile URLs (no TileJSON fetch) so the shell still boots — dark
      // ocean, no basemap — when api.protomaps.com is unreachable.
      protomaps: {
        type: "vector",
        tiles: [`https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=${PROTOMAPS_KEY}`],
        maxzoom: 15,
        attribution: "Protomaps © OpenStreetMap contributors",
      },
    },
    // The flavor's landcover layer repaints the whole landmass in its own
    // near-black tones, defeating the per-variable earth color — drop it so
    // land stays a flat themed slate under the data.
    // Basemap labels follow the UI locale.
    layers: basemapLayers("protomaps", flavor, { lang: basemapLang }).filter((layer) => layer.id !== "landcover"),
  };
}

const map = new maplibregl.Map({
  container: "map",
  center: [128, 28],
  zoom: 1.65,
  minZoom: 0,
  maxZoom: 7,
  attributionControl: false,
  style: buildBasemapStyle(),
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

const slider = required<HTMLInputElement>("frame-slider");
const runTime = required<HTMLElement>("run-time");
const validTime = required<HTMLElement>("valid-time");
const forecastHour = required<HTMLOutputElement>("forecast-hour");
const loadStatus = required<HTMLElement>("load-status");
const tickMarks = required<HTMLElement>("tick-marks");
const errorPanel = required<HTMLElement>("error-panel");
const errorMessage = required<HTMLElement>("error-message");
const retryButton = required<HTMLButtonElement>("retry-button");
const playButton = required<HTMLButtonElement>("play-button");
const playLabel = required<HTMLElement>("play-label");
const dataCard = required<HTMLElement>("data-card");
const dataCardIndex = required<HTMLElement>("data-card-index");
const dataCardTitle = required<HTMLElement>("data-card-title");
const preloadFrames = required<HTMLOutputElement>("preload-frames");
const preloadBytes = required<HTMLOutputElement>("preload-bytes");
const preloadFormat = required<HTMLOutputElement>("preload-format");
const preloadPercent = required<HTMLOutputElement>("preload-percent");
const preloadSegments = required<HTMLElement>("preload-segments");
const preloadState = required<HTMLElement>("preload-state");
const statsClose = required<HTMLButtonElement>("stats-close");
const contextMenu = required<HTMLElement>("context-menu");
const statsMenuToggle = required<HTMLButtonElement>("stats-menu-toggle");
const copyDebugButton = required<HTMLButtonElement>("copy-debug");
const statDataset = required<HTMLElement>("stat-dataset");
const statGrid = required<HTMLElement>("stat-grid");
const statCacheBytes = required<HTMLElement>("stat-cache-bytes");
const statDecode = required<HTMLElement>("stat-decode");
const statDecodeRate = required<HTMLElement>("stat-decode-rate");
const statGraph = required<HTMLCanvasElement>("stat-graph");
const statViewport = required<HTMLElement>("stat-viewport");
const statConnection = required<HTMLElement>("stat-connection");
const variableCode = required<HTMLElement>("variable-code");
const variableTitle = required<HTMLElement>("variable-title");
const legend = required<HTMLElement>("legend");
const legendUnit = required<HTMLElement>("legend-unit");
const legendLabels = required<HTMLElement>("legend-labels");
const trackHorizon = required<HTMLElement>("track-horizon");
const frameTooltip = required<HTMLOutputElement>("frame-tooltip");
const forecastDays = required<HTMLElement>("forecast-days");
const timelinePanel = required<HTMLElement>("timeline-panel");
// Scoped to buttons: <body> carries data-variable/data-model too (styling
// state), and must never be hidden or aria-pressed like a switch button.
const variableButtons = [...document.querySelectorAll<HTMLButtonElement>("button[data-variable]")];
const modelButtons = [...document.querySelectorAll<HTMLButtonElement>("button[data-model]")];
const modelEyebrow = required<HTMLElement>("model-eyebrow");

const MODEL_EYEBROW: Record<ForecastModelId, string> = {
  gfs: "NOAA / GFS (0.25°)",
  ecmwf: "ECMWF / IFS (0.25°)",
  sflux: "NOAA / GFS SFLUX (13 KM)",
};

// On phone-sized viewports the station panel starts collapsed — expanded it
// would cover most of the remaining map between the top strip and timeline.
const stationPanel = document.querySelector<HTMLDetailsElement>("details.station-panel");
if (stationPanel && window.matchMedia("(max-width: 720px)").matches) stationPanel.open = false;

// The timeline starts collapsed (play + slider only) so the map keeps the
// bottom of the viewport; the chevron reveals the five-day strip and the
// choice sticks across visits.
const timelineToggle = required<HTMLButtonElement>("timeline-toggle");
const TIMELINE_EXPANDED_KEY = "g2pv-timeline-expanded";

function setTimelineExpanded(expanded: boolean): void {
  document.body.classList.toggle("timeline-collapsed", !expanded);
  timelineToggle.setAttribute("aria-expanded", String(expanded));
  timelineToggle.setAttribute("aria-label", expanded ? t("collapseTimeline") : t("expandTimeline"));
}

let storedTimelineExpanded: string | null = null;
try {
  storedTimelineExpanded = localStorage.getItem(TIMELINE_EXPANDED_KEY);
} catch {
  // Storage can be unavailable (privacy modes); fall back to collapsed.
}
setTimelineExpanded(storedTimelineExpanded === "1");

timelineToggle.addEventListener("click", () => {
  const expanded = document.body.classList.contains("timeline-collapsed");
  setTimelineExpanded(expanded);
  try {
    localStorage.setItem(TIMELINE_EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    // Preference just won't persist.
  }
});

interface DecodedFrame {
  plane: Uint8Array;
  decodeMs: number;
}

/** One resident per-variable bundle: its decode channel and embedded metadata.
 * `worker` is either a real Worker (Xue/WASM path) or a WebCodecs
 * `DecodeChannel` (video path, tmp2m only, when the browser supports it) —
 * both speak the same booted/init/ready/decode/frame/error protocol. */
interface VariableSession {
  /** Bundle-level id this session was loaded for (wind10m carries two data
   * variables). */
  id: ForecastBundleId;
  worker: DecodeChannel;
  metadata: BundleMetadata;
  /** Primary data variable (drives palette/unit for scalars; the u component
   * for wind). */
  variable: BundleVariable;
  /** Every data variable a frame of this session needs decoded — one for
   * scalars, the u and v pair for wind. */
  variables: BundleVariable[];
  /** Delivery format actually in use for this variable ("Xue ½" is the
   * half-resolution variant tier). */
  format: "H.264" | "Xue" | "Xue ½";
  /** Network bytes downloaded for this variable's artifacts only. */
  bytes: number;
  /** Total bytes of this variable's artifacts (stream + index). */
  totalBytes: number;
  /** Bytes outside the channel's own reporting (e.g. the video index),
   * added on top of streaming `progress` messages. */
  extraBytes: number;
  /** On-demand range delivery; `bytes` grows as groups arrive. */
  streaming: boolean;
  /** Every artifact byte is local (immediately true for full downloads). */
  resident: boolean;
}

/** Streaming progress that arrived before its session finished registering. */
const pendingStream = new Map<string, { bytes: number; resident: boolean }>();

let manifest: ForecastManifest | null = null;
/** Absolute URL the manifest was loaded from; artifact paths resolve against it. */
let manifestUrl: string | null = null;
/** Run id from the latest.json live pointer, e.g. "2026081600". */
let currentRun: string | null = null;
let metadata: BundleMetadata | null = null;
let layer: ForecastLayer | null = null;
let layerAdded = false;
/** Wind GPU particle layer; created alongside the scalar layer and toggled by
 * the active variable. */
let windLayer: WindParticleLayer | null = null;
let windLayerAdded = false;
let windLayerGridSource: BundleMetadata | null = null;
/** Metadata object whose grid the layer is currently configured for. Sessions
 * can legitimately differ in grid (resolution tiers), so this tracks
 * the exact metadata identity rather than a poster/full flag. */
let layerGridSource: BundleMetadata | null = null;
/** Bundle whose REAL (bundle-decoded) frame is on screen, if any. */
let displayedReal: ForecastBundleId | null = null;
/** Shareable URL entry (e.g. /?model=ecmwf&type=wind) picks the initial model
 * and layer; a missing or unrecognized param falls back to the default. */
let selectedModelId: ForecastModelId = parseModelFromSearch(window.location.search);
let selectedVariableId: ForecastBundleId = parseVariableFromSearch(window.location.search) ?? "prate";
let activeSession: VariableSession | null = null;
let activeVariable: BundleVariable | null = null;
let activeFrameIndex: number | null = null;
let initializeSequence = 0;
let generation = 0;
let playing = false;
let playbackFrame: number | null = null;
let nextFrameAt = 0;
let ready = false;
let switchingVariable = false;

const sessions = new Map<ForecastBundleId, VariableSession>();
const sessionsByNumericId = new Map<number, VariableSession>();
const sessionLoads = new Map<ForecastBundleId, Promise<VariableSession>>();

const planeCache = new Map<string, DecodedFrame>();
let planeCacheBytes = 0;
/** Worker-reported decode time of the most recent plane, for the stats panel. */
let lastDecodeMs: number | null = null;
/** Rolling record of decoded planes (bytes + worker decode time), feeding the
 * stats panel's activity graph — the YouTube network-activity analog. The
 * download is one-shot, but decode work streams for as long as playback
 * prefetches planes. */
const DECODE_GRAPH_WINDOW_MS = 30_000;
const DECODE_GRAPH_BUCKET_MS = 500;
const decodeEvents: { at: number; bytes: number; ms: number }[] = [];

function recordDecodeEvent(bytes: number, ms: number): void {
  const at = performance.now();
  decodeEvents.push({ at, bytes, ms });
  const cutoff = at - DECODE_GRAPH_WINDOW_MS - DECODE_GRAPH_BUCKET_MS;
  while (decodeEvents.length > 0 && decodeEvents[0]!.at < cutoff) decodeEvents.shift();
}

/** Decoded bytes per second, averaged over the last two seconds. */
function decodeRateBytesPerSec(): number {
  const cutoff = performance.now() - 2_000;
  let sum = 0;
  for (let index = decodeEvents.length - 1; index >= 0; index -= 1) {
    if (decodeEvents[index]!.at < cutoff) break;
    sum += decodeEvents[index]!.bytes;
  }
  return sum / 2;
}
/** Last prefetch window sent, to skip redundant messages. */
let lastPrefetchWindow = "";
const inflight = new Map<number, string>();
let nextRequestId = 1;
let desiredKey: string | null = null;
let queuedRequest: { variableId: number; hour: number } | null = null;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function required<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing element #${id}`);
  return element as T;
}

function formatDate(value: string | number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  })
    .format(new Date(value))
    .replace("24:", "00:") + " UTC";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCompactDate(value: number): string {
  const date = new Date(value);
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  const hour = String(date.getUTCHours()).padStart(2, "0");
  return `${month}/${day} ${hour}Z`;
}

function formatDay(value: number): string {
  const date = new Date(value);
  return `${String(date.getUTCMonth() + 1).padStart(2, "0")}/${String(date.getUTCDate()).padStart(2, "0")}`;
}

function frameCount(): number {
  return metadata?.time.frameCount ?? FRAME_COUNT;
}

function frameHour(index: number): number {
  const time = metadata?.time;
  return time ? time.firstForecastHour + index * time.stepHours : index;
}

function frameValidTime(index: number): number {
  const base = metadata ? Date.parse(metadata.runTime) : 0;
  return base + frameHour(index) * 3_600_000;
}

function cacheKey(variableId: number, hour: number): string {
  return `${variableId}:${hour}`;
}

function showError(message: string): void {
  stopPlayback();
  document.body.classList.remove("is-data-loading");
  dataCard.setAttribute("aria-busy", "false");
  dataCard.classList.add("is-error");
  preloadState.textContent = t("dataInterrupted");
  errorMessage.textContent = t("errorHint", { message });
  errorPanel.hidden = false;
  loadStatus.textContent = t("loadFailed");
  loadStatus.className = "load-status is-error";
  setVariableButtonsDisabled(false);
}

function hideError(): void {
  errorPanel.hidden = true;
}

function buildPreloadSegments(total: number): void {
  preloadSegments.replaceChildren();
  preloadSegments.style.setProperty("--frame-count", String(total));
  for (let index = 0; index < total; index += 1) {
    preloadSegments.append(document.createElement("i"));
  }
  preloadSegments.setAttribute("aria-valuemax", String(total));
}

function resetPreloadCard(total: number): void {
  dataCard.classList.remove("is-complete", "is-error");
  dataCardIndex.textContent = `${total}F`;
  buildPreloadSegments(total);
  updateDownloadProgress(0, 1);
  preloadFrames.value = `0 / ${total}`;
  preloadFormat.value = "--";
  preloadState.textContent = t("awaitingManifest");
}

function updateDownloadProgress(bytes: number, total: number): void {
  const fraction = total === 0 ? 0 : Math.min(1, bytes / total);
  const segments = preloadSegments.children.length;
  const filled = Math.floor(fraction * segments);
  preloadBytes.value = formatBytes(bytes);
  preloadPercent.value = `${Math.round(fraction * 100)}%`;
  preloadSegments.setAttribute("aria-valuenow", String(filled));
  [...preloadSegments.children].forEach((segment, index) => {
    segment.classList.toggle("is-loaded", index < filled);
  });
}

function cachedFrameCount(): number {
  if (!activeVariable) return 0;
  let cached = 0;
  for (const key of planeCache.keys()) {
    if (key.startsWith(`${activeVariable.numericId}:`)) cached += 1;
  }
  return cached;
}

function updateCacheReadout(): void {
  if (!activeVariable) return;
  preloadFrames.value = `${cachedFrameCount()} / ${frameCount()}`;
  updateStatsReadout();
}

// The data card is a YouTube-style "stats for nerds" panel: hidden by
// default, pinned by the map's right-click menu (body.stats-visible shows the
// extended rows below), and closable from the card itself.
const STATS_VISIBLE_KEY = "g2pv-stats-visible";

function statsVisible(): boolean {
  return document.body.classList.contains("stats-visible");
}

function setStatsVisible(visible: boolean): void {
  document.body.classList.toggle("stats-visible", visible);
  statsMenuToggle.setAttribute("aria-checked", String(visible));
  if (visible) {
    updateStatsReadout();
    startStatsGraph();
  }
  try {
    localStorage.setItem(STATS_VISIBLE_KEY, visible ? "1" : "0");
  } catch {
    // Preference just won't persist.
  }
}

function connectionLabel(): string {
  const connection = (navigator as { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
  if (!connection?.effectiveType) return "--";
  return connection.saveData ? `${connection.effectiveType} · ${t("saveData")}` : connection.effectiveType;
}

function updateStatsReadout(): void {
  if (!statsVisible()) return;
  statDataset.textContent = currentRun ? `${selectedModelId}.${currentRun}` : "--";
  const grid = activeSession?.metadata.grid ?? null;
  statGrid.textContent = grid ? `${grid.width} × ${grid.height}` : "--";
  statCacheBytes.textContent = `${formatBytes(planeCacheBytes)} / ${formatBytes(planeCacheBudgetBytes())}`;
  statDecode.textContent = lastDecodeMs === null ? "--" : `${lastDecodeMs.toFixed(1)} ms`;
  statDecodeRate.textContent = `${formatBytes(decodeRateBytesPerSec())}/s`;
  const needed = Math.round(neededGridWidth());
  statViewport.textContent = grid ? `${needed} / ${grid.width} col` : `${needed} col`;
  statConnection.textContent = connectionLabel();
}

/** Scrolling decode-activity graph (30 s window): per-bucket decoded bytes as
 * accent bars, per-plane decode time as a line, both normalized to the
 * window's own peak. Runs on requestAnimationFrame only while the panel is
 * pinned; prefers-reduced-motion drops it to one redraw per second. */
function drawDecodeGraph(): void {
  const context = statGraph.getContext("2d");
  if (!context) return;
  const width = statGraph.clientWidth;
  const height = statGraph.clientHeight;
  if (width === 0 || height === 0) return;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (statGraph.width !== Math.round(width * dpr) || statGraph.height !== Math.round(height * dpr)) {
    statGraph.width = Math.round(width * dpr);
    statGraph.height = Math.round(height * dpr);
  }
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);

  const now = performance.now();
  const buckets = Math.ceil(DECODE_GRAPH_WINDOW_MS / DECODE_GRAPH_BUCKET_MS);
  const bytesPerBucket = new Array<number>(buckets).fill(0);
  let peakMs = 0;
  for (const event of decodeEvents) {
    const age = now - event.at;
    if (age < 0 || age >= DECODE_GRAPH_WINDOW_MS) continue;
    bytesPerBucket[buckets - 1 - Math.floor(age / DECODE_GRAPH_BUCKET_MS)]! += event.bytes;
    peakMs = Math.max(peakMs, event.ms);
  }
  const peakBytes = Math.max(...bytesPerBucket);

  const styles = getComputedStyle(document.body);
  const accent = styles.getPropertyValue("--accent").trim() || "#54d6c7";
  const mutedColor = styles.getPropertyValue("--muted").trim() || "#8ca6b2";
  const textColor = styles.getPropertyValue("--text").trim() || "#eaf4f6";

  const barWidth = width / buckets;
  if (peakBytes > 0) {
    context.globalAlpha = 0.6;
    context.fillStyle = accent;
    for (const [index, bytes] of bytesPerBucket.entries()) {
      if (bytes === 0) continue;
      const barHeight = Math.max(1, (bytes / peakBytes) * (height - 12));
      context.fillRect(index * barWidth, height - barHeight, Math.max(1, barWidth - 0.5), barHeight);
    }
    context.globalAlpha = 1;
  }

  if (peakMs > 0) {
    context.strokeStyle = textColor;
    context.globalAlpha = 0.85;
    context.lineWidth = 1;
    context.beginPath();
    let started = false;
    for (const event of decodeEvents) {
      const age = now - event.at;
      if (age < 0 || age >= DECODE_GRAPH_WINDOW_MS) continue;
      const x = (1 - age / DECODE_GRAPH_WINDOW_MS) * width;
      const y = 10 + (1 - event.ms / peakMs) * (height - 12);
      if (started) context.lineTo(x, y);
      else context.moveTo(x, y);
      started = true;
    }
    context.stroke();
    context.globalAlpha = 1;
  }

  context.fillStyle = mutedColor;
  context.font = "8px 'IBM Plex Mono', monospace";
  context.textBaseline = "top";
  context.textAlign = "left";
  context.fillText(`${formatBytes(peakBytes * (1000 / DECODE_GRAPH_BUCKET_MS))}/s`, 3, 3);
  context.textAlign = "right";
  context.fillText(peakMs > 0 ? `${peakMs.toFixed(1)} ms` : "idle", width - 3, 3);
}

let statsGraphFrame: number | null = null;
let lastGraphDrawAt = 0;

function statsGraphLoop(timestamp: number): void {
  statsGraphFrame = null;
  if (!statsVisible()) return;
  if (timestamp - lastGraphDrawAt >= (reducedMotion.matches ? 1_000 : 0)) {
    lastGraphDrawAt = timestamp;
    drawDecodeGraph();
    // The rate row rides the same clock as the graph it summarizes.
    statDecodeRate.textContent = `${formatBytes(decodeRateBytesPerSec())}/s`;
  }
  statsGraphFrame = window.requestAnimationFrame(statsGraphLoop);
}

function startStatsGraph(): void {
  if (statsGraphFrame === null && statsVisible()) {
    statsGraphFrame = window.requestAnimationFrame(statsGraphLoop);
  }
}

/** YouTube「复制调试信息」: one plain-text snapshot of everything the stats
 * panel knows, for bug reports. */
function debugInfoText(): string {
  const session = activeSession;
  const time = metadata?.time;
  const lines = [
    `xue-debug ${new Date().toISOString()}`,
    `dataset: ${currentRun ? `${selectedModelId}.${currentRun}` : "--"}`,
    `variable: ${session ? `${session.id} (${session.variable.unit})` : "--"}`,
    `format: ${session?.format ?? "--"}`,
    `grid: ${session ? `${session.metadata.grid.width} × ${session.metadata.grid.height}` : "--"}`,
    `time: ${time ? `${time.frameCount}F · first ${time.firstForecastHour}h · step ${time.stepHours}h` : "--"}`,
    `frame: ${activeFrameIndex === null ? "--" : `F${String(frameHour(activeFrameIndex)).padStart(3, "0")}`}`,
    `planes: ${cachedFrameCount()} / ${frameCount()} · ${formatBytes(planeCacheBytes)} / ${formatBytes(planeCacheBudgetBytes())}`,
    `network: ${session ? `${formatBytes(session.bytes)} / ${formatBytes(session.totalBytes)}${session.resident ? " · resident" : session.streaming ? " · streaming" : ""}` : "--"}`,
    `decode: ${lastDecodeMs === null ? "--" : `${lastDecodeMs.toFixed(1)} ms`} · ${formatBytes(decodeRateBytesPerSec())}/s`,
    `viewport: ${Math.round(neededGridWidth())} col · zoom ${map.getZoom().toFixed(2)} · dpr ${window.devicePixelRatio || 1}`,
    `connection: ${connectionLabel()}`,
    `ua: ${navigator.userAgent}`,
  ];
  return lines.join("\n");
}

async function copyDebugInfo(): Promise<void> {
  const previous = loadStatus.textContent;
  let notice = t("debugCopied");
  try {
    await navigator.clipboard.writeText(debugInfoText());
  } catch {
    notice = t("copyFailed");
  }
  loadStatus.textContent = notice;
  window.setTimeout(() => {
    if (loadStatus.textContent === notice) loadStatus.textContent = previous;
  }, 1600);
}

function hideContextMenu(): void {
  contextMenu.hidden = true;
}

function showContextMenu(x: number, y: number): void {
  contextMenu.hidden = false;
  const rect = contextMenu.getBoundingClientRect();
  contextMenu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
  contextMenu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
}

function markBundleResident(): void {
  document.body.classList.remove("is-data-loading");
  dataCard.setAttribute("aria-busy", "false");
  dataCard.classList.add("is-complete");
  preloadState.textContent = t("bundleResident");
}

/** Sync the data card with one session's delivery state. */
function refreshDataCard(session: VariableSession): void {
  updateDownloadProgress(session.bytes, session.totalBytes);
  if (session.resident) {
    markBundleResident();
    return;
  }
  document.body.classList.remove("is-data-loading");
  dataCard.setAttribute("aria-busy", "false");
  dataCard.classList.remove("is-complete");
  preloadState.textContent = t("streamingOnDemand");
}

/** Handles `progress`/`resident` messages from streaming decode channels. */
function handleStreamMessage(message: {
  type: string;
  variableKey?: unknown;
  bytes?: unknown;
  totalBytes?: unknown;
}): void {
  const id = message.variableKey;
  if (typeof id !== "string" || !FORECAST_BUNDLE_IDS.includes(id as ForecastBundleId)) return;
  const bundleId = id as ForecastBundleId;
  const session = sessions.get(bundleId);
  if (!session) {
    const entry = pendingStream.get(bundleId) ?? { bytes: 0, resident: false };
    if (message.type === "progress" && typeof message.bytes === "number") entry.bytes = message.bytes;
    if (message.type === "resident") entry.resident = true;
    pendingStream.set(bundleId, entry);
    return;
  }
  if (message.type === "progress" && typeof message.bytes === "number") {
    session.bytes = Math.min(session.totalBytes, session.extraBytes + message.bytes);
  }
  if (message.type === "resident") session.resident = true;
  if (activeSession?.id === bundleId) refreshDataCard(session);
}

function updateTransport(): void {
  playButton.classList.toggle("is-playing", playing);
  const transportLabel = playing ? t("pauseAnimation") : t("playAnimation");
  playButton.setAttribute("aria-label", transportLabel);
  playButton.title = transportLabel;
  playLabel.textContent = playing ? "PAUSE" : "PLAY";
}

function stopPlayback(): void {
  playing = false;
  if (playbackFrame !== null) {
    window.cancelAnimationFrame(playbackFrame);
    playbackFrame = null;
  }
  updateTransport();
}

function advancePlayback(timestamp: number): void {
  playbackFrame = null;
  if (!playing || !activeVariable) return;
  if (timestamp >= nextFrameAt) {
    // Until a frame of this session is on screen there is nothing to advance
    // from: hold on the pending frame instead of walking the slider forward.
    // Advancing here spun the playhead at rAF rate ahead of the decoder, and
    // once the plane cache was eviction-bound (13 resident frames of a
    // 120-frame loop on sflux) the sweep could never land on a still-cached
    // plane — playback never showed a single frame while decodes churned.
    const next = activeFrameIndex === null
      ? Number(slider.value)
      : (activeFrameIndex + 1) % frameCount();
    const shown = trySelectFrame(next);
    if (shown) {
      // Under one interval late is normal rAF quantization: keep the fixed
      // cadence so playback averages exactly 12 fps. Later than that was a
      // decode/network stall — re-base so the following frame waits a full
      // interval instead of landing at a leftover fraction of one.
      nextFrameAt = timestamp - nextFrameAt < FRAME_INTERVAL_MS
        ? nextFrameAt + FRAME_INTERVAL_MS
        : timestamp + FRAME_INTERVAL_MS;
    }
    // When the frame is still decoding, hold the current image; the decode
    // completion will pull playback forward without touching opacity.
  } else {
    blendTowardNext(timestamp);
  }
  playbackFrame = window.requestAnimationFrame(advancePlayback);
}

/** Between 12 fps frame steps, sweep the shader blend weight toward the
 * next frame's plane so playback reads as continuous motion. Skipped across
 * the loop seam (last -> first frame is a restart, not a transition) and
 * whenever either plane is not decoded yet — the current image just holds. */
function blendTowardNext(timestamp: number): void {
  if (!activeSession || !activeVariable || !layer || activeFrameIndex === null) return;
  if (displayedReal !== activeSession.id) return;
  // Wind frames are not blended — the particles themselves provide the
  // continuous motion between field updates.
  if (activeSession.id === "wind10m") return;
  const next = activeFrameIndex + 1;
  if (next >= frameCount()) return;
  const current = planeCache.get(cacheKey(activeVariable.numericId, frameHour(activeFrameIndex)));
  const upcoming = planeCache.get(cacheKey(activeVariable.numericId, frameHour(next)));
  if (!current || !upcoming) return;
  const weight = 1 - (nextFrameAt - timestamp) / FRAME_INTERVAL_MS;
  ensureSessionGrid();
  layer.setBlend(current.plane, upcoming.plane, weight);
}

function startPlayback(): void {
  if (!activeVariable || slider.disabled || playing || !ready) return;
  hideError();
  playing = true;
  nextFrameAt = performance.now() + FRAME_INTERVAL_MS;
  updateTransport();
  playbackFrame = window.requestAnimationFrame(advancePlayback);
}

function updateFrameReadout(index: number): void {
  const hour = frameHour(index);
  const valid = frameValidTime(index);
  slider.value = String(index);
  slider.setAttribute("aria-valuetext", `F${String(hour).padStart(3, "0")}, ${formatDate(valid)}`);
  forecastHour.value = `F${String(hour).padStart(3, "0")}`;
  validTime.textContent = formatDate(valid);
  frameTooltip.value = `F${String(hour).padStart(3, "0")} · ${formatCompactDate(valid)}`;
  frameTooltip.style.setProperty("--frame-progress", `${(index / Math.max(1, frameCount() - 1)) * 100}%`);
  updateTicks(index);
  updateForecastDay(index);
}

/** Reconfigure the layer for the active session's own bundle grid (poster
 * grid, full grid, and variant grids all differ) before showing a real
 * plane of that session. */
function ensureSessionGrid(): void {
  if (!layer || !activeSession) return;
  if (layerGridSource === activeSession.metadata) return;
  layer.configureGrid(activeSession.metadata);
  layerGridSource = activeSession.metadata;
}

/** Same for the wind particle layer: adopt the wind session's grid and its
 * u/v quantization before feeding it planes. */
function ensureWindGrid(session: VariableSession): void {
  if (!windLayer || windLayerGridSource === session.metadata) return;
  windLayer.configureGrid(session.metadata);
  windLayerGridSource = session.metadata;
}

/** Tell the active session which frames to keep resident (windowed
 * prefetch): the window ahead of the playhead, wrapped at the loop point. */
function sendPrefetchWindow(index: number): void {
  const session = activeSession;
  if (!session || !session.streaming || session.resident) return;
  const total = frameCount();
  const hours: number[] = [];
  for (let step = 0; step <= prefetchWindowFrames(); step += 1) {
    hours.push(frameHour((index + step) % total));
  }
  const key = `${session.id}:${hours[0]}`;
  if (key === lastPrefetchWindow) return;
  lastPrefetchWindow = key;
  session.worker.postMessage({ type: "prefetch-window", hours, concurrency: prefetchConcurrency() });
}

/** Show the frame if every needed plane is decoded (one for scalars, the u/v
 * pair for wind); otherwise request the missing decodes. */
function trySelectFrame(index: number): boolean {
  const session = activeSession;
  if (!session || !layer) return false;
  const hour = frameHour(index);
  updateFrameReadout(index);
  sendPrefetchWindow(index);
  const keys = session.variables.map((variable) => cacheKey(variable.numericId, hour));
  const planes = keys.map((key) => planeCache.get(key));
  if (planes.every((plane) => plane !== undefined)) {
    // Refresh LRU positions, then swap textures — no opacity involved.
    for (const [position, key] of keys.entries()) {
      planeCache.delete(key);
      planeCache.set(key, planes[position]!);
    }
    if (session.id === "wind10m") {
      ensureWindGrid(session);
      windLayer?.setWindPlanes(planes[0]!.plane, planes[1]!.plane);
    } else {
      ensureSessionGrid();
      layer.setFrame(planes[0]!.plane);
    }
    displayedReal = session.id;
    activeFrameIndex = index;
    desiredKey = null;
    prefetchNext(index);
    return true;
  }
  // Track the first missing plane; when it arrives the frame is retried,
  // which walks desiredKey to the next still-missing plane (if any).
  desiredKey = keys.find((_, position) => planes[position] === undefined) ?? null;
  for (const [position] of keys.entries()) {
    if (planes[position] !== undefined) continue;
    requestDecode(session.variables[position]!.numericId, hour);
  }
  return false;
}

/** Frames decoded ahead of the playhead during playback. One is not enough:
 * a single slow decode (byte-budget eviction forces perpetual re-decodes on
 * long loops) then stalls the very next frame step. A small pipeline absorbs
 * that jitter; the inflight cap in requestDecode still bounds worker load. */
const PLAYBACK_DECODE_AHEAD = 3;

function prefetchNext(index: number): void {
  if (!activeSession || !playing) return;
  for (let step = 1; step <= PLAYBACK_DECODE_AHEAD; step += 1) {
    const hour = frameHour((index + step) % frameCount());
    for (const variable of activeSession.variables) {
      const key = cacheKey(variable.numericId, hour);
      if (!planeCache.has(key)) requestDecode(variable.numericId, hour);
    }
  }
}

function requestDecode(variableId: number, hour: number): void {
  const session = sessionsByNumericId.get(variableId);
  if (!session || !ready) return;
  const key = cacheKey(variableId, hour);
  if ([...inflight.values()].includes(key)) return;
  // The wind session needs two planes per frame, so scale the inflight cap to
  // the active session's channel count.
  if (inflight.size >= 2 * (activeSession?.variables.length ?? 1)) {
    // Keep only the newest queued request while scrubbing.
    queuedRequest = { variableId, hour };
    return;
  }
  const requestId = nextRequestId++;
  inflight.set(requestId, key);
  session.worker.postMessage({
    type: "decode",
    requestId,
    generation,
    variableId,
    forecastHour: hour,
  });
}

function handleDecodedFrame(message: {
  /** Absent on cache-warm-up frames the video path decodes alongside a target. */
  requestId?: number;
  variableId: number;
  forecastHour: number;
  decodeMs: number;
  buffer: ArrayBuffer;
}): void {
  if (typeof message.requestId === "number") inflight.delete(message.requestId);
  const key = cacheKey(message.variableId, message.forecastHour);
  const previous = planeCache.get(key);
  if (previous) planeCacheBytes -= previous.plane.byteLength;
  const plane = new Uint8Array(message.buffer);
  planeCache.set(key, { plane, decodeMs: message.decodeMs });
  planeCacheBytes += plane.byteLength;
  lastDecodeMs = message.decodeMs;
  recordDecodeEvent(plane.byteLength, message.decodeMs);
  // Evict by byte budget, oldest first; never evict the just-inserted
  // frame or the ones on screen (the blend path may still sample them; wind
  // keeps a u/v pair displayed).
  const displayedKeys = new Set<string>();
  if (activeSession && activeFrameIndex !== null) {
    for (const variable of activeSession.variables) {
      displayedKeys.add(cacheKey(variable.numericId, frameHour(activeFrameIndex)));
    }
  }
  while (planeCacheBytes > planeCacheBudgetBytes() && planeCache.size > 2) {
    const oldest = planeCache.keys().next().value;
    if (oldest === undefined || oldest === key) break;
    const evicted = planeCache.get(oldest)!;
    planeCache.delete(oldest);
    if (displayedKeys.has(oldest)) {
      // Refresh instead of evicting: move to the newest LRU position.
      planeCache.set(oldest, evicted);
      continue;
    }
    planeCacheBytes -= evicted.plane.byteLength;
    const owner = sessionsByNumericId.get(Number(oldest.split(":")[0]));
    if (owner) {
      const buffer = evicted.plane.buffer as ArrayBuffer;
      owner.worker.postMessage({ type: "recycle", buffer }, [buffer]);
    }
  }
  updateCacheReadout();

  if (queuedRequest) {
    const queued = queuedRequest;
    queuedRequest = null;
    const queuedKey = cacheKey(queued.variableId, queued.hour);
    if (queuedKey !== key && !planeCache.has(queuedKey)) {
      requestDecode(queued.variableId, queued.hour);
    }
  }
  // Display only the newest requested target; stale decodes stay cached.
  if (desiredKey === key && activeVariable && layer) {
    const index = (message.forecastHour - (metadata?.time.firstForecastHour ?? 0)) /
      (metadata?.time.stepHours ?? 1);
    const shown = trySelectFrame(index);
    // A stalled playhead resumes here, off the decode completion, so restart
    // the cadence too: without this the next rAF tick sees a long-expired
    // deadline and steps again immediately — the fast half of the visible
    // fast/slow playback jitter.
    if (shown && playing) nextFrameAt = performance.now() + FRAME_INTERVAL_MS;
  }
}

function updateTicks(selected: number): void {
  [...tickMarks.children].forEach((element, index) => element.classList.toggle("is-active", index <= selected));
}

function buildTicks(total: number): void {
  tickMarks.replaceChildren();
  for (let index = 0; index < total; index += 1) {
    const tick = document.createElement("i");
    // Major tick on every day boundary, whatever the model's frame step.
    tick.className = frameHour(index) % 24 === 0 ? "major" : "";
    tickMarks.append(tick);
  }
}

/** Frame index of one forecast day boundary (24, 48, ... hours out), or null
 * when the model's step does not land a frame exactly on it. */
function dayFrameIndex(day: number): number | null {
  const time = metadata?.time ?? { firstForecastHour: 0, stepHours: 1, frameCount: FRAME_COUNT };
  const index = (day * 24 - time.firstForecastHour) / time.stepHours;
  return Number.isInteger(index) && index < time.frameCount ? index : null;
}

function buildForecastDays(): void {
  forecastDays.replaceChildren();
  for (let day = 1; day <= 5; day += 1) {
    const index = dayFrameIndex(day);
    if (index === null) continue;
    const segment = document.createElement("span");
    segment.className = "forecast-day";
    const label = document.createElement("b");
    label.textContent = `D+${String(day).padStart(2, "0")}`;
    const date = document.createElement("time");
    const valid = frameValidTime(index);
    date.dateTime = new Date(valid).toISOString();
    date.textContent = formatDay(valid);
    const hour = document.createElement("small");
    hour.textContent = `+${day * 24}H`;
    segment.append(label, date, hour);
    forecastDays.append(segment);
  }
  updateForecastDay(0);
}

function updateForecastDay(frameIndex: number): void {
  const selectedDay = Math.min(4, Math.max(0, Math.ceil(frameHour(frameIndex) / 24) - 1));
  [...forecastDays.children].forEach((segment, index) => {
    const active = index === selectedDay;
    segment.classList.toggle("is-active", active);
    if (active) segment.setAttribute("aria-current", "step");
    else segment.removeAttribute("aria-current");
  });
}

function setVariableButtonsDisabled(disabled: boolean): void {
  for (const button of variableButtons) button.disabled = disabled;
  for (const button of modelButtons) button.disabled = disabled;
}

function updateModelPresentation(): void {
  document.body.dataset.model = selectedModelId;
  modelEyebrow.textContent = MODEL_EYEBROW[selectedModelId];
  for (const button of modelButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.model === selectedModelId));
  }
}

function updateVariablePresentation(session: VariableSession): void {
  const ui = VARIABLE_UI[session.id];
  const model = FORECAST_MODELS[selectedModelId];
  document.body.dataset.variable = session.id;
  applyBasemapTheme();
  updateModelPresentation();
  variableCode.textContent = `${model.label} / ${ui.code}`;
  dataCardTitle.textContent = ui.bufferTitle;
  document.title = `${ui.title.join(" ")} · ${model.label} 120H`;
  variableTitle.replaceChildren();
  ui.title.forEach((line, index) => {
    if (index > 0) variableTitle.append(document.createElement("br"));
    variableTitle.append(document.createTextNode(line));
  });
  legend.setAttribute("aria-label", t("legendAria", { label: ui.label }));
  legendUnit.textContent = session.variable.unit;
  legendLabels.replaceChildren(...ui.legend.map((label) => {
    const span = document.createElement("span");
    span.textContent = label;
    return span;
  }));
  for (const button of variableButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.variable === session.id));
  }
}

/** Keep the address bar shareable: reflect the on-screen model and variable
 * into the query string (replaceState — switches are not history entries). */
function syncUrl(variableId: ForecastBundleId): void {
  const search = searchForVariable(variableId, window.location.search, selectedModelId);
  if (search === window.location.search) return;
  window.history.replaceState(null, "", `${window.location.pathname}${search}${window.location.hash}`);
}

function dataBaseUrl(): string {
  return import.meta.env.VITE_DATA_BASE_URL || "data/";
}

/** Absolute artifact URL, resolved against the run manifest's own URL
 * (manifest paths are manifest-relative, HLS style). Artifact paths are stable
 * per run, but their content can be re-encoded (e.g. a codebook change);
 * keying the URL on the artifact CRC keeps a returning visitor's HTTP cache
 * from serving bytes the integrity checks reject. */
function artifactUrl(path: string, crc32: string): string {
  if (!manifestUrl) throw new Error("manifest not loaded");
  const url = new URL(path, manifestUrl);
  url.searchParams.set("v", crc32);
  return url.href;
}

/** True when the server honors single byte ranges with exact 206 responses,
 * which is what the on-demand streaming paths require. A single-range GET is
 * a CORS-safelisted request, so this needs no preflight. */
async function supportsRangeRequests(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { headers: { Range: "bytes=0-0" } });
    const supported = response.status === 206;
    await response.body?.cancel();
    return supported;
  } catch {
    return false;
  }
}

/** Downloads and CRC32-verifies one manifest-declared artifact. Used for both
 * .xue bundles and the tmp2m video stream — both carry {path, byteLength, crc32}. */
async function downloadBundle(
  descriptor: { path: string; byteLength: number; crc32: string },
  sequence: number,
): Promise<ArrayBuffer> {
  preloadState.textContent = t("receivingBundle");
  const response = await fetch(artifactUrl(descriptor.path, descriptor.crc32));
  if (!response.ok) throw new Error(t("bundleRequestFailed", { status: response.status }));
  const total = descriptor.byteLength;
  const data = new Uint8Array(total);
  let offset = 0;
  let crc = CRC32_INITIAL;

  if (response.body) {
    const reader = response.body.getReader();
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
      const chunk = result.value;
      if (offset + chunk.byteLength > total) throw new Error(t("bundleTooLong"));
      data.set(chunk, offset);
      offset += chunk.byteLength;
      crc = crc32Update(crc, chunk);
      updateDownloadProgress(offset, total);
      loadStatus.textContent = t("receivingBundlePercent", { percent: Math.round((offset / total) * 100) });
    }
  } else {
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.byteLength > total) throw new Error(t("bundleTooLong"));
    data.set(buffer, 0);
    offset = buffer.byteLength;
    crc = crc32Update(crc, buffer);
    updateDownloadProgress(offset, total);
  }

  if (offset !== total) throw new Error(t("bundleLengthMismatch"));
  if (crc32Hex(crc) !== descriptor.crc32) throw new Error(t("bundleChecksumMismatch"));
  return data.buffer;
}

/** Fetches the small per-frame index for a video artifact. Not CRC-checked
 * like the stream itself — it's a few KB of JSON and fetch/JSON.parse already
 * fail loudly on a truncated or corrupt response. */
async function downloadVideoIndex(
  descriptor: VideoBundleDescriptor,
): Promise<{ frames: VideoFrameIndexEntry[]; byteLength: number }> {
  if (!manifestUrl) throw new Error("manifest not loaded");
  const url = new URL(descriptor.indexPath, manifestUrl);
  const response = await fetch(url.href, { cache: "no-cache" });
  if (!response.ok) throw new Error(t("videoIndexRequestFailed", { status: response.status }));
  const buffer = await response.arrayBuffer();
  const payload = JSON.parse(new TextDecoder().decode(buffer)) as { frames: VideoFrameIndexEntry[] };
  return { frames: payload.frames, byteLength: buffer.byteLength };
}

function spawnWorker(): Worker {
  const decodeWorker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
  decodeWorker.onerror = (event) => {
    showError(event.message || t("workerStartFailed"));
  };
  return decodeWorker;
}

function initializeChannel(
  channel: DecodeChannel,
  initMessage: unknown,
  transfer: Transferable[],
  sequence: number,
): Promise<{ worker: DecodeChannel; metadata: BundleMetadata }> {
  return new Promise((resolve, reject) => {
    channel.onmessage = (event: MessageEvent) => {
      const message = event.data as Record<string, unknown>;
      if (sequence !== initializeSequence) return;
      switch (message.type) {
        case "booted":
          channel.postMessage(initMessage, transfer);
          break;
        case "progress":
        case "resident":
          handleStreamMessage(message as Parameters<typeof handleStreamMessage>[0]);
          break;
        case "ready": {
          try {
            const parsed = parseBundleMetadata(message.metadataJson as string);
            resolve({ worker: channel, metadata: parsed });
          } catch (error) {
            reject(error instanceof Error ? error : new Error(String(error)));
          }
          break;
        }
        case "frame":
          handleDecodedFrame(message as unknown as Parameters<typeof handleDecodedFrame>[0]);
          break;
        case "error": {
          const text = String(message.message ?? t("decodeFailed"));
          if (typeof message.requestId === "number") {
            inflight.delete(message.requestId);
            showError(t("frameDecodeFailed", { message: text }));
          } else {
            reject(new Error(text));
          }
          break;
        }
      }
    };
  });
}

/** Download and initialize one variable's bundle; resident sessions are reused. */
function loadVariable(variableId: ForecastBundleId, sequence: number): Promise<VariableSession> {
  const resident = sessions.get(variableId);
  if (resident) return Promise.resolve(resident);
  const pending = sessionLoads.get(variableId);
  if (pending) return pending;
  const load = (async () => {
    if (!manifest) throw new Error("manifest not loaded");
    const descriptor = manifest.bundles.find((bundle) => bundle.variable === variableId);
    if (!descriptor) throw new Error(t("manifestMissingBundle", { id: variableId }));

    let channel: DecodeChannel;
    let initMessage: unknown = { type: "init" };
    let transfer: Transferable[] = [];
    let format: VariableSession["format"];
    let downloadedBytes: number;
    let totalBytes: number;
    let extraBytes = 0;
    let streaming = false;
    // Pick a resolution tier before choosing the decode path. A selected
    // variant always rides the Xue path — the video artifacts are full
    // resolution, so whenever a reduced tier suffices the half bundle is
    // strictly cheaper.
    const variant = pickBundleVariant(descriptor.variants, neededGridWidth(), slowConnection());
    const video = descriptor.video;
    // The video path must also earn its bytes — prefer it only when the
    // stream is not larger than the bundle it replaces (lossless H.264 wins
    // that comparison for tmp2m but loses it for prate; the manifest's
    // byteLengths decide, so a future lossy tier flips this automatically).
    if (
      !variant &&
      video &&
      video.byteLength <= descriptor.byteLength &&
      (await isWebCodecsSupported(video.codec, video.width, video.height))
    ) {
      const streamUrl = artifactUrl(video.streamPath, video.crc32);
      streaming = await supportsRangeRequests(streamUrl);
      if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
      const index = await downloadVideoIndex(video);
      if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
      let source: VideoStreamSource;
      if (streaming) {
        source = { kind: "url", url: streamUrl, byteLength: video.byteLength, variableKey: variableId };
        downloadedBytes = index.byteLength;
      } else {
        const streamBuffer = await downloadBundle(
          { path: video.streamPath, byteLength: video.byteLength, crc32: video.crc32 },
          sequence,
        );
        if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
        source = { kind: "buffer", buffer: streamBuffer };
        downloadedBytes = video.byteLength + index.byteLength;
      }
      channel = createVideoDecodeChannel({
        source,
        frames: index.frames,
        codec: video.codec,
        width: video.width,
        height: video.height,
        metadataJson: video.metadataJson,
      });
      format = "H.264";
      totalBytes = video.byteLength + index.byteLength;
      extraBytes = index.byteLength;
    } else {
      const target = variant ?? descriptor;
      const url = artifactUrl(target.path, target.crc32);
      streaming = await supportsRangeRequests(url);
      if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
      if (streaming) {
        channel = spawnWorker();
        initMessage = {
          type: "init-stream",
          url,
          byteLength: target.byteLength,
          variableKey: variableId,
        };
        downloadedBytes = 0;
      } else {
        const initBuffer = await downloadBundle(target, sequence);
        if (sequence !== initializeSequence) throw new DOMException("aborted", "AbortError");
        channel = spawnWorker();
        initMessage = { type: "init", buffer: initBuffer };
        transfer = [initBuffer];
        downloadedBytes = target.byteLength;
      }
      format = variant ? "Xue ½" : "Xue";
      totalBytes = target.byteLength;
    }

    loadStatus.textContent = streaming ? t("readingIndex") : t("initializingDecoder");
    const { worker: sessionWorker, metadata: bundleMetadata } = await initializeChannel(
      channel,
      initMessage,
      transfer,
      sequence,
    );
    if (sequence !== initializeSequence) {
      sessionWorker.terminate();
      throw new DOMException("aborted", "AbortError");
    }
    if (manifest && Date.parse(bundleMetadata.runTime) !== Date.parse(manifest.runTime)) {
      throw new Error(t("bundleRunMismatch"));
    }
    // Grids may differ between sessions (resolution tiers), and time
    // axes may differ per variable since the ECMWF source: its de-accumulated
    // prate has no analysis frame, so that series starts at the first real
    // step. syncTimeline() rebuilds the timeline for whichever session is
    // active; only the run cycle above must agree.
    // The wind bundle carries the u/v pair; every scalar bundle carries
    // exactly its own variable.
    const wantedIds = variableId === "wind10m" ? WIND_COMPONENT_IDS : [variableId];
    const sessionVariables = wantedIds.map((wanted) => {
      const found = bundleMetadata.variables.find((item) => item.id === wanted);
      if (!found) throw new Error(t("bundleMissingVariable", { id: wanted }));
      return found;
    });
    const session: VariableSession = {
      id: variableId,
      worker: sessionWorker,
      metadata: bundleMetadata,
      variable: sessionVariables[0]!,
      variables: sessionVariables,
      format,
      bytes: downloadedBytes,
      totalBytes,
      extraBytes,
      streaming,
      resident: !streaming,
    };
    // Streaming progress may have raced ahead of session registration.
    const early = pendingStream.get(variableId);
    if (early) {
      pendingStream.delete(variableId);
      if (early.bytes > 0) session.bytes = Math.min(totalBytes, extraBytes + early.bytes);
      if (early.resident) session.resident = true;
    }
    sessions.set(variableId, session);
    for (const item of sessionVariables) sessionsByNumericId.set(item.numericId, session);
    return session;
  })();
  sessionLoads.set(variableId, load);
  return load.finally(() => sessionLoads.delete(variableId));
}

/** Create the custom layer (and add it to the map) if it does not exist yet.
 * Grid configuration is the caller's business — poster and bundle planes use
 * different grids. */
function ensureLayer(): ForecastLayer {
  if (!layer) {
    layer = new ForecastLayer((message) => showError(message));
  }
  if (!layerAdded) {
    map.addLayer(layer, FORECAST_ANCHOR_LAYER);
    layerAdded = true;
  }
  return layer;
}

/** Create the wind particle layer on first use, above the scalar plane and
 * below the boundary lines. */
function ensureWindLayer(): WindParticleLayer {
  if (!windLayer) {
    windLayer = new WindParticleLayer((message) => showError(message));
    windLayer.animate = !reducedMotion.matches;
  }
  if (!windLayerAdded) {
    map.addLayer(windLayer, FORECAST_ANCHOR_LAYER);
    windLayerAdded = true;
  }
  return windLayer;
}

/** Fast channel change: paint the variable's tiny first-frame poster
 * (half-resolution f000 plane) while the real stream loads. Best-effort —
 * any failure just means the map stays as it was until real data arrives. */
async function showPoster(variableId: ForecastBundleId, sequence: number): Promise<void> {
  if (!manifest || !isPosterSupported()) return;
  const descriptor = manifest.bundles.find((bundle) => bundle.variable === variableId)?.poster;
  if (!descriptor) return;
  try {
    const plane = await fetchPoster(artifactUrl(descriptor.path, descriptor.crc32), descriptor);
    if (sequence !== initializeSequence || selectedVariableId !== variableId) return;
    // A real frame of this variable beat the poster to the screen.
    if (displayedReal === variableId) return;
    const posterMetadata = parseBundleMetadata(descriptor.metadataJson);
    const variable = posterMetadata.variables.find((item) => item.id === variableId);
    if (!variable) return;
    const target = ensureLayer();
    target.configureGrid(posterMetadata);
    layerGridSource = posterMetadata;
    displayedReal = null;
    target.setPalette(buildPalette(variable));
    target.setFrame(plane);
  } catch (error) {
    console.warn("poster skipped:", error);
  }
}

/** Adopt the session's time axis as the timeline's. Axes can differ per
 * variable (ECMWF prate starts at the first real step, not the analysis
 * frame), so on a change the slider, ticks, and day strip are rebuilt and the
 * playhead is remapped to the nearest frame of the same forecast hour. */
function syncTimeline(session: VariableSession): void {
  const previous = metadata?.time ?? null;
  metadata = session.metadata;
  const time = metadata.time;
  if (
    previous &&
    previous.frameCount === time.frameCount &&
    previous.firstForecastHour === time.firstForecastHour &&
    previous.stepHours === time.stepHours
  ) {
    return;
  }
  const previousIndex = activeFrameIndex ?? Number(slider.value);
  const hour = previous
    ? previous.firstForecastHour + previousIndex * previous.stepHours
    : time.firstForecastHour;
  const index = Math.max(
    0,
    Math.min(time.frameCount - 1, Math.round((hour - time.firstForecastHour) / time.stepHours)),
  );
  slider.max = String(time.frameCount - 1);
  slider.value = String(index);
  activeFrameIndex = null;
  trackHorizon.textContent = `+${frameHour(time.frameCount - 1)}H`;
  buildTicks(time.frameCount);
  buildForecastDays();
  dataCardIndex.textContent = `${time.frameCount}F`;
  buildPreloadSegments(time.frameCount);
}

function applyVariable(session: VariableSession): void {
  if (!layer) return;
  activeSession = session;
  activeVariable = session.variable;
  selectedVariableId = session.id;
  syncTimeline(session);
  updateVariablePresentation(session);
  syncUrl(session.id);
  const wind = session.id === "wind10m";
  if (wind) {
    ensureWindLayer();
    ensureWindGrid(session);
  } else {
    layer.setPalette(buildPalette(session.variable));
  }
  // Exactly one weather layer owns the screen: the scalar raster plane or the
  // wind particles.
  layer.setVisible(!wind);
  windLayer?.setVisible(wind);
  updateCacheReadout();
  // The data card reflects only the variable on screen: its own delivery
  // format, its own downloaded bytes, and its own delivery state — never a
  // cross-variable total.
  preloadFormat.value = session.format;
  refreshDataCard(session);
  const index = activeFrameIndex ?? Number(slider.value);
  trySelectFrame(index);
}

async function activateVariable(variableId: ForecastBundleId): Promise<void> {
  if (!manifest || !layer || switchingVariable || variableId === activeSession?.id) return;
  const sequence = initializeSequence;
  const resident = sessions.get(variableId);
  if (resident) {
    applyVariable(resident);
    // Frame counts can differ per variable (ECMWF prate has no analysis
    // frame), so the readiness line follows the session it now describes.
    loadStatus.textContent = t("framesReady", { count: frameCount() });
    loadStatus.className = "load-status";
    return;
  }
  switchingVariable = true;
  const wasPlaying = playing;
  stopPlayback();
  setVariableButtonsDisabled(true);
  document.body.classList.add("is-data-loading");
  dataCard.setAttribute("aria-busy", "true");
  resetPreloadCard(frameCount());
  loadStatus.textContent = t("readingData");
  loadStatus.className = "load-status is-loading";
  selectedVariableId = variableId;
  void showPoster(variableId, sequence);
  try {
    const session = await loadVariable(variableId, sequence);
    if (sequence !== initializeSequence) return;
    applyVariable(session);
    loadStatus.textContent = t("framesReady", { count: frameCount() });
    loadStatus.className = "load-status";
    if (wasPlaying && !reducedMotion.matches) startPlayback();
  } catch (error) {
    if (sequence !== initializeSequence) return;
    if (error instanceof DOMException && error.name === "AbortError") return;
    showError(error instanceof Error ? error.message : t("bundleLoadFailed"));
  } finally {
    switchingVariable = false;
    if (sequence === initializeSequence && ready) setVariableButtonsDisabled(false);
  }
}

async function initialize(): Promise<void> {
  const sequence = ++initializeSequence;
  stopPlayback();
  ready = false;
  switchingVariable = false;
  desiredKey = null;
  queuedRequest = null;
  inflight.clear();
  planeCache.clear();
  planeCacheBytes = 0;
  lastDecodeMs = null;
  decodeEvents.length = 0;
  lastPrefetchWindow = "";
  pendingStream.clear();
  for (const session of sessions.values()) session.worker.terminate();
  sessions.clear();
  sessionsByNumericId.clear();
  sessionLoads.clear();
  manifest = null;
  manifestUrl = null;
  currentRun = null;
  metadata = null;
  layerGridSource = null;
  windLayerGridSource = null;
  windLayer?.setVisible(false);
  displayedReal = null;
  activeSession = null;
  activeVariable = null;
  activeFrameIndex = null;
  slider.value = "0";
  slider.disabled = true;
  playButton.disabled = true;
  setVariableButtonsDisabled(true);
  document.body.classList.add("is-data-loading");
  dataCard.setAttribute("aria-busy", "true");
  hideError();
  resetPreloadCard(FRAME_COUNT);
  updateModelPresentation();
  loadStatus.textContent = t("readingData");
  loadStatus.className = "load-status is-loading";
  runTime.textContent = t("awaitingData");

  try {
    // The mutable live pointer names the current run; the run manifest
    // and everything below it are immutable and cached via ?v=<crc32>.
    // Each model has its own pointer, so the selected model picks the feed.
    const loaded = await fetchManifest(dataBaseUrl(), selectedModelId);
    if (sequence !== initializeSequence) return;
    manifest = loaded.manifest;
    manifestUrl = loaded.manifestUrl;
    currentRun = loaded.latest.run;
    runTime.textContent = formatDate(loaded.manifest.runTime);

    // Optional bundles are per run: wind10m everywhere, dswrf on the
    // sflux source only — each button appears only when the manifest
    // actually ships its bundle.
    for (const optionalId of ["dswrf", "wind10m"] as const) {
      const available = hasBundle(loaded.manifest, optionalId);
      for (const button of variableButtons) {
        if (button.dataset.variable === optionalId) button.hidden = !available;
      }
      if (!available && selectedVariableId === optionalId) selectedVariableId = "prate";
    }

    // Paint the poster while the bundle opens (never blocks the load).
    void showPoster(selectedVariableId, sequence);

    const session = await loadVariable(selectedVariableId, sequence);
    if (sequence !== initializeSequence) return;

    ensureLayer();
    // If a poster is on screen, keep it: ensureSessionGrid() switches the
    // layer to the session's bundle grid the moment the first real plane is
    // ready (trySelectFrame's cached branch).

    ready = true;
    // applyVariable adopts the session's time axis (syncTimeline) and builds
    // the slider, ticks, and day strip from it.
    applyVariable(session);

    loadStatus.textContent = t("framesReady", { count: frameCount() });
    loadStatus.className = "load-status";
    slider.disabled = false;
    playButton.disabled = false;
    setVariableButtonsDisabled(false);
    if (!reducedMotion.matches) startPlayback();
  } catch (error) {
    if (sequence !== initializeSequence) return;
    if (error instanceof DOMException && error.name === "AbortError") return;
    showError(error instanceof Error ? error.message : t("bundleLoadFailed"));
  }
}

slider.addEventListener("input", () => {
  stopPlayback();
  generation += 1;
  trySelectFrame(Number(slider.value));
});
slider.addEventListener("pointerdown", () => timelinePanel.classList.add("is-scrubbing"));
window.addEventListener("pointerup", () => timelinePanel.classList.remove("is-scrubbing"));
slider.addEventListener("blur", () => timelinePanel.classList.remove("is-scrubbing"));
slider.addEventListener("keydown", (event) => {
  if (!activeVariable || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
  event.preventDefault();
  stopPlayback();
  generation += 1;
  const direction = event.key === "ArrowRight" ? 1 : -1;
  const next = Math.max(0, Math.min(frameCount() - 1, Number(slider.value) + direction));
  trySelectFrame(next);
});
for (const button of variableButtons) {
  button.addEventListener("click", () => {
    const variableId = button.dataset.variable;
    if (variableId && FORECAST_BUNDLE_IDS.includes(variableId as ForecastBundleId)) {
      void activateVariable(variableId as ForecastBundleId);
      // On phones the open panel keeps covering the map; picking a variable is
      // the task it was opened for, so it tucks itself away again.
      if (stationPanel && window.matchMedia("(max-width: 720px)").matches) stationPanel.open = false;
    }
  });
}
for (const button of modelButtons) {
  button.addEventListener("click", () => {
    const modelId = button.dataset.model;
    if (!modelId || !FORECAST_MODEL_IDS.includes(modelId as ForecastModelId)) return;
    if (modelId === selectedModelId || switchingVariable) return;
    // A model is a separate dataset (own pointer, own run, own time axis), so
    // switching tears the whole session state down and re-tunes, keeping the
    // currently selected variable.
    selectedModelId = modelId as ForecastModelId;
    updateModelPresentation();
    syncUrl(selectedVariableId);
    void initialize();
  });
}
retryButton.addEventListener("click", () => void initialize());
// The locale is fixed per page load (the basemap style bakes it in), so the
// toggle persists the choice and reloads onto the other language.
required<HTMLButtonElement>("lang-toggle").addEventListener("click", toggleLocale);
// Right-click (long-press on touch) over the map opens the custom menu:
// 「详细统计信息」 pins the stats card, 「复制调试信息」 copies a plain-text
// snapshot. The map-level event (not a DOM listener) is what makes this
// coexist with right-drag rotate: MapLibre suppresses the native menu for map
// listeners and skips firing after a rotate drag.
map.on("contextmenu", (event) => {
  showContextMenu(event.originalEvent.clientX, event.originalEvent.clientY);
});
window.addEventListener("pointerdown", (event) => {
  if (!contextMenu.hidden && !contextMenu.contains(event.target as Node)) hideContextMenu();
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideContextMenu();
});
window.addEventListener("blur", hideContextMenu);
map.on("movestart", hideContextMenu);
statsMenuToggle.addEventListener("click", () => {
  setStatsVisible(!statsVisible());
  hideContextMenu();
});
copyDebugButton.addEventListener("click", () => {
  hideContextMenu();
  void copyDebugInfo();
});
statsClose.addEventListener("click", () => setStatsVisible(false));
// The viewport-sampling row tracks zoom/resize while the panel is pinned.
map.on("moveend", () => updateStatsReadout());
window.addEventListener("resize", () => updateStatsReadout());
playButton.addEventListener("click", () => {
  if (playing) stopPlayback();
  else startPlayback();
});
/** Poll the live pointer; a changed run id re-initializes onto the new
 * run ("排播型电视直播" — the client tunes itself to the newest broadcast). */
async function checkForNewRun(): Promise<void> {
  if (!currentRun || document.hidden || switchingVariable) return;
  try {
    const model = selectedModelId;
    const latest = await fetchLatestPointer(dataBaseUrl(), model);
    if (model !== selectedModelId) return;
    if (currentRun !== null && latest.run !== currentRun) void initialize();
  } catch {
    // Transient poll failures never disturb the running app.
  }
}
window.setInterval(() => void checkForNewRun(), LATEST_POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPlayback();
  else void checkForNewRun();
});
reducedMotion.addEventListener("change", () => {
  if (reducedMotion.matches) stopPlayback();
  if (windLayer) windLayer.animate = !reducedMotion.matches;
});

// Stats visibility sticks across visits, like the timeline expansion.
try {
  if (localStorage.getItem(STATS_VISIBLE_KEY) === "1") setStatsVisible(true);
} catch {
  // Panel just starts hidden.
}

updateTransport();
buildTicks(FRAME_COUNT);
resetPreloadCard(FRAME_COUNT);
map.once("load", () => {
  applyBasemapTheme();
  void initialize();
});
